"""Same20 immutable V3. Frozen transport/model, one initial/repair/writer each."""
import argparse,copy,json,sys,time,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v2 import run as v2
from evaluation.candidate_v3 import contracts_v3 as c
from control import exclusive,now
from asset_scanner import EVAL,ROOT,read_json,sha,canonical_hash

REV='golden-pregame-system-candidate-v3'
HERE=Path(__file__).resolve().parent
BASE=EVAL/'execution/candidates'/REV

def messages(stage,ctx,forecast=None,original=None,error=None):
    if stage=='forecast':
        system=(HERE/'forecast_protocol.txt').read_text(encoding='utf-8')+'\n'+v2.ts.embedded_skill('forecasting')+'\n本场JSON schema（包含合法evidence enum）：\n'+json.dumps(c.schema(ctx),ensure_ascii=False)
        user={'closed_book_context':ctx,'evidence_catalog':v2.pointers(ctx)}
    elif stage=='repair':
        system=(HERE/'repair_protocol.txt').read_text(encoding='utf-8')+'\nJSON schema:\n'+json.dumps(c.schema(ctx),ensure_ascii=False)
        user={'closed_book_context':ctx,'evidence_catalog':v2.pointers(ctx),'original_response':original,'validation_error':error}
    else:
        base=v2.messages('editorial',ctx,forecast);system=base[0]['content'];user=json.loads(base[1]['content'])
    return [{'role':'system','content':system},{'role':'user','content':json.dumps(user,ensure_ascii=False,allow_nan=False)}]

def contract_body():
    parent=v2.verify();samples=v2.b.selection()
    files=[Path(__file__),HERE/'contracts_v3.py',HERE/'forecast_protocol.txt',HERE/'repair_protocol.txt',HERE/'trace_v3.py']
    return {'system_revision':REV,'dataset_revision':v2.b.DATASET_ID,'golden_set_revision':v2.b.GOLDEN_ID,
        'parent_candidate_hash':parent['contract_hash'],'selection_hash':parent['selection_hash'],'sample_ids':[s['sample_id'] for s in samples],
        'source_hashes':{str(p.relative_to(ROOT)):sha(p) for p in files},'skill_hashes':parent['skill_hashes'],
        'provider':'deepseek','model':'deepseek-v4-flash','generation':parent['generation_parameters'],'tools':[],'retrieval':False,'history':False,
        'max_calls':60,'repair_limit_per_sample':1,'quality_retry_limit':0,'normalization':'Exact frozen member resolution + lossless formatting on copies, no paraphrase/truncate',
        'local_schema_only_enum':True,'provider_schema_enforced':False,'length_limits_changed':False,
        'sample_contracts':{s['sample_id']:{'context_hash':s['context_sha256'],'schema_hash':canonical_hash(c.schema(v2.b.context_for(s))),
            'forecast_prompt_hash':canonical_hash(messages('forecast',v2.b.context_for(s))[0]),
            'repair_prompt_hash':canonical_hash(messages('repair',v2.b.context_for(s),original={})[0]),
            'editorial_prompt_hash':canonical_hash(messages('editorial',v2.b.context_for(s),{})[0])} for s in samples}}

def prepare():
    if (BASE/'contract.json').exists():return verify()
    value=contract_body();value['contract_hash']=canonical_hash(value);exclusive(BASE/'contract.json',value);return value

def verify():
    saved=read_json(BASE/'contract.json')
    if saved['contract_hash']!=canonical_hash({k:v for k,v in saved.items() if k!='contract_hash'}):raise ValueError('contract tamper')
    # Verify all pinned source/Skill/parent/sample bytes without reconstructing20 prompts per call.
    v2.verify()
    for path,digest in saved['source_hashes'].items():
        if sha(ROOT/path)!=digest:raise ValueError('candidate source drift')
    if saved['sample_ids']!=[s['sample_id'] for s in v2.b.selection()]:raise ValueError('selection drift')
    return saved

def raw_schema(obj,stage,ctx):
    if stage=='editorial':
        v2.ts.EditorialDraft.model_validate(obj)
        if 'player_analysis' not in obj:raise ValueError('required_player_analysis_missing')
    else:
        c.jsonschema.validate(obj,c.schema(ctx))
        v2.c.CandidateForecast.model_validate(obj)

def emit_trace(sample,stage,art):
    try:
        from evaluation.candidate_v3 import trace_v3
        return trace_v3.emit(REV,sample['sample_id'],stage,art)
    except Exception as e:return {'error':type(e).__name__+':'+str(e)[:150]}

