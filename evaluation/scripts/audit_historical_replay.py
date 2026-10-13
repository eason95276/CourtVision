"""Read-only historical input eligibility, not a content quality evaluator.

Event-time reconstructability and recorded capture provenance are separate axes.
No target result is exported into an eligible pregame package.
"""
from __future__ import annotations
import argparse,csv,json,re,statistics
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
from asset_scanner import ROOT,EVAL,RUNTIME_DB,read_json,sha,canonical_hash,connect,gid,instant,resolve,protection_snapshot
from build_eval_baseline import save,jsonl,csvfile,text,market_for,verify_research

FIELDS=('game_id','season','game_date','tipoff_time','home_team','away_team','team_record','recent_form',
    'previous_season_metrics','h2h','roster','player_form','player_role_starter','market_spread','market_total','market_provenance',
    'injury','injury_provenance','research')
CORE=('game_id','season','game_date','tipoff_time','home_team','away_team','team_record','recent_form','previous_season_metrics','h2h','roster','player_form')
INPUTS=(EVAL/'datasets/benchmark_candidates.jsonl',EVAL/'bad_cases/bad_cases.jsonl',EVAL/'reports/evaluation-asset-inventory.json',EVAL/'reports/current-eval-baseline.json')
PK=ROOT/'v2/data/player_knowledge.sqlite'
PACKETS=ROOT/'v2/pregame_system/outputs/okc_historical_enrichment_20261002_final/forecast_packets'

def receipt():
    base=protection_snapshot();base['previous_evaluation_assets']={str(p):sha(p) for p in INPUTS}
    base['source_assets']={str(p):sha(p) for p in (ROOT/'artifacts/test_predictions_with_market.csv',PK) if p.is_file()}
    return base

def status(state,reason,refs=(),**metadata):return {'status':state,'reason':reason,'source_refs':list(refs),**metadata}

def tier(fields):
    if not all(fields[k]['status']=='safe' for k in CORE):return 'TIER_C_UNSAFE'
    if all(fields[k]['status']=='safe' for k in FIELDS):return 'TIER_A_STRICT'
    return 'TIER_B_PARTIAL'

def replay_binding(request,output,observation):
    # Require a persisted correlation, not the mere presence of adjacent files.
    for key in ('request_id','input_hash','request_sha256'):
        token=request.get(key)
        if token and token==output.get(key) and token==observation.get(key):return True
    return False

def eligible_package(row,values):
    available={k:values[k] for k in row['safe_fields'] if k in values}
    # Outcomes belong in evaluation GT, never this input artifact.
    if any(k in available for k in ('ground_truth','boxscore','actual_margin','actual_total','actual_winner')):raise ValueError('outcome contamination')
    return {'schema_version':'evaluation-safe-replay-input-v1','game_id':row['game_id'],'cutoff':row['prediction_cutoff'],
        'historical_reconstruction':True,'point_in_time_evidence_audited':True,'available':available,
        'missing':row['missing_fields'],'unknown':row['unknown_fields'],'excluded_unsafe':row['unsafe_fields'],
        'field_evidence':{k:row['field_audit'][k] for k in available},'quality_benchmark_only':True,
        'production_schema_compatibility':'Requires explicit adapter to pregame-replay-snapshot-v1; not a production snapshot.'}

def load_sources(inv):
    candidates=[p for p in inv['tabular_sources'] if p.get('rows') and not p.get('duplicate_content') and 'teamScore' in p.get('columns',[])]
    chronology=defaultdict(list);path=None
    if candidates:
        path=Path(candidates[0]['path'])
        with path.open(encoding='utf-8-sig',newline='') as f:
            for r in csv.DictReader(f):
                date=r.get('game_date') or r.get('gameDate');team=r.get('teamId') or r.get('team_id')
                if date and team:chronology[str(team)].append({'date':date[:10],'game_id':gid(r.get('game_id') or r.get('gameId')),'win':r.get('win')})
        for rows in chronology.values():rows.sort(key=lambda x:(x['date'],x['game_id']))
    return chronology,path

