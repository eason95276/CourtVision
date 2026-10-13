"""Existing historical market assets only; no quote timestamp invention."""
import json,sqlite3
from pathlib import Path
from .metadata import ROOT,AUDIT
from .safe_csv import projected_rows
from .validation import qualify_market
from evaluation.research_shadow.runner import write
from evaluation.research_shadow.core import sha

def existing_market(sample):
    rawid=sample['target_game_id'].split(':')[-1];path=ROOT/'artifacts/test_predictions_with_market.csv';candidates=[]
    cached_path=AUDIT/(sample['sample_id']+'-market-candidates.json')
    if cached_path.exists():
        cached=json.loads(cached_path.read_text(encoding='utf-8'))
        candidates=cached['candidates'];quote=cached['selected']
        if any(r.get('game_id')!=sample['target_game_id']or(r.get('source_sha256')and r['source_sha256']!=sha(path.read_bytes()))for r in candidates):raise ValueError('cached_market_asset_identity_or_hash_changed')
        return {'status':'COMPLETE'if quote and quote.get('home_spread')is not None and quote.get('total')is not None else'PARTIAL'if quote else'UNAVAILABLE',
                'quote':quote,'latest_known_before_cutoff':bool(quote),'rejected_candidate_count':sum(not r['qualification']['eligible']for r in candidates),
                'reason':None if quote else'no_existing_identity_verified_pre_cutoff_quote_timestamp','candidate_audit_path':str(cached_path)}
    fields={'game_id','home_team_id','away_team_id','game_date','market_expected_home_margin','market_total','moneyline_home','moneyline_away','id_spread','id_total','source','source_url','quote_timestamp','captured_at'}
    if path.exists():
        for r in projected_rows(path,fields,allowed_game_ids={rawid}):
            # No actual result/model prediction/error columns are tokenized.
            row={'game_id':sample['target_game_id'],'source':'existing historical market benchmark CSV','source_path':str(path),
                 'source_sha256':sha(path.read_bytes()),'source_url':r.get('source_url'),
                 'quote_timestamp':r.get('quote_timestamp'),'capture_timestamp':r.get('captured_at'),
                 'home_spread':-float(r['market_expected_home_margin'])if r.get('market_expected_home_margin')else None,
                 'total':float(r['market_total'])if r.get('market_total')else None,
                 'moneyline_home':r.get('moneyline_home'),'moneyline_away':r.get('moneyline_away'),
                 'identity_verified':r.get('home_team_id')==sample['home_team_id']and r.get('away_team_id')==sample['away_team_id'],
                 'closing_label_not_a_timestamp':True}
            row['qualification']=qualify_market(row,sample['cutoff_at']);candidates.append(row)
    inv=json.loads((ROOT/'evaluation/reports/evaluation-asset-inventory.json').read_text(encoding='utf-8'))
    for entry in inv['database_inventory']:
        table=next((t for t in entry['tables']if t['table']=='market_snapshots'),None)
        dbpath=Path(entry['path'])
        if not table or not dbpath.exists():continue
        # Only market table, never target boxscore/postgame/prediction-error tables.
        db=sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
        available={r[1]for r in db.execute('PRAGMA table_info(market_snapshots)')}
        needed=['game_id','source','source_url','source_published_at','captured_at','home_spread','total','raw_path']
        chosen=[k for k in needed if k in available]
        rows=db.execute('SELECT '+','.join(chosen)+' FROM market_snapshots WHERE game_id IN (?,?)',(rawid,sample['target_game_id'])).fetchall();db.close()
        for r in rows:
            value=dict(r);row={**value,'source_path':str(dbpath),'quote_timestamp':None,'capture_timestamp':value.get('captured_at'),
                'identity_verified':True,'reason':'source_published_at_semantics_require_quote_time_proof'}
            row['qualification']=qualify_market(row,sample['cutoff_at']);candidates.append(row)
    legal=[r for r in candidates if r['qualification']['eligible']]
    quote=max(legal,key=lambda r:r['quote_timestamp'])if legal else None
    write(AUDIT/(sample['sample_id']+'-market-candidates.json'),{'candidates':candidates,'selected':quote,'no_target_outcomes_read':True})
    return {'status':'COMPLETE'if quote and quote.get('home_spread')is not None and quote.get('total')is not None else'PARTIAL'if quote else'UNAVAILABLE',
            'quote':quote,'latest_known_before_cutoff':bool(quote),'rejected_candidate_count':len(candidates)-len(legal),
            'reason':None if quote else'no_existing_identity_verified_pre_cutoff_quote_timestamp',
            'candidate_audit_path':str(AUDIT/(sample['sample_id']+'-market-candidates.json'))}
