import json,sys,urllib.request
from pathlib import Path
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asset_scanner import EVAL,read_json
from build_eval_baseline import save,text

def load(name):return read_json(EVAL/'reports'/name)
data=load('dataset-adversarial-audit.json');sampling=load('sampling-adversarial-audit.json');secondary=load('secondary-adversarial-audit.json');calendar=load('temporal-source-conflicts.json');workbench=load('workbench-blindness-audit.json')
before=load('temporal-attacks-before.json');after=load('temporal-attacks-after.json');ident=load('identity-attacks.json');mutants=load('test-mutation-audit.json')
issues=[
 {'id':'E01','severity':'P0','issue':'Nested fields, malformed dates, incomplete proof groups and window/record inconsistencies accepted','evidence':'temporal-attacks-before.json','status':'FIXED','fix':'Positive context shape + proof group/date/count checks; trusted context hash for value forgery.'},
 {'id':'E02','severity':'P0','issue':'Freeze script could rewrite existing manifest; file additions and metadata/manifest alterations were incompletely checked','evidence':'scripts/freeze_closed_book.py old copy; mutation fixtures','status':'FIXED','fix':'Independent revision/byte pins, exact inventory, traversal/symlink rejection; existing freeze verifies only.'},
 {'id':'E03','severity':'P0','issue':'SDK wrapper input/metadata could be blindly serialized; anonymous wrapper did not prove frozen binding','evidence':'scripts/future_eval_adapters.py old copy','status':'FIXED','fix':'Frozen sample binding required by default, explicit test-only escape; pure context-only stateless request boundary.'},
 {'id':'E04','severity':'P1','issue':'Identity detector omitted city/arena/abbreviations, case/punctuation, timestamp and provider-ID forms','evidence':'identity-attacks.json','status':'FIXED','fix':'18 direct-layer attacks rejected; actual immutable corpus explicit leakage still0.'},
 {'id':'E05','severity':'P1','issue':'Removing target ID exclusion left legacy tests green','evidence':'test-mutation-audit.json legacy_only_exit_code=0','status':'FIXED_TEST','fix':'Permanent rescheduled target-ID/date property test; all8 code mutants now killed.'},
 {'id':'E06','severity':'P0 before human eval','issue':'Same Label Studio admin token/browser accesses both stages and joins20 shared sample IDs','evidence':'workbench-blindness-audit.json','status':'UNFIXED_LIMIT','fix':'No upstream/ACL rewrite. Separate trusted operator from blind annotator and stage access before real scoring.'},
 {'id':'E07','severity':'P0 before model execution','issue':'No real model runtime/OS filesystem or network sandbox has been implemented or exercised','evidence':'generation_profile.json; dry-run disabled','status':'UNFIXED_NOT_STARTED','fix':'Do not equate JSON flags/data directories with a sandbox. Keep execution disabled until constrained backend is authorized.'},
 {'id':'E08','severity':'P1','issue':'Exact recent-score and H2H fingerprints uniquely identify all1231 inputs within this corpus','evidence':'dataset-adversarial-audit.json','status':'UNFIXED_CHARACTERISTIC','fix':'Cannot claim zero parameter-memory contamination; coarsened/synthetic alternative requires a separately authorized dataset revision.'},
 {'id':'E09','severity':'P1 before human/model eval','issue':'Later context can expose another sample outcome as valid past result; Golden has one directed pair with2 window appearances','evidence':'cross_sample_dependencies in dataset-adversarial-audit.json','status':'UNFIXED_OPERATIONAL','fix':'Stateless separate model requests; chronological human batch; never reuse shared conversation or re-review blind earlier tasks after later disclosure.'},
 {'id':'E10','severity':'P1 source coverage','issue':'Stale Jan25 teamId=0 placeholder rows coexist with March played rows; inventory treated 3 IDs as incomplete boxscore and excluded two valid events','evidence':'temporal-source-conflicts.json; TeamStatistics rows1304/1305; placeholder regression','status':'FIXED_CONSUMER_DATA_NOT_REGENERATED','fix':'Ignore invalid teamId0 in future asset coverage scan. Current1231 frozen dataset/selection not enlarged; no premature event use found. Authorized next revision must recount eligibility.'},
 {'id':'E11','severity':'P1 baseline evidence','issue':'Three numerical export outcome fields disagree across model and market-result columns','evidence':'secondary-adversarial-audit.json','status':'REPORTED_DATA_NO_REWRITE','fix':'Do not silently mix truth definitions or rewrite old metrics; source adjudication needed for stronger baseline claims.'},
 {'id':'E12','severity':'P2','issue':'40 Bad Case events include3 duplicate observation groups and diverse pipeline/data/UI/numeric failures','evidence':'secondary-adversarial-audit.json','status':'AUDITED_NOT_RECLASSIFIED','fix':'Candidate triage separate; no assertion of40 independent LLM reasoning failures.'},
 {'id':'E13','severity':'P2 privacy','issue':'Self-hosted Label Studio browser attempts external Sentry telemetry despite local data setup','evidence':'fresh browser performance resource observation','status':'UNFIXED_CONFIGURATION','fix':'No payload exfiltration asserted. Verify supported telemetry disable/egress policy before sensitive human/model responses; no upstream patch.'}]
