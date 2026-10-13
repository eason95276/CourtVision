"""One active case per isolated LS instance; next requires an immutable human freeze."""
import argparse,copy,json,os,secrets,subprocess,sys,time,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import run as r
from workspace import clean_pregame_task,listening,stop_owned
from first_sample_workspace import context_summary,_normalize_task
from control import exclusive,now
from asset_scanner import EVAL,read_json,canonical_hash,sha

PORT=8684
HOME=EVAL/'label_studio/runtime/candidate_v2'
LEDGER=EVAL/'execution/candidates'/r.REV/'human'

def current():
    samples=r.b.selection()
    for s in samples:
        folder=LEDGER/s['sample_id'];path=folder/'freeze.json'
        if not path.exists():return s
        receipt=read_json(path)
        if receipt.get('sample_id')!=s['sample_id'] or receipt.get('freeze_hash')!=canonical_hash({k:v for k,v in receipt.items() if k!='freeze_hash'}):raise ValueError('human freeze corrupt')
        for name,key in [('annotation-export.json','annotation_export_sha256'),('normalized-labels.json','labels_sha256')]:
            if sha(folder/name)!=receipt[key]:raise ValueError('frozen human artifact changed')
    raise ValueError('all 20 frozen; postgame remains closed')

def load(sample):
    folder=r.BASE/'samples'/sample['sample_id'];m=read_json(folder/'chain-manifest.json')
    if m['manifest_hash']!=canonical_hash({k:v for k,v in m.items() if k!='manifest_hash'}):raise ValueError('manifest corruption')
    if m['sample_id']!=sample['sample_id'] or m['context_hash']!=sample['context_sha256'] or m['system_revision']!=r.REV:raise ValueError('binding mismatch')
    for ref in m['artifact_refs']:
        if sha(r.ROOT/ref['path'])!=ref['sha256']:raise ValueError('component artifact changed')
    ctx=r.b.context_for(sample);editorial=None
    if m['chain_status']=='SUCCEEDED':
        art=read_json(folder/'editorial/artifact.json');editorial=art['parsed_output']
        if art['artifact_hash']!=canonical_hash({k:v for k,v in art.items() if k!='artifact_hash'}):raise ValueError('writer artifact corruption')
        req=read_json(folder/'editorial/request.json')['request'];actual=json.loads(req['messages'][1]['content'])
        accepted=read_json(folder/'accepted-forecast.json')
        if actual!={'closed_book_context':ctx,'locked_forecast':accepted['forecast'],'evidence_catalog':r.pointers(ctx)}:raise ValueError('writer input contamination')
        raw=json.loads(art['raw_response'])['choices'][0]['message']['content']
        if json.loads(raw)!=editorial:raise ValueError('not original editorial')
    return m,ctx,editorial

def task_for(sample):
    m,ctx,editorial=load(sample);success=editorial is not None
    data={'sample_id':sample['sample_id'],'stage':'pregame','context':copy.deepcopy(ctx),'context_json':json.dumps(ctx,ensure_ascii=False,indent=2),
        'context_summary':context_summary(ctx),'model_output_status':'accepted' if success else 'NO_CONTENT',
        'model_output_display':json.dumps(editorial,ensure_ascii=False,indent=2) if success else 'EXECUTION_FAILURE / NO_CONTENT。Soft Metrics = NA。请只确认已阅读执行失败状态，不给内容评分。',
        'stage_notice':'只评价当前最终Editorial；所有控件无默认值。' if success else '当前没有可评价文章。Postgame仍关闭。'}
    if success:data['model_output']=editorial
    meta={'sample_id':sample['sample_id'],'stage':'pregame','dataset_revision':r.b.DATASET_ID,'golden_set_revision':r.b.GOLDEN_ID,
        'generation_revision':r.REV,'context_hash':sample['context_sha256'],'chain_manifest_hash':m['manifest_hash'],
        'rubric_revision':'human-rubric-v1-'+canonical_hash(read_json(EVAL/'golden_set/v1/rubric.json'))[:20],
        'content_status':'PENDING_HUMAN' if success else 'NA','editorial_output_hash':canonical_hash(editorial) if success else None}
    return clean_pregame_task({'data':data,'meta':meta})

def xml_for(sample):
    if load(sample)[2] is not None:return (EVAL/'golden_set/v1/pregame_config.xml').read_text(encoding='utf-8')
    return '<View><Header value="Execution failure / No content"/><Text name="notice" value="$model_output_display"/><Choices name="execution_ack" toName="notice" choice="single-radio" required="true"><Choice value="EXECUTION_FAILURE_NO_CONTENT"/></Choices></View>'

