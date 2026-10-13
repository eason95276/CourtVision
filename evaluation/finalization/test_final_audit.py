"""Synthetic workspace states only; no annotations submitted or fabricated."""
import copy
import pytest
from .common import *
from .verify_closeout import validate_human_workspace

def prepared():return [{'meta':{'sample_id':'synthetic-'+str(i),'revision':'test-only'},'data':{'text':'fixture'}} for i in range(3)]
def submitted_task(task):
    out=copy.deepcopy(task);r=read(ROOT/'evaluation/golden_set/v1/rubric.json')['pregame']
    out['annotations']=[{'id':'synthetic-not-published','result':[{'from_name':key,'type':'choices','value':{'choices':['PASS' if key in r['hard_gates'] else '3']}} for key in list(r['hard_gates'])+list(r['soft_metrics'])]}]
    return out
def test_initial_workspace_still_available():
    p=prepared();s=validate_human_workspace([p[0]],p,initial=True)
    assert s['human_labels']==0 and s['human_visible_tasks']==1
def test_real_annotation_prefix_no_longer_rejected():
    p=prepared();s=validate_human_workspace([submitted_task(p[0]),p[1]],p)
    assert s['human_labels']==1 and s['human_visible_tasks']==2
    with pytest.raises(ValueError,match='initial'):validate_human_workspace([submitted_task(p[0]),p[1]],p,initial=True)
def test_changed_binding_still_rejected():
    p=prepared();x=copy.deepcopy(p[0]);x['meta']['revision']='different'
    with pytest.raises(ValueError,match='drift'):validate_human_workspace([x],p)
def test_nonchronological_or_duplicate_task_rejected():
    p=prepared()
    with pytest.raises(ValueError,match='prefix'):validate_human_workspace([p[1]],p)
    with pytest.raises(ValueError,match='prefix'):validate_human_workspace([p[0],p[0]],p)
def test_automatic_judge_predictions_still_rejected():
    p=prepared();x=copy.deepcopy(p[0]);x['predictions']=[{'model_version':'test-only'}]
    with pytest.raises(ValueError,match='automatic'):validate_human_workspace([x],p)
