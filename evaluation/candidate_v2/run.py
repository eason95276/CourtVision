"""Pinned one-pass Candidate V2 evaluation. No production writes or retrieval."""
import argparse,copy,json,os,sys,time,urllib.request,urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'execution'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import contracts as c
import run_baseline_v1 as b
import two_stage_first as ts
from control import exclusive,now
from asset_scanner import EVAL,ROOT,read_json,sha,canonical_hash
from runner import ENDPOINT
from v2.pregame_system import forecasting_workflow as fw

REV='golden-pregame-system-candidate-v2'
HERE=Path(__file__).resolve().parent
BASE=EVAL/'execution/candidates'/REV

def pointers(ctx):
    return [{k:v for k,v in e.items() if k!='value'} for e in c.catalog(ctx)['entries']]

def messages(stage,ctx,forecast=None,original=None,error=None):
    if stage=='forecast':
        system=(HERE/'forecast_protocol.txt').read_text(encoding='utf-8')+'\n'+ts.embedded_skill('forecasting')+'\nJSON schema:\n'+json.dumps(c.schema(),ensure_ascii=False)
        user={'closed_book_context':ctx,'evidence_catalog':pointers(ctx)}
    elif stage=='repair':
        system=(HERE/'repair_protocol.txt').read_text(encoding='utf-8')+'\nJSON schema:\n'+json.dumps(c.schema(),ensure_ascii=False)
        user={'closed_book_context':ctx,'evidence_catalog':pointers(ctx),'original_response':original,'validation_error':error}
    else:
        system=ts.editorial_messages(ctx,forecast)[0]['content']+'\n'+(HERE/'editorial_protocol.txt').read_text(encoding='utf-8')
        user={'closed_book_context':ctx,'locked_forecast':forecast,'evidence_catalog':pointers(ctx)}
    return [{'role':'system','content':system},{'role':'user','content':json.dumps(user,ensure_ascii=False,allow_nan=False)}]

def request(stage,msg):
    return {'model':'deepseek-v4-flash','messages':msg,'temperature':.1,'max_tokens':3500 if stage=='editorial' else 4800,
        'thinking':{'type':'disabled'},'stream':False,'response_format':{'type':'json_object'}}

def body():
    old=b.assert_contract()
    return {'system_revision':REV,'dataset_revision':old['dataset_revision'],'golden_set_revision':old['golden_set_revision'],
        'selection_hash':old['selection_hash'],'sample_ids':old['sample_ids'],'provider':'deepseek','model':'deepseek-v4-flash',
        'generation_parameters':{'temperature':.1,'forecast_max_tokens':4800,'editorial_max_tokens':3500,'thinking':{'type':'disabled'},'response_format':{'type':'json_object'}},
        'baseline_contract_hash':old['contract_hash'],'skill_hashes':old['skill_hashes'],'production_source_hashes':old['source_hashes'],
        'candidate_files':{p.name:sha(p) for p in [HERE/'contracts.py',Path(__file__),HERE/'trace_candidate.py',*[HERE/(x+'_protocol.txt') for x in ('forecast','repair','editorial')]]},
        'forecast_schema_hash':canonical_hash(c.schema()),'editorial_schema_hash':canonical_hash(ts.EditorialDraft.model_json_schema()),
        'prompt_hashes':{
             'forecast':canonical_hash(messages('forecast',b.context_for(b.selection()[0]))[0]),
             'repair':canonical_hash(messages('repair',b.context_for(b.selection()[0]),original={})[0]),
             'editorial':canonical_hash(messages('editorial',b.context_for(b.selection()[0]),{})[0])},
        'max_calls':60,'per_sample_max':{'forecast':1,'repair':1,'editorial':1},'retries':0,'tools':[],'retrieval':False,'history':False,
        'production_semantics_changed':False,'candidate_schema_semantics_changed':True,
        'normalization_policy':'Explicit metadata/field migration on copies; raw schema metric precedes normalization; no changed numbers/text.'}

def prepare():
    if (BASE/'contract.json').exists():return verify()
    obj=body();obj['contract_hash']=canonical_hash(obj);exclusive(BASE/'contract.json',obj);return obj

def verify():
    saved=read_json(BASE/'contract.json')
    if saved['contract_hash']!=canonical_hash({k:v for k,v in saved.items() if k!='contract_hash'}) or {k:v for k,v in saved.items() if k!='contract_hash'}!=body():raise ValueError('candidate pin drift')
    return saved

