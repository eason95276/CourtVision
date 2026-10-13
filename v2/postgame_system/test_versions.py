import copy
import json
import sqlite3
from pathlib import Path
import pytest
from v2.pregame_system.storage import digest
from v2.frontend.test_prediction_version_service import Clock,game,FakeAnalysisService
from v2.frontend.prediction_version_service import PredictionVersionService
from v2.pregame_system.prediction_versions import PredictionVersionStore
from v2.pregame_system.editorial_test_helpers import seal_fixture_payload
from .version_service import (normalize_completed_summary,PostgameVersionService,read_postgame,
    read_postgame_status,project_player_usage_review,render_review)
from .record import _prediction_review, _settled_spread_review
from v2.pregame_system.ats import build_spread_comparison


def sample(tmp_path):
    g=game();g['game_type']='regular_season'
    labels=['MIN','PTS','FG','3PT','FT','REB','AST','TO','STL','BLK','OREB','DREB','PF']
    competitors=[];teams=[];players=[]
    for side,espn,abbr,pts,nba in [('home','18','NY',100,1610612752),('away','20','PHI',110,1610612755)]:
        team={'id':espn,'abbreviation':abbr,'displayName':abbr}
        competitors.append({'homeAway':side,'team':team,'score':str(pts),'winner':side=='away','linescores':[]})
        teams.append({'team':team,'statistics':[{'name':'fieldGoalsMade-fieldGoalsAttempted','displayValue':f'{pts//2}-100'},
            {'name':'threePointFieldGoalsMade-threePointFieldGoalsAttempted','displayValue':'0-0'},
            {'name':'freeThrowsMade-freeThrowsAttempted','displayValue':'0-0'}]})
        players.append({'team':team,'statistics':[{'labels':labels,'athletes':[{'athlete':{'id':espn,'displayName':side+' Player'},
            'didNotPlay':False,'starter':True,'stats':['20',str(pts),f'{pts//2}-100','0-0','0-0','1','1','0','0','0','0','1','1']}]}]})
    d={'header':{'id':g['id'][5:],'competitions':[{'id':g['id'][5:],'date':g['tipoff'],
        'status':{'type':{'completed':True,'state':'post'}},'competitors':competitors}]},'boxscore':{'teams':teams,'players':players}}
    raw=tmp_path/'response.json';raw.write_text(json.dumps(d),encoding='utf-8')
    src={'status':'success','raw_path':str(raw),'sha256':digest(raw),'captured_at':'2026-10-21T03:00:00Z','url':'https://site.api.espn.com/summary'}
    return g,d,src


@pytest.mark.parametrize('bad',['event','team','tipoff','status','score'])
def test_result_rejects_identity_nonfinal_and_score_conflicts(tmp_path,bad):
    g,d,s=sample(tmp_path)
    c=d['header']['competitions'][0]
    if bad=='event':d['header']['id']='wrong'
    if bad=='team':c['competitors'][0]['team']['abbreviation']='MIA'
    if bad=='tipoff':c['date']='2026-10-21T23:00:00Z'
    if bad=='status':c['status']['type']['completed']=False
    if bad=='score':c['competitors'][0]['score']='200'
    with pytest.raises(ValueError):normalize_completed_summary(g,d,s)


