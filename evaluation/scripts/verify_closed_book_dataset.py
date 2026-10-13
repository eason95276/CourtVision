"""Reopen all generated files, not just builder in-memory objects."""
import json
from asset_scanner import EVAL,read_json,canonical_hash
from build_eval_baseline import save
from validate_closed_book_context import validate

def verify():
    from evaluation_integrity import verify_closedbook
    integrity=verify_closedbook(EVAL)
    rows=[json.loads(s) for s in (EVAL/'datasets/closed_book_candidates.jsonl').read_text(encoding='utf-8').splitlines()]
    manifest={r['sample_id']:r for r in (json.loads(s) for s in (EVAL/'datasets/anonymized/manifest.jsonl').read_text(encoding='utf-8').splitlines())}
    failures=[]
    for folder in ('closed_book_contexts','closed_book_provenance','closed_book_ground_truth','anonymized/contexts','anonymized/mappings'):
        assert len(list((EVAL/'datasets'/folder).glob('*.json')))==len(rows),'Orphan/missing JSON: '+folder
    for r in rows:
        cp=EVAL/r['context_path'];pp=EVAL/r['provenance_path'];gp=EVAL/r['ground_truth_path'];a=manifest[r['anonymous_sample_id']]
        context=read_json(cp);proof=read_json(pp);gt=read_json(gp);anon=read_json(EVAL/a['context_path']);mapping=read_json(EVAL/a['mapping_path'])
        errors=validate(context,proof,context_path=cp,truth_path=gp,expected_context_hash=r['context_sha256'])+validate(anon,proof,mapping,EVAL/a['context_path'],gp,expected_context_hash=a['context_sha256'])
        if canonical_hash(context)!=r['context_sha256']:errors.append('candidate_context_hash_mismatch')
        if canonical_hash(anon)!=a['context_sha256']:errors.append('anonymous_manifest_hash_mismatch')
        if gt['sample_id']!=r['sample_id'] or gt['target_game_id']!=r['target_game_id']:errors.append('hidden_ground_truth_wrong_identity')
        if errors:failures.append({'sample_id':r['sample_id'],'errors':errors})
    export_count=0
    for path in (EVAL/'datasets/closed_book_exports').glob('deepeval-*.jsonl'):
        for s in path.read_text(encoding='utf-8').splitlines():
            item=json.loads(s);assert item['actual_output'] is None and item['expected_output'] is None
            raw=json.dumps(item['context']);assert 'closed_book_ground_truth' not in raw and 'ground_truth_path' not in raw
            export_count+=1
    report={'normal_files_checked':len(rows),'anonymous_files_checked':len(rows),'provenance_and_gt_bindings_checked':len(rows),
        'null_output_export_rows_checked':export_count,'failed_samples':failures,'model_calls':0,'integrity':integrity}
    save(EVAL/'reports/closed-book-full-validation.json',report);print(json.dumps(report));assert not failures

if __name__=='__main__':verify()
