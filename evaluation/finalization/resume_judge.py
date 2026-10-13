"""Authorized provider-failure recovery; original Judge protocol stays frozen."""
import argparse,urllib.request
from .common import *
from . import judge_final,run

RECOVERY=BASE/'judge-final-v1-provider-recovery-01'
def eligible_samples():
    eligible={r['sample_id'] for r in read(HERE/'qualification.json')['rows'] if r['qualified_e2e']}
    return [s for s in run.verify()['samples'] if s['sample_id'] in eligible]
def main():
    judge_final.config();original_contract=read(judge_final.DEST/'contract.json')
    if original_contract['code_hash']!=digest(Path(judge_final.__file__).read_bytes()):raise ValueError('judge_protocol_source_drift')
    if not (RECOVERY/'authorization.json').exists():
        credentials()
        req=urllib.request.Request('https://api.deepseek.com/user/balance',headers={'Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY']})
        with urllib.request.urlopen(req,timeout=20) as r:balance=json.load(r)
        write(RECOVERY/'authorization.json',{'reason':'User explicitly confirmed account/credit handled; append-only technical recovery after HTTP402 with no returned model response',
            'authorized_at':now(),'original_judge_contract_hash':digest(original_contract),'parent_generation_revision':REV,'samples':[s['sample_id'] for s in eligible_samples()],
            'balance_available':balance.get('is_available'),'balance':[{'currency':b.get('currency'),'total_balance':b.get('total_balance')} for b in balance.get('balance_infos',[])],
            'forecast_editorial_calls':0,'quality_retries':0})
        print(json.dumps({'account_available':balance.get('is_available'),'eligible_articles':len(eligible_samples())}),flush=True)
        if not balance.get('is_available'):raise ValueError('provider_account_still_unavailable')
    parent_dest=judge_final.DEST
    # Only the output namespace changes. Model, prompt, schema, projection and
    # generation parameters are reused byte-for-byte; no protocol optimization.
    judge_final.DEST=RECOVERY
    rows=[]
    for s in eligible_samples():
        original=parent_dest/s['sample_id']/'artifact.json'
        if original.exists():
            prior=read(original)
            if prior['provider_status']=='SUCCEEDED':raise ValueError('successful_final_judge_must_not_be_regenerated')
            if (prior.get('error') or {}).get('message')!='provider_http_402':raise ValueError('recovery_not_authorized_for_this_failure')
            if (parent_dest/s['sample_id']/'raw-response.json').exists():raise ValueError('provider_failure_with_response_requires_review')
        a=judge_final.one(s)
        request=RECOVERY/s['sample_id']/'request.json'
        if original.exists() and read(request)['request_hash']!=read(original.parent/'request.json')['request_hash']:raise ValueError('technical_retry_request_changed')
        rows.append({'sample_id':s['sample_id'],'artifact_path':str((RECOVERY/s['sample_id']/'artifact.json').relative_to(ROOT)),
            'artifact_file_hash':digest((RECOVERY/s['sample_id']/'artifact.json').read_bytes()),'request_hash':a['request_hash'],
            'provider_status':a['provider_status'],'parse_status':a['parse_status'],'schema_status':a['raw_schema_status'],
            'accepted':a['raw_schema_status']=='PASSED' and a['error'] is None,'quote_audit_status':a.get('quote_audit_status'),'error':a.get('error')})
        if (a.get('error') or {}).get('message')=='provider_http_402':break
    if not (RECOVERY/'index.json').exists():write(RECOVERY/'index.json',{'revision':'judge-final-v1','recovery_run':'provider-recovery-01','n_eligible':len(eligible_samples()),
       'rows':rows,'original_contract_hash':digest(original_contract),'accepted_labels':sum(r['accepted'] for r in rows),'quality_retries':0,'pro_scores_pooled':False,'created_at':now()})
    print(json.dumps({'attempted':len(rows),'accepted':sum(r['accepted'] for r in rows)}))
if __name__=='__main__':main()
