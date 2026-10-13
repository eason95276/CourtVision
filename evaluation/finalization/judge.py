"""Real judge, blind to outcomes and earlier revisions. No generated human labels."""
import concurrent.futures,json,time
from .common import *
from . import run
from .contracts import ExactValidator

RUBRIC=ROOT/'evaluation/golden_set/v1/rubric.json'
JUDGE_PROMPT='''你是独立的赛前文章评测者，不是作者的辩护人。严格使用给定Context、锁定预测和Pregame Rubric。不得联网、不得使用记忆中的赛果或球员信息，不按实际胜负评分，不看其他样本。不把有篮球机制的条件推断当无依据硬事实；历史候选不能变成已确认首发，明确伤病与日期/样本N必须准确。市场价格不是胜负答案。文章与锁定数字方向矛盾算明确逻辑错误。
对五个hard gates分别PASS/FAIL（quantitative_or_logic_error可NA），说明reason、原文quote及context_reference；没有具体错误不要凑FAIL。六个soft metrics分别1～5，按Rubric实际锚点评，不因为长、华丽或站队就高分。每项给短reason及原文quote。error_tags从Rubric选。不输出整体通过阈值，不修改文章。只返回schema JSON。'''

def schema():
    rubric=read(RUBRIC)['pregame'];gate={'type':'object','additionalProperties':False,'required':['value','reason','quote','context_reference'],
       'properties':{'value':{'enum':['PASS','FAIL','NA']},'reason':{'type':'string'},'quote':{'type':'string'},'context_reference':{'type':'string'}}}
    soft={'type':'object','additionalProperties':False,'required':['score','reason','quote'],'properties':{'score':{'type':'integer','minimum':1,'maximum':5},'reason':{'type':'string'},'quote':{'type':'string'}}}
    gates={k:json.loads(json.dumps(gate)) for k in rubric['hard_gates']}
    for key in gates:
        if key!='quantitative_or_logic_error':gates[key]['properties']['value']['enum']=['PASS','FAIL']
    return {'type':'object','additionalProperties':False,'required':['hard_gates','soft_metrics','error_tags','summary'],
      'properties':{'hard_gates':{'type':'object','additionalProperties':False,'required':list(gates),'properties':gates},
       'soft_metrics':{'type':'object','additionalProperties':False,'required':list(rubric['soft_metrics']),'properties':{k:soft for k in rubric['soft_metrics']}},
       'error_tags':{'type':'array','items':{'enum':rubric['error_tags']}},'summary':{'type':'string'}}}

def one(s):
    folder=BASE/'samples'/s['sample_id']
    if not (folder/'chain-manifest.json').exists():return None
    chain=read(folder/'chain-manifest.json')
    if chain['chain_status']!='SUCCEEDED':return None
    outfolder=BASE/'judge'/s['sample_id']
    if (outfolder/'artifact.json').exists():return read(outfolder/'artifact.json')
    ctx=run.ctx_for(s);article=read(folder/'editorial/artifact.json')['parsed_output'];locked=read(folder/'locked-final.json')
    user={'frozen_context':ctx,'locked_prediction':locked['prediction'],'editorial':article,'pregame_rubric':read(RUBRIC)['pregame']};check_visible(user)
    req={'model':'deepseek-v4-pro','temperature':0,'max_tokens':6000,'thinking':{'type':'enabled'},'reasoning_effort':'low','stream':False,'response_format':{'type':'json_object'},
       'messages':[{'role':'system','content':JUDGE_PROMPT+'\nJSON schema:\n'+json.dumps(schema(),ensure_ascii=False)}, {'role':'user','content':json.dumps(user,ensure_ascii=False)}]}
    write(outfolder/'request.json',{'request':req,'request_hash':digest(req),'prompt_hash':digest(req['messages'][0]),'rubric_hash':digest(read(RUBRIC)),
       'context_hash':s['context_hash'],'editorial_hash':digest(article),'final_hash':digest(locked),'annotation_type':'LLM_JUDGE_NOT_HUMAN'})
    art={'sample_id':s['sample_id'],'stage':'judge','request_hash':digest(req),'context_hash':s['context_hash'],'editorial_hash':digest(article),'config':{k:v for k,v in req.items() if k!='messages'},
       'provider_status':'FAILED','parse_status':'NOT_RUN','raw_schema_status':'NOT_RUN','error':None,'annotation_type':'LLM_JUDGE_NOT_HUMAN','created_at':now()};start=time.monotonic()
    try:
        raw=run.wire(req);write(outfolder/'raw-response.json',{'response':raw,'response_hash':digest(raw.encode())});env=json.loads(raw);art['provider_status']='SUCCEEDED'
        art['provider_metadata']={k:env.get(k) for k in ('model','system_fingerprint','id','usage','created')};choice=env['choices'][0]
        art['parse_status']='FAILED';parsed=json.loads(choice['message']['content']);art['parsed_output']=parsed;art['parse_status']='PASSED';art['raw_schema_status']='FAILED'
        if choice.get('finish_reason')!='stop':raise ValueError('judge_not_complete')
        ExactValidator(schema()).validate(parsed)
        # Evidence quotes must actually appear; do not treat invented judge
        # quotes as trustworthy labels. Reference semantics remain human-checkable.
        def strings(x):
            if isinstance(x,str):return [x]
            if isinstance(x,dict):return [text for v in x.values() for text in strings(v)]
            if isinstance(x,list):return [text for v in x for text in strings(v)]
            return []
        rendered='\n'.join(strings(article))
        for item in list(parsed['hard_gates'].values())+list(parsed['soft_metrics'].values()):
            if item['quote'] and item['quote'] not in rendered:raise ValueError('judge_quote_not_in_editorial')
        art['raw_schema_status']='PASSED'
    except Exception as e:art['error']={'type':type(e).__name__,'message':str(e)[:1800]}
    art['latency_seconds']=round(time.monotonic()-start,3);art['artifact_hash']=digest(art);write(outfolder/'artifact.json',art)
    try:
        from evaluation.candidate_v4.trace_v3 import emit
        write(outfolder/'trace-receipt.json',emit(REV,s['sample_id'],'judge',art))
    except Exception as e:write(outfolder/'trace-receipt.json',{'error':type(e).__name__})
    print(json.dumps({'judge_sample':s['sample_id'],'status':art['raw_schema_status'],'error':art['error']},ensure_ascii=True),flush=True);return art

def main():
    cfg=run.verify();write(BASE/'judge-contract.json',{'model':'deepseek-v4-pro','provider':'DeepSeek','thinking':'enabled','reasoning_effort':'low','temperature':0,'max_tokens':6000,
        'rubric_hash':digest(read(RUBRIC)),'prompt_hash':digest(JUDGE_PROMPT),'judge_code_hash':digest(Path(__file__).read_bytes()),'max_judge_calls':20,'quality_retry':0,'generator_provider_overlap':True,'human_calibration':'PENDING_REAL_LABELS'}) if not (BASE/'judge-contract.json').exists() else None
    if read(BASE/'judge-contract.json')['judge_code_hash']!=digest(Path(__file__).read_bytes()):raise ValueError('judge_code_drift')
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(one,cfg['samples']))
if __name__=='__main__':main()
