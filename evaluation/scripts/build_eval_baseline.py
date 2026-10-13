"""Rebuild a provenance-aware inventory and descriptive baseline without generating anything."""
from __future__ import annotations
import argparse,csv,json,statistics,sys
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from asset_scanner import Assets,ROOT,EVAL,RUNTIME_DB,files_in,gid,sha,read_json,finite,instant,protection_snapshot

DISCLAIMER='Historical descriptive statistics are not strict out-of-sample predictive performance; LLM parameters may contain historical result contamination.'

def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
def jsonl(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8') as f:
        for row in rows:f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
def csvfile(path,rows):
    fields=list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for row in rows:writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()})
def text(path,value):path.write_text(value.rstrip()+'\n',encoding='utf-8')

def market_for(p):
    ref=p.get('public_market_reference') or (p.get('market_collection') or {}).get('public_market_reference')
    if not isinstance(ref,dict):return None
    identity=ref.get('game_identity') or {};game=p.get('game_identity') or {}
    capture=instant(ref.get('captured_at'));cutoff=instant(p.get('input_cutoff_at'))
    tip=instant(game.get('scheduled_tipoff') or (p.get('input_snapshot') or {}).get('tipoff'))
    valid=bool(capture and cutoff and tip and capture<=cutoff<tip and ref.get('source_url') and str(ref['source_url']).startswith('https://'))
    for key in ('home_team_id','away_team_id'):
        if identity.get(key) is not None and game.get(key) is not None:valid=valid and str(identity[key])==str(game[key])
    if identity.get('game_id'):valid=valid and identity['game_id']==p['game_id']
    supplied=ref.get('expected_home_margin');home_spread=finite(ref.get('home_spread'))
    if finite(supplied) is not None and home_spread is not None:valid=valid and abs(float(supplied)+home_spread)<1e-7
    return ref if valid else None

def verify_research(p):
    envelope=p.get('research') or {};art=(p.get('provenance') or {}).get('research_artifact') or {}
    files=art.get('files') or []
    verified=[]
    for item in files:
        from asset_scanner import resolve
        file=resolve(item.get('path'))
        if file and item.get('sha256')==sha(file):verified.append(str(file))
    trace=envelope.get('trace') or (p.get('analyst_trace') or {}).get('research') or {}
    has_input=False
    def inspect(value):
        nonlocal has_input
        if isinstance(value,dict):
            if isinstance(value.get('messages'),list) and any(isinstance(m,dict) and m.get('role')=='user' and m.get('content') for m in value['messages']):has_input=True
            if value.get('local_context') is not None:has_input=True
            for v in value.values():inspect(v)
        elif isinstance(value,list):
            for v in value:inspect(v)
    inspect(trace)
    for file in verified:
        try:inspect(read_json(file))
        except (OSError,ValueError):pass
    # The real researcher persists request messages beside the hash-bound attempt,
    # not inside trace.json. Verify the saved messages against attempt.input_hash.
    from asset_scanner import canonical_hash
    for file in list(verified):
        try:
            attempt=read_json(file)
            if not attempt.get('input_hash'):continue
            request=Path(file).parent/'search_model_request.json'
            if not request.is_file():continue
            body=read_json(request);messages=body.get('messages')
            if canonical_hash(messages)!=attempt['input_hash']:continue
            context=json.loads(next(m['content'] for m in messages if m.get('role')=='user'))
            if context.get('game',{}).get('game_id')!=p.get('game_id'):continue
            if instant(context.get('cutoff'))!=instant(p.get('input_cutoff_at')):
                # Research cutoff can precede the final input cutoff, never follow it.
                if not (instant(context.get('cutoff')) and instant(p.get('input_cutoff_at')) and instant(context['cutoff'])<=instant(p['input_cutoff_at'])):continue
            if 'local_context' not in context:continue
            has_input=True;verified.append(str(request))
        except (OSError,ValueError,TypeError,StopIteration):pass
    return bool(envelope.get('records')),has_input and bool(verified),verified

