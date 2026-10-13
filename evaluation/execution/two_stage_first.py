"""First Golden sample only: production component protocols, no orchestration.

No target truth files or production predictions are loaded. Extracting prompt
construction avoids production repair/market/research/publication side effects.
"""
import argparse,ast,copy,hashlib,json,os,sys,urllib.request,urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from control import Control,exclusive,now
from asset_scanner import EVAL,ROOT,read_json,sha,canonical_hash,connect
from evaluation_integrity import DATASET_ID,GOLDEN_ID,verify_golden,inside
from safe_model_input import serialize_context
from runner import ProviderFailure,ENDPOINT,digest_bytes
from v2.pregame_system import forecasting_workflow as fw
from v2.pregame_system.editorial_pipeline import EditorialDraft

HERE=Path(__file__).resolve().parent
SAMPLE='cb-10c3baf5823776e51d36'
REVISION='golden-pregame-production-two-stage-v1'
RUN=HERE/'outputs'/REVISION/SAMPLE
PROFILE=HERE/'profiles'/REVISION/'profile.json'
SOURCE_FILES={'forecasting':ROOT/'v2/pregame_system/forecasting_workflow.py',
              'editorial':ROOT/'v2/pregame_system/editorial_pipeline.py',
              'transport':ROOT/'v2/agent/generate_historical_pregame_analysis.py'}


def sample_and_context():
    verify_golden()
    manifest=read_json(EVAL/'golden_set/v1/selection_manifest.json')
    earliest=min(s['game_date'] for s in manifest['samples'])
    matches=[s for s in manifest['samples'] if s['sample_id']==SAMPLE]
    if len(matches)!=1:raise ValueError('specified first sample missing/duplicate')
    s=matches[0]
    if (s['game_date']!=earliest or s['game_date']!='2025-10-29' or s['home_team']!='Minnesota Timberwolves'
        or s['away_team']!='Los Angeles Lakers' or s['target_game_id']!='nba:0022500136'):
        raise ValueError('first sample identity/chronology mismatch')
    # Read only this Context. Parent manifest provides byte pins; never open truth.
    freeze_path=EVAL/'reports/closed-book-freeze-manifest.json'
    freeze=read_json(freeze_path)
    anchor=read_json(EVAL/'reports/golden-v1-protection-before.json')['closed_book_manifest_sha256']
    if sha(freeze_path)!=anchor or freeze['dataset_id']!=DATASET_ID or manifest['parent_dataset_id']!=DATASET_ID:
        raise ValueError('parent revision/manifest mismatch')
    p=inside(EVAL,s['context_path'])
    if sha(p)!=freeze['files'][s['context_path']]:raise ValueError('frozen Context byte hash mismatch')
    ctx=read_json(p);serialize_context(ctx,s['context_sha256'])
    return {k:s[k] for k in ('sample_id','target_game_id','game_date','home_team','away_team','context_sha256')},ctx


def basketball_adapter(ctx):
    """IDs refer only to JSON paths in the same frozen Context, not new facts."""
    m=ctx['matchup']
    groups=('matchup','team_a','team_b','head_to_head','schedule_context','injury','known_missing_information')
    return {'game':{'game_id':m['target_game_id'],'game_date':m['game_date'],'season':m['season'],
            'game_type':m['game_type'],'home':{'id':ctx['team_a']['team_id'],'name':m['home_team']},
            'away':{'id':ctx['team_b']['team_id'],'name':m['away_team']}},
            'closed_book_context':copy.deepcopy(ctx),
            'pure':{'status':'unavailable'},
            'evidence':[{'id':'context.'+key,'section':'frozen_context','kind':'FrozenContext',
                'context_pointer':'/'+key,'value':copy.deepcopy(ctx[key])} for key in groups]}


def embedded_skill(role):
    p=EVAL/'golden_set/v1/skill_snapshots'/f'{role}.txt'
    if sha(p)!=sha(ROOT/'agent/skills'/role/'SKILL.md'):
        raise ValueError('runtime Skill differs from frozen Golden Skill')
    text=p.read_text(encoding='utf-8').strip()
    return f'<{role}_skill>\n{text}\n</{role}_skill>'


