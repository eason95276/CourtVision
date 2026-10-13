"""Live local/read-only acceptance + protection receipts; never a model call."""
import json,os,sys,urllib.request,xml.etree.ElementTree as ET
from pathlib import Path
from control import Control,now
from runner import load_profile,preflight,samples,HERE,EVAL,context_for
from workspace import api,export,listening,clean_pregame_task
from asset_scanner import read_json,canonical_hash
sys.path.insert(0,str(EVAL/'adversarial'))
from capture import capture


def cross_selected_history():
    selected=samples();target={s['target_game_id']:s['sample_id'] for s in selected};found=[]
    def walk(value,owner,path=''):
        if isinstance(value,dict):
            ref=value.get('game_ref')
            if ref in target and target[ref]!=owner and any(k in value for k in ('points_for','points_against','home_points','away_points','home_score','away_score')):
                found.append({'earlier_sample':target[ref],'later_sample':owner,'path':path})
            for key,child in value.items():walk(child,owner,path+'/'+key)
        elif isinstance(value,list):
            for i,child in enumerate(value):walk(child,owner,path+'/'+str(i))
    for sample in selected:walk(context_for(sample),sample['sample_id'])
    return found


def main():
    control=Control();config=load_profile(HERE/'profiles/closed-book-exec-v1/profile.json')
    flight=preflight(config,'pregame',control)
    tasks=export('pregame',control)
    for task in tasks:clean_pregame_task(task)
    projects=api('pregame','/api/projects/');projects=projects['results'] if isinstance(projects,dict) else projects
    counts={'task_count':len(tasks),'annotations':sum(len(t.get('annotations',[])) for t in tasks),
            'predictions':sum(len(t.get('predictions',[])) for t in tasks),
            'real_outputs':len(list((HERE/'outputs').rglob('artifact.json'))) if (HERE/'outputs').exists() else 0}
    try:api('pregame','/api/projects/3/');foreign_status=200
    except urllib.error.HTTPError as exc:foreign_status=exc.code
    post_blocked=False
    try:control.postgame_gate('acceptance-probe')
    except ValueError:post_blocked=True
    with urllib.request.urlopen('http://127.0.0.1:8625/',timeout=15) as response:production_status=response.status
    protection=capture();before=read_json(EVAL/'reports/execution-protection-before.json')
    checks={k:protection[k]==before[k] for k in before if k!='captured_at'}
    tests=ET.parse(EVAL/'reports/execution-readiness-tests.xml').getroot().find('testsuite')
    result={'collected':int(tests.get('tests')),'failed':int(tests.get('failures')),'errors':int(tests.get('errors')),'skipped':int(tests.get('skipped'))}
    result['passed']=result['collected']-result['failed']-result['errors']-result['skipped']
    overlaps=cross_selected_history()
    assert len(projects)==1 and len(tasks)==20 and counts['annotations']==counts['predictions']==counts['real_outputs']==0
    assert foreign_status==404 and post_blocked and not listening(8681) and not listening(8683)
    assert production_status==200 and all(checks.values()) and result['failed']==result['errors']==0
    report={'created_at':now(),'readiness':'READY FOR REAL PREGAME GENERATION',
            'real_generation_authorized':False,'real_llm_calls':0,'new_articles':0,'new_human_labels':0,
            'phase':control.state(),'preflight':flight,'tests':result,'protection':checks,
            'workbench':{'projects':[{'id':p['id'],'title':p['title']} for p in projects],**counts,
                'pregame_url':control.workspace('pregame')['url'],'old_shared_port_closed':not listening(8681),
                'postgame_port_closed':not listening(8683),'foreign_postgame_project_status':foreign_status,'postgame_phase_gate_rejected':post_blocked},
            'provider':{'name':config['provider'],'model':config['model'],'model_revision':None,
                'credential_environment_present':bool(os.environ.get('DEEPSEEK_API_KEY')),'live_api_verified':False,
                'calls_per_sample':1,'automatic_retry':False,'request_tools_schema_present':False},
            'production_http_status':production_status,'generation_blocker':None,
            'strict_all_20_outcome_blindness':{'status':'NOT_PROVEN','cross_selected_history_edges':overlaps,
                'reason':'Frozen later context contains an earlier selected target as legal historical data; separate Postgame DB does not erase that knowledge. No frozen data changed.'},
            'limits':['Not an OS sandbox against the local owner; no promise to block independent internet lookup.',
                'Label Studio login produced third-party Sentry network errors; authenticated task view has CSP report-only/MobX warnings. No claim of zero third-party telemetry.',
                'No live provider/real generation/real annotation freeze end-to-end validation; fake transport and synthetic temp labels only.',
                'Generation readiness does not certify absolute all-20 human blindness or article quality.']}
    (EVAL/'reports/execution-protection-after.json').write_text(json.dumps(protection,ensure_ascii=False,indent=2),encoding='utf-8')
    (EVAL/'reports/golden-set-execution-readiness.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'readiness':report['readiness'],'tests':result,'protection':checks,'state':control.state()['phase'],
        'cross_selected_visible_history_edges':len(overlaps),'production_status':production_status},ensure_ascii=False))


if __name__=='__main__':main()
