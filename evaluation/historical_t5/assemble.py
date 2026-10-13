"""Freeze three reconstruction contexts. No model/API/publication hook."""
import json,shutil,re
from datetime import datetime,timezone
from pathlib import Path
from .metadata import ROOT,AUDIT,SOURCE_GAMES,SOURCE_TEAMS,PK
from .validation import validate_context
from .research import extract_lineups,roster_from_history,primary_body
from .market import existing_market
from evaluation.research_shadow.core import sha
from evaluation.research_shadow.runner import read,write,protection

OUTPUT=ROOT/'evaluation/historical_t5/pilot-v1'
def envelope(value,source,*,event_time=None,published_at=None,captured_at=None,grade='TEMPORAL_A',confidence='derived_canonical_prior_event_history_not_as_was_capture'):
    return {'value':value,'source':source,'event_time':event_time,'published_at':published_at,
            'captured_at':captured_at or datetime.now(timezone.utc).isoformat(),'temporal_grade':grade,'confidence':confidence}
def coverage(ctx):
    out={};v=lambda group:ctx[group]['value']
    out['team_record']='COMPLETE'if all(v('team_records')[s]['observed_n']>0 and v('team_records')[s]['observed_n']==v('team_records')[s].get('expected_observed_games')for s in('home','away'))else'PARTIAL'
    out['recent_form']='COMPLETE'if all(v('recent_form')[s]['recent_10']['observed_n']>=10 for s in('home','away'))else'PARTIAL'if any(v('recent_form')[s]['recent_5']['observed_n']for s in('home','away'))else'UNAVAILABLE'
    out['previous_season']='COMPLETE'if all(v('previous_season_metrics')[s]['full_82_games_observed']for s in('home','away'))else'PARTIAL'if any(v('previous_season_metrics')[s]['observed_n']for s in('home','away'))else'UNAVAILABLE'
    out['H2H']='COMPLETE'if v('head_to_head')['observed_n']>=10 else'PARTIAL'if v('head_to_head')['observed_n']else'UNAVAILABLE'
    out['rest']='COMPLETE'if all(v('schedule_context')[s]and v('schedule_context')[s].get('days_rest')is not None for s in('home','away'))else'PARTIAL'
    out['player_history']='COMPLETE'if all(v('player_history')[s]['players']and v('player_history')[s]['observed_team_games']==min(10,v('team_records')[s]['observed_n'])for s in('home','away'))else'PARTIAL'
    out['roster']='PARTIAL'if any(v('roster')[s]['players']for s in('home','away'))else'UNAVAILABLE'
    out['market']=v('market')['status'];out['injury']=v('injury')['status']
    ls=v('starting_lineup')['teams']
    for key,basis in(('confirmed_lineup','confirmed_before_cutoff'),('expected_lineup','source_expected')):
        n=sum(r['basis']==basis for r in ls);out[key]='COMPLETE'if n==2 else'PARTIAL'if n else'UNAVAILABLE'
    out['research']='PARTIAL'if v('research')['evidence']else'UNAVAILABLE'
    return out

def compare_closedbook(sample,ctx):
    path=(ROOT/'evaluation'/sample['context_path']).resolve()
    if not path.is_relative_to(ROOT/'evaluation/datasets/closed_book_contexts'):raise ValueError('closedbook_path_not_model_visible')
    raw=path.read_bytes()
    old=json.loads(raw)
    canonical=sha(json.dumps(old,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False))
    if canonical!=sample['context_sha256']:raise ValueError('closedbook_context_hash_mismatch')
    v=lambda group:ctx[group]['value']
    current={
      'injury':{'old':old.get('injury',{}).get('status'),'new':v('injury')['status'],'new_information':'official pre-cutoff report statuses/reasons'},
      'market':{'old':old.get('market',{}).get('status'),'new':v('market')['status'],'new_information':'none; missing legal quote timestamp remains unavailable'},
      'lineup':{'old':'historical rotation candidates, not current confirmed lineup','new':coverage(ctx)['confirmed_lineup'],'new_information':'source-bound confirmed five-player lists when found'},
      'roster':{'old':'historical players/rotation, not authoritative current identity','new':'PARTIAL','new_information':'official injury document team identity added; completeness still unknown'},
      'research':{'old':old.get('research',{}).get('status'),'new_source_count':len(v('research')['evidence']),'new_information':'TEMPORAL_B original pregame reporting'},
      'recent_context':{'old':'record/recent/H2H/player/rest already present','new_information':'home/away record, streak, current-season-only windows, 3-in-4, exact T-5 and metadata; not all new facts'},
    }
    for side,label in(('home','team_a'),('away','team_b')):
        current['recent_context'][side]={'old_recent5_observed':old.get(label,{}).get('recent_form',{}).get('recent_5',{}).get('sample_games'),
            'new_recent5_observed':v('recent_form')[side]['recent_5']['observed_n'],
            'old_previous_status':old.get(label,{}).get('previous_season',{}).get('status'),'new_previous_observed':v('previous_season_metrics')[side]['observed_n'],
            'note':'Old recent windows may mix prior season/playoffs; pilot current-season windows intentionally do not.'}
    return {'closedbook_context_path':str(path),'closedbook_context_sha256':canonical,'closedbook_context_file_sha256':sha(raw),
            'hash_protocol':'existing canonical JSON hash plus separate byte hash','comparison':current,'old_context_not_modified':True}

