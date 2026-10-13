import json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v3 import deterministic_metrics as d

def case(revision=None):
    sample=d.a.v2.b.selection()[0];row=d.a.load(revision or d.a.v2.REV,sample)
    return d.LLMTestCase(input=json.dumps(row['context']),actual_output=json.dumps(d.model_outputs(row)),metadata={'revision':row['revision'],'sample_id':sample['sample_id']})

def test_actual_sdk_metric_success_and_failure():
    d._CACHE.clear();m=d.DeterministicGate('execution_success')
    assert m.measure(case())==1
    assert m.measure(case(d.a.v2.b.REV))==0

@pytest.mark.parametrize('key',d.KEYS)
def test_altered_actual_output_never_scores_as_valid(key):
    c=case();c.actual_output='{}';m=d.DeterministicGate(key);assert m.measure(c)==0

def test_cross_sample_context_rejected():
    c=case();obj=json.loads(c.input);obj['matchup']['target_game_id']='nba:another';c.input=json.dumps(obj)
    assert d.DeterministicGate('context_binding').measure(c)==0

def test_no_content_not_human_low_score():
    row=d.a.load(d.a.v2.b.REV,d.a.v2.b.selection()[0]);assert row['editorial'] is None
    assert row['record']['content_quality_status']=='NA'