def component(sample,stage,req,ctx,folder,transport):
    if len(list(BASE.glob('samples/*/*/request.json')))>=60:raise ValueError('call budget exhausted')
    exclusive(folder/'request.json',{'sample_id':sample['sample_id'],'revision':REV,'created_at':now(),'request':req,'request_hash':canonical_hash(req)})
    art={'sample_id':sample['sample_id'],'system_revision':REV,'dataset_revision':v2.b.DATASET_ID,'golden_set_revision':v2.b.GOLDEN_ID,
        'stage':stage,'created_at':now(),'context_hash':sample['context_sha256'],'request_hash':canonical_hash(req),
        'prompt_hash':canonical_hash(req['messages'][0]),'schema_hash':canonical_hash(c.schema(ctx)) if stage!='editorial' else canonical_hash(v2.ts.EditorialDraft.model_json_schema()),
        'skill_hashes':verify()['skill_hashes'],'config':{k:v for k,v in req.items() if k!='messages'},
        'provider_status':'FAILED','parse_status':'NOT_RUN','protocol_status':'NOT_RUN','raw_schema_status':'NOT_RUN','raw_response':None,'parsed_output':None,'error':None}
    start=time.monotonic()
    try:
        raw=transport(copy.deepcopy(req));art['raw_response']=raw;art['response_hash']=canonical_hash(raw)
        exclusive(folder/'raw-response.json',{'raw_response':raw,'response_hash':canonical_hash(raw)})
        env=json.loads(raw);art['provider_metadata']={k:env.get(k) for k in ('id','model','system_fingerprint','created','usage')};art['provider_status']='SUCCEEDED'
        choice=env['choices'][0];art['finish_reason']=choice.get('finish_reason');art['parse_status']='FAILED'
        art['parsed_output']=json.loads(choice['message'].get('content'));art['parse_status']='PASSED'
        if len(env['choices'])!=1 or choice.get('finish_reason')!='stop' or choice['message'].get('tool_calls') or choice['message'].get('function_call'):raise ValueError('response_protocol_failure')
        art['protocol_status']='PASSED';art['raw_schema_status']='FAILED';raw_schema(art['parsed_output'],stage,ctx);art['raw_schema_status']='PASSED'
    except Exception as e:art['error']={'type':type(e).__name__,'message':str(e)[:1500]}
    art['latency_seconds']=round(time.monotonic()-start,3);art['artifact_hash']=canonical_hash(art);exclusive(folder/'artifact.json',art)
    exclusive(folder/'trace-receipt.json',emit_trace(sample,stage,art));return art

def accept(output,ctx):
    result=c.validate(output,ctx);v2.fw.validate_independent_market_boundary(result);return result.model_dump()

def preflight(sample,ctx):
    locked=verify()['sample_contracts'][sample['sample_id']]
    if locked['context_hash']!=canonical_hash(ctx) or locked['schema_hash']!=canonical_hash(c.schema(ctx)):raise ValueError('context/schema drift')
    for stage in ('forecast','repair','editorial'):
        if canonical_hash(messages(stage,ctx,{} if stage=='editorial' else None,{} if stage=='repair' else None)[0])!=locked[stage+'_prompt_hash']:raise ValueError('prompt drift')
    return {'status':'PASS','sample_id':sample['sample_id'],'context_hash':sample['context_sha256'],'tools':[],'history':[],'external_retrieval':False,'target_truth_files_opened':0}

