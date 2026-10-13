"""Offline Policy V2 contracts: temporary registry, fake generation, no outside services."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from v2.pregame_system.generation_tasks import GenerationTaskStore
from v2.pregame_system.prediction_versions import PredictionVersionStore, VersionStoreError
from v2.pregame_system.scheduler_policy import SchedulerPolicyPlanner, POLICY_VERSION, product_category
from .prediction_version_service import PredictionVersionService
from .pregame_scheduler import PregameScheduler
from .server import build_ai_response
from .test_pregame_scheduler import Clock, GeneratorState, game, TIP, no_external_services

START = datetime(2026, 10, 19, 16, tzinfo=timezone.utc)  # Oct 20 00:00 China; tipoff is Oct 21 China.


@pytest.fixture
def setup_v2(tmp_path):
    clock = Clock(START - timedelta(seconds=1))
    generator = GeneratorState(clock)
    service = PredictionVersionService(store=PredictionVersionStore(tmp_path / "p.sqlite", clock=clock),
        service_factory=generator.factory, analyst_factory=lambda request: object())
    g = service.tasks.register_game(game())["game"]
    return service, SchedulerPolicyPlanner(service.tasks), generator, clock, g


def tasks(service):
    with service.store.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM generation_tasks ORDER BY created_at,stage")]


def event(g, clock, batch="verified-batch"):
    return {"game_id": g["id"], "schedule_revision": g["schedule_revision"], "event_type": "questionable_to_out",
        "affected_object": {"player_id": "fixture-core-player", "role": "core"},
        "old_state": "Questionable", "new_state": "Out", "evidence_batch_id": batch,
        "evidence_ids": ["fixture-verified-source"], "observed_at": clock.at.isoformat(), "reason": "confirmed material availability change"}


def test_registration_far_game_has_no_tasks_and_facts_remain_readable(setup_v2):
    service, planner, _, _, g = setup_v2
    assert service.tasks.register_game(g)["tasks"] == []
    assert planner.plan_game(g) == [] and tasks(service) == []
    from .generation_input import merge_registered_games
    facts = merge_registered_games({"games": []}, service.tasks)["games"]
    assert facts[0]["report_status"] == "unavailable" and facts[0]["product_category"] == "future"
    assert not facts[0]["homepage_visible"]


def test_china_midnight_creates_only_one_preview_and_final_repeated_ticks(setup_v2):
    service, planner, _, clock, g = setup_v2
    assert planner.plan_game(g) == []
    clock.at = START
    for _ in range(3):
        planner.plan_game(g)
    rows = tasks(service)
    assert len(rows) == 2 and {r["stage"] for r in rows} == {"EARLY_PREVIEW", "FINAL_PREGAME"}
    early = next(r for r in rows if r["stage"] == "EARLY_PREVIEW")
    assert early["scheduled_for"] == clock.at.isoformat()
    assert all(r["policy_version"] == POLICY_VERSION and r["schedule_revision"] == 1 for r in rows)


def test_late_start_uses_actual_now_and_never_backdates(setup_v2):
    service, planner, _, clock, g = setup_v2
    clock.at = START + timedelta(hours=10)
    early = next(t for t in planner.plan_game(g) if t["stage"] == "EARLY_PREVIEW")
    assert early["scheduled_for"] == early["created_at"] == clock.at.isoformat()


def test_explicit_preheat_not_default_for_preseason_or_playoffs(setup_v2):
    service, planner, _, clock, g = setup_v2
    special = planner.configure(g["id"], {"preheat_enabled": True, "preheat_reason": "explicit experiment", "preheat_lead_hours": 72})
    early = next(t for t in planner.plan_game(special) if t["stage"] == "EARLY_PREVIEW")
    assert json.loads(early["metadata_json"])["preview_kind"] == "special_preheat"
    clock.at = START
    planner.plan_game(special)
    assert len(tasks(service)) == 2


@pytest.mark.parametrize("kind", ["preseason", "playoffs"])
def test_game_type_alone_does_not_enable_preheat(setup_v2, kind):
    service, planner, _, _, _ = setup_v2
    g = game(kind); g["id"] = "espn:499999001"
    registered = service.tasks.register_game(g)["game"]
    assert planner.plan_game(registered) == []


def test_disabled_policy_produces_no_tasks_and_internal_generation_rejected(setup_v2):
    service, planner, _, clock, g = setup_v2
    g = planner.configure(g["id"], {"report_enabled": False})
    clock.at = START
    assert planner.plan_game(g) == []
    with pytest.raises(VersionStoreError, match="report_window_not_open"):
        service.tasks.request_manual(g)


def test_updated_requires_material_event_and_batch_is_idempotent(setup_v2):
    service, planner, _, clock, g = setup_v2
    clock.at = START
    planner.plan_game(g)
    clock.at = TIP - timedelta(hours=24)
    planner.plan_game(g)
    assert all(t["stage"] != "UPDATED_PREVIEW" for t in tasks(service))
    e = event(g, clock)
    with pytest.raises(VersionStoreError, match="admin_confirmation"):
        planner.material_update(e)
    first = planner.material_update(e, admin_confirmed=True)
    repeated = planner.material_update(e, admin_confirmed=True)
    assert first["task"]["task_id"] == repeated["task"]["task_id"]
    assert first["task"]["stage"] == "UPDATED_PREVIEW" and first["task"]["trigger"] == "evidence_update"
    assert json.loads(first["task"]["metadata_json"])["research_refresh_required"] is True


@pytest.mark.parametrize("change", ["same_state", "future_time", "minor_odds", "stale_revision", "missing_evidence"])
def test_material_entry_rejects_invalid_or_unsupported_changes(setup_v2, change):
    _, planner, _, clock, g = setup_v2
    clock.at = START
    e = event(g, clock)
    if change == "same_state": e["new_state"] = e["old_state"]
    if change == "future_time": e["observed_at"] = (clock.at + timedelta(seconds=1)).isoformat()
    if change == "minor_odds": e["event_type"] = "small_market_move"
    if change == "stale_revision": e["schedule_revision"] = 0
    if change == "missing_evidence": e["evidence_ids"] = []
    with pytest.raises(VersionStoreError): planner.material_update(e, admin_confirmed=True)


def test_final_timing_priority_and_near_tipoff_no_chain(setup_v2):
    service, planner, _, clock, g = setup_v2
    clock.at = TIP - timedelta(minutes=10)
    rows = planner.plan_game(g)
    assert [r["stage"] for r in rows] == ["FINAL_PREGAME"]
    assert rows[0]["scheduled_for"] == (TIP - timedelta(minutes=5)).isoformat()
    assert service.tasks.list_due_tasks() == []
    deferred = planner.material_update(event(g, clock), admin_confirmed=True)
    assert deferred["status"] == "deferred_to_final"
    assert all(t["stage"] != "UPDATED_PREVIEW" for t in tasks(service))
    clock.at = TIP - timedelta(minutes=5)
    assert service.tasks.list_due_tasks()[0]["stage"] == "FINAL_PREGAME"


def test_global_due_priority_puts_final_before_other_games_update(setup_v2):
    service, planner, _, clock, g = setup_v2
    clock.at = START
    planner.plan_game(g)
    other = game(); other["id"] = "espn:499999002"; other["tipoff"] = (TIP + timedelta(hours=3)).isoformat()
    other = service.tasks.register_game(other)["game"]
    planner.plan_game(other)
    clock.at = TIP - timedelta(minutes=5)
    planner.material_update(event(other, clock), admin_confirmed=True)
    assert service.tasks.list_due_tasks()[0]["stage"] == "FINAL_PREGAME"


def test_fake_early_success_final_failure_preserves_current_and_freezes_metadata(setup_v2):
    service, _, generator, clock, g = setup_v2
    clock.at = START
    scheduler = PregameScheduler(service)
    first = scheduler.run_due_tasks({"games": [g]})
    assert first["results"][0]["update_status"] == "published"
    version = service.selected(g)["version"]
    assert version["policy_version"] == POLICY_VERSION and version["schedule_revision"] == 1
    assert version["task_metadata"]["preview_kind"] == "normal_window"
    generator.fail = True
    clock.at = TIP - timedelta(minutes=5)
    scheduler.run_due_tasks({"games": [g]})
    assert service.selected(g)["version"]["version_id"] == version["version_id"]
    assert service.selected(g)["version"]["stage"] == "EARLY_PREVIEW"


def test_generation_crossing_tipoff_keeps_old_version_locked(setup_v2):
    service, _, generator, clock, g = setup_v2
    clock.at = START
    scheduler = PregameScheduler(service)
    scheduler.run_due_tasks({"games": [g]})
    old = service.selected(g)["version_id"]
    clock.at = TIP - timedelta(minutes=5)
    generator.finish_at = TIP
    result = scheduler.run_due_tasks({"games": [g]})
    assert result["results"][0]["update_status"] == "failed"
    assert service.selected(g)["version_id"] == old and service.selected(g)["status"] == "locked"


@pytest.mark.parametrize("change", ["tipoff", "venue", "home_away"])
def test_revision_invalidates_running_worker_without_editing_old_versions(setup_v2, change):
    service, planner, _, clock, g = setup_v2
    clock.at = START
    scheduler = PregameScheduler(service)
    scheduler.run_due_tasks({"games": [g]})
    with service.store.connect() as db:
        frozen = db.execute("SELECT payload_json,payload_sha256 FROM prediction_versions").fetchall()
    clock.at += timedelta(hours=1)
    task = planner.material_update(event(g, clock), admin_confirmed=True)["task"]
    claim = service.tasks.claim(task["task_id"])
    request = service.store.begin_request(g, request_id=claim["request_id"])
    changed = copy.deepcopy(g)
    if change == "tipoff": changed["tipoff"] = (TIP + timedelta(hours=1)).isoformat()
    if change == "venue": changed["venue"] = {"fullName": "confirmed fixture venue"}; changed["neutral_site"] = True
    if change == "home_away": changed["home"], changed["away"] = changed["away"], changed["home"]
    updated = planner.revise_schedule(g["id"], changed, expected_revision=1,
        reason="verified schedule revision", evidence={"id": "fixture-confirmed-source"}, admin_confirmed=True)
    assert updated["schedule_revision"] == 2 and len(service.tasks.registered_games()) == 1
    assert "relationship" not in updated and "prediction" not in updated
    assert service.selected(updated)["version"] is None
    with pytest.raises(VersionStoreError): service.tasks.assert_claim(claim)
    with pytest.raises(VersionStoreError): service.store.assert_request_open(g, request)
    late_payload = json.loads(frozen[0][0])
    late_payload.update(request_id=request["request_id"], requested_at=request["requested_at"],
        input_cutoff_at=request["requested_at"], input_frozen_at=request["requested_at"], generated_at=request["requested_at"],
        stage=claim["stage"], task_id=claim["task_id"], scheduled_for=claim["scheduled_for"],
        trigger={"type": claim["trigger"]}, policy_version=claim["policy_version"], schedule_revision=1)
    with pytest.raises(VersionStoreError):
        service.store.publish(g, request, late_payload, generation_claim=claim)
    with service.store.connect() as db:
        assert db.execute("SELECT payload_json,payload_sha256 FROM prediction_versions").fetchall() == frozen
        assert db.execute("SELECT COUNT(*) FROM game_schedule_revisions").fetchone()[0] == 2
    assert all(t["status"] in {"succeeded", "cancelled"} for t in tasks(service) if t["schedule_revision"] == 1)


def test_revision_cannot_unlock_started_game_or_accept_stale_admin(setup_v2):
    service, planner, _, clock, g = setup_v2
    clock.at = TIP
    assert service.selected(g)["status"] == "locked_unavailable"
    changed = copy.deepcopy(g); changed["tipoff"] = (TIP + timedelta(days=1)).isoformat()
    with pytest.raises(VersionStoreError, match="pregame_generation_closed"):
        planner.revise_schedule(g["id"], changed, expected_revision=1, reason="reschedule", evidence={"id":"source"}, admin_confirmed=True)


def test_stale_admin_revision_and_implicit_venue_change_are_rejected(setup_v2):
    service, planner, _, clock, g = setup_v2
    changed = {**g, "venue": {"fullName": "confirmed fixture venue"}}
    with pytest.raises(VersionStoreError, match="schedule_changed"):
        service.tasks.register_game(changed)
    updated = planner.revise_schedule(g["id"], changed, expected_revision=1,
        reason="confirmed venue", evidence={"id": "source"}, admin_confirmed=True)
    with pytest.raises(VersionStoreError, match="schedule_revision_changed"):
        planner.revise_schedule(g["id"], changed, expected_revision=1,
            reason="stale admin", evidence={"id": "source"}, admin_confirmed=True)
    with pytest.raises(VersionStoreError, match="schedule_revision_changed"):
        service.tasks.register_game(g)
    assert updated["schedule_revision"] == 2


def test_migration_keeps_cancelled_legacy_history_and_new_policy_replaces_it(setup_v2):
    service, planner, _, clock, g = setup_v2
    clock.at = START
    planner.plan_game(g)
    with service.tasks.transaction() as db:
        db.execute("UPDATE generation_tasks SET policy_version='legacy-v1',status='cancelled',last_error='scheduler_policy_pending'")
    old_ids = {t["task_id"] for t in tasks(service)}
    GenerationTaskStore(service.store)
    planner.plan_game(g)
    rows = tasks(service)
    assert len(rows) == 4
    assert all(t["status"] == "cancelled" and t["last_error"] == "scheduler_policy_pending" for t in rows if t["task_id"] in old_ids)
    GenerationTaskStore(service.store); planner.plan_game(g)
    assert len(tasks(service)) == 4


@pytest.mark.parametrize("status", ["pending", "failed", "running"])
def test_old_executable_policy_is_cancelled_and_never_claimed(setup_v2, status):
    service, planner, _, clock, g = setup_v2
    clock.at = START
    planner.plan_game(g)
    with service.tasks.transaction() as db:
        db.execute("UPDATE generation_tasks SET policy_version='legacy-v1',status=? WHERE stage='EARLY_PREVIEW'", (status,))
    GenerationTaskStore(service.store)
    old = next(t for t in tasks(service) if t["policy_version"] == "legacy-v1")
    assert old["status"] == "cancelled" and old["last_error"] == "policy_replaced"
    assert service.tasks.claim(old["task_id"]) is None


@pytest.mark.parametrize("offset,expected", [(-1,"future"),(0,"tomorrow"),(86400,"today")])
def test_china_day_boundary_classification(setup_v2, offset, expected):
    _, _, _, _, g = setup_v2
    assert product_category(g, START + timedelta(seconds=offset))["product_category"] == expected


def test_special_live_completed_history_without_elapsed_time_guess(setup_v2):
    _, planner, _, _, g = setup_v2
    special = planner.configure(g["id"], {"preheat_enabled": True, "preheat_reason": "explicit", "preheat_lead_hours": 72})
    assert product_category(special, TIP - timedelta(days=2))["product_category"] == "special_preheat"
    assert product_category(g, TIP + timedelta(hours=12))["product_category"] == "live"
    completed = {**g, "status": "completed"}
    assert product_category(completed, TIP + timedelta(hours=2))["product_category"] == "completed"
    assert product_category(completed, TIP + timedelta(days=1))["product_category"] == "history"


def test_public_read_and_legacy_post_cannot_generate_but_internal_can(setup_v2, monkeypatch):
    service, _, generator, clock, g = setup_v2
    clock.at = START
    status, result = build_ai_response(g["id"], data={"games": [g]}, api_key="not-used", version_service=service)
    assert status == 200 and result["update_status"] == "read_only" and generator.calls == 0 and tasks(service) == []
    reply = service.generate({"games": [g]}, g["id"])
    assert reply["update_status"] == "published" and generator.calls == 1


def test_three_preseason_games_replace_nine_legacy_tasks_plan_only(tmp_path, monkeypatch):
    from .test_preseason_facts import intake as intake_fixture
    from .register_schedule import register_verified_schedule
    from v2.pregame_system.generation_tasks import stage_windows
    clock = Clock(datetime(2026, 10, 2, 12, tzinfo=timezone.utc))
    service = PredictionVersionService(store=PredictionVersionStore(tmp_path / "three.sqlite", clock=clock))
    boards, verification = intake_fixture.__wrapped__()
    registered = register_verified_schedule(boards, verification, service.tasks)
    with service.tasks.transaction() as db:
        for item in registered:
            for stage, (due, end) in stage_windows(item["game"]["tipoff"]).items():
                old = service.tasks._insert(db, item["game"]["id"], stage, "scheduled", due, end, clock.at, status="cancelled")
                db.execute("UPDATE generation_tasks SET policy_version='legacy-v1',last_error='scheduler_policy_pending' WHERE task_id=?", (old["task_id"],))
    monkeypatch.setattr(service, "execute_task", lambda *a: pytest.fail("Plan-only must not generate"))
    planner = SchedulerPolicyPlanner(service.tasks)
    for item in registered:
        planner.configure(item["game"]["id"], {"preheat_enabled": True, "preheat_reason": "explicit preseason test", "preheat_lead_hours": 72})
    scheduler = PregameScheduler(service)
    for _ in range(2):
        report = scheduler.run_due_tasks({"games": []}, plan_only=True)
        assert report["due_count"] == 3 and all(r["stage"] == "EARLY_PREVIEW" for r in report["results"])
    rows = tasks(service)
    assert len(rows) == 15
    assert len([t for t in rows if t["policy_version"] == "legacy-v1" and t["status"] == "cancelled"]) == 9
    assert len([t for t in rows if t["policy_version"] == POLICY_VERSION]) == 6
    assert all(t["attempt_count"] == 0 for t in rows)
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM prediction_versions").fetchone()[0] == 0
