"""Failure event ledger, archived replay regression and real workload trace reports."""
import csv,json,sys,sqlite3,statistics
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v3 import analysis_tools as a,final_workspace as h
from asset_scanner import EVAL,ROOT,read_json,sha,canonical_hash
from control import exclusive,now

def save_csv(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader()
        for row in rows:writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()})

def domain(row):
    record=row['record'];text=json.dumps(record.get('error') or record.get('failure_code') or '')
    initial=row['initial'];first_text=json.dumps(initial['artifact'].get('error') or '')
    if 'is not a multiple of 0.01' in first_text:return 'VALIDATOR','decimal_multipleOf_false_rejection','Exact valid decimal probability rejected by float JSON Schema implementation.'
    if initial['parsed'] is None:return 'MODEL_GENERATION','invalid_json_content','Malformed content despite successful provider envelope; generation/formatting origin not fully separated, not basketball-reasoning attribution.'
    if 'historical_player_not_grounded_by_reference' in text:return 'EVALUATION_ADAPTER','partial_player_citation_coverage','Names exist, partial refs not deterministically completed inV2.'
    if 'at most' in text:return 'PROMPT_CONTRACT','bounded_text_verbosity','Generated scalar fields exceeded existing caps; exact text preservation prevents free shortening.'
    if 'estimated_lineup_current_identity' in text:return 'EVALUATION_ADAPTER','historical_live_identity_contract','Historical observations cannot prove live current identity.'
    if 'semantic_repair' in text or 'schema_repair_dropped_core' in text:return 'REPAIR','core_preservation_or_metadata_conflict','Repair cannot satisfy strict preservation/schema simultaneously; inspect original first gate, do not assume changed basketball judgment.'
    if 'evidence' in text or 'enum' in text:return 'SCHEMA','evidence_enum_or_reference_contract','Alias not valid in exact per-sample catalog; no fuzzy correction.'
    return 'SCHEMA',record['failure_type'],'Observed schema/acceptance mismatch; exact RCA retained, causal attribution remains engineering review.'

