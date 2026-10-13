"""Frozen Baseline V1 batch dispatcher; never repairs the system or opens UI.

Model-visible inputs are per-case frozen data only. Production schema repair
is the existing native continuation; no ordinary/quality retry is provided.
"""
import argparse,copy,json,sys,tempfile,urllib.request
from collections import Counter
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import two_stage_first as ts
import execute_first_schema_recovery as recovery
from control import Control,exclusive,now
from asset_scanner import EVAL,ROOT,read_json,sha,canonical_hash
from evaluation_integrity import DATASET_ID,GOLDEN_ID,verify_golden,inside
from safe_model_input import serialize_context
from runner import digest_bytes
from workspace import listening
from v2.pregame_system import forecasting_workflow as fw
from v2.agent import generate_historical_pregame_analysis as provider

REV='golden-pregame-system-baseline-v1'
BASE=ts.HERE/'baselines'/REV
FIRST_RECORD=recovery.REPAIR_ROOT/'baseline-final-record.json'
REPORT_JSON=EVAL/'reports/golden-pregame-baseline-v1.json'
REPORT_MD=EVAL/'reports/golden-pregame-baseline-v1.md'


def selection():
    verify_golden();m=read_json(EVAL/'golden_set/v1/selection_manifest.json')
    allowed=('sample_id','target_game_id','game_date','home_team','away_team','context_path','context_sha256')
    rows=[{k:s[k] for k in allowed} for s in m['samples']]
    return sorted(rows,key=lambda s:(s['game_date'],s['sample_id']))


def context_for(sample):
    manifest_path=EVAL/'reports/closed-book-freeze-manifest.json'
    manifest=read_json(manifest_path)
    anchor=read_json(EVAL/'reports/golden-v1-protection-before.json')['closed_book_manifest_sha256']
    if sha(manifest_path)!=anchor or manifest['dataset_id']!=DATASET_ID:raise ValueError('parent dataset revision/manifest drift')
    path=inside(EVAL,sample['context_path'])
    if not path.resolve().is_relative_to((EVAL/'datasets/closed_book_contexts').resolve()):raise ValueError('non-model context path')
    if sha(path)!=manifest['files'][sample['context_path']]:raise ValueError('frozen Context byte drift')
    ctx=read_json(path);serialize_context(ctx,sample['context_sha256'])
    if ctx['matchup']['target_game_id']!=sample['target_game_id']:raise ValueError('target identity drift')
    return ctx


def root_protection():
    m=read_json(EVAL/'reports/closed-book-freeze-manifest.json')
    refs=[EVAL/'reports/closed-book-freeze-manifest.json',EVAL/'golden_set/v1/selection_manifest.json',
          EVAL/'golden_set/v1/selection.csv',EVAL/'golden_set/v1/sampling_config.json',
          EVAL/'golden_set/v1/rubric.json',EVAL/'golden_set/v1/pregame_import.json',
          EVAL/'golden_set/v1/pregame_config.xml',EVAL/'golden_set/v1/postgame_config.xml',
          FIRST_RECORD,recovery.PARENT,recovery.REPAIR_ROOT/'artifact.json',ts.PROFILE,ts.PROFILE.with_suffix('.pin.json'),
          *[EVAL/'golden_set/v1/skill_snapshots'/f'{r}.txt' for r in ('forecasting','editorial','postgame')],
          *[inside(EVAL,s['context_path']) for s in selection()]]
    return {'production':ts.protection(),'frozen_references':{str(p.relative_to(ROOT)):sha(p) for p in refs}}


