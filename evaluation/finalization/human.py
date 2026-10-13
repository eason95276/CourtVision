"""Blind final-article human workspace; zero automated labels or judge hints."""
import argparse,secrets,subprocess,time,urllib.request,urllib.error,os
from .common import *
from .services import listening
from .run import verify,ctx_for

HOME=ROOT/'evaluation/label_studio/runtime/historical_final_v1'
EXPORT=HERE/'human'
PORT=8686
def task_file():return EXPORT/'tasks-qualified.json' if (EXPORT/'tasks-qualified.json').exists() else EXPORT/'tasks.json'
def api(path,value=None):
    cred=read(HOME/'credentials.json')
    req=urllib.request.Request(f'http://127.0.0.1:{PORT}'+path,data=json.dumps(value).encode() if value is not None else None,
       headers={'Authorization':'Token '+cred['token'],'Content-Type':'application/json'})
    # Local SQLite export occasionally closes its streaming cursor. A single
    # retry is safe for this read-only endpoint; never retry annotation/import.
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req,timeout=20) as r:return json.load(r)
        except urllib.error.HTTPError as e:
            if value is not None or '/export?' not in path or e.code!=500 or attempt==1:raise
def start():
    HOME.mkdir(parents=True,exist_ok=True)
    if not (HOME/'credentials.json').exists():
        old=read(ROOT/'evaluation/label_studio/runtime/credentials.json')
        write(HOME/'credentials.json',{'username':old['username'],'password':old['password'],'token':secrets.token_hex(32)})
    if not listening(PORT):
        cred=read(HOME/'credentials.json');env=os.environ.copy();env.update(LABEL_STUDIO_DISABLE_TELEMETRY='true',SENTRY_DSN='')
        args=[str(ROOT/'evaluation/label_studio/.venv/Scripts/label-studio.exe'),'start','--no-browser','--internal-host','127.0.0.1','--port',str(PORT),'--data-dir',str(HOME),
             '--username',cred['username'],'--password',cred['password'],'--user-token',cred['token'],'--enable-legacy-api-token']
        with (HOME/'server.log').open('ab') as log:
            p=subprocess.Popen(args,cwd=ROOT/'evaluation',env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        write(HOME/('process-'+str(p.pid)+'.json'),{'pid':p.pid,'port':PORT,'created_at':now()})
    for _ in range(60):
        try:return api('/api/projects/')
        except (OSError,ValueError):time.sleep(1)
    raise ValueError('human_workspace_startup_timeout')
def tasks():
    out=[]
    for s in verify()['samples']:
        folder=BASE/'samples'/s['sample_id'];chain=read(folder/'chain-manifest.json')
        if chain['chain_status']!='SUCCEEDED':continue
        ctx=ctx_for(s);editorial=read(folder/'editorial/artifact.json')['parsed_output']
        text=editorial['title']+'\n\n赛前短评\n'+editorial['lead']+'\n\n'+editorial['article']
        for theme in editorial['watch_themes']:text+='\n\n'+theme['title']+'\n'+theme['statement']
        for p in editorial['player_analysis']:text+='\n\n'+p['player_name']+'\n'+p['statement']
        out.append({'data':{'sample_id':s['sample_id'],'stage_notice':'只评最终Editorial，无赛果、Forecast中间稿或Judge意见。请独立评分。',
           'context_summary':ctx['matchup']['away_team']+' @ '+ctx['matchup']['home_team']+' | '+ctx['matchup']['game_date'],
           'context_json':json.dumps(ctx,ensure_ascii=False,indent=2),'model_output_display':text},
           'meta':{'sample_id':s['sample_id'],'revision':REV,'dataset_revision':DATA,'context_hash':s['context_hash'],'editorial_hash':digest(editorial),'chain_hash':chain['manifest_hash']}})
    check_visible(out);return out
def prepare():
    EXPORT.mkdir(parents=True,exist_ok=True)
    if not (EXPORT/'tasks.json').exists():write(EXPORT/'tasks.json',tasks())
    projects=start();projects=projects.get('results',[]) if isinstance(projects,dict) else projects
    found=[p for p in projects if p['title']=='CourtVision Final V1 Historical Pregame Human']
    xml=(ROOT/'evaluation/golden_set/v1/pregame_config.xml').read_text(encoding='utf-8')
    project=found[0] if found else api('/api/projects/',{'title':'CourtVision Final V1 Historical Pregame Human','label_config':xml,'description':'Blind final Editorial; historical reconstruction; Judge is not Human.'})
    identity={'project_id':project['id'],'port':PORT,'url':f'http://127.0.0.1:{PORT}/projects/{project["id"]}/data'}
    if not (EXPORT/'workspace.json').exists():write(EXPORT/'workspace.json',identity)
    live=api(f'/api/projects/{project["id"]}/export?exportType=JSON&download_all_tasks=true')
    if not live:api(f'/api/projects/{project["id"]}/import',[read(task_file())[0]])
    live=api(f'/api/projects/{project["id"]}/export?exportType=JSON&download_all_tasks=true')
    if any(t.get('predictions') for t in live):raise ValueError('automatic_label_leakage')
    ready={'human_eval_ready':True,'prepared_articles':len(read(task_file())),'live_tasks':len(live),'annotations':sum(len(t.get('annotations',[])) for t in live),
       'workspace':identity,'rubric_path':'evaluation/golden_set/v1/rubric.json','protocol':'chronological blind, one new task after current annotation; intermediate Forecast and Judge labels excluded'}
    if not (EXPORT/'ready.json').exists():write(EXPORT/'ready.json',ready)
    print(json.dumps(ready));return ready
def export_labels():
    identity=read(EXPORT/'workspace.json');live=api(f'/api/projects/{identity["project_id"]}/export?exportType=JSON&download_all_tasks=true')
    expected={t['meta']['sample_id']:t for t in read(task_file())};labels=[]
    rubric=read(ROOT/'evaluation/golden_set/v1/rubric.json')['pregame']
    for t in live:
        meta=t.get('meta',{});sid=meta.get('sample_id')
        if sid not in expected or meta!=expected[sid]['meta'] or t['data']!=expected[sid]['data']:raise ValueError('human_binding_mismatch')
        for ann in t.get('annotations',[]):
            if ann.get('was_cancelled'):continue
            vals={r['from_name']:r.get('value',{}).get('choices',[None])[0] for r in ann['result'] if r.get('type')=='choices' and r.get('value',{}).get('choices')}
            if not set(rubric['hard_gates'])|set(rubric['soft_metrics'])<=set(vals):continue
            labels.append({'sample_id':sid,'revision':REV,'context_hash':meta['context_hash'],'editorial_hash':meta['editorial_hash'],'annotation_id':ann['id'],
               'annotator':ann.get('completed_by'),'hard_gates':{k:vals[k] for k in rubric['hard_gates']},'soft_metrics':{k:int(vals[k]) for k in rubric['soft_metrics']}})
    write(EXPORT/('human-export-'+now().replace(':','').replace('.','')+'.json'),{'raw_export':live,'labels':labels,'raw_export_hash':digest(live),'created_at':now()})
    return live,labels
def advance():
    live,labels=export_labels()
    if not labels:raise ValueError('submit_real_human_annotation_first')
    annotated={r['sample_id'] for r in labels};existing={t['meta']['sample_id'] for t in live}
    if not existing<=annotated:raise ValueError('current_task_not_annotated')
    pending=[t for t in read(task_file()) if t['meta']['sample_id'] not in existing]
    if pending:
        pid=read(EXPORT/'workspace.json')['project_id'];api(f'/api/projects/{pid}/import',[pending[0]])
    print(json.dumps({'next_task_opened':bool(pending),'completed_articles':len(annotated)}))
def calibration():
    _,labels=export_labels();pairs=[]
    for h in labels:
        p=BASE/'judge-final-v1'/h['sample_id']/'artifact.json'
        recovery_index=BASE/'judge-final-v1-provider-recovery-01/index.json'
        if recovery_index.exists():
            matched=[r for r in read(recovery_index)['rows'] if r['sample_id']==h['sample_id'] and r['accepted']]
            if matched:
                p=ROOT/matched[0]['artifact_path']
                if digest(p.read_bytes())!=matched[0]['artifact_file_hash']:raise ValueError('judge_artifact_binding_changed')
        if not p.exists():continue
        j=read(p)
        if j['raw_schema_status']!='PASSED' or j['editorial_hash']!=h['editorial_hash'] or j['context_hash']!=h['context_hash']:continue
        pred=j['parsed_output'];g=[h['hard_gates'][k]==v['value'] for k,v in pred['hard_gates'].items()];scores=[(h['soft_metrics'][k],v['score']) for k,v in pred['soft_metrics'].items()]
        pairs.append({'sample_id':h['sample_id'],'hard_gate_agreement':sum(g)/len(g),'soft_exact':sum(a==b for a,b in scores)/len(scores),'soft_within1':sum(abs(a-b)<=1 for a,b in scores)/len(scores),
           'per_metric':{k:{'human':h['soft_metrics'][k],'judge':v['score'],'exact':h['soft_metrics'][k]==v['score'],'within1':abs(h['soft_metrics'][k]-v['score'])<=1} for k,v in pred['soft_metrics'].items()}})
    result={'status':'COMPUTED' if pairs else 'PENDING_REAL_HUMAN_LABELS','n_pairs':len(pairs),'pairs':pairs,'human_human_IAA':None,'kappa':None,'kappa_reason':'No sufficiently sized independent categorical/ordinal label cohort yet; one annotator is not Human-Human IAA.'}
    write(EXPORT/('calibration-'+now().replace(':','').replace('.','')+'.json'),result);print(json.dumps(result));return result
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','advance','calibrate']);a=p.parse_args()
    {'prepare':prepare,'advance':advance,'calibrate':calibration}[a.command]()
