"""Read-only generation preflight; never calls a provider."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'execution'))
from runner import load_profile,preflight
from control import Control
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--profile',type=Path,required=True)
    p.add_argument('--stage',choices=['pregame','postgame'],default='pregame');a=p.parse_args()
    print(json.dumps(preflight(load_profile(a.profile),a.stage,Control()),ensure_ascii=False,indent=2))