def contract_body():
    old=ts.load_profile();rows=selection()
    first=read_json(FIRST_RECORD)
    if first['record_hash']!=canonical_hash({k:v for k,v in first.items() if k!='record_hash'}):raise ValueError('first frozen final record corrupted')
    if first['sample_id']!=ts.SAMPLE or first['chain_status']!='FAILED' or first['attempt_counts']['total_provider_calls']!=2:
        raise ValueError('first frozen result mismatch')
    if sha(recovery.PARENT)!=first['initial_artifact_sha256'] or sha(recovery.REPAIR_ROOT/'artifact.json')!=first['repair_artifact_sha256']:
        raise ValueError('first immutable attempts changed')
    return {'system_revision':REV,'dataset_revision':DATASET_ID,'golden_set_revision':GOLDEN_ID,
        'selection_hash':old['selection_hash'],'sample_ids':[s['sample_id'] for s in rows],
        'model':'deepseek-v4-flash','provider':'deepseek','components':old['components'],
        'source_hashes':old['source_hashes'],'skill_hashes':old['skill_hashes'],
        'forecast_prompt_hash':old['forecast_prompt_hash'],'editorial_prompt_hash':old['editorial_prompt_hash'],
        'forecast_validation_schema_hash':canonical_hash(fw.VersionedIndependentForecast.model_json_schema()),
        'editorial_validation_schema_hash':canonical_hash(ts.EditorialDraft.model_json_schema()),
        'native_repair_prompt_hash':read_json(recovery.REPAIR_ROOT/'preflight-lock.json')['prompt_hash'],
        'locked_content_preservation_source_hash':old['source_hashes']['forecasting'],
        'adapter_hashes':{p.name:sha(p) for p in [Path(__file__),Path(ts.__file__),Path(recovery.__file__)]},
        'first_frozen_record_hash':sha(FIRST_RECORD),'first_record_path':str(FIRST_RECORD.relative_to(ROOT)),
        'tools':[],'retrieval':False,'history':False,'search':False,
        'max_initial_forecast_calls':1,'max_schema_repair_calls':1,'max_editorial_calls':1,
        'ordinary_provider_retries':0,'quality_retries':0,'protocol_revision_calls':0,
        'scope_note':'Closed-book component baseline; only native schema repair is authorized. No Market/Research/publication/Editorial repair or protocol revision calls.',
        'transport_timeout_seconds':{'initial_forecast':180,'schema_repair':40,'editorial':180}}


def assert_contract():
    path=BASE/'system-contract.json';saved=read_json(path)
    if saved['contract_hash']!=canonical_hash({k:v for k,v in saved.items() if k!='contract_hash'}):raise ValueError('contract integrity')
    current=contract_body()
    if {k:v for k,v in saved.items() if k!='contract_hash'}!=current:raise ValueError('frozen baseline system changed; do not continue')
    return saved


def component_record(sample,stage,request,transport,folder):
    request_hash=canonical_hash(request)
    exclusive(folder/'request.json',{'sample_id':sample['sample_id'],'system_revision':REV,'stage':stage,
        'exact_model_visible_request':request,'request_hash':request_hash})
    result={'sample_id':sample['sample_id'],'game_id':sample['target_game_id'],'system_revision':REV,
        'dataset_revision':DATASET_ID,'golden_set_revision':GOLDEN_ID,'component':stage,'attempt_number':1,
        'created_at':now(),'provider':'deepseek','model':'deepseek-v4-flash',
        'parameters':{k:v for k,v in request.items() if k not in ('messages','model')},
        'context_hash':sample['context_sha256'],'prompt_hash':digest_bytes(request['messages'][0]['content'].encode()),
        'skill_hash':ts.load_profile()['skill_hashes'][stage],'request_hash':request_hash,
        'provider_status':'NOT_RUN','parse_status':'NOT_RUN','schema_status':'NOT_RUN','status':'FAILED',
        'raw_response':None,'response_hash':None,'parsed_output':None,'provider_metadata':None,'error':None}
    schema_error=None
    try:
        raw=transport(copy.deepcopy(request));result['raw_response']=raw;result['response_hash']=digest_bytes(raw.encode())
        exclusive(folder/'response.json',{'raw_response':raw,'response_hash':result['response_hash']})
        envelope=json.loads(raw);choice=envelope['choices'][0];message=choice['message']
        result['provider_metadata']={k:envelope.get(k) for k in ('id','model','system_fingerprint','created','usage')}
        result['finish_reason']=choice.get('finish_reason')
        if not isinstance(message.get('content'),str) or not message['content'].strip():raise ValueError('empty_model_response')
        result['provider_status']='SUCCEEDED'
        try:parsed=json.loads(message['content']);result['parse_status']='PASSED';result['parsed_output']=parsed
        except ValueError:
            result['parse_status']='FAILED';raise
        # Preserve the original frozen component's finish/tool protocol.
        if choice.get('finish_reason')!='stop':raise ValueError('incomplete_response')
        if message.get('tool_calls') or message.get('function_call'):raise ValueError('tool_response')
        schema=fw.VersionedIndependentForecast if stage=='forecasting' else ts.EditorialDraft
        try:schema.model_validate(parsed);result['schema_status']='PASSED'
        except fw.ValidationError as exc:
            schema_error=exc;result['schema_status']='FAILED'
            result['validation_errors']=[{'loc':list(x['loc']),'type':x['type'],'msg':x['msg']}
                for x in exc.errors(include_url=False,include_input=False)]
            raise
        if stage=='editorial' and 'player_analysis' not in parsed:raise ValueError('required_player_analysis_missing')
        result['status']='SCHEMA_VALIDATED'
    except Exception as exc:
        if result['provider_status']=='NOT_RUN':result['provider_status']='FAILED'
        result['error']={'type':type(exc).__name__,'code':getattr(exc,'code',None) or str(exc).splitlines()[0][:300]}
    exclusive(folder/'artifact.json',result)
    return result,schema_error


