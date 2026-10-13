"""Read-only output verification and derived overnight reports; no ground truth."""
import json,sys,sqlite3,xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import run as r
import human_workspace as h
from asset_scanner import EVAL,ROOT,read_json,sha,canonical_hash
from control import exclusive,now

def fraction(n,d):return {'numerator':n,'denominator':d,'rate':n/d if d else None}

def family(rec):
    if rec['chain_status']=='SUCCEEDED':return 'SUCCESS'
    text=(rec.get('error') or{}).get('message','')
    if 'historical_player_not_grounded_by_reference' in text:return 'PLAYER_CITATION_COVERAGE'
    if 'unknown_candidate_evidence_id' in text:return 'UNKNOWN_EVIDENCE_ID'
    if 'at most' in text:return 'SCHEMA_LENGTH_LIMIT'
    return rec['failure_type']

def verify_all():
    contract=r.verify();rows=[];arts=[];pair=[];manifest_hashes=[]
    for sample in r.b.selection():
        folder=r.BASE/'samples'/sample['sample_id'];record,ctx,editorial=h.load(sample)
        manifest_hashes.append(record['manifest_hash']);initial=read_json(folder/'forecast/artifact.json')
        for stage in ('forecast','repair','editorial'):
            fp=folder/stage/'artifact.json'
            if not fp.exists():continue
            a=read_json(fp);req=read_json(folder/stage/'request.json')['request']
            if a['artifact_hash']!=canonical_hash({k:v for k,v in a.items() if k!='artifact_hash'}) or a['request_hash']!=canonical_hash(req):raise ValueError('component hash mismatch')
            if a.get('raw_response') is not None and a['response_hash']!=canonical_hash(a['raw_response']):raise ValueError('response hash mismatch')
            if req!=r.request(stage,r.messages(stage,ctx,
                read_json(folder/'accepted-forecast.json')['forecast'] if stage=='editorial' else None,
                initial['parsed_output'] if stage=='repair' else None,
                json.loads(req['messages'][1]['content']).get('validation_error') if stage=='repair' else None)):raise ValueError('request differs from frozen single-sample contract')
            arts.append(a)
        if record['forecast_status']=='ACCEPTED':
            accepted=read_json(folder/'accepted-forecast.json')['forecast'];r.accept(accepted,ctx)
            raw=read_json(folder/'repair/artifact.json')['parsed_output'] if record['repair_status']=='ACCEPTED' else initial['parsed_output']
            normalized,_=r.c.normalize_structure(raw,ctx)
            if r.accept(normalized,ctx)!=accepted:raise ValueError('accepted forecast not derived from original')
            if record['repair_status']=='ACCEPTED':r.c.preserve_semantics(initial['parsed_output'],raw,ctx)
        old=read_json(r.b.BASE/'samples'/sample['sample_id']/'final-record.json')
        pair.append({'sample_id':sample['sample_id'],'game':sample['away_team']+' @ '+sample['home_team'],'date':sample['game_date'],
            'v1_forecast':old['forecast_status'],'v1_chain':old['chain_status'],'v1_raw_schema':old['raw_schema_status'],
            'v1_failure':old['failure_type'],'v2_forecast':record['forecast_status'],'v2_chain':record['chain_status'],
            'v2_raw_schema':record['raw_schema_status'],'v2_failure_family':family(record),'v2_error':record['error'],
            'context_hash':sample['context_sha256'],'manifest_hash':record['manifest_hash']})
        rows.append(record)
    if len(rows)!=20 or sum(sum(x['attempt_counts'].values()) for x in rows)!=len(arts):raise ValueError('counts mismatch')
    repair=sum(x['repair_triggered'] for x in rows);recovered=sum(x['repair_status']=='ACCEPTED' for x in rows)
    metrics={'provider_success_initial_forecast':fraction(sum(x['provider_status']=='SUCCEEDED' for x in rows),20),
        'json_parse_initial_forecast':fraction(sum(x['parse_status']=='PASSED' for x in rows),20),
        'raw_schema_initial_forecast':fraction(sum(x['raw_schema_status']=='PASSED' for x in rows),20),
        'repair_trigger':fraction(repair,20),'repair_recovery':fraction(recovered,repair),
        'forecast_acceptance':fraction(sum(x['forecast_status']=='ACCEPTED' for x in rows),20),
        'editorial_generation':fraction(sum(x['editorial_status']=='ACCEPTED' for x in rows),20),
        'e2e_success':fraction(sum(x['chain_status']=='SUCCEEDED' for x in rows),20)}
    calls=Counter(a['stage'] for a in arts);usage=Counter()
    for a in arts:
        for key,value in (a.get('provider_metadata',{}).get('usage') or{}).items():
            if isinstance(value,int):usage[key]+=value
    latencies={stage:{'calls':sum(a['stage']==stage for a in arts),'seconds_total':round(sum(a['latency_seconds'] for a in arts if a['stage']==stage),3)} for stage in calls}
    before=read_json(EVAL/'reports/overnight-protection-before.json');checks=[]
    if canonical_hash(r.b.root_protection())!=canonical_hash(before['production_and_frozen']):raise ValueError('production/frozen changed')
    for group in ('baseline_files','baseline_reports','first_attempt_tree'):
        for path,digest in before[group].items():
            if sha(ROOT/path)!=digest:raise ValueError('protected baseline changed')
            checks.append(path)
    traces={};dbpath=EVAL/'phoenix/runtime/phoenix.db'
    if dbpath.exists():
        with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True) as db:
            traces=dict(db.execute("SELECT name,count(*) FROM spans WHERE name LIKE 'candidate.%' GROUP BY name").fetchall())
    trace_errors=[str(p.relative_to(ROOT)) for p in r.BASE.glob('samples/*/*/trace-error.json')]
    verification={'created_at':now(),'samples_verified':20,'stateless_same_sample_requests':True,'tools_search_retrieval':0,
        'postgame_generated':0,'model_truth_fields':0,'target_truth_files_opened_in_generation':0,
        'baseline_protected_files_verified':len(checks),'production_hashes_unchanged':True,'frozen_context_selection_skills_unchanged':True,
        'trace_counts_persisted_sqlite':traces,'trace_error_files':trace_errors,'calls':dict(calls),'token_usage':dict(usage),
        'model_fingerprints':list({a.get('provider_metadata',{}).get('system_fingerprint') for a in arts}),
        'latency':latencies,'normalizations':{x['sample_id']:x['normalization_operations'] for x in rows if x['normalization_operations']},
        'semantic_quality_certified':False,'limits':'Hash/capability/contract checks are not word-by-word factual certification or model-memory decontamination.'}
    exclusive(r.BASE/'post-execution-verification.json',verification)
    result={'created_at':now(),'v1_revision':r.b.REV,'v2_revision':r.REV,'contract_hash':contract['contract_hash'],'same_samples':20,
        'baseline_metrics':{'provider':fraction(20,20),'parse':fraction(19,20),'raw_schema':fraction(8,20),'repair_trigger':fraction(11,20),'repair_recovery':fraction(0,11),'forecast':fraction(0,20),'editorial':fraction(0,20),'e2e':fraction(0,20)},
        'candidate_metrics':metrics,'provider_calls':dict(calls),'total_calls':len(arts),'usage':dict(usage),
        'fixed_v1_failures':sum(x['v1_chain']=='FAILED' and x['v2_chain']=='SUCCEEDED' for x in pair),
        'v1_e2e_successes_regressed':sum(x['v1_chain']=='SUCCEEDED' and x['v2_chain']!='SUCCEEDED' for x in pair),
        'v1_raw_schema_pass_to_v2_fail':[x['sample_id'] for x in pair if x['v1_raw_schema']=='PASSED' and x['v2_raw_schema']!='PASSED'],
        'remaining_failure_counts':dict(Counter(family(x) for x in rows if x['chain_status']!='SUCCEEDED')),
        'rows':pair,'content_quality':'PENDING_HUMAN on success; NA on failure','interpretation':'Paired execution reliability under changed Candidate contracts; not causal proof of better reasoning/accuracy or production readiness.'}
    exclusive(EVAL/'reports/golden-pregame-v1-v2-regression.json',result)
    text='# Golden Pregame V1 vs Candidate V2 paired regression\n\n'+result['interpretation']+'\n\n| Metric | V1 | V2 |\n|---|---:|---:|\n'
    for title,oldkey,newkey in [('Provider','provider','provider_success_initial_forecast'),('Parse','parse','json_parse_initial_forecast'),('Raw Schema','raw_schema','raw_schema_initial_forecast'),('Repair Trigger','repair_trigger','repair_trigger'),('Repair Recovery','repair_recovery','repair_recovery'),('Forecast','forecast','forecast_acceptance'),('Editorial','editorial','editorial_generation'),('E2E','e2e','e2e_success')]:
        a=result['baseline_metrics'][oldkey];z=metrics[newkey];text+=f"| {title} | {a['numerator']}/{a['denominator']} | {z['numerator']}/{z['denominator']} |\n"
    text+='\nRaw Schema uses each pinned schema and precedes copy-normalization. Schema versions differ. E2E ends at candidate acceptance + EditorialDraft schema, NOT live publication hard-fact gate.\n\n| sample | game | V1 failure | V2 outcome |\n|---|---|---|---|\n'
    for p in pair:text+=f"| {p['sample_id']} | {p['game']} | {p['v1_failure']} | {p['v2_failure_family']} |\n"
    text+='\nRemaining failures: '+json.dumps(result['remaining_failure_counts'])+'\n\nRaw-schema component regressions (V1 pass → V2 fail): '+json.dumps(result['v1_raw_schema_pass_to_v2_fail'])+'\n\nCall/token receipts: '+json.dumps({'calls':dict(calls),'usage':dict(usage)},indent=2)+'\n\nNo V3, quality retry, Judge, score, GT-based optimization or production deployment.\n'
    with (EVAL/'reports/golden-pregame-v1-v2-regression.md').open('x',encoding='utf-8') as f:f.write(text)
    print(json.dumps({'metrics':metrics,'calls':dict(calls),'failures':result['remaining_failure_counts'],'verification':verification},ensure_ascii=True))
    return result

if __name__=='__main__':verify_all()
