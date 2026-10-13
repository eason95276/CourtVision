"""Execute one existing production schema-repair continuation, unchanged.

The initial response is replayed locally, not regenerated. Production AST
statements are executed verbatim; instrumentation only records receipts and
caps the newly authorized provider call at one. No production file is edited.
"""
import argparse,ast,copy,json,sys,tempfile,urllib.request
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import two_stage_first as ts
from control import Control,exclusive,now
from asset_scanner import read_json,sha,canonical_hash
from runner import digest_bytes
from v2.pregame_system import forecasting_workflow as fw
from v2.agent import generate_historical_pregame_analysis as provider

REPAIR_ROOT=ts.RUN/'production-schema-recovery'
PARENT=ts.RUN/'forecasting/attempt-1/artifact.json'
ROLE='forecasting_schema_repair'


def continuation():
    """Production call + repair handler + acceptance, stopping before market."""
    tree=ast.parse(ts.SOURCE_FILES['forecasting'].read_text(encoding='utf-8'))
    outer=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_run_two_stage_impl')
    call=next(n for n in outer.body if isinstance(n,ast.FunctionDef) and n.name=='call')
    repair_try=next(n for n in outer.body if isinstance(n,ast.Try) and any(
        isinstance(h.type,ast.Name) and h.type.id=='ValidationError' for h in n.handlers))
    handler=next(h for h in repair_try.handlers if isinstance(h.type,ast.Name) and h.type.id=='ValidationError')
    prefix=ast.parse('calls=0\nrequire_analysis_core=True\nindependent_schema=VersionedIndependentForecast\nschema_repair_used=False\nschema_repair_provenance=None\ntransport_label=None').body
    body=[*prefix,copy.deepcopy(call),*copy.deepcopy(handler.body)]
    for stmt in outer.body[outer.body.index(repair_try)+1:]:
        if isinstance(stmt,ast.If) and any(isinstance(n,ast.Name) and n.id=='independent_value' for n in ast.walk(stmt.test)):
            raise ValueError('unexpected production continuation boundary')
        if isinstance(stmt,ast.If) and isinstance(stmt.test,ast.BoolOp) and any(
            isinstance(n,ast.Name) and n.id=='schema_repair_provenance' for n in ast.walk(stmt.test)) and any(
            isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='collection' for t in n.targets) for n in stmt.body):
            clipped=copy.deepcopy(stmt);clipped.body=[]
            for item in stmt.body:
                if isinstance(item,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='collection' for t in item.targets):break
                clipped.body.append(copy.deepcopy(item))
            else:raise ValueError('production market boundary not found')
            body.append(clipped);break
        body.append(copy.deepcopy(stmt))
    else:raise ValueError('production durable receipt boundary not found')
    body+=ast.parse('return {"independent":independent_value,"independent_hash":independent_hash,"schema_repair_provenance":schema_repair_provenance,"calls":calls}').body
    args=ast.arguments(posonlyargs=[],args=[ast.arg(arg=n) for n in ('folder','basketball','original_error','client','skill')],
        kwonlyargs=[],kw_defaults=[],defaults=[],vararg=None,kwarg=None)
    node=ast.FunctionDef(name='production_recovery',args=args,body=body,decorator_list=[])
    namespace=dict(vars(fw))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),
        '<unchanged production schema repair continuation>','exec'),namespace)
    return namespace['production_recovery']


def original_inputs():
    config=ts.load_profile();sample,context=ts.sample_and_context()
    parent=read_json(PARENT)
    previous=read_json(ts.EVAL/'reports/first-real-pregame-two-stage-result.json')
    if sha(PARENT)!=previous['artifact_sha256'] or parent['status']!='FAILED' or parent['attempt']!=1:
        raise ValueError('original immutable attempt identity/hash mismatch')
    if digest_bytes(parent['raw_response'].encode())!=parent['response_hash']:
        raise ValueError('original raw-response hash mismatch')
    request=parent['exact_model_visible_request']
    if canonical_hash(request)!=parent['request_hash'] or parent['context_hash']!=config['context_hash']:
        raise ValueError('original request/context hash mismatch')
    basketball=json.loads(request['messages'][1]['content'])
    if basketball!=ts.basketball_adapter(context):raise ValueError('repair would use different original input')
    envelope=json.loads(parent['raw_response']);content=envelope['choices'][0]['message']['content']
    original=json.loads(content)
    try:fw.VersionedIndependentForecast.model_validate(original)
    except fw.ValidationError as error:
        return config,parent,basketball,content,error
    raise ValueError('original is not schema-invalid; repair not authorized')


