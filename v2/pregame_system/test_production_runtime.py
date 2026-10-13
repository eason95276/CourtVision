"""Offline asset contracts. Models are existing bundled artifacts, never fitted here."""
import copy
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from . import ROOT
from .prediction import Predictor
from .production_runtime import configured_root
from .runtime_assets import DEMO_ROOT, RuntimeAssetError
from courtvision.data import TEAM_ID_TO_ABBR
from v2.frontend.test_pregame_scheduler import no_external_services  # autouse: offline only


@pytest.fixture
def asset_root(tmp_path, monkeypatch):
    root = tmp_path / "external-assets"
    raw = root / "data/raw"
    models = root / "v2/pregame_system/outputs/production/test-version"
    raw.mkdir(parents=True)
    models.mkdir(parents=True)
    meta = json.loads((DEMO_ROOT / "pure/model.json").read_text(encoding="utf-8"))
    for info in meta["models"].values():
        src = DEMO_ROOT / "pure" / Path(info["path"]).name
        shutil.copyfile(src, models / src.name)
        info["path"] = src.name
    (models / "model.json").write_text(json.dumps(meta), encoding="utf-8")
    games, stats = [], []
    teams = list(TEAM_ID_TO_ABBR)
    for round_number in range(3):
        for i in range(15):
            home, away = teams[2*i], teams[2*i+1]
            gid = f"00225{round_number*15+i+1:05d}"
            date = f"2026-04-{round_number+1:02d} 19:00:00"
            games.append(dict(gameId=gid, gameDate=date, hometeamCity="Home", hometeamName="Team",
                hometeamId=home, awayteamCity="Away", awayteamName="Team", awayteamId=away,
                homeScore=110, awayScore=100, gameType="Regular Season"))
            for team, opponent, is_home in [(home, away, 1), (away, home, 0)]:
                stats.append(dict(gameId=gid, gameDate=date, teamId=team, opponentTeamId=opponent,
                    home=is_home, win=is_home, teamScore=110 if is_home else 100,
                    opponentScore=100 if is_home else 110, fieldGoalsAttempted=90, fieldGoalsMade=40,
                    fieldGoalsPercentage=40/90, threePointersAttempted=30, threePointersMade=10,
                    threePointersPercentage=1/3, freeThrowsAttempted=25, freeThrowsMade=20,
                    freeThrowsPercentage=.8, reboundsOffensive=10, reboundsTotal=45,
                    turnovers=12, numMinutes=240, gameType="Regular Season"))
    pd.DataFrame(games).to_csv(raw / "Games.csv", index=False)
    pd.DataFrame(stats).to_csv(raw / "TeamStatistics.csv", index=False)
    monkeypatch.setenv("COURTVISION_RUNTIME_ASSET_ROOT", str(root))
    monkeypatch.setenv("COURTVISION_PRODUCTION_MODEL", "")
    return root


def test_fixture_assets_are_ready_and_identified(asset_root):
    predictor = Predictor(require_production=True)
    assert predictor.demo_context is None and predictor.runtime_mode == "PRODUCTION"
    assert len(predictor.games) == 45
    assert len(predictor.runtime_asset_identity["teams_covered"]) == 30
    assert predictor.runtime_asset_identity["data_sources"]["Games.csv"]["sha256"]
    assert predictor.runtime_asset_identity["feature_schema_hash"]


@pytest.mark.parametrize("missing", ["Games.csv", "TeamStatistics.csv", "win_probability.joblib", "model.json"])
def test_missing_asset_fails_closed(asset_root, missing):
    path = next(asset_root.rglob(missing))
    path.rename(path.with_suffix(path.suffix + ".hidden"))
    with pytest.raises(RuntimeAssetError, match="registered_game_requires_production_runtime_assets") as error:
        Predictor(require_production=True)
    assert "runtime_check" in str(error.value)


@pytest.mark.parametrize("filename", ["Games.csv", "TeamStatistics.csv"])
def test_csv_schema_mismatch_fails_closed(asset_root, filename):
    (asset_root / "data/raw" / filename).write_text("unexpected\n1\n", encoding="utf-8")
    with pytest.raises(RuntimeAssetError, match="Usecols"):
        Predictor(require_production=True)


def test_model_schema_order_mismatch_fails_closed(asset_root):
    path = next(asset_root.rglob("model.json"))
    meta = json.loads(path.read_text())
    meta["models"]["expected_margin"]["feature_columns"].reverse()
    path.write_text(json.dumps(meta))
    with pytest.raises(RuntimeAssetError, match="schema/order mismatch"):
        Predictor(require_production=True)


def test_corrupted_model_fails_closed(asset_root):
    next(asset_root.rglob("expected_total.joblib")).write_bytes(b"not a model")
    with pytest.raises(RuntimeAssetError, match="hash-mismatched"):
        Predictor(require_production=True)


