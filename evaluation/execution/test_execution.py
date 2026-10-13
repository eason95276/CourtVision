"""Deterministic fake transport / synthetic labels in temp dirs, never real labels."""
import copy,json,sys,sqlite3
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import runner
from control import Control,exclusive,PHASES
from runner import build_request,validate_config,load_profile,preflight,run_sample,parse_response,ProviderFailure
from workspace import clean_pregame_task,stage_tasks
from asset_scanner import read_json,canonical_hash,sha,EVAL

PROFILE=Path(__file__).parent/'profiles/closed-book-exec-v1/profile.json'

@pytest.fixture
def config():return copy.deepcopy(load_profile(PROFILE))

@pytest.fixture
def ctx():return read_json(EVAL/read_json(runner.GOLD/'selection_manifest.json')['samples'][0]['context_path'])

@pytest.fixture
def ledger(tmp_path):return Control(tmp_path/'control.sqlite')

def response(context):
    return json.dumps({'id':'synthetic-test-response','model':'deepseek-v4-flash','choices':[{'finish_reason':'stop','message':{'content':json.dumps({
        'title':'SYNTHETIC UNIT TEST ONLY','short_review':'not a real article','article':'UNIT TEST FIXTURE ONLY',
        'important_assumptions':[],'final_prediction':{'winner':context['matchup']['home_team'],'win_probability':.55,'expected_margin':3,'expected_total':223.5}})}}]})

def fake_case(ctx,tag):
    c=copy.deepcopy(ctx);c['matchup']['home_team']=tag;c['team_a']['team_name']=tag
    return {'sample_id':tag,'context_sha256':canonical_hash(c)},c

def test_postgame_locked_prepared(ledger):
    with pytest.raises(ValueError,match='PREGAME_FROZEN'):ledger.postgame_gate('test')

def test_override_explicit_logged_not_phase_change(ledger):
    with pytest.raises(ValueError):ledger.postgame_gate('test','')
    ledger.postgame_gate('test','SYNTHETIC OVERRIDE TEST')
    assert ledger.state()['phase']=='PREPARED'
    with ledger.tx() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='override_used'").fetchone()[0]==1

def test_phase_cannot_skip_generation(ledger):
    with pytest.raises(ValueError):ledger.start_annotations('pregame','test')
    with pytest.raises(ValueError):ledger.mark_generated('postgame','unit',['x']*20,'test')
    with pytest.raises(ValueError):ledger.mark_generated('pregame','unit',['x']*20,'test')

def test_only_pregame_workspace_import(ledger):
    tasks=stage_tasks('pregame',ledger)
    assert len(tasks)==20
    for task in tasks:clean_pregame_task(task)
    with pytest.raises(ValueError):stage_tasks('postgame',ledger)

def test_workspace_gt_injection_rejected():
    task=read_json(runner.GOLD/'pregame_import.json')[0]
    task['data']['ground_truth']={'score':999}
    with pytest.raises(ValueError):clean_pregame_task(task)

@pytest.mark.parametrize('capability',[{'tools':[{'type':'function'}]},{'retrieval':True},{'shared_history':True},{'search':True},{'database_tool':True}])
def test_capabilities_rejected(config,capability):
    config.update(capability)
    with pytest.raises(ValueError):validate_config(config)

@pytest.mark.parametrize('field',['ground_truth','hidden_outcome_bucket','metadata','selection_manifest','postgame_evidence','other_sample','filesystem_path'])
def test_dynamic_metadata_rejected(config,ctx,field):
    ctx[field]={'marker':'SECRET_FIXTURE_ONLY'}
    with pytest.raises(ValueError):build_request(config,'pregame',ctx,canonical_hash(ctx))

def test_gt_argument_forbidden(config,ctx):
    with pytest.raises(ValueError):build_request(config,'pregame',ctx,canonical_hash(ctx),{'ground_truth':{}})

def test_wrong_context_hash(config,ctx):
    with pytest.raises(ValueError):build_request(config,'pregame',ctx,'0'*64)

def test_wrong_golden_revision(config):
    config['golden_set_revision']='wrong'
    with pytest.raises(ValueError):validate_config(config)