tests_path=EVAL/'adversarial/final-tests.xml';cases=ET.parse(tests_path).getroot().findall('.//testcase') if tests_path.exists() else []
tests={'executed':len(cases),'passed':sum(c.find('failure') is None and c.find('error') is None and c.find('skipped') is None for c in cases),
    'failed_or_error':sum(c.find('failure') is not None or c.find('error') is not None for c in cases),'skipped':sum(c.find('skipped') is not None for c in cases),
    'skip_reason':'Real Windows symlink creation lacks privilege; traversal and other byte-integrity fixtures executed.',
    'not_executed':['Production full suite (outside scope, not certified)','Formal DeepEval/Judge/model execution','Real human annotation/agreement'],
    'setup_errors_resolved':['Initial mutant copies computed wrong asset root; collection errors were NOT counted as mutant kills, root pinned and all rerun.']}
with urllib.request.urlopen('http://127.0.0.1:8625/',timeout=20) as r:site=r.status
report={'audit_scope':'Evaluation code/tests + directly related historical processing/version readers; read-only immutable datasets/production; isolated mutation fixtures.',
    'confirmed_issues':issues,'temporal_attacks':{'scenarios':len(after),'before_rejected':sum(r['rejected'] for r in before),'after_rejected':sum(r['rejected'] for r in after),'after_crashes':sum(bool(r['crash']) for r in after)},
    'identity_attacks':{'scenarios':len(ident),'before_rejected':sum(r['before_rejected'] for r in ident),'after_rejected':sum(r['after_rejected'] for r in ident)},
    'dependency_scan':{'python_files':secondary['python_files_ast_scanned'],'functions':secondary['functions'],'static_only':True},
    'duplicate_audit':data['duplicates'],'source_audit':{'contexts':data['contexts_audited'],'references':data['source_events_checked'],'source_issues':len(data['independent_source_issues']),
        'duplicated_player_event_facts':len(data['duplicated_player_event_facts']),'calendar_conflicts':2,'premature_conflict_usage_samples':len(calendar['affected_sample_ids'])},
    'fingerprints':data['statistical_fingerprints'],'cross_sample':data['cross_sample_dependencies'],
    'sampling_audit':sampling,'bad_case_audit':{'records':40,'unique_ids':secondary['unique_case_ids'],'duplicate_observation_groups':secondary['duplicate_observation_groups'],'broken_test_refs':secondary['broken_test_ref_count'],'candidate_taxonomy':secondary['candidate_failure_taxonomy']},
    'workbench':workbench,'browser_observation':{'fresh_pregame_dom_final_fact_panel':False,'fresh_pregame_dom_actual_margin_token':False,'checked_score_radios':0,
        'same_browser_can_access_postgame_ground_truth':True,'session_storage_keys':[],'local_storage_keys':['userpic-color-1','panelState','autoAnnotation'],
        'external_telemetry_attempt':'Sentry ingest resource observed, payload contents/success not certified','annotations_submitted':0},
    'mutation_audit':{'mutants':len(mutants),'killed':sum(r['killed_by_regression'] for r in mutants),'legacy_target_guard_removal_survived':next(r['legacy_only_exit_code']==0 for r in mutants if r['mutation']=='remove_target_exclusion')},
    'freeze_validation':load('closed-book-full-validation.json').get('integrity'),
    'tests':tests,'production_http':site,'protection':load('adversarial-protection-result.json'),
    'syntax':'passed','git_diff_check':read_json(EVAL/'reports/adversarial-diff-check.json'),
    'llm_calls':0,'models_generated':0,'annotations_created':0,'golden_selection_changed':False,'dataset_regenerated':False,'commit':False,'push':False,
    'evidence_documents':['tool-capability-matrix.md','resume-evidence-matrix.md','interview-evidence-pack.md','evaluation-capability-map.md'],
    'highest_priority_next_actions':['Implement actual pregame-only human access separation and no-shared-context model execution before outputs/labels.',
        'Keep V1 immutable; source calendar/coverage/truth discrepancies need adjudication and an authorized new revision if corrected.',
        'Then generate real outputs and human labels; only afterward discuss Judge calibration.'],
    'monday_resume_boundary':'Can strongly describe traceable dataset construction, deterministic sampling/version binding, adversarial/mutation regression and workbench preparation. Cannot claim scored Benchmark, labeled Gold, agreement, Judge, zero memory contamination, production observability, or forward uplift.'}