def test_hash_valid_but_unloadable_model_has_actionable_error(asset_root):
    from .storage import digest
    path = next(asset_root.rglob("expected_total.joblib"))
    path.write_bytes(b"not a serialized model")
    metadata = next(asset_root.rglob("model.json"))
    meta = json.loads(metadata.read_text())
    meta["models"]["expected_total"]["sha256"] = digest(path)
    metadata.write_text(json.dumps(meta))
    with pytest.raises(RuntimeAssetError, match="cannot load model") as error:
        Predictor(require_production=True)
    assert "runtime_check" in str(error.value)


def test_legacy_absolute_metadata_uses_verified_sibling_artifact(asset_root):
    path = next(asset_root.rglob("model.json"))
    meta = json.loads(path.read_text())
    for info in meta["models"].values():
        info["path"] = '<local-configured-path>' + Path(info["path"]).name
    path.write_text(json.dumps(meta))
    predictor = Predictor(require_production=True)
    assert all(Path(info["path"]).parent == path.parent for info in predictor.metadata["models"].values())


def test_feature_builder_incompatible_fails_closed(asset_root, monkeypatch):
    from . import prediction
    original = prediction.feature_row
    def without_elo(*args, **kwargs):
        row, groups, audit, prior = original(*args, **kwargs)
        return row.drop(columns=["elo_home"]), groups, audit, prior
    monkeypatch.setattr(prediction, "feature_row", without_elo)
    with pytest.raises(RuntimeAssetError, match="feature schema incompatible"):
        Predictor(require_production=True)


def test_missing_team_mapping_coverage_fails_closed(asset_root):
    raw = asset_root / "data/raw"
    games = pd.read_csv(raw / "Games.csv")
    removed_ids = set(games.loc[games.hometeamId == 1610612737, "gameId"])
    games.loc[~games.gameId.isin(removed_ids)].to_csv(raw / "Games.csv", index=False)
    stats = pd.read_csv(raw / "TeamStatistics.csv")
    stats.loc[~stats.gameId.isin(removed_ids)].to_csv(raw / "TeamStatistics.csv", index=False)
    with pytest.raises(RuntimeAssetError, match="missing modern team coverage"):
        Predictor(require_production=True)


def test_required_production_without_configuration_cannot_use_demo(monkeypatch):
    monkeypatch.setenv("COURTVISION_RUNTIME_ASSET_ROOT", "")
    monkeypatch.setenv("COURTVISION_PRODUCTION_MODEL", "")
    with pytest.raises(RuntimeAssetError, match="missing production model.json"):
        Predictor(require_production=True)


def test_explicit_demo_survives_configured_production(asset_root):
    predictor = Predictor(runtime_root=DEMO_ROOT)
    assert predictor.runtime_mode == "DEMO" and predictor.demo_context
    with pytest.raises(RuntimeAssetError, match="Demo is not allowed"):
        Predictor(runtime_root=DEMO_ROOT, require_production=True)


def test_configured_missing_root_does_not_fallback_to_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("COURTVISION_RUNTIME_ASSET_ROOT", str(tmp_path / "missing"))
    monkeypatch.setenv("COURTVISION_PRODUCTION_MODEL", "")
    with pytest.raises(RuntimeAssetError, match="missing production model.json"):
        Predictor()


def test_relative_root_is_project_anchored(monkeypatch, tmp_path):
    monkeypatch.setenv("COURTVISION_RUNTIME_ASSET_ROOT", "../asset-fixture")
    monkeypatch.chdir(tmp_path)
    assert configured_root() == (ROOT / "../asset-fixture").resolve()


