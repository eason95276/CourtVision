"""Formal DeepEval custom metrics on immutable real outputs, zero Judge/network."""
import json,os,socket,sys,importlib.metadata
from pathlib import Path
from collections import Counter,defaultdict
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
os.environ['DEEPEVAL_TELEMETRY_OPT_OUT']='YES'
os.environ['DEEPEVAL_NO_INSPECT_PROMPT']='1'
if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8')
from deepeval import evaluate
from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase
from deepeval.evaluate.configs import AsyncConfig,DisplayConfig,CacheConfig
from evaluation.candidate_v3 import analysis_tools as a
from asset_scanner import EVAL,canonical_hash
from control import exclusive,now

KEYS=('execution_success','parse_success','schema_compliance','evidence_validity','temporal_integrity','ground_truth_isolation','context_binding','output_contract_validity')
_CACHE={}

def model_outputs(row):return {'initial_forecast':row['initial']['parsed'],'accepted_forecast':row['accepted'],'editorial':row['editorial']}

class DeterministicGate(BaseMetric):
    def __init__(self,key):
        if key not in KEYS:raise ValueError('unknown deterministic metric')
        self.key=key;self.threshold=1.0;self.async_mode=False;self.strict_mode=False;self.verbose_mode=False;self.include_reason=True
        self.evaluation_model=None;self.evaluation_cost=0;self.error=None
    def measure(self,test_case,*args,**kwargs):
        meta=test_case.metadata;pair=(meta['revision'],meta['sample_id'])
        try:
            if pair not in _CACHE:
                sample=next(s for s in a.v2.b.selection() if s['sample_id']==meta['sample_id']);row=a.load(meta['revision'],sample)
                _CACHE[pair]=(row,a.metric_checks(row))
            row,checks=_CACHE[pair]
            if json.loads(test_case.actual_output)!=model_outputs(row) or json.loads(test_case.input)!=row['context']:
                ok=False;reason='SDK case does not match the immutable actual outputs/Context'
            else:ok,reason=checks[self.key]
            self.score=float(ok);self.reason=reason;self.success=bool(ok);self.error=None
        except Exception as exc:self.score=0.0;self.success=False;self.reason='Verification exception: '+type(exc).__name__+':'+str(exc)[:180];self.error=None
        return self.score
    async def a_measure(self,test_case,*args,**kwargs):return self.measure(test_case)
    @property
    def __name__(self):return self.key

def blocked_network(*args,**kwargs):raise RuntimeError('DeepEval deterministic workload forbids outbound network/Judge')

def run():
    _CACHE.clear();cases=[];revisions=[]
    for revision in a.REVISIONS:
        if not all((a.folder_for(revision,s)/('final-record.json' if revision==a.v2.b.REV else 'chain-manifest.json')).exists() for s in a.v2.b.selection()):continue
        revisions.append(revision)
        for sample in a.v2.b.selection():
            row=a.load(revision,sample)
            cases.append(LLMTestCase(name=revision+':'+sample['sample_id'],input=json.dumps(row['context'],ensure_ascii=False),
                actual_output=json.dumps(model_outputs(row),ensure_ascii=False),metadata={'revision':revision,'sample_id':sample['sample_id']}))
    # SDK real execution, not a hand-produced replacement report. No remote endpoint allowed.
    with patch.object(socket.socket,'connect',blocked_network),patch.object(socket,'create_connection',blocked_network):
        result=evaluate(cases,[DeterministicGate(k) for k in KEYS],async_config=AsyncConfig(run_async=False),
            display_config=DisplayConfig(show_indicator=False,print_results=False,inspect_after_run=False),cache_config=CacheConfig(write_cache=False,use_cache=False))
    rows=[];counts=defaultdict(Counter)
    if len(result.test_results)!=len(cases):raise ValueError('DeepEval result count mismatch')
    for test,case in zip(result.test_results,cases):
        revision=case.metadata['revision'];sample=case.metadata['sample_id'];metrics=[]
        for metric in test.metrics_data:
            score=metric.score;metrics.append({'name':metric.name,'score':score,'success':metric.success,'reason':metric.reason,'error':metric.error})
            if metric.error:raise ValueError('SDK metric execution error')
            counts[revision][metric.name]+=int(score==1)
        if {m['name'] for m in metrics}!=set(KEYS):raise ValueError('missing deterministic metric')
        rows.append({'revision':revision,'sample_id':sample,'metrics':metrics,'sdk_success_all_gates':test.success})
    report={'created_at':now(),'sdk':'deepeval','sdk_version':importlib.metadata.version('deepeval'),'real_sdk_evaluate_called':True,
        'n_records':len(rows),'metric_evaluations':len(rows)*8,'revisions':revisions,'denominator_per_revision':20,
        'counts':{rev:dict(counts[rev]) for rev in revisions},'judge_calls':0,'outbound_network_blocked':True,
        'binary_gate_threshold':1,'content_quality_pass_threshold':None,'rows':rows,
        'limitations':'Deterministic execution/reference/temporal-input checks, not LLM Judge, article factual certification, Human quality or strict PIT. Failed generation scores0 for applicable execution gates, content quality remainsNA.'}
    exclusive(EVAL/'reports/deepeval-deterministic-regression.json',report)
    text='# Formal DeepEval deterministic regression\n\nActual SDK evaluate() with8 custom BaseMetric metrics and LLMTestCase objects; network disabled during SDK execution. No Judge or Human quality scoring.\n\n| Revision | '+ ' | '.join(KEYS)+' |\n|---|'+'---:|'*len(KEYS)+'\n'
    for revision in revisions:text+='| '+revision+' | '+' | '.join(str(counts[revision][k])+'/20' for k in KEYS)+' |\n'
    text+='\n'+report['limitations']+'\n\nSDK '+report['sdk_version']+'; '+str(len(rows))+' records; '+str(len(rows)*8)+' metric evaluations. Non-generation input checks may pass even for an execution-failed case; this does not grade nonexistent content.'
    with (EVAL/'reports/deepeval-deterministic-regression.md').open('x',encoding='utf-8') as f:f.write(text)
    print(json.dumps({'records':len(rows),'metric_evaluations':len(rows)*8,'counts':report['counts'],'judge_calls':0}))
if __name__=='__main__':run()