def candidates(assets):
    rows=[]
    for key,base in sorted(assets.games.items()):
        if key.startswith('unresolved:'):continue
        r={k:v for k,v in base.items() if not k.startswith('_') and k!='ground_truth'}
        version=assets.selected_predictions.get(key);pgid=assets.selected_postgames.get(key)
        pv=assets.raw_predictions.get(version) or {};p=pv.get('payload') or {}
        pg=assets.raw_postgames.get(pgid) or {};post=pg.get('payload') or {}
        article=(p.get('judgment') or {}).get('analysis_article') or ''
        market=market_for(p) if p else None
        research,research_input,research_refs=verify_research(p) if p else (False,False,[])
        ground=base.get('ground_truth');output=assets.editorials.get(pgid) or {}
        has_pg=bool(output and output.get('valid') and post.get('base_prediction_version_id')==version and
                    post.get('base_prediction_payload_sha256')==pv.get('stored_hash'))
        key=assets.aliases.get(key,key)
        pred=(p.get('judgment') or {}).get('final_prediction') or p.get('prediction') or {}
        winner=pred.get('winner_side');margin=finite(pred.get('expected_margin'));total=finite(pred.get('expected_total'))
        gt_margin=finite((ground or {}).get('actual_home_margin'));gt_total=finite((ground or {}).get('actual_total'))
        spread=finite((market or {}).get('home_spread'));line_total=finite((market or {}).get('total'))
        historical_marker=any(p.get(k) is True for k in ('historical_reconstruction','historical_replay'))
        classification='historical' if historical_marker else 'unknown'
        timing=bool(instant(p.get('published_at')) and instant((p.get('game_identity') or {}).get('scheduled_tipoff')) and
                    instant(p['published_at'])<instant(p['game_identity']['scheduled_tipoff'])) if p else None
        uncertainty=(p.get('analyst_context') or {}).get('missing_data') or []
        explicit_unknown=any((x.get('section') or x.get('topic')) in {'availability','research.availability'} for x in uncertainty if isinstance(x,dict))
        comparison=p.get('spread_comparison') or {};relationship=(comparison.get('final') or {}).get('winner_relationship')
        r.update(game_id=key,season=base.get('season'),game_date=base.get('game_date'),home_team=base.get('home_team'),away_team=base.get('away_team'),
            prediction_version_id=version,postgame_version_id=pgid,has_frozen_prediction=bool(pv.get('valid_hash')),
            has_research=research,has_complete_research_input=research_input,has_pregame_editorial=bool(article),
            has_market_spread=spread is not None,has_market_total=line_total is not None,has_postgame_ground_truth=bool(ground),
            has_boxscore=key in assets.player_box and key in assets.team_box,has_postgame_editorial=has_pg,
            has_injury_report=key in assets.official_injury or key in assets.archive_injury,
            injury_scope='archival_postcutoff_capture' if key in assets.archive_injury else 'official_bound_snapshot' if key in assets.official_injury else None,
            prediction_cutoff=p.get('input_cutoff_at'),prediction_payload_hash=pv.get('stored_hash'),postgame_payload_hash=pg.get('stored_hash'),
            winner_settleable=bool(ground and winner in {'home','away'} and margin is not None and gt_margin is not None),
            ats_settleable=bool(ground and winner in {'home','away'} and margin is not None and spread is not None and gt_margin is not None and abs((margin if winner=='home' else -margin)+spread)>1e-7),
            total_settleable=bool(ground and total is not None and line_total is not None and gt_total is not None and abs(total-line_total)>1e-7),
            actual_home_margin=gt_margin,actual_total=gt_total,close_game=None,blowout=None,
            market_model_disagreement=relationship=='winner_opposed' if relationship in {'winner_opposed','winner_aligned'} else None,
            market_spread_edge=(comparison.get('final') or {}).get('spread_edge'),availability_uncertainty=True if explicit_unknown else None,
            prediction_temporal_class=classification if p else None,reported_pregame_publication=timing,
            frozen_input_exists=bool(p.get('input_snapshot') and p.get('analyst_context')),
            prediction_source_ref=pv.get('source_ref'),postgame_source_ref=pg.get('source_ref'),research_source_refs=research_refs,
            pregame_article_source_ref=(pv.get('source_ref')+'#/judgment/analysis_article') if article else None,
            postgame_article_source_ref=output.get('path'),ground_truth_source_ref=(ground or {}).get('source_ref'),
            historical_ml_forecast=bool(base.get('has_historical_ml_prediction')))
        core=r['has_frozen_prediction'] and r['frozen_input_exists'] and r['has_pregame_editorial'] and r['has_postgame_ground_truth'] and r['has_postgame_editorial']
        r['is_complete_core_chain']=core
        flags=['has_complete_research_input','has_market_spread','has_market_total','has_boxscore','has_injury_report']
        r['is_complete_eval_chain']=core and all(r[f] for f in flags)
        r['missing_data_components']=[f for f in ['has_frozen_prediction','has_pregame_editorial','has_postgame_ground_truth','has_postgame_editorial',*flags] if not r[f]]
        rows.append(r)
    return rows

