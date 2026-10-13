import json,sys,copy
from pathlib import Path
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'golden_set/v1'))
from select_samples import HERE,candidates,risk_audit,select,read_json,canonical_hash,EVAL
from build_eval_baseline import save

def stats(rows):
    teams=Counter(str(r[s+'_team_id']) for r in rows for s in ('home','away'))
    return {'team_coverage':len(teams),'max_team_frequency':max(teams.values()),'phases':dict(Counter(r['season_phase'] for r in rows)),
        'representative_phases':dict(Counter(r['season_phase'] for r in rows if r['selection_bucket']=='representative')),
        'challenge_coverage':dict(Counter(k for r in rows if r['selection_bucket']=='challenge' for k in r['challenge_reason'])),
        'outcomes':dict(Counter(r['hidden_outcome_bucket'] for r in rows))}

cfg=read_json(HERE/'sampling_config.json');m=read_json(HERE/'selection_manifest.json');pool=candidates(cfg);risk,anchors=risk_audit(pool)
official=[]
for n in range(3):
    rows=select(pool,cfg,anchors);same=[r['sample_id'] for r in rows]==m['selected_sample_ids'] and canonical_hash(rows)==canonical_hash(m['samples'])
    assert same;official.append({'run':n+1,'ordered_samples_sha256':canonical_hash(rows),'same_order_and_fields':same});print('official_replay',n+1,flush=True)
sensitivity=[]
for offset in (1,7,19,101,2026):
    alternate=copy.deepcopy(cfg);alternate['seed']+=offset;rows=select(pool,alternate,anchors)
    sensitivity.append({'seed':alternate['seed'],**stats(rows),'selected_id_hash':canonical_hash([r['sample_id'] for r in rows])});print('audit_seed',alternate['seed'],flush=True)
dates=Counter(r['game_date'] for r in m['samples']);pairs=Counter(tuple(sorted([r['home_team_id'],r['away_team_id']])) for r in m['samples'])
report={'official_replays':official,'official':stats(m['samples']),'sensitivity':sensitivity,'date_counts':dict(dates),'repeated_matchup_counts':[{'teams':list(k),'games':n} for k,n in pairs.items() if n>1],
    'file_or_pandas_order_dependency':'Sorted candidate sample IDs + fixed Python random.Random; no pandas or filesystem iteration in selection ranking.',
    'outcome_dependence':'Intentionally uses hidden actual margins for stratification, never forecast error or generated-answer quality. Not a natural outcome-proportional representative population.',
    'extrapolation_limits':'20 games from one season, manually defined structural challenge proxies; no model quality validation or class prevalence estimate.',
    'v1_modified':False,'model_calls':0}
save(EVAL/'reports/sampling-adversarial-audit.json',report);print('sampling audit complete')
