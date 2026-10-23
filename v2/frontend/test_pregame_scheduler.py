from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from v2.pregame_system.generation_tasks import GenerationTaskStore, stage_windows
from v2.pregame_system.official_final import FINAL_PREGAME_LEAD
from v2.pregame_system.prediction_versions import PredictionVersionStore, VersionStoreError, game_identity
from v2.pregame_system.editorial_test_helpers import seal_fixture_payload, protocol_trace

from .generation_input import merge_registered_games, prepare_generation_data
from .prediction_version_service import PredictionVersionService
from .pregame_scheduler import PregameScheduler, main
from .server import build_ai_response, build_prediction_versions_response
from v2.pregame_system.scheduler_policy import SchedulerPolicyPlanner


TIP = datetime(2026, 10, 20, 23, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    import urllib.request
    from urllib.parse import urlparse
    original_open = urllib.request.OpenerDirector.open
    def local_only(opener, request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise AssertionError("Scheduler tests cannot call external providers")
        return original_open(opener, request, *args, **kwargs)
    def blocked(*args, **kwargs):
        raise AssertionError("Scheduler tests cannot call external providers")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setattr("v2.frontend.server.load_local_env", lambda *args: None)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", local_only)
    monkeypatch.setattr("requests.Session.request", blocked)


class Clock:
    def __init__(self, at=TIP - timedelta(days=4)):
        self.at = at

    def __call__(self):
        return self.at


def game(kind="regular_season"):
    return {
        "id": "espn:401909089", "status": "scheduled", "tipoff": TIP.isoformat(), "game_type": kind,
        "home": {"id": 1610612752, "name": "Knicks", "name_zh": "尼克斯"},
        "away": {"id": 1610612755, "name": "76ers", "name_zh": "76人"},
        "relationship": {"final": {"win_probability": 0.6, "expected_margin": 4.5, "expected_total": 220}},
        "prediction": {"available": True, "winner_team_id": 1610612752, "winner_probability": 0.6},
    }


def offline_judgment(final, article):
    return {
        "winner_lean": final["winner_side"], "side_lean": "no_lean", "total_lean": "no_lean",
        "confidence": "low", "supporting_evidence_ids": [], "conflicting_evidence_ids": [],
        "baseline_agreement": {"pure": "not_applicable", "market": "not_applicable", "residual": "not_applicable"},
        "subjective_rationale": "Offline scheduler fixture judgment.",
        "pregame_story": {"title": "Offline fixture", "statement": "Offline scheduler fixture story.",
            "classification": "subjective_analysis", "evidence_ids": []},
        "watch_themes": [
            {"title": "Offline theme one", "statement": "Offline scheduler fixture theme.",
             "classification": "subjective_analysis", "evidence_ids": []},
            {"title": "Offline theme two", "statement": "Another scheduler fixture theme.",
             "classification": "subjective_analysis", "evidence_ids": []}],
        "analysis_article": article, "analysis_article_evidence_ids": [],
        "final_prediction": {**final, "evidence_ids": []},
        "classification": "subjective_analyst_judgment",
    }


class GeneratorState:
    def __init__(self, clock):
        self.clock = clock
        self.calls = 0
        self.fail = False
        self.finish_at = None
        self.entered = threading.Event()
        self.release = None

    def factory(self, data, *, analyst):
        state = self

        class Analysis:
            def analyze(self, game_id, *, cutoff, before_writer, allow_lookup):
                state.calls += 1
                state.entered.set()
                before_writer()
                if state.release is not None:
                    assert state.release.wait(timeout=10)
                if state.fail:
                    raise RuntimeError("offline_generator_failure")
                selected = next(item for item in data["games"] if item["id"] == game_id)
                requested = datetime.fromisoformat(cutoff)
                frozen = requested + timedelta(seconds=1)
                generated = state.finish_at or requested + timedelta(seconds=2)
                state.clock.at = generated + timedelta(seconds=1)
                final = {"winner_side": "home", "win_probability": 0.63,
                         "expected_margin": 6.5, "expected_total": 224.5}
                result = {
                    "analysis_contract_version": "two-stage-forecast-v1", "cutoff": cutoff,
                    "input_frozen_at": frozen.isoformat(), "generated_at": generated.isoformat(),
                    "judgment": {"final_prediction": final, "analysis_article": f"Article call {state.calls}"},
                    "official_numeric_prediction": selected["relationship"]["final"],
                    "model_layers": {"pure_prediction": selected["relationship"]["final"]},
                    "input_snapshot": copy.deepcopy(selected), "analyst_context": {"cutoff": cutoff},
                    "research": {"records": []}, "evidence_index": {}, "sources": [], "cited_sources": [],
                    "context_hash": "offline-context", "base_input_hash": "offline-base",
                    "search": {"status": "not_requested"}, "analyst_trace": {}, "provenance": {},
                }
                from v2.pregame_system.editorial_test_helpers import seal_fixture_payload
                result.update(game_id=game_id, game_identity={"game_id":game_id,
                    "home_team_id":selected["home"]["id"],"away_team_id":selected["away"]["id"],"scheduled_tipoff":selected["tipoff"]},prediction=final)
                return seal_fixture_payload(result)

        return Analysis()


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    versions = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    generator = GeneratorState(clock)
    service = PredictionVersionService(store=versions, service_factory=generator.factory,
                                       analyst_factory=lambda request: object())
    data = {"games": [game()]}
    scheduler = PregameScheduler(service)
    registered = service.tasks.register_game(data["games"][0])["game"]
    configured = SchedulerPolicyPlanner(service.tasks).configure(registered["id"],
        {"preheat_enabled": True, "preheat_reason": "offline scheduler fixture", "preheat_lead_hours": 72})
    SchedulerPolicyPlanner(service.tasks).plan_game(configured)
    return service, scheduler, generator, clock, data


def stage_task(service, stage):
    if stage == "UPDATED_PREVIEW":
        registered = service.tasks.registered_games()[0]
        at = service.store.clock()
        with service.store.connect() as db:
            existing = db.execute("SELECT * FROM generation_tasks WHERE stage='UPDATED_PREVIEW'").fetchone()
        if existing is None:
            SchedulerPolicyPlanner(service.tasks).material_update({"game_id": registered["id"], "schedule_revision": registered["schedule_revision"],
                "event_type": "availability_change", "affected_object": {"player_id": "fixture-core"},
                "old_state": "unknown", "new_state": "Out", "evidence_batch_id": "scheduler-fixture", "evidence_ids": ["verified-fixture"],
                "observed_at": at.isoformat(), "reason": "verified fixture material change"}, admin_confirmed=True)
    with service.store.connect() as db:
        return dict(db.execute("SELECT * FROM generation_tasks WHERE stage=?", (stage,)).fetchone())


def test_repeat_registration_has_exactly_one_scheduled_task_per_stage(setup):
    service, _, _, _, data = setup
    ids = {task["task_id"] for task in service.tasks.ensure_schedule(data["games"][0])["tasks"]}
    assert len(ids) == 2
    assert ids == {task["task_id"] for task in service.tasks.ensure_schedule(data["games"][0])["tasks"]}
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0] == 2


def test_stage_targets_reuse_official_final_five_minute_rule():
    windows = stage_windows(TIP.isoformat())
    assert datetime.fromisoformat(windows["EARLY_PREVIEW"][0]) == TIP - timedelta(days=3)
    assert datetime.fromisoformat(windows["UPDATED_PREVIEW"][0]) == TIP - timedelta(hours=24)
    assert datetime.fromisoformat(windows["FINAL_PREGAME"][0]) == TIP - FINAL_PREGAME_LEAD


def test_concurrent_registration_uses_one_shared_schedule(setup):
    service, _, _, clock, data = setup
    other = GenerationTaskStore(PredictionVersionStore(service.store.path, clock=clock))
    barrier = threading.Barrier(2)
    results = []
    def register(tasks):
        barrier.wait(timeout=5)
        results.append(tasks.register_game(data["games"][0]))
    threads = [threading.Thread(target=register, args=(tasks,)) for tasks in (service.tasks, other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert {task["task_id"] for task in results[0]["tasks"]} == {task["task_id"] for task in results[1]["tasks"]}


def test_due_early_executes_once_and_not_before_due(setup):
    service, scheduler, generator, clock, data = setup
    assert scheduler.run_due_tasks(data)["due_count"] == 0
    assert generator.calls == 0
    clock.at = TIP - timedelta(days=3)
    result = scheduler.run_due_tasks(data)
    assert result["results"][0]["task"]["status"] == "succeeded"
    assert generator.calls == 1
    assert scheduler.run_due_tasks(data)["due_count"] == 0
    assert generator.calls == 1
    version = service.selected(data["games"][0])["version"]
    assert version["stage"] == "EARLY_PREVIEW"
    assert version["trigger"]["type"] == "scheduled"
    assert version["task_id"] == stage_task(service, "EARLY_PREVIEW")["task_id"]


def test_late_first_registration_marks_early_missed_but_updated_runs(tmp_path):
    clock = Clock(TIP - timedelta(days=2))
    state = GeneratorState(clock)
    service = PredictionVersionService(store=PredictionVersionStore(tmp_path / "v.sqlite", clock=clock),
        service_factory=state.factory, analyst_factory=lambda request: object())
    data = {"games": [game()]}
    registered = service.tasks.register_game(data["games"][0])
    assert registered["tasks"] == []
    clock.at = TIP - timedelta(hours=24)
    assert PregameScheduler(service).run_due_tasks(data)["results"][0]["task"]["stage"] == "EARLY_PREVIEW"
    assert state.calls == 1
    assert service.selected(data["games"][0])["version"]["published_at"] > (TIP - timedelta(hours=24)).isoformat()


def test_late_tick_skips_older_window_without_fabricating_history(setup):
    service, scheduler, generator, clock, data = setup
    clock.at = TIP - timedelta(hours=24)
    scheduler.run_due_tasks(data)
    assert stage_task(service, "EARLY_PREVIEW")["status"] == "succeeded"
    assert service.selected(data["games"][0])["version"]["stage"] == "EARLY_PREVIEW"
    assert generator.calls == 1


def test_two_independent_claimers_only_one_wins(setup):
    service, _, _, clock, _ = setup
    clock.at = TIP - timedelta(days=3)
    other = GenerationTaskStore(PredictionVersionStore(service.store.path, clock=clock))
    task_id = stage_task(service, "EARLY_PREVIEW")["task_id"]
    barrier = threading.Barrier(2)
    results = []
    def claim(tasks):
        barrier.wait(timeout=5)
        results.append(tasks.claim(task_id))
    threads = [threading.Thread(target=claim, args=(tasks,)) for tasks in (service.tasks, other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len([value for value in results if value is not None]) == 1


def test_manual_request_shares_scheduler_running_work(setup):
    service, scheduler, generator, clock, data = setup
    clock.at = TIP - timedelta(days=3)
    generator.release = threading.Event()
    results = []
    worker = threading.Thread(target=lambda: results.append(scheduler.run_due_tasks(data)))
    worker.start()
    assert generator.entered.wait(timeout=5)
    status, manual = build_ai_response(data["games"][0]["id"], data=data, api_key="offline-fixture", version_service=service)
    assert status == 200
    assert manual["update_status"] == "read_only"
    assert manual["generation_task"]["stage"] == "EARLY_PREVIEW"
    generator.release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert generator.calls == 1
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM prediction_versions").fetchone()[0] == 1


def test_simultaneous_manual_consumers_share_one_expensive_generation(setup):
    service, _, generator, clock, data = setup
    clock.at = TIP - timedelta(days=3)
    generator.release = threading.Event()
    answers = []
    worker = threading.Thread(target=lambda: answers.append(service.generate(data, data["games"][0]["id"])))
    worker.start()
    assert generator.entered.wait(timeout=5)
    other = PredictionVersionService(store=PredictionVersionStore(service.store.path, clock=service.clock),
        service_factory=generator.factory, analyst_factory=lambda request: object())
    assert other.generate(data, data["games"][0]["id"])["update_status"] == "running"
    generator.release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert generator.calls == 1


def test_failed_update_keeps_previous_success_and_retry_is_delayed(setup):
    service, scheduler, generator, clock, data = setup
    clock.at = TIP - timedelta(days=3)
    scheduler.run_due_tasks(data)
    first = service.selected(data["games"][0])["version_id"]
    clock.at = TIP - timedelta(hours=24)
    generator.fail = True
    stage_task(service, "UPDATED_PREVIEW")
    result = scheduler.run_due_tasks(data)
    assert result["results"][0]["task"]["status"] == "failed"
    assert service.selected(data["games"][0])["version_id"] == first
    assert scheduler.run_due_tasks(data)["due_count"] == 0
    assert generator.calls == 2
    generator.fail = False
    clock.at += timedelta(seconds=30)
    scheduler.run_due_tasks(data)
    assert service.selected(data["games"][0])["version_id"] != first


def test_retry_count_is_bounded_at_three(setup):
    service, scheduler, generator, clock, data = setup
    clock.at = TIP - timedelta(days=3)
    generator.fail = True
    for wait in (0, 30, 60):
        clock.at += timedelta(seconds=wait)
        scheduler.run_due_tasks(data)
    clock.at += timedelta(minutes=20)
    assert scheduler.run_due_tasks(data)["due_count"] == 0
    assert generator.calls == 3
    task = stage_task(service, "EARLY_PREVIEW")
    assert task["status"] == "failed"
    assert task["attempt_count"] == 3


@pytest.mark.parametrize("stage", ["EARLY_PREVIEW", "UPDATED_PREVIEW", "FINAL_PREGAME"])
def test_generation_crossing_stage_deadline_cannot_publish(setup, stage):
    service, scheduler, generator, clock, data = setup
    due, deadline = stage_windows(TIP.isoformat())[stage]
    clock.at = datetime.fromisoformat(due)
    if stage == "UPDATED_PREVIEW": stage_task(service, stage)
    generator.finish_at = datetime.fromisoformat(stage_task(service, stage)["deadline_at"])
    result = scheduler.run_due_tasks(data)
    assert result["results"][0]["update_status"] == "failed"
    assert stage_task(service, stage)["status"] == "expired"
    assert service.selected(data["games"][0])["version"] is None
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM prediction_versions").fetchone()[0] == 0


def test_final_crossing_tipoff_retains_last_legal_version(setup):
    service, scheduler, generator, clock, data = setup
    clock.at = TIP - timedelta(hours=24)
    scheduler.run_due_tasks(data)
    prior = service.selected(data["games"][0])["version_id"]
    clock.at = TIP - FINAL_PREGAME_LEAD
    generator.finish_at = TIP
    scheduler.run_due_tasks(data)
    selected = service.selected(data["games"][0])
    assert selected["status"] == "locked"
    assert selected["version_id"] == prior
    assert stage_task(service, "FINAL_PREGAME")["status"] == "expired"


def test_tipoff_crossed_during_publication_rolls_back_version_and_current(setup, monkeypatch):
    from v2.pregame_system import prediction_versions
    service, _, _, clock, data = setup
    clock.at = TIP - timedelta(seconds=1)
    claim = service.tasks.claim(stage_task(service, "FINAL_PREGAME")["task_id"])
    request = service.store.begin_request(data["games"][0], request_id=claim["request_id"])
    final = {"winner_side": "home", "win_probability": 0.6, "expected_margin": 2.5, "expected_total": 221.5}
    payload = {
        "game_id": data["games"][0]["id"], "game_identity": game_identity(data["games"][0]),
        "request_id": request["request_id"], "requested_at": request["requested_at"],
        "input_cutoff_at": request["requested_at"], "input_frozen_at": request["requested_at"],
        "generated_at": request["requested_at"], "input_snapshot": data["games"][0],
        "prediction": final, "judgment": {"final_prediction": final}, "stage": claim["stage"],
        "task_id": claim["task_id"], "scheduled_for": claim["scheduled_for"], "trigger": {"type": "scheduled"},
        "policy_version": claim["policy_version"], "schedule_revision": claim["schedule_revision"],
    }
    original_json = prediction_versions._json
    serializations = []
    def slow_publication(value):
        if isinstance(value, dict) and value.get("version_id"):
            serializations.append(value["version_id"])
            if len(serializations) == 2:
                clock.at = TIP
        return original_json(value)
    monkeypatch.setattr(prediction_versions, "_json", slow_publication)
    with pytest.raises(VersionStoreError, match="pregame_generation_closed"):
        service.store.publish(data["games"][0], request, seal_fixture_payload(payload), generation_claim=claim)
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM prediction_versions").fetchone()[0] == 0
        assert db.execute("SELECT current_version_id FROM game_prediction_state").fetchone()[0] is None


def test_three_successes_switch_current_and_keep_all_old_payloads(setup):
    service, scheduler, _, clock, data = setup
    previous = None
    for stage, (due, _) in stage_windows(TIP.isoformat()).items():
        clock.at = datetime.fromisoformat(due)
        if stage == "UPDATED_PREVIEW": stage_task(service, stage)
        scheduler.run_due_tasks(data)
        version = service.selected(data["games"][0])["version"]
        assert version["stage"] == stage
        assert version["previous_successful_version_id"] == previous
        assert version["prediction"] == version["judgment"]["final_prediction"]
        previous = version["version_id"]
    with service.store.connect() as db:
        versions = db.execute("SELECT * FROM prediction_versions ORDER BY request_seq").fetchall()
        assert len(versions) == 3
        for version in versions:
            service.store._decode_version(version)
    clock.at = TIP
    assert service.selected(data["games"][0])["version_id"] == previous
    assert service.selected(data["games"][0])["status"] == "locked"


@pytest.mark.parametrize("lower_stage", ["EARLY_PREVIEW", "UPDATED_PREVIEW"])
def test_final_cannot_be_overridden_by_lower_stage_even_with_newer_request_seq(setup, lower_stage):
    service, scheduler, _, clock, data = setup
    clock.at = TIP - FINAL_PREGAME_LEAD
    scheduler.run_due_tasks(data)
    saved = service.selected(data["games"][0])["version"]
    request = service.store.begin_request(data["games"][0])
    payload = copy.deepcopy(saved)
    payload.update(request_id=request["request_id"], requested_at=request["requested_at"],
                   input_cutoff_at=request["requested_at"], input_frozen_at=request["requested_at"],
                   generated_at=request["requested_at"], stage=lower_stage)
    with pytest.raises(VersionStoreError, match="prediction_stage_regression"):
        service.store.publish(data["games"][0], request, payload)
    assert service.selected(data["games"][0])["version_id"] == saved["version_id"]


def test_after_tipoff_manual_and_scheduler_cannot_start_generation(setup):
    service, scheduler, generator, clock, data = setup
    clock.at = TIP
    assert scheduler.run_due_tasks(data)["due_count"] == 0
    with pytest.raises(VersionStoreError, match="pregame_generation_closed"):
        service.generate(data, data["games"][0]["id"])
    assert generator.calls == 0
    assert service.selected(data["games"][0])["status"] == "locked_unavailable"


def test_restart_recovers_abandoned_claim_and_fences_previous_owner(setup):
    service, _, _, clock, data = setup
    clock.at = TIP - timedelta(days=3)
    task_id = stage_task(service, "EARLY_PREVIEW")["task_id"]
    old_claim = service.tasks.claim(task_id)
    request = service.store.begin_request(data["games"][0], request_id=old_claim["request_id"])
    clock.at += timedelta(seconds=121)
    reopened = GenerationTaskStore(PredictionVersionStore(service.store.path, clock=clock))
    assert reopened.list_due_tasks()[0]["task_id"] == task_id
    new_claim = reopened.claim(task_id)
    assert new_claim["owner_token"] != old_claim["owner_token"]
    assert new_claim["attempt_count"] == 2
    final = {"winner_side": "home", "win_probability": 0.6, "expected_margin": 2.5, "expected_total": 221.5}
    payload = {
        "game_id": data["games"][0]["id"], "game_identity": game_identity(data["games"][0]),
        "request_id": request["request_id"], "requested_at": request["requested_at"],
        "input_cutoff_at": request["requested_at"], "input_frozen_at": request["requested_at"],
        "generated_at": clock.at.isoformat(), "input_snapshot": data["games"][0],
        "prediction": final, "judgment": {"final_prediction": final}, "stage": old_claim["stage"],
        "task_id": task_id, "scheduled_for": old_claim["scheduled_for"], "trigger": {"type": "scheduled"},
    }
    with pytest.raises(VersionStoreError, match="generation_claim_lost"):
        service.store.publish(data["games"][0], request, seal_fixture_payload(payload), generation_claim=old_claim)
    assert service.selected(data["games"][0])["version"] is None


def test_heartbeat_extends_live_claim_but_never_revives_expired_owner(setup):
    service, _, _, clock, _ = setup
    tasks = GenerationTaskStore(service.store, lease_seconds=9)
    clock.at = TIP - timedelta(days=3)
    claim = tasks.claim(stage_task(service, "EARLY_PREVIEW")["task_id"])
    clock.at += timedelta(seconds=5)
    assert tasks.heartbeat(claim) is True
    clock.at += timedelta(seconds=5)
    tasks.assert_claim(claim)
    clock.at += timedelta(seconds=10)
    assert tasks.heartbeat(claim) is False
    with pytest.raises(VersionStoreError, match="generation_claim_lost"):
        tasks.assert_claim(claim)


def test_reopen_after_success_does_not_regenerate_and_task_links_exact_version(setup):
    service, scheduler, generator, clock, data = setup
    clock.at = TIP - timedelta(days=3)
    scheduler.run_due_tasks(data)
    reopened = PredictionVersionService(store=PredictionVersionStore(service.store.path, clock=clock),
        service_factory=generator.factory, analyst_factory=lambda request: object())
    assert PregameScheduler(reopened).run_due_tasks(data)["due_count"] == 0
    assert generator.calls == 1
    version = reopened.selected(data["games"][0])["version"]
    task = reopened.tasks.get_task(version["task_id"])
    assert task["status"] == "succeeded"
    assert task["published_version_id"] == version["version_id"]
    assert build_prediction_versions_response(data=data, version_service=reopened)[1]["predictions"][version["game_id"]]["version_id"] == version["version_id"]
    assert generator.calls == 1


def test_preseason_registration_and_input_adapter_share_same_scheduler(setup):
    service, scheduler, generator, clock, _ = setup
    preseason = game("preseason")
    preseason["id"] = "espn:499999999"
    preseason.pop("relationship")
    preseason.pop("prediction")
    registered = service.tasks.register_game(preseason)
    assert len(registered["tasks"]) == 0
    SchedulerPolicyPlanner(service.tasks).configure(preseason["id"],
        {"preheat_enabled": True, "preheat_reason": "explicit second fixture", "preheat_lead_hours": 72})
    fact = next(item for item in merge_registered_games(
        {"games": []}, service.tasks, ready_only=True)["games"] if item["id"] == preseason["id"])
    assert fact["prediction"] is None and fact["report_status"] == "unavailable"
    calls = []
    def baseline(raw, cutoff):
        calls.append(cutoff)
        result = game("preseason")
        result["id"] = raw["id"]
        return result
    service.input_preparer = lambda data, game_id, cutoff: prepare_generation_data(
        data, game_id, cutoff=cutoff, baseline_builder=baseline)
    clock.at = TIP - timedelta(days=3)
    scheduler.run_due_tasks({"games": []})
    selected = service.selected(registered["game"])
    assert selected["version"]["input_snapshot"]["season_phase"]["phase"] == "preseason"
    assert selected["version"]["input_snapshot"]["game_type"] == "preseason"
    assert len(calls) == 1
    assert generator.calls == 2  # Both distinct registered games had a due task.
    assert len(merge_registered_games({"games": []}, service.tasks, ready_only=True)["games"]) == 2


@pytest.mark.parametrize("change,error", [
    ({"game_type": "summer_league"}, "game_type_invalid"),
    ({"tipoff": "2026-10-20T23:00:00"}, "timezone_required"),
    ({"home": {"id": 123}}, "unknown_nba_team"),
])
def test_registration_rejects_illegal_type_time_or_team(setup, change, error):
    service, _, _, _, _ = setup
    invalid = game()
    invalid.update(change)
    with pytest.raises(VersionStoreError, match=error):
        service.tasks.register_game(invalid)


def test_changed_schedule_cannot_silently_reuse_registered_tasks(setup):
    service, _, _, _, data = setup
    changed = copy.deepcopy(data["games"][0])
    changed["tipoff"] = (TIP + timedelta(days=1)).isoformat()
    with pytest.raises(VersionStoreError, match="schedule_changed"):
        service.tasks.register_game(changed)


def test_game_type_cannot_silently_change_after_registration(setup):
    service, _, _, _, data = setup
    changed = copy.deepcopy(data["games"][0])
    changed["game_type"] = "preseason"
    with pytest.raises(VersionStoreError, match="registered_game_type_changed"):
        service.tasks.register_game(changed)


def test_cancelled_game_cancels_pending_work_before_expensive_generation(setup):
    service, scheduler, generator, clock, data = setup
    clock.at = TIP - timedelta(days=3)
    data["games"][0]["status"] = "cancelled"
    assert scheduler.run_due_tasks(data)["due_count"] == 0
    assert generator.calls == 0
    assert stage_task(service, "EARLY_PREVIEW")["status"] == "cancelled"
    assert service.selected(data["games"][0])["status"] == "locked_unavailable"


def test_scheduler_cli_plan_only_creates_tasks_without_generation(tmp_path, capsys):
    data_file = tmp_path / "data.json"
    future = game()
    future["tipoff"] = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    data_file.write_text(json.dumps({"games": [future]}), encoding="utf-8")
    main(["--database", str(tmp_path / "cli.sqlite"), "--once", "--plan-only", "--data", str(data_file)])
    reply = json.loads(capsys.readouterr().out)
    assert reply["plan_only"] is True
    assert reply["due_count"] == 0


def test_preseason_phase_reaches_actual_research_and_writer_context(tmp_path, monkeypatch):
    from . import analysis_service
    from .analysis_service import SavedGameAnalysisService
    clock = Clock()
    monkeypatch.setattr(analysis_service, "now", lambda: clock.at.isoformat())
    seen = {}
    class Researcher:
        def investigate(self, identity, cutoff, **kwargs):
            seen["research"] = kwargs["local_context"]["season_phase"]
            return {"status": "unavailable", "records": []}, {"status": "offline-fixture"}
    class PlayerFacts:
        def matchup_context(self, *args, **kwargs):
            return {"status": "unavailable", "teams": {}, "numeric_model_usage": False}
    class Analyst:
        def judge(self, context):
            seen["writer"] = [item["value"] for item in context.objective_evidence if item["section"] == "season_phase"][0]
            clock.at += timedelta(seconds=2)
            class Judgment:
                def model_dump(self):
                    return offline_judgment({"winner_side": "home", "win_probability": 0.56,
                        "expected_margin": 2.5, "expected_total": 221.5}, "Preseason fixture")
            judgment = Judgment()
            return judgment, {"forecast_audit_path": "offline-forecast", **protocol_trace(judgment.model_dump(),context)}
    class Analysis(SavedGameAnalysisService):
        def __init__(self, data, *, analyst):
            super().__init__(data, analyst=analyst, researcher=Researcher(), player_knowledge_store=PlayerFacts())
    service = PredictionVersionService(store=PredictionVersionStore(tmp_path / "p.sqlite", clock=clock),
        service_factory=Analysis, analyst_factory=lambda request: Analyst(), input_preparer=prepare_generation_data)
    data = {"games": [game("preseason")]}
    service.tasks.register_game(data["games"][0])
    clock.at = TIP - timedelta(hours=24)
    result = PregameScheduler(service).run_due_tasks(data)
    assert result["results"][0]["task"]["status"] == "succeeded"
    assert seen["research"]["game_type"] == "preseason"
    assert seen["writer"]["phase"] == "preseason"
    selected = service.selected(data["games"][0])["version"]
    assert selected["analyst_context"]["season_phase"]["game_type"] == "preseason"


def test_new_game_baseline_adapter_reuses_explicit_deterministic_pipeline(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from v2.pregame_system import agent, prediction, storage, matchup_history
    from .generation_input import build_registered_baseline
    seen = {}
    predictor = SimpleNamespace(demo_context=None, games=object(), stats=object())
    monkeypatch.setattr(prediction, "Predictor", lambda **kwargs: predictor)
    roster = {"teams": {"home": {}, "away": {}}}
    class DeterministicAgent:
        def __init__(self, *, predictor):
            self.schedule = [{"game_id": "espn:499999998"}]
        def ask(self, question, **kwargs):
            seen.update(kwargs)
            target = self.schedule[-1]
            values = {"win_probability": 0.6, "expected_margin": 4.5, "expected_total": 220,
                      "expected_home_score": 112.25, "expected_away_score": 107.75}
            return {"game": target, "prediction": values, "pure_prediction": values,
                "official_numeric_prediction": values, "prediction_method": "pure_basketball_fallback",
                "roster_context": roster, "snapshot": {"cutoff": kwargs["cutoff"]}}, None
    monkeypatch.setattr(agent, "PregameAgent", DeterministicAgent)
    monkeypatch.setattr(storage, "new_run", lambda name: tmp_path)
    monkeypatch.setattr(matchup_history, "build_matchup_history", lambda *args: {"status": "unavailable"})
    monkeypatch.setattr(matchup_history, "build_season_form_context", lambda *args: {"status": "unavailable"})
    monkeypatch.setattr(matchup_history, "season_phase_context", lambda *args: {"phase": "opening_night"})
    result = build_registered_baseline(game("preseason"), (TIP - timedelta(days=3)).isoformat())
    assert seen["allow_lookup"] is False
    assert seen["generate_judgment"] is False
    assert seen["plan"]["intent"] == "report"
    assert seen["plan"]["game_id"] == result["id"]
    assert len(predictor.schedule) == 2
    assert result["roster_context"] == roster
    assert result["relationship"]["basketball_provenance"]["market_independent"] is True


def test_registered_game_http_and_frontend_restore_one_published_version(setup, monkeypatch):
    import functools
    import shutil
    import subprocess
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen
    from . import server
    from .export_data import STATIC_ROOT
    from .test_prediction_version_restore import NODE_FRONTEND_PROBE
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for actual frontend restore functions")
    service, scheduler, _, clock, data = setup
    clock.at = TIP - timedelta(days=3)
    scheduler.run_due_tasks(data)
    expected = service.selected(data["games"][0])["version"]
    # This game is absent from the static app file, present only in the registry.
    # The homepage now lists today/tomorrow; restore the old saved preview inside that window.
    clock.at = TIP - timedelta(hours=12)
    monkeypatch.setattr(server, "read_json", lambda path: {"games": []})
    monkeypatch.setattr(server, "_PREDICTION_VERSION_SERVICE", service)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(server.Handler, directory=str(STATIC_ROOT)))
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        with urlopen(f"http://127.0.0.1:{httpd.server_port}/app_data.json", timeout=5) as reply:
            app_data = json.load(reply)
        with urlopen(f"http://127.0.0.1:{httpd.server_port}/api/current-analyses", timeout=5) as reply:
            current = json.load(reply)
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)
    assert len(app_data["games"]) == 1
    probe = subprocess.run(["node", "-e", NODE_FRONTEND_PROBE, str(STATIC_ROOT / "app.js")],
        input=json.dumps({"game": app_data["games"][0], "response": current}),
        text=True, encoding="utf-8", capture_output=True, timeout=20, check=True)
    restored = json.loads(probe.stdout)
    assert restored["restoredVersionId"] == expected["version_id"]
    assert restored["displayedVersionId"] == expected["version_id"]
    assert restored["restoredArticle"] == expected["judgment"]["analysis_article"]
    assert restored["displayedMargin"] == expected["judgment"]["final_prediction"]["expected_margin"]


def test_retry_keeps_one_task_but_uses_isolated_pipeline_artifact_roots(tmp_path, monkeypatch):
    from . import analysis_service, prediction_version_service
    from v2.pregame_system import player_knowledge, web_search
    clock = Clock()
    monkeypatch.setattr(analysis_service, "now", lambda: clock.at.isoformat())
    roots = []
    class Writer:
        def __init__(self, *, root, **kwargs):
            roots.append(root)
        def judge(self, context):
            if len(roots) == 1:
                raise RuntimeError("offline_first_attempt_failure")
            clock.at += timedelta(seconds=2)
            class Judgment:
                def model_dump(self):
                    return offline_judgment({"winner_side": "home", "win_probability": 0.56,
                        "expected_margin": 2.5, "expected_total": 221.5}, "Retry fixture")
            judgment = Judgment()
            return judgment, {"forecast_audit_path": "offline-forecast", **protocol_trace(judgment.model_dump(),context)}
    class Researcher:
        def __init__(self, **kwargs):
            pass
        def investigate(self, *args, **kwargs):
            return {"status": "unavailable", "records": []}, {"status": "offline-fixture"}
    class PlayerFacts:
        def matchup_context(self, *args, **kwargs):
            return {"status": "unavailable", "teams": {}, "numeric_model_usage": False}
    monkeypatch.setattr(prediction_version_service, "EvidenceBoundAnalyst", Writer)
    monkeypatch.setattr(prediction_version_service, "AutonomousWebResearcher", Researcher)
    monkeypatch.setattr(web_search, "PregameWebSearch", lambda: object())
    monkeypatch.setattr(player_knowledge, "PlayerKnowledgeStore", PlayerFacts)
    service = PredictionVersionService(store=PredictionVersionStore(tmp_path / "a.sqlite", clock=clock))
    data = {"games": [game()]}
    service.tasks.register_game(data["games"][0])
    clock.at = TIP - timedelta(hours=24)
    scheduler = PregameScheduler(service)
    assert scheduler.run_due_tasks(data)["results"][0]["update_status"] == "failed"
    clock.at += timedelta(seconds=30)
    assert scheduler.run_due_tasks(data)["results"][0]["update_status"] == "published"
    assert len(roots) == 2
    assert roots[0] != roots[1]
    assert roots[0].parent.parent == roots[1].parent.parent
    version = service.selected(data["games"][0])["version"]
    task = service.tasks.get_task(version["task_id"])
    assert task["attempt_count"] == 2
    assert task["published_version_id"] == version["version_id"]