def test_postgame_uses_published_base_and_never_mutates_prediction(tmp_path):
    g,d,s=sample(tmp_path);clock=Clock();clock.value=__import__('datetime').datetime(2026,10,20,12,tzinfo=__import__('datetime').timezone.utc);FakeAnalysisService.clock=clock;FakeAnalysisService.fail=False;FakeAnalysisService.cross_tipoff=False
    svc=PredictionVersionService(store=PredictionVersionStore(tmp_path/'v.sqlite',clock=clock),service_factory=FakeAnalysisService,analyst_factory=lambda _:object())
    published=svc.generate({'games':[g]},g['id']);base=published['version_id']
    with svc.store.connect() as db:before=tuple(db.execute('SELECT payload_json,payload_sha256 FROM prediction_versions').fetchone())
    clock.value=__import__('datetime').datetime(2026,10,21,3,tzinfo=__import__('datetime').timezone.utc)
    post=PostgameVersionService(svc.store);rid,b=post.sync_result(g,d,s);p=post.publish(g,rid,required_base_version=base)
    assert not p['evaluation']['winner_correct'] and p['evaluation']['actual_home_margin']==-10
    assert p['evaluation']['margin_error']==16.5 and p['evaluation']['total_error']==14.5
    assert p['evaluation']['ats']['final']['settlement']['result']=='unavailable'
    assert p['base_prediction_version_id']==base and p['automatic_lesson_feedback'] is False
    assert read_postgame(svc.store,g['id'])['version_id']==p['version_id']
    status=read_postgame_status(svc.store,g['id'],game_status='final',has_published_pregame=True)
    assert status['status']=='available' and status['version_id']==p['version_id']
    with svc.store.connect() as db:
        old_postgame=tuple(db.execute('SELECT payload_json,payload_sha256 FROM postgame_versions WHERE version_id=?',
                                      (p['version_id'],)).fetchone())
    state_before=svc.store.connect()
    try:revision_before=state_before.execute('SELECT state_revision FROM game_prediction_state WHERE game_id=?',(g['id'],)).fetchone()[0]
    finally:state_before.close()
    duplicate_source={**s,'captured_at':'2026-10-21T03:05:00Z'}
    duplicate_id,_=post.sync_result(g,d,duplicate_source)
    assert duplicate_id==rid
    duplicate_post=post.publish(g,duplicate_id,required_base_version=base)
    assert duplicate_post['version_id']==p['version_id']
    with svc.store.connect() as db:
        assert db.execute('SELECT state_revision FROM game_prediction_state WHERE game_id=?',(g['id'],)).fetchone()[0]==revision_before
        assert db.execute('SELECT COUNT(*) FROM postgame_versions WHERE game_id=?',(g['id'],)).fetchone()[0]==1
        assert tuple(db.execute('SELECT payload_json,payload_sha256 FROM postgame_versions WHERE version_id=?',
                                (p['version_id'],)).fetchone())==old_postgame
        assert tuple(db.execute('SELECT payload_json,payload_sha256 FROM prediction_versions').fetchone())==before
        with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE postgame_versions SET published_at=?',('wrong',))
    with pytest.raises(ValueError,match='last_published'):post.publish(g,rid,required_base_version='failed-attempt')


