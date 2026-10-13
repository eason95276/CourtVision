"""Derived final audit handoff; no model, retrieval, labels or dataset writes."""
from collections import Counter
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
import sqlite3,xml.etree.ElementTree as ET
from .common import *

def main():
    m=read(HERE/'metrics.json');summary=read(HERE/'closeout-judge-recovery-01/judge-summary.json')
    paired=read(ROOT/'evaluation/reports/golden-pregame-v1-v4-regression.json')
    revisions=[]
    for item in paired['revisions']:
        x=item['metrics'];revisions.append({'revision':item['revision'],'status':'HISTORICAL_PRESERVED','n':20,
            'provider_success':x['provider_success']['n'],'parse_success':x['parse_success']['n'],'raw_schema_success':x['raw_schema_compliance']['n'],
            'e2e_success':x['e2e_success']['n'],'repair_triggered':x['repair_trigger']['n'],'repair_recovered':x['repair_recovery']['n']})
    g=m['generation'];d=m['data'];rows=read(HERE/'qualification.json')['rows']
    revisions.append({'revision':REV,'status':'CURRENT_FINAL_EVAL_ADAPTER','n':20,'provider_success':g['provider_success'],'parse_success':g['parse_success'],
        'raw_schema_success':g['raw_schema_success'],'raw_e2e_success':g['e2e_success'],'qualified_e2e_success':g['qualified_e2e_success'],
        'repair_triggered':g['repair_triggered'],'repair_recovered':g['repair_recovered'],'production_equivalence':False})
    with sqlite3.connect((ROOT/'v2/frontend/output/prediction_versions.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        stages={str(k):n for k,n in db.execute("SELECT json_extract(payload_json,'$.stage'),COUNT(*) FROM prediction_versions GROUP BY json_extract(payload_json,'$.stage')")}
        product={'prediction_versions':db.execute('SELECT COUNT(*) FROM prediction_versions').fetchone()[0],
            'postgame_versions':db.execute('SELECT COUNT(*) FROM postgame_versions').fetchone()[0],
            'generation_tasks':db.execute('SELECT COUNT(*) FROM generation_tasks').fetchone()[0],
            'prediction_stages':stages,'published_final_pregame':stages.get('FINAL_PREGAME',0),
            'final_task_status':dict(db.execute("SELECT status,COUNT(*) FROM generation_tasks WHERE stage='FINAL_PREGAME' GROUP BY status"))}
    dataset=read(ROOT/'evaluation/reports/closed-book-benchmark-preparation.json');sampling=read(ROOT/'evaluation/golden_set/v1/sampling_summary.json')
    freeze=read(ROOT/'evaluation/benchmark/v1/freeze_manifest.json');meta=read(ROOT/'evaluation/reports/metamorphic-evaluation.json')
    shadow=read(ROOT/'evaluation/reports/agent-reach-shadow-3games.json')
    def locate(x):
        if isinstance(x,dict):
            if 'global_url_metrics' in x:return x['global_url_metrics']
            for v in x.values():
                r=locate(v)
                if r:return r
        return None
    tests=ET.parse(HERE/'final-audit-20261013-tests.xml').getroot().find('testsuite')
    audit_tests={k:int(tests.get(k,'0')) for k in ('tests','failures','errors','skipped')}
    audit_tests['passed']=audit_tests['tests']-sum(audit_tests[k] for k in ('failures','errors','skipped'))
    stamp=datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).isoformat()
    safe_claims=[
        'Built fixed Contexts, static Benchmark, Golden20 and a hard/soft Rubric.',
        'Executed same20 versioned closed-book candidates with failures retained; Final raw15 versus qualified14 are distinct.',
        'Executed actual DeepEval SDK on Final20×8=160 bounded deterministic checks.',
        'Executed one homogeneous final LLM Judge protocol on14 qualified articles;154 literal Editorial quotes match.',
        'Prepared independent Human workspace and basic agreement/within-one export workflow; no actual Human quality labels yet.',
        'Preserved87 failure event groups and reproduced47 archived expected outcomes, including20 expected rejections.',
        'Persisted/reverified evaluation tracing; not production monitoring.'
    ]
    unsafe_claims=[
        '300 full model generations or1,231 fully verified historical live replays.',
        'Final14/20 is basketball prediction accuracy or exact production E2E reliability.',
        'Prompt-only causal uplift from0/20 to70%; candidate contracts/inputs changed.',
        '154 independently verified facts or expert-certified zero-error articles.',
        'Completed Human labels, Human-Judge agreement, Cohen kappa or Human-Human IAA.',
        '87 distinct root causes resolved or47 bugs all fixed.',
        'Production automatically publishes a verified T-20 FINAL; runtime remainsT-5 and publishedFINAL0.',
        'Full Postgame Golden/Judge quality evaluation or forward validation completed.',
        'Agent Reach proven production quality uplift; integration did not occur.'
    ]
    result={
      'as_of_beijing':stamp,'project_status':{'engineering_v1':'FINALIZED','audit':'COMPLETE','human_semantic_validation':'NOT_DONE','production_release':'NOT_CERTIFIED'},
      'product_scope':{'description':'Unified pregame basketball judgment after independent reasoning and market review, plus separately bound deterministic/AI Postgame.',
          'planned_final_lead_minutes':20,'current_runtime_final_lead_minutes':5,'runtime_evidence':['v2/pregame_system/scheduler_policy.py:13','v2/pregame_system/official_final.py:16'],
          'live_chain':'roster preflight → Production Pure/Generation Input → fresh Research → AnalystContext → Independent → Market Collector/Review → locked Final → Editorial/hard facts → immutable version → API/Reader',
          'stored_product_artifacts':product,'manual_portfolio_overrides':'Optional version/hash-bound Lakers/Warriors pregame and postgame manual sidecars; not Golden model outputs.'},
      'evaluation_scope':{'mode':'Historical Final Pregame Replay, frozen closed-book candidate adapter','production_equivalence':False,'research_during_model_requests':False,
          'final_pure_baseline':'UNAVAILABLE','sources_retrospectively_reconstructed':True,'strict_point_in_time':False,'main_target':'Pregame final Editorial','postgame_golden_evaluation':'NOT_DONE'},
      'datasets':{'revision':'controlled-cb-v1-6d788de247e93eaaedf4','normal_contexts':dataset['successful_contexts'],'anonymous_variants':dataset['anonymized_contexts'],
          'game_types':dataset['game_type_distribution'],'frozen_file_hashes_verified':6162,'anonymous_not_complete_decontamination':True,
          'benchmark':{'revision':freeze['benchmark_revision'],'n':freeze['n'],'teams':30,'status':'STATIC_READY','model_calls':0,'golden_overlap':6,'independent_holdout':False}},
      'golden_set':{'revision':GOLD,'n':20,'teams':sampling['unique_teams'],'representative':sampling['counts']['representative'],'challenge':sampling['counts']['challenge'],
          'phase_counts':sampling['phases_all'],'phase_rule':'observed prior current-season games, mean+1: early≤20,mid21–60,late>60; not official ordinal',
          'selection_seed':20261010,'eligible_regular_season_pool':sampling['eligible_pool'],'human_gold_labels':0,'selection_private_labels_not_model_input':True},
      'revisions':revisions,
      'final_execution':{'revision':REV,'dataset_revision':DATA,'n':20,'provider_success':20,'parse_success':18,'raw_schema_success':7,'raw_articles':15,'qualified_e2e':14,
          'generation_failures':5,'acceptance_hold':1,'component_attempts':g['component_attempts'],'generation_component_calls':55,
          'repair_recovered':{'n':2,'d':5},'qualification_case':'cb-0050013765f1198cade4','qualification_reason':'Absence disclosure rejected by strict production guard; tentative accepted leaked through adapter exception. Exception branch fixed; original15/20 record retained; no actual observed-price input leakage demonstrated.',
          'quality_NA_samples':6,'model_requested':'deepseek-v4-flash','model_revision_pin':'provider immutable revision unavailable; record response model fields','quality_retries':0},
      'deepeval':{'final_revision':REV,'n_records':20,'metric_count':8,'checks':160,'property_passed':128,'property_failed':32,'all_metric_pass_records':6,
          'pass_counts':m['deepeval']['passes'],'historical':{'status':'HISTORICAL_SEPARATE','n_records':80,'revisions':'V1–V4','checks':640},
          'semantic_quality_certification':False,'sdk_evaluate_real':True},
      'judge':{'status':'DONE','n':14,'provider_success':14,'parse_success':14,'schema_success':14,'literal_editorial_quotes':154,
          'quote_location_counts':{'article':128,'lead':13,'other_editorial_fields':13},'context_reference_semantically_verified':False,
          'model_requested':'deepseek-v4-flash','model_returned':'deepseek-flash','immutable_model_revision':None,'temperature':0.1,'max_tokens':3500,'thinking':'disabled',
          'hard_gates':summary['hard_gates'],'soft_metrics':summary['soft_metrics'],'error_tags':summary['error_tags'],
          'pro_diagnostic':{'attempts':15,'provider_success':10,'parse_success':5,'accepted':0,'provider_402':5,'parse_failures':5,'literal_quote_rejections':5,'status':'HISTORICAL_EXCLUDED'},
          'flash_initial_402':1,'technical_recovery_calls':14,'successful_answer_selection':False,'same_provider_model_family_bias':'UNMEASURED','human_calibrated':False},
      'human_eval':{'status':'READY','port':8686,'project_id':1,'project_title':'CourtVision Final V1 Historical Pregame Human','articles_ready':14,'tasks_open':1,'labels':0,'predictions':0,
          'current_sample':'cb-c55e9597dfab8e9de493','unlock':'Submit genuine annotation; python -m evaluation.finalization.human advance',
          'basic_agreement':'IMPLEMENTED_BUT_NOT_MEASURED','kappa':'NOT_IMPLEMENTED','human_human_IAA':'NOT_DONE','postgame_workbench':'CLOSED',
          'blind_from_judge_and_ground_truth':True,'verification_supports_future_annotations':True},
      'bad_cases':{'event_groups':87,'legacy_events':40,'failed_revision_sample_chains':47,'unattributed_events':39,'unique_root_cause_count':None,'all_resolved':False},
      'regression':{'cases':47,'unique_samples':20,'expected_accept':27,'expected_reject':20,'matched':47,'mismatched':0,'scope':'read-only pinnedV4 archived replay, not fresh model generation'},
      'phoenix':{'status':'DONE_EVALUATION_TRACING','port':8682,'historical_spans':138,'historical_model_attempt_spans':98,'historical_acceptance_spans':40,
          'final_attempt_spans':85,'final_provider_responses':79,'final_402':6,'total_reverified_spans':223,'production_monitoring':False},
      'metamorphic':{'status':'HISTORICAL_EXECUTED','target':'V4','audited_pairs':25,'executed_pairs':10,'model_calls':20,'completed_pairs':8,'incomplete_pairs':2,
          'consistent_pairs':8,'full_attempted_rate':'8/10','conditional_rate':'8/8','probability_flat_completed_pairs':4,'static_market_invariants':5,'causal_or_accuracy_claim':False},
      'agent_reach':{'status':'SHADOW_ONLY_NO_PRODUCTION_INTEGRATION','n_games':3,'global_urls':locate(shadow),'hard_fact_eligible':0,'certified_matchup_information':0,
          'policy_at_test':'strict historical availability proof, different from pragmatic Final Replay','decision':'do not integrate or enlarge now; more discovery did not prove downstream benefit'},
      'historical_pregame':{'revision':DATA,'n':20,'nominal_lead_minutes':20,'market_spread':20,'market_total':20,'market_type':'historical_pregame_market_reference','opening_closing':'unknown',
          'injury_complete_games':19,'injury_partial_games':1,'injury_rows':238,'research_sources':19,'research_games':13,'lineup_team_entries':40,
          'confirmed_lineup_teams':2,'source_expected_teams':10,'unknown_lineup_teams':28,'roster':'PARTIAL','pure':'UNAVAILABLE'},
      'resume_metrics':{'recommended':['Golden20/30teams','5hard+6soft Rubric,14 real Judge outputs','160 Final deterministic checks','87 event groups,47/47 archived contract matches'],
          'secondary':['85 attempt-level spans','300 static Benchmark','1,231 contexts +anonymous variants'],'do_not_lead_with':['allJudgeHardPASS','68or73 unit-test count','archive74545games','historical numeric accuracy unrelated to finalLLMcohort']},
      'safe_claims':safe_claims,'unsafe_claims':unsafe_claims,
      'remaining_gaps':{'P0_resolved':['14/20 production-equivalence ambiguity','T20 target versusT5 runtime documentation','154 quotes versus semantic facts','Kappa implemented versus pending ambiguity'],
          'P1':['Real independent Human labels and inspection of Judge false negatives','Can explain evaluation contracts, sampling and error attribution without tool-name recitation'],
          'P2':['Kappa implementation when actually useful','Forward cohort','Real productionT20 scheduling/final publication','Optional small Postgame content evaluation'],
          'DROP':['Another candidate/20game quality regeneration','300LLM batch','Full historical internet/Wayback archaeology','Agent Reach productionization','Full Postgame platform','More sentence-level validators']},
      'recommended_next_actions':['Stop CourtVision feature development and begin targeted applications/interview preparation.',
          'Spend the next2hours on chronological blind Human scoring of the first6–8 articles; then complete14 if useful.',
          'Use actual matched human/Judge labels for disagreement RCA and basic agreement; no new model generation needed.'],
      'postgame_scope_decision':{'recommendation':'Do not add Postgame Golden now. Existing deterministic record and3 writer artifacts support product scope, not content evaluation.',
          'minimal_future_option':'Only if a specific interview requires it: reuse3–5 existing frozen result/Article pairs with hindsight Rubric, no new paid generation.'},
      'audit':{'as_of_beijing':stamp,'llm_calls':0,'annotation_writes':0,'generation_revision_changes':0,'tests':audit_tests,
          'final_release_tests_historical':m['tests'],'independent_reviewers':3,'scope':'core systems and cited artifact roots, not every archive row or full repository test suite',
          'new_code_fix':'Closeout verifier accepts valid annotated chronological task prefixes while enforcing identity/hash-equivalent data and no automatic predictions.',
          'product_payloads_unchanged':True,'commit':False,'push':False},
      'evidence_index':{'final_metrics':'evaluation/finalization/closeout-judge-recovery-01/verified/metrics.json','current_derived_metrics':'evaluation/finalization/metrics.json',
          'judge_index':'evaluation/finalization/artifacts/golden-pregame-system-final-v1/judge-final-v1-provider-recovery-01/index.json',
          'human_tasks':'evaluation/finalization/human/tasks-qualified.json','rubric':'evaluation/golden_set/v1/rubric.json',
          'latest_audit_tests':'evaluation/finalization/final-audit-20261013-tests.xml','latest_audit_verification':'evaluation/finalization/final-audit-20261013-final-verification.json'}
    }
    target=ROOT/'evaluation/reports/FINAL_AI_HANDOFF.json';write(target,result)
    print(json.dumps({'handoff_json':str(target),'status':result['project_status'],'audit_tests':audit_tests},ensure_ascii=True))
if __name__=='__main__':main()
