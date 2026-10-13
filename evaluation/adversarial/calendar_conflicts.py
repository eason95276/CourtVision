import json,sys,gzip
from pathlib import Path
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asset_scanner import EVAL,ROOT,read_json,connect,resolve,gid
from build_eval_baseline import save

d=read_json(EVAL/'reports/dataset-adversarial-audit.json');conflicts=d['source_calendar_date_disagreements'];evidence=[]
with connect(ROOT/'v2/data/player_knowledge.sqlite') as c:
    for game,dates in conflicts.items():
        r=c.execute('SELECT g.*,s.source_url,s.source_path,s.collection_time_utc,s.response_sha256 FROM player_knowledge_games g JOIN player_knowledge_source_receipts s ON g.latest_success_receipt_id=s.receipt_id WHERE g.game_id IN (?,?)',(game,game.removeprefix('nba:'))).fetchone()
        item={'game_id':game,'conflicting_dates':dates,'knowledge_metadata':dict(r) if r else None}
        source=resolve(r['source_path']) if r else None
        data=source.read_bytes() if source else b'{}'
        if data[:2]==b'\x1f\x8b':data=gzip.decompress(data)
        raw=json.loads(data.decode('utf-8-sig'))
        header=raw.get('header',{})
        if header:item['provider_header']={'id':header.get('id'),'date':header.get('date'),'competitions':[{'id':x.get('id'),'date':x.get('date'),'status':x.get('status'),'competitors':[{'homeAway':y.get('homeAway'),'team':y.get('team',{}).get('displayName'),'score':y.get('score')} for y in x.get('competitors',[])]} for x in header.get('competitions',[])]}
        else:item['raw_top_keys']=list(raw)[:15]
        evidence.append(item)
rows=[json.loads(s) for s in (EVAL/'datasets/closed_book_candidates.jsonl').read_text(encoding='utf-8').splitlines()];gold={r['sample_id'] for r in read_json(EVAL/'golden_set/v1/selection_manifest.json')['samples']};affected=[]
for r in rows:
    p=read_json(EVAL/r['provenance_path']);day=p['target_event_time']
    for game,dates in conflicts.items():
        if min(dates)<day<=max(dates):
            fields=[f for f,proof in p['fields'].items() if any(x['game_id']==game for x in proof['source_events'])]
            if fields:affected.append({'sample_id':r['sample_id'],'target_game_id':r['target_game_id'],'target_day':day,'conflict_game':game,'fields':fields,'golden_v1':r['sample_id'] in gold})
report={'source_conflict_evidence':evidence,'affected_sample_ids':sorted({r['sample_id'] for r in affected}),
    'affected_golden_ids':sorted({r['sample_id'] for r in affected if r['golden_v1']}),'affected_dependencies':affected,
    'note':'Months-apart dates are not a timezone issue. Immutable V1 not edited; affected inputs require source adjudication and an explicitly authorized corrected revision.'}
save(EVAL/'reports/temporal-source-conflicts.json',report)
print(json.dumps({'evidence':evidence,'affected_samples':len(report['affected_sample_ids']),'affected_golden':len(report['affected_golden_ids'])},ensure_ascii=False))