def forecast_messages(ctx):
    """Extract only production call's pure construction, before any save/call."""
    tree=ast.parse(SOURCE_FILES['forecasting'].read_text(encoding='utf-8'))
    outer=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_run_two_stage_impl')
    inner=next(n for n in outer.body if isinstance(n,ast.FunctionDef) and n.name=='call')
    body=[]
    for stmt in inner.body:
        if isinstance(stmt,ast.Nonlocal):continue
        body.append(stmt)
        if isinstance(stmt,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='messages' for t in stmt.targets):break
    else:raise ValueError('production independent prompt boundary changed')
    mod=ast.Module(body=body,type_ignores=[])
    scope={**vars(fw),'stage':'independent','payload':basketball_adapter(ctx),
           'schema':fw.VersionedIndependentForecast,'require_analysis_core':True,
           'basketball':basketball_adapter(ctx),'skill':embedded_skill('forecasting'),
           'ats_tiebreak':False,'messages_override':None}
    exec(compile(ast.fix_missing_locations(mod),'<production independent prompt construction>','exec'),scope)
    return scope['messages']


def editorial_messages(ctx,forecast):
    tree=ast.parse(SOURCE_FILES['editorial'].read_text(encoding='utf-8'))
    outer=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_editorial')
    call=next(n.value for n in outer.body if isinstance(n,ast.Assign) and isinstance(n.value,ast.Call)
        and isinstance(n.value.func,ast.Name) and n.value.func.id=='call_recorded')
    # Same writer0 system expression, no source filesystem paths in model input.
    system_expr=call.args[2].elts[0].values[1]
    system=eval(compile(ast.Expression(system_expr),'<production writer0 system>','eval'),{'skill':embedded_skill('editorial')})
    schema=EditorialDraft.model_json_schema()
    schema['required']=list(dict.fromkeys(schema.get('required',[])+['player_analysis']))
    system+='\n只返回以下schema的JSON，不输出代码围栏：\n'+json.dumps(schema,ensure_ascii=False)
    return [{'role':'system','content':system},{'role':'user','content':json.dumps(
        {'closed_book_context':ctx,'locked_forecast':forecast},ensure_ascii=False,allow_nan=False)}]


def payload(stage,messages):
    if stage not in ('forecasting','editorial'):raise ValueError('component not allowed')
    if [m['role'] for m in messages]!=['system','user'] or any(set(m)!={'role','content'} for m in messages):
        raise ValueError('history/tool message forbidden')
    return {'model':'deepseek-v4-flash','messages':copy.deepcopy(messages),'temperature':.1,
            'max_tokens':4800 if stage=='forecasting' else 3500,'thinking':{'type':'disabled'},'stream':False}


def prepare():
    s,ctx=sample_and_context();dest=PROFILE.parent
    if PROFILE.exists():return load_profile()
    dest.mkdir(parents=True,exist_ok=False)
    forecast=payload('forecasting',forecast_messages(ctx))
    config={'protocol':'golden-pregame-production-two-stage-v1','generation_revision':REVISION,
        'sample_id':SAMPLE,'game_id':s['target_game_id'],'dataset_revision':DATASET_ID,'golden_set_revision':GOLDEN_ID,
        'context_hash':s['context_sha256'],'selection_hash':read_json(EVAL/'golden_set/v1/selection_manifest.json')['selection_manifest_hash'],
        'provider':'deepseek','model':'deepseek-v4-flash','model_revision':None,'endpoint':ENDPOINT,
        'components':{stage:{'temperature':.1,'max_tokens':tokens,'thinking':{'type':'disabled'},'stream':False,
                            'automatic_retries':0,'timeout_seconds':180} for stage,tokens in [('forecasting',4800),('editorial',3500)]},
        'source_hashes':{r:sha(p) for r,p in SOURCE_FILES.items()},
        'skill_hashes':{r:sha(EVAL/'golden_set/v1/skill_snapshots'/f'{r}.txt') for r in ('forecasting','editorial')},
        'forecast_prompt_hash':digest_bytes(forecast['messages'][0]['content'].encode()),
        'editorial_prompt_hash':digest_bytes(editorial_messages(ctx,{})[0]['content'].encode()),
        'forecast_request_hash':canonical_hash(forecast),'tools':[],'retrieval':False,'history':False,
        'input_adapter':'same Context + JSON-pointer evidence aliases; no production baseline/current-roster lookup',
        'writer_adapter':'same Context + this frozen Forecast; production writer0 system/schema; no publication/repair',
        'credential_env':'DEEPSEEK_API_KEY'}
    exclusive(dest/'forecast-request-lock.json',forecast)
    exclusive(PROFILE,config);exclusive(PROFILE.with_suffix('.pin.json'),{'profile_hash':canonical_hash(config),'file_hash':sha(PROFILE)})
    return config