def runtime_stats(rows,assets):
    by_class={}
    for category in ('historical','forward','unknown'):
        selected=[r for r in rows if r.get('prediction_temporal_class')==category]
        metrics={'games_with_prediction':len(selected),'winner':{'n':0,'correct':0},'ats':{'n':0,'correct':0,'push':0},'total':{'n':0,'correct':0,'push':0}}
        margin_errors=[];total_errors=[]
        for r in selected:
            p=assets.raw_predictions[r['prediction_version_id']]['payload'];prediction=(p.get('judgment') or {}).get('final_prediction') or p.get('prediction') or {}
            if not r['winner_settleable']:continue
            home_margin=float(prediction['expected_margin'])*(1 if prediction['winner_side']=='home' else -1)
            actual=r['actual_home_margin'];expected_total=finite(prediction.get('expected_total'))
            metrics['winner']['n']+=1;metrics['winner']['correct']+=int((actual>0)==(prediction['winner_side']=='home'))
            margin_errors.append(abs(home_margin-actual))
            if expected_total is not None and r['actual_total'] is not None:total_errors.append(abs(expected_total-r['actual_total']))
            market=market_for(p) or {}
            if r['ats_settleable']:
                edge=home_margin+market['home_spread'];result=actual+market['home_spread'];metrics['ats']['n']+=1
                metrics['ats']['push']+=int(abs(result)<1e-7);metrics['ats']['correct']+=int(abs(result)>1e-7 and (edge>0)==(result>0))
            if r['total_settleable']:
                edge=expected_total-market['total'];result=r['actual_total']-market['total'];metrics['total']['n']+=1
                metrics['total']['push']+=int(abs(result)<1e-7);metrics['total']['correct']+=int(abs(result)>1e-7 and (edge>0)==(result>0))
        metrics.update(margin_mae=statistics.mean(margin_errors) if margin_errors else None,margin_mae_n=len(margin_errors),
            total_mae=statistics.mean(total_errors) if total_errors else None,total_mae_n=len(total_errors),
            sample_game_ids=[r['game_id'] for r in selected if r['winner_settleable']],
            definition='Unique game, selected latest Postgame baseline; home-minus-away margin; predictions minus actual, absolute errors. Push excluded from correct.')
        by_class[category]=metrics
    return by_class


