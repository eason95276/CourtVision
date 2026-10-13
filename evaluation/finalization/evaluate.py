"""Eight bounded deterministic DeepEval checks, Final revision only."""
import os,socket,importlib.metadata
import sys
from collections import Counter
from unittest.mock import patch
os.environ['DEEPEVAL_TELEMETRY_OPT_OUT']='YES'
os.environ['DEEPEVAL_NO_INSPECT_PROMPT']='1'
from deepeval import evaluate
from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase
from deepeval.evaluate.configs import AsyncConfig,DisplayConfig,CacheConfig
from .common import *
from .run import verify,ctx_for
from . import contracts as c
if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8')

KEYS=('execution_success','parse_success','schema_compliance','evidence_validity','temporal_integrity','ground_truth_isolation','context_binding','output_contract_validity')
CHECKS={}
def checks(s):
    folder=BASE/'samples'/s['sample_id'];r=read(folder/'chain-manifest.json');ctx=ctx_for(s);f=read(folder/'forecast/artifact.json')
    qualification=read(HERE/'qualification.json');eligible={x['sample_id'] for x in qualification['rows'] if x['qualified_e2e']}
    success=s['sample_id'] in eligible;accepted=read(folder/'accepted-forecast.json') if (folder/'accepted-forecast.json').exists() else None
    referenced=False
    if accepted:
        try:c.accept(accepted,c.independent_context(ctx));referenced=True
        except Exception:pass
    editorial=None
    if success:
        art=read(folder/'editorial/artifact.json');editorial=art['parsed_output']
        referenced=referenced and all(set(p['fact_ids'])<={e['id'] for e in c.catalog(ctx,True)[0]} for p in editorial['player_analysis'])
    cutoff=ctx['cutoff'];temporal=True
    for side,key in (('home','team_a'),('away','team_b')):
        for p in ctx[key]['player_history']['players']:
            if p['last_observed_before_target'][:10]>=ctx['matchup']['game_date']:temporal=False
    if ctx['injury'].get('report_time') and ctx['injury']['report_time']>cutoff:temporal=False
    temporal=temporal and all(e['published_at']<=cutoff for e in ctx['research']['evidence'])
    requests=list(folder.glob('*/request.json'));isolation=True;bound=True
    for p in requests:
        req=read(p);wire=req['request']
        try:check_visible(json.loads(wire['messages'][1]['content']))
        except Exception:isolation=False
        isolation=isolation and len(wire['messages'])==2 and [m['role'] for m in wire['messages']]==['system','user'] and not wire.get('tools') and not wire.get('functions')
        bound=bound and req['context_hash']==s['context_hash'] and req['request_hash']==digest(wire)
    for path,h in r['refs'].items():bound=bound and digest((folder/path).read_bytes())==h
    if success:bound=bound and digest(read(folder/'locked-final.json'))==r['final_hash']
    return {'execution_success':success,'parse_success':f['parse_status']=='PASSED','schema_compliance':f['raw_schema_status']=='PASSED',
       'evidence_validity':referenced,'temporal_integrity':temporal,'ground_truth_isolation':isolation,'context_binding':bound,'output_contract_validity':success and editorial is not None}

class Gate(BaseMetric):
    def __init__(self,key):
        self.key=key;self.threshold=1;self.async_mode=False;self.strict_mode=False;self.verbose_mode=False;self.include_reason=True;self.evaluation_model=None;self.evaluation_cost=0;self.error=None
    def measure(self,test_case,*args,**kw):
        self.score=float(CHECKS[test_case.metadata['sample_id']][self.key]);self.success=bool(self.score);self.reason=self.key+' receipt check, not semantic quality or strict historical PIT certification';return self.score
    async def a_measure(self,test_case,*args,**kw):return self.measure(test_case)
    @property
    def __name__(self):return self.key
def main():
    cfg=verify();cases=[]
    for s in cfg['samples']:
        CHECKS[s['sample_id']]=checks(s)
        cases.append(LLMTestCase(name=REV+':'+s['sample_id'],input=json.dumps(ctx_for(s),ensure_ascii=False),actual_output=json.dumps(read(BASE/'samples'/s['sample_id']/'chain-manifest.json'),ensure_ascii=False),metadata={'sample_id':s['sample_id']}))
    def blocked(*args,**kw):raise RuntimeError('deterministic_eval_no_network')
    with patch.object(socket.socket,'connect',blocked),patch.object(socket,'create_connection',blocked):
        result=evaluate(cases,[Gate(k) for k in KEYS],async_config=AsyncConfig(run_async=False),display_config=DisplayConfig(show_indicator=False,print_results=False,inspect_after_run=False),cache_config=CacheConfig(write_cache=False,use_cache=False))
    rows=[];counts=Counter()
    for case,test in zip(cases,result.test_results):
        metrics=[{'name':m.name,'score':m.score,'success':m.success,'reason':m.reason,'error':m.error} for m in test.metrics_data]
        if any(m['error'] for m in metrics):raise ValueError('sdk_execution_error')
        counts.update(m['name'] for m in metrics if m['score']==1)
        rows.append({'sample_id':case.metadata['sample_id'],'metrics':metrics})
    report={'revision':REV,'n_records':len(rows),'checks':len(rows)*len(KEYS),'sdk_version':importlib.metadata.version('deepeval'),'real_sdk_evaluate_called':True,
        'passes':dict(counts),'failures':{k:len(rows)-counts[k] for k in KEYS},'total_property_passed':sum(counts.values()),'total_property_failed':len(rows)*len(KEYS)-sum(counts.values()),'rows':rows,'judge_calls':0,'network_blocked':True,
        'definitions':{k:'Binary bounded receipt/property check; see checks() and per-record results.' for k in KEYS},'content_quality_not_measured':True}
    write(HERE/'deepeval-final.json',report);print(json.dumps({'records':len(rows),'checks':report['checks'],'pass_counts':dict(counts)}))
if __name__=='__main__':main()
