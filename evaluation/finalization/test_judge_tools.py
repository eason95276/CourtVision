from .common import *
from .judge_final import compact_context
from .data import CONTEXTS
from .contracts import ExactValidator
from .judge import schema

def test_compaction_keeps_facts_and_market():
    s=read(CONTEXTS/'final-manifest.json')['samples'][0];ctx=read(ROOT/s['context_path']);out=compact_context(ctx)
    assert out['matchup']==ctx['matchup']
    assert out['team_a']['record']==ctx['team_a']['record']
    assert out['injury']['records']==ctx['injury']['records']
    assert out['market']['reference']['home_spread']==ctx['market']['reference']['home_spread']
    assert out['market']['reference']['source']==ctx['market']['reference']['source']
    assert len(json.dumps(out))<len(json.dumps(ctx))
def test_human_tasks_no_judge_or_intermediate_forecast():
    tasks=read(HERE/'human/tasks.json');assert len(tasks)==15
    for t in tasks:
        check_visible(t);assert 'judge' not in t['data'] and 'forecast' not in t['data'] and 'predictions' not in t
        assert t['meta']['revision']==REV
def test_provider_failure_not_an_accepted_judge():
    a=read(BASE/'judge-final-v1/cb-c55e9597dfab8e9de493/artifact.json')
    assert a['provider_status']=='FAILED' and a['raw_schema_status']=='NOT_RUN' and 'parsed_output' not in a

def test_qualification_retains_raw_history():
    q=read(HERE/'qualification.json');assert q['raw_e2e']==15 and q['qualified_e2e']==14 and q['n']==20
    assert len(read(HERE/'human/tasks-qualified.json'))==14
    assert len(read(HERE/'human/tasks.json'))==15

def test_validation_exception_cannot_leave_accepted(monkeypatch,tmp_path):
    from . import run
    sid='cb-0050013765f1198cade4'
    s=next(x for x in read(BASE/'contract.json')['samples'] if x['sample_id']==sid)
    ctx=read(ROOT/s['context_path']);obj=read(BASE/'samples'/sid/'accepted-forecast.json');calls=[]
    def fake_component(sample,stage,*args,**kwargs):
        calls.append(stage)
        return {'provider_status':'SUCCEEDED','parse_status':'PASSED','raw_schema_status':'PASSED','finish_reason':'stop','parsed_output':obj,'error':None}
    monkeypatch.setattr(run,'BASE',tmp_path);monkeypatch.setattr(run,'verify',lambda:{})
    monkeypatch.setattr(run,'ctx_for',lambda sample:ctx);monkeypatch.setattr(run,'component',fake_component)
    result=run.execute(s,transport=lambda req:None)
    assert result['chain_status']=='FAILED' and result['forecast_status']=='FAILED' and result['editorial_status']=='NOT_RUN'
    assert calls==['forecast'] and not (tmp_path/'samples'/sid/'accepted-forecast.json').exists()
