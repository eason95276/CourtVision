"""Read-only market endpoint recognizes the same registered games as current API."""
from datetime import datetime,timezone,timedelta
from v2.pregame_system.prediction_versions import PredictionVersionStore
from .prediction_version_service import PredictionVersionService
from .server import build_market_response


def test_registered_game_without_market_is_unavailable_not_unknown(tmp_path):
    at=datetime(2026,10,4,0,tzinfo=timezone.utc)
    service=PredictionVersionService(store=PredictionVersionStore(tmp_path/"read.sqlite",clock=lambda:at))
    game={"id":"espn:499999999","game_type":"preseason","status":"scheduled",
        "tipoff":(at+timedelta(hours=7)).isoformat(),"home":{"id":1610612761,"name":"Raptors"},
        "away":{"id":1610612748,"name":"Heat"}}
    service.tasks.register_game(game)
    class ForbiddenSearch:
        def search(self,*args,**kwargs):raise AssertionError("ordinary GET must not research")
    status,payload=build_market_response(game["id"],data={"games":[]},version_service=service,searcher=ForbiddenSearch())
    assert status==200 and payload["status"]=="unavailable"
    assert build_market_response("espn:wrong",data={"games":[]},version_service=service)[0]==404
    with service.store.connect() as db:
        assert db.execute("SELECT count(*) FROM prediction_versions").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM generation_tasks").fetchone()[0]==0
