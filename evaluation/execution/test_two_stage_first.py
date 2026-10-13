"""Fake-only component regression; no truth files, providers or human labels."""
import copy,json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import two_stage_first as ts
from control import Control
from asset_scanner import read_json,canonical_hash,sha


def fake_forecast():
    return {'prediction':{'winner_side':'home','win_probability':.55,'expected_home_margin':3.0,'expected_total':223.5},
        'factors':[{'classification':'inference','evidence_ids':['context.team_a'],'effect':'home','summary':'SYNTHETIC TEST ONLY'}],
        'uncertainties':[],'pregame_plan':None,
        'analysis_core':{'central_question':'SYNTHETIC QUESTION','central_thesis':{'statement':'SYNTHETIC CLAIM','classification':'inference',
            'evidence_ids':['context.team_a'],'confidence':'medium'},'matchup_status':'no_clear_matchup','key_matchups':[],
            'expected_game_script':'SYNTHETIC SCRIPT','flip_conditions':[]}}


def fake_editorial():
    return {'title':'SYNTHETIC UNIT TEST','lead':'NO REAL ARTICLE','article':'SYNTHETIC UNIT TEST ONLY',
        'watch_themes':[],'confidence':'medium','player_analysis':[]}


def raw(data):
    return json.dumps({'id':'unit-fixture','model':'deepseek-v4-flash','choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}]})


@pytest.fixture
def controlled(tmp_path,monkeypatch):
    monkeypatch.setattr(ts,'HERE',tmp_path)
    monkeypatch.setattr(ts,'RUN',tmp_path/'outputs'/ts.REVISION/ts.SAMPLE)
    return Control(tmp_path/'control.sqlite')


def test_model_and_request_protocol():
    _,ctx=ts.sample_and_context()
    first=ts.payload('forecasting',ts.forecast_messages(ctx));second=ts.payload('editorial',ts.editorial_messages(ctx,fake_forecast()))
    assert first['temperature']==second['temperature']==.1
    assert first['max_tokens']==4800 and second['max_tokens']==3500
    assert first['model']==second['model']=='deepseek-v4-flash'
    for packet in (first,second):
        assert packet['thinking']=={'type':'disabled'}
        assert set(packet)=={'model','messages','temperature','max_tokens','thinking','stream'}
        assert [m['role'] for m in packet['messages']]==['system','user']
        assert '<local-configured-path>' not in packet['messages'][0]['content']
    assert json.loads(first['messages'][1]['content'])['closed_book_context']==ctx
    assert json.loads(second['messages'][1]['content'])=={'closed_book_context':ctx,'locked_forecast':fake_forecast()}


def test_forecast_durable_before_writer_and_chain_bound(controlled):
    calls=[]
    def fake(packet):
        calls.append(packet)
        if len(calls)==2:
            artifact=next((ts.RUN/'forecasting').rglob('artifact.json'))
            saved=read_json(artifact)
            assert saved['status']=='ACCEPTED'
            assert json.loads(packet['messages'][1]['content'])['locked_forecast']==saved['parsed_output']
        return raw(fake_forecast() if len(calls)==1 else fake_editorial())
    manifest=ts.execute(fake,controlled)
    assert len(calls)==2 and manifest['model_calls']==2
    assert sha(ts.HERE/manifest['forecast_artifact'])==manifest['forecast_artifact_hash']
    assert sha(ts.HERE/manifest['editorial_artifact'])==manifest['editorial_artifact_hash']
    assert ts.load_chain()['editorial']==fake_editorial()
    with pytest.raises(ValueError,match='destination not empty'):ts.execute(fake,controlled)
    assert len(calls)==2


def test_forecast_failure_no_editorial_no_retry(controlled):
    calls=[]
    def fail(packet):calls.append(packet);return '{bad json'
    with pytest.raises(ValueError,match='forecasting_FAILED'):ts.execute(fail,controlled)
    assert len(calls)==1 and not (ts.RUN/'editorial').exists()
    artifact=read_json(next(ts.RUN.rglob('artifact.json')))
    assert artifact['status']=='FAILED' and artifact['raw_response']=='{bad json'


def test_editorial_failure_preserves_first_forecast(controlled):
    calls=[]
    def fake(packet):
        calls.append(packet);return raw(fake_forecast()) if len(calls)==1 else '{bad writer'
    with pytest.raises(ValueError,match='editorial_FAILED'):ts.execute(fake,controlled)
    assert len(calls)==2
    assert read_json(next((ts.RUN/'forecasting').rglob('artifact.json')))['status']=='ACCEPTED'
    assert not (ts.RUN/'chain-manifest.json').exists()


def test_chain_rejects_component_tamper(controlled):
    ts.execute(lambda packet:raw(fake_forecast() if packet['max_tokens']==4800 else fake_editorial()),controlled)
    artifact=next((ts.RUN/'forecasting').rglob('artifact.json'));record=read_json(artifact)
    record['parsed_output']['prediction']['expected_home_margin']=5.0
    artifact.write_text(json.dumps(record),encoding='utf-8')
    with pytest.raises(ValueError,match='artifact hash'):ts.load_chain()


def test_phase_not_advanced_to_twenty_generated(controlled):
    ts.execute(lambda packet:raw(fake_forecast() if packet['max_tokens']==4800 else fake_editorial()),controlled)
    assert controlled.state()['phase']=='PREPARED'
    with pytest.raises(ValueError):controlled.postgame_gate('unit-test')


def test_no_direct_single_request_formal_entry():
    source=(Path(__file__).parent/'runner.py').read_text(encoding='utf-8')
    assert 'Single-request 0.2/4096 preset retired' in source

def test_batch_workspace_not_reopened(controlled):
    from workspace import start
    with pytest.raises(ValueError,match='Chronological single-sample scope active'):
        start('pregame',controlled,'unit-test')
