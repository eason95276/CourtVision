"""Receipt-derived closeout. Never turns a model attempt into an accepted label."""
import ast,json,sqlite3,statistics,subprocess,xml.etree.ElementTree as ET
from collections import Counter
from .common import *
from .run import verify,ctx_for
from .judge import schema as judge_schema
from .contracts import ExactValidator
OUTPUT=HERE

def judge_stats(root):
    rows=[]
    for p in root.glob('*/artifact.json'):
        a=read(p);struct=False
        if a.get('parsed_output'):
            try:ExactValidator(judge_schema()).validate(a['parsed_output']);struct=True
            except Exception:pass
        rows.append({'sample_id':a['sample_id'],'provider':a['provider_status'],'parse':a['parse_status'],'raw_schema_status':a['raw_schema_status'],
          'schema_only_replay':struct,'literal_quote_status':a.get('quote_audit_status') or ('FAILED' if (a.get('error') or {}).get('message')=='judge_quote_not_in_editorial' else 'NOT_RUN'),
          'error':a.get('error'),'artifact_path':str(p.relative_to(ROOT))})
    return {'attempts':len(rows),'provider_success':sum(r['provider']=='SUCCEEDED' for r in rows),'parse_success':sum(r['parse']=='PASSED' for r in rows),
        'schema_only_valid':sum(r['schema_only_replay'] for r in rows),'accepted_labels':sum(r['raw_schema_status']=='PASSED' and r['error'] is None for r in rows),'rows':rows,
        'no_scores_pooled_across_judge_revisions':True,'provisional_schema_replay_not_accepted_labels':True}

def trace_stats():
    dbpath=ROOT/'evaluation/phoenix/runtime/phoenix.db';paths=list(BASE.rglob('trace-receipt.json'));persisted=0;errors=[]
    with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True) as db:
        for p in paths:
            r=read(p)
            if not r.get('trace_id'):errors.append(str(p.relative_to(ROOT)));continue
            if db.execute('SELECT count(*) FROM spans s JOIN traces t ON s.trace_rowid=t.id WHERE t.trace_id=?',(r['trace_id'],)).fetchone()[0]:persisted+=1
            else:errors.append(str(p.relative_to(ROOT)))
        historical=read(ROOT/'evaluation/reports/phoenix-evaluation-workload.json');historic_found=0
        old_receipts=[]
        for revision in ('golden-pregame-system-candidate-v3','golden-pregame-system-candidate-v4'):
            old_receipts+=list((ROOT/'evaluation/execution/candidates'/revision).rglob('*trace-receipt.json'))
        old_receipts+=list((ROOT/'evaluation/metamorphic/v1').rglob('*trace-receipt.json'))
        for p in old_receipts:
            r=read(p)
            if r.get('trace_id') and db.execute('SELECT count(*) FROM spans s JOIN traces t ON s.trace_rowid=t.id WHERE t.trace_id=?',(r['trace_id'],)).fetchone()[0]:historic_found+=1
    return {'final_receipts':len(paths),'final_persisted':persisted,'missing_or_errors':errors,'historical_reported_spans':historical['persisted_spans_proved'],
        'historical_reopened_receipt_count':len(old_receipts),'historical_reverified_persisted':historic_found,'historical_reported_model_calls':historical['real_model_calls'],
        'historical_reported_acceptance_spans':historical['non_llm_chain_acceptance_spans'],'production_monitoring':False}

