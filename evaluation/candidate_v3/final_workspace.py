"""Frozen single-revision target and chronological one-task LS workspace."""
import argparse,copy,json,os,secrets,subprocess,sys,time,urllib.request,urllib.error,http.cookiejar,urllib.parse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v3 import analysis_tools as a
from control import exclusive,now
from asset_scanner import EVAL,ROOT,read_json,sha,canonical_hash
from workspace import clean_pregame_task,listening,stop_owned
from first_sample_workspace import context_summary,_normalize_task

HOME=EVAL/'label_studio/runtime/human_final'
EXPORT=EVAL/'human_evaluation/final'
PORT=8684
TITLE='CourtVision Pregame Golden Set V1 Human Evaluation'

def readable(editorial):
    text=editorial['title']+'\n\n赛前短评\n'+editorial['lead']+'\n\n完整赛前文章\n'+editorial['article']
    if editorial['watch_themes']:text+='\n\n看点\n'+'\n\n'.join(x['title']+'\n'+x['statement'] for x in editorial['watch_themes'])
    if editorial['player_analysis']:text+='\n\n球员分析\n'+'\n\n'.join(x['player_name']+'\n'+x['statement'] for x in editorial['player_analysis'])
    return text+'\n\nWriter confidence: '+editorial['confidence']

def build_task(row):
    success=row['record']['chain_status'] in ('SUCCEEDED','ACCEPTED');ctx=row['context'];sample=row['sample']
    data={'sample_id':sample['sample_id'],'stage':'pregame','context':copy.deepcopy(ctx),'context_json':json.dumps(ctx,ensure_ascii=False,indent=2),
        'context_summary':context_summary(ctx),'model_output_status':'accepted' if success else 'NO_CONTENT',
        'model_output_display':readable(row['editorial']) if success else 'Execution Failure / No Content。没有文章可评，Soft Metrics=NA。请只确认已读执行失败状态。',
        'stage_notice':'当前仅这一场。请评价最终Editorial；所有评分无默认值。Postgame关闭。'}
    if success:data['model_output']=copy.deepcopy(row['editorial'])
    task={'data':data,'meta':{'sample_id':sample['sample_id'],'stage':'pregame','dataset_revision':a.v2.b.DATASET_ID,'golden_set_revision':a.v2.b.GOLDEN_ID,
        'generation_revision':row['revision'],'context_hash':sample['context_sha256'],'chain_manifest_hash':row['record'].get('manifest_hash',row['record'].get('record_hash')),
        'content_quality_status':'PENDING_HUMAN' if success else 'NA','editorial_output_hash':canonical_hash(row['editorial']) if success else None,
        'rubric_revision':'human-rubric-v1-'+canonical_hash(read_json(EVAL/'golden_set/v1/rubric.json'))[:20]}}
    return clean_pregame_task(task)

