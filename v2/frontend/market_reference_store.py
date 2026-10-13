"""Independent market display snapshots, never retroactive prediction inputs."""
from __future__ import annotations
import json
import threading
import hashlib
from datetime import timedelta
from pathlib import Path
from v2.pregame_system.storage import BASE,now,instant,digest,content_hash
from v2.pregame_system.prediction_versions import _json,payload_sha256
from v2.pregame_system.market_collector import market_game,BoundedMarketCollector
from v2.pregame_system.web_search import parse_public_market_reference
from v2.pregame_system.market import events,normalize_odds,qualify_prediction_book,BOOK_ORDER
from v2.pregame_system.sources import get


def identity(game):
    g=market_game(game)
    return {**{k:g[k]for k in ('game_id','home_team_id','away_team_id')},'scheduled_tipoff':instant(g['scheduled_tipoff']).isoformat()}


class MarketReferenceStore:
    def __init__(self,versions,*,clock=now):
        self.versions=versions;self.clock=clock
        with versions.connect()as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS market_display_snapshots (
                snapshot_id TEXT PRIMARY KEY,game_id TEXT NOT NULL,payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,recorded_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS market_display_fetch_state (
                game_id TEXT PRIMARY KEY,status TEXT NOT NULL,lease_until TEXT NOT NULL,next_refresh_at TEXT NOT NULL,last_error TEXT);
              CREATE TRIGGER IF NOT EXISTS market_display_no_update BEFORE UPDATE ON market_display_snapshots BEGIN SELECT RAISE(ABORT,'immutable_market_display_snapshot'); END;
              CREATE TRIGGER IF NOT EXISTS market_display_no_delete BEFORE DELETE ON market_display_snapshots BEGIN SELECT RAISE(ABORT,'immutable_market_display_snapshot'); END;
            ''')

    def save(self,game,reference,*,kind='current_reference'):
        ref=dict(reference);expected=identity(game)
        if ref.get('game_identity') and identity(ref['game_identity'])!=expected:raise ValueError('market_display_wrong_game')
        ref.update(game_identity=expected,reference_kind=kind,eligible_prediction_input=False,
            usage='display_only_market_reference',recorded_at=self.clock())
        captured=instant(ref['captured_at']);tip=instant(expected['scheduled_tipoff'])
        if captured>instant(self.clock()):raise ValueError('market_display_future_capture')
        if kind!='historical_archive' and captured>=tip:raise ValueError('market_display_not_pregame_capture')
        if kind=='historical_archive' and not ref.get('provider_archive_label'):raise ValueError('market_display_archive_scope_missing')
        if not ref.get('source_url')or all(ref.get(k)is None for k in ('home_spread','total','home_moneyline','away_moneyline')):
            raise ValueError('market_display_source_or_values_missing')
        key=content_hash(ref)
        ref['snapshot_id']=key
        with self.versions.connect()as db:
            db.execute('INSERT OR IGNORE INTO market_display_snapshots VALUES (?,?,?,?,?)',
                (key,expected['game_id'],_json(ref),payload_sha256(ref),ref['recorded_at']))
        return ref

    def read(self,game):
        with self.versions.connect()as db:
            rows=db.execute('SELECT * FROM market_display_snapshots WHERE game_id=? ORDER BY recorded_at DESC,rowid DESC',(game.get('id')or game.get('game_id'),)).fetchall()
        for row in rows:
            ref=json.loads(row['payload_json'])
            if payload_sha256(ref)!=row['payload_sha256']:continue
            if ref['game_identity']!=identity(game):continue
            return ref
        return None

    def state(self,game_id):
        with self.versions.connect()as db:
            row=db.execute('SELECT * FROM market_display_fetch_state WHERE game_id=?',(game_id,)).fetchone()
        return dict(row)if row else None

    def claim(self,game_id):
        at=instant(self.clock())
        with self.versions.connect()as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM market_display_fetch_state WHERE game_id=?',(game_id,)).fetchone()
            if row and ((row['status']=='running' and instant(row['lease_until'])>at)or instant(row['next_refresh_at'])>at):
                db.commit();return False
            lease=(at+timedelta(seconds=90)).isoformat()
            db.execute('INSERT INTO market_display_fetch_state VALUES (?,?,?,?,?) ON CONFLICT(game_id) DO UPDATE SET status=excluded.status,lease_until=excluded.lease_until,next_refresh_at=excluded.next_refresh_at,last_error=NULL',
                (game_id,'running',lease,at.isoformat(),None))
            db.commit();return True

    def finish(self,game_id,error=None):
        at=instant(self.clock())
        with self.versions.connect()as db:
            db.execute('UPDATE market_display_fetch_state SET status=?,lease_until=?,next_refresh_at=?,last_error=? WHERE game_id=?',
                ('unavailable'if error else'ready',at.isoformat(),(at+timedelta(minutes=10 if error else 20)).isoformat(),error,game_id))


class MarketReferenceService:
    def __init__(self,store,*,collector=None,fetcher=get):
        self.store=store;self.collector=collector or BoundedMarketCollector();self.fetcher=fetcher

    def frozen_reference(self,game):
        # Only bodies hash-bound to real saved prediction inputs, not snippets.
        with self.store.versions.connect()as db:
            rows=db.execute('SELECT payload_json,payload_sha256 FROM prediction_versions WHERE game_id=? ORDER BY published_at DESC',(game['id'],)).fetchall()
        candidates=[];repo=BASE.parent.parent;allowed=BASE/'outputs'
        for row in rows:
            if hashlib.sha256(row[0].encode('utf-8')).hexdigest()!=row[1]:continue
            v=json.loads(row[0]);artifact=(v.get('provenance')or{}).get('research_artifact')or{}
            root=Path(artifact.get('path')or'')
            root=(repo/root).resolve()if not root.is_absolute()else root.resolve()
            if not root.is_relative_to(allowed.resolve())or not root.is_dir():continue
            pinned={s['url']:s for s in (v.get('research')or{}).get('source_records',[])}
            for path in root.glob('source_fetch_receipt_*.json'):
                receipt=json.loads(path.read_text(encoding='utf-8'))
                for source in receipt.get('sources',[]):
                    known=pinned.get(source.get('url'));body=source.get('body')or''
                    if not known or content_hash(body)!=known.get('body_sha256'):continue
                    captured=known.get('captured_at')
                    if not captured or instant(captured)>=instant(game['tipoff']):continue
                    q=parse_public_market_reference({'url':source['url'],'title':known.get('title',''),'content':body,
                        'published_date':known.get('published_at')},market_game(game),captured_at=captured)
                    if q:
                        q.update(origin_prediction_version_id=v.get('version_id'),used_by_published_prediction=False,
                            source_receipt_path=str(path),source_receipt_sha256=digest(path),body_sha256=known['body_sha256'],
                            raw_response_sha256=known.get('raw_response_sha256'),time_semantics='Pre-tipoff captured source quote; normalized after the game, not used by the original prediction')
                        candidates.append(q)
        return max(candidates,key=lambda q:instant(q['captured_at']))if candidates else None

    def historical_archive(self,game):
        eid=game['id'].removeprefix('espn:')
        summary,sr=self.fetcher('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='+eid)
        header=(summary or{}).get('header',{});comp=(header.get('competitions')or[{}])[0]
        if str(header.get('id'))!=eid or str(comp.get('id'))!=eid:return None
        event=next((e for e in events({'events':[{'id':eid,'date':comp['date'],'competitions':[comp]}]},sr)if identity(e)==identity(game)),None)
        if not event:return None
        data,receipt=self.fetcher(f'https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{eid}/competitions/{eid}/odds')
        quotes=[q for q in normalize_odds(data,event,receipt)if qualify_prediction_book({'id':q['provider_id'],'name':q['provider_name']})['eligible']]
        if not quotes:return None
        q=min(quotes,key=lambda q:(0 if q['quote_kind']=='close'else 1,BOOK_ORDER.index(q['provider_id'])if q['provider_id']in BOOK_ORDER else 999))
        return {'status':'available','game_identity':identity(game),'home_spread':q['home_spread'],
            'away_spread':-q['home_spread']if q['home_spread']is not None else None,'expected_home_margin':q['expected_home_margin'],
            'total':q['total'],'home_moneyline':q['home_moneyline'],'away_moneyline':q['away_moneyline'],
            'sportsbook':q['provider_name'],'source_url':receipt['url'],'source_title':'ESPN '+q['provider_name']+' historical quote',
            'published_at':None,'observed_at':None,'captured_at':receipt['captured_at'],'provider_archive_label':q['quote_kind'],
            'raw_response_sha256':receipt.get('sha256'),'used_by_published_prediction':False,
            'time_semantics':'Retrospectively retrieved provider archive label; exact quote/open/close time unverified'}

    def refresh(self,game):
        try:
            if instant(self.store.clock())>=instant(game['tipoff'])or game.get('status')in ('final','post','completed'):
                ref=self.frozen_reference(game)
                kind='historical_backfill'
                if ref is None:ref=self.historical_archive(game);kind='historical_archive'
            else:
                result=self.collector.collect(game);ref=result.get('public_market_reference');kind='current_reference'
            saved=self.store.save(game,ref,kind=kind)if ref else None
            self.store.finish(game['id'],None if saved else 'no_qualified_market_reference')
            return saved
        except Exception as exc:
            self.store.finish(game['id'],type(exc).__name__);return None

    def start(self,game):
        current=self.store.read(game)
        if current and (instant(self.store.clock())-instant(current['recorded_at'])).total_seconds()<1200:return False
        if not self.store.claim(game['id']):return False
        threading.Thread(target=self.refresh,args=(dict(game),),name='market-reference-'+game['id'],daemon=True).start()
        return True
