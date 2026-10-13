"""Revision readers, bounded integrity checks and paired dashboard. No GT content."""
import json,sys,importlib
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v2 import run as v2
from asset_scanner import EVAL,ROOT,read_json,sha,canonical_hash
from control import exclusive,now
from validate_closed_book_context import validate as temporal_validate

REVISIONS=[v2.b.REV,v2.REV,'golden-pregame-system-candidate-v3','golden-pregame-system-candidate-v4']

def module_for(revision):
    if revision==v2.REV:return v2
    if revision.endswith('-v3'):return importlib.import_module('evaluation.candidate_v3.execution_v3')
    if revision.endswith('-v4'):return importlib.import_module('evaluation.candidate_v4.execution_v4')
    return v2.b

def folder_for(revision,sample):
    return (v2.b.BASE if revision==v2.b.REV else EVAL/'execution/candidates'/revision)/'samples'/sample['sample_id']

def component(revision,sample,stage='forecast'):
    folder=folder_for(revision,sample)
    if revision==v2.b.REV and sample['sample_id']==v2.ts.SAMPLE:
        if stage=='forecast':path=v2.b.recovery.PARENT
        elif stage=='repair':path=v2.b.recovery.REPAIR_ROOT/'artifact.json'
        else:return None
    else:
        key=({'forecast':'forecasting','repair':'schema-repair','editorial':'editorial'}[stage] if revision==v2.b.REV else stage)
        path=folder/key/'artifact.json'
    if not path.exists():return None
    art=read_json(path);req=art.get('exact_model_visible_request')
    requestpath=path.parent/'request.json'
    if req is None and requestpath.exists():
        raw=read_json(requestpath);req=raw.get('request') or raw.get('exact_model_visible_request') or raw.get('exact_payload')
    if req is None and (path.parent/'exact-wire-request.json').exists():req=read_json(path.parent/'exact-wire-request.json')['payload']
    raw=art.get('raw_response');env=None;parsed=None
    if raw:
        try:env=json.loads(raw);parsed=json.loads(env['choices'][0]['message']['content'])
        except (ValueError,KeyError,TypeError):pass
    return {'path':str(path.relative_to(ROOT)),'file_hash':sha(path),'artifact':art,'request':req,'envelope':env,'parsed':parsed,'stage':stage}

def load(revision,sample):
    folder=folder_for(revision,sample);path=folder/('final-record.json' if revision==v2.b.REV else 'chain-manifest.json');rec=read_json(path)
    key='record_hash' if revision==v2.b.REV else 'manifest_hash'
    if rec[key]!=canonical_hash({k:v for k,v in rec.items() if k!=key}):raise ValueError('record hash mismatch')
    for ref in rec['artifact_refs']:
        if sha(ROOT/ref['path'])!=ref['sha256']:raise ValueError('component artifact changed')
    if rec['sample_id']!=sample['sample_id'] or rec['context_hash']!=sample['context_sha256']:raise ValueError('sample binding mismatch')
    ctx=v2.b.context_for(sample);initial=component(revision,sample)
    accepted=read_json(folder/'accepted-forecast.json')['forecast'] if (folder/'accepted-forecast.json').exists() else None
    writer=component(revision,sample,'editorial');editorial=writer['parsed'] if writer and rec['editorial_status']=='ACCEPTED' else None
    return {'revision':revision,'sample':sample,'record':rec,'context':ctx,'initial':initial,'accepted':accepted,'editorial':editorial,
        'components':[a for stage in ('forecast','repair','editorial') if (a:=component(revision,sample,stage)) is not None]}

def rates(rows):
    def f(n,d=20):return {'n':n,'d':d,'rate':n/d if d else None}
    rr=[x['record'] for x in rows];repair=sum(x['repair_triggered'] for x in rr)
    return {'n':len(rr),'provider_success':f(sum(x['provider_status']=='SUCCEEDED' for x in rr)),
        'parse_success':f(sum(x['parse_status']=='PASSED' for x in rr)),
        'raw_schema_compliance':f(sum(x['raw_schema_status']=='PASSED' for x in rr)),
        'repair_trigger':f(repair),'repair_recovery':f(sum(x['repair_status']=='ACCEPTED' for x in rr),repair),
        'forecast_accepted':f(sum(x['forecast_status']=='ACCEPTED' for x in rr)),
        'editorial_success':f(sum(x['editorial_status']=='ACCEPTED' for x in rr)),
        'e2e_success':f(sum(x['chain_status'] in ('ACCEPTED','SUCCEEDED') for x in rr)),
        'execution_failure_rate':f(sum(x['chain_status']=='FAILED' for x in rr))}

