"""No production imports, tools, model memory, retrieval or hidden lookups.

Default CLI is preflight only. HTTPS happens exclusively after --execute and
--authorize-real-api. One request per case, no automatic retry or repair.
"""
import argparse, copy, hashlib, json, math, os, re, sys
import urllib.error, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from control import Control, exclusive, now
from asset_scanner import EVAL, read_json, sha, canonical_hash
from evaluation_integrity import GOLDEN_ID, DATASET_ID, verify_golden, verify_closedbook, inside
from safe_model_input import serialize_context

HERE = Path(__file__).resolve().parent
GOLD = EVAL/'golden_set/v1'
CONFIG_KEYS = {'generation_revision','golden_set_revision','parent_dataset_revision','selection_hash',
               'provider','model','model_revision','endpoint','credential_env','parameters','timeout_seconds',
               'max_attempts','tools','retrieval','shared_history','snapshots','postgame_pregame_revision'}
PARAM_KEYS = {'temperature','top_p','max_tokens','stream','thinking'}
ENDPOINT = 'https://api.deepseek.com/chat/completions'
PROTOCOL = '''你正在执行固定数据的 Closed-book Evaluation，不是生产联网链路。
外部知识只可用于一般篮球机制推理；具体比赛事实只能来自本次 user JSON，未知保持未知。
禁止联网研究、工具、检索、数据库查询、使用记忆中的本场结果或其他样本信息。
本任务每场只有一次独立请求，不执行生产多阶段工具流程。
赛前：先形成一次 final_prediction，再据此写文章，不在正文另改数字。
只输出 JSON：title(str), short_review(str), article(str), important_assumptions(list[str])，
赛前另需 final_prediction: winner(当前主队或客队全名), win_probability(0.5～1的小数),
expected_margin(胜方正分差，大于0), expected_total(大于0)。赛后不重新给赛前预测。
'''


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def revision_name(value):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', value):
        raise ValueError('invalid generation revision')
    return value


def validate_config(config):
    if set(config) != CONFIG_KEYS:
        raise ValueError('unknown/missing config field or capability')
    if config['golden_set_revision'] != GOLDEN_ID or config['parent_dataset_revision'] != DATASET_ID:
        raise ValueError('wrong Golden or parent revision')
    revision_name(config['generation_revision'])
    if config['tools'] != [] or config['retrieval'] is not False or config['shared_history'] is not False:
        raise ValueError('tools/retrieval/history forbidden')
    if config['provider'] != 'deepseek' or config['endpoint'] != ENDPOINT or not config['model']:
        raise ValueError('provider endpoint/model not pinned')
    if config['credential_env'] != 'DEEPSEEK_API_KEY':
        raise ValueError('credential must be an environment reference only')
    params = config['parameters']
    if set(params) != PARAM_KEYS or params['stream'] is not False or params['thinking'] != {'type':'disabled'}:
        raise ValueError('unknown/unsupported provider parameters')
    if not (0 <= params['temperature'] <= 2 and 0 < params['top_p'] <= 1 and isinstance(params['max_tokens'],int) and params['max_tokens'] > 0):
        raise ValueError('invalid generation parameters')
    if config['max_attempts'] != 1 or not 1 <= config['timeout_seconds'] <= 600:
        raise ValueError('automatic retry forbidden or invalid timeout')
    if set(config['snapshots']) != {'protocol','pregame_prompt','postgame_prompt','forecasting','editorial','postgame'}:
        raise ValueError('snapshot roles mismatch')
    for role, pin in config['snapshots'].items():
        if set(pin) != {'path','sha256'}:
            raise ValueError('snapshot pin shape')
        path = inside(EVAL, pin['path'])
        if sha(path) != pin['sha256']:
            raise ValueError('prompt/skill hash mismatch: '+role)
    return config


def load_profile(path):
    path = Path(path)
    config = read_json(path)
    receipt = read_json(path.with_suffix('.pin.json'))
    if receipt != {'profile_hash':canonical_hash(config),'profile_file_hash':sha(path)}:
        raise ValueError('execution profile pin mismatch')
    return validate_config(config)


