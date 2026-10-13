"""Refresh one game's display-only market reference, with no AI generation."""
import argparse
import json
from .prediction_version_service import PredictionVersionService
from .market_reference_store import MarketReferenceStore,MarketReferenceService


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('game_id');args=parser.parse_args()
    service=PredictionVersionService();game=service.tasks.registered_game(args.game_id)
    if not game:raise ValueError('unknown_registered_game')
    store=MarketReferenceStore(service.store);reference=MarketReferenceService(store).refresh(game)
    print(json.dumps({'status':'available'if reference else'unavailable','reference':reference},ensure_ascii=True))
    return 0

if __name__=='__main__':raise SystemExit(main())
