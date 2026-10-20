import copy
from datetime import datetime,timedelta,timezone
import pytest
from .prediction_versions import PredictionVersionStore,game_identity
from .scheduler_policy import SchedulerPolicyPlanner,normalized_policy
from .final_readiness import evaluate_readiness,next_check,snapshot_from_espn,FinalReadinessStore

TIP=datetime(2026,10,21,2,tzinfo=timezone.utc)
def fixture():
    game={'id':'espn:123','tipoff':TIP.isoformat(),'status':'scheduled','game_type':'regular_season','timezone':'Asia/Shanghai',
        'home':{'id':1610612744,'name':'Golden State Warriors','espn_team_id':'9'},'away':{'id':1610612747,'name':'Los Angeles Lakers','espn_team_id':'13'},'schedule_revision':1}
    at=TIP-timedelta(minutes=20);stamp=at.isoformat();lineups={s:[s+' Player '+str(i)for i in range(5)]for s in ('home','away')}
    early={'version_id':'early','stage':'EARLY_PREVIEW','editorial_material':[{'pregame_plan':{'lineups':[{'side':s,'players':n}for s,n in lineups.items()]}}]}
    snapshot={'game_identity':game_identity(game),'schedule_revision':1,'captured_at':stamp,'game_state':'pre',
        'lineups':{s:{'basis':'confirmed','players':n,'source_url':'https://site.api.espn.com/game','observed_at':stamp}for s,n in lineups.items()},
        'rosters':{s:{'status':'verified','players':[{'name':name}for name in n]}for s,n in lineups.items()},
        'availability':{'records':[{'side':s,'player_name':name,'status':'Available','source_url':'https://site.api.espn.com/game','observed_at':stamp}for s,n in lineups.items()for name in n]},
        'injury':{'coverage':'complete','source_url':'https://official.nba.com/report','observed_at':stamp},
        'market_collection':{'public_market_reference':{'game_identity':game_identity(game),'home_spread':-4.5,'total':220.5,'source_url':'https://sports.core.api.espn.com/odds','captured_at':stamp}}}
    return game,early,snapshot,at

def test_all_ready_and_no_model_calls():
    game,early,snapshot,at=fixture();r=evaluate_readiness(game,early,snapshot,at)
    assert r['ready']and r['llm_calls']==0 and len(r['lineups'])==2


def test_market_tipoff_compares_instant_not_provider_format():
    game,early,snapshot,at=fixture()
    snapshot['market_collection']['public_market_reference']['game_identity']['scheduled_tipoff']='2026-10-21T02:00Z'
    assert evaluate_readiness(game,early,snapshot,at)['ready']
    snapshot['market_collection']['public_market_reference']['game_identity']['scheduled_tipoff']='2026-10-21T02:01Z'
    assert 'market_identity_mismatch'in evaluate_readiness(game,early,snapshot,at)['reasons']

@pytest.mark.parametrize('status',['Questionable','Doubtful','Unknown','Probable'])
def test_core_uncertain_not_final(status):
    game,early,snapshot,at=fixture();snapshot['availability']['records'][0]['status']=status
    r=evaluate_readiness(game,early,snapshot,at);assert not r['ready']and any('availability_unresolved'in x for x in r['reasons'])

def test_predicted_lineup_not_confirmation():
    game,early,snapshot,at=fixture();snapshot['lineups']['home']['basis']='agent_expected'
    assert 'home_lineup_not_confirmed'in evaluate_readiness(game,early,snapshot,at)['reasons']

def test_membership_and_missing_injury_not_available():
    game,early,snapshot,at=fixture();snapshot['availability']['records']=[]
    assert not evaluate_readiness(game,early,snapshot,at)['ready']

def test_old_pdf_not_fresh_because_just_fetched():
    game,early,snapshot,at=fixture();r=snapshot['availability']['records'][0]
    r['published_at']=(at-timedelta(hours=4)).isoformat();r['observed_at']=at.isoformat()
    assert not evaluate_readiness(game,early,snapshot,at)['ready']

def test_market_old_and_missing_time_fail_closed():
    game,early,snapshot,at=fixture();snapshot['market_collection']['public_market_reference']['captured_at']=(at-timedelta(minutes=6)).isoformat()
    assert 'market_stale'in evaluate_readiness(game,early,snapshot,at)['reasons']
    snapshot['market_collection']={};assert not evaluate_readiness(game,early,snapshot,at)['ready']

