import ast,csv,json,sys,re
from pathlib import Path
from collections import Counter,defaultdict
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asset_scanner import EVAL,ROOT,read_json,canonical_hash,resolve,sha
from build_eval_baseline import save

def run():
    files=[];dependencies={};functions=0
    candidates=list((EVAL/'scripts').glob('*.py'))+list((EVAL/'golden_set/v1').glob('*.py'))+list((EVAL/'adversarial').glob('*.py'))+list((ROOT/'tests').glob('*.py'))
    production=['src/courtvision/features.py','src/courtvision/market.py','src/courtvision/data.py','v2/pregame_system/historical_replay.py','v2/frontend/analysis_service.py','v2/frontend/prediction_version_service.py','v2/frontend/server.py','v2/frontend/export_data.py','v2/pregame_system/official_final.py']
    candidates.extend(ROOT/p for p in production)
    for p in candidates:
        if not p.exists():continue
        tree=ast.parse(p.read_text(encoding='utf-8-sig'));imports=[]
        for n in ast.walk(tree):
            if isinstance(n,ast.Import):imports.extend(x.name for x in n.names)
            elif isinstance(n,ast.ImportFrom):imports.append(n.module or '.')
            elif isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):functions+=1
        dependencies[str(p.relative_to(ROOT))]=sorted(set(imports));files.append({'path':str(p),'sha256':sha(p)})
    cases=[json.loads(s) for s in (EVAL/'bad_cases/bad_cases.jsonl').read_text(encoding='utf-8').splitlines()];dedup=defaultdict(list);audit=[]
    for c in cases:
        source=resolve(c['source_file']);broken=[]
        for t in c.get('regression_tests',[]):
            f=resolve(t['source_file'])
            if not f or not re.search(r'\b'+re.escape(t['name'])+r'\b',f.read_text(encoding='utf-8-sig')):broken.append(t)
        code=c['failure_type'];category='pipeline';basis='Failure code is infrastructure evidence, not proof of reasoning quality.'
        if code.startswith(('largest_','worst_win')):category='numerical_prediction_error';basis='Forecast error/extreme is not an LLM quality failure.'
        elif code in ('JSONDecodeError','editorial_internal_model_layer_leak'):category='generation';basis='Recorded format/content protocol failure; no broad model reasoning score.'
        elif code.startswith('official_injury') or code=='same_url_injury_revision_not_refetched':category='data';basis='Source/temporal data acquisition issue.'
        elif code in ('version_specific_postgame_summary','market_display_spread_sign'):category='UI/presentation';basis='Rendering/binding error.'
        elif 'replay' in str(c.get('stage','')):category='evaluation'
        signature=canonical_hash({'game':c.get('game_id'),'code':code,'evidence':c.get('evidence')});dedup[signature].append(c['case_id'])
        audit.append({'case_id':c['case_id'],'candidate_category':category,'basis':basis,'source_exists':bool(source),'evidence_present':bool(c.get('evidence')),
            'broken_test_references':broken,'fix_status':c.get('regression_status_if_known'),'fix_conflict':'No execution rerun; documented fix is not certified current full replay.',
            'failure_code':code,'source_file':c['source_file']})
    with (ROOT/'artifacts/test_predictions_with_market.csv').open(encoding='utf-8-sig',newline='') as f:raw=list(csv.DictReader(f))
    truth_conflicts=[{k:r[k] for k in ('game_id','game_date','point_margin','total_points','actual_home_margin','actual_total')} for r in raw if abs(float(r['point_margin'])-float(r['actual_home_margin']))>0.01 or abs(float(r['total_points'])-float(r['actual_total']))>0.01]
    report={'python_files_ast_scanned':len(files),'functions':functions,'dependency_imports':dependencies,'file_hashes':files,
        'bad_cases':audit,'candidate_failure_taxonomy':dict(Counter(r['candidate_category'] for r in audit)),
        'unverified_model_reasoning_count':0,'root_cause_code_recurrence':dict(Counter(c['failure_type'] for c in cases)),
        'duplicate_observation_groups':[ids for ids in dedup.values() if len(ids)>1],'unique_case_ids':len({c['case_id'] for c in cases}),
        'broken_test_ref_count':sum(len(r['broken_test_references']) for r in audit),'missing_evidence_count':sum(not r['evidence_present'] for r in audit),
        'numerical_truth_provider_disagreements':truth_conflicts,
        'taxonomy_status':'Candidate triage only. A publication blocker may be an over-strict validator, missing input or generated claim; do not infer model reasoning cause from the code alone.'}
    save(EVAL/'reports/secondary-adversarial-audit.json',report);print(json.dumps({'scanned':len(files),'bad_cases':len(audit),'candidate_categories':report['candidate_failure_taxonomy'],'duplicate_groups':len(report['duplicate_observation_groups']),'broken_refs':report['broken_test_ref_count'],'numerical_truth_conflicts':len(truth_conflicts)}))

if __name__=='__main__':run()
