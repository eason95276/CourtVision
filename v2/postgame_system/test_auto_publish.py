import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from v2.frontend.schedule_catalog import ScheduleCatalog
from v2.pregame_system.prediction_versions import payload_sha256

from . import auto_publish
from .version_service import read_postgame_status


GAME={"id":"espn:401999901","tipoff":"2026-10-05T23:00:00Z","status":"scheduled",
      "game_type":"preseason","season_start":2026,
      "home":{"id":1610612755,"name_zh":"76人"},
      "away":{"id":1610612752,"name_zh":"尼克斯"}}
BASE={"version_id":"pregame-v1","stage":"EARLY_PREVIEW","published_at":"2026-10-05T16:00:00Z",
      "payload_sha256":"a"*64}


def _response(event):
    raw=json.dumps({"events":[event]},sort_keys=True).encode()
    return {"url":"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261005",
        "captured_at":"2026-10-06T00:00:00Z","status":"success","http_status":200,
        "raw_sha256":hashlib.sha256(raw).hexdigest(),"payload":{"events":[event]}}


def _final_event():
    event={"id":"401999901","date":GAME["tipoff"],"season":{"year":2027,"displayName":"2026-27"},
        "seasonType":{"id":"1","type":1,"name":"Preseason"},"competitions":[{
            "id":"401999901","date":GAME["tipoff"],"neutralSite":False,"venue":{"fullName":"Test Arena"},
            "status":{"type":{"name":"STATUS_FINAL","state":"post","completed":True}},
            "competitors":[
                {"id":"20","homeAway":"home","team":{"id":"20","abbreviation":"PHI","displayName":"Philadelphia 76ers"},"score":"120","winner":True},
                {"id":"18","homeAway":"away","team":{"id":"18","abbreviation":"NYK","displayName":"New York Knicks"},"score":"97","winner":False}]}]}
    return event


class FakeStore:
    def __init__(self,path): self.path=path
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15,isolation_level=None)
        db.row_factory=sqlite3.Row
        return db
    def select_current(self,game):
        return {"status":"locked","version":BASE,"version_id":BASE["version_id"]}


class FakeTasks:
    def registered_game(self,game_id): return dict(GAME) if game_id==GAME["id"] else None


class FakePredictionService:
    def __init__(self,store): self.store=store;self.tasks=FakeTasks()