def https_once(req):
    if req['model']!='deepseek-v4-flash' or req['temperature']!=.1 or req['max_tokens'] not in (3500,4800) or req['response_format']!={'type':'json_object'}:raise ValueError('wire configuration drift')
    if set(req)!={'model','messages','temperature','max_tokens','thinking','stream','response_format'} or [m['role'] for m in req['messages']]!=['system','user']:raise ValueError('tools/history forbidden')
    key=os.environ.get('DEEPSEEK_API_KEY')
    if not key:raise ValueError('credential_missing')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*a,**kw):return None
    wire=urllib.request.Request(ENDPOINT,data=json.dumps(req,ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(wire,timeout=180) as r:return r.read().decode('utf-8')
    except urllib.error.HTTPError as e:raise ValueError('provider_http_'+str(e.code)) from None
    except (OSError,urllib.error.URLError):raise ValueError('provider_transport_failure') from None

def component(sample,stage,req,folder,transport):
    # Claim before dispatch. Even a crash cannot silently repeat this stage.
    existing=len(list(BASE.glob('samples/*/*/request.json')))
    if existing>=60:raise ValueError('call ceiling')
    exclusive(folder/'request.json',{'created_at':now(),'sample_id':sample['sample_id'],'stage':stage,'request':req,'request_hash':canonical_hash(req)})
    art={'sample_id':sample['sample_id'],'stage':stage,'created_at':now(),'request_hash':canonical_hash(req),
        'context_hash':sample['context_sha256'],'prompt_hash':canonical_hash(req['messages'][0]),'config':{k:v for k,v in req.items() if k!='messages'},
        'provider_status':'FAILED','parse_status':'NOT_RUN','protocol_status':'NOT_RUN','schema_status':'NOT_RUN','raw_response':None,'parsed_output':None,'error':None}
    started=time.monotonic()
    try:
        raw=transport(copy.deepcopy(req));art['raw_response']=raw;art['response_hash']=canonical_hash(raw)
        exclusive(folder/'raw-response.json',{'raw_response':raw,'response_hash':canonical_hash(raw)})
        envelope=json.loads(raw);art['provider_metadata']={k:envelope.get(k) for k in ('id','model','system_fingerprint','created','usage')}
        art['provider_status']='SUCCEEDED';choice=envelope['choices'][0];art['finish_reason']=choice.get('finish_reason')
        content=choice['message'].get('content');art['parse_status']='FAILED';art['parsed_output']=json.loads(content);art['parse_status']='PASSED'
        if len(envelope['choices'])!=1 or choice.get('finish_reason')!='stop' or choice['message'].get('tool_calls') or choice['message'].get('function_call'):raise ValueError('response_protocol_failure')
        art['protocol_status']='PASSED'
        try:
            (ts.EditorialDraft if stage=='editorial' else c.CandidateForecast).model_validate(art['parsed_output']);art['schema_status']='PASSED'
            if stage=='editorial' and 'player_analysis' not in art['parsed_output']:raise ValueError('required_player_analysis_missing')
        except Exception:art['schema_status']='FAILED';raise
    except Exception as exc:art['error']={'type':type(exc).__name__,'message':str(exc)[:1200]}
    art['latency_seconds']=round(time.monotonic()-started,3);art['artifact_hash']=canonical_hash(art)
    exclusive(folder/'artifact.json',art)
    # Optional local trace forwarding; a failure here never changes acceptance.
    try:
        import trace_candidate
        trace_candidate.emit(sample['sample_id'],stage,art)
    except Exception as exc:
        exclusive(folder/'trace-error.json',{'type':type(exc).__name__,'message':str(exc)[:150]})
    return art

def accept(obj,ctx):
    parsed=c.validate_historical(obj,ctx);fw.validate_independent_market_boundary(parsed);return parsed.model_dump()

def execute_sample(sample,transport):
    verify();ctx=b.context_for(sample);folder=BASE/'samples'/sample['sample_id']
    if folder.exists():raise ValueError('sample already attempted; never rerun')
    folder.mkdir(parents=True,exist_ok=False)
    exclusive(folder/'preflight.json',{'sample_id':sample['sample_id'],'context_hash':sample['context_sha256'],'dataset_revision':b.DATASET_ID,'golden_set_revision':b.GOLDEN_ID,'capabilities':{'tools':[],'search':False,'retrieval':False,'history':[]},'status':'PASS'})
    r={'sample_id':sample['sample_id'],'game_id':sample['target_game_id'],'game_date':sample['game_date'],'system_revision':REV,
        'context_hash':sample['context_sha256'],'dataset_revision':b.DATASET_ID,'golden_set_revision':b.GOLDEN_ID,
        'forecast_status':'FAILED','editorial_status':'NOT_RUN','chain_status':'FAILED','content_quality_status':'NA',
        'repair_triggered':False,'repair_status':'NOT_RUN','normalization_operations':[],'failure_stage':None,'failure_type':None,'attempt_counts':{'forecast':1,'repair':0,'editorial':0}}
    initial=component(sample,'forecast',request('forecast',messages('forecast',ctx)),folder/'forecast',transport)
    r.update(provider_status=initial['provider_status'],parse_status=initial['parse_status'],raw_schema_status=initial['schema_status'])
    accepted=None;error=initial['error']
    if initial['parse_status']=='PASSED' and initial['protocol_status']=='PASSED':
        try:
            normalized,ops=c.normalize_structure(initial['parsed_output'],ctx);accepted=accept(normalized,ctx);r['normalization_operations']=ops
        except Exception as exc:
            error={'type':type(exc).__name__,'message':str(exc)[:1200]}
            # Only structural/schema failures are eligible. Unsupported identities/evidence do not trigger a second forecast.
            if initial['schema_status']=='FAILED' and isinstance(initial['parsed_output'],dict) and 'prediction' in initial['parsed_output']:
                try:fw.ForecastNumbers.model_validate(initial['parsed_output']['prediction']);eligible=True
                except ValueError:eligible=False
                if eligible:
                    r['repair_triggered']=True;r['attempt_counts']['repair']=1
                    repair=component(sample,'repair',request('repair',messages('repair',ctx,original=initial['parsed_output'],error=error)),folder/'repair',transport)
                    r['repair_status']='FAILED';r['repair_schema_status']=repair['schema_status']
                    if repair['schema_status']=='PASSED':
                        try:
                            c.preserve_semantics(initial['parsed_output'],repair['parsed_output'],ctx)
                            accepted=accept(repair['parsed_output'],ctx);r['repair_status']='ACCEPTED'
                        except Exception as exc:error={'type':type(exc).__name__,'message':str(exc)[:1200]}
                    else:error=repair['error']
    if accepted is not None:
        r['forecast_status']='ACCEPTED';exclusive(folder/'accepted-forecast.json',{'sample_id':sample['sample_id'],'context_hash':sample['context_sha256'],'forecast':accepted,'normalization_operations':r['normalization_operations']})
        frozen=read_json(folder/'accepted-forecast.json');fhash=sha(folder/'accepted-forecast.json')
        r['attempt_counts']['editorial']=1
        editorial=component(sample,'editorial',request('editorial',messages('editorial',ctx,frozen['forecast'])),folder/'editorial',transport)
        r['editorial_status']='ACCEPTED' if editorial['schema_status']=='PASSED' else 'FAILED'
        if r['editorial_status']=='ACCEPTED':
            r['chain_status']='SUCCEEDED';r['content_quality_status']='PENDING_HUMAN';error=None
        else:error=editorial['error'];r['failure_stage']='EDITORIAL';r['failure_type']='EDITORIAL_SCHEMA_OR_PROVIDER'
        r['forecast_artifact_hash']=fhash
    else:
        r['failure_stage']='FORECAST';r['failure_type']='REPAIR_OR_SCHEMA' if initial['schema_status']=='FAILED' else 'ACCEPTANCE' if initial['parse_status']=='PASSED' else 'PARSE_OR_PROVIDER'
    r['error']=error;r['created_at']=now()
    r['artifact_refs']=[{'path':str(p.relative_to(ROOT)),'sha256':sha(p)} for p in sorted(folder.rglob('*.json'))]
    r['manifest_hash']=canonical_hash(r);exclusive(folder/'chain-manifest.json',r)
    print(json.dumps({'sample_id':sample['sample_id'],'chain':r['chain_status'],'calls':r['attempt_counts'],'error':error},ensure_ascii=True),flush=True)
    return r

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','verify','execute']);p.add_argument('--authorize-real-api',action='store_true');a=p.parse_args()
    if a.command=='prepare':print(json.dumps(prepare(),ensure_ascii=True))
    elif a.command=='verify':print(json.dumps(verify(),ensure_ascii=True))
    else:
        if not a.authorize_real_api:raise ValueError('real call authorization required')
        verify()
        for sample in b.selection():
            if (BASE/'samples'/sample['sample_id']/'chain-manifest.json').exists():continue
            execute_sample(sample,https_once)

if __name__=='__main__':main()
