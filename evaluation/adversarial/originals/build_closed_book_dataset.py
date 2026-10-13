"""Freeze historical contexts and separate answers; zero model/tool calls."""
import argparse,json
from collections import Counter
from pathlib import Path
from asset_scanner import ROOT,EVAL,read_json,canonical_hash,sha,protection_snapshot
from audit_historical_replay import receipt as original_receipt
from build_eval_baseline import save,jsonl,csvfile,text
from closed_book_data import Ledger,build_context
from closed_book_anonymize import anonymize
from validate_closed_book_context import validate

def protection():
    p=original_receipt();p['prior_replay_datasets']={str(f):sha(f) for f in (EVAL/'datasets/historical_replay_candidates.jsonl',EVAL/'bad_cases/replay_feasibility.jsonl')}
    return p

def build():
    frozen=EVAL/'reports/closed-book-freeze-manifest.json'
    if frozen.exists():
        manifest=read_json(frozen)
        mismatches=[p for p,h in {**manifest['files'],**manifest['builder_files']}.items() if not (EVAL/p).is_file() or sha(EVAL/p)!=h]
        if mismatches:raise RuntimeError('Frozen dataset changed; use a separately authorized dataset revision: '+str(mismatches[:3]))
        print('Existing frozen dataset verified; no regeneration or overwrite. '+manifest['dataset_id']);return
    ledger=Ledger();mother=[json.loads(s) for s in (EVAL/'datasets/historical_replay_candidates.jsonl').read_text(encoding='utf-8').splitlines()]
    records=[];manifests=[];issues=[];anonymous_failures=[];source_readiness=Counter()
    for n,target in enumerate(mother):
        # The original mother audit retained team IDs in home_away.
        target={**target,'home_team_id':target['home_away']['home_team_id'],'away_team_id':target['home_away']['away_team_id'],'game_id':target['game_id']}
        context,proof=build_context(ledger,target);sample='cb-'+canonical_hash({'target':target['game_id'],'contract':'v1'})[:20]
        cp=EVAL/'datasets/closed_book_contexts'/(sample+'.json');pp=EVAL/'datasets/closed_book_provenance'/(sample+'.json');gp=EVAL/'datasets/closed_book_ground_truth'/(sample+'.json')
        errors=validate(context,proof,context_path=cp,truth_path=gp)
        if errors:issues.append({'sample_id':sample,'errors':errors});continue
        g=ledger.games.get(target['game_id'])
        if not g:issues.append({'sample_id':sample,'errors':['hidden_ground_truth_identity_unresolved']});continue
        if g['home_id']!=target['home_team_id'] or g['away_id']!=target['away_team_id']:
            issues.append({'sample_id':sample,'errors':['ground_truth_home_away_identity_mismatch']});continue
        gt={'sample_id':sample,'target_game_id':target['game_id'],'home_points':g['home_points'],'away_points':g['away_points'],
            'winner_side':'home' if g['home_points']>g['away_points'] else 'away','actual_margin':g['home_points']-g['away_points'],'actual_total':g['home_points']+g['away_points'],
            'source_ref':g['source_ref'],'usage':'Evaluator hidden answer only; never append to model input'}
        save(cp,context);save(pp,proof);save(gp,gt)
        anonymous,mapping=anonymize(context);anon_id='anon-'+canonical_hash({'sample_id':sample,'contract':'v1'})[:20]
        ap=EVAL/'datasets/anonymized/contexts'/(anon_id+'.json');mp=EVAL/'datasets/anonymized/mappings'/(anon_id+'.json')
        anon_errors=validate(anonymous,proof,mapping,ap,gp)
        if anon_errors:anonymous_failures.append({'sample_id':sample,'errors':anon_errors})
        else:save(ap,anonymous);save(mp,mapping)
        def both(key):return all(context[s][key]['status'] in {'available','partial'} for s in ('team_a','team_b'))
        flags={'has_team_record':both('record'),'has_recent_form':all(context[s]['recent_form']['recent_5']['status']=='available' for s in ('team_a','team_b')),
            'has_previous_season_metrics':both('previous_season'),'has_h2h':context['head_to_head']['status']=='available','has_player_history':both('player_history'),
            'has_rotation_candidates':both('recent_rotation_candidates'),'has_rest_context':all(context['schedule_context'][s]['status']=='available' for s in ('team_a','team_b'))}
        missing=[k for k,v in flags.items() if not v]
        record={'sample_id':sample,'target_game_id':target['game_id'],'season':context['matchup']['season'],'game_date':context['matchup']['game_date'],
            'home_team':target['home_team'],'away_team':target['away_team'],'home_team_id':target['home_team_id'],'away_team_id':target['away_team_id'],'game_type':target['game_type'],
            'context_available':True,'context_completeness':{'available_components':sum(flags.values()),'total_components':len(flags),'missing_components':missing},
            **flags,'missing_fields':missing,'partial_fields':['previous_season_metrics'] if any(context[s]['previous_season']['status']=='partial' for s in ('team_a','team_b')) else [],
            'full_previous_season_archive':all(context[s]['previous_season'].get('full_archive_coverage_verified',False) for s in ('team_a','team_b')),
            'context_path':str(cp.relative_to(EVAL)),'provenance_path':str(pp.relative_to(EVAL)),'context_sha256':canonical_hash(context),
            'ground_truth_available':True,'ground_truth_path':str(gp.relative_to(EVAL)),'early_current_season_games_before':{s:context[s]['record']['sample_games'] for s in ('team_a','team_b')},
            'anonymous_sample_id':anon_id,'anonymous_context_path':str(ap.relative_to(EVAL)) if not anon_errors else None}
        records.append(record)
        manifests.append({'sample_id':anon_id,'base_sample_id':sample,'context_path':str(ap.relative_to(EVAL)),'mapping_path':str(mp.relative_to(EVAL)),
            'context_sha256':canonical_hash(anonymous),'validated':not anon_errors,'visibility':'Evaluator-only manifest; model receives context bytes only'})
        if (n+1)%200==0:print('frozen',n+1,flush=True)
    jsonl(EVAL/'datasets/closed_book_candidates.jsonl',records);csvfile(EVAL/'datasets/closed_book_candidates.csv',records)
    jsonl(EVAL/'datasets/anonymized/manifest.jsonl',manifests)
    coverage={k:{'games':sum(r[k] for r in records),'percent':round(100*sum(r[k] for r in records)/len(records),3)} for k in flags} if records else {}
    report={'processed_games':len(mother),'successful_contexts':len(records),'validation_failures':issues,'anonymized_contexts':len(records)-len(anonymous_failures),
        'anonymous_validation_failures':anonymous_failures,'completeness_distribution':dict(Counter(str(r['context_completeness']['available_components'])+'/7' for r in records)),
        'season_distribution':dict(Counter(r['season'] for r in records)),'game_type_distribution':dict(Counter(r['game_type'] for r in records)),
        'per_team_distribution':dict(Counter(str(r[side+'_team_id']) for r in records for side in ('home','away'))),'coverage':coverage,
        'early_season_missingness':{str(limit):{'games_with_team_before_limit':sum(any(v<limit for v in r['early_current_season_games_before'].values()) for r in records),
            'games_missing_player_history':sum(not r['has_player_history'] and any(v<limit for v in r['early_current_season_games_before'].values()) for r in records),
            'games_missing_full_recent_5_current_season':sum(any(v<5 for v in r['early_current_season_games_before'].values()) for r in records)} for limit in (5,10)},
        'sampling_readiness':{str(n):{'context_count_sufficient':len(records)>=n,'all_7_components_count_sufficient':sum(not r['missing_fields'] for r in records)>=n,
            'interpretation':'Candidate capacity only; no final sampling or quality claim'} for n in (50,100,300)},
        'source_hashes':ledger.source_hashes,'source_season_counts':dict(ledger.season_counts),
        'previous_season_archive_coverage':{'both_teams_full':sum(r['full_previous_season_archive'] for r in records),'explicitly_partial':sum(bool(r['partial_fields']) for r in records)},
        'model_calls':0,'tools_enabled_for_future_run':False,'quality_threshold_defined':False,
        'limitations':['Same-day events conservatively excluded; date-level, not exact historical cutoff.',
            'Player observations are historical team appearances, not verified target roster/availability/starters.',
            'Previous season must have ended before target (source NBA Finals series completion). Full versus incomplete archive coverage is explicit; partial metrics retain actual N.',
            'Anonymous statistical fingerprints remain; anonymity cannot prove absence of parameter memory.',
            'Future runner must enforce no search/tools and load context only. No real model runner is enabled here.']}
    save(EVAL/'reports/closed-book-benchmark-preparation.json',report)
    text(EVAL/'reports/closed-book-benchmark-preparation.md','# Controlled Closed-book Preparation\n\n```json\n'+json.dumps({k:v for k,v in report.items() if k not in {'source_hashes','per_team_distribution','source_season_counts'}},ensure_ascii=False,indent=2)+'\n```\n\nFull source hashes, season/team distributions and provenance are in JSON. No formal Benchmark or LLM evaluation was executed.')
    print(json.dumps({k:report[k] for k in ('processed_games','successful_contexts','anonymized_contexts','completeness_distribution','coverage')},ensure_ascii=False))
    assert not issues and not anonymous_failures,'Generated contexts failed leakage validation'

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--protect',choices=['before','after']);a=p.parse_args()
    if a.protect:
        now=protection();save(EVAL/'reports'/('closed-book-protection-'+a.protect+'.json'),now)
        if a.protect=='after':
            before=read_json(EVAL/'reports/closed-book-protection-before.json');checks={k:before[k]==now[k] for k in ('versions','published_artifacts','writer_attempts','previous_evaluation_assets','source_assets','prior_replay_datasets')}
            save(EVAL/'reports/closed-book-protection-result.json',checks);print(json.dumps(checks));assert all(checks.values())
    else:build()