def repair_once(sample,basketball,initial,schema_error,folder):
    folder.mkdir(parents=True,exist_ok=False)
    original_content=json.loads(initial['raw_response'])['choices'][0]['message']['content']
    fw.save(folder/'independent_response.json',{'raw':original_content,'replay_only':True,'provider_call':False,
        'source_sample_id':sample['sample_id'],'original_response_hash':initial['response_hash']})
    fw.save(folder/'independent_validation_error.json',{'error_type':type(schema_error).__name__,
        'errors':[{'loc':list(x['loc']),'type':x['type'],'msg':x['msg']} for x in schema_error.errors(include_url=False)],
        'message':str(schema_error).splitlines()[0][:400]})
    frame={'calls':0,'provider_status':'NOT_RUN','parse_status':'NOT_RUN','schema_status':'NOT_RUN',
           'content_preservation_status':'NOT_RUN','raw_response':None,'request':None,'usage':None}
    original_transport=provider.request_deepseek
    class ResponseRecorder:
        def __init__(self,response):self.response=response
        def __enter__(self):self.response.__enter__();return self
        def __exit__(self,*args):return self.response.__exit__(*args)
        def __getattr__(self,name):return getattr(self.response,name)
        def read(self,*args):
            raw=self.response.read(*args);frame['raw_response']=raw.decode('utf-8')
            exclusive(folder/'raw-provider-response.json',{'raw_response':frame['raw_response'],'response_hash':digest_bytes(raw)})
            return raw
    def recorded_request(messages,key,model,max_tokens,*,timeout_seconds,opener=None):
        if frame['calls']:raise ValueError('second_schema_repair_forbidden')
        frame['calls']+=1
        def wire(request,timeout):
            exact=json.loads(request.data.decode());frame['request']=exact
            if exact!=ts.payload('forecasting',messages):raise ValueError('repair_wire_protocol_mismatch')
            if digest_bytes(messages[0]['content'].encode())!=assert_contract()['native_repair_prompt_hash']:
                raise ValueError('repair_prompt_drift')
            exclusive(folder/'exact-wire-request.json',{'payload':exact,'request_hash':canonical_hash(exact),'timeout_seconds':timeout})
            return ResponseRecorder(urllib.request.urlopen(request,timeout=timeout))
        return original_transport(messages,key,model,max_tokens,timeout_seconds=timeout_seconds,opener=wire)
    def client(messages,schema):
        if schema is not fw.VersionedIndependentForecast:raise ValueError('repair_schema_drift')
        inputs=json.loads(messages[1]['content'])
        if inputs['basketball']!=basketball or inputs['original_response']!=initial['parsed_output']:
            raise ValueError('cross_sample_repair_input')
        with patch.object(provider,'request_deepseek',recorded_request):value=fw.default_client(messages,schema)
        frame['provider_status']='SUCCEEDED';frame['usage']=value[1];return value
    accepted=None;error=None
    try:accepted=recovery.continuation()(folder,basketball,schema_error,client,ts.embedded_skill('forecasting'))
    except Exception as exc:error={'type':type(exc).__name__,'code':str(exc)[:600]}
    provenance=read_json(folder/'schema_repair_provenance.json') if (folder/'schema_repair_provenance.json').exists() else None
    native_response=folder/'independent_schema_repair_response.json'
    if native_response.exists():
        try:
            value=read_json(native_response)['raw'];frame['parsed_output']=json.loads(value) if isinstance(value,str) else value
            frame['parse_status']='PASSED'
        except (ValueError,TypeError):frame['parse_status']='FAILED'
    if frame['parse_status']=='PASSED':frame['schema_status']='PASSED' if (folder/'independent_schema_repair_validation.json').exists() else 'FAILED'
    acceptance=read_json(folder/'independent_schema_repair_acceptance.json') if (folder/'independent_schema_repair_acceptance.json').exists() else None
    if frame['schema_status']=='PASSED':frame['content_preservation_status']='PASSED' if (acceptance or{}).get('content_lock_validated') else 'FAILED'
    if frame['calls'] and frame['provider_status']=='NOT_RUN':frame['provider_status']='FAILED'
    # Precondition rejection can occur before the repair provider call.
    result={'sample_id':sample['sample_id'],'game_id':sample['target_game_id'],'system_revision':REV,
        'dataset_revision':DATASET_ID,'golden_set_revision':GOLDEN_ID,'component':'production_schema_repair','attempt_number':2,
        'created_at':now(),'context_hash':sample['context_sha256'],'parent_response_hash':initial['response_hash'],
        'status':'ACCEPTED' if accepted else 'FAILED','accepted_forecast':accepted,'provider':'deepseek','model':'deepseek-v4-flash',
        'generation_parameters':{k:v for k,v in (frame['request'] or{}).items() if k not in ('messages','model')},
        'exact_model_visible_request':frame['request'],'request_hash':canonical_hash(frame['request']) if frame['request'] else None,
        'prompt_hash':digest_bytes(frame['request']['messages'][0]['content'].encode()) if frame['request'] else None,
        'skill_hash':canonical_hash({}),'skill_applied_to_this_request':False,
        'inherited_forecast_skill_hash':ts.load_profile()['skill_hashes']['forecasting'],
        'response_hash':digest_bytes(frame['raw_response'].encode()) if frame['raw_response'] else None,
        'raw_response':frame['raw_response'],'provider_metadata':frame['usage'],
        **{k:frame[k] for k in ('calls','provider_status','parse_status','schema_status','content_preservation_status')},
        'parsed_output':frame.get('parsed_output'),'finish_reason':(frame['usage'] or{}).get('finish_reason'),
        'production_acceptance':acceptance,'production_provenance':provenance,'error':error}
    exclusive(folder/'artifact.json',result)
    return result


