import hashlib,json,sqlite3
from .current_season_context import attach_current_season


def fixture(tmp_path):
    path=tmp_path/'facts.sqlite'
    with sqlite3.connect(path)as db:
        db.execute('CREATE TABLE schedule_catalog_games(game_id,season,game_type,scheduled_tipoff,status,home_team_id,away_team_id,home_team_name,away_team_name,home_score,away_score,captured_at,source_url)')
        db.execute("INSERT INTO schedule_catalog_games VALUES('espn:prior',2027,'regular_season','2026-10-20T23:00:00+00:00','final',2,1,'Knicks','76ers',100,106,'2026-10-24T00:00:00+00:00','https://example.org/game')")
        db.execute('CREATE TABLE game_result_snapshots(result_id,game_id,payload_json,payload_sha256,captured_at)')
        value={'game':{'status':'final','game_id':'espn:prior','season_start':2026,'season_type':'Regular Season',
            'scheduled_tipoff':'2026-10-20T23:00:00+00:00','home_team_id':2,'away_team_id':1,'home_team':'Knicks','away_team':'76ers','home_points':100,'away_points':106}}
        raw=json.dumps(value);db.execute('INSERT INTO game_result_snapshots VALUES(?,?,?,?,?)',('result','espn:prior',raw,hashlib.sha256(raw.encode()).hexdigest(),'2026-10-21T02:00:00+00:00'))
    game={'id':'espn:target','game_type':'regular_season','season_start':2026,'home':{'id':1},'away':{'id':3},
        'season_form':{'status':'available','teams':{'home':{'current_season_games':0}}},'relationship':{'final':{'expected_total':220.5}}}
    return path,game


def test_frozen_prior_result_reaches_analysis_despite_later_catalog_refresh(tmp_path):
    path,game=fixture(tmp_path);original=json.dumps(game,sort_keys=True)
    result=attach_current_season(game,'2026-10-22T18:00:00+00:00',path)
    home=result['season_form']['teams']['home']
    assert home['current_season_games']==1 and home['regular_season_game_number']==2
    assert home['last_result']['team_score']==106 and home['last_result']['opponent_score']==100
    assert result['season_phase']['phase']!='opening_night'
    assert result['relationship']==game['relationship']and json.dumps(game,sort_keys=True)==original


def test_future_capture_and_target_result_cannot_enter_pregame_context(tmp_path):
    path,game=fixture(tmp_path)
    result=attach_current_season(game,'2026-10-20T22:00:00+00:00',path)
    assert result['season_form']['teams']['home']['current_season_games']is None
    game['id']='espn:prior'
    assert attach_current_season(game,'2026-10-22T18:00:00+00:00',path)['season_form']['teams']['home']['last_result']is None


def test_corrupt_saved_result_is_not_a_fact(tmp_path):
    path,game=fixture(tmp_path)
    with sqlite3.connect(path)as db:db.execute("UPDATE game_result_snapshots SET payload_sha256='invalid'")
    assert attach_current_season(game,'2026-10-22T18:00:00+00:00',path)['season_form']['teams']['home']['last_result']is None