def test_postgame_settles_independent_and_final_ats_from_frozen_quote(tmp_path):
    g,d,s=sample(tmp_path);g['game_type']='regular_season'
    clock=Clock();clock.value=__import__('datetime').datetime(2026,10,20,12,tzinfo=__import__('datetime').timezone.utc)
    FakeAnalysisService.clock=clock;FakeAnalysisService.fail=False;FakeAnalysisService.cross_tipoff=False
    quote={'status':'available','game_identity':{'game_id':g['id'],'home_team_id':g['home']['id'],
        'away_team_id':g['away']['id'],'home_team':'Knicks','away_team':'76ers'},
        'home_spread':-4.5,'away_spread':4.5,'expected_home_margin':4.5,'total':220.5,
        'sportsbook':'Draft Kings','source_url':'https://example.test/odds',
        'captured_at':'2026-10-20T11:59:00Z','observed_at':'2026-10-20T11:59:00Z',
        'published_at':None,'raw_response_sha256':'c'*64}
    collection={'status':'available','public_market_reference':quote,
        'evidence':[{'id':'frozen-quote-id','value':{'market_reference':quote}}]}
    independent={'winner_side':'away','win_probability':.56,'expected_home_margin':-2.5,'expected_total':224.5}
    FakeAnalysisService.market_collection=collection
    FakeAnalysisService.spread_comparison=build_spread_comparison(g,collection,independent,{
        'winner_side':'home','win_probability':.63,'expected_margin':6.5,'expected_total':224.5})
    svc=PredictionVersionService(store=PredictionVersionStore(tmp_path/'ats-v.sqlite',clock=clock),
        service_factory=FakeAnalysisService,analyst_factory=lambda _:object())
    published=svc.generate({'games':[g]},g['id']);base=published['version']
    assert base['spread_comparison']['prediction_version_id']==base['version_id']
    with svc.store.connect() as db:
        before=tuple(db.execute('SELECT payload_json,payload_sha256 FROM prediction_versions WHERE version_id=?',
                                (base['version_id'],)).fetchone())
    clock.value=__import__('datetime').datetime(2026,10,21,3,tzinfo=__import__('datetime').timezone.utc)
    post=PostgameVersionService(svc.store);rid,bundle=post.sync_result(g,d,s)
    result=post.publish(g,rid,required_base_version=base['version_id'])
    ats=result['evaluation']['ats']
    assert ats['market']['market_quote_id']=='frozen-quote-id'
    assert ats['independent']['ats_lean']=='away'
    assert ats['independent']['spread_edge']==-7.0
    assert ats['independent']['settlement']['result']=='win'
    assert ats['final']['ats_lean']=='home'
    assert ats['final']['spread_edge']==2.0
    assert ats['final']['settlement']['result']=='loss'
    summary=post.ats_evaluation_summary()
    assert summary['total_games']==1
    assert summary['independent']['wins']==1 and summary['independent']['hit_rate']==1.0
    assert summary['final']['losses']==1 and summary['final']['hit_rate']==0.0
    with svc.store.connect() as db:
        after=tuple(db.execute('SELECT payload_json,payload_sha256 FROM prediction_versions WHERE version_id=?',
                               (base['version_id'],)).fetchone())
    assert after==before
    FakeAnalysisService.market_collection=None
    FakeAnalysisService.spread_comparison=None


def test_away_winner_probability_and_margin_convert_to_home_view():
    bundle={'identity':{'espn_game_id':'espn:1'},'game':{'actual_home_margin':-5,'actual_total':220}}
    base={'version_id':'v','game_id':'espn:1','stage':'EARLY_PREVIEW','payload_sha256':'hash','input_cutoff_at':'cutoff',
        'judgment':{'final_prediction':{'winner_side':'away','win_probability':0.6,'expected_margin':3.5,'expected_total':224.5}}}
    r=_prediction_review(bundle,base,None)
    assert r['source_type']=='published_prediction_version'
    assert r['public_prediction']['prediction']['win_probability']==0.4
    assert r['public_prediction']['prediction']['expected_margin']==-3.5
    assert r['public_prediction']['errors']['win_correct']


def test_postgame_frozen_integer_line_can_settle_push_without_rewriting_prediction():
    g=game(); g['game_type']='regular_season'
    quote={'status':'available','game_identity':{'game_id':g['id'],'home_team_id':g['home']['id'],
        'away_team_id':g['away']['id']},'home_spread':-4.0,'away_spread':4.0,
        'expected_home_margin':4.0,'captured_at':'2026-10-20T11:59:00Z',
        'observed_at':'2026-10-20T11:59:00Z','sportsbook':'Draft Kings',
        'source_url':'https://example.test/odds'}
    collection={'status':'available','public_market_reference':quote}
    independent={'winner_side':'home','win_probability':.6,'expected_home_margin':5.5,'expected_total':220}
    final={'winner_side':'home','win_probability':.6,'expected_margin':5.0,'expected_total':220}
    comparison=build_spread_comparison(g,collection,independent,final)
    version={'version_id':'postgame-push-fixture','game_id':g['id'],'stage':'EARLY_PREVIEW',
        'payload_sha256':'d'*64,'input_cutoff_at':'2026-10-20T11:00:00Z',
        'judgment':{'final_prediction':final},'prediction':final,
        'input_snapshot':g,'market_collection':collection,'spread_comparison':comparison}
    review=_settled_spread_review(version,{'actual_home_margin':4,'actual_total':224})
    assert comparison['final']['ats_lean']=='home'
    assert comparison['final']['ats_lean_reference']['line']==-4.0
    assert review['final']['settlement']['result']=='push'
    assert version['judgment']['final_prediction']==final