def base_status(sample):
    return {'sample_id':sample['sample_id'],'game_id':sample['target_game_id'],'game_date':sample['game_date'],
        'system_revision':REV,'dataset_revision':DATASET_ID,'golden_set_revision':GOLDEN_ID,'created_at':now(),
        'provider_status':'NOT_RUN','parse_status':'NOT_RUN','raw_schema_status':'NOT_RUN','repair_triggered':False,
        'repair_provider_status':'NOT_RUN','repair_parse_status':'NOT_RUN','repair_schema_status':'NOT_RUN',
        'repair_content_preservation_status':'NOT_RUN','repair_status':'NOT_RUN','forecast_status':'FAILED',
        'editorial_provider_status':'NOT_RUN','editorial_parse_status':'NOT_RUN','editorial_status':'NOT_RUN',
        'chain_status':'FAILED','failure_stage':None,'failure_type':None,'content_quality_status':'NA',
        'context_hash':sample['context_sha256'],'attempt_counts':{'forecast':0,'schema_repair':0,'editorial':0},'artifact_refs':[]}


def ref(path):return {'path':str(Path(path).relative_to(ROOT)),'sha256':sha(path)}


def import_first(sample):
    frozen=read_json(FIRST_RECORD);r=base_status(sample)
    r.update(provider_status='SUCCEEDED',parse_status='PASSED',raw_schema_status='FAILED',repair_triggered=True,
        repair_provider_status=frozen['provider_status'],repair_parse_status=frozen['parse_status'],repair_schema_status=frozen['schema_status'],
        repair_content_preservation_status='FAILED',repair_status=frozen['repair_status'],forecast_status=frozen['forecast_status'],
        editorial_status=frozen['editorial_status'],chain_status=frozen['chain_status'],failure_stage=frozen['failure_stage'],
        failure_type=frozen['failure_type'],failure_code=frozen['production_repair_error'],
        attempt_counts={'forecast':1,'schema_repair':1,'editorial':0},imported_frozen_result=True,
        artifact_refs=[ref(FIRST_RECORD),ref(recovery.PARENT),ref(recovery.REPAIR_ROOT/'artifact.json')])
    r['record_hash']=canonical_hash(r);return r