def prepare_profile(revision, model='deepseek-v4-flash', parent_pre=None):
    verify_golden(); verify_closedbook()
    revision_name(revision)
    dest = HERE/'profiles'/revision
    dest.mkdir(parents=True,exist_ok=False)
    protocol = dest/'protocol.txt'
    with protocol.open('x',encoding='utf-8') as f:
        f.write(PROTOCOL)
    selection = read_json(GOLD/'selection_manifest.json')
    refs = {'protocol':protocol, 'pregame_prompt':GOLD/'prompt_snapshots/pregame.txt',
            'postgame_prompt':GOLD/'prompt_snapshots/postgame.txt',
            **{r:GOLD/'skill_snapshots'/f'{r}.txt' for r in ('forecasting','editorial','postgame')}}
    config = {'generation_revision':revision,'golden_set_revision':GOLDEN_ID,'parent_dataset_revision':DATASET_ID,
              'selection_hash':selection['selection_manifest_hash'],'provider':'deepseek','model':model,
              'model_revision':None,'endpoint':ENDPOINT,'credential_env':'DEEPSEEK_API_KEY',
              'parameters':{'temperature':0.2,'top_p':1.0,'max_tokens':4096,'stream':False,'thinking':{'type':'disabled'}},
              'timeout_seconds':180,'max_attempts':1,'tools':[],'retrieval':False,'shared_history':False,
              'snapshots':{r:{'path':str(p.relative_to(EVAL)),'sha256':sha(p)} for r,p in refs.items()},
              'postgame_pregame_revision':parent_pre}
    validate_config(config)
    profile = dest/'profile.json'
    exclusive(profile, config)
    exclusive(profile.with_suffix('.pin.json'), {'profile_hash':canonical_hash(config),'profile_file_hash':sha(profile)})
    return profile


def samples():
    return read_json(GOLD/'selection_manifest.json')['samples']


def context_for(sample):
    ctx = read_json(inside(EVAL, sample['context_path']))
    # Context hash is canonical in the selection, byte-authenticated in parent.
    serialize_context(ctx, sample['context_sha256'])
    return ctx


def accepted_artifact(control, revision, sample):
    record = control.accepted(revision,'pregame',sample['sample_id'])
    path = inside(HERE, record['artifact'])
    if sha(path) != record['artifact_hash']:
        raise ValueError('accepted Pregame artifact hash mismatch')
    artifact = read_json(path)
    if (artifact['sample_id'] != sample['sample_id'] or artifact['generation_revision'] != revision or
        artifact['golden_set_revision'] != GOLDEN_ID or artifact['context_hash'] != sample['context_sha256'] or artifact['status'] != 'ACCEPTED'):
        raise ValueError('wrong Pregame revision/sample/context')
    request=read_json(path.parent/'request.json')
    if canonical_hash(request['exact_payload'])!=artifact['request_hash'] or request['request_hash']!=artifact['request_hash']:
        raise ValueError('Pregame exact request hash mismatch')
    if request['model_visible_inputs']!=json.loads(request['exact_payload']['messages'][1]['content']):
        raise ValueError('Pregame visible input/request mismatch')
    if digest_bytes(artifact['raw_response'].encode())!=artifact['response_hash']:
        raise ValueError('Pregame raw response hash mismatch')
    return artifact


def bound_postgame_tasks():
    """Original import is not trusted as a substitute for frozen truth bytes."""
    selected={s['sample_id']:s for s in samples()}
    rows=read_json(GOLD/'postgame_import.json')
    if len(rows)!=20 or {r['data']['sample_id'] for r in rows}!=set(selected):
        raise ValueError('Postgame selected task set mismatch')
    for row in rows:
        task=row['data'];sample=selected[task['sample_id']]
        truth=read_json(inside(EVAL,sample['ground_truth_path']))
        if task['ground_truth']!=truth or truth['sample_id']!=sample['sample_id'] or truth['target_game_id']!=sample['target_game_id']:
            raise ValueError('Postgame frozen Ground Truth binding mismatch')
    return {r['data']['sample_id']:r['data'] for r in rows}


