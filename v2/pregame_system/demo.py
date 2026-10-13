"""Run the fixed offline Prediction V2 scenario after bootstrap."""
import json
from .runtime_assets import DEMO_ROOT
from .prediction import Predictor
from .agent import PregameAgent


def run_demo(runtime_root=DEMO_ROOT):
    predictor=Predictor(runtime_root=runtime_root)
    agent=PregameAgent(predictor=predictor)
    context=predictor.demo_context
    report,_=agent.ask('offline deterministic demo',cutoff=context['cutoff'],
                      plan={'intent':'report','game_id':context['game']['game_id']})
    return {'demo_runtime':True,'notice':'Frozen real pregame features and synthetic market fixture; not an official live forecast',
            'report':report}


if __name__=='__main__':
    print(json.dumps(run_demo(),ensure_ascii=True,indent=2))
