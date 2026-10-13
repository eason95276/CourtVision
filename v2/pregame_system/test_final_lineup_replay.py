"""Offline replay of the actual failed Heat FINAL; never publishes that game."""
import copy
import json
from pathlib import Path
import pytest
from .pregame_estimates import PregamePlan,validate_pregame_plan
from .forecasting_workflow import run_two_stage


def fixture():return json.loads((Path(__file__).parent/'fixtures/heat_final_lineup_basis_20261004.json').read_text(encoding='utf-8'))


def test_frozen_final_partial_source_was_mislabeled_as_complete_source_lineup(tmp_path):
    f=fixture();raw=f['raw_forecast'];b=f['basketball']
    assert len(f['frozen_response_sha256'])==64
    with pytest.raises(ValueError,match='lineup_basis_source_unverified:Davion Mitchell'):
        validate_pregame_plan(PregamePlan.model_validate(raw['pregame_plan']),b)
    calls=[]
    def client(messages,schema):
        calls.append(messages)
        if len(calls)==1:return copy.deepcopy(raw),{}
        candidate=copy.deepcopy(json.loads(messages[-1]['content'])['protocol_correction']['candidate'])
        candidate['pregame_plan']['lineups'][1]['basis']='agent_expected'
        candidate['pregame_plan']['lineups'][1]['rationale']='Current members; partial season projection is not this-game source-confirmed lineup. This is my estimate.'
        return candidate,{}
    result=run_two_stage(b,{'evidence':[]},tmp_path/'replay',client=client,skill_loader=lambda:'FORECAST')
    assert len(calls)==2 and result['final_prediction']==raw['prediction']
    assert result['review']['pregame_plan']['lineups'][1]['basis']=='agent_expected'


@pytest.mark.parametrize('bad',['wrong_identity','confirmed_out'])
def test_agent_estimate_never_bypasses_membership_or_confirmed_out(bad):
    f=fixture();plan=copy.deepcopy(f['raw_forecast']['pregame_plan']);plan['lineups'][1]['basis']='agent_expected'
    b=f['basketball']
    if bad=='wrong_identity':plan['lineups'][1]['players'][0]='Unverified Player'
    else:b['evidence'].append({'id':'out','section':'availability','value':{'records':[{'team_id':1610612748,
        'game_id':b['game']['game_id'],'player_name':'Giannis Antetokounmpo','status':'Out'}]}})
    with pytest.raises(ValueError,match='current_identity_unverified|confirmed_out'):
        validate_pregame_plan(PregamePlan.model_validate(plan),b)


def test_reference_repair_does_not_consume_lineup_basis_repair(tmp_path):
    f=fixture();calls=[]
    def client(messages,schema):
        calls.append(messages);candidate=copy.deepcopy(f['raw_forecast'])
        if len(calls)==1:candidate['factors'][2]['evidence_ids'][0]='8e4e16b1f4f325ced112df8f68919f463dccc149e7cbc5ec66fdfa788ab578f'
        if len(calls)==3:candidate['pregame_plan']['lineups'][1]['basis']='agent_expected'
        return candidate,{}
    result=run_two_stage(f['basketball'],{'evidence':[]},tmp_path/'replay',client=client,skill_loader=lambda:'FORECAST')
    assert len(calls)==3 and result['final_prediction']==f['raw_forecast']['prediction']
    assert (tmp_path/'replay'/'independent_protocol_revision_2_response.json').exists()
