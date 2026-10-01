from __future__ import annotations

from datetime import datetime, timedelta, timezone

from v2.pregame_system.prediction_versions import PredictionVersionStore

from .analysis_service import SavedGameAnalysisService
from .export_data import build_app_data
from .prediction_version_service import PredictionVersionService


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def game():
    return {
        "id": "espn:401909089",
        "status": "scheduled",
        "tipoff": "2026-10-20T23:00:00Z",
        "home": {"id": 1610612752, "name_zh": "尼克斯"},
        "away": {"id": 1610612755, "name_zh": "76人"},
        "relationship": {"final": {"win_probability": 0.6, "expected_margin": 4.5, "expected_total": 220}},
    }


class FakeAnalysisService:
    calls = 0
    fail = False
    cross_tipoff = False
    clock = None

    def __init__(self, data, *, analyst):
        self.data = data
        self.analyst = analyst

    def analyze(self, game_id, *, allow_lookup, cutoff, before_writer):
        type(self).calls += 1
        if type(self).fail:
            raise RuntimeError("mock_writer_failure")
        before_writer()
        frozen_at = type(self).clock.value + timedelta(seconds=1)
        generated_at = (
            datetime.fromisoformat("2026-10-20T23:00:01+00:00")
            if type(self).cross_tipoff
            else type(self).clock.value + timedelta(seconds=2)
        )
        type(self).clock.value = generated_at + timedelta(seconds=1)
        judgment = {
            "winner_lean": "home",
            "final_prediction": {
                "winner_side": "home",
                "win_probability": 0.63,
                "expected_margin": 6.5,
                "expected_total": 224.5,
                "evidence_ids": ["evidence-1"],
            },
            "analysis_article": "A complete fixture article.",
        }
        return {
            "game_id": game_id,
            "analysis_contract_version": "matchup-history-v1",
            "game_identity": {
                "game_id": game_id,
                "home_team_id": 1610612752,
                "away_team_id": 1610612755,
                "scheduled_tipoff": "2026-10-20T23:00:00+00:00",
            },
            "cutoff": cutoff,
            "input_frozen_at": frozen_at.isoformat(),
            "generated_at": generated_at.isoformat(),
            "judgment": judgment,
            "official_numeric_prediction": self.data["games"][0]["relationship"]["final"],
            "model_layers": {"pure_prediction": self.data["games"][0]["relationship"]["final"]},
            "input_snapshot": self.data["games"][0],
            "research": {"records": [{"id": "evidence-1", "snippet": "full selected research record"}]},
            "analyst_context": {"context_hash": "ctx-1", "objective_evidence": [{"id": "evidence-1"}]},
            "evidence_index": {"evidence-1": {"section": "web_context"}},
            "sources": [{"id": "evidence-1", "url": "https://example.com"}],
            "cited_sources": [{"id": "evidence-1", "url": "https://example.com"}],
            "context_hash": "ctx-1",
            "base_input_hash": "base-1",
            "search": {"status": "available"},
            "analyst_trace": {"status": "success"},
            "provenance": {"analyst_skill_hashes": {"forecasting": "f", "editorial": "e"}},
        }


def make_service(tmp_path):
    clock = Clock()
    FakeAnalysisService.calls = 0
    FakeAnalysisService.fail = False
    FakeAnalysisService.cross_tipoff = False
    FakeAnalysisService.clock = clock
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    return PredictionVersionService(
        store=store,
        clock=clock,
        service_factory=FakeAnalysisService,
        analyst_factory=lambda request: object(),
    ), store, clock


def test_generation_freezes_complete_input_and_publishes_one_version(tmp_path):
    service, store, _ = make_service(tmp_path)
    source = game()
    data = {"games": [source]}
    result = service.generate(data, source["id"], request_id="00000000-0000-0000-0000-000000000091")

    version = result["version"]
    assert result["status"] == "success"
    assert result["update_status"] == "published"
    assert version["payload_sha256"]
    assert version["input_cutoff_at"] <= version["input_frozen_at"] <= version["generated_at"] <= version["published_at"]
    assert version["request_seq"] == 1
    assert version["prediction"] == version["judgment"]["final_prediction"]
    assert version["research"]["records"][0]["snippet"] == "full selected research record"
    assert version["analyst_context"]["context_hash"] == "ctx-1"
    assert version["model_layers"]["pure_prediction"]
    assert version["provenance"]["analyst_skill_hashes"]["forecasting"] == "f"

    source["home"]["name_zh"] = "changed after publish"
    selected = service.selected(source)
    assert selected["version"]["input_snapshot"]["home"]["name_zh"] == "尼克斯"
    assert store.select_current(source)["version_id"] == version["version_id"]