def run_sample(sample):
    assert_contract();ctx=context_for(sample);folder=BASE/'samples'/sample['sample_id']
    if folder.exists():raise ValueError('sample destination exists; no rerun')
    folder.mkdir(parents=True,exist_ok=False);r=base_status(sample);basketball=ts.basketball_adapter(ctx)
    request=ts.payload('forecasting',ts.forecast_messages(ctx))
    if digest_bytes(request['messages'][0]['content'].encode())!=assert_contract()['forecast_prompt_hash']:raise ValueError('Forecast prompt drift')
    r['attempt_counts']['forecast']=1
    initial,schema_error=component_record(sample,'forecasting',request,ts.https_once,folder/'forecasting')
    r['artifact_refs'].append(ref(folder/'forecasting/artifact.json'))
    r.update(provider_status=initial['provider_status'],parse_status=initial['parse_status'],raw_schema_status=initial['schema_status'])
    accepted=None;failure_code=None
    if schema_error is not None:
        repaired=repair_once(sample,basketball,initial,schema_error,folder/'schema-repair')
        r['artifact_refs'].append(ref(folder/'schema-repair/artifact.json'))
        r.update(repair_triggered=bool(repaired['calls']),repair_provider_status=repaired['provider_status'],
            repair_parse_status=repaired['parse_status'],repair_schema_status=repaired['schema_status'],
            repair_content_preservation_status=repaired['content_preservation_status'],
            repair_status=repaired['status'] if repaired['calls'] else 'NOT_ATTEMPTED')
        r['attempt_counts']['schema_repair']=repaired['calls']
        if repaired['status']=='ACCEPTED':accepted=repaired['accepted_forecast']['independent']
        else:
            failure_code=(repaired.get('production_provenance') or{}).get('repair_error') or (repaired.get('error') or{}).get('code') or 'repair_precondition_failed'
            r['failure_stage']='FORECAST_SCHEMA_REPAIR'
            r['failure_type']=('SCHEMA_ERROR' if not repaired['calls'] else 'PROVIDER_ERROR' if repaired['provider_status']!='SUCCEEDED'
                else 'PARSE_ERROR' if repaired['parse_status']=='FAILED' else 'SCHEMA_ERROR' if repaired['schema_status']=='FAILED'
                else 'LOCKED_CONTENT_PRESERVATION_ERROR' if repaired['content_preservation_status']=='FAILED'
                else 'FORECAST_ACCEPTANCE_ERROR')
    elif initial['status']=='SCHEMA_VALIDATED':
        try:
            candidate=fw.VersionedIndependentForecast.model_validate(initial['parsed_output'])
            fw._validate_evidence(candidate,fw._ids(basketball))
            fw.validate_independent_market_boundary(candidate)
            fw.validate_pregame_plan(candidate.pregame_plan,basketball)
            accepted=candidate.model_dump()
        except Exception as exc:
            r.update(failure_stage='FORECAST_ACCEPTANCE',failure_type='FORECAST_ACCEPTANCE_ERROR');failure_code=str(exc)
    else:
        r['failure_stage']='FORECAST'
        r['failure_type']='PROVIDER_ERROR' if initial['provider_status']!='SUCCEEDED' else 'PARSE_ERROR' if initial['parse_status']=='FAILED' else 'OTHER'
        failure_code=(initial.get('error') or{}).get('code')
    if accepted is not None:
        f={'sample_id':sample['sample_id'],'system_revision':REV,'context_hash':sample['context_sha256'],
           'status':'ACCEPTED','forecast':accepted,'forecast_hash':canonical_hash(accepted),'source_attempt_refs':copy.deepcopy(r['artifact_refs'])}
        exclusive(folder/'accepted-forecast.json',f);r['artifact_refs'].append(ref(folder/'accepted-forecast.json'))
        frozen=read_json(folder/'accepted-forecast.json')
        if frozen['forecast_hash']!=canonical_hash(frozen['forecast']):raise ValueError('accepted Forecast hash drift')
        r['forecast_status']='ACCEPTED'
        second=ts.payload('editorial',ts.editorial_messages(ctx,frozen['forecast']))
        if digest_bytes(second['messages'][0]['content'].encode())!=assert_contract()['editorial_prompt_hash']:raise ValueError('Editorial prompt drift')
        if json.loads(second['messages'][1]['content'])!={'closed_book_context':ctx,'locked_forecast':frozen['forecast']}:
            raise ValueError('Writer same-sample binding error')
        r['attempt_counts']['editorial']=1
        editorial,_=component_record(sample,'editorial',second,ts.https_once,folder/'editorial')
        r['artifact_refs'].append(ref(folder/'editorial/artifact.json'))
        r.update(editorial_provider_status=editorial['provider_status'],editorial_parse_status=editorial['parse_status'])
        if editorial['status']=='SCHEMA_VALIDATED':
            e={'sample_id':sample['sample_id'],'system_revision':REV,'status':'ACCEPTED','context_hash':sample['context_sha256'],
               'forecast_hash':frozen['forecast_hash'],'editorial_output':editorial['parsed_output'],
               'editorial_response_hash':editorial['response_hash'],'source_artifact_ref':ref(folder/'editorial/artifact.json')}
            exclusive(folder/'accepted-editorial.json',e);r['artifact_refs'].append(ref(folder/'accepted-editorial.json'))
            r.update(editorial_status='ACCEPTED',chain_status='ACCEPTED',content_quality_status='NOT_EVALUATED')
            exclusive(folder/'chain-manifest.json',{'sample_id':sample['sample_id'],'system_revision':REV,
                'context_hash':sample['context_sha256'],'forecast_hash':frozen['forecast_hash'],
                'artifacts':copy.deepcopy(r['artifact_refs'])})
            r['artifact_refs'].append(ref(folder/'chain-manifest.json'))
        else:
            r.update(editorial_status='FAILED',failure_stage='EDITORIAL',failure_type='EDITORIAL_ERROR')
            failure_code=(editorial.get('error') or{}).get('code')
    if r['chain_status']!='ACCEPTED':
        r['failure_code']=failure_code
        exclusive(folder/'failure-evidence.json',{'failure_stage':r['failure_stage'],'failure_type':r['failure_type'],'failure_code':failure_code})
        r['artifact_refs'].append(ref(folder/'failure-evidence.json'))
    r['record_hash']=canonical_hash(r);exclusive(folder/'final-record.json',r)
    return r


