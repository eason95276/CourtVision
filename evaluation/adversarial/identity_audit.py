import sys,importlib.util,copy,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from attacks import base
from asset_scanner import EVAL
from build_eval_baseline import save
from closed_book_anonymize import anonymize,identity_leaks

def run():
    spec=importlib.util.spec_from_file_location('old_identity',EVAL/'adversarial/originals/closed_book_anonymize.py');old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    c,p=base();a,m=anonymize(c);attacks=[]
    for value in ('LAL','Los Angeles','Chase Center','https://example.test/identity','<local-configured-path>','2025-10-21T19:00:00Z','NBA:0022500001'):
        v=copy.deepcopy(a);v['task_mode']=value;attacks.append(('value:'+value,v))
    for key in ('abbreviation','city','arena','source_metadata','filename','ordering','debug','comments','espn_id','player_id'):
        v=copy.deepcopy(a);v[key]='opaque';attacks.append(('key:'+key,v))
    name=next(iter(m['player_names']));v=copy.deepcopy(a);v['task_mode']='-'.join(name.swapcase());attacks.append(('case_punctuation_name',v))
    rows=[{'attack':name,'before_rejected':bool(old.identity_leaks(v,m)),'after_rejected':bool(identity_leaks(v,m))} for name,v in attacks]
    save(EVAL/'reports/identity-attacks.json',rows);print(json.dumps({'scenarios':len(rows),'before_rejected':sum(r['before_rejected'] for r in rows),'after_rejected':sum(r['after_rejected'] for r in rows)}))

if __name__=='__main__':run()
