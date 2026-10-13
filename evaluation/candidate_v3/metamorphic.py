"""Predeclared synthetic conditional task; Forecast-only, no GT/repair/quality retries."""
import copy,json,sys
from pathlib import Path
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v3 import analysis_tools as a
from evaluation.candidate_v3 import final_workspace as human
from asset_scanner import EVAL,ROOT,read_json,sha,canonical_hash
from control import exclusive,now

FOLDER=EVAL/'metamorphic/v1'

def audit():
    if (FOLDER/'protocol.json').exists():return read_json(FOLDER/'protocol.json')
    manifest=read_json(EVAL/'datasets/counterfactual_candidate_v1/manifest.json');rows=[]
    for item in manifest['cases']:
        path=EVAL/'datasets/counterfactual_candidate_v1'/(item['mutation_id']+'.json')
        if sha(path)!=item['sha256']:raise ValueError('synthetic candidate drift')
        case=read_json(path);kind=item['kind'];eligible=kind in ('availability','rest')
        reason={'availability':'Explicit fictional major home contributor available→out; no claim of real injury/current status.',
            'rest':'Explicit hypothetical completed previous-day game vs2rest days; dates are not real new schedule evidence.',
            'venue':'Not run: control venue is unspecified; proposed away-advantage object is not an established venue-flip contract.',
            'recent_form':'Not run: replaces multiple form metrics with an incomplete structure; not a clean single-factor task.',
            'market':'Not run as paired LLM: independent Forecast forbids market input. Static removal/identical projection check only.'}[kind]
        if kind=='availability':
            player=case['control_context']['team_a']['availability']['players'][0]['name']
            if a.v2.c.name_key(player) not in a.v2.c.catalog(case['control_context'])['names']['home']:raise ValueError('synthetic player not historical home candidate')
        rows.append({'mutation_id':item['mutation_id'],'kind':kind,'file':str(path.relative_to(ROOT)),'hash':item['sha256'],'eligible':eligible,'reason':reason,
            'expected_relation':case['mutation']['expected_relation_if_defined'],'base_context_hash':case['base_context_hash']})
    protocol={'created_at':now(),'target_revision':human.target()['target_revision'],'rows':rows,'total_pairs':25,'eligible_pairs':sum(r['eligible'] for r in rows),
        'max_forecast_calls':50,'repair_calls_allowed':0,'editorial_calls_allowed':0,'quality_retry':0,
        'relation_operationalization':'Adverse home availability/rest: mutated home win probability must not exceed control by more than0.02; also report0 and0.05 sensitivity. Predeclared before calls; not exact expected effect.',
        'material_increase_tolerance':.02,'assumptions':'All added conditions explicitly synthetic hypotheses, not Frozen V1 facts. Only conditional numeric response measured; no real outcome accuracy.',
        'scenario_task_contract':'Same target Forecast system/model/schema; user labels input as hypothetical conditional task. No current membership/confirmed availability inferred from history.',
        'gt_content_opened':0,'postgame_used':False}
    protocol['protocol_hash']=canonical_hash(protocol);exclusive(FOLDER/'protocol.json',protocol);return protocol

def projected_context(case,key,kind):
    ctx=copy.deepcopy(case[key])
    # Market is structurally excluded from independent Forecast even in synthetic tasks.
    ctx['market']=copy.deepcopy(case['control_context']['market']) if kind!='market' else {'status':'unavailable','reason':'Independent Forecast market boundary'}
    return ctx