save(EVAL/'reports/evaluation-adversarial-audit.json',report)
flow='''```text
Games.csv / TeamStatistics.csv / Player Knowledge receipts
  → date-exclusive reconstruction (historical, NOT captured-before-cutoff proof)
  → frozen Context ──────→ anonymized Context (identities removed, fingerprints remain)
  → provenance + byte manifest (private integrity roots)
  → hidden Ground Truth (separate files, NOT OS access control)
Context + hidden GT → private candidate/Golden sampling → fixed20 selection
  ├→ Pregame import: Context + empty output, NO GT/strata
  └→ Postgame import: Context + GT/boxscore + waiting Pregame/output
Same local admin can read both projects → strong blindness NOT established
Frozen-only context loader → stateless request serialization → future model backend NOT enabled
Future actual output → human labels NOT STARTED → evaluator/Judge NOT STARTED
```
'''
table='\n'.join('| '+r['id']+' | '+r['severity']+' | '+r['issue']+' | '+r['status']+' | '+r['evidence']+' |' for r in issues)
text(EVAL/'reports/evaluation-adversarial-audit.md','# Evaluation Infrastructure Adversarial Audit & Hardening\n\n'+flow+
    '\n## Confirmed issues / fixes / limits\n\n| ID | Priority | Finding | Status | Evidence |\n|---|---|---|---|---|\n'+table+
    '\n\n## Attacks and real data\n\n45 temporal/structure/binding attacks: old20 rejected, new45; 18 direct identity attacks: old2, new18. Numeric forgery detection requires trusted frozen hash, not a self-authored proof. All1231 contexts+anonymous files revalidated without changing bytes;955009 source refs independently compared,0 detected target/future source use. This does not prove precise historical publication or source completeness.\n\n'+
    'Fingerprint: recent-score and H2H sequences unique1231/1231; record1162 unique, player lines1217 unique. This is corpus linkability, not a measured LLM memory-retrieval rate. Explicit identity leakage in actual immutable anonymous corpus0.\n\n'+
    'Exact normal/anonymous/normalized and same-date/provider/flipped identity duplicates0;227 repeated score-value groups do not imply duplicate games. Top20 near-numeric candidates audited, no guessed identity merging.\n\n'+
    'Two date disagreements (Jan25 vs Mar18/31) verified against saved gzip ESPN final headers; no premature reference in frozen inputs found. Source archive coverage and3 old numerical GT discrepancies remain data limitations; no frozen correction performed.\n\n'+
    '## Sampling\n\n3 official repeats identical ordered selections/hash;5 alternative seeds (audit-only) each cover30 teams, representative phases3/6/3 stable, all-phase/outcome counts change by at most1 in observed seeds. V1 not extreme in these trials, not proof of unbiased natural prevalence. No duplicate matchups;2 dates have2 games each. No forecast-error/generated-output ranking.\n\n'+
    '## Ground Truth and workbench\n\nActual Pregame API/DOM show no GT or final-fact panel. Same browser admin successfully reads Postgame GT;20 shared sample IDs permit joining. Session/local storage inspected only in a fresh session; older-cache safety is not guaranteed. External Sentry requests observed. No submit or annotations. No upstream/ACL changes, no strong blind claim.\n\n'+
    '## Bad Cases and tests\n\n40 event IDs,3 duplicate observation groups,4 numeric-error cases,30 provisional pipeline,2 generation,2 data,2UI. No verified causal model-reasoning attribution. Broken source test refs0; static reference != real replay.\n\n'+
    'All8 temporary code mutants killed by new tests; deleting target-ID exclusion left legacy tests green. Initial mutation setup collection errors were resolved and not counted as kills. Traversal/byte/inventory/manifests tested; real Windows symlink creation skipped for privilege. No root-attacker/signed-manifest guarantee.\n\n'+
    '## Execution and protection\n\n'+json.dumps(tests,ensure_ascii=False,indent=2)+'\n\n'+json.dumps(report['protection'],ensure_ascii=False,indent=2)+
    f'\n\nPython syntax passed; git diff check see JSON receipt. Production8625 HTTP{site}. Frozen dataset/Golden selection/Rubric/import bytes and5 Prediction/7 Postgame hashes preserved. 0 LLM/Judge/outputs/labels; no commit/push.\n\n'+
    '## Evidence documents / Monday boundary\n\nSee tool-capability-matrix.md, resume-evidence-matrix.md, interview-evidence-pack.md (35 project questions), evaluation-capability-map.md. Can describe construction, source binding, reproducible sampling, bounded red-team/mutation tests and workbench readiness strongly. Formal model quality, human labels/agreement, Judge calibration, full production tracing and forward uplift must wait for actual execution and evidence—not just a weekend plan.\n\n'+
    '## Next priorities\n\n1. Separate human stage access and actual constrained stateless model execution.\n2. Adjudicate source calendar/GT/coverage issues; only explicit new revision can correct frozen data.\n3. Real outputs and labels first; no Judge design started here.')
print(json.dumps({'fixed':sum(r['status'].startswith('FIXED') for r in issues),'unfixed_limits':sum(not r['status'].startswith('FIXED') for r in issues),'tests':tests,'site':site,'data_unchanged':all(report['protection'].values())}))