def load_profile():
    config=read_json(PROFILE);pin=read_json(PROFILE.with_suffix('.pin.json'))
    if pin!={'profile_hash':canonical_hash(config),'file_hash':sha(PROFILE)}:raise ValueError('profile integrity')
    if config['sample_id']!=SAMPLE or config['generation_revision']!=REVISION or config['tools']!=[] or config['retrieval'] is not False or config['history'] is not False:
        raise ValueError('profile scope/capability mismatch')
    for role,path in SOURCE_FILES.items():
        if sha(path)!=config['source_hashes'][role]:raise ValueError('production protocol source changed')
    for role in ('forecasting','editorial'):
        if sha(EVAL/'golden_set/v1/skill_snapshots'/f'{role}.txt')!=config['skill_hashes'][role]:raise ValueError('Skill changed')
    return config


def preflight(control=None):
    control=control or Control();control.assert_phase(('PREPARED',))
    config=load_profile();s,ctx=sample_and_context()
    request=payload('forecasting',forecast_messages(ctx))
    if canonical_hash(request)!=config['forecast_request_hash']:raise ValueError('request pin mismatch')
    if json.loads(request['messages'][1]['content'])!=basketball_adapter(ctx):raise ValueError('context-only adapter mismatch')
    if RUN.exists() and any(RUN.iterdir()):raise ValueError('output destination not empty')
    with control.tx() as db:
        if db.execute('SELECT count(*) FROM attempts WHERE sample=?',(SAMPLE,)).fetchone()[0]:raise ValueError('sample already attempted')
    forbidden={'tools','functions','tool_choice','retrieval','search','conversation'}
    if set(request)&forbidden or 'Ground Truth' in request['messages'][1]['content']:raise ValueError('capability or truth leaked')
    if config['context_hash']!=s['context_sha256']:raise ValueError('context hash mismatch')
    return {'status':'PASS','sample_id':SAMPLE,'game_id':s['target_game_id'],'requests_authorized':2,
            'phase':control.state()['phase'],'profile_hash':canonical_hash(config),'target_system':config,
            'target_truth_files_opened':0,'other_contexts_opened':0,'model_calls':0}


