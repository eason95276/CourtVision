import subprocess,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asset_scanner import ROOT,EVAL
from build_eval_baseline import save
r=subprocess.run(['git','diff','--check'],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace')
issues=[]
for folder in (EVAL/'scripts',EVAL/'adversarial',EVAL/'golden_set/v1'):
    for p in folder.rglob('*'):
        if p.suffix not in {'.py','.md'} or any(x in p.parts for x in ('runtime','originals','__pycache__','.playwright-cli')):continue
        for n,line in enumerate(p.read_text(encoding='utf-8-sig').splitlines(),1):
            if line.rstrip()!=line:issues.append({'file':str(p),'line':n})
save(EVAL/'reports/adversarial-diff-check.json',{'git_diff_check_exit_code':r.returncode,'output':r.stdout+r.stderr,'scope':'Tracked working-tree diff. Evaluation is untracked, so its source whitespace was separately scanned.',
    'evaluation_source_whitespace_issues':issues})
print('git_diff_check',r.returncode,'untracked_source_whitespace_issues',len(issues))
