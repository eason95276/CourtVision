"""Last read-only checks, independent of generation and of report wording."""
from .common import *
from . import run
from .contracts import CandidateForecast
from v2.pregame_system.forecasting_workflow import validate_independent_market_boundary,ForecastNumbers
from .human import api,EXPORT

def validate_human_workspace(tasks,prepared,*,initial=False):
    """Validate a chronological prefix; real annotation is not a failure."""
    if not isinstance(tasks,list) or not tasks or len(tasks)>len(prepared):raise ValueError('unexpected_human_task_count')
    expected={t['meta']['sample_id']:t for t in prepared}
    ids=[t.get('meta',{}).get('sample_id') for t in tasks]
    if len(set(ids))!=len(ids) or set(ids)!={t['meta']['sample_id'] for t in prepared[:len(tasks)]}:raise ValueError('human_tasks_not_chronological_prefix')
    rubric=read(ROOT/'evaluation/golden_set/v1/rubric.json')['pregame']
    required=set(rubric['hard_gates'])|set(rubric['soft_metrics']);raw_annotations=0;complete=0
    for task in tasks:
        wanted=expected[task['meta']['sample_id']]
        if task['meta']!=wanted['meta'] or task['data']!=wanted['data']:raise ValueError('live_human_task_drift')
        if task.get('predictions'):raise ValueError('automatic_predictions_in_human_workspace')
        for annotation in task.get('annotations',[]):
            if annotation.get('was_cancelled'):continue
            raw_annotations+=1
            fields={r.get('from_name') for r in annotation.get('result',[]) if r.get('type')=='choices' and r.get('value',{}).get('choices')}
            complete+=int(required<=fields)
    if initial and (len(tasks)!=1 or raw_annotations):raise ValueError('initial_human_workspace_not_empty_single_task')
    return {'human_visible_tasks':len(tasks),'human_annotations':raw_annotations,'human_labels':complete,
            'human_initial_state_required':initial,'human_automatic_predictions':0}

def main(output_path=None,*,initial_workspace=False):
    cfg=run.verify();m=read(HERE/'metrics.json');e2e=0;bound=0;market_isolated=0;rejected=[]
    for s in cfg['samples']:
        ctx=run.ctx_for(s);folder=BASE/'samples'/s['sample_id'];r=read(folder/'chain-manifest.json')
        if r['manifest_hash']!=digest({k:v for k,v in r.items() if k!='manifest_hash'}):raise ValueError('chain_manifest_hash')
        req=read(folder/'forecast/request.json');visible=json.loads(req['request']['messages'][1]['content'])['closed_book_context']
        if 'market' in visible:raise ValueError('independent_market_not_isolated')
        market_isolated+=1
        eligible=True
        if (folder/'accepted-forecast.json').exists():
            try:validate_independent_market_boundary(CandidateForecast.model_validate(read(folder/'accepted-forecast.json')))
            except ValueError:eligible=False;rejected.append(s['sample_id'])
        if r['chain_status']=='SUCCEEDED':
            final=read(folder/'locked-final.json');ForecastNumbers.model_validate(final['prediction'])
            writer=json.loads(read(folder/'editorial/request.json')['request']['messages'][1]['content'])
            if digest(writer['locked_forecast'])!=digest(final) or digest(writer['closed_book_context'])!=s['context_hash']:raise ValueError('writer_final_context_mismatch')
            e2e+=int(eligible);bound+=1
    old=read(HERE/'protection-before.json');current=protected()
    if any(current['files'].get(p)!=h for p,h in old['files'].items()) or digest(old['versions'])!=digest(current['versions']):raise ValueError('protected_original_drift')
    pid=read(EXPORT/'workspace.json')['project_id'];tasks=api(f'/api/projects/{pid}/export?exportType=JSON&download_all_tasks=true')
    prepared=read(EXPORT/'tasks-qualified.json')
    human_state=validate_human_workspace(tasks,prepared,initial=initial_workspace)
    for file in ('FINAL_EVALUATION_REPORT.md','RESUME_PROJECT.md','INTERVIEW_GUIDE.md','README.md','evaluation/reports/FINAL_PROJECT_STATUS.md'):
        if not (ROOT/file).exists():raise ValueError('deliverable_missing:'+file)
    report={'n':20,'e2e_success':e2e,'writer_final_and_context_bound':bound,'independent_market_isolated':market_isolated,**human_state,
        'original_files_unchanged':len(old['files']),'prediction_versions_unchanged':len(old['versions']['prediction_versions']),'postgame_versions_unchanged':len(old['versions']['postgame_versions']),
        'reported_e2e_matches':e2e==m['generation']['qualified_e2e_success'],'qualification_rejected':rejected,'candidate_verified':True,'status':m['status'],'generated_models':0,'created_at':now()}
    if m['status']=='FINALIZED':
        from .judge_results import load_labels
        index,labels=load_labels()
        if len(labels)!=e2e:raise ValueError('final_judge_cohort_not_complete')
        report['accepted_judge_labels']=len(labels)
        if 'COURTVISION EVALUATION V1 = FINALIZED' not in (ROOT/'evaluation/reports/FINAL_PROJECT_STATUS.md').read_text(encoding='utf-8'):raise ValueError('final_status_document_mismatch')
    path=Path(output_path) if output_path else HERE/'closeout-verification.json'
    if not path.resolve().is_relative_to(HERE):raise ValueError('verification_output_out_of_scope')
    write(path,report);print(json.dumps(report))
if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--output');parser.add_argument('--initial-workspace',action='store_true');args=parser.parse_args();main(args.output,initial_workspace=args.initial_workspace)
