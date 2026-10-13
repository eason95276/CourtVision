import json,sys
from collections import Counter
from asset_scanner import ROOT,files_in,read_json
inv=read_json(ROOT/'evaluation/reports/evaluation-asset-inventory.json')
for db in inv['database_inventory']:
    for t in db['tables']:
        if t['table']=='player_game_facts':print(json.dumps({'path':db['path'],'columns':t['columns']}))
samples=[];states=Counter()
for p in files_in(ROOT):
    if p.suffix!='.json' or p.stat().st_size>400000:continue
    try:d=read_json(p)
    except (ValueError,OSError):continue
    if not isinstance(d,dict):continue
    state=d.get('status')
    if isinstance(state,str):states[state]+=1
    if state in ('no_go','failed') and (d.get('error') or d.get('reason') or d.get('failure_reason')):
        if len(samples)<15:samples.append({'path':str(p),'status':state,'keys':list(d),'reason':str(d.get('error') or d.get('reason') or d.get('failure_reason'))[:180]})
print(json.dumps({'states':states,'examples':samples},ensure_ascii=False))
