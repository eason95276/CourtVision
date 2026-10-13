"""Deterministic selection; no LLM outputs or forecast errors in ranking."""
import json,random,sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from asset_scanner import EVAL,read_json,canonical_hash,sha,resolve
from build_eval_baseline import save,csvfile,text
from validate_closed_book_context import validate

HERE=Path(__file__).resolve().parent

def phase(c):
    ordinal=1+sum(c[s]['record']['sample_games'] for s in ('team_a','team_b'))/2
    return ('early' if ordinal<=20 else 'mid' if ordinal<=60 else 'late'),ordinal

def outcome(gt,cfg):
    n=abs(gt['actual_margin']);return 'close' if n<=cfg['outcome_buckets']['close_max_abs_margin'] else 'medium' if n<=cfg['outcome_buckets']['medium_max_abs_margin'] else 'blowout'

def features(c,cfg):
    f=cfg['challenge_features'];a=c['team_a'];b=c['team_b'];signals={}
    prior=[a['record']['sample_games'],b['record']['sample_games']]
    if min(prior)<=f['early_sparse_max_prior_games'] or any(t['player_history']['status']=='unavailable' for t in (a,b)):
        signals['early_sparse']={'prior_observed_games':prior,'player_history_status':[a['player_history']['status'],b['player_history']['status']]}
    sw=[t['record'].get('win_pct_before_game') for t in (a,b)];rw=[t['recent_form']['recent_5'].get('win_pct') for t in (a,b)]
    if None not in sw+rw:
        sg=sw[0]-sw[1];rg=rw[0]-rw[1]
        if sg*rg<0 and min(abs(sg),abs(rg))>=f['record_recent_opposed_min_abs_win_pct_gap']:signals['record_recent_conflict']={'season_gap':sg,'recent5_gap':rg}
        nr=[t['recent_form']['recent_5']['net_rating']['value'] for t in (a,b)]
        if None not in nr and abs(sg)<=f['similar_season_win_pct_max_gap'] and abs(nr[0]-nr[1])<=f['similar_recent_net_rating_max_gap']:
            signals['similar_historical_signals']={'season_win_pct_gap':sg,'recent5_net_rating_gap':nr[0]-nr[1]}
    rest=[c['schedule_context'][s] for s in ('team_a','team_b')];days=[r.get('days_rest') for r in rest]
    if None not in days and (abs(days[0]-days[1])>=f['observed_rest_min_day_gap'] or rest[0]['back_to_back']!=rest[1]['back_to_back']):
        signals['observed_schedule_asymmetry']={'days_rest':days,'back_to_back':[r['back_to_back'] for r in rest],'scope':'Observed completed-game dates; not complete schedule proof'}
    mixed=[]
    for t in (a,b):mixed.append(sum(p['starter_observed_n']>=f['mixed_starting_min_observations'] and p['starter_frequency'] is not None and f['mixed_starting_frequency_lower']<p['starter_frequency']<f['mixed_starting_frequency_upper'] for p in t['recent_rotation_candidates']['players']))
    if max(mixed)>=f['mixed_starting_roles_min_players']:signals['mixed_observed_starting_roles']={'mixed_candidate_counts':mixed,'scope':'Historical starting-frequency ambiguity; not proof of target lineup instability'}
    previous=[t['previous_season'].get('sample_games',0) for t in (a,b)]
    if min(previous)<=f['previous_season_partial_max_team_n'] and any(t['previous_season']['status']=='partial' for t in (a,b)):
        signals['previous_season_archive_gap']={'observed_games':previous,'status':[a['previous_season']['status'],b['previous_season']['status']]}
    return signals

def candidates(cfg):
    from evaluation_integrity import verify_closedbook
    verify_closedbook(EVAL)
    frozen=read_json(EVAL/'reports/closed-book-freeze-manifest.json')
    if frozen['dataset_id']!=cfg['parent_dataset_id']:raise ValueError('parent dataset mismatch')
    for p,h in frozen['files'].items():
        if sha(EVAL/p)!=h:raise ValueError('parent frozen file changed: '+p)
    result=[]
    for r in (json.loads(s) for s in (EVAL/'datasets/closed_book_candidates.jsonl').read_text(encoding='utf-8').splitlines()):
        if r['season']!=cfg['season'] or r['game_type']!=cfg['game_type']:continue
        c=read_json(EVAL/r['context_path']);p=read_json(EVAL/r['provenance_path']);gt=read_json(EVAL/r['ground_truth_path'])
        if validate(c,p,context_path=EVAL/r['context_path'],truth_path=EVAL/r['ground_truth_path']):continue
        if gt['target_game_id']!=r['target_game_id'] or canonical_hash(c)!=r['context_sha256']:continue
        ph,ordinal=phase(c)
        result.append({**r,'season_phase':ph,'phase_observed_ordinal':ordinal,'hidden_outcome_bucket':outcome(gt,cfg),'challenge_features':features(c,cfg)})
    return sorted(result,key=lambda r:r['sample_id'])