def request_context(data):
    if 'closed_book_context' in data:return data['closed_book_context']
    if 'basketball' in data:return data['basketball'].get('closed_book_context')
    return None

def evidence_ok(row):
    if row['accepted'] is None:return False,'No accepted Forecast; not a content score'
    try:
        module=module_for(row['revision'])
        if row['revision']==v2.REV:module.accept(row['accepted'],row['context'])
        elif row['revision']!=v2.b.REV:module.accept(row['accepted'],row['context'])
        else:return False,'No accepted V1 Forecast'
        return True,'Reference existence/same-side candidate contract; not claim-to-source semantic truth'
    except Exception as e:return False,type(e).__name__+':'+str(e)[:150]

def binding_and_isolation(row):
    forbidden={'ground_truth','ground_truth_path','actual_margin','actual_total','target_boxscore','target_minutes','target_dnp','hidden_outcome_bucket','selection_bucket','postgame_evidence'}
    def check(x):
        if isinstance(x,dict):return not forbidden.intersection(x) and all(check(v) for v in x.values())
        if isinstance(x,list):return all(check(v) for v in x)
        return True
    binding=True;isolated=True
    for comp in row['components']:
        req=comp['request']
        if not req:return False,False
        if [m['role'] for m in req.get('messages',[])]!=['system','user'] or set(req)&{'tools','functions','tool_choice','search','retrieval','conversation'}:return False,False
        data=json.loads(req['messages'][1]['content']);frozen=request_context(data)
        binding=binding and frozen==row['context'];isolated=isolated and check(data)
        if comp['stage']=='editorial':binding=binding and data.get('locked_forecast')==row['accepted']
        elif comp['stage']=='repair':
            original=data.get('original_response')
            if original is not None:binding=binding and original==row['initial']['parsed']
    return binding,isolated

def static_temporal(row):
    match=next(x for x in map(json.loads,(EVAL/'datasets/closed_book_candidates.jsonl').read_text(encoding='utf-8').splitlines()) if x['sample_id']==row['sample']['sample_id'])
    prov=read_json(EVAL/match['provenance_path'])
    errors=temporal_validate(row['context'],prov,expected_context_hash=row['sample']['context_sha256'])
    return not errors,errors

def metric_checks(row):
    binding,isolation=binding_and_isolation(row);evidence,why=evidence_ok(row);temporal,errors=static_temporal(row)
    parsed=row['initial']['parsed'];schema=False
    if parsed is not None:
        try:
            module=module_for(row['revision'])
            if row['revision']==v2.b.REV:module.fw.VersionedIndependentForecast.model_validate(parsed)
            elif row['revision']==v2.REV:module.c.CandidateForecast.model_validate(parsed)
            else:module.raw_schema(parsed,'forecast',row['context'])
            schema=True
        except Exception:pass
    success=row['record']['chain_status'] in ('SUCCEEDED','ACCEPTED')
    outputvalid=False
    if success and row['accepted'] is not None and row['editorial'] is not None:
        try:module_for(row['revision']).ts.EditorialDraft.model_validate(row['editorial']) if row['revision'] in (v2.REV,v2.b.REV) else v2.ts.EditorialDraft.model_validate(row['editorial']);outputvalid=evidence and binding
        except Exception:pass
    return {'execution_success':(success,'Candidate chain accepted; not publication/content quality'),
        'parse_success':(parsed is not None,'Initial Forecast JSON parse'),
        'schema_compliance':(schema,'Initial raw schema of its pinned revision, pre-normalization'),
        'evidence_validity':(evidence,why),'temporal_integrity':(temporal,'Source event dates/target exclusion only; not strict PIT or article fact certification. '+str(errors)),
        'ground_truth_isolation':(isolation,'No explicit hidden truth fields/tools/history in saved requests; parameter memory not excluded'),
        'context_binding':(binding,'Exact same-sample Context / persisted accepted Forecast binding'),
        'output_contract_validity':(outputvalid,'Accepted numeric/reference/schema bindings; not word-by-word article hard-fact gate')}

