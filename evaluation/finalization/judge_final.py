"""Single cost-bounded final Judge after an unsuccessful Pro diagnostic run.

No selection among scores; every success uses the same Flash/non-thinking
protocol. Original Pro outputs remain separate and are never pooled.
"""
import json,time,urllib.request,urllib.error
from .common import *
from . import run,judge
from .contracts import ExactValidator

DEST=BASE/'judge-final-v1'
NONFACT={'source','source_urls','source_hashes','source_path','source_ref','captured_at','acquisition_reference','document_hash','source_sha256','interpretation','sql_target_exclusion'}
def compact_context(ctx):
    def trim(x,path=''):
        if isinstance(x,dict):
            # Preserve news/market/official source URLs; only remove repeated
            # player receipt strings, not any stat, identity, status or date.
            return {k:trim(v,path+'/'+k) for k,v in x.items() if k not in NONFACT or path in ('/market/reference',) or k=='source' and path.startswith('/research')}
        if isinstance(x,list):return [trim(v,path+f'/{i}') for i,v in enumerate(x)]
        return x
    result=trim(ctx);check_visible(result);return result

PROMPT=judge.JUDGE_PROMPT+'''\nquote必须是editorial中实际存在的连续原文片段，不改数字、标点，不拼接两处，不写省略号；找不到可留空。不要在quote里复述Context数字。reason和context_reference分别解释事实支持。输出简洁完整JSON，每项reason一到两句，不把长解释挤进quote。'''
def config():
    body={'revision':'judge-final-v1','generator_revision':REV,'model':'deepseek-v4-flash','provider':'DeepSeek','temperature':0.1,'max_tokens':3500,'thinking':'disabled',
        'prompt_hash':digest(PROMPT),'rubric_hash':digest(read(judge.RUBRIC)),'code_hash':digest(Path(__file__).read_bytes()),'schema_hash':digest(judge.schema()),
        'max_attempts_per_article':1,'quality_retries':0,'no_pro_score_selection':True,'source_provenance_compaction':'repeated source/receipt/hash strings removed; facts/values/identity/status/dates preserved',
        'generator_judge_provider_model_family_overlap':True,'human_calibration':'pending','reason':'Pro diagnostic budget/format failure; one homogeneous affordable final evaluator'}
    if not (DEST/'contract.json').exists():write(DEST/'contract.json',body)
    if read(DEST/'contract.json')!=body:raise ValueError('final_judge_contract_drift')
    return body
def one(s):
    cfg=config();folder=BASE/'samples'/s['sample_id'];dest=DEST/s['sample_id']
    if (dest/'artifact.json').exists():return read(dest/'artifact.json')
    ctx=run.ctx_for(s);compact=compact_context(ctx);editorial=read(folder/'editorial/artifact.json')['parsed_output'];prediction=read(folder/'locked-final.json')['prediction']
    user={'frozen_context':compact,'locked_prediction':prediction,'editorial':editorial,'pregame_rubric':read(judge.RUBRIC)['pregame']};check_visible(user)
    req={'model':'deepseek-v4-flash','temperature':0.1,'max_tokens':3500,'thinking':{'type':'disabled'},'stream':False,'response_format':{'type':'json_object'},
        'messages':[{'role':'system','content':PROMPT+'\nJSON schema:\n'+json.dumps(judge.schema(),ensure_ascii=False)},{'role':'user','content':json.dumps(user,ensure_ascii=False)}]}
    write(dest/'request.json',{'request':req,'request_hash':digest(req),'context_hash':s['context_hash'],'judge_context_hash':digest(compact),'editorial_hash':digest(editorial),'annotation_type':'JUDGE_NOT_HUMAN'})
    art={'sample_id':s['sample_id'],'stage':'judge_final','context_hash':s['context_hash'],'editorial_hash':digest(editorial),'judge_context_hash':digest(compact),'request_hash':digest(req),
        'config':{k:v for k,v in req.items() if k!='messages'},'provider_status':'FAILED','parse_status':'NOT_RUN','raw_schema_status':'NOT_RUN','error':None,'created_at':now()};start=time.monotonic()
    try:
        raw=run.wire(req);write(dest/'raw-response.json',{'response':raw,'response_hash':digest(raw.encode())});env=json.loads(raw);art['provider_status']='SUCCEEDED'
        art['provider_metadata']={k:env.get(k) for k in ('id','model','created','system_fingerprint','usage')};choice=env['choices'][0];art['finish_reason']=choice.get('finish_reason')
        art['parse_status']='FAILED';out=json.loads(choice['message']['content']);art['parsed_output']=out;art['parse_status']='PASSED';art['raw_schema_status']='FAILED'
        ExactValidator(judge.schema()).validate(out)
        if art['finish_reason']!='stop':raise ValueError('judge_output_incomplete')
        art['raw_schema_status']='PASSED'
        def strings(x):
            if isinstance(x,str):return [x]
            if isinstance(x,dict):return [text for v in x.values() for text in strings(v)]
            if isinstance(x,list):return [text for v in x for text in strings(v)]
            return []
        text='\n'.join(strings(editorial));quotes=[]
        for group in ('hard_gates','soft_metrics'):
            for k,item in out[group].items():quotes.append({'metric':k,'exact':not item['quote'] or item['quote'] in text,'quote':item['quote']})
        art['quote_audit']=quotes;art['quote_audit_status']='PASSED' if all(q['exact'] for q in quotes) else 'WARNING_REQUIRES_HUMAN_REVIEW'
        # Literal-quote warnings are evaluator reliability findings, not a new
        # article blocker or permission to rewrite its ratings.
    except Exception as e:art['error']={'type':type(e).__name__,'message':str(e)[:1500]}
    art['latency_seconds']=round(time.monotonic()-start,3);art['artifact_hash']=digest(art);write(dest/'artifact.json',art)
    try:
        from evaluation.candidate_v4.trace_v3 import emit
        write(dest/'trace-receipt.json',emit(REV,s['sample_id'],'judge_final',art))
    except Exception as e:write(dest/'trace-receipt.json',{'error':type(e).__name__})
    print(json.dumps({'judge_final':s['sample_id'],'status':art['raw_schema_status'],'quote_audit':art.get('quote_audit_status'),'error':art['error']},ensure_ascii=True),flush=True)
    return art
def main():
    config()
    for s in run.verify()['samples']:
        r=read(BASE/'samples'/s['sample_id']/'chain-manifest.json')
        if r['chain_status']!='SUCCEEDED':continue
        a=one(s)
        if (a.get('error') or {}).get('message')=='provider_http_402':break
if __name__=='__main__':main()
