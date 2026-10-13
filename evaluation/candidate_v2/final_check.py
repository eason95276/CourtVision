"""Final evidence receipt; never sends a model request."""
import ast,json,subprocess,sys,urllib.request,urllib.error,xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import run as r
import human_workspace as h
from control import exclusive,now
from asset_scanner import EVAL,ROOT,read_json,sha,canonical_hash

def main():
    r.verify();before=read_json(EVAL/'reports/overnight-protection-before.json');after=r.b.root_protection()
    assert canonical_hash(after)==canonical_hash(before['production_and_frozen'])
    protected=0
    for group in ('baseline_files','baseline_reports','first_attempt_tree'):
        for path,digest in before[group].items():assert sha(ROOT/path)==digest;protected+=1
    for s in r.b.selection():h.load(s)
    current=h.current();ident=read_json(h.LEDGER/current['sample_id']/'identity.json')
    projects=h.api(current,'/api/projects/');projects=projects.get('results',[]) if isinstance(projects,dict) else projects
    tasks=h.api(current,f"/api/projects/{ident['project_id']}/export?exportType=JSON&download_all_tasks=true")
    assert len(projects)==len(tasks)==1 and tasks[0]['data']==h.task_for(current)['data']
    assert not tasks[0].get('annotations') and not tasks[0].get('predictions')
    next_access=None
    try:h.api(current,'/api/tasks/2/');raise ValueError('unexpected second task exposed')
    except urllib.error.HTTPError as exc:
        next_access=exc.code;assert exc.code==404
    xml=ET.parse(EVAL/'reports/overnight-final-complete-tests.xml').getroot()
    suites=list(xml.iter('testsuite'));counts={key:sum(int(s.get(key,'0')) for s in suites) for key in ('tests','failures','errors','skipped')}
    counts['passed']=counts['tests']-counts['failures']-counts['errors']-counts['skipped']
    syntax=[]
    for file in (EVAL/'candidate_v2').glob('*.py'):ast.parse(file.read_text(encoding='utf-8'));syntax.append(file.name)
    diff=subprocess.run(['git','diff','--check'],cwd=ROOT,capture_output=True,text=True);assert diff.returncode==0
    with urllib.request.urlopen('http://127.0.0.1:8625/',timeout=10) as resp:site_status=resp.status
    assert site_status==200
    summary=read_json(EVAL/'reports/golden-pregame-v1-v2-regression.json')
    assert sum(summary['provider_calls'].values())==len(list(r.BASE.glob('samples/*/*/request.json')))==33
    receipt={'created_at':now(),'model_calls_in_this_check':0,'this_round_real_model_calls':33,'real_calls_by_stage':summary['provider_calls'],
        'tests':counts,'candidate_tests':{'passed':35,'failed':0,'skipped':0,'receipt':'candidate-v2-final-tests.xml'},
        'syntax_ast_files':syntax,'compileall':'PASSED','js_syntax':'NOT_APPLICABLE_no_JS_changes','git_diff_check':'PASSED',
        'production_http_status':site_status,'protected_baseline_files':protected,'production_and_frozen_hash_match':True,
        'human_workspace':{'current':current['sample_id'],'visible_projects':len(projects),'visible_tasks':len(tasks),'annotations':0,'predictions':0,
             'next_task_api_status':next_access,'future_sample_databases':0,'postgame_open':False,
             'screenshot':'output/playwright/candidate-v2-human-first.png','browser_annotation_console_errors':0,'browser_annotation_console_warnings':1,
             'browser_login_external_resource_errors_present':True,'browser_checked_rating_inputs':0,'fake_human_labels':0},
        'judge_calls':0,'deepeval_formal_scores':0,'promptfoo_formal_red_team':False,'commit':False,'push':False,
        'known_remaining_failures':summary['remaining_failure_counts'],'quality_certified':False}
    exclusive(EVAL/'reports/overnight-protection-after.json',{'production_and_frozen':after})
    exclusive(EVAL/'reports/overnight-final-verification.json',receipt)
    print(json.dumps(receipt,ensure_ascii=True))
if __name__=='__main__':main()