def api(sample,route,body=None):
    creds=read_json(HOME/sample['sample_id']/'credentials.json')
    req=urllib.request.Request(f'http://127.0.0.1:{PORT}'+route,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,
        headers={'Authorization':'Token '+creds['token'],'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=30) as resp:return json.loads(resp.read())

def open_current():
    r.verify();sample=current();wanted=task_for(sample);folder=HOME/sample['sample_id'];ledger=LEDGER/sample['sample_id']
    # Never touch Phoenix or production. Verify ownership of this port before stopping a prior isolated case.
    if listening(PORT):stop_owned(PORT,HOME)
    folder.mkdir(parents=True,exist_ok=True)
    cp=folder/'credentials.json'
    if not cp.exists():
        creds=read_json(EVAL/'label_studio/runtime/credentials.json');exclusive(cp,{'username':creds['username'],'password':creds['password'],'token':secrets.token_hex(32)})
    creds=read_json(cp);env=os.environ.copy();env.update(LABEL_STUDIO_DISABLE_TELEMETRY='true',SENTRY_DSN='')
    cmd=[str(EVAL/'label_studio/.venv/Scripts/label-studio.exe'),'start','--no-browser','--internal-host','127.0.0.1','--port',str(PORT),'--data-dir',str(folder),
        '--username',creds['username'],'--password',creds['password'],'--user-token',creds['token'],'--enable-legacy-api-token']
    with (folder/'server.log').open('ab') as log:
        process=subprocess.Popen(cmd,cwd=EVAL,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    for _ in range(60):
        try:projects=api(sample,'/api/projects/');break
        except (OSError,ValueError):
            if process.poll() is not None:raise ValueError('Label Studio startup failed')
            time.sleep(1)
    else:raise ValueError('workspace startup timeout')
    projects=projects.get('results',[]) if isinstance(projects,dict) else projects
    title='CourtVision Candidate V2 · '+sample['sample_id'];xml=xml_for(sample)
    if len(projects)>1 or any(p['title']!=title for p in projects):raise ValueError('unexpected project; do not expose')
    project=projects[0] if projects else api(sample,'/api/projects/',{'title':title,'label_config':xml,'description':'One chronological case. No Forecast, Ground Truth or future outputs.'})
    project=api(sample,f"/api/projects/{project['id']}/")
    if project['label_config'].strip()!=xml.strip():raise ValueError('rubric drift')
    if project['task_number']==0:
        if (ledger/'identity.json').exists():raise ValueError('previously bound task disappeared')
        api(sample,f"/api/projects/{project['id']}/import",[wanted])
    tasks=api(sample,f"/api/projects/{project['id']}/export?exportType=JSON&download_all_tasks=true")
    if len(tasks)!=1 or tasks[0]['data']!=wanted['data'] or tasks[0]['meta']!=wanted['meta'] or tasks[0].get('predictions'):raise ValueError('one-case import mismatch')
    identity={'sample_id':sample['sample_id'],'project_id':project['id'],'task_id':tasks[0]['id'],'task_hash':canonical_hash(wanted),'xml_hash':canonical_hash(xml),
        'url':f"http://127.0.0.1:{PORT}/projects/{project['id']}/data",'visible_tasks':1,'future_tasks':0,'postgame_open':False,'model_calls':0}
    if (ledger/'identity.json').exists():
        if read_json(ledger/'identity.json')!=identity:raise ValueError('workspace binding changed')
    else:
        if tasks[0].get('annotations'):raise ValueError('unexpected existing human labels')
        exclusive(ledger/'identity.json',identity)
    return identity

def freeze_current(actor):
    sample=current();ledger=LEDGER/sample['sample_id'];ident=read_json(ledger/'identity.json');wanted=task_for(sample)
    if canonical_hash(wanted)!=ident['task_hash']:raise ValueError('task binding changed')
    project=api(sample,f"/api/projects/{ident['project_id']}/")
    if canonical_hash(project['label_config'])!=ident['xml_hash']:raise ValueError('rubric changed')
    tasks=api(sample,f"/api/projects/{ident['project_id']}/export?exportType=JSON&download_all_tasks=true")
    if len(tasks)!=1 or tasks[0]['id']!=ident['task_id'] or tasks[0]['data']!=wanted['data'] or tasks[0]['meta']!=wanted['meta']:raise ValueError('freeze task mismatch')
    annotations=[a for a in tasks[0].get('annotations',[]) if not a.get('was_cancelled')]
    if not annotations:raise ValueError('real completed human annotation required before next')
    if load(sample)[2] is not None:
        labels,disagreements=_normalize_task(tasks[0],read_json(EVAL/'golden_set/v1/rubric.json')['pregame'])
        # Reused normalizer contains a legacy constant; rebind output identity from the verified live task, not another sample.
        for label in labels:label['sample_id']=sample['sample_id']
        for item in disagreements:item['sample_id']=sample['sample_id']
    else:
        for a in annotations:
            if not a.get('id') or not a.get('completed_by') or not a.get('created_at') or len(a.get('result',[]))!=1 or a['result'][0].get('from_name')!='execution_ack' or a['result'][0].get('value',{}).get('choices')!=['EXECUTION_FAILURE_NO_CONTENT']:raise ValueError('real failure acknowledgment required')
        labels=[{'sample_id':sample['sample_id'],'status':'EXECUTION_FAILURE','content':'NO_CONTENT','soft_metrics':None}];disagreements=[]
    exclusive(ledger/'annotation-export.json',tasks);exclusive(ledger/'normalized-labels.json',{'labels':labels,'disagreements':disagreements})
    receipt={'sample_id':sample['sample_id'],'created_at':now(),'actor':actor,'identity_hash':canonical_hash(ident),
        'annotation_export_sha256':sha(ledger/'annotation-export.json'),'labels_sha256':sha(ledger/'normalized-labels.json'),'postgame_open':False}
    receipt['freeze_hash']=canonical_hash(receipt);exclusive(ledger/'freeze.json',receipt)
    stop_owned(PORT,HOME)
    return {'frozen':sample['sample_id'],'next_requires_explicit_open':True}

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['open','freeze','status']);p.add_argument('--actor',default='local-human-operator');a=p.parse_args()
    value=open_current() if a.command=='open' else freeze_current(a.actor) if a.command=='freeze' else {'current':current()['sample_id'],'frozen_count':len(list(LEDGER.glob('*/freeze.json'))),'postgame_open':False}
    print(json.dumps(value,ensure_ascii=True))
if __name__=='__main__':main()