def bad_cases():
    target=h.target()['target_revision'];events=[];regression=[]
    for old in map(json.loads,(EVAL/'bad_cases/bad_cases.jsonl').read_text(encoding='utf-8').splitlines()):
        typ=old['failure_type'];dom='OTHER_UNATTRIBUTED'
        if any(x in typ for x in ('schema','json')):dom='SCHEMA'
        elif 'identity' in typ:dom='VALIDATOR'
        elif any(x in typ for x in ('frontend','render')):dom='UI'
        events.append({'case_id':'legacy-'+old['case_id'],'source_revision':'legacy_inventory','sample_id':None,'failure_domain':dom,
            'failure_type':typ,'root_cause':old.get('root_cause_if_known') or 'Not established; legacy numeric errors are not automatically LLM reasoning failures.',
            'evidence':{'source_file':old['source_file'],'source_locator':old['source_locator'],'original_case_id':old['case_id']},
            'fix_revision':old.get('fix_if_known'),'regression_status':old.get('regression_status_if_known') or 'NOT_RERUN',
            'reproduced':False,'resolved':False,'issue_group':canonical_hash([old['source_file'],old['source_locator'],typ]),'scope':'Legacy receipt inventory; not new model execution or GT used for optimization.'})
    for revision in a.REVISIONS:
        for sample in a.v2.b.selection():
            row=a.load(revision,sample)
            if row['record']['chain_status'] in ('SUCCEEDED','ACCEPTED'):continue
            dom,typ,cause=domain(row);original=row['initial']['parsed'];accepted_replay=False;replay_error=None
            if original is not None:
                try:
                    module=a.module_for(target);norm,_=module.c.normalize(original,row['context']);module.accept(norm,row['context']);accepted_replay=True
                except Exception as exc:replay_error=type(exc).__name__+':'+str(exc)[:400]
            else:replay_error='Original JSON remains unparseable; no regex/deterministic rescue applied.'
            current=a.load(target,sample)['record'];cid='bad-'+canonical_hash([revision,sample['sample_id'],typ])[:24]
            events.append({'case_id':cid,'source_revision':revision,'sample_id':sample['sample_id'],'failure_domain':dom,'failure_type':typ,'root_cause':cause,
                'evidence':{'initial_artifact':row['initial']['path'],'sha256':row['initial']['file_hash'],'final_error':row['record'].get('error') or row['record'].get('failure_code')},
                'fix_revision':target if accepted_replay else None,'regression_status':'ARCHIVED_OUTPUT_ACCEPTED_BY_TARGET_ADAPTER' if accepted_replay else 'KNOWN_FAILURE_RETAINED',
                'reproduced':True,'resolved':accepted_replay,'issue_group':canonical_hash([revision,sample['sample_id'],typ]),
                'scope':'Actual captured model failure + read-only archived replay; not another model call. Resolved is evaluation contract only, not live production or global root-cause fix.'})
            regression.append({'case_id':cid,'source_revision':revision,'sample_id':sample['sample_id'],'original_failure':typ,'expected_property':'No prediction/text/name deletion; exact frozen citations/decimal numeric ticks; malformed/unsupported content fails closed.',
                'original_artifact':row['initial']['path'],'original_artifact_hash':row['initial']['file_hash'],'context_hash':sample['context_sha256'],
                'fixed_revision':target if accepted_replay else None,'current_status':'REPLAY_ACCEPTED' if accepted_replay else 'REPLAY_REJECTED',
                'replay_error':replay_error,'fresh_target_chain_status':current['chain_status'],'replay_not_fresh_llm':True})
    if len({x['issue_group'] for x in events})!=len(events):raise ValueError('duplicate exact failure event group')
    folder=EVAL/'bad_cases/v2';folder.mkdir(parents=True,exist_ok=True)
    with (folder/'bad_cases_v2.jsonl').open('x',encoding='utf-8') as f:
        for event in events:f.write(json.dumps(event,ensure_ascii=False)+'\n')
    save_csv(folder/'bad_cases_v2.csv',events);counts=Counter(x['failure_domain'] for x in events)
    with (folder/'taxonomy_report.md').open('x',encoding='utf-8') as f:
        f.write('# Bad Case V2 taxonomy\n\n'+str(len(events))+' event groups,40 legacy inventory +47 real failed revision/sample chains. Raw and repair symptoms within each chain are one event, not multiple cases. These are not87 distinct root causes. Shared root families remain grouped by failure_type; cross-revision events are retained to trace regression.\n\n'+json.dumps(dict(counts),indent=2)+'\n\nOTHER_UNATTRIBUTED preserves legacy unknown attribution; numerical prediction errors are not called LLM reasoning errors. Resolved means only archived output accepted under the chosen historical evaluation adapter; fresh target success and archived replay are separate fields. Golden target Ground Truth files were not opened for RCA/optimization. Legacy inventory includes historic numerical error/outcome fields; they were read only in the separate authorized bookkeeping process, deliberately omitted from the new ledger, and never used in Pregame optimization/model requests.')
    exclusive(EVAL/'regression/v1/manifest.json',{'created_at':now(),'target_revision':target,'n':len(regression),'cases':regression,'model_calls':0,'expected_property_not_outcome_accuracy':True})
    save_csv(EVAL/'regression/v1/cases.csv',regression)
    return {'events':len(events),'real_failed_chain_events':len(regression),'legacy_events':40,'domains':dict(counts),'archived_replay_accepted':sum(x['resolved'] for x in events)}

def percentile(values,p):
    if not values:return None
    values=sorted(values);x=(len(values)-1)*p;lo=int(x);hi=min(lo+1,len(values)-1);return round(values[lo]+(values[hi]-values[lo])*(x-lo),3)