def historical_ml_stats(assets):
    """Existing numeric-model test CSVs are retrospective, not LLM or forward evaluation."""
    result={'scope':'historical_numeric_model_dataset_descriptive','winner':{'n':0,'correct':0},
        'ats':{'n':0,'correct':0,'push':0},'total':{'n':0,'correct':0,'push':0}}
    margins=[];totals=[];ids=set();sources=set()
    for r in assets.ml_rows:
        key=gid(r.get('game_id'))
        if key in ids:continue
        probability=finite(r.get('win_probability'));actual_win=finite(r.get('home_win'))
        margin=finite(r.get('predicted_margin'));actual_margin=finite(r.get('point_margin'))
        total=finite(r.get('predicted_total'));actual_total=finite(r.get('total_points'))
        if None in (probability,actual_win,margin,actual_margin,total,actual_total):continue
        ids.add(key);sources.add(r['source_file']);result['winner']['n']+=1
        result['winner']['correct']+=int((probability>=0.5)==bool(actual_win))
        margins.append(abs(margin-actual_margin));totals.append(abs(total-actual_total))
        market_margin=finite(r.get('market_expected_home_margin'));market_total=finite(r.get('market_total'))
        if market_margin is not None and abs(margin-market_margin)>1e-7:
            edge=margin-market_margin;settle=actual_margin-market_margin;result['ats']['n']+=1
            result['ats']['push']+=int(abs(settle)<=1e-7);result['ats']['correct']+=int(abs(settle)>1e-7 and (edge>0)==(settle>0))
        if market_total is not None and abs(total-market_total)>1e-7:
            edge=total-market_total;settle=actual_total-market_total;result['total']['n']+=1
            result['total']['push']+=int(abs(settle)<=1e-7);result['total']['correct']+=int(abs(settle)>1e-7 and (edge>0)==(settle>0))
    result.update(margin_mae=statistics.mean(margins) if margins else None,total_mae=statistics.mean(totals) if totals else None,
        source_files=sorted(sources),winner_label_definition='Existing src/courtvision/metrics.py uses probability >= 0.5; this is not a quality threshold.',
        market_qualification='Archived normalized market references; original quote capture timestamps unverified.',
        disclaimer=DISCLAIMER+' These are numeric model rows, not DeepSeek answer metrics.')
    return result

def curated_doc_cases(assets):
    # Actual recorded repairs, not inferred from a hypothetical test rejection.
    specs=[('docs/qa/2026-10-09-global-capabilities.md','postgame_result_bundle_hash_mismatch','Postgame reader rejected the old receipt-bound result hash',
        '旧Heat的结果bundle','test_non_portfolio_api_readers_candidates_and_restore'),
        ('docs/qa/2026-10-09-global-capabilities.md','official_injury_publication_metadata_mismatch','Official PDF header time differed from nominal filename time',
        '实际遇到文件名','test_pdf_header_time_not_nominal_filename_is_cutoff_basis'),
        ('docs/qa/2026-10-09-global-capabilities.md','same_url_injury_revision_not_refetched','Seen report URL reused an old PDF body',
        '已见URL','test_known_url_is_refetched_and_updated_without_overwriting_history'),
        ('docs/qa/2026-10-09-global-capabilities.md','version_specific_postgame_summary','Public rail used a version-specific summary map',
        'postgameRailReviewByVersion','test_dynamic_settlement_summary'),
        ('docs/qa/2026-10-09-global-capabilities.md','market_display_spread_sign','Home spread inversion and away spread absolute-value formatting',
        '主队盘口符号','test_market_side_line_keeps_true_signed_spread')]
    for relative,code,description,marker,test in specs:
        path=ROOT/relative
        if not path.is_file():continue
        lines=path.read_text(encoding='utf-8').splitlines();found=[(i+1,s) for i,s in enumerate(lines) if marker in s]
        if not found:continue
        refs=[{'source_file':t['source_file'],'name':t['name'],'basis':'explicit recorded repair + related test definition'} for t in assets.tests if t['name']==test]
        passed=any(any(name.startswith(test) for name in run['case_names']) and run['failed_or_error']==0 for run in assets.test_runs)
        assets.failures.append({'case_id':code,'game_id':None,'stage':'postgame' if 'postgame' in code else 'data_projection',
            'prediction_version_id':None,'postgame_version_id':None,'failure_type':code,'severity_if_known':None,'description':description,
            'evidence':{'quoted_report':found[0][1]},'source_file':str(path),'source_locator':f'line {found[0][0]}',
            'root_cause_if_known':found[0][1],'fix_if_known':found[0][1],
            'regression_status_if_known':'passed_recorded_scope' if passed and refs else 'documented_fixed',
            'first_observed_at_if_known':None,'scope':'documented_observed_repair','regression_tests':refs})