def test_update_failure_keeps_previous_version_selected(tmp_path):
    service, store, _ = make_service(tmp_path)
    data = {"games": [game()]}
    first = service.generate(data, data["games"][0]["id"])
    FakeAnalysisService.fail = True
    try:
        service.generate(data, data["games"][0]["id"])
    except RuntimeError:
        pass
    selected = service.selected(data["games"][0])
    assert selected["version_id"] == first["version_id"]
    assert FakeAnalysisService.calls == 2
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM prediction_versions").fetchone()[0] == 1


def test_same_request_id_reuses_existing_version_without_running_analysis_again(tmp_path):
    service, _, _ = make_service(tmp_path)
    data = {"games": [game()]}
    request_id = "00000000-0000-0000-0000-000000000092"
    first = service.generate(data, data["games"][0]["id"], request_id=request_id)
    again = service.generate(data, data["games"][0]["id"], request_id=request_id)
    assert again["version_id"] == first["version_id"]
    assert again["update_status"] == "already_published"
    assert FakeAnalysisService.calls == 1


def test_post_start_get_locks_last_version_and_publish_attempt_fails(tmp_path):
    service, store, clock = make_service(tmp_path)
    data = {"games": [game()]}
    first = service.generate(data, data["games"][0]["id"])
    clock.value = datetime.fromisoformat(data["games"][0]["tipoff"].replace("Z", "+00:00"))
    selected = service.selected(data["games"][0])
    assert selected["status"] == "locked"
    assert selected["version_id"] == first["version_id"]
    try:
        service.generate(data, data["games"][0]["id"])
    except Exception:
        pass
    assert store.select_current(data["games"][0])["version_id"] == first["version_id"]
    assert FakeAnalysisService.calls == 1


def test_request_that_finishes_after_tipoff_cannot_be_published(tmp_path):
    service, store, clock = make_service(tmp_path)
    data = {"games": [game()]}
    FakeAnalysisService.cross_tipoff = True
    try:
        service.generate(data, data["games"][0]["id"])
    except Exception:
        pass
    locked = service.selected(data["games"][0])
    assert locked["status"] == "locked_unavailable"
    assert locked["version_id"] is None
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM prediction_versions").fetchone()[0] == 0


def test_saved_game_analysis_accepts_version_cutoff_and_freezes_before_writer():
    game_data = build_app_data()
    game = game_data["games"][0]
    cutoff = "2026-09-20T12:00:00+00:00"
    events = []

    class FakeJudgment:
        def model_dump(self):
            return {
                "final_prediction": {
                    "winner_side": "home", "win_probability": 0.55,
                    "expected_margin": 0.5, "expected_total": 221.5,
                },
                "analysis_article": "fixture article",
            }

    class FakeAnalyst:
        def judge(self, context):
            assert context.objective_evidence
            events.append("writer")
            return FakeJudgment(), {"status": "success", "path": "fake/response.json"}

    service = SavedGameAnalysisService(game_data, analyst=FakeAnalyst())
    def before_writer():
        events.append("inputs_frozen")

    result = service.analyze(
        game["id"], allow_lookup=False, cutoff=cutoff, before_writer=before_writer
    )
    assert events == ["inputs_frozen", "writer"]
    assert result["cutoff"] == cutoff
    assert result["input_frozen_at"] >= cutoff
    assert result["input_snapshot"]["id"] == game["id"]
    assert result["model_layers"]["official_numeric_prediction"] == game["relationship"]["final"]
    assert result["analyst_context"]["context_hash"] == result["context_hash"]
    assert result["official_numeric_prediction"] == game["relationship"]["final"]