def test_postgame_minutes_projection_preserves_participation_and_dnp_states():
    usage = [
        {"player_name": "Joel Embiid", "side": "home", "minutes_min": 10, "minutes_max": 20},
        {"player_name": "Played No Minutes", "side": "home", "minutes_min": 10, "minutes_max": 20},
        {"player_name": "Incomplete Source", "side": "home", "minutes_min": 10, "minutes_max": 20},
        {"player_name": "Unknown Player", "side": "home", "minutes_min": 10, "minutes_max": 20},
        {"player_name": "Absent From Feed", "side": "home", "minutes_min": 10, "minutes_max": 20},
        {"player_name": "Normal Player", "side": "home", "minutes_min": 10, "minutes_max": 20},
    ]
    players = [
        {"full_name": "Joel Embiid", "minutes": None,
         "participation_status": "did_not_play_confirmed"},
        {"full_name": "Played No Minutes", "minutes": None,
         "participation_status": "played"},
        {"full_name": "Incomplete Source", "minutes": None,
         "participation_status": "data_missing"},
        {"full_name": "Unknown Player", "minutes": None,
         "participation_status": "unknown"},
        {"full_name": "Normal Player", "minutes": 17.0,
         "participation_status": "played"},
    ]

    review = project_player_usage_review(usage, players)
    by_name = {row["player_name"]: row for row in review}

    assert by_name["Joel Embiid"]["actual_minutes"] is None
    assert by_name["Joel Embiid"]["participation_status"] == "did_not_play_confirmed"
    assert by_name["Joel Embiid"]["status"] == "did_not_play_confirmed"
    assert by_name["Played No Minutes"]["participation_status"] == "played"
    assert by_name["Played No Minutes"]["status"] == "minutes_missing"
    assert by_name["Incomplete Source"]["participation_status"] == "data_missing"
    assert by_name["Incomplete Source"]["status"] == "data_missing"
    assert by_name["Unknown Player"]["participation_status"] == "unknown"
    assert by_name["Unknown Player"]["status"] == "unknown"
    assert by_name["Absent From Feed"]["participation_status"] == "not_in_source"
    assert by_name["Absent From Feed"]["status"] == "not_in_source"
    assert by_name["Normal Player"]["participation_status"] == "played"
    assert by_name["Normal Player"]["actual_minutes"] == 17.0
    assert by_name["Normal Player"]["status"] == "supported"


def test_postgame_renderer_does_not_call_confirmed_dnp_minutes_unknown():
    payload = {
        "record": {
            "game": {"away_team_tricode": "NY", "away_points": 97, "home_points": 120,
                     "home_team_tricode": "PHI", "actual_home_margin": 23, "actual_total": 217},
            "team_boxscores": [
                {"side": "away", "team_tricode": "NY", "fieldGoalsMade": 35,
                 "points": 97, "fieldGoalsAttempted": 91, "threePointersMade": 8, "threePointersAttempted": 41,
                 "freeThrowsMade": 19, "freeThrowsAttempted": 32},
                {"side": "home", "team_tricode": "PHI", "fieldGoalsMade": 41,
                 "points": 120, "fieldGoalsAttempted": 97, "threePointersMade": 16, "threePointersAttempted": 46,
                 "freeThrowsMade": 22, "freeThrowsAttempted": 28},
            ],
            "key_players": [],
        },
        "base_stage": "EARLY_PREVIEW",
        "evaluation": {"predicted_home_margin": -4.5, "predicted_total": 224.5,
                       "winner_correct": False, "absolute_margin_error": 27.5,
                       "absolute_total_error": 7.5},
        "scoring_decomposition": [
            {"side": "away", "field_goal_points": 78, "free_throw_points": 19, "bench_points": 63},
            {"side": "home", "field_goal_points": 98, "free_throw_points": 22, "bench_points": 77},
        ],
        "judgment_review": [], "lineup_review": [],
        "minutes_review": project_player_usage_review(
            [{"player_name": "Joel Embiid", "side": "home", "minutes_min": 10, "minutes_max": 20}],
            [{"full_name": "Joel Embiid", "minutes": None,
              "participation_status": "did_not_play_confirmed"}],
        ),
    }

    article = render_review(payload)
    assert "Joel Embiid 预计 10–20 分钟，本场确认未出场（DNP）" in article
    assert "Joel Embiid 预计 10–20 分钟，实际 未取得，尚无法验证" not in article