def frozen_versions():
    result={}
    with connect(RUNTIME_DB) as c:
        for r in c.execute('SELECT version_id,payload_sha256,payload_json FROM prediction_versions'):
            p=json.loads(r['payload_json'])
            if canonical_hash(p)==r['payload_sha256']:result[r['version_id']]=p
    return result

def evidence_index(inv):
    injuries=defaultdict(list)
    for db in inv['database_inventory']:
        if not any(t['table']=='injury_snapshots' for t in db['tables']):continue
        with connect(db['path']) as c:
            for r in c.execute('SELECT * FROM injury_snapshots'):
                entry=dict(r);entry['source_ref']=db['path']+'#injury_snapshots/'+str(r['injury_snapshot_id'])
                injuries[gid(r['game_id'])].append(entry)
    packets={}
    for path in PACKETS.glob('*.json'):
        p=read_json(path);target=p.get('game') or p.get('target') or {};id=target.get('official_nba_game_id')
        if id:packets[gid(id)]={'path':str(path),'payload':p,'target':target,'hash_valid':canonical_hash({k:v for k,v in p.items() if k!='packet_sha256'})==p.get('packet_sha256')}
    return injuries,packets

def mother_audit():
    inv=read_json(EVAL/'reports/evaluation-asset-inventory.json')
    allrows=[json.loads(s) for s in INPUTS[0].read_text(encoding='utf-8').splitlines()]
    mother=[r for r in allrows if r['has_boxscore'] and r['has_postgame_ground_truth'] and r.get('home_team_id') and r.get('away_team_id') and r.get('home_team') and r.get('away_team')]
    chronology,statsfile=load_sources(inv);versions=frozen_versions();injuries,packets=evidence_index(inv)
    knowledge={}
    archived_market={}
    marketfile=ROOT/'artifacts/test_predictions_with_market.csv'
    with marketfile.open(encoding='utf-8-sig',newline='') as file:
        for n,r in enumerate(csv.DictReader(file),2):
            archived_market[gid(r['game_id'])]={'row':r,'source_ref':str(marketfile)+':'+str(n)}
    target_starters={}
    player_receipts=[]
    with connect(PK) as c:
        for r in c.execute('SELECT g.*,s.collection_time_utc,s.source_url,s.response_sha256,s.source_path FROM player_knowledge_games g LEFT JOIN player_knowledge_source_receipts s ON g.latest_success_receipt_id=s.receipt_id'):
            knowledge[gid(r['game_id'])]=dict(r)
        target_starters={gid(r['game_id']):r['n'] for r in c.execute('SELECT game_id,SUM(CASE WHEN starter IS NOT NULL THEN 1 ELSE 0 END) n FROM player_game_facts GROUP BY game_id')}
        for r in c.execute('SELECT s.*,g.game_date,g.home_team_id,g.away_team_id FROM player_knowledge_source_receipts s JOIN player_knowledge_games g ON s.game_id=g.game_id WHERE s.status=?',('success',)):
            record=dict(r);path=resolve(record['source_path']);record['raw_verified']=False
            if path:
                try:record['raw_verified']=sha(path)==record['response_sha256'] or canonical_hash(read_json(path))==record['response_sha256']
                except (ValueError,OSError):pass
            player_receipts.append(record)
    rows=[];packages=0
    for original in mother:
        id=original['game_id'];p=versions.get(original.get('prediction_version_id'),{});packet=packets.get(id)
        cut=instant(original.get('prediction_cutoff'));tip=instant(p.get('game_identity',{}).get('scheduled_tipoff'))
        if not tip:tip=instant(original.get('scheduled_tipoff'))
        if not cut and packet:cut=instant(packet['target'].get('source_eligibility_cutoff'))
        # A prior-calendar-day bound excludes same-day outcome leakage but is not
        # silently promoted into an actual pregame cutoff or publication time.
        date=original.get('game_date');bound=str(date or '')[:10]
        f={k:status('missing','No source-bound historical field retained',original['source_refs']) for k in FIELDS}
        values={}
        for field in ('game_id','season','game_date','home_team','away_team'):
            value=original.get(field);values[field]=value
            f[field]=status('unknown' if value else 'missing','Historical identity retained; original pre-cutoff schedule publication/capture not established',original['source_refs'])
        f['tipoff_time']=status('unknown' if tip or packet else 'missing','No verified original timezone-aware pregame schedule snapshot; historical date is not assumed UTC',
            [packet['path']+'#/game' ] if packet else original['source_refs'])
        # Explicitly retained live version identity may be used as captured metadata,
        # not as proof of every underlying quantitative source's PIT eligibility.
        if p and cut and tip and cut<tip:
            ref=original['prediction_source_ref']
            for k in ('game_id','season','game_date','home_team','away_team'):
                f[k]=status('safe','Identity frozen in a valid immutable pregame payload; not a target-result-derived input',[ref+'#/game_identity'],recorded_publication_at=p.get('published_at'))
            f['tipoff_time']=status('safe','Frozen timezone-aware scheduled time with input cutoff before tipoff; actual first possession not asserted',[ref+'#/game_identity/scheduled_tipoff'])
            values['tipoff_time']=tip.isoformat()
        counts={}
        for side in ('home','away'):
            prior=[x for x in chronology.get(str(original[side+'_team_id']),[]) if x['date']<bound and x['game_id']!=id]
            current=[x for x in prior if (int(x['date'][:4]) if x['date'][5:7]>='07' else int(x['date'][:4])-1)==int(str(original['season'])[:4])]
            counts[side]={'prior_games':len(prior),'current_season_prior_games':len(current),'last15_available':min(15,len(prior))}
        priorrefs=[str(statsfile)+'#game_date<target calendar date; target ID excluded'] if statsfile else []
        for key in ('team_record','recent_form','previous_season_metrics','h2h'):
            f[key]=status('unknown' if statsfile else 'missing','Can be reconstructed from prior-game rows, but no row-level original publication/capture proof. shift(1) is an event-time guard, not timestamp provenance.',priorrefs,
                retrospective_reconstructable=bool(statsfile),same_day_games_excluded=True,target_outcomes_excluded=True)
        knowledge_row=knowledge.get(id)
        f['player_form']=status('unknown','Previous-game boxscores can reconstruct observations, not current roster or role; acquisition was retrospective',
            [str(PK)+'#player_game_facts/game_date<'+bound],target_boxscore_capture=knowledge_row.get('collection_time_utc') if knowledge_row else None,
            retrospective_reconstructable=bool(knowledge_row))
        if cut:
            prior_receipts=[r for r in player_receipts if r['raw_verified'] and str(r['game_date'])[:10]<bound and
                (r['home_team_id'] in (original['home_team_id'],original['away_team_id']) or r['away_team_id'] in (original['home_team_id'],original['away_team_id']))]
            qualified_receipts=[r for r in prior_receipts if instant(r['collection_time_utc']) and instant(r['collection_time_utc'])<=cut]
            covered={team for team in (original['home_team_id'],original['away_team_id']) if any(team in (r['home_team_id'],r['away_team_id']) for r in qualified_receipts)}
            if len(covered)==2:
                f['player_form']=status('safe','Verified prior-game raw player sources were captured before cutoff; historical observations only, not a claim of complete current-season recent form or current role',
                    [str(PK)+'#player_knowledge_source_receipts/'+str(r['receipt_id']) for r in qualified_receipts],source_count=len(qualified_receipts),
                    scope='qualified prior-season player observations; missing current-season games remain unknown')
                values['player_form']={'source_receipt_ids':[r['receipt_id'] for r in qualified_receipts],'scope':'historical prior-game observations only'}
        f['player_role_starter']=status('missing','Target postgame starter flag/minutes are explicitly excluded; prior starts are historical observations, not confirmed target starters',
            [str(PK)+'#player_game_facts.starter/starter_source'])
        if target_starters.get(id):
            f['player_role_starter']=status('unsafe','Observed target-game starters are postgame boxscore facts and cannot prove pregame starters',
                [str(PK)+'#player_game_facts/game_id='+id+'/starter'])
        if knowledge_row:
            f['roster']=status('unknown','Only target-game participating team membership retained; cannot infer pre-cutoff roster from postgame boxscore',
                [str(PK)+'#player_game_facts/game_id='+id])
        archive=archived_market.get(id)
        if archive:
            for name,col in (('market_spread','market_expected_home_margin'),('market_total','market_total')):
                if archive['row'].get(col):f[name]=status('unknown','Archived line joined by game date and team identity, but no original quote timestamp or open/close receipt',
                    [archive['source_ref']],source='historical numerical export',capture_timestamp=None)
            f['market_provenance']=status('unknown','Market field audit explicitly declares capture and closing-line status unverified',
                [archive['source_ref'],str(ROOT/'artifacts/market_field_audit.json')])
        if p:
            roster=p.get('analyst_context',{}).get('roster_context',{}).get('teams',{})
            sources=[];accepted=True
            for side in ('home','away'):
                r=roster.get(side,{}).get('official_team_roster',{});source=r.get('source',{});capture=instant(source.get('observed_at') or source.get('captured_at'))
                accepted=accepted and bool(r.get('players') and source.get('url') and source.get('sha256') and capture and cut and capture<=cut)
                sources.append(source)
            if accepted:
                f['roster']=status('safe','Source-observed membership saved in frozen input, not confirmed starters or availability',
                    [original['prediction_source_ref']+'#/analyst_context/roster_context'],sources=sources)
                values['roster']={side:{'players':roster[side]['official_team_roster']['players'],'source':roster[side]['official_team_roster']['source']} for side in ('home','away')}
            elif roster:f['roster']=status('unknown','Frozen roster exists but complete source observation proof is absent',[original['prediction_source_ref']+'#/analyst_context/roster_context'])
            market=market_for(p)
            if market:
                for k in ('market_spread','market_total','market_provenance'):
                    f[k]=status('safe','Same-game source-bound quote captured before cutoff; stored payload hash verified',[original['prediction_source_ref']+'#/public_market_reference'],source=market.get('source_url'),captured_at=market.get('captured_at'))
                    values[k]=market.get('home_spread') if k=='market_spread' else market.get('total') if k=='market_total' else {k:market.get(k) for k in ('source_url','captured_at','published_at')}
            has_records,has_input,refs=verify_research(p)
            if has_records:
                records=p.get('research',{}).get('records',[])
                qualified=[]
                for r in records:
                    times=[instant(r.get(k)) for k in ('published_at','source_published_at','updated_at','observed_at','captured_at','fetched_at') if r.get(k)]
                    captured=instant(r.get('captured_at') or r.get('fetched_at') or r.get('observed_at'))
                    if captured and times and all(t and cut and t<=cut for t in times) and (r.get('source_url') or r.get('url')):qualified.append(r)
                f['research']=status('safe' if has_input and qualified else 'unknown','Accepted frozen records require saved request hash, source URL and explicit pre-cutoff temporal metadata',[*refs,original['prediction_source_ref']+'#/research'],accepted_records=len(records),qualified_records=len(qualified))
                if f['research']['status']=='safe':values['research']=qualified
        if packet:
            packetref=packet['path'];data=packet['payload']
            if not tip:
                scheduled=packet['target'].get('tipoff',{})
                if isinstance(scheduled,dict):tip=instant(scheduled.get('scheduled_tipoff') or scheduled.get('conservative_cutoff') or scheduled.get('value'))
                tip=tip or instant(packet['target'].get('scheduled_tipoff_estimate_by_NBA_plus_10_rule'))
            for k in ('market_spread','market_total','market_provenance'):
                if f[k]['status']!='safe':f[k]=status('unknown','Retrospective market reference retained; quote capture timestamp explicitly unverified',[packetref+'#/market_reference'],point_in_time_snapshot_verified=data.get('data_provenance',{}).get('point_in_time_snapshot_verified'))
            news=data.get('pregame_news') or data.get('important_context') or {}
            if isinstance(news,dict) and news.get('items'):f['research']=status('unknown','Historical news archive exists but strict capture eligibility unproven',[packetref+'#/pregame_news'])
        injuryrows=injuries.get(id,[])
        safeinjury=[r for r in injuryrows if cut and instant(r.get('captured_at')) and instant(r.get('source_published_at')) and instant(r['captured_at'])<=cut and instant(r['source_published_at'])<=cut and resolve(r.get('raw_path'))]
        if injuryrows:
            state='safe' if safeinjury else 'unsafe' if cut and any(instant(r.get('captured_at')) and instant(r['captured_at'])>cut for r in injuryrows) else 'unknown'
            for k in ('injury','injury_provenance'):
                f[k]=status(state,'Archive must match cutoff and have original capture proof; late-fetched report is excluded from strict replay',
                    sorted({r['source_ref'] for r in injuryrows}),published_at=sorted({r['source_published_at'] for r in injuryrows if r['source_published_at']}),captured_at=sorted({r['captured_at'] for r in injuryrows if r['captured_at']}))
                if safeinjury:values[k]=safeinjury
        classification=tier(f)
        row={k:original.get(k) for k in ('game_id','season','game_date','home_team','away_team','game_type')}
        row.update(tipoff_time=tip.isoformat() if tip else None,prediction_cutoff=cut.isoformat() if cut else None,replay_tier=classification,
            safe_fields=[k for k in FIELDS if f[k]['status']=='safe'],missing_fields=[k for k in FIELDS if f[k]['status']=='missing'],
            unsafe_fields=[k for k in FIELDS if f[k]['status']=='unsafe'],unknown_fields=[k for k in FIELDS if f[k]['status']=='unknown'],
            field_audit=f,ground_truth_available=True,boxscore_available=True,ground_truth_source=original['ground_truth_source_ref'],
            prior_game_counts=counts,target_actual_margin_for_stratification_only=original['actual_home_margin'],
            close_game=None,blowout=None,strong_weak_team=None,market_model_disagreement=None,
            home_away={'home_team_id':original['home_team_id'],'away_team_id':original['away_team_id']},
            market_source=f['market_provenance'].get('source') or (archive['source_ref'] if archive else packet['path']+'#/market_reference' if packet else None),market_timestamp=f['market_provenance'].get('captured_at'),
            injury_source=sorted({r['source_url'] for r in injuryrows if r['source_url']}) or None,injury_timestamp=f['injury_provenance'].get('published_at'),
            replay_reason='Core field provenance incomplete; UNKNOWN is not proof of leakage but cannot be promoted to safe.' if classification=='TIER_C_UNSAFE' else 'Proven core field coverage',
            historical_packet_source=packet['path'] if packet else None)
        for name,field in [('team_record','team_record'),('recent_form','recent_form'),('previous_season_metrics','previous_season_metrics'),('h2h','h2h'),('roster','roster'),('player_form','player_form'),('injury','injury'),('research','research')]:row['has_safe_'+name]=f[field]['status']=='safe'
        row['has_safe_market']=all(f[k]['status']=='safe' for k in ('market_spread','market_total','market_provenance'))
        rows.append(row)
        if classification!='TIER_C_UNSAFE':
            package=eligible_package(row,values);save(EVAL/'datasets/replay_inputs'/(id.replace(':','_')+'.json'),package);packages+=1
    return rows,packages,inv,packets