def freeze_target():
    if (EXPORT/'target_manifest.json').exists():return target()
    candidates=[]
    for revision in a.REVISIONS[1:]:
        if not all((a.folder_for(revision,s)/'chain-manifest.json').exists() for s in a.v2.b.selection()):continue
        summary=a.summarize(revision);candidates.append(summary)
    if not candidates:raise ValueError('no complete candidate revision')
    def critical_false_rejections(item):
        count=0
        for sample in a.v2.b.selection():
            row=a.load(item['revision'],sample);err=(row['initial']['artifact'].get('error') or{}).get('message','')
            # Critical correctness regression: checker falsely rejects a legal numeric tick.
            # General provider/parse failures are still reported as E2E regressions, not hidden.
            if 'is not a multiple of 0.01' in err:
                try:a.v2.fw.ForecastNumbers.model_validate(row['initial']['parsed']['prediction']);count+=1
                except (ValueError,KeyError,TypeError):pass
        return count
    def rank(item):
        successes={r['sample_id'] for r in item['rows'] if r['chain_status'] in ('SUCCEEDED','ACCEPTED')}
        return (len(successes),-critical_false_rejections(item),int(item['revision'].endswith('-v4')),item['revision'])
    chosen=max(candidates,key=rank);rows=[a.load(chosen['revision'],s) for s in a.v2.b.selection()];tasks=[build_task(row) for row in rows]
    xml=(EVAL/'golden_set/v1/pregame_config.xml').read_text(encoding='utf-8')
    EXPORT.mkdir(parents=True,exist_ok=True)
    # Display text only; definitions, control names/options/required flags stay byte-identical.
    xml=xml.replace('value="Model Output"','value="最终赛前文章 · 请先读正文，再评分"')
    with (EXPORT/'pregame_config.xml').open('x',encoding='utf-8') as f:f.write(xml)
    exclusive(EXPORT/'human_eval_tasks.json',tasks)
    manifest={'created_at':now(),'target_revision':chosen['revision'],'selection_policy':'E2E count, fewer proven critical correctness false-rejections of legal numeric ticks, decimal-correct contract tie-break. All E2E/schema regressions remain reported separately. No article appearance used.',
        'critical_numeric_false_rejections':{x['revision']:critical_false_rejections(x) for x in candidates},
        'candidate_execution_counts':{x['revision']:x['metrics']['e2e_success'] for x in candidates},'dataset_revision':a.v2.b.DATASET_ID,'golden_set_revision':a.v2.b.GOLDEN_ID,
        'annotation_revision':'human-pregame-final-v1','n':20,'successes':sum(r['record']['chain_status']=='SUCCEEDED' for r in rows),
        'task_file_hash':sha(EXPORT/'human_eval_tasks.json'),'xml_hash':sha(EXPORT/'pregame_config.xml'),'rubric_hash':sha(EVAL/'golden_set/v1/rubric.json'),
        'rows':[{'sample_id':r['sample']['sample_id'],'game_date':r['sample']['game_date'],'status':'SUCCESS' if r['editorial'] is not None else 'EXECUTION_FAILURE',
            'content_quality':'PENDING_HUMAN' if r['editorial'] is not None else 'NA','task_hash':canonical_hash(task),
            'record_path':str((a.folder_for(chosen['revision'],r['sample'])/'chain-manifest.json').relative_to(ROOT)),
            'record_hash':sha(a.folder_for(chosen['revision'],r['sample'])/'chain-manifest.json'),'failure_stage':r['record']['failure_stage'],'failure_type':r['record']['failure_type'],
            'artifact_refs':r['record']['artifact_refs'],'model_config':a.read_json(a.folder_for(chosen['revision'],r['sample'])/'forecast/artifact.json')['config']} for r,task in zip(rows,tasks)],
        'postgame_open':False,'mixed_revisions':False,'human_labels_created':0,'limits':'One current task in normal UI, not an OS-admin sandbox; export file contains20 outputs, not imported together.'}
    manifest['manifest_hash']=canonical_hash(manifest);exclusive(EXPORT/'target_manifest.json',manifest);return manifest

def target():
    m=read_json(EXPORT/'target_manifest.json')
    if m['manifest_hash']!=canonical_hash({k:v for k,v in m.items() if k!='manifest_hash'}) or sha(EXPORT/'human_eval_tasks.json')!=m['task_file_hash'] or sha(EXPORT/'pregame_config.xml')!=m['xml_hash'] or sha(EVAL/'golden_set/v1/rubric.json')!=m['rubric_hash']:raise ValueError('human target drift')
    return m

def current():
    for row in target()['rows']:
        folder=EXPORT/'annotations'/row['sample_id'];receiptpath=folder/'freeze.json'
        if not receiptpath.exists():return next(s for s in a.v2.b.selection() if s['sample_id']==row['sample_id'])
        receipt=read_json(receiptpath)
        if receipt['freeze_hash']!=canonical_hash({k:v for k,v in receipt.items() if k!='freeze_hash'}) or receipt['sample_id']!=row['sample_id'] or receipt['target_manifest_hash']!=target()['manifest_hash']:raise ValueError('human freeze tamper')
        for name,key in [('annotation-export.json','export_hash'),('normalized-labels.json','labels_hash')]:
            if sha(folder/name)!=receipt[key]:raise ValueError('human frozen labels changed')
    raise ValueError('all20 frozen; Postgame still closed')

