"""Final evaluator-private file manifest. Does not publish production records."""
from asset_scanner import EVAL,sha,canonical_hash,read_json
from build_eval_baseline import save

def freeze():
    files={}
    for name in ('closed_book_contexts','closed_book_provenance','closed_book_ground_truth','anonymized','closed_book_exports'):
        for path in (EVAL/'datasets'/name).rglob('*'):
            if path.is_file():files[str(path.relative_to(EVAL))]=sha(path)
    for name in ('closed_book_candidates.jsonl','closed_book_candidates.csv'):
        path=EVAL/'datasets'/name;files[str(path.relative_to(EVAL))]=sha(path)
    scripts={str(p.relative_to(EVAL)):sha(p) for p in (EVAL/'scripts').glob('*.py') if p.name.startswith(('closed_book','build_closed_book','validate_closed_book','export_closed_book'))}
    body={'schema_version':'controlled-closed-book-freeze-v1','files':files,'builder_files':scripts,'visibility':'Evaluator-only integrity manifest; not a model input'}
    body['dataset_id']='controlled-cb-v1-'+canonical_hash(body)[:20]
    save(EVAL/'reports/closed-book-freeze-manifest.json',body);print(body['dataset_id'],len(files),'files frozen')

if __name__=='__main__':freeze()
