import copy
import json
from datetime import datetime,timezone
import pytest
import sqlite3
from v2.pregame_system.prediction_versions import PredictionVersionStore
from v2.pregame_system.web_search import parse_public_market_reference
from .prediction_version_service import PredictionVersionService
from .market_reference_store import MarketReferenceStore,MarketReferenceService
from .server import build_market_response


def game():
    return {'id':'espn:499999901','tipoff':'2026-10-06T23:00:00Z','status':'scheduled','game_type':'preseason',
        'home':{'id':1610612761,'name':'Toronto Raptors'},'away':{'id':1610612748,'name':'Miami Heat'}}


def reference():
    return {'status':'available','home_spread':1.5,'away_spread':-1.5,'expected_home_margin':-1.5,'total':227.5,
        'source_url':'https://sports.yahoo.com/odds','captured_at':'2026-10-04T10:00:00Z'}


@pytest.mark.parametrize('favorite,expected',[('Heat',1.5),('Raptors',-1.5)])
def test_prose_favorite_normalizes_sign_for_either_home_or_away(favorite,expected):
    g={'home_team':'Toronto Raptors','away_team':'Miami Heat','home_team_id':1610612761,'away_team_id':1610612748,
        'scheduled_tipoff':'2026-10-03T23:00:00Z'}
    result={'url':'https://sports.yahoo.com/articles/nba-odds','title':'Heat vs Raptors October 3, 2026',
        'content':f'The {favorite} are 1.5-point favorites on the spread, with the over/under set at 227.5 total points.'}
    q=parse_public_market_reference(result,g,captured_at='2026-10-03T19:31:00Z')
    assert q['home_spread']==expected and q['expected_home_margin']==-expected


def test_author_prediction_is_not_a_market_favorite_quote():
    g={'home_team':'Toronto Raptors','away_team':'Miami Heat','scheduled_tipoff':'2026-10-03T23:00:00Z'}
    r={'url':'https://sports.yahoo.com/articles/nba-odds','title':'Heat vs Raptors October 3, 2026',
        'content':'I expect the Heat to be 1.5-point favorites on the spread with over/under 227.5.'}
    assert parse_public_market_reference(r,g,captured_at='2026-10-03T19:31:00Z')is None


def test_future_reference_refresh_works_without_prediction_or_ai(tmp_path):
    versions=PredictionVersionStore(tmp_path/'v.sqlite');service=PredictionVersionService(store=versions)
    g=game();service.tasks.register_game(g)
    store=MarketReferenceStore(versions,clock=lambda:'2026-10-04T10:01:00Z')
    class Collector:
        def collect(self,selected):
            assert selected['id']==g['id'];return {'status':'available','public_market_reference':reference()}
    fetched=MarketReferenceService(store,collector=Collector()).refresh(g)
    assert fetched['reference_kind']=='current_reference'
    status,payload=build_market_response(g['id'],data={'games':[]},version_service=service)
    assert status==200 and payload['public_market_reference']['total']==227.5
    with versions.connect()as db:
        assert db.execute('SELECT count(*) FROM prediction_versions').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM generation_tasks').fetchone()[0]==0


def test_same_game_refresh_dedup_and_failure_retains_previous_quote(tmp_path):
    store=MarketReferenceStore(PredictionVersionStore(tmp_path/'v.sqlite'),clock=lambda:'2026-10-04T10:01:00Z');g=game()
    original=store.save(g,reference())
    assert store.claim(g['id']) and not store.claim(g['id'])
    store.finish(g['id'],'source_unavailable')
    assert store.read(g)['home_spread']==1.5 and not store.claim(g['id'])


def test_wrong_game_hash_or_posttipoff_current_quote_cannot_display(tmp_path):
    store=MarketReferenceStore(PredictionVersionStore(tmp_path/'v.sqlite'),clock=lambda:'2026-10-07T10:01:00Z');g=game()
    late=reference();late['captured_at']='2026-10-07T00:00:00Z'
    with pytest.raises(ValueError,match='not_pregame_capture'):store.save(g,late)
    q=reference();q['game_identity']={'game_id':'espn:9','home_team_id':1610612761,'away_team_id':1610612748,'scheduled_tipoff':g['tipoff']}
    with pytest.raises(ValueError,match='wrong_game'):store.save(g,q)
    original=store.save(g,reference())
    changed=copy.deepcopy(g);changed['tipoff']='2026-10-07T23:00:00Z'
    assert store.read(changed)is None
    with store.versions.connect()as db:
        with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE market_display_snapshots SET payload_sha256=?',('bad',))
        db.execute('INSERT INTO market_display_snapshots VALUES (?,?,?,?,?)',('bad',g['id'],json.dumps(original),'bad','2099-01-01T00:00:00Z'))
    assert store.read(g)['snapshot_id']==original['snapshot_id']


def test_historical_backfill_keeps_original_capture_and_explicit_usage(tmp_path):
    store=MarketReferenceStore(PredictionVersionStore(tmp_path/'v.sqlite'),clock=lambda:'2026-10-07T10:01:00Z');g=game()
    q=store.save(g,{**reference(),'used_by_published_prediction':False},kind='historical_backfill')
    assert q['captured_at']=='2026-10-04T10:00:00Z' and q['eligible_prediction_input']is False
    assert q['reference_kind']=='historical_backfill' and q['used_by_published_prediction']is False