def test_skill_hash_drift(config):
    config['snapshots']['editorial']['sha256']='0'*64
    with pytest.raises(ValueError):validate_config(config)

def test_request_exact_boundary(config,ctx):
    request=build_request(config,'pregame',ctx,canonical_hash(ctx))
    assert 'tools' not in request and 'functions' not in request
    assert [m['role'] for m in request['messages']]==['system','user']
    assert json.loads(request['messages'][1]['content'])=={'context':ctx}
    assert set(request)=={'model','messages',*runner.PARAM_KEYS}
    assert all(k not in request for k in ('history','retrieval','memory','search'))

@pytest.mark.parametrize('order',[('A','B'),('B','A')])
def test_stateless_order(config,ctx,ledger,tmp_path,monkeypatch,order):
    a,ca=fake_case(ctx,'MARKER_ONLY_IN_A');b,cb=fake_case(ctx,'clean_B')
    cases={'A':(a,ca),'B':(b,cb)};seen={}
    monkeypatch.setattr(runner,'HERE',tmp_path)
    monkeypatch.setattr(runner,'context_for',lambda s:copy.deepcopy(ca if s['sample_id']==a['sample_id'] else cb))
    def transport(payload):
        c=json.loads(payload['messages'][1]['content'])['context'];key=c['matchup']['home_team']
        seen[key]=copy.deepcopy(payload)
        return response(c)
    for key in order:
        assert run_sample(config,'pregame',cases[key][0],ledger,transport,'test')=='ACCEPTED'
    assert 'MARKER_ONLY_IN_A' not in json.dumps(seen['clean_B'])
    assert not any(k in ledger.__dict__ for k in ('context','messages','response','history','memory'))

def test_failed_and_retry_a_then_clean_b(config,ctx,ledger,tmp_path,monkeypatch):
    a,ca=fake_case(ctx,'MARKER_ONLY_IN_A');b,cb=fake_case(ctx,'clean_B');seen=[]
    monkeypatch.setattr(runner,'HERE',tmp_path)
    monkeypatch.setattr(runner,'context_for',lambda s:copy.deepcopy(ca if s['sample_id']==a['sample_id'] else cb))
    def fail(payload):raise ProviderFailure('http_429','30')
    assert run_sample(config,'pregame',a,ledger,fail,'test')=='FAILED'
    def good(payload):
        seen.append(copy.deepcopy(payload));return response(json.loads(payload['messages'][1]['content'])['context'])
    assert run_sample(config,'pregame',b,ledger,good,'test')=='ACCEPTED'
    assert 'MARKER_ONLY_IN_A' not in json.dumps(seen[-1])
    assert run_sample(config,'pregame',a,ledger,good,'test',retry=True)=='ACCEPTED'
    b2,cb2=fake_case(ctx,'clean_B_after_retry')
    monkeypatch.setattr(runner,'context_for',lambda s:copy.deepcopy(cb2))
    assert run_sample(config,'pregame',b2,ledger,good,'test')=='ACCEPTED'
    assert 'MARKER_ONLY_IN_A' not in json.dumps(seen[-1])
    assert len(list(tmp_path.rglob('artifact.json')))==4

def test_duplicate_output_rejected(config,ctx,ledger,tmp_path,monkeypatch):
    s,c=fake_case(ctx,'unit_case');monkeypatch.setattr(runner,'HERE',tmp_path)
    monkeypatch.setattr(runner,'context_for',lambda _:copy.deepcopy(c))
    assert run_sample(config,'pregame',s,ledger,lambda p:response(c),'test')=='ACCEPTED'
    with pytest.raises(ValueError):run_sample(config,'pregame',s,ledger,lambda p:response(c),'test')
    with pytest.raises(ValueError):run_sample(config,'pregame',s,ledger,lambda p:response(c),'test',True)
    record=ledger.accepted(config['generation_revision'],'pregame',s['sample_id'])
    art=read_json(tmp_path/record['artifact'])
    assert art['response_hash']==runner.digest_bytes(art['raw_response'].encode())
    assert sha(tmp_path/record['artifact'])==record['artifact_hash']
    with pytest.raises(FileExistsError):exclusive(tmp_path/record['artifact'],{})

