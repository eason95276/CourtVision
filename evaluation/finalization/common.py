import hashlib,json,os,sys
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
HERE=Path(__file__).resolve().parent
REV='golden-pregame-system-final-v1'
DATA='historical-final-pregame-golden20-v1'
GOLD='golden-set-v1-4b0c7cfdf4032d8aba43'
BASE=HERE/'artifacts'/REV
sys.path.insert(0,str(ROOT/'evaluation/scripts'))
sys.path.insert(0,str(ROOT/'evaluation/execution'))

def digest(value):
    raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def now():return datetime.now(timezone.utc).isoformat()
def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
def credentials():
    # Existing project credential, never emitted to receipts or console.
    if not os.environ.get('DEEPSEEK_API_KEY'):
        for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
            if line.strip().startswith('DEEPSEEK_API_KEY='):
                os.environ['DEEPSEEK_API_KEY']=line.split('=',1)[1].strip().strip('"').strip("'")
    if not os.environ.get('DEEPSEEK_API_KEY'):raise ValueError('existing_deepseek_credential_missing')

FORBIDDEN={'ground_truth','hidden_outcome_bucket','selection_bucket','challenge_reason','target_final_score','target_winner','target_boxscore','postgame','actual_target_margin','actual_target_total'}
def check_visible(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if key.casefold() in FORBIDDEN:raise ValueError('private_target_key:'+key)
            check_visible(item)
    elif isinstance(value,list):
        for item in value:check_visible(item)

def protected():
    from evaluation.research_shadow.runner import protection
    result=protection()
    for prefix in ('evaluation/execution/baselines','evaluation/execution/candidates','evaluation/candidate_v2','evaluation/candidate_v3','evaluation/candidate_v4','evaluation/historical_t5/pilot-v1','evaluation/reports'):
        for p in (ROOT/prefix).rglob('*'):
            if p.is_file() and p.suffix not in {'.pyc','.log'}:
                result['files'][str(p.relative_to(ROOT))]=digest(p.read_bytes())
    return result