def risk_audit(pool):
    bygame={r['target_game_id']:r for r in pool};replays={r['case_id']:r for r in (json.loads(s) for s in (EVAL/'bad_cases/replay_feasibility.jsonl').read_text(encoding='utf-8').splitlines())}
    audit=[];eligible=[]
    excluded_numeric={'largest_margin_error','largest_total_error','worst_win_probability','largest_model_market_divergence'}
    for bad in (json.loads(s) for s in (EVAL/'bad_cases/bad_cases.jsonl').read_text(encoding='utf-8').splitlines()):
        match=bygame.get(bad.get('game_id'));reason=None
        if bad['failure_type'] in excluded_numeric:reason='Numerical prediction-error/market extreme, not a generation-quality anchor; selecting it would violate no-error-based sampling.'
        elif not match:reason='No explicit eligible 2025-26 regular-season sample identity; no directory-name or matchup-date guess.'
        elif any(x in bad['failure_type'].lower() for x in ('injury','market','lineup_current_identity')):reason='Requires evidence absent from closed-book context.'
        elif not bad.get('evidence') or not resolve(bad['source_file']):reason='Original failure evidence unavailable.'
        entry={'bad_case_id':bad['case_id'],'game_id':bad.get('game_id'),'matched_sample_id':match['sample_id'] if match else None,
            'original_failure_type':bad['failure_type'],'original_evidence':bad.get('evidence'),'eligible':reason is None,'reason':reason,
            'mapping_confidence':'exact_explicit_game_id' if match else 'unmapped','replayability':replays.get(bad['case_id'],{}).get('replayable'),
            'source_file':bad['source_file']}
        audit.append(entry)
        if reason is None:eligible.append((match,entry))
    return audit,eligible

def select(pool,cfg,anchors):
    best=None
    for trial in range(cfg['selection_trials']):
        rng=random.Random(cfg['seed']+trial);ranks={r['sample_id']:rng.random() for r in pool}
        chosen=[];team_counts=Counter();buckets=Counter();reasons=Counter()
        def allowed(r):return r['sample_id'] not in {x['sample_id'] for x in chosen} and max(team_counts[str(r['home_team_id'])],team_counts[str(r['away_team_id'])])<cfg['team_cap']
        def add(r,bucket,anchor=None):
            row={**r,'selection_bucket':bucket,'bad_case_anchor_if_any':anchor,'challenge_reason':list(r['challenge_features']) if bucket=='challenge' else [],
                'selection_evidence':{'context_path':r['context_path'],'provenance_path':r['provenance_path'],'context_sha256':r['context_sha256']}}
            chosen.append(row);team_counts.update([str(r['home_team_id']),str(r['away_team_id'])]);buckets[row['hidden_outcome_bucket']]+=1
            if bucket=='challenge':reasons.update(row['challenge_reason'])
        for r,anchor in anchors[:cfg['risk_anchor_max']]:
            if allowed(r):add(r,'historical_risk_anchor',anchor)
        challenge_n=cfg['total_games']-cfg['representative']-len(chosen)
        for _ in range(challenge_n):
            options=[r for r in pool if allowed(r) and r['challenge_features']]
            if not options:break
            def challenge_score(r):
                fresh=sum(reasons[k]==0 for k in r['challenge_features']);newteams=sum(team_counts[str(r[s+'_team_id'])]==0 for s in ('home','away'))
                return (fresh,newteams,-buckets[r['hidden_outcome_bucket']],ranks[r['sample_id']])
            add(max(options,key=challenge_score),'challenge')
        if len(chosen)!=cfg['total_games']-cfg['representative']:continue
        for ph,quota in cfg['representative_phase_quota'].items():
            for _ in range(quota):
                options=[r for r in pool if allowed(r) and r['season_phase']==ph and not r['missing_fields']]
                if not options:break
                def rep_score(r):
                    newteams=sum(team_counts[str(r[s+'_team_id'])]==0 for s in ('home','away'))
                    rep_buckets=Counter(x['hidden_outcome_bucket'] for x in chosen if x['selection_bucket']=='representative')
                    deficit=cfg['soft_representative_outcome_target'][r['hidden_outcome_bucket']]-rep_buckets[r['hidden_outcome_bucket']]
                    return (newteams,deficit,-team_counts[str(r['home_team_id'])]-team_counts[str(r['away_team_id'])],ranks[r['sample_id']])
                add(max(options,key=rep_score),'representative')
        if len(chosen)!=cfg['total_games']:continue
        phases=Counter(r['season_phase'] for r in chosen if r['selection_bucket']=='representative')
        if dict(phases)!=cfg['representative_phase_quota']:continue
        score=(len(team_counts),len(reasons),min(buckets.values()),-max(team_counts.values()))
        tie=tuple(sorted(r['sample_id'] for r in chosen))
        if best is None or score>best[0] or (score==best[0] and tie<best[1]):best=(score,tie,chosen)
    if best is None:raise ValueError('No selection satisfies hard constraints; do not silently relax')
    chosen=best[2];team_counts=Counter(str(r[s+'_team_id']) for r in chosen for s in ('home','away'))
    for r in chosen:
        r['unique_team_coverage']=len(team_counts);r['selection_reason']='7/7 structural input, phase quota and diverse-team/outcome soft targets' if r['selection_bucket']=='representative' else 'Observed safe-field challenge coverage; not selected by forecast correctness' if r['selection_bucket']=='challenge' else 'Known exact-ID historical failure coverage, not full replay claim'
    return chosen

