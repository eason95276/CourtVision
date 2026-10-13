import csv,json
from asset_scanner import EVAL,read_json,gid
inv=read_json(EVAL/'reports/evaluation-asset-inventory.json')
path=next(r['path'] for r in inv['tabular_sources'] if r.get('rows') and 'hometeamId' in r.get('columns',[]) and not r.get('duplicate_content'))
rows=[]
with open(path,encoding='utf-8-sig',newline='') as f:
    for r in csv.DictReader(f):
        if '2024-10'<=r['gameDate'][:7]<='2025-06' and r['gameType']=='Regular Season' and not gid(r['gameId']).startswith('nba:002'):
            rows.append({k:r.get(k) for k in ('gameId','gameDate','gameType','gameSubtype','gameLabel','gameSubLabel','homeScore','awayScore')})
print(json.dumps(rows,ensure_ascii=False))
