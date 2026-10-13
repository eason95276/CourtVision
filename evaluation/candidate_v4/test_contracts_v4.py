import copy,sys
from pathlib import Path
import pytest,jsonschema
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v4 import contracts_v4 as c
from evaluation.candidate_v2.test_contracts import context,output

@pytest.mark.parametrize('p',[.51,.55,.56,.57,.58,.69,.99])
def test_legal_decimal_probabilities(p):
    o=output();o['prediction']['win_probability']=p;c.validate(o,context())

@pytest.mark.parametrize('p',[.5,.501,.561,.999,1.0])
def test_real_invalid_probabilities_rejected(p):
    o=output();o['prediction']['win_probability']=p
    with pytest.raises((ValueError,jsonschema.ValidationError)):c.validate(o,context())

def test_clear_v3_checker_bug_reproduced():
    o=output();o['prediction']['win_probability']=.56
    with pytest.raises(jsonschema.ValidationError):c.v3.validate(o,context())
    c.validate(o,context());assert o['prediction']['win_probability']==.56

def test_schema_and_length_semantics_unchanged():
    assert c.schema(context())==c.v3.schema(context())
    o=output();o['analysis_core']['central_question']='X'*181
    with pytest.raises(jsonschema.ValidationError):c.validate(o,context())

@pytest.mark.parametrize('bad',['unknown','wrong_team','changed_prediction','deleted_uncertainty'])
def test_other_boundaries_not_relaxed(bad):
    o=output();z=copy.deepcopy(o)
    if bad=='unknown':z['factors'][0]['evidence_ids']=['invented']
    elif bad=='wrong_team':z['pregame_plan']['lineups'][0]['players']=['Away Player']
    elif bad=='changed_prediction':z['prediction']['expected_total']=225.0
    else:z['uncertainties']=[]
    with pytest.raises((ValueError,jsonschema.ValidationError)):c.preserve(o,z,context())
