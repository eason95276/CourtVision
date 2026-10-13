import json,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
creds=json.loads((ROOT/'label_studio/runtime/credentials.json').read_text())
def request(path,body=None):
    req=urllib.request.Request('http://127.0.0.1:8681'+path,data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':'Token '+creds['token'],'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=30) as response:return json.loads(response.read())
project=request('/api/projects/',{'title':'CourtVision environment smoke - no labels',
    'label_config':'<View><Text name="output" value="$actual_output"/></View>',
    'description':'Read-only text preview, no final labeling config or annotations.'})
tasks=json.loads((ROOT/'label_studio/smoke-import.json').read_text(encoding='utf-8'))
imported=request('/api/projects/'+str(project['id'])+'/import',tasks)
current=request('/api/projects/'+str(project['id'])+'/')
receipt={'project_id':project['id'],'imported':imported,'task_count':current['task_number'],
    'annotations':current['num_tasks_with_annotations'],'labeling_decisions':0}
(ROOT/'reports/label-studio-smoke.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8');print(json.dumps(receipt))
