"""Non-portfolio checks use the existing Heat @ Raptors frozen game, never production writes."""
import copy
import json
import sqlite3
from datetime import datetime,timezone
from pathlib import Path
import pytest

from v2.pregame_system.prediction_versions import DEFAULT_DATABASE,PredictionVersionStore
from .server import _prediction_display_projection, schedule_sync_enabled,build_game_detail_response
from .prediction_version_service import PredictionVersionService
from .schedule_catalog import ScheduleCatalog

@pytest.fixture
def frozen_store(tmp_path):
    if not DEFAULT_DATABASE.is_file():pytest.skip('Existing local frozen NBA versions required')
    source=sqlite3.connect('file:'+str(DEFAULT_DATABASE)+'?mode=ro',uri=True)
    target=sqlite3.connect(tmp_path/'versions.sqlite');source.backup(target);source.close();target.close()
    return PredictionVersionStore(tmp_path/'versions.sqlite')

def test_non_lal_current_api_uses_frozen_prediction_and_h2h(frozen_store):
    service=PredictionVersionService(store=frozen_store)
    before=[]
    with frozen_store.connect() as db:
        before=[tuple(row) for row in db.execute('select version_id,payload_sha256 from prediction_versions order by version_id')]
    code,response=build_game_detail_response('espn:401902644',version_service=service,catalog=ScheduleCatalog(frozen_store.path))
    assert code==200
    assert {response['game']['home']['id'],response['game']['away']['id']}=={1610612761,1610612748}
    version=response['prediction']['version']
    assert version['game_id']=='espn:401902644'
    assert response['prediction']['display_projection']['version_id']==version['version_id']
    history=version['input_snapshot'].get('matchup_history') or {}
    for row in history.get('meetings') or []:
        assert {row['home_team_id'],row['away_team_id']}=={1610612761,1610612748}
        assert row['winner_team_id']==(row['home_team_id'] if row['home_score']>row['away_score'] else row['away_team_id'])
    stats=response['game']['previous_season_team_stats']
    assert stats['status']=='available' and stats['season']=='2025-26'
    assert {row['team_id'] for row in stats['teams'].values()}=={1610612761,1610612748}
    with frozen_store.connect() as db:
        assert before==[tuple(row) for row in db.execute('select version_id,payload_sha256 from prediction_versions order by version_id')]

def test_non_lal_market_projection_uses_shared_deterministic_adapter(frozen_store):
    with frozen_store.connect() as db:
        v=json.loads(db.execute('select payload_json from prediction_versions where game_id=?',('espn:401914101',)).fetchone()[0])
    before=copy.deepcopy(v)
    projection=_prediction_display_projection(v)
    from v2.pregame_system.ats import spread_comparison_for_version
    from v2.pregame_system.editorial_contract import _writer_market_interpretation
    expected=_writer_market_interpretation({'spread_comparison':spread_comparison_for_version(v)},v['judgment']['final_prediction'])
    assert projection['market_interpretation']==expected
    assert expected['spread']['status']=='available' and expected['total']['status']=='available'
    assert v==before

@pytest.mark.parametrize('env,value,disabled,expected',[
    ('development',None,False,False),('production',None,False,True),
    ('development','true',False,True),('production','false',False,False),('production','true',True,False)])
def test_sync_configuration_is_explicit(monkeypatch,env,value,disabled,expected):
    monkeypatch.setenv('COURTVISION_ENV',env)
    if value is None:monkeypatch.delenv('COURTVISION_SCHEDULE_SYNC_ENABLED',raising=False)
    else:monkeypatch.setenv('COURTVISION_SCHEDULE_SYNC_ENABLED',value)
    assert schedule_sync_enabled(disabled)==expected

def test_runtime_has_no_portfolio_version_branches():
    for filename in ['static/app.js','static/editorial.js','server.py']:
        source=(Path(__file__).parent/filename).read_text(encoding='utf-8')
        assert 'ffc2947f-ae38-424c-894f-c2f05d71c3d6' not in source
        assert 'f7764dcc-9896-4f97-9bef-0ad7225608af' not in source
        assert 'postgameRailReviewByVersion' not in source
        assert 'frozenUsage' not in source


def test_non_lal_recent_form_excludes_target_future_and_other_season(tmp_path):
    from .test_schedule_catalog import _event,_response,_board
    from .server import _current_schedule_season_form
    catalog=ScheduleCatalog(tmp_path/'catalog.sqlite')
    prior=_event(event_id='401902600',date='2026-10-02T23:00:00Z',season_type='1',state='post',status_name='STATUS_FINAL',completed=True,
        home_id='28',home_abbr='TOR',away_id='14',away_abbr='MIA',home_score='112',away_score='109')
    target=copy.deepcopy(prior);target['id']='401902644';target['date']='2026-10-03T23:00:00Z'
    target['competitions'][0]['id']=target['id'];target['competitions'][0]['date']=target['date']
    later=copy.deepcopy(target);later['id']='401902700';later['date']='2026-10-04T23:00:00Z'
    later['competitions'][0]['id']=later['id'];later['competitions'][0]['date']=later['date']
    catalog.ingest('fixture',[_response(_board(prior),captured_at='2026-10-03T00:00:00Z'),
        _response(_board(target,later),captured_at='2026-10-05T00:00:00Z')])
    game={'id':'espn:401902644','tipoff':'2026-10-03T23:00:00Z','season':2027,
        'home':{'id':1610612761},'away':{'id':1610612748}}
    form=_current_schedule_season_form(game,{'input_cutoff_at':'2026-10-03T22:00:00Z'},catalog)
    assert form['teams']['home']['current_season_record']=={'wins':1,'losses':0}
    assert form['teams']['away']['current_season_record']=={'wins':0,'losses':1}
    assert form['teams']['home']['current_season_game_ids']==['espn:401902600']
    assert form['season_start']==2026
