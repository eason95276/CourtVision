"""One final revision, bounded structural recovery, no production writes."""
import argparse,copy,json,time,urllib.request,urllib.error,concurrent.futures
from .common import *
from .data import CONTEXTS
from . import contracts as c
from v2.pregame_system.editorial_pipeline import EditorialDraft
from v2.pregame_system.forecasting_workflow import validate_independent_market_boundary

FORECAST='''你是第25秒篮球分析师。只用当前冻结赛前输入，无搜索、工具、记忆、目标赛果或其他样本。中文简洁分析，球员保留输入canonical name。所有概率是winner概率，分差是home minus away。数字按schema刻度给明确方向。injury/roster/source lineup有来源则可用，historical observation不证明当前出场；缺失保持未知。pregame_plan可为null：已有冻结首发在Context中，无需重复造名单。需要预计时仅选输入本队候选并明确是agent_expected/historical_candidate。
uncertainties在顶层，不在analysis_core内。identified/analytical须非空key_matchups；analytical全部classification=inference；无明确对位用no_clear_matchup和空列表。只引用提供context.* ID。central_thesis必须是对象而不是字符串。简短字段，通常2～4 factors，1～2matchups。不要把schema定义复制到输出字段。只返回一个JSON对象。当前Independent未开放市场，跳过Skill尚需市场的步骤。'''
REPAIR='''这是唯一一次schema/结构修复，不是重新预测。原始输出永久保存。不得改变prediction数字、选中的球员、预计分钟范围。只修合法键、enum、引用metadata、长度和格式；允许压缩过长文字，不得补入新的事实。uncertainties移到顶层；schema annotation不是内容；按提供schema只输出JSON。引用顺序不代表篮球语义。不能修复则不伪造成功。'''
MARKET='''你负责第25秒市场复核。独立判断已经冻结。市场只是历史赛前参照，不是答案。明显分歧先检查已有伤病、轮换、实际可用性、主场和具体对位；不联网，不假设市场掌握哪些消息，不编盘口定价原因。分别权衡分差和总分，可以保持也可以修正，向市场靠近或远离均可，不固定加权。reference的open/close和quote timestamp未知，不能宣称精确T-20报价。
输出prediction、decision=keep或revise、rationale、evidence_ids。keep数字必须与Independent相同。prediction遵守同样winner概率与主队视角分差协议。只输出JSON。'''
EDITORIAL='''你是第25秒赛前作者，遵守Editorial Skill。只读本场Frozen Context和本次锁定Final Forecast。不得更改数字，不能用赛后结果。写完整连贯400～600中文字短评，文章可自然分2～4段。标题抓本场独有对位。明确事实与假设，未知名单不补造。历史部分指标按实际N，不称完整赛季。市场比较按deterministic market_interpretation，不替市场解释动机。来源指针仅使用evidence_catalog。只返回EditorialDraft JSON。'''

def skill(role):return (HERE/'snapshots'/f'{role}.txt').read_text(encoding='utf-8')
def ctx_for(s):
    ctx=read(ROOT/s['context_path'])
    if digest(ctx)!=s['context_hash']:raise ValueError('context_drift')
    check_visible(ctx);return ctx
def market_schema():
    from v2.pregame_system.forecasting_workflow import ForecastNumbers
    return {'type':'object','additionalProperties':False,'required':['prediction','decision','rationale','evidence_ids'],
      'properties':{'prediction':ForecastNumbers.model_json_schema(),'decision':{'enum':['keep','revise']},'rationale':{'type':'string','minLength':1,'maxLength':1600},'evidence_ids':{'type':'array','minItems':1,'items':{'type':'string'}}}}
def interpretation(pred,ctx):
    q=ctx['market'].get('reference')
    if not q:return {'status':'unavailable'}
    edge=pred['expected_home_margin']+q['home_spread'] if q.get('home_spread') is not None else None
    total=pred['expected_total']-q['total'] if q.get('total') is not None else None
    return {'margin_perspective':'home_minus_away','home_spread':q.get('home_spread'),'market_total':q.get('total'),'spread_edge':edge,
       'ats_lean':'home' if edge is not None and edge>0 else 'away' if edge is not None and edge<0 else 'no_edge',
       'total_edge':total,'total_lean':'over' if total is not None and total>0 else 'under' if total is not None and total<0 else 'aligned'}
