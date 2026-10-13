from asset_scanner import *
inv=read_json(EVAL/'reports/evaluation-asset-inventory.json')
with connect(ROOT/'v2/data/player_knowledge.sqlite') as db:
    print('PLAYER_CAPTURE_RANGE',tuple(db.execute('SELECT MIN(collection_time_utc),MAX(collection_time_utc) FROM player_game_facts').fetchone()))
    print('PLAYER_RECEIPT_SAMPLE',dict(db.execute('SELECT source_path,collection_time_utc,status FROM player_knowledge_source_receipts LIMIT 1').fetchone()))
for item in inv['database_inventory']:
    if any(t['table'] in ('player_knowledge_games','player_knowledge_source_receipts','injury_snapshots') for t in item['tables']):
        print(json.dumps({'db':item['path'],'tables':[t for t in item['tables'] if t['table'] in ('player_knowledge_games','player_knowledge_source_receipts','injury_snapshots')]},ensure_ascii=False))
with connect(RUNTIME_DB) as db:
    for r in db.execute('SELECT game_id,payload_json FROM prediction_versions'):
        p=json.loads(r['payload_json']);ctx=p.get('analyst_context',{})
        rosters={side:{k:v for k,v in t.items() if k in ('official_team_roster','source','captured_at','roster_source')} for side,t in ctx.get('roster_context',{}).get('teams',{}).items()}
        for t in rosters.values():
            if isinstance(t.get('official_team_roster'),dict):t['official_team_roster']={k:v for k,v in t['official_team_roster'].items() if k!='players'}
        print(json.dumps({'game':r['game_id'],'cutoff':p.get('input_cutoff_at'),'rosters':rosters},ensure_ascii=False))
folder=ROOT/'v2/pregame_system/outputs/okc_historical_enrichment_20261002_final/forecast_packets'
p=next(folder.glob('*.json'));packet=read_json(p)
print(json.dumps({'packet':str(p),'capture':packet['data_provenance'],'identity':packet['game'],'injury':str(packet['injuries'])[:900]},ensure_ascii=False))