def build_request(config, stage, context, expected_context_hash, post_inputs=None):
    """Pure function: the factory has no access to other samples or outputs."""
    validate_config(config)
    serialize_context(context, expected_context_hash)
    inputs = {'context':copy.deepcopy(context)}
    roles = ('forecasting','editorial') if stage == 'pregame' else ('postgame',)
    if stage not in ('pregame','postgame'):
        raise ValueError('unknown stage')
    if stage == 'pregame' and post_inputs is not None:
        raise ValueError('Ground Truth or hidden metadata forbidden in Pregame')
    if stage == 'postgame':
        if not post_inputs or set(post_inputs) != {'frozen_pregame_output','ground_truth','postgame_evidence'}:
            raise ValueError('Postgame requires frozen Pregame and bound Ground Truth')
        if post_inputs['ground_truth']['target_game_id'] != context['matchup']['target_game_id']:
            raise ValueError('Postgame GT target mismatch')
        inputs.update(copy.deepcopy(post_inputs))
    def snapshot(role):
        return inside(EVAL,config['snapshots'][role]['path']).read_text(encoding='utf-8')
    system = snapshot('protocol')+'\n'+snapshot(stage+'_prompt')
    for role in roles:
        system += '\n\n# '+role+' frozen skill\n'+snapshot(role)
    payload = {'model':config['model'],'messages':[{'role':'system','content':system},
               {'role':'user','content':json.dumps(inputs,ensure_ascii=False,allow_nan=False)}], **copy.deepcopy(config['parameters'])}
    assert set(payload) == {'model','messages',*PARAM_KEYS}
    assert [m['role'] for m in payload['messages']] == ['system','user']
    return payload


def preflight(config, stage, control, require_empty=True):
    validate_config(config); verify_golden(); verify_closedbook()
    manifest = read_json(GOLD/'selection_manifest.json')
    if config['selection_hash'] != manifest['selection_manifest_hash']:
        raise ValueError('20-case selection hash mismatch')
    control.assert_phase(('PREPARED',) if stage == 'pregame' else ('PREGAME_FROZEN',))
    dest = HERE/'outputs'/config['generation_revision']/stage
    if require_empty and dest.exists() and any(dest.iterdir()):
        raise ValueError('destination not empty; explicit retry/new revision required')
    requests = []
    if stage == 'postgame' and config['postgame_pregame_revision'] != control.state()['pre_revision']:
        raise ValueError('wrong Pregame revision for Postgame')
    post_tasks = bound_postgame_tasks() if stage=='postgame' else {}
    for sample in manifest['samples']:
        ctx = context_for(sample)
        post = None
        if stage == 'postgame':
            pre = accepted_artifact(control, config['postgame_pregame_revision'], sample)
            task = post_tasks[sample['sample_id']]
            post = {'frozen_pregame_output':pre['parsed_output'],'ground_truth':task['ground_truth'],'postgame_evidence':task['postgame_evidence']}
        request = build_request(config,stage,ctx,sample['context_sha256'],post)
        requests.append({'sample_id':sample['sample_id'],'request_hash':canonical_hash(request),
                         'context_hash':sample['context_sha256']})
    return {'status':'READY','stage':stage,'count':len(requests),'requests':requests,'real_calls':0,
            'profile_hash':canonical_hash(config),'phase':control.state()['phase']}


