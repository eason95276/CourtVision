import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v4 import execution_v4 as r
from evaluation.candidate_v3 import execution_v3 as prior
from evaluation.candidate_v2.test_contracts import context,output

def test_same_model_prompt_schema_and_only_decimal_delta():
    ctx=context();assert r.messages('forecast',ctx)==prior.messages('forecast',ctx)
    assert r.c.schema(ctx)==prior.c.schema(ctx)
    o=output();o['prediction']['win_probability']=.56;r.raw_schema(o,'forecast',ctx)

def test_v3_results_remain_immutable():
    assert prior.verify()['sample_ids']==r.v2.verify()['sample_ids']
    assert len(list(prior.BASE.glob('samples/*/chain-manifest.json')))==20