def build():
    main=list(files_in(ROOT));external_root=ROOT.parent/'courtvision'
    external=[p for p in files_in(external_root) if p.suffix.lower() in {'.sqlite','.db','.csv'}] if external_root.is_dir() else []
    all_files=main+external;a=Assets()
    dbs=sorted(p for p in all_files if p.suffix.lower() in {'.sqlite','.db'})
    # Read the authoritative namespace crosswalk first.
    dbs.sort(key=lambda p:0 if p.resolve()==RUNTIME_DB.resolve() else 1)
    a.scan_databases(dbs);a.scan_csv(sorted(p for p in all_files if p.suffix.lower()=='.csv'))
    a.scan_json(sorted(p for p in main if p.suffix.lower()=='.json'))
    tests=[p for p in main if p.suffix=='.py' and (p.name.startswith('test_') or p.name.endswith('_test.py'))]
    a.scan_tests(tests,[p for p in main if p.suffix=='.xml']);curated_doc_cases(a)
    # Content-indexed audit/shadow documents; filename placement alone is not evidence.
    support=[]
    for path in main:
        if path.suffix.lower() not in {'.md','.txt'} or path.stat().st_size>2_000_000:continue
        try:
            content=path.read_text(encoding='utf-8-sig')
        except (UnicodeError,OSError):continue
        markers=[word for word in ('audit','shadow','bad case','bad-case','回归','审计','验收') if word in content.lower()]
        if markers:support.append({'path':str(path),'sha256':sha(path),'content_markers':markers})
    rows=candidates(a);reports=EVAL/'reports';reports.mkdir(parents=True,exist_ok=True)
    jsonl(EVAL/'datasets/benchmark_candidates.jsonl',rows);csvfile(EVAL/'datasets/benchmark_candidates.csv',rows)
    jsonl(EVAL/'bad_cases/bad_cases.jsonl',a.failures);csvfile(EVAL/'bad_cases/bad_cases.csv',a.failures)
    latest=max(a.test_runs,key=lambda r:r['mtime']) if a.test_runs else None
    inv={'generated_at':datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).isoformat(),
        'scope':{'main_repository':str(ROOT),'external_accessible_asset_checkout':str(external_root),
                 'excluded':'evaluation, virtualenvs, node_modules, git metadata; no PostgreSQL connection configured by this workspace',
                 'deduplication':'Explicit namespaced IDs and runtime NBA/provider crosswalk only; duplicate CSV contents counted once. Unkeyed market rows are not fabricated games.'},
        'counts':{'accessible_namespaced_games':len(rows),'games_by_season':dict(Counter(str(r['season']) for r in rows)),
            'unresolved_non_game_or_unverified_identity_records':sum(k.startswith('unresolved:') for k in a.games),
            'prediction_versions_authoritative':len(a.raw_predictions),'postgame_versions_authoritative':len(a.raw_postgames),
            'distinct_games_with_frozen_prediction':sum(r['has_frozen_prediction'] for r in rows),
            'complete_core_eval_chains':sum(r['is_complete_core_chain'] for r in rows),
            'complete_all_data_eval_chains':sum(r['is_complete_eval_chain'] for r in rows),
            'games_complete_research_input':sum(r['has_complete_research_input'] for r in rows),
            'games_qualified_frozen_market_spread':sum(r['has_market_spread'] for r in rows),
            'games_qualified_frozen_market_total':sum(r['has_market_total'] for r in rows),
            'games_final_ground_truth':sum(r['has_postgame_ground_truth'] for r in rows),'games_complete_team_and_player_boxscore':sum(r['has_boxscore'] for r in rows),
            'games_official_injury_bound_snapshot':len(a.official_injury),'games_official_injury_archive':len(a.archive_injury),
            'editorial_artifacts_by_kind':dict(Counter(r['kind'] for r in a.json_assets)),
            'editorial_unique_outputs_by_kind':{kind:len({r['article_output_hash'] for r in a.json_assets if r['kind']==kind}) for kind in {r['kind'] for r in a.json_assets}},
            'editorial_current_reader_files_by_kind':dict(Counter(r['kind'] for r in a.json_assets if r['used_by_current_reader'])),
            'games_original_frozen_pregame_article':sum(r['has_pregame_editorial'] for r in rows),
            'games_published_model_postgame_article':sum(r['has_postgame_editorial'] for r in rows),
            'test_definitions':len(a.tests),'test_files':len(tests),'bad_case_records':len(a.failures),
            'content_indexed_audit_shadow_qa_documents':len(support),
            'unkeyed_historical_market_reference_rows':len(a.unkeyed_market_rows)},
        'database_inventory':a.db_inventory,'tabular_sources':a.sources,'editorial_assets':a.json_assets,'audit_shadow_qa_document_index':support,
        'test_categories':dict(Counter(t['category'] for t in a.tests)),'recorded_test_runs':a.test_runs,
        'conflicts':a.conflicts,'backup_unique_version_hash_pairs':len(a.backup_versions),
        'definitions':{'complete_core_chain':'valid immutable prediction + frozen input + original pregame article + final GT + valid same-base model postgame article',
            'complete_eval_chain':'core chain plus complete Research input, frozen qualified spread and total, both team/player boxscore and official injury data',
            'forward':'Not inferred from timestamps alone. Current runtime rows remain unknown until provenance certification.',
            'large_json':'Files over 32MB are recorded with size instead of decoded; authoritative tabular stores provide game counts.',
            'test_results':'Recorded execution receipts, not a fresh production regression run.'},'disclaimer':DISCLAIMER}
    inv['definitions']['boxscore']='Two-team team-stat records plus two-team player facts; played player rows require points, minutes and basic counting stats, not shooting percentages on zero attempts. No possession-by-possession completeness claim.'
    save(reports/'evaluation-asset-inventory.json',inv)
    distribution={'candidate_rows':len(rows),'complete_core_chain':sum(r['is_complete_core_chain'] for r in rows),
        'complete_all_data_chain':sum(r['is_complete_eval_chain'] for r in rows),
        'only_injury_missing':sum(r['is_complete_core_chain'] and r['missing_data_components']==['has_injury_report'] for r in rows),
        'frozen_prediction_missing_market':sum(r['has_frozen_prediction'] and not(r['has_market_spread'] and r['has_market_total']) for r in rows),
        'frozen_prediction_missing_postgame_writer':sum(r['has_frozen_prediction'] and not r['has_postgame_editorial'] for r in rows),
        'frozen_prediction_missing_research_input':sum(r['has_frozen_prediction'] and not r['has_complete_research_input'] for r in rows),
        'missing_component_combinations':dict(Counter('|'.join(r['missing_data_components']) or 'none' for r in rows))}
    save(reports/'candidate-completeness.json',distribution)
    baseline={'generated_at':inv['generated_at'],'prediction_versions':len(a.raw_predictions),'postgame_versions':len(a.raw_postgames),
        'complete_core_chains':distribution['complete_core_chain'],'complete_eval_chains':distribution['complete_all_data_chain'],
        'runtime_ai_descriptive_by_provenance':runtime_stats(rows,a),'original_pregame_articles':inv['counts']['games_original_frozen_pregame_article'],
        'historical_numeric_model_descriptive':historical_ml_stats(a),
        'published_model_postgame_articles':inv['counts']['games_published_model_postgame_article'],
        'test_definitions':len(a.tests),'latest_recorded_test_receipt':latest,'production_tests_rerun_this_task':False,'disclaimer':DISCLAIMER}
    save(reports/'current-eval-baseline.json',baseline)
    for name,body in [('evaluation-asset-inventory',inv['counts']),('current-eval-baseline',{k:v for k,v in baseline.items() if k!='latest_recorded_test_receipt'}),('candidate-completeness',distribution)]:
        text(reports/(name+'.md'),'# '+name+'\n\n```json\n'+json.dumps(body,ensure_ascii=False,indent=2)+'\n```\n\n'+DISCLAIMER+'\n\nFull source references are in evaluation-asset-inventory.json and candidate JSONL. No sampling/rubric/threshold decisions have been made.')
    fixed=sum(bool(c.get('fix_if_known')) for c in a.failures);refs=sum(bool(c['regression_tests']) for c in a.failures)
    verified=sum(c.get('regression_status_if_known')=='passed_recorded_scope' for c in a.failures)
    summary={'records':len(a.failures),'failure_type_counts':dict(Counter(c['failure_type'] for c in a.failures)),
        'scope_counts':dict(Counter(c['scope'] for c in a.failures)),'documented_fixed':fixed,'unknown_fix_status':len(a.failures)-fixed,
        'with_static_regression_reference':refs,'with_recorded_related_passing_test':verified}
    text(reports/'bad-case-inventory.md','# Bad Case Inventory\n\n```json\n'+json.dumps(summary,ensure_ascii=False,indent=2)+'\n```\n\nFailure strings are existing codes, not a new Red Team taxonomy. Static test references do not prove individual regression coverage. Unknown severity/root cause remains null.')
    save(reports/'bad-case-inventory.json',summary)
    smoke=[]
    for r in [row for row in rows if row['has_frozen_prediction']][:3]:
        p=a.raw_predictions[r['prediction_version_id']]['payload'];snapshot=p['input_snapshot'];pred=p['judgment']['final_prediction']
        source=r['prediction_source_ref']
        smoke.append({'sample_id':'environment-smoke-'+r['prediction_version_id'],'game_id':r['game_id'],'stage':'pregame',
            'input_context':{'game':p.get('game_identity'),'prediction':pred,'market':p.get('public_market_reference'),
                'frozen_context_reference':{'source_ref':source+'#/analyst_context','payload_sha256':r['prediction_payload_hash']}},
            'actual_output':{'article':p['judgment'].get('analysis_article'),'prediction':pred},'ground_truth':a.games[r['game_id']].get('ground_truth'),
            'metadata':{'purpose':'environment_smoke_only','quality_label':None,'temporal_class':r['prediction_temporal_class'],'disclaimer':DISCLAIMER},
            'prediction_version':r['prediction_version_id'],'postgame_version':r['postgame_version_id'],'cutoff':r['prediction_cutoff'],'payload_hash':r['prediction_payload_hash']})
    jsonl(EVAL/'datasets/smoke_samples.jsonl',smoke)
    save(EVAL/'label_studio/smoke-import.json',[{'data':{'sample_id':s['sample_id'],'game_id':s['game_id'],
        'actual_output':json.dumps(s['actual_output'],ensure_ascii=False),'purpose':'environment_smoke_only'},'meta':s['metadata']} for s in smoke])
    print(json.dumps({'counts':inv['counts'],'candidate_completeness':distribution,'bad_cases':summary,'runtime_metrics':baseline['runtime_ai_descriptive_by_provenance']},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--protect',choices=['before','after']);args=parser.parse_args()
    (EVAL/'reports').mkdir(parents=True,exist_ok=True)
    if args.protect:
        current=protection_snapshot();save(EVAL/'reports'/('protection-'+args.protect+'.json'),current)
        if args.protect=='after':
            previous=read_json(EVAL/'reports/protection-before.json')
            checks={key:previous[key]==current[key] for key in ('versions','published_artifacts','writer_attempts')}
            save(EVAL/'reports/protection-result.json',checks);print(json.dumps(checks));assert all(checks.values())
    else:build()