def initialize():
    if BASE.exists():raise ValueError('baseline destination exists; no overwrite')
    body=contract_body();BASE.mkdir(parents=True,exist_ok=False)
    exclusive(BASE/'system-contract.json',{**body,'contract_hash':canonical_hash(body)})
    exclusive(BASE/'protection-before.json',root_protection())
    records=[]
    for s in selection():
        ctx=context_for(s)
        if digest_bytes(ts.forecast_messages(ctx)[0]['content'].encode())!=body['forecast_prompt_hash']:
            raise ValueError('sample Prompt differs from first sample')
        records.append({'sample_id':s['sample_id'],'context_hash':s['context_sha256'],'request_hash':canonical_hash(ts.payload('forecasting',ts.forecast_messages(ctx)))})
    exclusive(BASE/'preflight.json',{'status':'PASS','total_samples':20,'remaining_samples':19,'contract_hash':canonical_hash(body),
        'model_tools':False,'retrieval':False,'history':False,'truth_files_opened':0,'records':records})
    first=next(s for s in selection() if s['sample_id']==ts.SAMPLE)
    folder=BASE/'samples'/ts.SAMPLE;folder.mkdir(parents=True,exist_ok=False)
    exclusive(folder/'final-record.json',import_first(first))
    return {'system_revision':REV,'preflight':'PASS','first_imported':True,'real_calls':0,'remaining':19}


