import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from build_closed_book_dataset import protection
from asset_scanner import EVAL,sha,read_json
from build_eval_baseline import save

def main():
    p=argparse.ArgumentParser();p.add_argument('stage',choices=['before','after']);a=p.parse_args();data=protection()
    manifest=EVAL/'reports/closed-book-freeze-manifest.json';data['closed_book_manifest_sha256']=sha(manifest)
    frozen=read_json(manifest);data['closed_book_files']={f:sha(EVAL/f) for f in frozen['files']}
    save(EVAL/'reports'/('golden-v1-protection-'+a.stage+'.json'),data)
    if a.stage=='after':
        old=read_json(EVAL/'reports/golden-v1-protection-before.json');checks={k:data[k]==old[k] for k in data if k!='captured_at'}
        save(EVAL/'reports/golden-v1-protection-result.json',checks);assert all(checks.values());print(json.dumps(checks))

if __name__=='__main__':main()