def seed_replay(folder,content,error):
    folder.mkdir(parents=True,exist_ok=False)
    # This is a local replay receipt in a NEW directory, not a new provider attempt.
    fw.save(folder/'independent_response.json',{'raw':content,'replayed_original_attempt':str(PARENT.relative_to(ts.HERE)),
        'replay_only':True,'provider_call':False,'original_artifact_sha256':sha(PARENT)})
    fw.save(folder/'independent_validation_error.json',{'error_type':type(error).__name__,
        'errors':[{'loc':list(e['loc']),'type':e['type'],'msg':e['msg']} for e in error.errors(include_url=False)],
        'message':str(error).splitlines()[0][:400]})


class RequestCaptured(BaseException):pass


def lock_request():
    config,parent,basketball,content,error=original_inputs();captured={}
    def capture(messages,schema):
        captured['messages']=copy.deepcopy(messages);captured['schema']=schema.__name__
        raise RequestCaptured()
    with tempfile.TemporaryDirectory(prefix='courtvision-repair-lock-') as folder:
        path=Path(folder)/'native-replay';seed_replay(path,content,error)
        try:continuation()(path,basketball,error,capture,ts.embedded_skill('forecasting'))
        except RequestCaptured:pass
    if captured.get('schema')!='VersionedIndependentForecast':raise ValueError('repair request not reached')
    messages=captured['messages'];payload=ts.payload('forecasting',messages)
    repair_input=json.loads(messages[1]['content'])
    if repair_input['basketball']!=basketball or repair_input['original_response']!=json.loads(content):
        raise ValueError('production repair input differs from original')
    if [m['role'] for m in messages]!=['system','user'] or set(payload)&{'tools','functions','tool_choice','retrieval'}:
        raise ValueError('history/tool leak')
    return {'config':config,'parent_hash':sha(PARENT),'exact_request':payload,
        'request_hash':canonical_hash(payload),'prompt_hash':digest_bytes(messages[0]['content'].encode()),
        'repair_triggered':True,'provider_calls':0,'production_source_hash':sha(ts.SOURCE_FILES['forecasting'])}