def regression_replay():
    from evaluation.candidate_v4 import execution_v4 as legacy
    from evaluation.candidate_v3 import analysis_tools as a
    from evaluation.candidate_v2.run import b
    rows=[]
    manifest=read(ROOT/'evaluation/regression/v1/manifest.json')
    for case in manifest['cases']:
        path=ROOT/case['original_artifact'];art=read(path)
        if digest(path.read_bytes())!=case['original_artifact_hash']:raise ValueError('legacy_regression_source_hash_drift')
        sample=next(s for s in b.selection() if s['sample_id']==case['sample_id'])
        loaded=a.load(case['source_revision'],sample);ctx=loaded['context']
        obj=loaded['initial']['parsed'];status='REPLAY_REJECTED';error=None
        if obj:
            try:norm,_=legacy.c.normalize(obj,ctx);legacy.accept(norm,ctx);status='REPLAY_ACCEPTED'
            except Exception as e:error=type(e).__name__+':'+str(e)[:240]
        else:error='Raw JSON unavailable; retained failure, no salvage.'
        expected=case['current_status'];rows.append({'case_id':case['case_id'],'observed':status,'expected':expected,'pass':status==expected,'error':error})
    report={'n':len(rows),'passed':sum(r['pass'] for r in rows),'failed':sum(not r['pass'] for r in rows),'skipped':0,'target':'replay pinned V4 archived acceptance/rejection contracts; not new Final E2E or repaired basketball reasoning','rows':rows}
    write(OUTPUT/'regression-replay.json',report);return report

