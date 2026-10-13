import sys,json,shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asset_scanner import EVAL,sha,read_json,protection_snapshot
from build_eval_baseline import save

def capture():
    body=protection_snapshot();manifest=EVAL/'reports/closed-book-freeze-manifest.json'
    body['frozen_manifest']=sha(manifest);frozen=read_json(manifest);body['frozen_files']={p:sha(EVAL/p) for p in frozen['files']}
    gs=EVAL/'golden_set/v1'
    keep=['selection_manifest.json','selection.csv','sampling_config.json','rubric.json','pregame_import.json','postgame_import.json','pregame_config.xml','postgame_config.xml','generation_profile.json','generation_dry_run.json','PREGAME_ANNOTATION_GUIDE.md','POSTGAME_ANNOTATION_GUIDE.md']
    body['golden_files']={p:sha(gs/p) for p in keep}
    return body

if __name__=='__main__':
    stage=sys.argv[1];now=capture();path=EVAL/'reports'/('adversarial-protection-'+stage+'.json')
    save(path,now)
    if stage=='before':
        folder=EVAL/'adversarial/originals';folder.mkdir(parents=True,exist_ok=True)
        for name in ('validate_closed_book_context.py','closed_book_anonymize.py','freeze_closed_book.py','build_closed_book_dataset.py'):
            shutil.copyfile(EVAL/'scripts'/name,folder/name)
    else:
        old=read_json(EVAL/'reports/adversarial-protection-before.json');checks={k:old[k]==now[k] for k in now if k!='captured_at'}
        save(EVAL/'reports/adversarial-protection-result.json',checks);assert all(checks.values());print(json.dumps(checks))