def main():
    if (HERE/'selection_manifest.json').exists():
        from evaluation_integrity import verify_golden
        verify_golden(EVAL)
    cfg=read_json(HERE/'sampling_config.json');pool=candidates(cfg);risk,anchors=risk_audit(pool);chosen=select(pool,cfg,anchors)
    body={'parent_dataset_id':cfg['parent_dataset_id'],'sampling_config_hash':canonical_hash(cfg),'selected_sample_ids':[r['sample_id'] for r in chosen],
        'samples':chosen,'risk_anchor_audit':risk,'selection_method':'Fixed-seed constrained multi-stratum sampling, no model-output scoring'}
    selection_hash=canonical_hash(body);revision='golden-set-v1-'+selection_hash[:20]
    output={**body,'golden_set_revision':revision,'selection_manifest_hash':selection_hash,'created_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()}
    path=HERE/'selection_manifest.json'
    if path.exists():
        old=read_json(path)
        if old['selection_manifest_hash']!=selection_hash:raise ValueError('Frozen Golden V1 cannot change; create v2')
        print('Frozen selection reproduced:',old['golden_set_revision']);return
    save(path,output);csvfile(HERE/'selection.csv',chosen)
    team_counts=Counter(str(r[s+'_team_id']) for r in chosen for s in ('home','away'))
    summary={'revision':revision,'eligible_pool':len(pool),'counts':dict(Counter(r['selection_bucket'] for r in chosen)),
        'phases_all':dict(Counter(r['season_phase'] for r in chosen)),'phases_representative':dict(Counter(r['season_phase'] for r in chosen if r['selection_bucket']=='representative')),
        'unique_teams':len(team_counts),'team_counts':dict(team_counts),'outcomes':dict(Counter(r['hidden_outcome_bucket'] for r in chosen)),
        'completeness':dict(Counter(str(r['context_completeness']['available_components'])+'/7' for r in chosen)),
        'risk_anchors_eligible':len(anchors),'risk_slots_transferred':cfg['risk_anchor_max']-min(len(anchors),cfg['risk_anchor_max'])}
    save(HERE/'sampling_summary.json',summary);text(HERE/'sampling_report.md','# Golden Set V1 Sampling Audit — evaluator private\n\n```json\n'+json.dumps(summary,ensure_ascii=False,indent=2)+'\n```\n\nHidden outcome buckets never enter Pregame annotation data. Three numerical error extremes mapped to regular games but are excluded; remaining recorded cases lack qualifying regular identities. Full 40-case audit is in selection_manifest.json. 7/7 is structural; partial previous-season archive remains visible.')
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