def assemble():
    if OUTPUT.exists():raise ValueError('immutable_pilot_destination_exists')
    samples=read(AUDIT/'selection.json')['samples']
    if len(samples)!=3:raise ValueError('exact_three_samples_required')
    OUTPUT.mkdir(parents=True,exist_ok=False);write(OUTPUT/'selection.json',read(AUDIT/'selection.json'))
    source_hashes=read(AUDIT/'ledger-source-hashes.json');summaries=[]
    for sample in samples:
        sid=sample['sample_id'];folder=OUTPUT/sid;folder.mkdir()
        ledger=read(AUDIT/(sid+'-ledger-final.json'));injury=read(AUDIT/(sid+'-injury-reviewed.json'));r=read(AUDIT/'research'/sid/'result.json')
        supplemental=AUDIT/'research'/sid/'supplemental-original-result.json'
        if supplemental.exists():r['evidence']+=read(supplemental)['evidence']
        roster=roster_from_history(sample,ledger,injury);lineups=extract_lineups(sample,r['evidence'],roster,injury);market=existing_market(sample)
        if injury.get('source_path'):
            (folder/'assets').mkdir();target=folder/'assets/official-injury-report.pdf'
            with target.open('xb')as f:f.write(Path(injury['source_path']).read_bytes())
            injury['source_path']=str(target)
            write(folder/'assets/official-injury-report-receipt.json',{k:v for k,v in injury.items()if k!='records'})
        event_time=max((x['event_time']for x in ledger['event_records']),default=None)
        stats_source=[str(SOURCE_GAMES),str(SOURCE_TEAMS)]
        evidence=[{k:v for k,v in e.items()if k not in {'raw_path','parsed_path'}}for e in r['evidence']]
        for e in evidence:
            e['acquisition_reference']=next(x['parsed_path']for x in r['evidence']if x['source_url']==e['source_url'])
            text=primary_body(read(e['acquisition_reference'])['body'])
            paragraphs=[p.strip()for p in text.splitlines()if len(p.strip())>45 and not re.search(r'Images|Mandatory Credit|Getty|Photo by',p,re.I)]
            snippet=paragraphs[0]if paragraphs else''
            e['source_excerpt']=' '.join(snippet.split()[:22]);e['source_excerpt_full_hash']=sha(snippet)
            e['primary_article_body_sha256']=sha(text);e['sidebar_and_related_content_excluded']=True
        ctx={'metadata':{'schema_version':'historical-t5-reconstruction-v1','sample_id':sid,'game_id':sample['target_game_id'],
              'parent_golden_revision':'golden-set-v1-4b0c7cfdf4032d8aba43','parent_dataset_revision':'controlled-cb-v1-6d788de247e93eaaedf4',
              'created_at':datetime.now(timezone.utc).isoformat(),'model_calls':0,'evaluation_only':True,'strict_original_capture_replay':False},
          'matchup':envelope({'game_id':sample['target_game_id'],'home_team':sample['home_team'],'away_team':sample['away_team'],'home_team_id':int(sample['home_team_id']),'away_team_id':int(sample['away_team_id']),
               'season':sample['season'],'game_type':sample['game_type'],'venue':sample['schedule_raw']['arenaName'],'venue_city':sample['schedule_raw']['arenaCity'],
               'scheduled_tipoff':sample['scheduled_tipoff'],'source_raw_tipoff':sample['schedule_raw']['gameDateTimeEst'],'source_timezone':'America/New_York',
               'time_precision':'minute','official_schedule_clock':sample['schedule_official_clock'],'actual_first_possession_not_used':True},
               [str(SOURCE_GAMES),sample['schedule_official_source_url']],event_time=sample['scheduled_tipoff'],confidence='historical_schedule_metadata_corroborated_by_official_pregame_PDF'),
          'cutoff':envelope(sample['cutoff_at'],[sample['schedule_official_source_url'],'scheduled_tipoff minus 5 minutes'],event_time=sample['cutoff_at'],confidence='deterministic_timezone_aware_T_minus_5'),
          'team_records':envelope({s:ledger[s]['record']for s in('home','away')},stats_source,event_time=event_time),
          'recent_form':envelope({s:ledger[s]['recent']for s in('home','away')},stats_source,event_time=event_time),
          'previous_season_metrics':envelope({s:ledger[s]['previous']for s in('home','away')},stats_source,event_time=event_time,confidence='observed_prior_season_boxscore_estimates_with_actual_n_not_official_ratings'),
          'head_to_head':envelope(ledger['head_to_head'],stats_source,event_time=event_time),
          'schedule_context':envelope({s:ledger[s]['rest']for s in('home','away')},stats_source,event_time=event_time),
          'player_history':envelope({s:ledger[s]['player_history']for s in('home','away')},[str(PK)],event_time=event_time,confidence='official_provider_historical_observations_not_current_availability'),
          'roster':envelope(roster,[str(PK),injury.get('source_url')],event_time=injury.get('report_time'),published_at=injury.get('report_time'),captured_at=injury.get('captured_at'),confidence='partial_identity_candidates_official_report_plus_prior_appearances_not_complete_roster'),
          'injury':envelope(injury,[injury.get('source_url')],event_time=injury.get('report_time'),published_at=injury.get('report_time'),captured_at=injury.get('captured_at'),grade='TEMPORAL_A'if injury['status']!='UNAVAILABLE'else'TEMPORAL_C',confidence=injury.get('confidence','unavailable')),
          'starting_lineup':envelope(lineups,[t.get('source_url')for t in lineups['teams']if t.get('source_url')],published_at=max((t['published_at']for t in lineups['teams']if t.get('published_at')),default=None),
               grade='TEMPORAL_B'if any(t.get('basis')!='unknown'for t in lineups['teams'])else'TEMPORAL_C',confidence='explicit_source_lists_or_unknown_no_history_ranking_fill'),
          'market':envelope(market,[market['candidate_audit_path']],grade='TEMPORAL_C'if market['status']=='UNAVAILABLE'else'TEMPORAL_A',confidence='unavailable_without_legal_quote_time'if market['status']=='UNAVAILABLE'else'latest_verified_quote_before_cutoff'),
          'research':envelope({'evidence':evidence,'grade_a_count':0,'grade_b_count':len(evidence),'lead_count':len(r['leads']),
               'discovery_receipt':str(AUDIT/'research'/sid/'result.json'),'no_provider_date_auto_upgrade':True,'no_llm_claim_extraction':True},
               [e['source_url']for e in evidence],grade='TEMPORAL_B'if evidence else'TEMPORAL_C',confidence='original_source_timestamp_plus_pregame_identity_no_evidence_of_material_late_update'),
          'known_missing_information':[],
          'provenance':{'source_hashes':source_hashes,'event_records':ledger['event_records'],'all_pilot_targets_excluded_from_numeric_readers':[s['target_game_id']for s in samples],
              'target_outcome_access_count':0,'target_boxscore_access_count':0,'prior_event_completion_precision':'source completed historical event records; same-local-calendar-day rows excluded when end-time unavailable',
              'acquisition_root':str(AUDIT),'policy':'archive_document_and_event_reconstruction; not proof of as-was original API revisions',
              'official_injury_discovery_exhaustive':False,'source_derived_after_target_is_not_renamed_original_capture':True}}
        for field,status in coverage(ctx).items():
            if status!='COMPLETE':ctx['known_missing_information'].append({'field':field,'status':status,'reason':'see field scope/provenance; no synthetic completion'})
        ctx['known_missing_information']+=[{'field':'injury_latest_edition','status':'PARTIAL','reason':'latest known valid within bounded official archive scan; exhaustive archive listing unavailable'},
           {'field':'baseline_prediction','status':'UNAVAILABLE','reason':'No Prediction/Forecast execution permitted in data-only pilot'}]
        check=validate_context(ctx)
        write(folder/'historical_t5_context.json',ctx);write(folder/'validation.json',check)
        comparison=compare_closedbook(sample,ctx);write(folder/'closedbook-comparison.json',comparison)
        cov=coverage(ctx);high=check['passed']and market['status']=='COMPLETE'and injury['status']=='COMPLETE'and cov['team_record']=='COMPLETE'and all(roster[s]['players']for s in('home','away'))
        summaries.append({'sample_id':sid,'game_id':sample['target_game_id'],'game':sample['away_team']+' @ '+sample['home_team'],'scheduled_tipoff':sample['scheduled_tipoff'],'cutoff_at':sample['cutoff_at'],
             'context_path':str(folder/'historical_t5_context.json'),'context_sha256':sha((folder/'historical_t5_context.json').read_bytes()),
             'coverage':cov,'validation':check,'high_quality_t5_snapshot':bool(high),'comparison':comparison,
             'injury_report_time':injury.get('report_time'),'injury_rows':len(injury.get('records',[])),'injury_age_minutes':injury.get('report_age_at_cutoff_minutes'),
             'research_temporal_b':len(evidence),'confirmed_lineup_teams':sum(t['basis']=='confirmed_before_cutoff'for t in lineups['teams']),
             'source_expected_teams':sum(t['basis']=='source_expected'for t in lineups['teams'])})
    after=protection();before=read(AUDIT/'protection-before.json')
    audit_after=AUDIT/'protection-after-pilot-final.json'
    write(audit_after,after)
    # Persisted JSON arrays and live SQL tuple rows carry the same contract.
    unchanged=json.dumps(before,sort_keys=True,ensure_ascii=False)==json.dumps(after,sort_keys=True,ensure_ascii=False)
    summary={'pilot_revision':'historical-t5-pilot-v1','created_at':datetime.now(timezone.utc).isoformat(),'samples':summaries,
       'protection':{'unchanged':unchanged,'protected_files':len(before['files']),'prediction_versions':len(before['versions']['prediction_versions']),'postgame_versions':len(before['versions']['postgame_versions'])},
       'policy':'new_archive_reconstruction_policy_A_B_C; old_strict_replay_contract_unchanged','model_calls':0,'expanded_to_twenty':False}
    write(OUTPUT/'manifest.json',summary)
    if not unchanged:raise ValueError('production_or_frozen_dataset_protection_drift')
    print(json.dumps({'pilot':str(OUTPUT),'validated':sum(s['validation']['passed']for s in summaries),'high_quality':sum(s['high_quality_t5_snapshot']for s in summaries)},ensure_ascii=False))
    return summary

