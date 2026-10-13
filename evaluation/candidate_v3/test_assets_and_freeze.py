import json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v3 import analysis_tools as a,final_workspace as h
from asset_scanner import EVAL,read_json,sha,canonical_hash

def test_benchmark_freeze_no_duplicate_no_unverified_contexts():
    freeze=read_json(EVAL/'benchmark/v1/freeze_manifest.json')
    assert freeze['n']==300 and freeze['model_calls']==0
    assert freeze['manifest_hash']==canonical_hash({k:v for k,v in freeze.items() if k not in ('manifest_hash','benchmark_revision')})
    for path,digest in freeze['files'].items():assert sha(EVAL/'benchmark/v1'/path)==digest
    report=read_json(EVAL/'reports/benchmark-v1-readiness.json');assert report['validated']==300 and report['invalid']==0
    assert report['unique_teams']==30 and report['max_team_appearances']==28

def test_real_deepeval_counts_and_no_judge():
    report=read_json(EVAL/'reports/deepeval-deterministic-regression.json')
    assert report['real_sdk_evaluate_called'] and report['n_records']==80 and report['metric_evaluations']==640
    assert report['judge_calls']==0 and report['outbound_network_blocked']
    assert report['counts'][a.REVISIONS[-1]]['execution_success']==14

def test_single_revision_all20_tasks_and_no_truth():
    manifest=h.target();tasks=read_json(h.EXPORT/'human_eval_tasks.json')
    assert len(tasks)==20 and manifest['successes']==14 and manifest['target_revision']==a.REVISIONS[-1]
    assert {t['meta']['generation_revision'] for t in tasks}=={a.REVISIONS[-1]}
    assert all('ground_truth' not in t['data'] and 'locked_forecast' not in t['data'] for t in tasks)

def test_real_trace_receipts_proved_not_fake_count():
    report=read_json(EVAL/'reports/phoenix-evaluation-workload.json')
    assert report['real_model_calls']==98 and report['persisted_spans_proved']==138 and not report['missing']
    assert not report['production_monitoring'] and report['dollar_cost'] is None

def test_badcase_events_not_distinct_root_causes():
    rows=list(map(json.loads,(EVAL/'bad_cases/v2/bad_cases_v2.jsonl').read_text(encoding='utf-8').splitlines()))
    assert len(rows)==87 and len({r['issue_group'] for r in rows})==87
    assert sum(r['source_revision']=='legacy_inventory' for r in rows)==40
    assert sum(r['source_revision']!='legacy_inventory' for r in rows)==47

def test_meta_failed_pairs_stay_in_denominator():
    report=read_json(EVAL/'reports/metamorphic-evaluation.json')
    assert report['real_model_calls']==20 and report['executed_pairs']==10 and report['completed_pairs']==8
    assert report['directional_consistency_rate']=={'n':8,'d':10,'rate':.8}
    assert report['conditional_on_completed']['d']==8 and report['invariant_scope'].startswith('5 static')

def test_promptfoo_blocked_not_fake_redteam():
    report=read_json(EVAL/'reports/promptfoo-red-team.json')
    assert report['status']=='BLOCKED' and report['actual_red_team_cases_executed']==0
