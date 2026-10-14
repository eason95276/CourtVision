"""Real store/service/incremental Writer integration, fake clock and model only."""
import copy
from datetime import timedelta
from v2.pregame_system.test_editorial_publication import candidate,Clock
from v2.pregame_system.test_final_delta import writer
from v2.pregame_system.test_final_readiness import fixture
from v2.pregame_system.prediction_versions import PredictionVersionStore,game_identity,_instant,payload_sha256
from v2.pregame_system.generation_tasks import GenerationTaskStore
from v2.pregame_system.scheduler_policy import SchedulerPolicyPlanner
from v2.pregame_system.editorial_test_helpers import seal_fixture_payload
from v2.pregame_system.analyst import EvidenceBoundAnalyst
from .analysis_service import SavedGameAnalysisService
from .prediction_version_service import PredictionVersionService


def setup(tmp_path):
    clock=Clock();versions=PredictionVersionStore(tmp_path/'test.sqlite',clock=clock)
    game,request,payload=candidate(versions,clock)
    game.update(game_type='regular_season',timezone='UTC',schedule_revision=1)
    _,_,snapshot,_=fixture();snapshot['game_identity']=game_identity(game)
    snapshot['market_collection']['public_market_reference']['game_identity']=game_identity(game)
    plan={'lineups':[{'side':s,'basis':'agent_expected','players':r['players'],
        'evidence_ids':['evidence-form'],'rationale':'Explicit synthetic lineup fixture.'}for s,r in snapshot['lineups'].items()],
        'player_usage':[],'key_matchup':'持球与协防','uncertainty':[]}
    c=payload['analyst_context'];c['roster_context']={'teams':{s:{'official_team_roster':{'status':'available','players':r['players']}}
        for s,r in snapshot['rosters'].items()}}
    prior=copy.deepcopy(snapshot['availability']['records'])
    for r in prior:r.update(observed_at=c['game']['cutoff'],team_id=game[r['side']]['id'],game_id=game['id'])
    c['objective_evidence'].append({'id':'early-status-fixture','section':'availability','kind':'Fact','value':{'records':prior}})
    c['evidence_index']['early-status-fixture']={'section':'availability','source_type':'Fact'}
    payload['objective_evidence']=copy.deepcopy(c['objective_evidence'])
    payload['editorial_material']=[{'summary':'Explicit fixture judgment.','effect':'no_change','evidence_ids':['evidence-form'],'pregame_plan':plan}]
    payload.update(stage='EARLY_PREVIEW',policy_version='scheduler-v2',context_hash=c['context_hash'],
        model_layers={k:c[k]for k in ('pure_prediction','market_benchmark','divergence','residual_prediction','official_numeric_prediction')},
        official_numeric_prediction=c['official_numeric_prediction'],sources=[],
        public_market_reference=copy.deepcopy(snapshot['market_collection']['public_market_reference']))
    payload['input_snapshot']['relationship']={'basketball':c['pure_prediction'],'market':c['market_benchmark'],
        'residual':c['residual_prediction'],'final':c['official_numeric_prediction']}
    seal_fixture_payload(payload)
    early=versions.publish(game,request,payload)
    tasks=GenerationTaskStore(versions);tasks.register_game(game)
    planner=SchedulerPolicyPlanner(tasks);registered=planner.configure(game['id'],{'report_enabled':True,'lifecycle':'early-final-v1','early_requested_at':clock.value.isoformat()})
    final=next(t for t in planner.plan_game(registered)if t['stage']=='FINAL_PREGAME')
    return clock,versions,tasks,game,early,snapshot,final


def test_http_wait_then_incremental_publication_keeps_early(tmp_path,monkeypatch):
    clock,versions,tasks,game,early,snapshot,task=setup(tmp_path)
    monkeypatch.setattr('v2.pregame_system.final_delta.now',lambda:clock.value.isoformat())
    calls=[];mode={'ready':False}
    def collect(g,e,at,v):
        calls.append('http');r=copy.deepcopy(snapshot)
        for side in ('home','away'):r['lineups'][side]['observed_at']=at
        for row in r['availability']['records']:row['observed_at']=at
        r['injury']['observed_at']=at;r['captured_at']=at
        r['market_collection']['public_market_reference']['captured_at']=at
        if not mode['ready']:r['lineups']['home']['basis']='agent_expected'
        return r
    def forbidden(*args,**kwargs):raise AssertionError('FINAL must not run EARLY Research/static/ML')
    def fake_writer(messages,schema):calls.append('writer');return writer(messages,schema)
    def factory(data,*,analyst):return SavedGameAnalysisService(data,analyst=analyst)
    service=PredictionVersionService(store=versions,task_store=tasks,service_factory=factory,
        analyst_factory=lambda r:EvidenceBoundAnalyst(root=tmp_path/r['request_id'],client=fake_writer,forecast_client=forbidden,max_calls=4),
        input_preparer=forbidden,pre_generation_preparer=forbidden,final_readiness_collector=collect,final_publish_guard=lambda g:None)
    tip=_instant(game['tipoff'],'tipoff');clock.value=tip-timedelta(minutes=21)
    assert service.execute_task({'games':[game]},game['id'],task['task_id'])['update_status']=='waiting_final_ready'
    assert calls==[]
    clock.value=tip-timedelta(minutes=20)
    first=service.execute_task({'games':[game]},game['id'],task['task_id'])
    assert first['version_id']==early['version_id']and calls==['http']
    assert tasks.get_task(task['task_id'])['attempt_count']==0
    clock.value=tip-timedelta(minutes=15);mode['ready']=True
    final=service.execute_task({'games':[game]},game['id'],task['task_id'])
    assert final['update_status']=='published' and final['version_id']!=early['version_id']
    assert calls==['http','http','writer']
    # Numeric judgment is stable; citations correctly bind the new confirmation.
    for key in ('winner_side','win_probability','expected_margin','expected_total'):
        assert final['version']['judgment']['final_prediction'][key]==early['judgment']['final_prediction'][key]
    assert final['version']['baseline_version_id']==early['version_id']
    with versions.connect()as db:
        row=db.execute('SELECT payload_json,payload_sha256 FROM prediction_versions WHERE version_id=?',(early['version_id'],)).fetchone()
        assert payload_sha256(__import__('json').loads(row[0]))==row[1]==early['payload_sha256']
        assert db.execute('SELECT COUNT(*) FROM prediction_versions').fetchone()[0]==2
    again=service.execute_task({'games':[game]},game['id'],task['task_id'])
    assert again['version_id']==final['version_id']and calls==['http','http','writer']
    clock.value=tip+timedelta(seconds=1)
    selected=service.selected(game)
    assert selected['status']=='locked'and selected['version_id']==final['version_id']
