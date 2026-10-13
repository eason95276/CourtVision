"""Generic fake-only checks; no editing or repairing the real model response."""
import copy,json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import execute_first_schema_recovery as recovery
from v2.pregame_system import forecasting_workflow as fw


def synthetic_original():
    return {'prediction':{'winner_side':'home','win_probability':.55,'expected_home_margin':3.0,'expected_total':223.5},
        'factors':[{'classification':'inference','evidence_ids':['fixture.fact'],'effect':'home','summary':'SYNTHETIC ONLY'}],
        'uncertainties':[], 'analysis_core':{'central_question':'SYNTHETIC QUESTION',
            'central_thesis':'SYNTHETIC THESIS','matchup_status':'no_clear_matchup',
            'key_matchups':[],'expected_game_script':'SYNTHETIC SCRIPT','flip_conditions':[]}}


def replay(tmp_path,original,client):
    try:fw.VersionedIndependentForecast.model_validate(original)
    except fw.ValidationError as error:
        folder=tmp_path/'native';recovery.seed_replay(folder,json.dumps(original),error)
        result=recovery.continuation()(folder,{'game':{'game_type':'regular_season'},
            'evidence':[{'id':'fixture.fact','section':'team_form','value':{'scope':'synthetic'}}]},error,client,'SYNTHETIC SKILL')
        return folder,result
    raise AssertionError('synthetic input must fail schema')


def synthetic_repaired():
    obj=synthetic_original()
    obj['analysis_core']['central_thesis']={'statement':'SYNTHETIC THESIS','classification':'inference',
        'evidence_ids':['fixture.fact'],'confidence':'medium'}
    return obj


def test_native_success_exactly_one_fake_repair_and_preserves_content(tmp_path):
    calls=[]
    def client(messages,schema):
        calls.append(messages)
        assert schema is fw.VersionedIndependentForecast
        assert json.loads(messages[1]['content'])['repair_mode']=='schema_only'
        return json.dumps(synthetic_repaired()),{}
    folder,result=replay(tmp_path,synthetic_original(),client)
    assert len(calls)==1 and result['calls']==1
    assert result['schema_repair_provenance']['status']=='accepted'
    assert (folder/'independent.json').exists()
    assert json.loads((folder/'independent_response.json').read_text())['replay_only'] is True


def test_native_schema_failure_has_no_second_repair(tmp_path):
    calls=[]
    def client(messages,schema):calls.append(messages);return json.dumps(synthetic_original()),{}
    with pytest.raises(ValueError,match='forecast_schema_repair_failed'):replay(tmp_path,synthetic_original(),client)
    assert len(calls)==1 and not (tmp_path/'native/independent.json').exists()


def test_native_preservation_failure_after_schema_pass(tmp_path):
    calls=[]
    def client(messages,schema):
        calls.append(messages);candidate=synthetic_repaired();candidate['prediction']['expected_home_margin']=4.0
        return json.dumps(candidate),{}
    with pytest.raises(ValueError,match='forecast_schema_repair_failed'):replay(tmp_path,synthetic_original(),client)
    assert len(calls)==1
    assert (tmp_path/'native/independent_schema_repair_validation.json').exists()
    state=json.loads((tmp_path/'native/independent_schema_repair_acceptance.json').read_text())
    assert state['schema_validated'] is True and state['content_lock_validated'] is False


def test_real_preflight_no_model_call_original_hash_locked():
    lock=recovery.lock_request()
    assert lock['provider_calls']==0 and lock['repair_triggered'] is True
    assert lock['parent_hash']=='d5e51fbeb61fd1e247537c162e2e99d7cdf36b3b54227e99c6a817c1346d3f6e'
    assert lock['exact_request']['max_tokens']==4800
    assert lock['exact_request']['temperature']==.1
    assert 'tools' not in lock['exact_request'] and 'functions' not in lock['exact_request']
