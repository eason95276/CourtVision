"""Strict structural transport, no network or model calls."""
import json
from types import SimpleNamespace
import pytest
from pydantic import BaseModel,Field,ValidationError
from .strict_forecast_transport import strict_schema,request_strict_forecast,TOOL_NAME,ENDPOINT

class Fixture(BaseModel):
    value: float=Field(ge=0,le=1)
    note: str|None=None

def test_strict_schema_requires_all_fields_without_loosening_local_validation():
    converted=strict_schema(Fixture.model_json_schema())
    assert converted['required']==['value','note']and converted['additionalProperties']is False
    assert converted['properties']['note']['anyOf'][1]['type']=='null'
    with pytest.raises(ValidationError):Fixture.model_validate({'value':2,'note':None})

@pytest.mark.parametrize('mode',['success','wrong_tool','truncated','invalid_json'])
def test_forced_tool_response_is_preserved_or_rejected(mode):
    raw='{"value":0.6,"note":null}'if mode!='invalid_json'else'{"value":'
    seen=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def read(self):return json.dumps({'model':'fixture','usage':{'total_tokens':12},'choices':[{
            'finish_reason':'length'if mode=='truncated'else'tool_calls','message':{'tool_calls':[{
                'function':{'name':'wrong'if mode=='wrong_tool'else TOOL_NAME,'arguments':raw}}]}}]}).encode()
    def opener(request,timeout):
        seen.append(json.loads(request.data));assert request.full_url==ENDPOINT
        assert timeout==5
        return Response()
    def invoke():return request_strict_forecast([{'role':'user','content':'{}'}],'fixture-key','deepseek-v4-flash',Fixture,100,5,opener=opener)
    if mode=='success':
        result=invoke();assert result.content==raw and result.response_metadata['output_protocol']=='strict_tool_v2'
    else:
        with pytest.raises(ValueError):invoke()
    assert len(seen)==1
    assert seen[0]['tools'][0]['function']['strict']is True
    assert seen[0]['tool_choice']['function']['name']==TOOL_NAME
    assert 'response_format'not in seen[0]


def test_array_limits_remain_in_instruction_and_local_validation():
    class Collection(BaseModel):
        candidates:list[str]=Field(max_length=5)
    converted=strict_schema(Collection.model_json_schema())
    field=converted['properties']['candidates']
    assert 'maxItems'not in field and 'maxItems=5'in field['description']
    with pytest.raises(ValidationError):Collection.model_validate({'candidates':['x']*6})


def test_roster_identity_is_conditioned_on_team_in_tool_schema():
    class Lineup(BaseModel):
        side:str
        players:list[str]
        candidates:list[str]
    converted=strict_schema(Lineup.model_json_schema(),{'home':['Home Player'],'away':['Away Player']})
    for branch,side,name in zip(converted['anyOf'],['home','away'],['Home Player','Away Player']):
        assert branch['properties']['side']['enum']==[side]
        assert branch['properties']['players']['items']['enum']==[name]
        assert branch['properties']['candidates']['properties']['slot_1']['anyOf'][0]['enum']==[name]


def test_optional_slot_decoding_changes_no_facts_names_or_prediction_numbers():
    from .strict_forecast_transport import decode_candidate_slots
    source={'prediction':{'expected_home_margin':3.5,'expected_total':220.5},'analysis_core':{'statement':'unchanged'},
        'pregame_plan':{'lineups':[{'side':'home','players':['A','B','C','D','E'],'candidates':{
            'slot_1':'Reserve A','slot_2':None,'slot_3':'Reserve B','slot_4':None,'slot_5':None}}]}}
    normalized,changes=decode_candidate_slots(source)
    assert normalized['prediction']==source['prediction']and normalized['analysis_core']==source['analysis_core']
    assert normalized['pregame_plan']['lineups'][0]['players']==source['pregame_plan']['lineups'][0]['players']
    assert normalized['pregame_plan']['lineups'][0]['candidates']==['Reserve A','Reserve B']
    assert len(changes)==1 and isinstance(source['pregame_plan']['lineups'][0]['candidates'],dict)
