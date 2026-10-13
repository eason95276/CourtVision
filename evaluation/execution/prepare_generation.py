"""Create an explicit immutable generation revision; no model calls."""
import argparse
from runner import prepare_profile
from control import Control
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--revision',required=True);p.add_argument('--model',default='deepseek-v4-flash')
    p.add_argument('--pregame-revision');a=p.parse_args()
    if a.pregame_revision:Control().assert_phase(('PREGAME_FROZEN',))
    else:Control().assert_phase(('PREPARED',))
    print(prepare_profile(a.revision,a.model,a.pregame_revision))