def execute_once():
    control=Control();control.assert_phase(('PREPARED',))
    if REPAIR_ROOT.exists():raise ValueError('single recovery already started; second repair forbidden')
    config,parent,basketball,content,error=original_inputs();lock=lock_request()
    before=ts.protection();attempt=control.claim(ts.REVISION,ROLE,ts.SAMPLE,canonical_hash(config),'human-authorized-single-production-repair')
    REPAIR_ROOT.mkdir(parents=True,exist_ok=False)
    exclusive(REPAIR_ROOT/'preflight-lock.json',lock)
    exclusive(REPAIR_ROOT/'protection-before.json',before)
    native=REPAIR_ROOT/'production';seed_replay(native,content,error)
    frame={'calls':0,'provider_status':'NOT_RUN','raw_http':None,'usage':None,'request':None}
    original_transport=provider.request_deepseek

    class RecordingResponse:
        def __init__(self,response):self.response=response
        def __enter__(self):self.response.__enter__();return self
        def __exit__(self,*args):return self.response.__exit__(*args)
        def read(self,*args):
            raw=self.response.read(*args);frame['raw_http']=raw.decode('utf-8')
            exclusive(REPAIR_ROOT/'raw-provider-response.json',{'raw_response':frame['raw_http'],
                'response_hash':digest_bytes(raw),'http_status':self.response.status})
            return raw
        def __getattr__(self,key):return getattr(self.response,key)

    def recorded_request(messages,key,model,max_tokens,*,timeout_seconds,opener=None):
        if frame['calls']:raise ValueError('second provider call forbidden')
        frame['calls']+=1
        def record_opener(request,timeout):
            exact=json.loads(request.data.decode('utf-8'));frame['request']=exact
            if canonical_hash(exact)!=lock['request_hash'] or exact!=lock['exact_request']:
                raise ValueError('wire request differs from production repair lock')
            exclusive(REPAIR_ROOT/'exact-wire-request.json',{'payload':exact,'request_hash':canonical_hash(exact),
                'timeout_seconds':timeout,'credentials_excluded':True})
            return RecordingResponse(urllib.request.urlopen(request,timeout=timeout))
        return original_transport(messages,key,model,max_tokens,timeout_seconds=timeout_seconds,opener=record_opener)

    def client(messages,schema):
        if schema is not fw.VersionedIndependentForecast or messages!=lock['exact_request']['messages']:
            raise ValueError('production repair schema/prompt changed')
        with patch.object(provider,'request_deepseek',recorded_request):
            result=fw.default_client(messages,schema)
        frame['provider_status']='SUCCEEDED';frame['usage']=result[1]
        return result

    accepted=None;failure=None
    try:accepted=continuation()(native,basketball,error,client,ts.embedded_skill('forecasting'))
    except Exception as exc:
        failure={'type':type(exc).__name__,'message':str(exc)[:600]}
        if frame['provider_status']=='NOT_RUN' and frame['calls']:frame['provider_status']='FAILED'
    parse_status='NOT_RUN';schema_status='NOT_RUN';parsed=None
    response_path=native/'independent_schema_repair_response.json'
    if response_path.exists():
        model_raw=read_json(response_path)['raw']
        try:parsed=json.loads(model_raw) if isinstance(model_raw,str) else model_raw;parse_status='PASSED'
        except (ValueError,TypeError):parse_status='FAILED'
    if parse_status=='PASSED':
        schema_status='PASSED' if (native/'independent_schema_repair_validation.json').exists() else 'FAILED'
    acceptance=read_json(native/'independent_schema_repair_acceptance.json') if (native/'independent_schema_repair_acceptance.json').exists() else None
    provenance=read_json(native/'schema_repair_provenance.json') if (native/'schema_repair_provenance.json').exists() else None
    failure_type=(None if accepted else 'PROVIDER_ERROR' if frame['provider_status']!='SUCCEEDED'
        else 'JSON_PARSE_ERROR' if parse_status!='PASSED' else 'SCHEMA_VALIDATION_ERROR' if schema_status!='PASSED'
        else 'LOCKED_CONTENT_PRESERVATION_ERROR' if not (acceptance or{}).get('content_lock_validated') else 'PRODUCTION_ACCEPTANCE_ERROR')
    artifact={'sample_id':ts.SAMPLE,'game_id':parent['game_id'],'dataset_revision':parent['dataset_revision'],
        'golden_set_revision':parent['golden_set_revision'],'generation_revision':ts.REVISION,'created_at':now(),
        'attempt_role':'production_schema_repair','attempt':2,'component_attempt':attempt,
        'parent_attempt':1,'parent_artifact_sha256':lock['parent_hash'],'status':'ACCEPTED' if accepted else 'FAILED',
        'provider':'deepseek','model':'deepseek-v4-flash','generation_parameters':{k:v for k,v in lock['exact_request'].items() if k not in ('messages','model')},
        'context_hash':parent['context_hash'],'prompt_hash':lock['prompt_hash'],'request_hash':lock['request_hash'],
        'exact_model_visible_request':lock['exact_request'],'response_hash':digest_bytes(frame['raw_http'].encode()) if frame['raw_http'] else None,
        'raw_provider_response':frame['raw_http'],'parsed_output':parsed,'accepted_independent':accepted,
        'provider_metadata':frame['usage'],'production_acceptance':acceptance,'production_provenance':provenance,
        'provider_status':frame['provider_status'],'parse_status':parse_status,'schema_status':schema_status,
        'repair_triggered':bool(frame['calls']),'repair_status':'ACCEPTED' if accepted else 'FAILED',
        'forecast_status':'ACCEPTED' if accepted else 'FAILED','editorial_status':'NOT_RUN',
        'chain_status':'RUNNING' if accepted else 'FAILED','failure_stage':None if accepted else 'FORECAST_SCHEMA_REPAIR',
        'failure_type':failure_type,'failure':failure,'native_source_hash':lock['production_source_hash'],
        'normal_retries':0,'additional_repairs':0,'manual_output_edits':0}
    if sha(PARENT)!=lock['parent_hash']:raise ValueError('original immutable attempt changed')
    after=ts.protection();protection={k:canonical_hash(v)==canonical_hash(after[k]) for k,v in before.items()}
    if not all(protection.values()):raise ValueError('production protection changed')
    exclusive(REPAIR_ROOT/'artifact.json',artifact)
    control.finish(ts.REVISION,ROLE,ts.SAMPLE,attempt,artifact['status'],
        (REPAIR_ROOT/'artifact.json').relative_to(ts.HERE),sha(REPAIR_ROOT/'artifact.json'),'human-authorized-single-production-repair')
    result={'sample_id':ts.SAMPLE,'game_id':parent['game_id'],'dataset_revision':parent['dataset_revision'],
        'golden_set_revision':parent['golden_set_revision'],'generation_revision':ts.REVISION,'created_at':now(),
        **{k:artifact[k] for k in ('provider_status','parse_status','schema_status','repair_triggered','repair_status',
            'forecast_status','editorial_status','chain_status','failure_stage','failure_type')},
        'initial_attempt_status':{'provider_status':'SUCCEEDED','parse_status':'PASSED','schema_status':'FAILED'},
        'initial_artifact':str(PARENT),'initial_artifact_sha256':lock['parent_hash'],
        'repair_artifact':str(REPAIR_ROOT/'artifact.json'),'repair_artifact_sha256':sha(REPAIR_ROOT/'artifact.json'),
        'content_quality':'NA','attempt_counts':{'initial_forecast':1,'schema_repair':frame['calls'],'editorial':0,
            'normal_retry':0,'second_repair':0,'total_provider_calls':1+frame['calls']},'production_acceptance':acceptance,
        'production_repair_error':(provenance or{}).get('repair_error'),'protection':protection,
        'original_attempt_unchanged':sha(PARENT)==lock['parent_hash'],'other_samples_generated':0,'postgame_generated':0}
    if accepted:
        # This branch is solely the authorized continuation; no new Forecast.
        independent=accepted['independent'];fore={'sample_id':ts.SAMPLE,'component':'forecasting',
            'status':'ACCEPTED','generation_revision':ts.REVISION,'parsed_output':independent,
            'source_repair_artifact_sha256':sha(REPAIR_ROOT/'artifact.json')}
        exclusive(REPAIR_ROOT/'accepted-forecast.json',fore)
        frozen_hash=sha(REPAIR_ROOT/'accepted-forecast.json')
        frozen_forecast=read_json(REPAIR_ROOT/'accepted-forecast.json')
        if frozen_forecast!=fore or sha(REPAIR_ROOT/'accepted-forecast.json')!=frozen_hash:
            raise ValueError('accepted Forecast durable receipt mismatch')
        second=ts.payload('editorial',ts.editorial_messages(ts.sample_and_context()[1],frozen_forecast['parsed_output']))
        if digest_bytes(second['messages'][0]['content'].encode())!=config['editorial_prompt_hash']:
            raise ValueError('locked Editorial prompt mismatch')
        try:
            path,editorial=ts.component('editorial',second,ts.load_profile(),control,ts.https_once,sha(REPAIR_ROOT/'accepted-forecast.json'))
            result.update(editorial_status='ACCEPTED',chain_status='ACCEPTED',content_quality='NOT_SCORED',
                editorial_artifact=str(path),editorial_artifact_sha256=sha(path))
        except Exception as exc:
            result.update(editorial_status='FAILED',chain_status='FAILED',failure_stage='EDITORIAL',failure_type='EDITORIAL_EXECUTION_ERROR',
                failure={'type':type(exc).__name__},content_quality='NA')
        result['attempt_counts']['editorial']=1;result['attempt_counts']['total_provider_calls']+=1
        if result['chain_status']=='ACCEPTED':
            manifest={**result,'forecast_artifact':str(REPAIR_ROOT/'accepted-forecast.json'),
                'forecast_artifact_sha256':sha(REPAIR_ROOT/'accepted-forecast.json'),
                'components':{'forecast_schema_repair':sha(REPAIR_ROOT/'artifact.json'),'editorial':result['editorial_artifact_sha256']}}
            manifest['manifest_hash']=canonical_hash(manifest);exclusive(REPAIR_ROOT/'chain-manifest.json',manifest)
    result['record_hash']=canonical_hash(result)
    exclusive(REPAIR_ROOT/'baseline-final-record.json',result)
    control.audit('baseline_sample_frozen','human-authorized-single-production-repair',
        {'sample_id':ts.SAMPLE,'chain_status':result['chain_status'],'record_hash':result['record_hash']})
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['preflight','execute'])
    p.add_argument('--authorize-single-production-repair',action='store_true');a=p.parse_args()
    if a.command=='execute':
        if not a.authorize_single_production_repair:raise ValueError('explicit authorization required')
        value=execute_once()
    else:
        lock=lock_request();value={k:lock[k] for k in ('parent_hash','request_hash','prompt_hash','repair_triggered','provider_calls','production_source_hash')}
        value.update(status='PASS',schema='VersionedIndependentForecast',model='deepseek-v4-flash',temperature=.1,max_tokens=4800,thinking='disabled')
    print(json.dumps(value,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