def task_for(sample):
    tasks=read_json(EXPORT/'human_eval_tasks.json');task=next(t for t in tasks if t['data']['sample_id']==sample['sample_id']);row=a.load(target()['target_revision'],sample)
    if build_task(row)!=task:raise ValueError('frozen task no longer matches chosen revision')
    return task

def xml_for(sample):
    if task_for(sample)['data']['model_output_status']=='accepted':return (EXPORT/'pregame_config.xml').read_text(encoding='utf-8')
    return '<View><Header value="Execution Failure / No Content"/><Text name="notice" value="$model_output_display"/><Choices name="execution_failure" toName="notice" choice="single-radio" required="true"><Choice value="execution_failure"/></Choices></View>'

def case_folder(sample):return HOME/target()['target_revision']/sample['sample_id']
def api(sample,route,body=None):
    creds=read_json(case_folder(sample)/'credentials.json');req=urllib.request.Request(f'http://127.0.0.1:{PORT}'+route,
        data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers={'Authorization':'Token '+creds['token'],'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=30) as resp:return json.loads(resp.read())

def open_current():
    sample=current();wanted=task_for(sample);folder=case_folder(sample);ledger=EXPORT/'annotations'/sample['sample_id']
    if listening(PORT):stop_owned(PORT,EVAL/'label_studio/runtime')
    folder.mkdir(parents=True,exist_ok=True)
    if not (folder/'credentials.json').exists():
        old=read_json(EVAL/'label_studio/runtime/credentials.json');exclusive(folder/'credentials.json',{'username':old['username'],'password':old['password'],'token':secrets.token_hex(32)})
    creds=read_json(folder/'credentials.json');env=os.environ.copy();env.update(LABEL_STUDIO_DISABLE_TELEMETRY='true',SENTRY_DSN='')
    cmd=[str(EVAL/'label_studio/.venv/Scripts/label-studio.exe'),'start','--no-browser','--internal-host','127.0.0.1','--port',str(PORT),'--data-dir',str(folder),
        '--username',creds['username'],'--password',creds['password'],'--user-token',creds['token'],'--enable-legacy-api-token']
    with (folder/'server.log').open('ab') as log:process=subprocess.Popen(cmd,cwd=EVAL,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    for _ in range(60):
        try:projects=api(sample,'/api/projects/');break
        except (OSError,ValueError):
            if process.poll() is not None:raise ValueError('LS startup failed')
            time.sleep(1)
    else:raise ValueError('LS startup timeout')
    projects=projects.get('results',[]) if isinstance(projects,dict) else projects
    if len(projects)>1 or any(p['title']!=TITLE for p in projects):raise ValueError('unexpected project')
    project=projects[0] if projects else api(sample,'/api/projects/',{'title':TITLE,'label_config':xml_for(sample),'description':'One chronological sample, single frozen revision, no Forecast/GT/Postgame.'})
    project=api(sample,f"/api/projects/{project['id']}/")
    if project['label_config'].strip()!=xml_for(sample).strip():raise ValueError('rubric drift')
    if project['task_number']==0:
        if (ledger/'identity.json').exists():raise ValueError('bound task disappeared')
        api(sample,f"/api/projects/{project['id']}/import",[wanted])
    tasks=api(sample,f"/api/projects/{project['id']}/export?exportType=JSON&download_all_tasks=true")
    if len(tasks)!=1 or tasks[0]['data']!=wanted['data'] or tasks[0]['meta']!=wanted['meta'] or tasks[0].get('predictions'):raise ValueError('single-task binding mismatch')
    ident={'sample_id':sample['sample_id'],'revision':target()['target_revision'],'project_id':project['id'],'task_id':tasks[0]['id'],'task_hash':canonical_hash(wanted),
        'xml_hash':canonical_hash(project['label_config']),'url':f"http://127.0.0.1:{PORT}/projects/{project['id']}/data",'visible_tasks':1,'future_tasks':0,'postgame_open':False}
    if (ledger/'identity.json').exists():
        if read_json(ledger/'identity.json')!=ident:raise ValueError('identity changed')
    else:
        if tasks[0].get('annotations'):raise ValueError('unexpected labels before opening')
        exclusive(ledger/'identity.json',ident)
    return ident

def freeze_current(actor):
    sample=current();ledger=EXPORT/'annotations'/sample['sample_id'];ident=read_json(ledger/'identity.json');wanted=task_for(sample)
    tasks=api(sample,f"/api/projects/{ident['project_id']}/export?exportType=JSON&download_all_tasks=true")
    project=api(sample,f"/api/projects/{ident['project_id']}/")
    if canonical_hash(project['label_config'])!=ident['xml_hash'] or len(tasks)!=1 or tasks[0]['data']!=wanted['data'] or tasks[0]['meta']!=wanted['meta'] or tasks[0]['id']!=ident['task_id']:raise ValueError('freeze binding mismatch')
    anns=[x for x in tasks[0].get('annotations',[]) if not x.get('was_cancelled')]
    if not anns:raise ValueError('real human annotation required before next')
    if wanted['data']['model_output_status']=='accepted':
        labels,disagreements=_normalize_task(tasks[0],read_json(EVAL/'golden_set/v1/rubric.json')['pregame'])
        for row in labels:row['sample_id']=sample['sample_id']
        for row in disagreements:row['sample_id']=sample['sample_id']
    else:
        for ann in anns:
            if not ann.get('id') or not ann.get('created_at') or not ann.get('completed_by') or len(ann.get('result',[]))!=1 or ann['result'][0].get('from_name')!='execution_failure' or ann['result'][0].get('value',{}).get('choices')!=['execution_failure']:raise ValueError('real failure acknowledgment required')
        labels=[{'sample_id':sample['sample_id'],'execution_status':'EXECUTION_FAILURE','content':'NO_CONTENT','soft_metrics':None}];disagreements=[]
    exclusive(ledger/'annotation-export.json',tasks);exclusive(ledger/'normalized-labels.json',{'labels':labels,'disagreements':disagreements})
    receipt={'sample_id':sample['sample_id'],'created_at':now(),'actor':actor,'target_manifest_hash':target()['manifest_hash'],
        'export_hash':sha(ledger/'annotation-export.json'),'labels_hash':sha(ledger/'normalized-labels.json'),'postgame_open':False}
    receipt['freeze_hash']=canonical_hash(receipt);exclusive(ledger/'freeze.json',receipt);stop_owned(PORT,HOME)
    return {'frozen':sample['sample_id'],'next_requires_open':True}

def browser_state():
    sample=current();folder=case_folder(sample);creds=read_json(folder/'credentials.json');jar=http.cookiejar.CookieJar();opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar));url=f'http://127.0.0.1:{PORT}/user/login/'
    with opener.open(url,timeout=30) as resp:resp.read()
    csrf=next(c.value for c in jar if c.name=='csrftoken');body=urllib.parse.urlencode({'email':creds['username'],'password':creds['password'],'csrfmiddlewaretoken':csrf}).encode()
    with opener.open(urllib.request.Request(url,data=body,headers={'Referer':url}),timeout=30) as resp:resp.read()
    if not any(c.name=='sessionid' for c in jar):raise ValueError('local browser login failed')
    state={'cookies':[{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path,'expires':c.expires or -1,'httpOnly':c.name=='sessionid','secure':c.secure,'sameSite':'Lax'} for c in jar],'origins':[]}
    with (folder/'browser-state.json').open('w',encoding='utf-8') as f:json.dump(state,f)
    return {'private_state_path':str(folder/'browser-state.json'),'annotations_submitted':0}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','open','freeze','status','browser']);p.add_argument('--actor',default='local-human');args=p.parse_args()
    value=freeze_target() if args.command=='prepare' else open_current() if args.command=='open' else freeze_current(args.actor) if args.command=='freeze' else browser_state() if args.command=='browser' else {'current':current()['sample_id'],'target':target()['target_revision'],'postgame_open':False}
    if args.command=='prepare':value={k:value[k] for k in ('target_revision','n','successes','manifest_hash')}
    print(json.dumps(value,ensure_ascii=True))