def test_real_production_registered_scheduler_chain(tmp_path, monkeypatch):
    """Real external model/CSV/Player DB; temporary registration/version storage; fake AI only."""
    from v2.frontend.test_generation_player_knowledge_integration import Harness, packet
    from v2.frontend.generation_input import prepare_generation_data
    from v2.frontend.analysis_service import prediction_layers
    from v2.frontend import server
    from .player_knowledge import DEFAULT_DATABASE, PlayerKnowledgeStore
    from .storage import content_hash
    from v2.pregame_system import prediction, storage, agent

    if configured_root() is None or not DEFAULT_DATABASE.is_file():
        pytest.skip("Local external production assets and real Player Knowledge DB are required")
    # Keep all validation artifacts and reports in the test temp directory.
    def new_run(name):
        import uuid
        path = tmp_path / name / uuid.uuid4().hex
        path.mkdir(parents=True)
        return path
    for module in (prediction, storage, agent):
        monkeypatch.setattr(module, "new_run", new_run)
    h = Harness(tmp_path, monkeypatch, kind="preseason", knowledge=PlayerKnowledgeStore(DEFAULT_DATABASE))
    monkeypatch.setattr(prediction, "now", lambda: h.clock.at.isoformat())
    monkeypatch.setattr(agent, "now", lambda: h.clock.at.isoformat())
    captured = {}
    original_ask = agent.PregameAgent.ask
    def ask(self, *args, **kwargs):
        report, session = original_ask(self, *args, **kwargs)
        captured["report"] = report
        return report, session
    monkeypatch.setattr(agent.PregameAgent, "ask", ask)
    def prepare(data, game_id, *, cutoff):
        result = prepare_generation_data(data, game_id, cutoff=cutoff)
        row = next(g for g in result["games"] if g["id"] == game_id)
        captured["baseline"] = copy.deepcopy(row)
        captured["layers"] = prediction_layers(row)
        return result
    h.service.input_preparer = prepare
    result, selected = h.run()
    assert result["results"][0]["update_status"] == "published", json.dumps({"result": result, "captured": captured})
    version = selected["version"]
    baseline = captured["baseline"]
    identity = version["provenance"]["runtime_assets"]
    assert identity["runtime_mode"] == baseline["technical"]["runtime_mode"] == "PRODUCTION"
    assert identity == version["input_snapshot"]["technical"]["runtime_asset_identity"]
    assert identity["asset_identity_hash"] and identity["data_sources"]["TeamStatistics.csv"]["sha256"]
    assert version["model_layers"] == captured["layers"]  # Player Knowledge did not mutate numeric layers.
    pure = baseline["relationship"]["basketball"]
    assert 0 < pure["win_probability"] < 1 and pure["expected_total"] > 0
    assert baseline["home"]["id"] == 1610612752 and baseline["away"]["id"] == 1610612755
    knowledge = packet(version)
    assert set(knowledge["coverage"]["team_coverage"]) == {"1610612752", "1610612755"}
    assert knowledge["coverage"]["team_coverage"]["1610612752"]["collected_games"] == 82
    assert knowledge["coverage"]["team_coverage"]["1610612755"]["collected_games"] == 82
    status, response = server.build_prediction_versions_response(data={"games": []}, version_service=h.service)
    assert status == 200
    item = response["predictions"][h.game["id"]]
    assert item["version_id"] == selected["version_id"]
    frozen_hash = content_hash(version)
    assert content_hash(h.service.selected(h.game)["version"]) == frozen_hash
    # The real target is an ESPN synthetic registration: no result row was used.
    receipts = list((tmp_path / "snapshots").glob("*/inputs.json"))
    assert receipts
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["game"]["game_id"] == h.game["id"]
    assert receipt["runtime_asset_identity"] == identity
    assert not any(key in receipt["game"] for key in ("homeScore", "awayScore", "total_points"))
    # Real GET endpoint -> actual app.js first-load restore functions; no generation on read.
    import functools
    import subprocess
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen
    from v2.frontend.export_data import STATIC_ROOT
    from v2.frontend.test_prediction_version_restore import NODE_FRONTEND_PROBE
    monkeypatch.setattr(server, "_PREDICTION_VERSION_SERVICE", h.service)
    monkeypatch.setattr(server, "read_json", lambda path: {"games": []})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(server.Handler, directory=str(STATIC_ROOT)))
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        with urlopen(f"http://127.0.0.1:{httpd.server_port}/api/current-analyses", timeout=5) as reply:
            current = json.load(reply)
        with urlopen(f"http://127.0.0.1:{httpd.server_port}/app_data.json", timeout=5) as reply:
            data = json.load(reply)
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)
    assert current["predictions"][h.game["id"]]["version_id"] == selected["version_id"]
    assert shutil.which("node"), "Node is required for production frontend restore acceptance"
    probe = subprocess.run(["node", "-e", NODE_FRONTEND_PROBE, str(STATIC_ROOT / "app.js")],
        input=json.dumps({"game": data["games"][0], "response": current}), text=True, encoding="utf-8",
        capture_output=True, timeout=20, check=True)
    restored = json.loads(probe.stdout)
    assert restored["restoredVersionId"] == restored["displayedVersionId"] == selected["version_id"]
    assert restored["restoredArticle"] == version["judgment"]["analysis_article"]
    assert restored["displayedProbability"] == version["prediction"]["win_probability"]
    assert restored["displayedMargin"] == version["prediction"]["expected_margin"]
    assert restored["displayedTotal"] == version["prediction"]["expected_total"]
    baseline["technical"]["runtime_asset_identity"]["model_version"] = "future-mutation-sentinel"
    assert content_hash(h.service.selected(h.game)["version"]) == frozen_hash
    print(json.dumps({"game_id": h.game["id"], "baseline": pure,
          "runtime": identity, "player_counts": {side: len(knowledge["teams"][side].get("previous_season_players")
              or knowledge["teams"][side].get("historical_team_leaders") or [])
          for side in ("home", "away")}, "version_id": selected["version_id"]}, ensure_ascii=False))