def parse_response(raw, stage, context):
    response = json.loads(raw)
    choices = response.get('choices',[])
    if len(choices) != 1 or choices[0].get('finish_reason') != 'stop':
        raise ValueError('incomplete/refused response')
    message = choices[0]['message']
    if message.get('tool_calls') or message.get('function_call'):
        raise ValueError('provider returned forbidden tools')
    output = json.loads(message['content'])
    if not isinstance(output,dict) or any(not isinstance(output.get(k),str) or not output[k].strip() for k in ('title','short_review','article')):
        raise ValueError('invalid article JSON')
    if not isinstance(output.get('important_assumptions'),list) or any(not isinstance(x,str) for x in output['important_assumptions']):
        raise ValueError('invalid assumptions')
    if stage == 'pregame':
        pred = output.get('final_prediction',{})
        if set(pred) != {'winner','win_probability','expected_margin','expected_total'}:
            raise ValueError('prediction fields missing')
        if pred['winner'] not in (context['matchup']['home_team'],context['matchup']['away_team']):
            raise ValueError('prediction team identity mismatch')
        nums = [pred[k] for k in ('win_probability','expected_margin','expected_total')]
        if any(isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) for n in nums):
            raise ValueError('invalid prediction number')
        if not .5 <= nums[0] <= 1 or nums[1] <= 0 or nums[2] <= 0:
            raise ValueError('prediction range')
    return output, {'response_id':response.get('id'),'model':response.get('model'),
                    'model_revision_if_available':response.get('system_fingerprint'),
                    'usage':response.get('usage'),'finish_reason':choices[0]['finish_reason']}


class ProviderFailure(Exception):
    def __init__(self, code, retry_after=None):
        self.code, self.retry_after = code, retry_after
        super().__init__(code)


