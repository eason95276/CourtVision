import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'golden_set/v1'))
from setup_label_studio import request,HERE,EVAL,save,read_json

projects=read_json(HERE/'label_studio_projects.json');results={};pregame_ids=set();post_ids=set();pregame_samples=set();post_samples=set()
for stage,p in projects.items():
    response=request('/api/tasks/?project='+str(p['project_id'])+'&page_size=100');tasks=response.get('tasks') or response.get('results') if isinstance(response,dict) else response
    project=request('/api/projects/'+str(p['project_id'])+'/');leaks=[]
    for t in tasks:
        data=json.dumps(t['data'],ensure_ascii=False);meta=json.dumps(t['meta'],ensure_ascii=False)
        if stage=='pregame':
            for k in ('ground_truth','actual_margin','actual_total','hidden_outcome_bucket','postgame_evidence','selection_bucket'):
                if k in data or k in meta:leaks.append({'task':t['id'],'key':k})
            pregame_ids.add(t['id']);pregame_samples.add(t['meta']['sample_id'])
        else:post_ids.add(t['id']);post_samples.add(t['meta']['sample_id'])
        assert not t.get('annotations',[]) and not t.get('predictions',[]) and t['data']['model_output'] is None
    results[stage]={'project_id':p['project_id'],'tasks':len(tasks),'annotations':project['num_tasks_with_annotations'],'data_leaks':leaks,
        'stage_data_retrievable_with_same_admin_token':True,'maximum_annotations':project.get('maximum_annotations'),'show_annotation_history':project.get('show_annotation_history')}
results['blindness_limit']={'task_id_overlap':len(pregame_ids&post_ids),'shared_sample_ids':len(pregame_samples&post_samples),
    'same_auth_can_access_both_projects':True,'strong_acl_blindness':False,'local_storage_inspection':'Not freshly instrumented; earlier read-only browser receipt exists. Do not claim browser-cache isolation.',
    'browser_receipt':str(EVAL/'reports/golden-set-v1-preparation.json'),
    'risk':'An admin/annotator can explicitly GET Postgame data and join by sample_id. Logical pregame payload isolation is verified, strong human access isolation is not.'}
save(EVAL/'reports/workbench-blindness-audit.json',results);print(json.dumps(results))