def main():
    cfg=verify();records=[read(BASE/'samples'/s['sample_id']/'chain-manifest.json') for s in cfg['samples']]
    if len(records)!=20:raise ValueError('full20_denominator_required')
    stages=Counter();calls=Counter();usage=Counter();times=[];initials=[];all_arts=[]
    for r in records:
        stages[r['failure_stage'] or 'SUCCEEDED']+=1;calls.update(r['attempts'])
        folder=BASE/'samples'/r['sample_id'];initials.append(read(folder/'forecast/artifact.json'))
        for p in folder.glob('*/artifact.json'):all_arts.append(read(p))
        ctx_for(next(s for s in cfg['samples'] if s['sample_id']==r['sample_id']))
        for p,h in r['refs'].items():
            if digest((folder/p).read_bytes())!=h:raise ValueError('component_artifact_drift')
    for a in all_arts:
        usage.update({k:v for k,v in a.get('provider_metadata',{}).get('usage',{}).items() if isinstance(v,int)});times.append(a['latency_seconds'])
    data=read(HERE/'contexts'/DATA/'final-manifest.json');market=read(HERE/'market-coverage.json')
    before=read(HERE/'protection-before.json');after=protected();drift=[p for p,h in before['files'].items() if after['files'].get(p)!=h]
    versions_unchanged=digest(before['versions'])==digest(after['versions'])
    if drift or not versions_unchanged:raise ValueError('protected_asset_drift:'+str(drift[:3]))
    write(OUTPUT/'protection-after.json',{'protected_original_files':len(before['files']),'drift':drift,'published_payloads_unchanged':versions_unchanged,
        'prediction_versions':len(after['versions']['prediction_versions']),'postgame_versions':len(after['versions']['postgame_versions'])})
    test_path=OUTPUT/'tests.xml' if (OUTPUT/'tests.xml').exists() else HERE/'qualification-tests.xml'
    test=ET.parse(test_path).getroot();suite=test if test.tag=='testsuite' else test.find('testsuite');test_counts={k:int(suite.get(k,'0')) for k in ('tests','failures','errors','skipped')};test_counts['passed']=test_counts['tests']-test_counts['failures']-test_counts['errors']-test_counts['skipped']
    syntax=[]
    for p in HERE.glob('*.py'):ast.parse(p.read_text(encoding='utf-8'));syntax.append(p.name)
    diff=subprocess.run(['git','diff','--check'],cwd=ROOT,text=True,capture_output=True);write(OUTPUT/'diff-check.json',{'exit_code':diff.returncode,'stdout':diff.stdout,'stderr':diff.stderr})
    old_events=[json.loads(line) for line in (ROOT/'evaluation/bad_cases/v2/bad_cases_v2.jsonl').read_text(encoding='utf-8').splitlines()]
    regression=regression_replay();traces=trace_stats();human=read(HERE/'human/ready.json')
    pro=judge_stats(BASE/'judge');failed_final_judge=judge_stats(BASE/'judge-final-v1')
    recovery_root=BASE/'judge-final-v1-provider-recovery-01'
    final_judge=judge_stats(recovery_root) if (recovery_root/'index.json').exists() else failed_final_judge
    judge_summary=None
    if (recovery_root/'index.json').exists():
        from .judge_results import summarize
        judge_summary=summarize()
    qualification=read(HERE/'qualification.json');human=read(HERE/'human/ready-qualified.json')
    success=sum(r['chain_status']=='SUCCEEDED' for r in records);finalized=success>0 and final_judge['accepted_labels']==qualification['qualified_e2e'] and human['human_eval_ready']
    report={'created_at':now(),'status':'FINALIZED' if finalized else 'NOT_FINALIZED_JUDGE_BLOCKED','generation_revision':REV,'dataset_revision':DATA,'golden_set_revision':GOLD,
      'generation':{'n':20,'provider_success':sum(a['provider_status']=='SUCCEEDED' for a in initials),'parse_success':sum(a['parse_status']=='PASSED' for a in initials),
        'raw_schema_success':sum(a['raw_schema_status']=='PASSED' for a in initials),'forecast_accepted':sum(r['forecast_status']=='ACCEPTED' for r in records),
        'editorial_accepted':success,'e2e_success':success,'e2e_failure':20-success,'qualified_e2e_success':qualification['qualified_e2e'],'qualified_e2e_failure':20-qualification['qualified_e2e'],'qualification':qualification,'repair_triggered':sum(r['repair_triggered'] for r in records),
        'repair_recovered':sum(r.get('repair_status')=='ACCEPTED' for r in records),'component_attempts':dict(calls),'total_component_calls':sum(calls.values()),'failure_stages':dict(stages),
        'token_usage':dict(usage),'latency_p50':statistics.median(times)},
      'data':{'n':20,'market':market,'injury_status':dict(Counter(s['injury_status'] for s in data['samples'])),'injury_rows':sum(s['injury_rows'] for s in data['samples']),
          'research_sources':sum(s['research_sources'] for s in data['samples']),'research_games':sum(s['research_sources']>0 for s in data['samples']),
          'lineup_basis_teams':dict(Counter(b for s in data['samples'] for b in s['lineup_basis']))},
      'deepeval':read(HERE/'deepeval-final.json'),'judge_pro_diagnostic':pro,'judge_final_failed_provider_attempt':failed_final_judge,'judge_final':final_judge,'judge_summary':judge_summary,'human':human,
      'bad_case_event_groups':len(old_events),'bad_case_taxonomy':dict(Counter(e['failure_domain'] for e in old_events)),'regression':regression,'phoenix':traces,
      'tests':test_counts,'syntax_files':syntax,'diff_check':'PASSED' if diff.returncode==0 else 'FAILED','protection':read(OUTPUT/'protection-after.json'),
      'baseline_v1_e2e':0,'v4_e2e':14,'comparison_causal':False,'human_labels':0,'human_judge_agreement':None,
      'blockers':['DeepSeek HTTP 402 for5 Pro Judge requests and1 final Flash request; no authorized successful continuation yet.',
          'Pro diagnostic:5 parse failures and5 literal-quote validation failures; scores not accepted or pooled.'] if not finalized else [],
      'production_modified':False,'commit':False,'push':False}
    write(OUTPUT/'metrics.json',report)
    failures=[{'sample_id':r['sample_id'],'failure_stage':r['failure_stage'],'error':r['error'],'attempts':r['attempts']} for r in records if r['chain_status']!='SUCCEEDED']
    write(OUTPUT/'execution-failures.json',{'n_failures':len(failures),'denominator':20,'cases':failures})
    print(json.dumps({k:report[k] for k in ('status','generation','data','tests','protection')},ensure_ascii=True))
if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',default=str(HERE));args=parser.parse_args()
    OUTPUT=Path(args.output_dir).resolve()
    if not OUTPUT.is_relative_to(HERE):raise ValueError('closeout_output_outside_evaluation_scope')
    main()
