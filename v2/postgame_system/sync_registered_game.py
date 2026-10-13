"""Manual one-game final result/Postgame refresh; never generates pregame AI."""
import argparse
import json
from pathlib import Path
from v2.frontend.prediction_version_service import PredictionVersionService
from v2.pregame_system.sources import get
from v2.pregame_system.storage import new_run,save
from .version_service import PostgameVersionService


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('game_id');parser.add_argument('--source-root',type=Path);parser.add_argument('--required-base-version')
    args=parser.parse_args();service=PredictionVersionService();game=service.tasks.registered_game(args.game_id)
    if not game:raise ValueError('unknown_registered_game')
    root=new_run('postgame_registered_refresh')
    if args.source_root:
        summary=json.loads((args.source_root/'summary.json').read_text(encoding='utf-8'))
        receipt=json.loads((args.source_root/'source.json').read_text(encoding='utf-8'))
    else:
        summary,receipt=get('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='+args.game_id.removeprefix('espn:'))
    save(root/'source.json',receipt)
    if not summary:raise ValueError('postgame_source_unavailable')
    store=PostgameVersionService(service.store);rid,bundle=store.sync_result(game,summary,receipt)
    save(root/'bundle.json',bundle)
    post=store.publish(game,rid,required_base_version=args.required_base_version);save(root/'postgame.json',post)
    print(json.dumps({'status':'published','version_id':post['version_id'],'base':post['base_prediction_version_id'],
        'evaluation':post['evaluation'],'root':str(root)},ensure_ascii=True))
    return 0

if __name__=='__main__':raise SystemExit(main())