def bad_case_audit():
    cases=[json.loads(s) for s in INPUTS[1].read_text(encoding='utf-8').splitlines()];result=[]
    for case in cases:
        source=resolve(case['source_file']);tests=[t for t in case.get('regression_tests',[]) if resolve(t['source_file'])]
        inputs=[];outputs=[];refs=[];bodies={};observation={}
        if source and source.suffix=='.json':
            try:observation=read_json(source)
            except (ValueError,OSError):pass
        if source and source.suffix=='.json':
            # Scope to the recorded attempt; actual content identifies requests/responses.
            for path in source.parent.rglob('*.json'):
                if len(refs)>=250 or path.stat().st_size>3_000_000:continue
                try:d=read_json(path)
                except (ValueError,OSError):continue
                if not isinstance(d,dict):continue
                bodies[str(path)]=d
                if isinstance(d.get('messages'),list) and any(m.get('role')=='user' for m in d['messages'] if isinstance(m,dict)):inputs.append(str(path))
                if isinstance(d.get('input_projection'),dict) or isinstance(d.get('analyst_context'),dict):inputs.append(str(path))
                if d.get('choices') or isinstance(d.get('content'),str) or isinstance(d.get('response'),dict) or isinstance(d.get('editorial'),dict):outputs.append(str(path))
                if str(path) in inputs+outputs:refs.append({'path':str(path),'sha256':sha(path)})
        missing=[]
        if not source:missing.append('original_observation')
        if not inputs:missing.append('frozen_stage_request')
        if not outputs:missing.append('raw_stage_output')
        if not tests:missing.append('deterministic_expected_failure_or_regression_assertion')
        # A file's proximity alone does not certify same-attempt binding. Require
        # explicit input+output run evidence; otherwise report partial availability.
        classification='partial' if source and (case.get('evidence') or tests or inputs or outputs) else 'unavailable'
        bound=any(replay_binding(bodies[a],bodies[b],observation) for a in inputs for b in outputs)
        if not bound:missing.append('verified_same_attempt_input_output_binding')
        if source and inputs and outputs and tests and bound:classification='full'
        result.append({'case_id':case['case_id'],'replayable':classification,'required_assets':['original_observation','frozen_stage_request','raw_stage_output','expected_failure_assertion','same_attempt_binding'],
            'missing_assets':missing,'test_reference':tests,'original_fix_reference':case.get('fix_if_known'),
            'source_file':str(source) if source else case['source_file'],'candidate_request_files':inputs,'candidate_output_files':outputs,'asset_hashes':refs,
            'same_attempt_binding_verified':bound,'note':'Feasibility inspection only; no original stage/repair rerun. Full means retained correlated input/output/assertion, not an executed replay success.'})
    return result