def test_explicit_new_generation_revision(config,ctx,ledger,tmp_path,monkeypatch):
    s,c=fake_case(ctx,'unit_case');monkeypatch.setattr(runner,'HERE',tmp_path)
    monkeypatch.setattr(runner,'context_for',lambda _:copy.deepcopy(c))
    run_sample(config,'pregame',s,ledger,lambda p:response(c),'test')
    config['generation_revision']='explicit-second-revision'
    run_sample(config,'pregame',s,ledger,lambda p:response(c),'test')
    assert len(list(tmp_path.rglob('artifact.json')))==2

def test_parse_failure_raw_preserved_no_retry(config,ctx,ledger,tmp_path,monkeypatch):
    s,c=fake_case(ctx,'unit_case');monkeypatch.setattr(runner,'HERE',tmp_path)
    monkeypatch.setattr(runner,'context_for',lambda _:copy.deepcopy(c));calls=[]
    def broken(p):calls.append(1);return '{"unparseable":'
    assert run_sample(config,'pregame',s,ledger,broken,'test')=='FAILED'
    assert len(calls)==1
    assert read_json(next(tmp_path.rglob('artifact.json')))['raw_response']=='{"unparseable":'

def test_rate_limit_error_sanitized(config,ctx,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','UNIT_FAKE_SECRET')
    class Fail:
        def open(self,request,timeout):
            assert request.full_url==runner.ENDPOINT
            payload=json.loads(request.data)
            assert 'tools' not in payload
            import urllib.error
            raise urllib.error.HTTPError(request.full_url,429,'UNIT_FAKE_SECRET',{'Retry-After':'30'},None)
    monkeypatch.setattr(runner.urllib.request,'build_opener',lambda *args:Fail())
    with pytest.raises(ProviderFailure) as error:runner.HTTPSProvider(config)(build_request(config,'pregame',ctx,canonical_hash(ctx)))
    assert str(error.value)=='http_429' and error.value.retry_after=='30'
    assert 'UNIT_FAKE_SECRET' not in str(error.value)

def test_tool_response_rejected(ctx):
    raw=json.loads(response(ctx));raw['choices'][0]['message']['tool_calls']=[{}]
    with pytest.raises(ValueError):parse_response(json.dumps(raw),'pregame',ctx)

def test_transport_rejects_assistant_history(config,ctx):
    payload=build_request(config,'pregame',ctx,canonical_hash(ctx))
    payload['messages'].insert(1,{'role':'assistant','content':'MARKER_ONLY_IN_A'})
    with pytest.raises(ProviderFailure,match='history_or_message_protocol'):runner.HTTPSProvider(config)(payload)

def test_postgame_wrong_pregame_revision(config,ledger):
    with ledger.tx() as db:db.execute("UPDATE state SET phase='PREGAME_FROZEN',pre_revision='accepted-pre'")
    config['postgame_pregame_revision']='wrong-pre'
    with pytest.raises(ValueError,match='wrong Pregame revision'):preflight(config,'postgame',ledger)

def test_postgame_no_accepted_output(config,ctx):
    with pytest.raises(ValueError):build_request(config,'postgame',ctx,canonical_hash(ctx))

def test_profile_hash_cannot_change_between_cases(config,ledger):
    ledger.claim('unit','pregame','A',canonical_hash(config),'test')
    config['parameters']['temperature']=.4
    with pytest.raises(ValueError):ledger.claim('unit','pregame','B',canonical_hash(config),'test')

def test_actual_20_preflight(config,ledger):
    report=preflight(config,'pregame',ledger)
    assert report['count']==20 and report['real_calls']==0 and report['phase']=='PREPARED'

def test_full_synthetic_phase_freeze_and_revision(ledger,tmp_path):
    tasks=read_json(runner.GOLD/'pregame_import.json');ids=[t['data']['sample_id'] for t in tasks]
    for sample in ids:
        n=ledger.claim('unit','pregame',sample,'profile','test')
        ledger.finish('unit','pregame',sample,n,'ACCEPTED','test-only','test-only','test')
    ledger.mark_generated('pregame','unit',ids,'test')
    ledger.workspace('pregame',{'project_id':1,'instance':'SYNTHETIC TEMP FIXTURE'})
    ledger.start_annotations('pregame','test')
    rubric=read_json(runner.GOLD/'rubric.json')['pregame']
    for index,t in enumerate(tasks):
        t['data']['model_output']={'article':'SYNTHETIC UNIT FIXTURE NOT REAL OUTPUT'}
        result=[{'from_name':k,'value':{'choices':['PASS']}} for k in rubric['hard_gates']]
        result += [{'from_name':k,'value':{'choices':['3']}} for k in rubric['soft_metrics']]
        t['annotations']=[{'id':index,'completed_by':99,'created_at':'2026-10-11T00:00:00Z','result':result}]
    receipt=ledger.freeze('pregame',tasks,ids,tmp_path/'freeze-1.json','test')
    assert receipt['completed_count']==20 and receipt['annotator_identities']==['99']
    assert ledger.state()['phase']=='PREGAME_FROZEN';ledger.postgame_gate('test')
    with pytest.raises(ValueError):ledger.freeze('pregame',tasks,ids,tmp_path/'bad.json','test')
    original=sha(tmp_path/'freeze-1.json')
    ledger.revise_annotations('pregame','test','explicit synthetic revision')
    second=ledger.freeze('pregame',tasks,ids,tmp_path/'freeze-2.json','test')
    assert second['annotation_revision']==2 and sha(tmp_path/'freeze-1.json')==original
    with ledger.tx() as db:
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM audit')
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM freezes')

def test_less_than_20_annotations_rejected(ledger,tmp_path):
    with pytest.raises(ValueError):ledger.freeze('pregame',[],[],tmp_path/'freeze.json','test')

def test_accepted_basis_tampering_rejected(config,ctx,ledger,tmp_path,monkeypatch):
    s,c=fake_case(ctx,'unit_case');monkeypatch.setattr(runner,'HERE',tmp_path)
    monkeypatch.setattr(runner,'context_for',lambda _:copy.deepcopy(c))
    run_sample(config,'pregame',s,ledger,lambda p:response(c),'test')
    runner.accepted_artifact(ledger,config['generation_revision'],s)
    path=next(tmp_path.rglob('request.json'));record=read_json(path)
    record['exact_payload']['messages'][1]['content']='{}'
    # Deliberate corruption confined to temporary test artifact.
    path.write_text(json.dumps(record),encoding='utf-8')
    with pytest.raises(ValueError,match='request hash mismatch'):runner.accepted_artifact(ledger,config['generation_revision'],s)

def test_postgame_same_sample_input_bound(config,ctx,ledger,tmp_path,monkeypatch):
    sample=runner.samples()[0];monkeypatch.setattr(runner,'HERE',tmp_path)
    run_sample(config,'pregame',sample,ledger,lambda p:response(ctx),'test')
    with ledger.tx() as db:db.execute("UPDATE state SET phase='PREGAME_FROZEN',pre_revision=?",(config['generation_revision'],))
    post_config=copy.deepcopy(config);post_config['generation_revision']='unit-post-revision'
    post_config['postgame_pregame_revision']=config['generation_revision']
    data=runner.bound_postgame_tasks()[sample['sample_id']]
    pre=runner.accepted_artifact(ledger,config['generation_revision'],sample)
    post={'frozen_pregame_output':pre['parsed_output'],'ground_truth':data['ground_truth'],'postgame_evidence':data['postgame_evidence']}
    captured=[]
    def fake(payload):captured.append(payload);return response(ctx)
    assert run_sample(post_config,'postgame',sample,ledger,fake,'test',post=post)=='ACCEPTED'
    assert json.loads(captured[0]['messages'][1]['content'])['frozen_pregame_output']==pre['parsed_output']
    wrong=copy.deepcopy(post);wrong['ground_truth']['target_game_id']='other'
    with pytest.raises(ValueError):run_sample(post_config,'postgame',sample,ledger,fake,'test',post=wrong)