def summarize(revision):
    rows=[load(revision,s) for s in v2.b.selection()];metrics=rates(rows);calls=Counter();tokens=Counter();latencies=[]
    for row in rows:
        for comp in row['components']:
            # Native repair artifact may reflect precondition0calls; only dispatched requests count.
            if comp['request'] is None:continue
            calls[comp['stage']]+=1
            for key,val in ((comp['envelope'] or{}).get('usage') or{}).items():
                if isinstance(val,int):tokens[key]+=val
            if comp['artifact'].get('latency_seconds') is not None:latencies.append(comp['artifact']['latency_seconds'])
    failures=[{'sample_id':x['sample']['sample_id'],'stage':x['record']['failure_stage'],'type':x['record']['failure_type'],
        'error':x['record'].get('error') or x['record'].get('failure_code')} for x in rows if x['record']['chain_status']=='FAILED']
    return {'revision':revision,'dataset_revision':v2.b.DATASET_ID,'golden_set_revision':v2.b.GOLDEN_ID,'metrics':metrics,'calls':dict(calls),'total_calls':sum(calls.values()),
        'token_usage':dict(tokens),'latencies_seconds':latencies,'failures':failures,'rows':[{'sample_id':x['sample']['sample_id'],'date':x['sample']['game_date'],
            'game':x['sample']['away_team']+' @ '+x['sample']['home_team'],'chain_status':x['record']['chain_status'],'forecast_status':x['record']['forecast_status'],
            'editorial_status':x['record']['editorial_status'],'raw_schema_status':x['record']['raw_schema_status']} for x in rows]}

def final_reports():
    revisions=[rev for rev in REVISIONS if all((folder_for(rev,s)/('final-record.json' if rev==v2.b.REV else 'chain-manifest.json')).exists() for s in v2.b.selection())]
    data=[summarize(rev) for rev in revisions];paired=[]
    for sample in v2.b.selection():
        paired.append({'sample_id':sample['sample_id'],'game':sample['away_team']+' @ '+sample['home_team'],'revisions':{d['revision']:next(x for x in d['rows'] if x['sample_id']==sample['sample_id']) for d in data}})
    comparisons=[]
    for old,new in zip(data,data[1:]):
        ids=[(a,b) for a,b in zip(old['rows'],new['rows'])]
        comparisons.append({'from':old['revision'],'to':new['revision'],
            'failures_fixed':[a['sample_id'] for a,b in ids if a['chain_status']=='FAILED' and b['chain_status'] in ('SUCCEEDED','ACCEPTED')],
            'successes_retained':[a['sample_id'] for a,b in ids if a['chain_status'] in ('SUCCEEDED','ACCEPTED') and b['chain_status'] in ('SUCCEEDED','ACCEPTED')],
            'successes_regressed':[a['sample_id'] for a,b in ids if a['chain_status'] in ('SUCCEEDED','ACCEPTED') and b['chain_status']=='FAILED'],
            'raw_schema_pass_to_fail':[a['sample_id'] for a,b in ids if a['raw_schema_status']=='PASSED' and b['raw_schema_status']!='PASSED'],
            'still_failed':[a['sample_id'] for a,b in ids if a['chain_status']=='FAILED' and b['chain_status']=='FAILED']})
    report={'created_at':now(),'n_per_revision':20,'revisions':data,'paired':paired,'comparisons':comparisons,
        'limits':'Changed execution contracts, same frozen20; not isolated causal uplift, publication certification or Forward accuracy. No content scores.'}
    exclusive(EVAL/'reports/execution-quality-dashboard.json',report)
    text='# V1/V2/V3/V4 same20 paired execution\n\n'+report['limits']+'\n\n| Revision | Parse | Raw schema | Repair | Recovery | Forecast | Editorial | E2E |\n|---|---:|---:|---:|---:|---:|---:|---:|\n'
    for d in data:
        m=d['metrics'];values=[m[k] for k in ('parse_success','raw_schema_compliance','repair_trigger','repair_recovery','forecast_accepted','editorial_success','e2e_success')]
        text+='| '+d['revision']+' | '+' | '.join(str(v['n'])+'/'+str(v['d']) for v in values)+' |\n'
    text+='\n## Per-sample paired table\n\n| Sample | Game | '+' | '.join(d['revision'].split('-')[-1] for d in data)+' |\n|---|---|'+'---|'*len(data)+'\n'
    for row in paired:text+='| '+row['sample_id']+' | '+row['game']+' | '+' | '.join(row['revisions'][d['revision']]['chain_status'] for d in data)+' |\n'
    text+='\n## Regressions (never hidden)\n\n'+json.dumps(comparisons,ensure_ascii=False,indent=2)
    with (EVAL/'reports/golden-pregame-v1-v4-regression.md').open('x',encoding='utf-8') as f:f.write(text)
    exclusive(EVAL/'reports/golden-pregame-v1-v4-regression.json',report);return report

if __name__=='__main__':print(json.dumps({'revisions':[d['revision'] for d in final_reports()['revisions']]}))