def test_confirmed_dnp_survives_espn_to_postgame_version_and_chinese_display(tmp_path):
    g,d,s=sample(tmp_path)
    away_players=d['boxscore']['players'][1]['statistics'][0]['athletes']
    away_players.append({'athlete':{'id':'3059318','displayName':'Joel Embiid'},'didNotPlay':True,
        'reason':"Coach's Decision",'starter':False,
        'stats':['DNP','0','0-0','0-0','0-0','0','0','0','0','0','0','0','0']})
    raw_path=Path(s['raw_path'])
    raw_path.write_text(json.dumps(d),encoding='utf-8')
    s['sha256']=digest(raw_path)

    class UsageAnalysisService(FakeAnalysisService):
        def analyze(self,game_id,*,allow_lookup,cutoff,before_writer):
            result=super().analyze(game_id,allow_lookup=allow_lookup,cutoff=cutoff,before_writer=before_writer)
            result['editorial_material']=[{'pregame_plan':{'lineups':[],'player_usage':[
                {'player_name':'Joel Embiid','side':'away','minutes_min':10,'minutes_max':20,
                 'basis':'agent_estimate','evidence_ids':[]},
            ]}}]
            return seal_fixture_payload(result)

    clock=Clock();clock.value=__import__('datetime').datetime(2026,10,20,12,tzinfo=__import__('datetime').timezone.utc)
    UsageAnalysisService.clock=clock;UsageAnalysisService.fail=False;UsageAnalysisService.cross_tipoff=False
    store=PredictionVersionStore(tmp_path/'dnp-versions.sqlite',clock=clock)
    svc=PredictionVersionService(store=store,clock=clock,service_factory=UsageAnalysisService,analyst_factory=lambda _:object())
    from v2.frontend.player_display_names import PlayerDisplayNameStore
    PlayerDisplayNameStore(store).ensure([{"player_id":"3059318","full_name":"Joel Embiid","id_namespace":"espn"}])
    published=svc.generate({'games':[g]},g['id'],request_id='dnp-projection-fixture')
    clock.value=__import__('datetime').datetime(2026,10,21,3,tzinfo=__import__('datetime').timezone.utc)
    post=PostgameVersionService(store)
    result_id,bundle=post.sync_result(g,d,s)
    normalized=next(player for player in bundle['player_boxscores'] if player['full_name']=='Joel Embiid')
    assert normalized['participation_status']=='did_not_play_confirmed'

    version=post.publish(g,result_id,required_base_version=published['version_id'])
    row=next(item for item in version['minutes_review'] if item['player_name']=='Joel Embiid')
    assert row['participation_status']=='did_not_play_confirmed'
    assert row['actual_minutes'] is None
    assert row['status']=='did_not_play_confirmed'
    assert 'Joel Embiid 预计 10–20 分钟，本场确认未出场（DNP）' in version['article']
    assert '乔尔·恩比德 预计 10–20 分钟，本场确认未出场（DNP）' in version['display_article']
    assert 'Joel Embiid 预计 10–20 分钟，实际 未取得，尚无法验证' not in version['article']
