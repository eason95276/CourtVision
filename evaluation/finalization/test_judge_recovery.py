import copy,json
import pytest
from .common import *
from .resume_judge import RECOVERY,eligible_samples
from .judge_results import load_labels,summarize

def test_recovery_exact_frozen_protocol():
    assert read(RECOVERY/'contract.json')==read(BASE/'judge-final-v1/contract.json')
    assert len(eligible_samples())==14
    old=read(BASE/'judge-final-v1/cb-c55e9597dfab8e9de493/request.json')
    new=read(RECOVERY/'cb-c55e9597dfab8e9de493/request.json')
    assert old['request_hash']==new['request_hash']
    assert read(BASE/'judge-final-v1/cb-c55e9597dfab8e9de493/artifact.json')['provider_status']=='FAILED'
    assert read(RECOVERY/'cb-c55e9597dfab8e9de493/artifact.json')['provider_status']=='SUCCEEDED'

def test_qualified_only_no_held_sample():
    index,labels=load_labels()
    assert len(labels)==14 and len(index['rows'])==14
    assert all(row['sample_id']!='cb-0050013765f1198cade4' for row in index['rows'])
    assert len({row['sample_id'] for row in index['rows']})==14

def test_model_and_context_requests_stateless():
    for s in eligible_samples():
        r=read(RECOVERY/s['sample_id']/'request.json');req=r['request'];check_visible(json.loads(req['messages'][1]['content']))
        assert req['model']=='deepseek-v4-flash' and req['thinking']=={'type':'disabled'}
        assert req['temperature']==0.1 and req['max_tokens']==3500
        assert [m['role'] for m in req['messages']]==['system','user']
        assert 'tools' not in req and 'functions' not in req

def test_judge_all_articles_schema_and_literal_quotes():
    index,labels=load_labels()
    for row,a in labels:
        assert a['raw_schema_status']=='PASSED' and a['error'] is None and a['quote_audit_status']=='PASSED'
        assert all(q['exact'] for q in a['quote_audit'])

def test_score_denominator_does_not_exclude_execution_failures():
    s=summarize()
    assert s['n_golden']==20 and s['n_qualified_generation']==14 and s['execution_fail_or_hold_content_NA']==6
    assert s['n_accepted_labels']==14 and s['human_labels']==0 and s['human_judge_agreement'] is None
    assert all(v['n']==14 for v in s['soft_metrics'].values())

def test_recovery_index_not_selected_for_high_scores():
    index=read(RECOVERY/'index.json')
    assert index['pro_scores_pooled'] is False and index['quality_retries']==0
    assert all(row['artifact_path'].startswith(str(RECOVERY.relative_to(ROOT))) for row in index['rows'])

def test_recovery_does_not_add_forecast_editorial_calls():
    assert len(list((BASE/'samples').glob('*/*/request.json')))==55
    auth=read(RECOVERY/'authorization.json');assert auth['forecast_editorial_calls']==0

def test_local_read_only_export_500_retries_once(monkeypatch):
    import io,urllib.error
    from . import human
    calls=[]
    def fake(req,timeout):
        calls.append(req.get_method())
        if len(calls)==1:raise urllib.error.HTTPError(req.full_url,500,'test',{},None)
        return io.BytesIO(b'[]')
    monkeypatch.setattr(human.urllib.request,'urlopen',fake)
    assert human.api('/api/projects/1/export?exportType=JSON')==[] and calls==['GET','GET']

def test_mutating_annotation_import_never_retried(monkeypatch):
    import urllib.error
    from . import human
    calls=[]
    def fake(req,timeout):
        calls.append(req.get_method());raise urllib.error.HTTPError(req.full_url,500,'test',{},None)
    monkeypatch.setattr(human.urllib.request,'urlopen',fake)
    with pytest.raises(urllib.error.HTTPError):human.api('/api/projects/1/import',[])
    assert calls==['POST']
