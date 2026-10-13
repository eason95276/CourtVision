"""Separate-instance Label Studio workflow. No Postgame data in Pregame DB.

Local machine owner remains able to inspect files; that is outside normal
annotator workflow. Legacy shared instance is archived, not deleted.
"""
import argparse, copy, json, os, secrets, socket, subprocess, time, urllib.request
from pathlib import Path
from control import Control, exclusive, now
from runner import GOLD, EVAL, HERE, samples, accepted_artifact
from asset_scanner import read_json, canonical_hash

PORTS = {'pregame':8682,'postgame':8683}
RUNTIME = EVAL/'label_studio/runtime'


def api(stage, route, body=None, method=None):
    credentials = read_json(RUNTIME/stage/'credentials.json')
    request = urllib.request.Request(f'http://127.0.0.1:{PORTS[stage]}'+route,
        data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,
        headers={'Authorization':'Token '+credentials['token'],'Content-Type':'application/json'},method=method)
    with urllib.request.urlopen(request,timeout=30) as response:
        return json.loads(response.read()) if response.status!=204 else None


def listening(port):
    with socket.socket() as sock:
        return sock.connect_ex(('127.0.0.1',port))==0


def stop_owned(port, directory):
    """Check actual listener command path before stopping; no global Python kill."""
    if not listening(port):
        return
    path = str(Path(directory).resolve()).replace("'","''")
    script = f"""$listener=Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction Stop;
$owner=Get-CimInstance Win32_Process -Filter ('ProcessId='+$listener[0].OwningProcess);
if (-not $owner.CommandLine.Contains('{path}') -or -not $owner.CommandLine.Contains('label')) {{ throw 'Unverified listener ownership' }};
Stop-Process -Id $owner.ProcessId -ErrorAction Stop"""
    result = subprocess.run(['powershell','-NoProfile','-Command',script],capture_output=True,text=True)
    if result.returncode:
        raise ValueError('cannot safely stop verified evaluation listener')


def clean_pregame_task(task):
    task = copy.deepcopy(task)
    forbidden = {'ground_truth','hidden_outcome_bucket','actual_margin','actual_total','postgame_evidence',
                 'selection_bucket','target_boxscore','target_minutes','target_dnp'}
    def check(value):
        if isinstance(value,dict):
            if forbidden & set(value):
                raise ValueError('Pregame workspace Ground Truth/hidden metadata')
            for child in value.values():check(child)
        elif isinstance(value,list):
            for child in value:check(child)
    check(task)
    if task.get('annotations') or task.get('predictions'):
        raise ValueError('imports must not contain labels')
    return task


def stage_tasks(stage, control, with_outputs=False):
    state = control.state()
    bysample = {s['sample_id']:s for s in samples()}
    tasks = read_json(GOLD/(stage+'_import.json'))
    if stage=='postgame':control.postgame_gate('workspace-builder')
    for task in tasks:
        data = task['data'];sample = bysample[data['sample_id']]
        if canonical_hash(data['context']) != sample['context_sha256']:
            raise ValueError('workspace Context differs from frozen sample')
        if stage=='postgame':
            pre = accepted_artifact(control,state['pre_revision'],sample)
            data['frozen_pregame_prediction']=pre['parsed_output']['final_prediction']
            data['important_assumptions']=pre['parsed_output']['important_assumptions']
            data['pregame_basis_display']=json.dumps(pre['parsed_output'],ensure_ascii=False,indent=2)
        if with_outputs:
            revision = state['pre_revision' if stage=='pregame' else 'post_revision']
            if stage=='pregame':
                artifact=accepted_artifact(control,revision,sample)
            else:
                row=control.accepted(revision,stage,sample['sample_id'])
                path=HERE/row['artifact']
                from asset_scanner import sha
                if sha(path)!=row['artifact_hash']:raise ValueError('Postgame artifact changed')
                artifact=read_json(path)
            data['model_output']=artifact['parsed_output']
            data['model_output_status']='accepted'
            data['model_output_display']=json.dumps(artifact['parsed_output'],ensure_ascii=False,indent=2)
            task['meta']['generation_revision']=revision
            task['meta']['accepted_request_hash']=artifact['request_hash']
        if stage=='pregame':clean_pregame_task(task)
    return tasks


