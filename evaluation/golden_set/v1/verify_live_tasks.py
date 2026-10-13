"""Read back the actual local server payloads; no annotation submissions."""
import json
from setup_label_studio import request,HERE,read_json,save

projects=read_json(HERE/'label_studio_projects.json');report={}
for stage,p in projects.items():
    response=request('/api/tasks/?project='+str(p['project_id'])+'&page_size=100')
    tasks=response.get('tasks') or response.get('results') if isinstance(response,dict) else response
    assert len(tasks)==20
    expected={t['meta']['sample_id']:t for t in read_json(HERE/(stage+'_import.json'))}
    for task in tasks:
        sample=task['meta']['sample_id'];assert sample in expected
        assert task['data']==expected[sample]['data'] and task['meta']==expected[sample]['meta']
        assert task.get('annotations',[])==[] and task.get('predictions',[])==[]
        assert task['data']['model_output'] is None
    project=request('/api/projects/'+str(p['project_id'])+'/')
    report[stage]={'tasks_verified':20,'annotations':project['num_tasks_with_annotations'],'predictions':0,'data_binding_verified':True}
save(HERE/'live_task_verification.json',report);print(json.dumps(report))
