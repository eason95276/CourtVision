"""Release tree/history checks; reports locations, never secret values."""
import argparse,ast,hashlib,json,re,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
CHECKS={
 'private_user_path':re.compile(r'(?i)[A-Z]:[\\/]+Users[\\/]+[^\s/\\"\']+'),
 'private_key':re.compile(r'(?i:BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY)'),
 'secret_token':re.compile(r'(?i)(?:sk-[a-z0-9_-]{20,}|gh[pousr]_[a-z0-9]{30,}|github_pat_[a-z0-9_]{35,})'),
 'assigned_credential':re.compile(r'(?i)(?:api_key|password|access_token|auth_token)\s*[=:]\s*[\"\'][A-Za-z0-9+/=_-]{16,}[\"\']'),
}
EXCLUDE={'.git','.venv','__pycache__','node_modules','_site','.pytest_cache','.playwright-cli','output'}
def inspect(name,data,findings):
    if len(data)>5_000_000:findings.append({'file':name,'rule':'oversize'})
    if name.endswith(('.png','.jpg','.woff2')):return
    text=data.decode('utf-8',errors='replace')
    for label,pattern in CHECKS.items():
        for m in pattern.finditer(text):findings.append({'file':name,'line':text[:m.start()].count('\n')+1,'rule':label})
def run(history=False):
    findings=[];files=0;syntax=[]
    for p in ROOT.rglob('*'):
        if not p.is_file() or any(x in EXCLUDE for x in p.relative_to(ROOT).parts):continue
        name=p.relative_to(ROOT).as_posix();files+=1;data=p.read_bytes();inspect(name,data,findings)
        if name.endswith('.py'):
            try:ast.parse(data.decode('utf-8-sig'),filename=name)
            except SyntaxError as e:syntax.append({'file':name,'line':e.lineno})
        if p.suffix in {'.sqlite','.db','.pkl','.joblib','.pem','.key'} or p.name=='.env':findings.append({'file':name,'rule':'forbidden_asset'})
    blobs=0
    if history:
        rows=subprocess.check_output(['git','rev-list','--objects','--all'],cwd=ROOT,text=True).splitlines()
        for row in rows:
            obj,_,name=row.partition(' ')
            if not name:continue
            kind=subprocess.check_output(['git','cat-file','-t',obj],cwd=ROOT,text=True).strip()
            if kind=='blob':blobs+=1;inspect('history:'+name,subprocess.check_output(['git','cat-file','blob',obj],cwd=ROOT),findings)
    result={'files':files,'history_blobs':blobs,'findings':findings,'syntax_errors':syntax,'passed':not findings and not syntax}
    print(json.dumps(result,ensure_ascii=True));return result
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--history',action='store_true');a=p.parse_args();raise SystemExit(0 if run(a.history)['passed'] else 1)