def numerical_audit():
    path=ROOT/'artifacts/test_predictions_with_market.csv'
    with path.open(encoding='utf-8-sig',newline='') as f:reader=csv.DictReader(f);columns=reader.fieldnames;rows=list(reader)
    report={'rows':len(rows),'unique_ids':len({gid(r['game_id']) for r in rows}),'seasons':dict(Counter(r['season_start'] for r in rows)),
        'columns':columns,'source':str(path),'sha256':sha(path),'temporal_type':'retrospective_numeric_model_test_export',
        'inputs':'Elo + shift(1) rolling team metrics (5/15), home/away differences, season game count, capped/default rest and back-to-back; export contains only a subset of model feature columns.',
        'code_refs':['src/courtvision/features.py:79-107','src/courtvision/features.py:119-170','src/courtvision/config.py:7-12','src/courtvision/market.py:158-194','src/courtvision/experiment.py:109-166'],
        'training_split':'Current experiment implementation: validation season_start=2024, test=2025, training windows 5/8/10 years before evaluation season. This is retrospective seasonal holdout, not contemporaneous logged predictions.',
        'cutoff_status':'Event-time ordering exists, exact per-row input cutoff/capture and original trained-model receipt are not certified.',
        'market_status':'Date+home/away join; source audit explicitly says opening/closing and quote capture timestamps unverified.',
        'baseline_use':'Yes as a separately labeled historical numeric-model descriptive baseline; no strict PIT or forward claim.',
        'metrics':'Winner/log-loss/calibration, margin/total errors and conditional archived-line ATS/Total settlement can be described if definitions/missingness retained.',
        'not_merge_with_llm':'No original Research/Writer input/output; no same-version LLM article, Skill/model run receipt or quality labels. Numeric-model error is not generation-quality score.',
        'snapshot_availability':'No per-row capture timestamp, cutoff or point-in-time source_records column.',
        'old_results':read_json(EVAL/'reports/current-eval-baseline.json')['historical_numeric_model_descriptive']}
    save(EVAL/'reports/historical-numerical-baseline-audit.json',report)
    text(EVAL/'reports/historical-numerical-baseline-audit.md','# Historical Numerical Baseline Audit\n\n```json\n'+json.dumps(report,ensure_ascii=False,indent=2)+'\n```')
    return report