def summarize():
    rows=selection();records=[read_json(BASE/'samples'/s['sample_id']/'final-record.json') for s in rows]
    assert len(records)==20 and {r['sample_id'] for r in records}=={s['sample_id'] for s in rows}
    for r in records:
        if r['record_hash']!=canonical_hash({k:v for k,v in r.items() if k!='record_hash'}):raise ValueError('final record hash mismatch')
        for a in r['artifact_refs']:
            if sha(inside(ROOT,a['path']))!=a['sha256']:raise ValueError('artifact hash drift')
    before=read_json(BASE/'protection-before.json');after=root_protection()
    protection={k:canonical_hash(v)==canonical_hash(after[k]) for k,v in before.items()}
    assert all(protection.values())
    def rate(n,d=20):return {'numerator':n,'denominator':d,'rate':n/d if d else None}
    provider_success=sum(r['provider_status']=='SUCCEEDED' for r in records)
    parsed=sum(r['parse_status']=='PASSED' for r in records)
    raw_pass=sum(r['raw_schema_status']=='PASSED' for r in records)
    triggered=sum(r['repair_triggered'] for r in records);repaired=sum(r['repair_status']=='ACCEPTED' for r in records)
    forecast=sum(r['forecast_status']=='ACCEPTED' for r in records);editorial=sum(r['editorial_status']=='ACCEPTED' for r in records)
    e2e=sum(r['chain_status']=='ACCEPTED' for r in records)
    attempts={k:sum(r['attempt_counts'][k] for r in records) for k in ('forecast','schema_repair','editorial')}
    failed=[r for r in records if r['chain_status']=='FAILED']
    calls=attempts['forecast']+attempts['schema_repair']+attempts['editorial']
    errors=Counter(r['failure_type'] for r in failed);stages=Counter(r['failure_stage'] for r in failed)
    code_counts=Counter((r.get('failure_code') or'UNKNOWN').split(':')[0].splitlines()[0] for r in failed)
    notes=[]
    if 'LOCKED_CONTENT_PRESERVATION_ERROR' in errors:
        notes.append('Observed native preservation failures after schema repair: consider a future general recovery policy for misplaced fields; do not change Baseline V1.')
    if 'FORECAST_ACCEPTANCE_ERROR' in errors:
        notes.append('Observed acceptance failures: inspect actual identity/evidence/market-boundary codes; closed-book history cannot establish target current roster. No new data or case-specific workaround in this baseline.')
    if 'SCHEMA_ERROR' in errors:
        notes.append('Observed schema failures: review exact error paths in sealed artifacts before proposing a future protocol change; no attribution to basketball reasoning quality.')
    if 'EDITORIAL_ERROR' in errors:
        notes.append('Observed Editorial execution failures: separate provider/parse/schema causes using sealed artifacts, not article-quality scores.')
    if 'PROVIDER_ERROR' in errors:
        notes.append('Observed provider failures remain in denominator; no native automatic transport retry was added.')
    inventory=[]
    for p in BASE.rglob('*'):
        if p.is_file():inventory.append(ref(p))
    report={'system_revision':REV,'created_at':now(),'total_samples':20,'newly_executed_samples':19,
        'actual_provider_calls':calls,'this_round_provider_calls':calls-2,'attempt_counts':attempts,
        'metrics':{'provider_success_rate_initial_forecast':rate(provider_success),'json_parse_rate_initial_forecast':rate(parsed),
            'raw_schema_compliance_rate':rate(raw_pass),'schema_repair_trigger_rate':rate(triggered),
            'schema_repair_success_rate':rate(repaired,triggered),'forecast_acceptance_rate':rate(forecast),
            'editorial_generation_rate':rate(editorial),'editorial_success_conditional_on_forecast':rate(editorial,forecast),
            'e2e_generation_success_rate':rate(e2e)},
        'failure_stage_distribution':dict(stages),'failure_type_distribution':dict(errors),'observed_failure_code_distribution':dict(code_counts),
        'content_quality':'NOT_COMPUTED','human_annotations':0,'judge_calls':0,'articles_exposed':0,
        'first_failure_preserved':True,'first_source_record_hash':sha(FIRST_RECORD),'rows':records,
        'candidate_fix_notes':notes,'artifact_inventory':inventory,'protection':protection,
        'production_http_status':urllib.request.urlopen('http://127.0.0.1:8625/',timeout=15).status,
        'label_studio_ports_closed':all(not listening(p) for p in (8681,8682,8683,8684)),
        'scope_limitations':['Frozen closed-book component adapter, not full live Research/Market/publication flow.',
            'Exactly the authorized single schema-repair branch is used; reference/basis protocol revisions and Editorial repair are excluded by this execution contract.',
            'Model alias pinned; any provider-reported revision/fingerprint is saved in individual response artifacts. No provider immutable-version guarantee.',
            'No outcomes/human/Judge scores; execution success is not basketball accuracy or content-quality evidence.']}
    exclusive(BASE/'protection-after.json',after);exclusive(REPORT_JSON,report)
    lines=['# Golden Pregame System Baseline V1','',f'System revision: `{REV}`。20场完成；第一场原结果保留，新增执行19场。',
        '',f'累计真实API调用 **{calls}**（本轮新增{calls-2}）：Forecast {attempts["forecast"]}、schema repair {attempts["schema_repair"]}、Editorial {attempts["editorial"]}。',
        '无普通retry、无第二次repair、无best-of-N、无系统修正。成功文章仅封存，不对人工开放。','',
        '## Execution metrics','', '| 指标 | 分子 / 分母 | 比例 |','|---|---:|---:|']
    for name,value in report['metrics'].items():
        lines.append(f'| {name} | {value["numerator"]} / {value["denominator"]} | '+(f'{value["rate"]:.1%}' if value['rate'] is not None else 'NA')+' |')
    lines+=['','Provider/parse指标分母均为20场首次Forecast；raw schema严格按20场计，不剔除失败。Repair成功指生产正式accepted，不只是schema通过。',
        'Editorial生成率表示accepted文章数/20；另列Forecast成功条件下的Writer成功率。Content Quality未计算，没有人工/ Judge评分。','',
        '## 每场状态（不显示文章/GT）','', '| sample_id | 首次provider | parse | raw schema | repair | Forecast | Editorial | chain | failure type |',
        '|---|---|---|---|---|---|---|---|---|']
    for r in records:lines.append('| '+' | '.join(str(r[k]) for k in ('sample_id','provider_status','parse_status','raw_schema_status','repair_status','forecast_status','editorial_status','chain_status','failure_type'))+' |')
    lines+=['','## Failure分布','', 'Stage: `'+json.dumps(dict(stages),ensure_ascii=False)+'`',
        '','Type: `'+json.dumps(dict(errors),ensure_ascii=False)+'`','', 'Observed code: `'+json.dumps(dict(code_counts),ensure_ascii=False)+'`','',
        '## 第一场及hash保护','',f'第一场 `{ts.SAMPLE}` 仍为FAILED，content quality NA；原provider+schema-repair共2次调用计入总数和20场分母。原记录没有回写。',
        '', '保护比较：`'+json.dumps(protection)+'`。Production 8625 HTTP '+str(report['production_http_status'])+'。Label Studio端口均关闭。',
        '', '所有request/response/metadata/component/accepted/chain/final-record的路径与hash见同名JSON中的artifact_inventory与每场artifact_refs。没有GT文件被读取或加入请求。','',
        '## 系统性观察 / candidate fixes（未执行）','']
    lines+=['- '+n for n in notes] or ['没有可从执行记录确认的新系统性问题；内容质量仍未评估。']
    lines+=['','## 范围与下一步','']+['- '+x for x in report['scope_limitations']]
    lines+=['','下一步：先人工分析执行失败与成功覆盖，再按chronological protocol逐场开放成功文章进行盲标。此轮不开放工作台，不启动V2修复。',
        '', '未修改生产代码、Prompt、Skill、Schema、repair、selection或冻结Context；未运行Judge/DeepEval评分/Postgame；未commit/push。完成后停止。']
    REPORT_MD.parent.mkdir(parents=True,exist_ok=True)
    with REPORT_MD.open('x',encoding='utf-8') as f:f.write('\n'.join(lines)+'\n')
    return {'total_samples':20,'actual_provider_calls':calls,'new_calls':calls-2,'raw_schema_pass':raw_pass,
        'repair_triggered':triggered,'repair_accepted':repaired,'forecast_accepted':forecast,'editorial_accepted':editorial,
        'e2e_accepted':e2e,'failure_types':dict(errors),'protection':protection}