def messages(stage,ctx,forecast=None,original=None,error=None):
    independent=c.independent_context(ctx)
    if stage=='forecast':
        system=FORECAST+'\n'+skill('forecasting');schema=c.schema(independent);user={'closed_book_context':independent,'evidence_catalog':c.catalog(independent)[0]}
    elif stage=='repair':
        system=REPAIR;schema=c.schema(independent);user={'closed_book_context':independent,'original_response':original,'error':error,'evidence_catalog':c.catalog(independent)[0]}
    elif stage=='market_review':
        system=MARKET+'\n'+skill('forecasting');schema=market_schema();user={'closed_book_context':ctx,'independent_forecast':forecast,'market_interpretation':interpretation(forecast['prediction'],ctx),'evidence_catalog':c.catalog(ctx,True)[0]}
    else:
        system=EDITORIAL+'\n'+skill('editorial');schema=EditorialDraft.model_json_schema();schema['properties']['article']['minLength']=400;user={'closed_book_context':ctx,'locked_forecast':forecast,'market_interpretation':interpretation(forecast['prediction'],ctx),'evidence_catalog':c.catalog(ctx,True)[0]}
    system+='\n只返回以下schema的单个JSON对象：\n'+json.dumps(schema,ensure_ascii=False)
    check_visible(user)
    return [{'role':'system','content':system},{'role':'user','content':json.dumps(user,ensure_ascii=False,allow_nan=False)}],schema
def request(stage,msg):
    return {'model':'deepseek-v4-flash','messages':msg,'temperature':0.1,'max_tokens':3500 if stage=='editorial' else 4800,'thinking':{'type':'disabled'},'stream':False,'response_format':{'type':'json_object'}}