def execute_sample(sample,transport=v2.https_once):
    ctx=v2.b.context_for(sample);flight=preflight(sample,ctx);folder=BASE/'samples'/sample['sample_id']
    if folder.exists():raise ValueError('sample already attempted, no rerun')
    exclusive(folder/'preflight.json',flight)
    record={'sample_id':sample['sample_id'],'game_id':sample['target_game_id'],'game_date':sample['game_date'],'system_revision':REV,
        'context_hash':sample['context_sha256'],'dataset_revision':v2.b.DATASET_ID,'golden_set_revision':v2.b.GOLDEN_ID,
        'forecast_status':'FAILED','editorial_status':'NOT_RUN','chain_status':'FAILED','content_quality_status':'NA',
        'repair_triggered':False,'repair_status':'NOT_RUN','failure_stage':None,'failure_type':None,'attempt_counts':{'forecast':1,'repair':0,'editorial':0}}
    initial=component(sample,'forecast',v2.request('forecast',messages('forecast',ctx)),ctx,folder/'forecast',transport)
    record.update(provider_status=initial['provider_status'],parse_status=initial['parse_status'],raw_schema_status=initial['raw_schema_status'])
    accepted=None;error=initial['error'];ops=[]
    if initial['protocol_status']=='PASSED':
        try:
            norm,ops=c.normalize(initial['parsed_output'],ctx);accepted=accept(norm,ctx)
            exclusive(folder/'canonical-receipt.json',{'raw_output_hash':canonical_hash(initial['parsed_output']),'canonical_output':norm,'operations':ops,'numbers_unchanged':initial['parsed_output']['prediction']==norm['prediction']})
        except Exception as e:
            error={'type':type(e).__name__,'message':str(e)[:1500]}
            try:v2.fw.ForecastNumbers.model_validate(initial['parsed_output']['prediction']);eligible=initial['raw_schema_status']=='FAILED'
            except (ValueError,KeyError,TypeError):eligible=False
            if eligible:
                record['repair_triggered']=True;record['attempt_counts']['repair']=1
                repaired=component(sample,'repair',v2.request('repair',messages('repair',ctx,original=initial['parsed_output'],error=error)),ctx,folder/'repair',transport)
                record['repair_status']='FAILED';record['repair_schema_status']=repaired['raw_schema_status']
                if repaired['protocol_status']=='PASSED':
                    try:
                        c.preserve(initial['parsed_output'],repaired['parsed_output'],ctx);norm,ops=c.normalize(repaired['parsed_output'],ctx);accepted=accept(norm,ctx);record['repair_status']='ACCEPTED'
                        exclusive(folder/'canonical-receipt.json',{'raw_output_hash':canonical_hash(initial['parsed_output']),'repair_output_hash':canonical_hash(repaired['parsed_output']),
                            'canonical_output':norm,'before_repair_semantics':c.canonical_semantics(initial['parsed_output'],ctx),'after_repair_semantics':c.canonical_semantics(repaired['parsed_output'],ctx),'operations':ops,'semantic_lock':'PASSED'})
                    except Exception as e:error={'type':type(e).__name__,'message':str(e)[:1500]}
                else:error=repaired['error']
    if accepted is not None:
        record['forecast_status']='ACCEPTED';exclusive(folder/'accepted-forecast.json',{'sample_id':sample['sample_id'],'context_hash':sample['context_sha256'],'forecast':accepted,'normalization_operations':ops})
        persisted=read_json(folder/'accepted-forecast.json');record['forecast_artifact_hash']=sha(folder/'accepted-forecast.json')
        record['attempt_counts']['editorial']=1
        writer=component(sample,'editorial',v2.request('editorial',messages('editorial',ctx,persisted['forecast'])),ctx,folder/'editorial',transport)
        if writer['raw_schema_status']=='PASSED' and writer['protocol_status']=='PASSED':record.update(editorial_status='ACCEPTED',chain_status='SUCCEEDED',content_quality_status='PENDING_HUMAN');error=None
        else:record.update(editorial_status='FAILED',failure_stage='EDITORIAL',failure_type='EDITORIAL_CONTRACT');error=writer['error']
    else:record.update(failure_stage='FORECAST',failure_type='REPAIR_OR_SCHEMA' if initial['raw_schema_status']=='FAILED' else 'PARSE_OR_PROVIDER')
    record['normalization_operations']=ops;record['error']=error;record['created_at']=now()
    record['artifact_refs']=[{'path':str(p.relative_to(ROOT)),'sha256':sha(p)} for p in sorted(folder.rglob('*.json'))]
    record['manifest_hash']=canonical_hash(record);exclusive(folder/'chain-manifest.json',record)
    # Final acceptance is separately traced, not misrepresented as provider/schema success.
    receipt=emit_trace(sample,'chain_acceptance',{'latency_seconds':0,'provider_status':record['provider_status'],'parse_status':record['parse_status'],'raw_schema_status':record['raw_schema_status'],
        'error':error,'chain_status':record['chain_status'],'failure_type':record['failure_type'],'request_hash':record['manifest_hash'],'provider_metadata':{}})
    exclusive(folder/'chain-trace-receipt.json',receipt)
    print(json.dumps({'sample_id':sample['sample_id'],'chain':record['chain_status'],'calls':record['attempt_counts'],'error':error},ensure_ascii=True),flush=True);return record

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','verify','execute']);p.add_argument('--authorize-real-api',action='store_true');args=p.parse_args()
    if args.command=='prepare':print(json.dumps({'contract_hash':prepare()['contract_hash'],'revision':REV}))
    elif args.command=='verify':print(json.dumps({'verified':verify()['contract_hash']}))
    else:
        if not args.authorize_real_api:raise ValueError('explicit API authorization required')
        verify()
        for sample in v2.b.selection():
            if (BASE/'samples'/sample['sample_id']/'chain-manifest.json').exists():continue
            execute_sample(sample)
if __name__=='__main__':main()