def execute():
    control=Control();control.assert_phase(('PREPARED',));assert_contract()
    remaining=[s for s in selection() if s['sample_id']!=ts.SAMPLE]
    for i,s in enumerate(remaining,1):
        final=BASE/'samples'/s['sample_id']/'final-record.json'
        if final.exists():
            # Resume only already-frozen completed samples, never rerun a call.
            print(json.dumps({'progress':f'{i}/19','sample_id':s['sample_id'],'status':'ALREADY_FROZEN'}),flush=True);continue
        if final.parent.exists():raise ValueError('incomplete sample attempt exists: do not retry silently')
        print(json.dumps({'progress':f'{i}/19','sample_id':s['sample_id'],'status':'STARTED'}),flush=True)
        r=run_sample(s)
        control.audit('baseline_sample_frozen','authorized-baseline-v1',{'sample_id':s['sample_id'],'chain_status':r['chain_status'],'record_hash':r['record_hash']})
        print(json.dumps({'progress':f'{i}/19','sample_id':s['sample_id'],'forecast':r['forecast_status'],
            'repair':r['repair_status'],'editorial':r['editorial_status'],'chain':r['chain_status'],'failure_type':r['failure_type'],
            'calls':sum(r['attempt_counts'].values())}),flush=True)
    return summarize()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['initialize','execute','summarize']);p.add_argument('--authorize-baseline-v1-remaining-19',action='store_true');args=p.parse_args()
    if args.command=='initialize':value=initialize()
    elif args.command=='execute':
        if not args.authorize_baseline_v1_remaining_19:raise ValueError('explicit batch authorization required')
        value=execute()
    else:value=summarize()
    print(json.dumps(value,ensure_ascii=True),flush=True)
