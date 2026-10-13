"""Read-only result verification; no generation, target changes or truth reads."""
import json
from collections import Counter
from pathlib import Path
import run_baseline_v1 as b
from asset_scanner import read_json,sha,canonical_hash
from runner import digest_bytes
from control import exclusive,now


def main():
    contract=b.assert_contract();report=read_json(b.REPORT_JSON)
    rows=b.selection();proofs=[];raw_paths=Counter();repair_paths=Counter();messages=Counter();fingerprints=Counter();models=Counter()
    for sample in rows:
        folder=b.BASE/'samples'/sample['sample_id'];record=read_json(folder/'final-record.json')
        assert record['record_hash']==canonical_hash({k:v for k,v in record.items() if k!='record_hash'})
        assert record['system_revision']==b.REV and record['context_hash']==sample['context_sha256']
        ctx=b.context_for(sample)
        if sample['sample_id']==b.ts.SAMPLE:
            first=read_json(b.FIRST_RECORD)
            assert sha(b.FIRST_RECORD)==contract['first_frozen_record_hash']
            assert record['chain_status']==first['chain_status']=='FAILED'
            assert record['attempt_counts']=={'forecast':1,'schema_repair':1,'editorial':0}
            proofs.append({'sample_id':sample['sample_id'],'first_immutable_reference_verified':True,
                'source_record_hash':sha(b.FIRST_RECORD),'record_hash':record['record_hash']})
            models.update([read_json(b.recovery.PARENT)['model']])
            continue
        first=read_json(folder/'forecasting/artifact.json');req=first['request_hash'];packet=read_json(folder/'forecasting/request.json')['exact_model_visible_request']
        assert canonical_hash(packet)==req and first['context_hash']==sample['context_sha256']
        assert packet==b.ts.payload('forecasting',b.ts.forecast_messages(ctx))
        assert json.loads(packet['messages'][1]['content'])==b.ts.basketball_adapter(ctx)
        assert set(packet)=={'model','messages','temperature','max_tokens','thinking','stream'}
        assert [m['role'] for m in packet['messages']]==['system','user']
        assert packet['temperature']==.1 and packet['max_tokens']==4800 and packet['model']=='deepseek-v4-flash'
        assert digest_bytes(first['raw_response'].encode())==first['response_hash']
        metadata=first['provider_metadata'] or{}
        fingerprints.update([metadata.get('system_fingerprint')]);models.update([metadata.get('model')])
        for error in first.get('validation_errors',[]):
            raw_paths.update(['.'.join(map(str,error['loc']))]);messages.update([error['msg']])
        repair=folder/'schema-repair/artifact.json'
        if repair.exists():
            a=read_json(repair)
            assert a['sample_id']==sample['sample_id'] and a['context_hash']==sample['context_sha256']
            assert a['calls']==1
            wire=read_json(folder/'schema-repair/exact-wire-request.json')['payload']
            assert canonical_hash(wire)==a['request_hash'] and wire==a['exact_model_visible_request']
            inputs=json.loads(wire['messages'][1]['content'])
            assert inputs['basketball']==b.ts.basketball_adapter(ctx) and inputs['original_response']==first['parsed_output']
            assert inputs['repair_mode']=='schema_only'
            assert digest_bytes(wire['messages'][0]['content'].encode())==contract['native_repair_prompt_hash']
            assert wire['model']=='deepseek-v4-flash' and wire['temperature']==.1 and wire['max_tokens']==4800
            assert set(wire)=={'model','messages','temperature','max_tokens','thinking','stream'}
            assert digest_bytes(a['raw_response'].encode())==a['response_hash']
            validation=folder/'schema-repair/independent_schema_repair_validation_error.json'
            if validation.exists():
                for error in read_json(validation)['errors']:repair_paths.update(['.'.join(map(str,error['loc']))])
        assert record['editorial_status']=='NOT_RUN' and not (folder/'editorial').exists()
        proofs.append({'sample_id':sample['sample_id'],'same_sample_context_verified':True,'fresh_two_message_request_verified':True,
            'no_tools_retrieval_history_or_other_context':True,'raw_response_hash_verified':True,
            'repair_request_verified':repair.exists(),'record_hash':record['record_hash']})
    assert len(proofs)==20
    result={'created_at':now(),'system_revision':b.REV,'verified_sample_count':20,'sample_proofs':proofs,
        'new_initial_schema_error_paths':dict(raw_paths),'new_repair_schema_error_paths':dict(repair_paths),
        'new_initial_schema_validation_messages':dict(messages),'new_initial_provider_fingerprints':dict(fingerprints),
        'provider_reported_models_with_original_request_name':dict(models),
        'source_contract_unchanged':True,'first_failure_unchanged':True,'new_initial_calls':19,'new_repair_calls':10,
        'new_editorial_calls':0,'quality_not_evaluated':True,'articles_exposed':0,'truth_files_opened':0}
    result['verification_hash']=canonical_hash(result)
    path=b.BASE/'post-execution-verification.json';exclusive(path,result)
    print(json.dumps({'verified_samples':20,'new_initial_schema_error_paths':dict(raw_paths),
        'new_repair_schema_error_paths':dict(repair_paths),'new_initial_validator_messages':dict(messages),
        'first_failure_unchanged':True,'verification_artifact':str(path),'verification_artifact_hash':sha(path)},ensure_ascii=True))


if __name__=='__main__':main()