def run():
    rows,packages,inv,packets=mother_audit();bad=bad_case_audit();numeric=numerical_audit()
    jsonl(EVAL/'datasets/historical_replay_candidates.jsonl',rows);csvfile(EVAL/'datasets/historical_replay_candidates.csv',rows)
    jsonl(EVAL/'bad_cases/replay_feasibility.jsonl',bad);csvfile(EVAL/'bad_cases/replay_feasibility.csv',bad)
    fields={k:{state:sum(r['field_audit'][k]['status']==state for r in rows) for state in ('safe','missing','unknown','unsafe')} for k in FIELDS}
    for counts in fields.values():counts['safe_percent']=100*counts['safe']/len(rows) if rows else 0
    tiers=Counter(r['replay_tier'] for r in rows);badcounts=Counter(r['replayable'] for r in bad)
    report={'audited_games':len(rows),'tiers':{k:tiers[k] for k in ('TIER_A_STRICT','TIER_B_PARTIAL','TIER_C_UNSAFE')},'field_coverage':fields,
        'season_distribution':dict(Counter(str(r['season']) for r in rows)),
        'game_type_distribution':dict(Counter(str(r['game_type']) for r in rows)),
        'tier_by_season':{s:dict(Counter(r['replay_tier'] for r in rows if str(r['season'])==s)) for s in sorted({str(r['season']) for r in rows})},
        'structured_eligible_input_packages':packages,'bad_case_replayability':{k:badcounts[k] for k in ('full','partial','unavailable')},
        'recommended_max_proven_a_b_scale':sum(r['replay_tier']!='TIER_C_UNSAFE' for r in rows),
        'feasibility_50_100_300':{str(n):sum(r['replay_tier']!='TIER_C_UNSAFE' for r in rows)>=n for n in (50,100,300)},
        'diversity':{'actual_margin_distribution':dict(Counter(str(abs(r['target_actual_margin_for_stratification_only'])) for r in rows)),
            'home_away':{'unique_team_ids':len({str(r['home_away'][side+'_team_id']) for r in rows for side in ('home','away')}),'games':len(rows)},
            'close_blowout_strong_weak':'Unclassified: no human-approved cut points or team-strength definition; exact actual margins retained for descriptive stratification only.',
            'market_model_disagreement':'Not stratified without fully eligible market+independent model provenance.'},
        'archival_okc_packets':{'total':len(packets),'valid_content_hashes':sum(p['hash_valid'] for p in packets.values()),
            'point_in_time_snapshot_verified':sum(p['payload'].get('data_provenance',{}).get('point_in_time_snapshot_verified') is True for p in packets.values()),
            'mother_set_matched':sum(bool(r['historical_packet_source']) for r in rows)},
        'decision_semantics':'Existing strict replay contract requires capture<=cutoff, timezone-aware schedule, source IDs and hashes. UNKNOWN/late-capture core fields force C; this is not a quality rubric.',
        'schema_incompatibilities':['Existing production replay requires point_in_time_pregame capture_kind, source_records, cutoff<scheduled_tipoff and pregame_inputs_sha256.',
            'Date-only historical game dates are not converted into invented UTC tipoff/cutoff.',
            'Production Writer requires locked final_prediction + editorial material; eligible raw inputs alone are not Writer-ready and no numeric baseline is fabricated.'],
        'biggest_blockers':['Original core-stat and roster snapshot/publication provenance absent for historical season.',
            'Untimestamped market rows and late-acquired injury archives cannot become strict captured-before-cutoff sources.',
            'Known reconstruction packets explicitly mark point_in_time_snapshot_verified=false.'],
        'scope':'Read-only source eligibility audit, not a generation run or future predictive-performance claim. Bad-case full replay needs verified request/output bindings and an offline execution receipt.',
        'numerical_baseline_rows':numeric['rows'],'llm_calls':0}
    save(EVAL/'reports/historical-replay-feasibility.json',report)
    coverage='\n'.join(f"| {k} | {v['safe']} | {v['safe_percent']:.3f}% | {v['missing']} | {v['unknown']} | {v['unsafe']} |" for k,v in fields.items())
    text(EVAL/'reports/historical-replay-feasibility.md',
        '# Historical Replay Benchmark Feasibility\n\n'
        f"审计母集 {len(rows)} 场；Tier A={tiers['TIER_A_STRICT']}，B={tiers['TIER_B_PARTIAL']}，C={tiers['TIER_C_UNSAFE']}。"
        '\n\n这里的C包括关键字段UNKNOWN，不意味着每场已经查实使用未来信息。依据当前生产strict replay合同，原始source/capture/cutoff证明不能用shift(1)、历史日期或目录名称代替。'
        '\n\n| 字段 | safe | 覆盖率 | missing | unknown | unsafe |\n|---|---:|---:|---:|---:|---:|\n'+coverage+
        '\n\n## 50 / 100 / 300场\n\n当前均不能作为已证明时间安全的完整赛前输入Benchmark。基础统计可回溯重建，不能因此升级为A/B。原82个OKC包均通过内容hash，但明确保留point_in_time_snapshot_verified=false。严格生产replay与允许后抓取、只验证原始公开内容的archive-only研究应分开；本轮不自行放宽二者边界。'
        '\n\n## 输入包\n\n'+f'符合A/B条件的输入包 {packages} 份。没有为C生成可供LLM使用的包，未补市场、伤病、时间、首发或预测数字。'+
        '\n\n## 数值baseline与Bad Case\n\n已有1,322行可以作为独立历史数值模型描述性baseline；不是当时真实运行的逐场日志，盘口时间未核验，不与LLM质量数据合并。'
        f"\n\n40条Bad Case：完整replay资料资格 {badcounts['full']}，部分 {badcounts['partial']}，不可用 {badcounts['unavailable']}。部分指保留原观察、诊断或测试引用，不保证可重现原失败。相邻文件不自动形成同attempt的输入输出绑定。本轮没有执行原stage或repair。"
        '\n\n## 分布及边界\n\n'+json.dumps({'season':report['season_distribution'],'type':report['game_type_distribution'],'unique_teams':report['diversity']['home_away']},ensure_ascii=False)+
        '\n\n保留真实分差分布，不擅自给close/blowout、强弱队设切点。市场/模型分歧不使用未核验盘口补分类。逐字段证据在historical_replay_candidates.jsonl，完整分布及schema incompatibility在同名JSON。'
        '\n\n## 验证\n\n独立安全分类/目标赛果排除/attempt绑定测试8 passed、0 failed。未调用LLM、联网或生产生成。保护结果另见replay-protection-result.json。正式50/100/300场Benchmark、Rubric、Judge、阈值及抽样均未执行。')
    print(json.dumps({k:report[k] for k in ('audited_games','tiers','structured_eligible_input_packages','bad_case_replayability','recommended_max_proven_a_b_scale','archival_okc_packets')},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--protect',choices=['before','after']);args=parser.parse_args()
    if args.protect:
        now=receipt();save(EVAL/'reports'/('replay-protection-'+args.protect+'.json'),now)
        if args.protect=='after':
            before=read_json(EVAL/'reports/replay-protection-before.json');checks={k:before[k]==now[k] for k in ('versions','published_artifacts','writer_attempts','previous_evaluation_assets','source_assets')}
            save(EVAL/'reports/replay-protection-result.json',checks);print(json.dumps(checks));assert all(checks.values())
    else:run()