def https_once(request):
    if set(request)!={'model','messages','temperature','max_tokens','thinking','stream'} or request['model']!='deepseek-v4-flash' or request['temperature']!=.1 or request['max_tokens'] not in (4800,3500):
        raise ProviderFailure('request_protocol')
    if request['thinking']!={'type':'disabled'} or request['stream'] is not False or [m['role'] for m in request['messages']]!=['system','user']:
        raise ProviderFailure('history_or_tool_protocol')
    key=os.environ.get('DEEPSEEK_API_KEY')
    if not key:raise ProviderFailure('credential_missing')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    req=urllib.request.Request(ENDPOINT,data=json.dumps(request,ensure_ascii=False).encode(),
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(req,timeout=180) as response:return response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:raise ProviderFailure('http_'+str(exc.code)) from None
    except (OSError,urllib.error.URLError):raise ProviderFailure('transport_or_timeout') from None


def parse_component(raw,stage):
    response=json.loads(raw);choices=response.get('choices',[])
    if len(choices)!=1 or choices[0].get('finish_reason')!='stop':raise ValueError('incomplete_response')
    message=choices[0]['message']
    if message.get('tool_calls') or message.get('function_call'):raise ValueError('tool_response')
    parsed=json.loads(message['content'])
    schema=fw.VersionedIndependentForecast if stage=='forecasting' else EditorialDraft
    schema.model_validate(parsed)
    if stage=='editorial' and 'player_analysis' not in parsed:raise ValueError('required player_analysis missing')
    return parsed,{'response_id':response.get('id'),'model':response.get('model'),
        'model_revision':response.get('system_fingerprint'),'usage':response.get('usage'),'finish_reason':choices[0]['finish_reason']}


def component(stage,request,config,control,transport,forecast_hash=None):
    attempt=control.claim(REVISION,stage,SAMPLE,canonical_hash(config),'human-authorized-first-sample')
    folder=RUN/stage/f'attempt-{attempt}'
    record={'sample_id':SAMPLE,'stage':stage,'exact_payload':request,'request_hash':canonical_hash(request),
            'model_visible_inputs':json.loads(request['messages'][1]['content'])}
    exclusive(folder/'request.json',record)
    raw=None;parsed=None;meta=None;error=None
    try:
        raw=transport(copy.deepcopy(request))
        exclusive(folder/'raw-response.json',{'raw_response':raw,'response_hash':digest_bytes(raw.encode())})
        parsed,meta=parse_component(raw,stage);status='ACCEPTED'
    except Exception as exc:
        status='FAILED';error={'code':exc.code if isinstance(exc,ProviderFailure) else 'parse_or_schema_failure','type':type(exc).__name__}
    artifact={'sample_id':SAMPLE,'game_id':config['game_id'],'component':stage,'status':status,'created_at':now(),
        'dataset_revision':DATASET_ID,'golden_set_revision':GOLDEN_ID,'generation_revision':REVISION,
        'provider':'deepseek','model':'deepseek-v4-flash','model_revision':meta.get('model_revision') if meta else None,
        'generation_parameters':{k:v for k,v in request.items() if k not in ('messages','model')},
        'context_hash':config['context_hash'],'config_hash':canonical_hash(config['components'][stage]),
        'prompt_hash':digest_bytes(request['messages'][0]['content'].encode()),'skill_hash':config['skill_hashes'][stage],
        'request_hash':record['request_hash'],'response_hash':digest_bytes(raw.encode()) if raw is not None else None,
        'exact_model_visible_request':request,'raw_response':raw,'parsed_output':parsed,'provider_metadata':meta,
        'accepted_attempt':attempt if status=='ACCEPTED' else None,'attempt':attempt,'error':error,
        'frozen_forecast_artifact_hash':forecast_hash,'automatic_retries':0}
    exclusive(folder/'artifact.json',artifact)
    control.finish(REVISION,stage,SAMPLE,attempt,status,(folder/'artifact.json').relative_to(HERE),sha(folder/'artifact.json'),'human-authorized-first-sample')
    if status!='ACCEPTED':raise ValueError(stage+'_FAILED; raw attempt preserved; no retry')
    return folder/'artifact.json',artifact


def verified_component(path,expected_hash=None):
    if expected_hash and sha(path)!=expected_hash:raise ValueError('component artifact hash mismatch')
    art=read_json(path)
    if art['sample_id']!=SAMPLE or art['generation_revision']!=REVISION or art['status']!='ACCEPTED':raise ValueError('component binding')
    if canonical_hash(art['exact_model_visible_request'])!=art['request_hash'] or digest_bytes(art['raw_response'].encode())!=art['response_hash']:
        raise ValueError('component request/response hash mismatch')
    parsed,_=parse_component(art['raw_response'],art['component'])
    if parsed!=art['parsed_output']:raise ValueError('parsed component differs from raw')
    return art


def execute(transport=None,control=None):
    control=control or Control();flight=preflight(control);config=load_profile();_,ctx=sample_and_context()
    exclusive(RUN/'preflight.json',flight)
    first=payload('forecasting',forecast_messages(ctx))
    fpath,forecast=component('forecasting',first,config,control,transport or https_once)
    fhash=sha(fpath)
    # Writer reads the persisted artifact, not an in-memory uncommitted answer.
    frozen=verified_component(fpath,fhash)
    second=payload('editorial',editorial_messages(ctx,frozen['parsed_output']))
    if digest_bytes(second['messages'][0]['content'].encode())!=config['editorial_prompt_hash']:raise ValueError('Writer prompt drift')
    epath,editorial=component('editorial',second,config,control,transport or https_once,fhash)
    manifest={'sample_id':SAMPLE,'game_id':config['game_id'],'dataset_revision':DATASET_ID,'golden_set_revision':GOLDEN_ID,
        'generation_revision':REVISION,'context_hash':config['context_hash'],'created_at':now(),'status':'ACCEPTED',
        'forecast_artifact':str(fpath.relative_to(HERE)),'forecast_artifact_hash':fhash,
        'editorial_artifact':str(epath.relative_to(HERE)),'editorial_artifact_hash':sha(epath),
        'components':{stage:{'provider':a['provider'],'model':a['model'],'model_revision':a['model_revision'],
            'config_hash':a['config_hash'],'prompt_hash':a['prompt_hash'],'skill_hash':a['skill_hash'],
            'request_hash':a['request_hash'],'response_hash':a['response_hash'],'accepted_attempt':a['accepted_attempt']}
            for stage,a in [('forecasting',forecast),('editorial',editorial)]},'model_calls':2,'other_samples_generated':0}
    manifest['manifest_hash']=canonical_hash(manifest)
    exclusive(RUN/'chain-manifest.json',manifest)
    control.audit('generation_completed','human-authorized-first-sample',{'sample_id':SAMPLE,'chain_manifest_hash':manifest['manifest_hash'],'components':2})
    return manifest


def load_chain():
    config=load_profile();_,ctx=sample_and_context();m=read_json(RUN/'chain-manifest.json')
    if m['manifest_hash']!=canonical_hash({k:v for k,v in m.items() if k!='manifest_hash'}) or m['sample_id']!=SAMPLE or m['context_hash']!=config['context_hash']:
        raise ValueError('chain manifest mismatch')
    f=verified_component(inside(HERE,m['forecast_artifact']),m['forecast_artifact_hash'])
    e=verified_component(inside(HERE,m['editorial_artifact']),m['editorial_artifact_hash'])
    expected={'closed_book_context':ctx,'locked_forecast':f['parsed_output']}
    if json.loads(e['exact_model_visible_request']['messages'][1]['content'])!=expected or e['frozen_forecast_artifact_hash']!=m['forecast_artifact_hash']:
        raise ValueError('Writer wrong Context/Forecast binding')
    if sha(inside(HERE,m['forecast_artifact']))!=m['forecast_artifact_hash']:raise ValueError('Forecast changed')
    return {'manifest':m,'context':ctx,'editorial':e['parsed_output']}


def protection():
    # Hash-only SQL record inspection; never select or parse Postgame truth.
    dbpath=ROOT/'v2/frontend/output/prediction_versions.sqlite'
    with connect(dbpath) as db:
        versions={t:[tuple(r) for r in db.execute(f'SELECT version_id,payload_sha256 FROM {t} ORDER BY version_id')]
                  for t in ('prediction_versions','postgame_versions')}
    files={str(p.relative_to(ROOT)):sha(p) for p in [*SOURCE_FILES.values(),
        ROOT/'agent/skills/forecasting/SKILL.md',ROOT/'agent/skills/editorial/SKILL.md',
        EVAL/'golden_set/v1/selection_manifest.json',EVAL/'golden_set/v1/rubric.json',
        EVAL/'datasets/closed_book_contexts'/f'{SAMPLE}.json']}
    return {'version_hash_records':versions,'files':files}


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','preflight','execute','verify'])
    p.add_argument('--authorize-real-api',action='store_true');a=p.parse_args()
    if a.command=='prepare':value=prepare()
    elif a.command=='preflight':value=preflight()
    elif a.command=='execute':
        if not a.authorize_real_api:raise ValueError('explicit API authorization required')
        value=execute()
    else:
        data=load_chain();value={'sample_id':SAMPLE,'chain_manifest_hash':data['manifest']['manifest_hash'],'verified':True}
    print(json.dumps(value,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
