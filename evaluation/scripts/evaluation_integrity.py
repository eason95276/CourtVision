"""Anchored byte integrity and path boundaries; not an OS security sandbox."""
import csv,json
from pathlib import Path
from asset_scanner import EVAL,sha,read_json,canonical_hash

DATASET_ID='controlled-cb-v1-6d788de247e93eaaedf4'
GOLDEN_ID='golden-set-v1-4b0c7cfdf4032d8aba43'
DIRS=('closed_book_contexts','closed_book_provenance','closed_book_ground_truth','anonymized','closed_book_exports')

def inside(root,relative):
    root=Path(root).resolve();p=Path(root)/relative
    if p.is_absolute() and not p.resolve().is_relative_to(root):raise ValueError('path outside evaluation root')
    if not p.resolve().is_relative_to(root):raise ValueError('path traversal')
    for x in (p,*p.parents):
        if x==root.parent:break
        if x.is_symlink() or (hasattr(x,'is_junction') and x.is_junction()):raise ValueError('symlink/junction boundary')
    return p

def verify_manifest(root,manifest,expected_id,expected_manifest_hash=None,manifest_path=None):
    if manifest['dataset_id']!=expected_id:raise ValueError('dataset revision mismatch')
    body={k:v for k,v in manifest.items() if k!='dataset_id'}
    if 'controlled-cb-v1-'+canonical_hash(body)[:20]!=expected_id:raise ValueError('manifest content identity mismatch')
    if expected_manifest_hash and (manifest_path is None or sha(manifest_path)!=expected_manifest_hash):raise ValueError('manifest external byte pin mismatch')
    listed=set(manifest['files']);actual=set()
    for name in DIRS:
        folder=Path(root)/'datasets'/name
        for p in folder.rglob('*'):
            if p.is_file() or p.is_symlink():actual.add(str(p.relative_to(root)))
    for name in ('closed_book_candidates.jsonl','closed_book_candidates.csv'):
        p=Path(root)/'datasets'/name
        if p.exists():actual.add(str(p.relative_to(root)))
    if actual!=listed:raise ValueError('frozen inventory addition/deletion: '+str(sorted(actual^listed)[:3]))
    for relative,value in manifest['files'].items():
        p=inside(root,relative)
        if not p.is_file() or sha(p)!=value:raise ValueError('frozen file byte mismatch: '+relative)
    # Creator hashes stay immutable provenance. New validator code can legitimately
    # differ, but must not be mislabeled as the original producer.
    drift=[p for p,h in manifest['builder_files'].items() if not (Path(root)/p).is_file() or sha(Path(root)/p)!=h]
    return {'files_verified':len(listed),'creator_code_drift':drift,'data_revision_unchanged':True}

def verify_closedbook(root=EVAL):
    path=Path(root)/'reports/closed-book-freeze-manifest.json';pin=read_json(Path(root)/'reports/golden-v1-protection-before.json')['closed_book_manifest_sha256']
    return verify_manifest(root,read_json(path),DATASET_ID,pin,path)

def verify_golden(root=EVAL):
    folder=Path(root)/'golden_set/v1';path=folder/'selection_manifest.json';m=read_json(path)
    body={k:v for k,v in m.items() if k not in ('golden_set_revision','selection_manifest_hash','created_at')}
    if m['golden_set_revision']!=GOLDEN_ID or canonical_hash(body)!=m['selection_manifest_hash'] or GOLDEN_ID!='golden-set-v1-'+m['selection_manifest_hash'][:20]:raise ValueError('golden content/revision mismatch')
    pin=read_json(Path(root)/'reports/golden-set-v1-preparation.json')['selection_manifest_file_sha256']
    if sha(path)!=pin:raise ValueError('golden external byte pin mismatch')
    if canonical_hash(read_json(folder/'sampling_config.json'))!=m['sampling_config_hash']:raise ValueError('sampling config drift')
    if len(m['samples'])!=20 or len(set(m['selected_sample_ids']))!=20:raise ValueError('golden size/uniqueness')
    with (folder/'selection.csv').open(encoding='utf-8-sig',newline='') as file:rows=list(csv.DictReader(file))
    if len(rows)!=20:raise ValueError('golden CSV count drift')
    for row,sample in zip(rows,m['samples']):
        for k,v in sample.items():
            expected=json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else '' if v is None else str(v)
            if row.get(k)!=expected:raise ValueError('golden CSV/manifest divergence: '+k)
    return {'golden_revision':GOLDEN_ID,'games_verified':20,'manifest_and_csv_bound':True}

def load_model_context(sample_id,anonymous=False,root=EVAL):
    verify_closedbook(root)
    if '/' in sample_id or '\\' in sample_id or '..' in sample_id:raise ValueError('sample ID is not a path')
    index=Path(root)/'datasets'/('anonymized/manifest.jsonl' if anonymous else 'closed_book_candidates.jsonl')
    rows=[json.loads(s) for s in index.read_text(encoding='utf-8').splitlines()]
    row=next(r for r in rows if r['sample_id']==sample_id)
    p=inside(root,row['context_path']);allowed=Path(root)/'datasets'/('anonymized/contexts' if anonymous else 'closed_book_contexts')
    if not p.resolve().is_relative_to(allowed.resolve()):raise ValueError('non-model-visible context path')
    return read_json(p)