def traces():
    arts=[];receipts=[];sources=[]
    for revision in a.REVISIONS[2:]:
        sources.extend((EVAL/'execution/candidates'/revision).glob('samples/*/*/artifact.json'))
        receipts.extend((EVAL/'execution/candidates'/revision).glob('samples/*/*/trace-receipt.json'))
        receipts.extend((EVAL/'execution/candidates'/revision).glob('samples/*/chain-trace-receipt.json'))
    sources.extend((EVAL/'metamorphic/v1').glob('pairs/*/*/artifact.json'));receipts.extend((EVAL/'metamorphic/v1').glob('pairs/*/*/trace-receipt.json'))
    tokens=Counter();by_stage={};errors=Counter()
    for file in sources:
        art=read_json(file);arts.append(art)
        for key,value in (art.get('provider_metadata',{}).get('usage') or{}).items():
            if isinstance(value,int):tokens[key]+=value
        if art.get('error'):errors[art['stage']]+=1
    dbpath=EVAL/'phoenix/runtime/phoenix.db';persisted=0;missing=[]
    with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True) as db:
        for file in receipts:
            receipt=read_json(file)
            if receipt.get('error'):missing.append(str(file.relative_to(ROOT)));continue
            found=db.execute('SELECT count(*) FROM spans s JOIN traces t ON s.trace_rowid=t.id WHERE t.trace_id=?',(receipt['trace_id'],)).fetchone()[0]
            if found:persisted+=1
            else:missing.append(str(file.relative_to(ROOT)))
    for stage in {x['stage'] for x in arts}:
        subset=[x for x in arts if x['stage']==stage];vals=[x['latency_seconds'] for x in subset];tu=Counter()
        for art in subset:
            for k,v in (art.get('provider_metadata',{}).get('usage') or{}).items():
                if isinstance(v,int):tu[k]+=v
        by_stage[stage]={'calls':len(subset),'latency_p50_seconds':percentile(vals,.5),'latency_p95_seconds':percentile(vals,.95),'token_usage':dict(tu),
            'component_contract_failures':sum(x['error'] is not None for x in subset)}
    latencies=[x['latency_seconds'] for x in arts];report={'created_at':now(),'real_model_calls':len(arts),'trace_receipts':len(receipts),'persisted_spans_proved':persisted,'missing':missing,
        'non_llm_chain_acceptance_spans':40,'by_stage':by_stage,'latency_p50_seconds':percentile(latencies,.5),'latency_p95_seconds':percentile(latencies,.95),
        'token_usage':dict(tokens),'component_failure_stage_distribution':dict(errors),'dollar_cost':None,'production_monitoring':False,
        'scope':'Evaluation only; component status, main-chain final acceptance spans, synthetic final acceptance in result journals. Token receipts, not billing estimates.'}
    exclusive(EVAL/'reports/phoenix-evaluation-workload.json',report)
    with (EVAL/'reports/phoenix-evaluation-workload.md').open('x',encoding='utf-8') as f:f.write('# Phoenix real Evaluation workload\n\n'+json.dumps(report,ensure_ascii=False,indent=2))
    return report

def blocked_promptfoo():
    report={'created_at':now(),'status':'BLOCKED','reason':'Production frontend server registers only AI-analysis/market POST, no stable isolated Game QA endpoint. Existing evaluation fixture is echo-like, not real Agent QA.',
        'code_evidence':{'path':'v2/frontend/server.py','sha256':sha(ROOT/'v2/frontend/server.py'),'route_location':'do_POST around785; GET routes around744'},
        'actual_red_team_cases_executed':0,'real_llm_calls':0,'new_game_qa_endpoint_built':False,
        'requested_categories':['Temporal Leakage','Prompt Injection','Mode Escalation','Cross-game Contamination','System Prompt Extraction','Missing Evidence Hallucination','Unauthorized Postgame','Ground Truth Request','Tool/Context Boundary'],
        'fixture_not_counted_as_red_team':True,'next_dependency':'Existing stable isolated real Game QA endpoint; no product reconstruction in this task.'}
    exclusive(EVAL/'reports/promptfoo-red-team.json',report)
    with (EVAL/'reports/promptfoo-red-team.md').open('x',encoding='utf-8') as f:f.write('# Promptfoo real Red Team\n\nBLOCKED.0 actual cases.\n\n'+json.dumps(report,ensure_ascii=False,indent=2))
    return report

if __name__=='__main__':
    command=sys.argv[1];result=bad_cases() if command=='badcases' else traces() if command=='traces' else blocked_promptfoo();print(json.dumps(result,ensure_ascii=True))