def run():
    protocol=audit();module=a.module_for(protocol['target_revision']);module.verify();records=[]
    for row in protocol['rows']:
        if not row['eligible']:continue
        pair_folder=FOLDER/'pairs'/row['mutation_id']
        if pair_folder.exists():raise ValueError('synthetic pair already attempted; no reexecution')
        case=read_json(ROOT/row['file']);pair=[]
        for label in ('control_context','mutated_context'):
            if len(list(FOLDER.glob('pairs/*/*/request.json')))>=protocol['max_forecast_calls']:raise ValueError('synthetic call cap')
            ctx=projected_context(case,label,row['kind']);req_messages=module.messages('forecast',ctx)
            user=json.loads(req_messages[1]['content']);user['synthetic_task_notice']='这不是新增真实比赛资料。请做条件预测：仅在此明确虚拟availability/rest条件成立时会怎样；不能写成官方确认、新闻或当前真实名单。保留其他条件，仍用historical_candidate身份与inference分析。'
            req_messages[1]['content']=json.dumps(user,ensure_ascii=False,allow_nan=False)
            request=a.v2.request('forecast',req_messages);sample={'sample_id':row['mutation_id']+':'+label,'context_sha256':canonical_hash(ctx)}
            art=module.component(sample,'metamorphic',request,ctx,pair_folder/label,a.v2.https_once)
            accepted=None;error=art['error'];ops=[]
            if art['protocol_status']=='PASSED':
                try:
                    norm,ops=module.c.normalize(art['parsed_output'],ctx);accepted=module.accept(norm,ctx);error=None
                except Exception as e:error={'type':type(e).__name__,'message':str(e)[:500]}
            result={'label':label,'status':'ACCEPTED' if accepted else 'FAILED','forecast':accepted,'error':error,'canonical_operations':ops,
                'context_hash':canonical_hash(ctx),'original_frozen_context_hash':row['base_context_hash'],'artifact_path':str((pair_folder/label/'artifact.json').relative_to(ROOT)),
                'artifact_hash':sha(pair_folder/label/'artifact.json'),'prediction':accepted['prediction'] if accepted else None}
            exclusive(pair_folder/label/'result.json',result);pair.append(result)
        complete=all(p['status']=='ACCEPTED' for p in pair);delta=None;unexpected=False;consistent=None
        if complete:
            def hp(p):return p['win_probability'] if p['winner_side']=='home' else 1-p['win_probability']
            delta=round(hp(pair[1]['prediction'])-hp(pair[0]['prediction']),6);consistent=delta<=protocol['material_increase_tolerance']
            unexpected=pair[0]['prediction']['winner_side']=='away' and pair[1]['prediction']['winner_side']=='home' and delta>protocol['material_increase_tolerance']
        rec={'mutation_id':row['mutation_id'],'kind':row['kind'],'complete':complete,'directionally_consistent':consistent,'home_probability_delta':delta,
            'unexpected_flip':unexpected,'expected_relation':row['expected_relation'],'protocol_hash':protocol['protocol_hash'],'components':pair}
        exclusive(pair_folder/'pair-result.json',rec);records.append(rec)
        print(json.dumps({'pair':row['mutation_id'],'kind':row['kind'],'complete':complete,'delta':delta,'consistent':consistent}),flush=True)
    completed=[x for x in records if x['complete']];n=sum(x['directionally_consistent'] is True for x in records);d=len(records)
    pertype={kind:{'pairs':sum(x['kind']==kind for x in records),'completed':sum(x['kind']==kind and x['complete'] for x in records),
        'consistent':sum(x['kind']==kind and x['directionally_consistent'] is True for x in records)} for kind in ('availability','rest')}
    market_static=[]
    for row in protocol['rows']:
        if row['kind']=='market':
            case=read_json(ROOT/row['file']);same=projected_context(case,'control_context','market')==projected_context(case,'mutated_context','market');market_static.append({'mutation_id':row['mutation_id'],'inputs_identical_after_market_exclusion':same})
    report={'created_at':now(),'target_revision':protocol['target_revision'],'audited_pairs':25,'executed_pairs':d,'completed_pairs':len(completed),
        'directional_consistency_rate':{'n':n,'d':d,'rate':n/d if d else None},'conditional_on_completed':{'n':n,'d':len(completed),'rate':n/len(completed) if completed else None},
        'unexpected_flip_count':sum(x['unexpected_flip'] for x in records),'invariant_violation_count':sum(not x['inputs_identical_after_market_exclusion'] for x in market_static),
        'invariant_scope':'5 static market projections, not5 model pairs','per_mutation_type':pertype,'rows':records,'market_static':market_static,
        'sensitivity':{str(t):{'consistent':sum(x['home_probability_delta']<=t for x in completed),'completed':len(completed)} for t in (0,.02,.05)},
        'real_model_calls':len(list(FOLDER.glob('pairs/*/*/request.json'))),'judge_calls':0,'limitations':'Synthetic conditional response and stochastic variation; not real-game accuracy or causal effect estimate. Unsupported15 not presented as passed.'}
    exclusive(EVAL/'reports/metamorphic-evaluation.json',report)
    with (EVAL/'reports/metamorphic-evaluation.md').open('x',encoding='utf-8') as f:f.write('# Real synthetic Forecast-only evaluation\n\n'+json.dumps({k:v for k,v in report.items() if k!='rows'},ensure_ascii=False,indent=2)+'\n\n'+json.dumps(records,ensure_ascii=False,indent=2))
if __name__=='__main__':run()