class FakePostgame:
    sync_calls=0
    publish_calls=0
    def __init__(self,store):
        self.store=store
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS game_result_snapshots(result_id TEXT PRIMARY KEY,game_id TEXT,payload_json TEXT,payload_sha256 TEXT,captured_at TEXT);
            CREATE TABLE IF NOT EXISTS postgame_versions(version_id TEXT PRIMARY KEY,game_id TEXT,base_prediction_version_id TEXT,result_id TEXT,payload_json TEXT,payload_sha256 TEXT,published_at TEXT);
            """)
    def sync_result(self,game,summary,receipt):
        type(self).sync_calls+=1
        bundle={"game":{"home_points":120,"away_points":97}}
        result_id="result-1"
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO game_result_snapshots VALUES(?,?,?,?,?)",
                (result_id,game["id"],json.dumps(bundle,sort_keys=True),payload_sha256(bundle),receipt["captured_at"]))
        return result_id,bundle
    def publish(self,game,result_id,*,required_base_version=None):
        type(self).publish_calls+=1
        payload={"game_id":game["id"],"version_id":"postgame-v1",
            "base_prediction_version_id":required_base_version,"result_id":result_id,
            "published_at":"2026-10-06T00:01:00+00:00"}
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO postgame_versions VALUES(?,?,?,?,?,?,?)",
                (payload["version_id"],game["id"],required_base_version,result_id,
                 json.dumps(payload,sort_keys=True),payload_sha256(payload),payload["published_at"]))
        return payload


def _fixture(tmp_path, *, with_prediction=True):
    path=tmp_path/"postgame.sqlite"
    clock=lambda:datetime(2026,10,6,12,5,tzinfo=timezone.utc)
    catalog=ScheduleCatalog(path,clock=clock)
    catalog.ingest("scoreboard",[_response(_final_event())],include_game_types={"preseason"})
    store=FakeStore(path)
    with store.connect() as db:
        db.execute("CREATE TABLE prediction_versions(game_id TEXT,published_at TEXT,version_id TEXT)")
        db.execute("CREATE TABLE game_prediction_state(game_id TEXT,registered_game_json TEXT,state_revision INTEGER)")
        db.execute("CREATE TABLE generation_tasks(task_id TEXT)")
        if with_prediction:
            db.execute("INSERT INTO prediction_versions VALUES(?,?,?)",(GAME["id"],BASE["published_at"],BASE["version_id"]))
        db.execute("INSERT INTO game_prediction_state VALUES(?,?,?)",(GAME["id"],json.dumps(GAME),0))
    return catalog,FakePredictionService(store),clock


def test_auto_postgame_publishes_once_from_final_registered_game(tmp_path,monkeypatch):
    catalog,service,clock=_fixture(tmp_path)
    FakePostgame.sync_calls=FakePostgame.publish_calls=0
    monkeypatch.setattr(auto_publish,"normalize_completed_summary",
        lambda game,summary,receipt:{"game":{"home_points":120,"away_points":97}})
    calls=[]
    def fetch(game_id):
        calls.append(game_id)
        return {"fixture":"summary"},{"status":"success","captured_at":"2026-10-06T00:00:00Z"}
    sync=auto_publish.PostgameAutoPublisher(catalog,prediction_service=service,summary_fetcher=fetch,
        postgame_factory=FakePostgame,clock=clock)
    first=sync.sync_pending()
    second=sync.sync_pending()
    assert first[0]["status"]=="published"
    assert first[0]["base_prediction_version_id"]==BASE["version_id"]
    assert second==[]
    assert calls==[GAME["id"]]
    assert FakePostgame.sync_calls==FakePostgame.publish_calls==1
    status=read_postgame_status(service.store,GAME["id"],game_status="final",has_published_pregame=True)
    assert status["status"]=="available" and status["version_id"]==first[0]["version_id"]
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM postgame_versions WHERE game_id=?",(GAME["id"],)).fetchone()[0]==1


def test_auto_postgame_invokes_separate_writer_after_immutable_publish(tmp_path,monkeypatch):
    catalog,service,clock=_fixture(tmp_path)
    FakePostgame.sync_calls=FakePostgame.publish_calls=0
    monkeypatch.setattr(auto_publish,"normalize_completed_summary",
        lambda game,summary,receipt:{"game":{"home_points":120,"away_points":97}})
    calls=[]
    from . import writer_retry
    monkeypatch.setattr(writer_retry,'load_postgame_editorial',lambda *a,**k:{'artifact_sha256':'a'*64} if calls else None)
    def writer(store,game_id,*,postgame_version_id):
        calls.append((game_id,postgame_version_id))
        return type("WriterResult",(),{"status":"success"})()
    sync=auto_publish.PostgameAutoPublisher(catalog,prediction_service=service,
        summary_fetcher=lambda game_id:({"fixture":"summary"},{"status":"success","captured_at":"2026-10-06T00:00:00Z"}),
        postgame_factory=FakePostgame,editorial_generator=writer,clock=clock)
    result=sync.sync_pending()
    assert result[0]["status"]=="published"
    assert result[0]["editorial_status"]=="success"
    assert calls==[(GAME["id"],result[0]["version_id"])]


def test_public_postgame_sync_defaults_to_runtime_writer_without_model_in_test(tmp_path,monkeypatch):
    from . import editorial
    catalog,service,clock=_fixture(tmp_path)
    monkeypatch.setattr(auto_publish,"normalize_completed_summary",
        lambda game,summary,receipt:{"game":{"home_points":120,"away_points":97}})
    calls=[]
    from . import writer_retry
    monkeypatch.setattr(writer_retry,'load_postgame_editorial',lambda *a,**k:{'artifact_sha256':'a'*64} if calls else None)
    def writer(store,game_id,*,postgame_version_id):
        calls.append((game_id,postgame_version_id))
        return type("WriterResult",(),{"status":"success"})()
    monkeypatch.setattr(editorial,"generate_postgame_editorial",writer)
    result=auto_publish.sync_pending_postgames(
        catalog,prediction_service=service,
        summary_fetcher=lambda game_id:({"fixture":"summary"},{"status":"success","captured_at":"2026-10-06T00:00:00Z"}),
        postgame_factory=FakePostgame,clock=clock,
    )
    assert result[0]["editorial_status"]=="success"
    assert calls==[(GAME["id"],result[0]["version_id"])]


def test_failed_postgame_fetch_remains_retryable_and_schedule_fact_is_preserved(tmp_path,monkeypatch):
    catalog,service,clock=_fixture(tmp_path)
    monkeypatch.setattr(auto_publish,"normalize_completed_summary",
        lambda game,summary,receipt:{"game":{"home_points":120,"away_points":97}})
    attempts=[]
    def fetch(game_id):
        attempts.append(game_id)
        if len(attempts)==1: raise OSError("temporary_provider_failure")
        return {"fixture":"summary"},{"status":"success","captured_at":"2026-10-06T00:00:00Z"}
    sync=auto_publish.PostgameAutoPublisher(catalog,prediction_service=service,summary_fetcher=fetch,
        postgame_factory=FakePostgame,clock=clock)
    assert sync.sync_pending()[0]["status"]=="failed"
    assert catalog.get_game(GAME["id"])["status"]=="final"
    assert sync.sync_pending()[0]["status"]=="published"
    assert len(attempts)==2


def test_schedule_only_final_without_pregame_prediction_does_not_publish(tmp_path):
    catalog,service,clock=_fixture(tmp_path,with_prediction=False)
    calls=[]
    sync=auto_publish.PostgameAutoPublisher(catalog,prediction_service=service,
        summary_fetcher=lambda game_id:(calls.append(game_id) or {},{}),postgame_factory=FakePostgame,clock=clock)
    assert sync.sync_pending()==[]
    assert calls==[]


def test_unpublished_final_state_is_distinct_from_upcoming_game(tmp_path):
    store=FakeStore(tmp_path/"status.sqlite")
    assert read_postgame_status(store,GAME["id"],game_status="scheduled",has_published_pregame=True) is None
    assert read_postgame_status(store,GAME["id"],game_status="final",has_published_pregame=True)=={"status":"pending"}
    assert read_postgame_status(store,GAME["id"],game_status="final",has_published_pregame=False)=={"status":"unavailable"}


def test_successful_old_artifacts_do_not_starve_later_article_batch(tmp_path,monkeypatch):
    from . import writer_retry
    catalog,service,clock=_fixture(tmp_path)
    factory=FakePostgame(service.store)
    factory.publish(GAME,'fixture-result',required_base_version=BASE['version_id'])
    later={'game_id':'espn:401999902','version_id':'later-postgame','base_prediction_version_id':'later-prediction',
           'result_id':'later-result','published_at':'2026-10-06T00:02:00+00:00'}
    with service.store.connect() as db:
        db.execute('INSERT INTO postgame_versions VALUES(?,?,?,?,?,?,?)',(later['version_id'],later['game_id'],
            later['base_prediction_version_id'],later['result_id'],json.dumps(later),payload_sha256(later),later['published_at']))
    calls=[]
    monkeypatch.setattr(writer_retry,'load_postgame_editorial',lambda post,**kwargs:
        {'artifact_sha256':'a'*64} if post['version_id']=='postgame-v1' or calls else None)
    def writer(store,gid,**kwargs):
        calls.append(gid);return type('Result',(),{'status':'success'})()
    sync=auto_publish.PostgameAutoPublisher(catalog,prediction_service=service,postgame_factory=FakePostgame,
        editorial_generator=writer,clock=clock,max_games=1)
    assert sync.sync_pending()==[]
    assert sync.sync_pending()[0]['editorial_status']=='success'
    assert sync.sync_pending()==[]
    assert calls==[later['game_id']]
