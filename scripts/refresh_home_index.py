"""Refresh lightweight projections of existing public records, not source versions."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from publish_game import home_record
def main():
    path=ROOT/'public_data/index.json';data=json.loads(path.read_text(encoding='utf-8'))
    for row in data['games']:
        record=json.loads((ROOT/'public_data'/row['file']).read_text(encoding='utf-8'))
        if record['public_sha256']!=row['public_sha256']:raise ValueError('public_record_hash_binding_invalid')
        row['home_record']=home_record(record)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');print({'home_records':len(data['games'])})
if __name__=='__main__':main()