def test_actual_start_before_scheduled_time_blocks():
    game,early,snapshot,at=fixture();snapshot['game_state']='in'
    assert 'game_started_or_cutoff_reached'in evaluate_readiness(game,early,snapshot,at)['reasons']
    snapshot['game_state']='pre';assert not evaluate_readiness(game,early,snapshot,TIP)['ready']

def test_poll_slots_and_last_minute_light_checks():
    assert next_check(TIP.isoformat(),(TIP-timedelta(minutes=20)).isoformat())==(TIP-timedelta(minutes=15)).isoformat()
    assert next_check(TIP.isoformat(),(TIP-timedelta(minutes=10)).isoformat())==(TIP-timedelta(minutes=5)).isoformat()
    assert next_check(TIP.isoformat(),(TIP-timedelta(minutes=4)).isoformat())==(TIP-timedelta(minutes=3,seconds=30)).isoformat()

def test_legacy_policy_identity_unchanged():
    assert normalized_policy()['policy_identity']=='scheduler-v2:e8a4c19aa2916829'

def test_two_stages_early_can_start_before_old_t72(tmp_path):
    from .generation_tasks import GenerationTaskStore
    game,early,snapshot,at=fixture();clock=lambda:TIP-timedelta(days=7)
    store=PredictionVersionStore(tmp_path/'versions.sqlite',clock=clock);tasks=GenerationTaskStore(store)
    tasks.register_game(game);planner=SchedulerPolicyPlanner(tasks)
    game=planner.configure(game['id'],{'report_enabled':True,'lifecycle':'early-final-v1','early_requested_at':clock().isoformat()})
    assert game['policy_config']['final_information_policy']=='latest_verified'
    planned=planner.plan_game(game);assert {t['stage']for t in planned}=={'EARLY_PREVIEW','FINAL_PREGAME'}
    final=next(t for t in planned if t['stage']=='FINAL_PREGAME');assert final['scheduled_for']==(TIP-timedelta(minutes=20)).isoformat()
    assert len(planner.plan_game(game))==2

def test_collection_finishes_after_initial_check_clock(tmp_path):
    game,early,snapshot,at=fixture();clock=[at]
    store=PredictionVersionStore(tmp_path/'versions.sqlite',clock=lambda:clock[0])
    def collect(*args):
        clock[0]+=timedelta(seconds=2);snapshot['captured_at']=clock[0].isoformat();return snapshot
    r=FinalReadinessStore(store,collect).check(game,early)
    assert r['ready']and r['checked_at']==clock[0].isoformat()


def test_ready_with_unknown_does_not_claim_availability_confirmation_on_fresh_or_cached_read(tmp_path):
    game,early,snapshot,at=fixture();game['policy_config']={'final_information_policy':'latest_verified'}
    for side in ('home','away'):snapshot['lineups'][side]['basis']='agent_expected'
    snapshot['availability']['records']=[];calls=[]
    def collect(*args):calls.append(1);return snapshot
    versions=PredictionVersionStore(tmp_path/'db.sqlite',clock=lambda:at)
    store=FinalReadinessStore(versions,collect)
    for result in (store.check(game,early),store.check(game,early)):
        assert result['ready']and result['warnings']
        assert result['availability_confirmed_at']is None and result['lineup_confirmed_at']is None
    assert calls==[1]

def test_espn_player_active_is_not_match_availability():
    game,early,snapshot,at=fixture();rows=[{'athlete':{'displayName':'home Player '+str(i),'active':True},'starter':False}for i in range(5)]
    data={'header':{'id':'123','competitions':[{'id':'123','date':game['tipoff'],'status':{'type':{'state':'pre'}},'competitors':[{'team':{'id':'9'},'homeAway':'home'},{'team':{'id':'13'},'homeAway':'away'}]}]},'boxscore':{'players':[{'team':{'id':'9'},'statistics':[{'athletes':rows}]}]}}
    result=snapshot_from_espn(game,data,{'url':'https://site.api.espn.com/game','captured_at':at.isoformat()})
    assert not result['lineups']and not result['availability']['records']

def test_fill_unknown_venue_without_overwriting_known_identity(tmp_path):
    from .generation_tasks import GenerationTaskStore
    game,early,snapshot,at=fixture();store=PredictionVersionStore(tmp_path/'p.sqlite',clock=lambda:at-timedelta(days=2));tasks=GenerationTaskStore(store)
    tasks.register_game(game)
    g={**game,'venue':'Verified Arena','neutral_site':False}
    assert tasks.register_game(g)['game']['venue']=='Verified Arena'
    with pytest.raises(Exception,match='schedule_changed'):tasks.register_game({**g,'venue':'Different Arena'})
