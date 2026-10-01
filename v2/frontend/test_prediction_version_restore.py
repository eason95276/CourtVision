"""The saved two-stage response must survive the real frontend reload path."""
from __future__ import annotations

import copy
import functools
import json
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import pytest

from v2.pregame_system.prediction_versions import PredictionVersionStore

from . import analysis_service, server
from .analysis_service import SavedGameAnalysisService
from .export_data import STATIC_ROOT, build_app_data
from .prediction_version_service import PredictionVersionService


NODE_FRONTEND_PROBE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {game, response} = JSON.parse(fs.readFileSync(0, 'utf8'));
const app = fs.readFileSync(process.argv[1], 'utf8');
const names = ['activePrediction', 'acceptPredictionSelection', 'analysisCompatible', 'loadCurrentAnalyses'];
const functions = names.map((name) => {
  const prefix = name === 'loadCurrentAnalyses' ? 'async function ' : 'function ';
  const start = app.indexOf(prefix + name + '(');
  if (start < 0) throw new Error('frontend function missing: ' + name);
  const end = app.indexOf('\n}\n', start);
  if (end < 0) throw new Error('frontend function boundary missing: ' + name);
  return app.slice(start, end + 2);
});
const state = {
  data: {games: [game]}, currentAnalyses: new Map(),
  predictionStates: new Map(), aiFailures: new Set(),
};
const context = vm.createContext({
  state,
  fetch: async () => ({ok: true, json: async () => response}),
});
vm.runInContext(functions.join('\n'), context);
const selection = response.predictions[game.id];
const version = selection.version;
const postAccepted = context.acceptPredictionSelection(game.id, selection);
const postPrediction = context.activePrediction(game);
const postArticle = state.currentAnalyses.get(game.id)?.judgment?.analysis_article;
state.currentAnalyses.clear();
state.predictionStates.clear();
(async () => {
  await context.loadCurrentAnalyses();
  const restored = state.currentAnalyses.get(game.id);
  const displayed = context.activePrediction(game);
  const old = {...version, analysis_contract_version: 'matchup-history-v1', cutoff: version.input_cutoff_at};
  delete old.input_cutoff_at;
  const oldStored = {...version, analysis_contract_version: 'matchup-history-v1'};
  const badCutoff = {...version, input_cutoff_at: game.tipoff};
  const conflictingCutoffs = {...version, cutoff: '2026-01-01T00:00:00+00:00'};
  const badIdentity = {...version, game_identity: {...version.game_identity, home_team_id: -1}};
  const badSchedule = {...version, game_identity: {...version.game_identity, scheduled_tipoff: '2026-10-21T23:00:00+00:00'}};
  const badHash = {...version, payload_sha256: 'invalid'};
  const unknownContract = {...version, analysis_contract_version: 'unknown-v1'};
  const lowerRevision = {...selection, state_revision: selection.state_revision - 1};
  const fakeNewer = {...selection, state_revision: selection.state_revision + 1,
    version_id: 'other-version', version: {...version, version_id: 'other-version', game_id: 'other-game'}};
  const lowerAccepted = context.acceptPredictionSelection(game.id, lowerRevision);
  const incompatiblePostAccepted = context.acceptPredictionSelection(game.id, fakeNewer);
  console.log(JSON.stringify({
    postAccepted, postVersionId: postPrediction?.version_id, postArticle,
    restoredVersionId: restored?.version_id,
    restoredArticle: restored?.judgment?.analysis_article,
    displayedVersionId: displayed?.version_id,
    displayedProbability: displayed?.winner_probability,
    displayedMargin: displayed?.expected_home_margin,
    displayedTotal: displayed?.expected_total,
    selectionVersionId: state.predictionStates.get(game.id)?.version_id,
    oldCompatible: context.analysisCompatible(game, old),
    oldStoredCompatible: context.analysisCompatible(game, oldStored),
    badCutoffCompatible: context.analysisCompatible(game, badCutoff),
    conflictingCutoffsCompatible: context.analysisCompatible(game, conflictingCutoffs),
    badIdentityCompatible: context.analysisCompatible(game, badIdentity),
    badScheduleCompatible: context.analysisCompatible(game, badSchedule),
    badHashCompatible: context.analysisCompatible(game, badHash),
    unknownContractCompatible: context.analysisCompatible(game, unknownContract),
    lowerAccepted, incompatiblePostAccepted,
  }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


class OfflineTwoStageAnalyst:
    def __init__(self, clock):
        self.clock = clock

    def judge(self, context):
        class Judgment:
            def model_dump(self):
                return {
                    "final_prediction": {
                        "winner_side": "home", "win_probability": 0.615,
                        "expected_margin": 5.5, "expected_total": 222.5,
                        "evidence_ids": [],
                    },
                    "analysis_article": "Two-stage editorial article tied to this version.",
                }

        self.clock[0] += timedelta(seconds=1)
        return Judgment(), {"status": "success", "forecast_audit_path": "offline-two-stage-trace"}


class OfflineSavedGameAnalysisService(SavedGameAnalysisService):
    def analyze(self, game_id, *, allow_lookup, cutoff, before_writer):
        return super().analyze(
            game_id, allow_lookup=False, cutoff=cutoff, before_writer=before_writer
        )


def test_two_stage_publish_get_and_frontend_refresh_restore_same_version(tmp_path, monkeypatch):
    if shutil.which("node") is None:
        pytest.skip("Node.js is required to execute the actual frontend functions")

    data = build_app_data()
    game = next(item for item in data["games"] if item["id"] == "espn:401909089")
    clock = [datetime(2026, 10, 1, tzinfo=timezone.utc)]
    assert datetime.fromisoformat(game["tipoff"].replace("Z", "+00:00")) > clock[0]
    monkeypatch.setattr(analysis_service, "now", lambda: clock[0].isoformat())
    database = tmp_path / "prediction_versions.sqlite"
    store = PredictionVersionStore(database, clock=lambda: clock[0])
    service = PredictionVersionService(
        store=store,
        service_factory=OfflineSavedGameAnalysisService,
        analyst_factory=lambda request: OfflineTwoStageAnalyst(clock),
    )
    posted = service.generate(data, game["id"])
    assert posted["version"]["analysis_contract_version"] == "two-stage-forecast-v1"
    assert "cutoff" not in posted["version"]

    reopened = PredictionVersionService(
        store=PredictionVersionStore(database, clock=lambda: clock[0])
    )
    monkeypatch.setattr(server, "_PREDICTION_VERSION_SERVICE", reopened)
    monkeypatch.setattr(server, "read_json", lambda path: data)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(server.Handler, directory=str(STATIC_ROOT))
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{httpd.server_port}/api/current-analyses", timeout=10) as response:
            assert response.status == 200
            current = json.load(response)
        clock[0] = datetime.fromisoformat(game["tipoff"].replace("Z", "+00:00"))
        with urlopen(f"http://127.0.0.1:{httpd.server_port}/api/current-analyses", timeout=10) as response:
            assert response.status == 200
            locked = json.load(response)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)

    selection = current["predictions"][game["id"]]
    assert selection["status"] == "current"
    assert selection["version_id"] == posted["version_id"]
    assert selection["version"]["payload_sha256"] == posted["version"]["payload_sha256"]
    assert locked["predictions"][game["id"]]["status"] == "locked"
    assert locked["predictions"][game["id"]]["version_id"] == posted["version_id"]

    probe = subprocess.run(
        ["node", "-e", NODE_FRONTEND_PROBE, str(STATIC_ROOT / "app.js")],
        input=json.dumps({"game": copy.deepcopy(game), "response": current}, ensure_ascii=True),
        text=True, encoding="utf-8", capture_output=True, timeout=20, check=True,
    )
    browser = json.loads(probe.stdout)
    locked_probe = subprocess.run(
        ["node", "-e", NODE_FRONTEND_PROBE, str(STATIC_ROOT / "app.js")],
        input=json.dumps({"game": copy.deepcopy(game), "response": locked}, ensure_ascii=True),
        text=True, encoding="utf-8", capture_output=True, timeout=20, check=True,
    )
    locked_browser = json.loads(locked_probe.stdout)
    final = posted["version"]["judgment"]["final_prediction"]
    article = posted["version"]["judgment"]["analysis_article"]
    assert browser["postAccepted"] is True
    assert browser["postVersionId"] == posted["version_id"]
    assert browser["postArticle"] == article
    assert browser["restoredVersionId"] == posted["version_id"]
    assert browser["selectionVersionId"] == posted["version_id"]
    assert browser["displayedVersionId"] == posted["version_id"]
    assert browser["restoredArticle"] == article
    assert browser["displayedProbability"] == final["win_probability"]
    assert browser["displayedMargin"] == final["expected_margin"]
    assert browser["displayedTotal"] == final["expected_total"]
    assert browser["oldCompatible"] is True
    assert browser["oldStoredCompatible"] is True
    assert browser["badCutoffCompatible"] is False
    assert browser["conflictingCutoffsCompatible"] is False
    assert browser["badIdentityCompatible"] is False
    assert browser["badScheduleCompatible"] is False
    assert browser["badHashCompatible"] is False
    assert browser["unknownContractCompatible"] is False
    assert browser["lowerAccepted"] is False
    assert browser["incompatiblePostAccepted"] is False
    assert locked_browser["restoredVersionId"] == posted["version_id"]
    assert locked_browser["restoredArticle"] == article
    assert locked_browser["displayedVersionId"] == posted["version_id"]