class HTTPSProvider:
    """Exactly one allowlisted HTTPS request. No redirects, automatic retries or tools."""
    def __init__(self, config):
        self.config = copy.deepcopy(validate_config(config))

    def __call__(self, payload):
        if set(payload) != {'model','messages',*PARAM_KEYS}:
            raise ProviderFailure('request_protocol')
        messages=payload['messages']
        if (not isinstance(messages,list) or len(messages)!=2 or
            [m.get('role') for m in messages]!=['system','user'] or
            any(set(m)!={'role','content'} or not isinstance(m['content'],str) for m in messages)):
            raise ProviderFailure('history_or_message_protocol')
        if payload['model']!=self.config['model'] or any(payload[k]!=v for k,v in self.config['parameters'].items()):
            raise ProviderFailure('provider_configuration_drift')
        key = os.environ.get(self.config['credential_env'])
        if not key:
            raise ProviderFailure('credential_missing')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):
                return None
        request = urllib.request.Request(ENDPOINT,data=json.dumps(payload,ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.build_opener(NoRedirect).open(request,timeout=self.config['timeout_seconds']) as response:
                return response.read().decode('utf-8')
        except urllib.error.HTTPError as exc:
            # Never save an HTTP body/error string which could contain echoed credentials.
            retry_after = exc.headers.get('Retry-After')
            raise ProviderFailure('http_'+str(exc.code), retry_after if retry_after and retry_after.isdigit() else None) from None
        except (OSError,urllib.error.URLError):
            raise ProviderFailure('transport_or_timeout') from None


def run_sample(config, stage, sample, control, transport, actor, retry=False, post=None):
    control.assert_phase(('PREPARED',) if stage=='pregame' else ('PREGAME_FROZEN',))
    if stage=='postgame':
        if config['postgame_pregame_revision']!=control.state()['pre_revision']:
            raise ValueError('wrong Pregame revision for Postgame')
        pre=accepted_artifact(control,config['postgame_pregame_revision'],sample)
        task=bound_postgame_tasks()[sample['sample_id']]
        bound={'frozen_pregame_output':pre['parsed_output'],'ground_truth':task['ground_truth'],'postgame_evidence':task['postgame_evidence']}
        if post!=bound:raise ValueError('Postgame input is not the frozen same-sample basis')
    context = context_for(sample)
    payload = build_request(config,stage,context,sample['context_sha256'],post)
    revision = config['generation_revision']
    attempt = control.claim(revision,stage,sample['sample_id'],canonical_hash(config),actor,retry)
    folder = HERE/'outputs'/revision/stage/sample['sample_id']/f'attempt-{attempt}'
    request_record = {'sample_id':sample['sample_id'],'stage':stage,'model_visible_inputs':json.loads(payload['messages'][1]['content']),
                      'exact_payload':payload,'request_hash':canonical_hash(payload),'profile_hash':canonical_hash(config)}
    exclusive(folder/'request.json',request_record)
    raw = None; parsed = None; metadata = None; error = None
    try:
        # Pass a new deep value to every transport; no previous response is retained.
        raw = transport(copy.deepcopy(payload))
        if not isinstance(raw,str):
            raise ValueError('response must be raw UTF-8 text')
        exclusive(folder/'raw-response.json',{'raw_response':raw,'response_hash':digest_bytes(raw.encode())})
        parsed,metadata = parse_response(raw,stage,context)
        status = 'ACCEPTED'
    except Exception as exc:
        status = 'FAILED'
        error = {'code':exc.code,'retry_after':exc.retry_after} if isinstance(exc,ProviderFailure) else {'code':'parse_or_execution_failure','type':type(exc).__name__}
    roles = ('forecasting','editorial') if stage=='pregame' else ('postgame',)
    artifact = {'sample_id':sample['sample_id'],'stage':stage,'golden_set_revision':GOLDEN_ID,'parent_dataset_revision':DATASET_ID,
                'generation_revision':revision,'attempt':attempt,'status':status,'model_provider':config['provider'],
                'model_name':config['model'],'model_revision_if_available':metadata.get('model_revision_if_available') if metadata else None,
                'generation_parameters':config['parameters'],'context_hash':sample['context_sha256'],
                'prompt_hash':canonical_hash({r:config['snapshots'][r]['sha256'] for r in ('protocol',stage+'_prompt')}),
                'skill_hash':canonical_hash({r:config['snapshots'][r]['sha256'] for r in roles}),
                'profile_hash':canonical_hash(config),'request_hash':canonical_hash(payload),
                'response_hash':digest_bytes(raw.encode()) if raw is not None else None,'created_at':now(),
                'raw_response':raw,'parsed_output':parsed,'provider_metadata':metadata,'error':error,
                'pregame_generation_revision':config['postgame_pregame_revision'] if stage=='postgame' else None,
                'postgame_input_hash':canonical_hash(post) if post is not None else None}
    exclusive(folder/'artifact.json',artifact)
    control.finish(revision,stage,sample['sample_id'],attempt,status,(folder/'artifact.json').relative_to(HERE),sha(folder/'artifact.json'),actor)
    return status


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--profile',type=Path,required=True); p.add_argument('--stage',choices=['pregame','postgame'],default='pregame')
    p.add_argument('--execute',action='store_true');p.add_argument('--authorize-real-api',action='store_true')
    p.add_argument('--actor',default='local-operator');p.add_argument('--retry-sample')
    args = p.parse_args();config = load_profile(args.profile);control = Control()
    if args.execute:
        raise ValueError('Single-request 0.2/4096 preset retired for formal Golden V1. Use two_stage_first.py.')
    report = preflight(config,args.stage,control,require_empty=not bool(args.retry_sample))
    if not args.execute:
        print(json.dumps(report,ensure_ascii=False));return
    if not args.authorize_real_api:
        raise ValueError('explicit --authorize-real-api required')
    if not os.environ.get(config['credential_env']):
        raise ValueError('credential environment missing; no requests started')
    transport = HTTPSProvider(config)
    selected = samples()
    if args.retry_sample and args.retry_sample not in {s['sample_id'] for s in selected}:
        raise ValueError('retry sample outside selection')
    post_tasks = bound_postgame_tasks() if args.stage=='postgame' else {}
    for sample in selected:
        if args.retry_sample and sample['sample_id'] != args.retry_sample:
            continue
        post = None
        if args.stage=='postgame':
            old = accepted_artifact(control,config['postgame_pregame_revision'],sample)
            task = post_tasks[sample['sample_id']]
            post = {'frozen_pregame_output':old['parsed_output'],'ground_truth':task['ground_truth'],'postgame_evidence':task['postgame_evidence']}
        result = run_sample(config,args.stage,sample,control,transport,args.actor,bool(args.retry_sample),post)
        print(sample['sample_id'],result)
    # Failed samples remain visible; no hidden retry or transition on incomplete batch.
    try:
        control.mark_generated(args.stage,config['generation_revision'],[s['sample_id'] for s in selected],args.actor)
    except ValueError:
        print('Batch incomplete; phase unchanged. Explicit failed-sample retry/new revision only.')
        raise SystemExit(1)


if __name__=='__main__':
    main()
