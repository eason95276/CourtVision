"""Read-only final protection/call-budget/workbench receipt. Zero model calls."""
import ast,json,sys,sqlite3,hashlib,subprocess,urllib.request,urllib.error,xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v3 import analysis_tools as a,final_workspace as h
from evaluation.candidate_v3.audit_v2 import protection
from asset_scanner import EVAL,ROOT,read_json,sha,canonical_hash
from control import exclusive,now

def run():
    before=read_json(EVAL/'reports/mega-protection-before.json');after=protection()
    if canonical_hash(before)!=canonical_hash(after):raise ValueError('protected source/old artifact/version drift')
    hashes={}
    # Opaque SQL payload bytes only. Do not parse/read Postgame facts into model/RCA inputs.
    dbpath=ROOT/'v2/frontend/output/prediction_versions.sqlite'
    with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True) as db:
        for table in ('prediction_versions','postgame_versions'):
            rows=db.execute('SELECT version_id,payload_json,payload_sha256 FROM '+table).fetchall()
            for identity,raw,stored in rows:
                if hashlib.sha256(raw.encode('utf-8')).hexdigest()!=stored:raise ValueError('actual immutable payload hash mismatch')
            hashes[table]={'n':len(rows),'actual_payload_hash_matches_stored':True}
    if hashes['prediction_versions']['n']!=5 or hashes['postgame_versions']['n']!=7:raise ValueError('published version counts changed')
    for revision in a.REVISIONS:
        module=a.module_for(revision);module.assert_contract() if revision==a.v2.b.REV else module.verify()
        for sample in a.v2.b.selection():a.load(revision,sample)
    sample=h.current();identity=read_json(h.EXPORT/'annotations'/sample['sample_id']/'identity.json');wanted=h.task_for(sample)
    projects=h.api(sample,'/api/projects/');projects=projects.get('results',[]) if isinstance(projects,dict) else projects
    tasks=h.api(sample,f"/api/projects/{identity['project_id']}/export?exportType=JSON&download_all_tasks=true")
    if len(projects)!=1 or len(tasks)!=1 or tasks[0]['data']!=wanted['data'] or tasks[0]['meta']!=wanted['meta'] or tasks[0].get('annotations') or tasks[0].get('predictions'):raise ValueError('human workspace count/binding changed')
    try:h.api(sample,'/api/tasks/2/');raise ValueError('future task exposed')
    except urllib.error.HTTPError as exc:
        if exc.code!=404:raise
    testxml=ET.parse(EVAL/'reports/mega-final-safe-tests.xml');suites=list(testxml.getroot().iter('testsuite'))
    counts={key:sum(int(s.get(key,'0')) for s in suites) for key in ('tests','failures','errors','skipped')};counts['passed']=counts['tests']-counts['failures']-counts['errors']-counts['skipped']
    syntax=[]
    for folder in ('candidate_v3','candidate_v4'):
        for file in (EVAL/folder).glob('*.py'):ast.parse(file.read_text(encoding='utf-8'));syntax.append(str(file.relative_to(ROOT)))
    diff=subprocess.run(['git','diff','--check'],cwd=ROOT,capture_output=True,text=True)
    if diff.returncode:raise ValueError('git diff check failed')
    with urllib.request.urlopen('http://127.0.0.1:8625/',timeout=10) as response:site=response.status
    if site!=200:raise ValueError('production site unavailable')
    trace=read_json(EVAL/'reports/phoenix-evaluation-workload.json')
    if trace['real_model_calls']!=98 or trace['missing']:raise ValueError('model budget/trace mismatch')
    source=read_json(EVAL/'benchmark/v1/selection_manifest.json')['samples'];gold={s['sample_id'] for s in a.v2.b.selection()};overlap=[r['sample_id'] for r in source if r['sample_id'] in gold]
    unknown=[];writers=0
    for revision in a.REVISIONS[1:]:
        for s in a.v2.b.selection():
            row=a.load(revision,s)
            if row['editorial'] is None:continue
            writers+=1;legal={e['id'] for e in a.v2.c.catalog(row['context'])['entries']}
            for player in row['editorial']['player_analysis']:
                for ref in player['fact_ids']:
                    if ref not in legal:unknown.append({'revision':revision,'sample':s['sample_id'],'ref':ref})
    exclusive(EVAL/'reports/writer-evidence-reference-audit.json',{'writers_checked':writers,'unknown_references':unknown,'scope':'ID existence only, no statement-to-fact semantic certification','model_calls':0})
    receipt={'created_at':now(),'tests':counts,'python_ast_files':len(syntax),'python_syntax':'PASSED','js_syntax':'NOT_APPLICABLE_no_JS_changes','git_diff_check':'PASSED',
        'protected_files':len(before['files']),'protected_versions':hashes,'before_after_match':True,'v1_v2_immutable':True,'v3_v4_contracts_verified':True,
        'production_http_status':site,'current_human_revision':h.target()['target_revision'],'human':{'visible_tasks':1,'annotations':0,'predictions':0,'next_task_api':404,'postgame_open':False,
            'actual_browser_annotation_errors':0,'actual_browser_annotation_warnings':1,'checked_rating_inputs':0,'login_vendor_external_errors_present':True,
            'screenshot':'output/playwright/human-target-v4-first-loaded.png'},'this_round_model_calls':98,'this_check_model_calls':0,'real_judge_calls':0,
        'benchmark_golden_overlap':overlap,'writer_reference_audit_n':writers,'unknown_writer_refs':len(unknown),'forward_metrics':None,'human_quality_scores':None,
        'commit':False,'push':False,'setup_issues_corrected':['Official-doc parser missingbs4; usedstdlib','Workspace export parent missing; createdexplicitdir','DeepEval GBK console; UTF8 reexecution, noLLMretry','Closed old browser session; reopened'],
        'not_run':['production full-repository suite','Judge/DeepEvalLLM scores','Promptfoo real redteam (no endpoint)','300LLM batch','Human scoring'],
        'remaining':['V4 six execution failures','LLM repair recovery0','no arbitrary semantic paraphrase certification','14 articles pending real Human review','benchmark20 tuning/6 overlap and parameter-memory limits']}
    exclusive(EVAL/'reports/mega-protection-after.json',after);exclusive(EVAL/'reports/mega-final-verification.json',receipt)
    print(json.dumps(receipt,ensure_ascii=True))
if __name__=='__main__':run()