def wire(req):
    credentials()
    if len(req['messages'])!=2 or [x['role'] for x in req['messages']]!=['system','user'] or 'tools' in req or 'functions' in req:raise ValueError('stateless_wire_violation')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kw):return None
    r=urllib.request.Request('https://api.deepseek.com/chat/completions',data=json.dumps(req,ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY'],'Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect).open(r,timeout=180) as response:return response.read().decode('utf-8')
    except urllib.error.HTTPError as e:raise ValueError('provider_http_'+str(e.code)) from None

def freeze():
    if (BASE/'contract.json').exists():return verify()
    manifest=read(CONTEXTS/'final-manifest.json')
    for role in ('forecasting','editorial'):
        p=HERE/'snapshots'/f'{role}.txt';p.parent.mkdir(parents=True,exist_ok=True)
        with p.open('x',encoding='utf-8') as f:f.write((ROOT/'agent/skills'/role/'SKILL.md').read_text(encoding='utf-8'))
    config={'system_revision':REV,'dataset_revision':DATA,'golden_set_revision':GOLD,'manifest_hash':manifest['manifest_hash'],'provider':'DeepSeek','model':'deepseek-v4-flash','model_revision':None,
      'generation_parameters':{'temperature':0.1,'forecast_max_tokens':4800,'market_review_max_tokens':4800,'editorial_max_tokens':3500,'thinking':'disabled','response_format':'json_object'},
      'repair_limit':1,'ordinary_retries':0,'quality_retries':0,'source_hashes':{p.name:digest(p.read_bytes()) for p in [Path(__file__),HERE/'contracts.py',HERE/'common.py']},
      'skill_hashes':{role:digest((HERE/'snapshots'/f'{role}.txt').read_bytes()) for role in ('forecasting','editorial')},'samples':manifest['samples'],
      'chain':'independent Forecast -> optional historical Market Review -> Editorial','production_equivalence':False,'reason':'richer historical replay; scoped final evaluation adapter; no production modification',
      'repair_semantic_lock':'prediction numbers + selected lineup players + minute ranges; metadata ordering/schema annotation may change; prose compression explicitly not lossless'}
    config['prompt_hashes']={stage:digest(messages(stage,ctx_for(manifest['samples'][0]),{'prediction':{'expected_home_margin':1.5,'expected_total':225.5}} if stage in ('market_review','editorial') else None)[0][0]) for stage in ('forecast','repair','market_review','editorial')}
    config['contract_hash']=digest(config);write(BASE/'contract.json',config);return config
def verify():
    config=read(BASE/'contract.json')
    if digest({k:v for k,v in config.items() if k!='contract_hash'})!=config['contract_hash']:raise ValueError('contract_tamper')
    for p,h in config['source_hashes'].items():
        current=digest((HERE/p).read_bytes())
        if current!=h:
            receipt=read(HERE/'runtime-correction.json') if (HERE/'runtime-correction.json').exists() else {}
            patch=receipt.get('files',{}).get(p,{})
            if patch.get('original_hash')!=h or patch.get('corrected_hash')!=current or digest((HERE/patch.get('original_snapshot','missing')).read_bytes())!=h:raise ValueError('candidate_source_drift')
    for role,h in config['skill_hashes'].items():
        if digest((HERE/'snapshots'/f'{role}.txt').read_bytes())!=h:raise ValueError('skill_snapshot_drift')
    return config
def component(s,stage,ctx,folder,forecast=None,original=None,error=None,transport=wire):
    msg,schema=messages(stage,ctx,forecast,original,error);req=request(stage,msg)
    write(folder/'request.json',{'sample_id':s['sample_id'],'request':req,'request_hash':digest(req),'context_hash':s['context_hash'],'prompt_hash':digest(msg[0]),'schema_hash':digest(schema)})
    art={'sample_id':s['sample_id'],'stage':stage,'request_hash':digest(req),'context_hash':s['context_hash'],'config':{k:v for k,v in req.items() if k!='messages'},'provider_status':'FAILED','parse_status':'NOT_RUN','raw_schema_status':'NOT_RUN','error':None,'created_at':now()};start=time.monotonic()
    try:
        raw=transport(req);write(folder/'raw-response.json',{'response':raw,'response_hash':digest(raw.encode())})
        env=json.loads(raw);art['provider_status']='SUCCEEDED';art['provider_metadata']={k:env.get(k) for k in ('id','model','created','system_fingerprint','usage')}
        choice=env['choices'][0];art['finish_reason']=choice.get('finish_reason');art['parse_status']='FAILED'
        content=choice['message']['content'];out=json.loads(content);art['parsed_output']=out;art['parse_status']='PASSED'
        if choice.get('finish_reason')!='stop' or choice['message'].get('tool_calls') or len(env['choices'])!=1:raise ValueError('provider_protocol_failure')
        art['raw_schema_status']='FAILED';c.ExactValidator(schema).validate(out)
        if stage=='editorial':EditorialDraft.model_validate(out)
        elif stage in ('forecast','repair'):c.CandidateForecast.model_validate(out)
        art['raw_schema_status']='PASSED'
    except Exception as e:art['error']={'type':type(e).__name__,'message':str(e)[:1800]}
    art['latency_seconds']=round(time.monotonic()-start,3);art['artifact_hash']=digest(art);write(folder/'artifact.json',art)
    try:
        from evaluation.candidate_v4.trace_v3 import emit
        write(folder/'trace-receipt.json',emit(REV,s['sample_id'],stage,art))
    except Exception as e:write(folder/'trace-receipt.json',{'status':'unavailable','error':type(e).__name__})
    return art

def execute(s,transport=wire):
    verify();ctx=ctx_for(s);folder=BASE/'samples'/s['sample_id'];folder.mkdir(parents=True,exist_ok=False)
    r={'sample_id':s['sample_id'],'game_id':s['game_id'],'context_hash':s['context_hash'],'system_revision':REV,'dataset_revision':DATA,'forecast_status':'FAILED','editorial_status':'NOT_RUN','market_status':'NOT_RUN','chain_status':'FAILED','failure_stage':'FORECAST','content_quality':'NA','repair_triggered':False,'attempts':{'forecast':1,'repair':0,'market_review':0,'editorial':0}}
    write(folder/'preflight.json',{'status':'PASS','context_hash':s['context_hash'],'tools':[],'history':[],'retrieval':False,'ground_truth_access':0})
    initial=component(s,'forecast',ctx,folder/'forecast',transport=transport);r.update({k:initial[k] for k in ('provider_status','parse_status','raw_schema_status')});accepted=None;err=initial['error']
    if initial.get('parsed_output') and initial.get('finish_reason')=='stop':
        try:
            norm,ops=c.normalize(initial['parsed_output']);accepted=c.accept(norm,c.independent_context(ctx))
            validate_independent_market_boundary(c.CandidateForecast.model_validate(accepted))
            write(folder/'normalization.json',{'operations':ops,'prediction_unchanged':initial['parsed_output']['prediction']==accepted['prediction']})
        except Exception as e:
            # A tentative schema result is not accepted until every boundary
            # validation has returned. Never retain it across an exception.
            accepted=None
            err={'type':type(e).__name__,'message':str(e)[:1800]}
            if initial['raw_schema_status']=='FAILED':
                try:c.ForecastNumbers.model_validate(initial['parsed_output']['prediction']);eligible=True
                except Exception:eligible=False
                if eligible:
                    r['repair_triggered']=True;r['attempts']['repair']=1
                    rep=component(s,'repair',ctx,folder/'repair',original=initial['parsed_output'],error=err,transport=transport)
                    try:
                        if rep['raw_schema_status']!='PASSED':raise ValueError(str(rep['error']))
                        c.preserve(initial['parsed_output'],rep['parsed_output']);accepted=c.accept(rep['parsed_output'],c.independent_context(ctx));r['repair_status']='ACCEPTED'
                        validate_independent_market_boundary(c.CandidateForecast.model_validate(accepted))
                    except Exception as e:accepted=None;r['repair_status']='FAILED';err={'type':type(e).__name__,'message':str(e)[:1800]}
    if accepted:
        r['forecast_status']='ACCEPTED';write(folder/'accepted-forecast.json',accepted);final=copy.deepcopy(accepted)
        if ctx['market']['status']!='UNAVAILABLE':
            r['attempts']['market_review']=1;r['failure_stage']='MARKET_REVIEW'
            review=component(s,'market_review',ctx,folder/'market_review',forecast=accepted,transport=transport)
            try:
                if review['raw_schema_status']!='PASSED':raise ValueError(str(review['error']))
                result=review['parsed_output'];c.ForecastNumbers.model_validate(result['prediction'])
                if not set(result['evidence_ids'])<={e['id'] for e in c.catalog(ctx,True)[0]}:raise ValueError('unknown_market_review_reference')
                if result['decision']=='keep' and result['prediction']!=accepted['prediction']:raise ValueError('keep_changed_prediction')
                final['prediction']=result['prediction'];r['market_status']='ACCEPTED';final['market_review']=result
            except Exception as e:r['market_status']='FAILED';err={'type':type(e).__name__,'message':str(e)[:1800]};final=None
        else:r['market_status']='UNAVAILABLE'
        if final:
            write(folder/'locked-final.json',final);r['final_hash']=digest(final);r['failure_stage']='EDITORIAL';r['attempts']['editorial']=1
            art=component(s,'editorial',ctx,folder/'editorial',forecast=read(folder/'locked-final.json'),transport=transport)
            if art['raw_schema_status']=='PASSED':
                try:
                    for p in art['parsed_output']['player_analysis']:
                        if not set(p['fact_ids'])<={e['id'] for e in c.catalog(ctx,True)[0]}:raise ValueError('unknown_writer_fact_id')
                    r.update(editorial_status='ACCEPTED',chain_status='SUCCEEDED',failure_stage=None,content_quality='PENDING_JUDGE_AND_HUMAN');err=None
                except Exception as e:r['editorial_status']='FAILED';err={'type':type(e).__name__,'message':str(e)}
            else:r['editorial_status']='FAILED';err=art['error']
    r['error']=err;r['created_at']=now();r['refs']={str(p.relative_to(folder)):digest(p.read_bytes()) for p in folder.rglob('*.json')};r['manifest_hash']=digest(r);write(folder/'chain-manifest.json',r)
    print(json.dumps({'sample':s['sample_id'],'chain':r['chain_status'],'attempts':r['attempts'],'error':err},ensure_ascii=True),flush=True);return r

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['freeze','execute']);parser.add_argument('--authorize-real-api',action='store_true');a=parser.parse_args()
    if a.command=='freeze':print(json.dumps({'contract_hash':freeze()['contract_hash']}))
    else:
        if not a.authorize_real_api:raise ValueError('explicit_authorization_required')
        config=verify();pending=[s for s in config['samples'] if not (BASE/'samples'/s['sample_id']/'chain-manifest.json').exists()]
        # Three independent stateless samples at a time; never combine contexts.
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(execute,pending))