def start(stage, control, actor, override_reason=None):
    first_profile=HERE/'profiles/golden-pregame-production-two-stage-v1/profile.json'
    if stage=='pregame' and (control.workspace('pregame_single') or first_profile.exists()):
        raise ValueError('Chronological single-sample scope active; batch workspace cannot reopen')
    state=control.state()
    if stage=='pregame' and state['phase'] not in ('PREPARED','PREGAME_GENERATED','PREGAME_ANNOTATING'):
        raise ValueError('frozen Pregame workspace cannot reopen silently')
    if stage=='postgame':control.postgame_gate(actor,override_reason)
    if stage=='postgame' and state['phase']=='POSTGAME_FROZEN':
        raise ValueError('frozen Postgame workspace needs explicit annotation revision')
    # Shut the known shared instance, retaining its original database and projects.
    stop_owned(8681,RUNTIME)
    folder=RUNTIME/stage;folder.mkdir(parents=True,exist_ok=True)
    cred=folder/'credentials.json'
    if not cred.exists():
        original=read_json(RUNTIME/'credentials.json')
        exclusive(cred,{'username':original['username'],'password':original['password'],'token':secrets.token_hex(32)})
    if not listening(PORTS[stage]):
        c=read_json(cred);env=os.environ.copy()
        env.update(LABEL_STUDIO_DISABLE_TELEMETRY='true',SENTRY_DSN='')
        cmd=[str(EVAL/'label_studio/.venv/Scripts/label-studio.exe'),'start','--no-browser',
             '--internal-host','127.0.0.1','--port',str(PORTS[stage]),'--data-dir',str(folder),
             '--username',c['username'],'--password',c['password'],'--user-token',c['token'],'--enable-legacy-api-token']
        with (folder/'workbench.log').open('ab') as log:
            process=subprocess.Popen(cmd,cwd=EVAL,env=env,stdout=log,stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        (folder/'process.json').write_text(json.dumps({'pid':process.pid,'port':PORTS[stage],'started_at':now()}))
        for _ in range(60):
            if listening(PORTS[stage]):break
            if process.poll() is not None:raise ValueError('Label Studio startup failed; inspect private workbench log')
            time.sleep(1)
        else:raise ValueError('Label Studio startup timeout')
    # Wait until authenticated API is ready, not merely until socket opens.
    for _ in range(20):
        try:
            response=api(stage,'/api/projects/');break
        except (OSError,ValueError):time.sleep(1)
    else:raise ValueError('workspace API unavailable')
    projects=response.get('results',[]) if isinstance(response,dict) else response
    title='CourtVision Isolated '+stage.capitalize()+' Golden V1'
    if len(projects)>1 or any(p['title']!=title for p in projects):
        raise ValueError('unexpected project in isolated instance')
    project=projects[0] if projects else api(stage,'/api/projects/',{'title':title,
        'label_config':(GOLD/(stage+'_config.xml')).read_text(encoding='utf-8'),'description':'Isolated closed-book workspace. Await generation before annotation.'})
    pid=project['id'];project=api(stage,f'/api/projects/{pid}/')
    if project['label_config'].strip()!=(GOLD/(stage+'_config.xml')).read_text(encoding='utf-8').strip():
        raise ValueError('Rubric UI drift')
    if project['task_number']==0:
        # Override opens an EMPTY Postgame workspace, never bypasses generation dependencies.
        tasks=stage_tasks(stage,control) if stage=='pregame' or state['phase'] in ('PREGAME_FROZEN','POSTGAME_GENERATED','POSTGAME_ANNOTATING','POSTGAME_FROZEN') else []
        if tasks:api(stage,f'/api/projects/{pid}/import',tasks)
    identity={'stage':stage,'instance':str(folder.relative_to(EVAL)),'port':PORTS[stage],
              'project_id':pid,'url':f'http://127.0.0.1:{PORTS[stage]}/projects/{pid}/data'}
    old=control.workspace(stage)
    if old and old!=identity:raise ValueError('workspace identity changed')
    control.workspace(stage,identity)
    actual=export(stage,control)
    if stage=='pregame':
        expected={s['sample_id']:s for s in samples()}
        if len(actual)!=20 or {t['data']['sample_id'] for t in actual}!=set(expected):
            raise ValueError('isolated Pregame inventory mismatch')
        for task in actual:
            clean_pregame_task(task)
            if canonical_hash(task['data']['context'])!=expected[task['data']['sample_id']]['context_sha256']:
                raise ValueError('live workspace Context mismatch')
    if stage=='postgame':control.audit('postgame_unlocked',actor,identity)
    return identity


def export(stage, control):
    identity=control.workspace(stage)
    if not identity:raise ValueError('workspace absent')
    return api(stage,f"/api/projects/{identity['project_id']}/export?exportType=JSON&download_all_tasks=true")


def open_annotations(stage,control,actor):
    control.assert_phase((stage.upper()+'_GENERATED',))
    identity=start(stage,control,actor)
    current=export(stage,control)
    if any(t.get('annotations') for t in current):raise ValueError('existing annotations; do not replace input')
    wanted={t['data']['sample_id']:t for t in stage_tasks(stage,control,True)}
    if {t['data']['sample_id'] for t in current}!=set(wanted):raise ValueError('workspace task set differs')
    for task in current:
        frozen=wanted[task['data']['sample_id']]
        api(stage,f"/api/tasks/{task['id']}/",{'data':frozen['data'],'meta':frozen['meta']},'PATCH')
    control.start_annotations(stage,actor)
    return identity


def freeze_workspace(stage,control,actor,new_revision=False):
    identity=control.workspace(stage)
    if not identity:raise ValueError('workspace absent')
    control.assert_phase((stage.upper()+'_ANNOTATING',))
    tasks=export(stage,control)
    wanted={t['data']['sample_id']:t for t in stage_tasks(stage,control,True)}
    for task in tasks:
        original=wanted[task['data']['sample_id']]
        if task['data']!=original['data'] or task['meta']!=original['meta']:
            raise ValueError('annotated task differs from accepted output binding')
    # Immutable revision path; SQLite transaction serializes competing freezes.
    path=HERE/'annotations'/f'{stage}-{now().replace(":","-")}.json'
    receipt=control.freeze(stage,tasks,list(wanted),path,actor,new_revision)
    stop_owned(PORTS[stage],RUNTIME/stage)
    return receipt


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','open','freeze','revise','status'])
    p.add_argument('--stage',choices=['pregame','postgame'],default='pregame');p.add_argument('--actor',default='local-operator')
    p.add_argument('--override-reason');a=p.parse_args();control=Control()
    if a.command=='status':result=control.state()
    elif a.command=='prepare':result=start(a.stage,control,a.actor,a.override_reason)
    elif a.command=='open':result=open_annotations(a.stage,control,a.actor)
    elif a.command=='revise':
        control.revise_annotations(a.stage,a.actor,a.override_reason)
        if a.stage=='pregame':stop_owned(PORTS['postgame'],RUNTIME/'postgame')
        result=start(a.stage,control,a.actor)
    else:result=freeze_workspace(a.stage,control,a.actor)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
