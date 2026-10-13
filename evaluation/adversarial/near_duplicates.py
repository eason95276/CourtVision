import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asset_scanner import EVAL,read_json
from build_eval_baseline import save
import numpy as np

rows=[json.loads(s) for s in (EVAL/'datasets/closed_book_candidates.jsonl').read_text(encoding='utf-8').splitlines()];vectors=[]
for r in rows:
    c=read_json(EVAL/r['context_path']);v=[]
    for side in ('team_a','team_b'):
        t=c[side];v.append(t['record'].get('win_pct_before_game'))
        for window in ('recent_5','recent_10'):
            x=t['recent_form'][window];v.extend([x.get('win_pct'),None if x.get('points_for_per_game') is None else x['points_for_per_game']/30,None if x.get('points_against_per_game') is None else x['points_against_per_game']/30,
                None if x.get('net_rating',{}).get('value') is None else x['net_rating']['value']/20])
    vectors.append([np.nan if x is None else x for x in v])
x=np.array(vectors,dtype=float);n=len(rows);dist=np.zeros((n,n));known=np.zeros((n,n),dtype=int)
for col in range(x.shape[1]):
    values=x[:,col];valid=np.isfinite(values[:,None])&np.isfinite(values[None,:]);d=np.abs(values[:,None]-values[None,:]);dist+=np.where(valid,d,0);known+=valid
dist=np.divide(dist,known,out=np.full_like(dist,np.inf),where=known>=10);dist[np.tril_indices(n)]=np.inf
indices=np.argpartition(dist.ravel(),20)[:20];indices=sorted(indices,key=lambda i:dist.ravel()[i]);pairs=[]
for i in indices:
    a,b=np.unravel_index(i,dist.shape);pairs.append({'a':rows[a]['sample_id'],'b':rows[b]['sample_id'],'scaled_mean_absolute_difference':float(dist[a,b]),'shared_dimensions':int(known[a,b]),'same_matchup':{rows[a]['home_team_id'],rows[a]['away_team_id']}=={rows[b]['home_team_id'],rows[b]['away_team_id']}})
save(EVAL/'reports/near-duplicate-audit.json',{'contexts':n,'method':'Candidate nearest pairs over 18 numeric summary dimensions, win rate scale1, scoring scale30, net efficiency scale20; no identity merging or quality threshold.',
    'minimum_shared_dimensions':10,'closest_20':pairs,'note':'Similar basketball summaries do not prove duplicate events; exact identity/hash audit remains separate.'})
print('nearest_pairs_audited',len(pairs),'closest_distance',pairs[0]['scaled_mean_absolute_difference'])
