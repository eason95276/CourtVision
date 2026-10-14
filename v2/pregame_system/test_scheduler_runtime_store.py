"""Offline runtime ledger contracts; every database is created under tmp_path."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from .generation_tasks import GenerationTaskStore
from .prediction_versions import PredictionVersionStore, VersionStoreError
from .scheduler_policy import SchedulerPolicyPlanner
from .scheduler_runtime_store import RuntimeTaskStore, classify_failure


TIP = datetime(2026, 10, 5, 23, tzinfo=timezone.utc)


class Clock:
    def __init__(self, at=TIP-timedelta(minutes=5)):
        self.at = at

    def __call__(self):
        return self.at


def game(game_id="espn:499999801"):
    return {"id":game_id,"tipoff":TIP.isoformat(),"status":"scheduled","game_type":"preseason",
            "home":{"id":1610612752,"name":"Knicks","name_zh":"尼克斯"},
            "away":{"id":1610612755,"name":"76ers","name_zh":"76人"}}


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    versions = PredictionVersionStore(tmp_path/"runtime.sqlite",clock=clock)
    tasks = RuntimeTaskStore(versions,run_id="fixture-run")
    g = tasks.register_game(game())["game"]
    rows = SchedulerPolicyPlanner(tasks).plan_game(g)
    final = next(t for t in rows if t["stage"]=="FINAL_PREGAME")
    return tasks,clock,g,final


def test_opt_in_schema_is_additive_and_repeated_initialization_does_not_rewrite_existing_rows(setup):
    tasks,clock,_,final = setup
    with tasks.versions.connect() as db:
        before = [tuple(r) for r in db.execute("SELECT * FROM generation_tasks")]
        state = [tuple(r) for r in db.execute("SELECT * FROM game_prediction_state")]
    RuntimeTaskStore(tasks.versions,run_id="second-run")
    with tasks.versions.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM generation_tasks")] == before
        assert [tuple(r) for r in db.execute("SELECT * FROM game_prediction_state")] == state
        assert db.execute("SELECT COUNT(*) FROM scheduler_runtime_attempts").fetchone()[0]==0


def test_heartbeat_deadline_race_is_expired_not_ambiguous_cancelled(setup):
    tasks,clock,game,task=setup
    claim=tasks.claim(task['task_id']);assert claim
    clock.at=TIP+timedelta(seconds=1)
    tasks._lose_lease(claim,'runtime_heartbeat_lost')
    assert tasks.get_task(task['task_id'])['status']=='expired'
    assert tasks.get_task(task['task_id'])['last_error']=='pregame_generation_closed'
    assert tasks.attempts_snapshot(task_id=task['task_id'])[0]['failure_stage']=='tipoff_crossed'


def test_claim_and_journal_are_atomic_with_identity_time_and_budget(setup):
    tasks,clock,g,final = setup
    clock.at += timedelta(seconds=7)
    claim = tasks.claim(final["task_id"])
    journal = tasks.attempts_snapshot(task_id=claim["task_id"])[0]
    assert claim["runtime_attempt_id"] == journal["runtime_attempt_id"]
    assert journal["game_id"]==g["id"] and journal["stage"]=="FINAL_PREGAME"
    assert journal["queue_delay_seconds"]==7
    assert journal["remaining_budget_seconds"]==278
    assert journal["run_id"]=="fixture-run" and "owner_token" not in journal
    assert claim["effective_deadline_at"]==(TIP-timedelta(seconds=15)).isoformat()
    assert tasks.claim(final["task_id"]) is None


def test_two_claimers_only_one_same_game_wins(setup):
    tasks,_,_,final=setup
    other=RuntimeTaskStore(tasks.versions,run_id="other")
    barrier=threading.Barrier(2)
    results=[]
    def worker(store):
        barrier.wait(timeout=5)
        results.append(store.claim(final["task_id"]))
    threads=[threading.Thread(target=worker,args=(store,)) for store in (tasks,other)]
    for thread in threads: thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sum(r is not None for r in results)==1
    assert len(tasks.attempts_snapshot())==1


def test_different_games_have_independent_claims_and_leases(setup):
    tasks,_,_,first=setup
    othergame=tasks.register_game(game("espn:499999802"))["game"]
    second=SchedulerPolicyPlanner(tasks).plan_game(othergame)[0]
    a,b=tasks.claim(first["task_id"]),tasks.claim(second["task_id"])
    assert a and b and a["owner_token"]!=b["owner_token"]
    tasks.assert_claim(a); tasks.assert_claim(b)


def test_final_requires_full_minimum_budget_and_margin(setup):
    tasks,clock,_,final=setup
    clock.at=TIP-timedelta(seconds=194)
    assert tasks.claim(final["task_id"]) is None
    assert tasks.get_task(final["task_id"])["status"]=="expired"
    assert tasks.get_task(final["task_id"])["last_error"]=="runtime_budget_insufficient"


def test_final_budget_exact_boundary_is_allowed(setup):
    tasks,clock,_,final=setup
    clock.at=TIP-timedelta(seconds=195)
    assert tasks.claim(final["task_id"])


def test_retry_uses_same_task_but_rejects_insufficient_remaining_time(setup):
    tasks,clock,_,final=setup
    claim=tasks.claim(final["task_id"])
    clock.at+=timedelta(seconds=100)
    tasks.fail(claim,"research_minimum_not_met")
    task=tasks.get_task(final["task_id"])
    assert task["status"]=="expired" and task["attempt_count"]==1
    assert tasks.attempts_snapshot()[0]["failure_stage"]=="research_failure"
    clock.at+=timedelta(seconds=30)
    assert tasks.claim(final["task_id"]) is None


def test_retry_stable_identity_when_budget_allows(setup):
    tasks,clock,_,final=setup
    a=tasks.claim(final["task_id"])
    tasks.fail(a,"research_provider_unavailable")
    assert tasks.get_task(final["task_id"])["status"]=="failed"
    assert tasks.claim(final["task_id"]) is None
    clock.at+=timedelta(seconds=30)
    b=tasks.claim(final["task_id"])
    assert b["task_id"]==a["task_id"] and b["request_id"]==a["request_id"]
    assert b["owner_token"]!=a["owner_token"] and b["attempt_count"]==2
    assert not tasks.stop_task(a["task_id"],"late_stop","lease_failure",owner_token=a["owner_token"])
    assert not tasks.finish_attempt(a,error="old_failure")
    tasks.assert_claim(b)


def test_valid_lease_is_not_stolen_after_reopen(setup):
    tasks,_,_,final=setup
    a=tasks.claim(final["task_id"])
    reopened=RuntimeTaskStore(tasks.versions,run_id="restarted")
    assert reopened.claim(final["task_id"]) is None
    reopened.assert_claim(a)


def test_expired_runtime_lease_is_quarantined_not_repeated(setup):
    tasks,clock,_,final=setup
    a=tasks.claim(final["task_id"])
    clock.at+=timedelta(seconds=121)
    reopened=RuntimeTaskStore(tasks.versions,run_id="restarted")
    assert reopened.list_due_tasks()==[]
    task=reopened.get_task(a["task_id"])
    assert task["status"]=="cancelled" and task["last_error"]=="runtime_recovery_requires_review"
    assert task["attempt_count"]==1
    assert reopened.attempts_snapshot()[0]["heartbeat_health"]=="lost"
    with pytest.raises(VersionStoreError,match="claim_lost"):
        tasks.assert_claim(a)


def test_non_runtime_lease_keeps_original_recovery_semantics(setup):
    tasks,clock,_,final=setup
    base=GenerationTaskStore(tasks.versions)
    a=base.claim(final["task_id"])
    clock.at+=timedelta(seconds=121)
    # Default final admission budget would now prohibit rerun, so lower fixture budget.
    reopened=RuntimeTaskStore(tasks.versions,final_min_budget_seconds=60)
    assert reopened.list_due_tasks()[0]["task_id"]==a["task_id"]
    assert reopened.claim(a["task_id"])["attempt_count"]==2


def test_heartbeat_extends_long_claim_never_revives_lost_owner(setup):
    tasks,clock,_,final=setup
    claim=tasks.claim(final["task_id"])
    clock.at+=timedelta(seconds=90)
    assert tasks.heartbeat(claim)
    clock.at+=timedelta(seconds=60)
    tasks.assert_claim(claim)
    assert tasks.attempts_snapshot()[0]["last_heartbeat_at"]==(TIP-timedelta(seconds=210)).isoformat()
    tasks.stop_task(claim["task_id"],"fixture_stop","deadline_expired",owner_token=claim["owner_token"])
    assert not tasks.heartbeat(claim)


def test_keep_alive_detects_renewal_failure_and_fences_claim(setup,monkeypatch):
    tasks,_,_,final=setup
    tasks.heartbeat_seconds=0.01
    claim=tasks.claim(final["task_id"])
    hit=threading.Event()
    def failed(claim):
        hit.set()
        return False
    monkeypatch.setattr(tasks,"heartbeat",failed)
    with pytest.raises(VersionStoreError,match="runtime_lease_lost"):
        with tasks.keep_alive(claim):
            assert hit.wait(timeout=2)
            # Joining the event locally waits for durable failure processing, not model work.
            while tasks.get_task(claim["task_id"])["status"]=="running":
                if hit.wait(timeout=0.01) and tasks._lease_health[claim["owner_token"]].is_set():
                    break
    with pytest.raises(VersionStoreError): tasks.assert_claim(claim)
    assert tasks.attempts_snapshot()[0]["failure_stage"]=="lease_failure"


def test_sqlite_renewal_failure_is_visible_even_when_fence_cannot_be_written(setup,monkeypatch):
    tasks,_,_,final=setup
    claim=tasks.claim(final["task_id"])
    original=tasks.transaction
    @contextmanager
    def locked():
        raise sqlite3.OperationalError("database is locked")
        yield
    monkeypatch.setattr(tasks,"transaction",locked)
    tasks._lose_lease(claim,"runtime_heartbeat_OperationalError")
    with pytest.raises(VersionStoreError,match="runtime_lease_lost"):
        tasks.assert_claim(claim)
    monkeypatch.setattr(tasks,"transaction",original)
    assert tasks.stop_task(claim["task_id"],"lease_write_failed","lease_failure",owner_token=claim["owner_token"],status="cancelled")


def test_db_lock_rolls_back_claim_then_subsequent_claim_succeeds(setup,monkeypatch):
    tasks,_,_,final=setup
    original=tasks.versions.connect
    def fast_connection():
        db=original(); db.execute("PRAGMA busy_timeout=10"); return db
    lock=original(); lock.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(tasks.versions,"connect",fast_connection)
    try:
        with pytest.raises(sqlite3.OperationalError,match="locked"):
            tasks.claim(final["task_id"])
    finally:
        lock.execute("ROLLBACK"); lock.close()
    assert tasks.get_task(final["task_id"])["status"]=="pending"
    assert tasks.attempts_snapshot()==[]
    assert tasks.claim(final["task_id"])


def test_phase_journal_and_finish_never_manufacture_success(setup):
    tasks,clock,_,final=setup
    claim=tasks.claim(final["task_id"])
    tasks.record_phase(claim,"research")
    clock.at+=timedelta(seconds=2)
    tasks.record_phase(claim,"forecasting")
    row=tasks.attempts_snapshot()[0]
    assert row["phase"]=="forecasting" and row["started_at"]
    assert set(json.loads(row["phase_times_json"]))=={"research","forecasting"}
    tasks.fail(claim,"forecast_stage_json_invalid")
    tasks.finish_attempt(claim,result={"update_status":"published"},failure_stage="forecasting_failure")
    row=tasks.attempts_snapshot()[0]
    assert row["status"]=="failed" and row["published_version_id"] is None
    assert row["total_latency_seconds"]==2 and row["failure_stage"]=="forecasting_failure"


def test_stop_requires_owner_and_old_owner_cannot_cancel_current(setup):
    tasks,_,_,final=setup
    claim=tasks.claim(final["task_id"])
    assert not tasks.stop_task(claim["task_id"],"unsafe","deadline_expired")
    assert not tasks.stop_task(claim["task_id"],"unsafe","deadline_expired",owner_token="stale")
    assert tasks.stop_task(claim["task_id"],"runtime_deadline_expired","deadline_expired",owner_token=claim["owner_token"])
    assert tasks.get_task(claim["task_id"])["owner_token"] is None


@pytest.mark.parametrize("change",["policy","revision","tipoff"])
def test_current_policy_revision_and_tipoff_gate(setup,change):
    tasks,clock,_,final=setup
    claim=tasks.claim(final["task_id"])
    if change=="tipoff": clock.at=TIP
    else:
        with tasks.transaction() as db:
            if change=="revision": db.execute("UPDATE game_prediction_state SET schedule_revision=schedule_revision+1")
            else: db.execute("UPDATE game_prediction_state SET policy_config_json=?",(json.dumps({"report_enabled":False}),))
    with pytest.raises(VersionStoreError): tasks.assert_claim(claim)


@pytest.mark.parametrize("code,phase,expected",[
    ("research_minimum_not_met",None,"research_failure"),
    ("forecast_stage_json_invalid",None,"forecasting_failure"),
    ("unsupported_championship_claim",None,"validation_failure"),
    ("runtime_deadline_expired",None,"deadline_expired"),
    ("pregame_generation_closed",None,"tipoff_crossed"),
    ("generation_claim_lost",None,"lease_failure"),
    ("schedule_revision_changed",None,"schedule_revision_changed"),
    ("policy_or_revision_changed",None,"cancelled_by_policy"),
    ("RuntimeError","editorial","editorial_failure"),
    ("sqlite_error","publish","publish_failure"),
])
def test_failure_taxonomy(code,phase,expected):
    assert classify_failure(code,phase)==expected


def test_runtime_attempt_cap_limits_existing_tasks_and_due_query(setup):
    tasks,clock,_,final=setup
    tasks.max_attempts=1
    claim=tasks.claim(final["task_id"])
    tasks.fail(claim,"research_fixture_failure")
    clock.at+=timedelta(seconds=31)
    assert tasks.list_due_tasks()==[]
    assert tasks.claim(final["task_id"]) is None


def test_actual_error_code_survives_coordinator_class_only_failure(setup):
    tasks,_,_,final=setup
    claim=tasks.claim(final["task_id"])
    tasks.record_phase(claim,"editorial")
    tasks.fail(claim,"ValueError")
    tasks.finish_attempt(claim,error=ValueError('unsupported_current_status_claim:{"private":"not stored"}'),failure_stage="validation_failure")
    row=tasks.attempts_snapshot()[0]
    assert row["error_code"]=="unsupported_current_status_claim"
    assert row["failure_stage"]=="validation_failure"
    assert "private" not in row["error_code"]


def test_baseexception_before_coordinator_fail_is_quarantined(setup):
    tasks,clock,_,final=setup
    claim=tasks.claim(final["task_id"])
    tasks.finish_attempt(claim,error=KeyboardInterrupt(),failure_stage="lease_failure")
    assert tasks.get_task(final["task_id"])["status"]=="cancelled"
    clock.at+=timedelta(seconds=121)
    assert tasks.list_due_tasks()==[]