def finalize_saved_contexts():
    """Resume metadata finalization only; never rebuild or overwrite contexts."""
    if (OUTPUT/'manifest.json').exists():raise ValueError('manifest_already_frozen')
    samples=read(OUTPUT/'selection.json')['samples'];summaries=[]
    for sample in samples:
        folder=OUTPUT/sample['sample_id'];path=folder/'historical_t5_context.json';ctx=read(path);check=validate_context(ctx)
        if not check['passed']:raise ValueError('saved_context_validation_failed:'+str(check['errors']))
        injury=ctx['injury']['value'];lineups=ctx['starting_lineup']['value'];market=ctx['market']['value'];cov=coverage(ctx)
        comparison=read(folder/'closedbook-comparison.json')
        summaries.append({'sample_id':sample['sample_id'],'game_id':sample['target_game_id'],'game':sample['away_team']+' @ '+sample['home_team'],
            'scheduled_tipoff':sample['scheduled_tipoff'],'cutoff_at':sample['cutoff_at'],'context_path':str(path),'context_sha256':sha(path.read_bytes()),
            'coverage':cov,'validation':check,'high_quality_t5_snapshot':bool(market['status']=='COMPLETE'and injury['status']=='COMPLETE'and cov['team_record']=='COMPLETE'),
            'comparison':comparison,'injury_report_time':injury.get('report_time'),'injury_rows':len(injury.get('records',[])),
            'injury_age_minutes':injury.get('report_age_at_cutoff_minutes'),'research_temporal_b':len(ctx['research']['value']['evidence']),
            'confirmed_lineup_teams':sum(r['basis']=='confirmed_before_cutoff'for r in lineups['teams']),
            'source_expected_teams':sum(r['basis']=='source_expected'for r in lineups['teams'])})
    before=read(AUDIT/'protection-before.json');after=protection();write(AUDIT/'protection-after-final.json',after)
    unchanged=json.dumps(before,sort_keys=True,ensure_ascii=False)==json.dumps(after,sort_keys=True,ensure_ascii=False)
    if not unchanged:raise ValueError('protected_asset_drift')
    summary={'pilot_revision':'historical-t5-pilot-v1','created_at':datetime.now(timezone.utc).isoformat(),'samples':summaries,
       'protection':{'unchanged':True,'protected_files':len(before['files']),'prediction_versions':len(before['versions']['prediction_versions']),
                      'postgame_versions':len(before['versions']['postgame_versions'])},
       'policy':'new_archive_reconstruction_policy_A_B_C; old_strict_replay_contract_unchanged','model_calls':0,'expanded_to_twenty':False,
       'finalization_mode':'saved_validated_contexts; no regenerated fields, source acquisition or models'}
    write(OUTPUT/'manifest.json',summary);print(json.dumps({'validated':3,'high_quality':sum(r['high_quality_t5_snapshot']for r in summaries),'protected_unchanged':True}))
    return summary

if __name__=='__main__':assemble()
