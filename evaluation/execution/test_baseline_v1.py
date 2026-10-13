"""Synthetic fake-only batch checks; no outputs shown, no real API or truth."""
import copy,json,sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import run_baseline_v1 as b
from asset_scanner import canonical_hash,read_json,sha


def forecast(thesis_string=False):
    obj={'prediction':{'winner_side':'home','win_probability':.55,'expected_home_margin':3.0,'expected_total':223.5},
        'factors':[{'classification':'inference','evidence_ids':['context.team_a'],'effect':'home','summary':'SYNTHETIC'}],
        'uncertainties':[], 'pregame_plan':None,'analysis_core':{'central_question':'SYNTHETIC',
            'central_thesis':{'statement':'SYNTHETIC','classification':'inference','evidence_ids':['context.team_a'],'confidence':'medium'},
            'matchup_status':'no_clear_matchup','key_matchups':[],'expected_game_script':'SYNTHETIC','flip_conditions':[]}}
    if thesis_string:obj['analysis_core']['central_thesis']='SYNTHETIC'
    return obj


def editorial():return {'title':'SYNTHETIC','lead':'NOT A REAL ARTICLE','article':'SYNTHETIC UNIT ONLY',
                       'watch_themes':[],'confidence':'medium','player_analysis':[]}


def raw(obj):return json.dumps({'id':'unit-fixture','model':'deepseek-flash','choices':[{'finish_reason':'stop','message':{'content':json.dumps(obj)}}]})


@pytest.fixture
def case(tmp_path,monkeypatch):
    s,ctx=b.ts.sample_and_context();s={'sample_id':'synthetic-unit','target_game_id':s['target_game_id'],
        'game_date':s['game_date'],'context_sha256':s['context_sha256']}
    monkeypatch.setattr(b,'BASE',tmp_path/'baseline');monkeypatch.setattr(b,'ROOT',tmp_path)
    monkeypatch.setattr(b,'context_for',lambda _:copy.deepcopy(ctx))
    p=b.ts.load_profile();monkeypatch.setattr(b,'assert_contract',lambda:p)
    return s


def test_success_two_calls_sealed_artifacts(case,monkeypatch):
    calls=[]
    def fake(request):calls.append(request);return raw(forecast() if request['max_tokens']==4800 else editorial())
    monkeypatch.setattr(b.ts,'https_once',fake)
    r=b.run_sample(case)
    assert len(calls)==2 and r['chain_status']=='ACCEPTED' and r['content_quality_status']=='NOT_EVALUATED'
    assert r['attempt_counts']=={'forecast':1,'schema_repair':0,'editorial':1}
    assert json.loads(calls[1]['messages'][1]['content'])['locked_forecast']==read_json(b.BASE/'samples'/case['sample_id']/'accepted-forecast.json')['forecast']
    with pytest.raises(ValueError):b.run_sample(case)
    assert len(calls)==2


def test_provider_failure_no_retry_or_writer(case,monkeypatch):
    calls=[]
    def fail(request):calls.append(request);raise b.ts.ProviderFailure('http_429')
    monkeypatch.setattr(b.ts,'https_once',fail)
    r=b.run_sample(case)
    assert len(calls)==1 and r['failure_type']=='PROVIDER_ERROR'
    assert r['editorial_status']=='NOT_RUN' and r['content_quality_status']=='NA'


def test_parse_failure_no_repair(case,monkeypatch):
    calls=[]
    def fake(request):calls.append(request);return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{broken'}}]})
    monkeypatch.setattr(b.ts,'https_once',fake)
    r=b.run_sample(case)
    assert len(calls)==1 and r['parse_status']=='FAILED' and r['repair_triggered'] is False


def test_schema_repair_accept_then_writer(case,monkeypatch):
    calls=[]
    monkeypatch.setattr(b.ts,'https_once',lambda r:raw(forecast(True) if r['max_tokens']==4800 else editorial()))
    def native_fake(messages,key,model,max_tokens,**kwargs):
        calls.append(messages);return SimpleNamespace(content=json.dumps(forecast()),usage={},finish_reason='stop',response_metadata={})
    monkeypatch.setattr(b.provider,'request_deepseek',native_fake)
    r=b.run_sample(case)
    assert len(calls)==1 and r['repair_status']=='ACCEPTED' and r['chain_status']=='ACCEPTED'
    assert r['attempt_counts']=={'forecast':1,'schema_repair':1,'editorial':1}


def test_repair_preservation_failure_no_writer(case,monkeypatch):
    initial=[]
    def initial_fake(request):initial.append(request);return raw(forecast(True))
    monkeypatch.setattr(b.ts,'https_once',initial_fake)
    def native_fake(messages,key,model,max_tokens,**kwargs):
        data=forecast();data['prediction']['expected_home_margin']=4.0
        return SimpleNamespace(content=json.dumps(data),usage={},finish_reason='stop',response_metadata={})
    monkeypatch.setattr(b.provider,'request_deepseek',native_fake)
    r=b.run_sample(case)
    assert len(initial)==1 and r['repair_triggered'] is True
    assert r['repair_schema_status']=='PASSED' and r['repair_content_preservation_status']=='FAILED'
    assert r['failure_type']=='LOCKED_CONTENT_PRESERVATION_ERROR' and r['editorial_status']=='NOT_RUN'


def test_schema_repair_stays_invalid_single_attempt(case,monkeypatch):
    monkeypatch.setattr(b.ts,'https_once',lambda r:raw(forecast(True)))
    calls=[]
    def native_fake(*args,**kwargs):calls.append(1);return SimpleNamespace(content=json.dumps(forecast(True)),usage={},finish_reason='stop',response_metadata={})
    monkeypatch.setattr(b.provider,'request_deepseek',native_fake)
    r=b.run_sample(case)
    assert len(calls)==1 and r['repair_schema_status']=='FAILED' and r['failure_type']=='SCHEMA_ERROR'


def test_initial_acceptance_failure_no_other_protocol_revision(case,monkeypatch):
    data=forecast();data['factors'][0]['evidence_ids']=['invalid-fixture-id'];calls=[]
    def fake(request):calls.append(request);return raw(data)
    monkeypatch.setattr(b.ts,'https_once',fake)
    r=b.run_sample(case)
    assert len(calls)==1 and r['raw_schema_status']=='PASSED' and r['failure_type']=='FORECAST_ACCEPTANCE_ERROR'
    assert not r['repair_triggered'] and r['content_quality_status']=='NA'


def test_editorial_failure_retains_forecast(case,monkeypatch):
    calls=[]
    def fake(request):
        calls.append(request);return raw(forecast()) if len(calls)==1 else json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{bad'}}]})
    monkeypatch.setattr(b.ts,'https_once',fake)
    r=b.run_sample(case)
    assert len(calls)==2 and r['forecast_status']=='ACCEPTED' and r['editorial_status']=='FAILED'
    assert r['chain_status']=='FAILED' and r['failure_type']=='EDITORIAL_ERROR'


def test_original_first_frozen_not_modified():
    before=sha(b.FIRST_RECORD);old=read_json(b.FIRST_RECORD)
    s=next(s for s in b.selection() if s['sample_id']==b.ts.SAMPLE)
    projected=b.import_first(s)
    assert projected['chain_status']=='FAILED' and projected['raw_schema_status']=='FAILED'
    assert projected['repair_schema_status']=='PASSED' and projected['attempt_counts']=={'forecast':1,'schema_repair':1,'editorial':0}
    assert sha(b.FIRST_RECORD)==before and read_json(b.FIRST_RECORD)==old
