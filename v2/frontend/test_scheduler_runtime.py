"""Offline runtime acceptance: real claims/versions, spawned fake providers only."""
from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from v2.pregame_system.prediction_versions import PredictionVersionStore
from v2.pregame_system.scheduler_policy import SchedulerPolicyPlanner
from v2.pregame_system.scheduler_runtime_store import RuntimeTaskStore
from .prediction_version_service import PredictionVersionService
from .scheduler_runtime import PersistentScheduler, main, migrate_runtime, status_report, validate_activation
from .scheduler_runtime_config import RuntimeConfig
from .scheduler_worker import failure_category
from .server import build_prediction_versions_response


TIP = datetime(2026, 10, 20, 23, tzinfo=timezone.utc)
def publication_integration(test):
    """Publication fixtures now exercise the real receipt gate unconditionally."""
    return test


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def block_network(*args, **kwargs):
    raise AssertionError("Runtime offline workers cannot access external providers")


def fake_service(versions, tasks, data, report_phase):
    # Spawn does not inherit parent monkeypatches. Enforce the boundary here too.
    os.environ["DEEPSEEK_API_KEY"] = ""
    os.environ["TAVILY_API_KEY"] = ""
    socket.create_connection = block_network
    import requests
    import urllib.request
    requests.Session.request = block_network
    urllib.request.OpenerDirector.open = block_network

    class Analysis:
        def analyze(self, game_id, *, cutoff, before_writer, allow_lookup):
            game = next(g for g in data["games"] if g["id"] == game_id)
            behavior = game.get("runtime_test", {})
            started = versions.clock()
            report_phase("pre_execution")
            versions.clock.at += timedelta(seconds=6.554)
            report_phase("research")
            versions.clock.at += timedelta(seconds=108.138)
            if behavior.get("bridge_pid_file"):
                bridge=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])
                Path(behavior["bridge_pid_file"]).write_text(str(bridge.pid))
            time.sleep(behavior.get("research_sleep", .15))
            if behavior.get("crash"):
                os._exit(17)
            if behavior.get("fail"):
                raise RuntimeError("research_stub_failure")
            cutoff = versions.clock().isoformat()
            frozen = cutoff
            before_writer()
            report_phase("forecasting")
            versions.clock.at += timedelta(seconds=6.510)
            time.sleep(.08)
            report_phase("editorial")
            versions.clock.at += timedelta(seconds=16.384)
            time.sleep(.08)
            report_phase("validation")
            versions.clock.at = started + timedelta(seconds=behavior.get("simulated_seconds", 141.487))
            final = {"winner_side": "home", "win_probability": .63, "expected_margin": 6.5, "expected_total": 224.5}
            payload = {"analysis_contract_version": "two-stage-forecast-v1", "cutoff": cutoff,
                "input_frozen_at": frozen, "generated_at": versions.clock().isoformat(),
                "judgment": {"final_prediction": final, "analysis_article": "OFFLINE " + game_id},
                "official_numeric_prediction": game["relationship"]["final"],
                "model_layers": {"pure_prediction": game["relationship"]["final"]},
                "input_snapshot": copy.deepcopy(game), "analyst_context": {"game": {
                    "game_id": game_id, "home": game["home"], "away": game["away"],
                    "scheduled_tipoff": game["tipoff"], "cutoff": cutoff, "game_type": "preseason"}},
                "research": {"records": [], "offline_stub": True}, "evidence_index": {}, "sources": [], "cited_sources": [],
                "context_hash": "offline-runtime", "base_input_hash": "offline-base", "search": {}, "analyst_trace": {}, "provenance": {}}
            from v2.pregame_system.editorial_test_helpers import seal_fixture_payload
            return seal_fixture_payload(payload)

    return PredictionVersionService(store=versions, task_store=tasks, service_factory=lambda *_args, **_kwargs: Analysis(),
        analyst_factory=lambda _: object(), input_preparer=lambda inputs, *_args, **_kwargs: inputs)


def broken_service(*args):
    raise RuntimeError("offline_factory_pre_execution_failure")


def process_alive(pid):
    if os.name=="nt":
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x100000,False,pid)
        if not handle:return False
        try:return kernel.WaitForSingleObject(handle,0)==0x102
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False


def make_game(number, *, tip=TIP, behavior=None):
    return {"id": "espn:" + str(499991100+number), "tipoff": tip.isoformat(), "status": "scheduled", "game_type": "preseason",
        "home": {"id": 1610612752, "name": "Knicks", "name_zh": "Knicks"},
        "away": {"id": 1610612755, "name": "76ers", "name_zh": "76ers"},
        "relationship": {"final": {"win_probability": .6, "expected_margin": 4.5, "expected_total": 220}},
        "prediction": {"winner_team_id": 1610612752, "winner_probability": .6}, "runtime_test": behavior or {}}


def setup_runtime(tmp_path, games=None):
    config = RuntimeConfig(tick_seconds=.05, planner_interval_seconds=60, heartbeat_seconds=.03,
                           shutdown_grace_seconds=.15)
    clock = Clock(TIP-timedelta(days=1))
    versions = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    tasks = RuntimeTaskStore(versions, heartbeat_seconds=config.heartbeat_seconds,
                            final_min_budget_seconds=config.final_min_budget_seconds, publish_margin_seconds=config.publish_margin_seconds)
    service = PredictionVersionService(store=versions, task_store=tasks)
    games = games or [make_game(1), make_game(2)]
    for game in games:
        registered = tasks.register_game(game)["game"]
        SchedulerPolicyPlanner(tasks).plan_game(registered)
    clock.at = TIP-timedelta(minutes=5)
    runner = PersistentScheduler(service, config=config, data_source=lambda: {"games": []}, service_builder=fake_service)
    return runner, service, clock


def wait_for(runner, predicate, timeout=25):
    until = time.monotonic()+timeout
    while time.monotonic()<until:
        runner.tick()
        if predicate():
            return
        time.sleep(.03)
    raise AssertionError("offline runtime condition did not complete")


def task_rows(service, stage="FINAL_PREGAME"):
    with service.store.connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM generation_tasks WHERE stage=? ORDER BY game_id", (stage,))]


@pytest.mark.parametrize('old_state',['completed','failed','running'])
def test_old_prewarm_slots_do_not_starve_later_games(tmp_path,monkeypatch,old_state):
    from concurrent.futures import Future
    from v2.pregame_system import two_stage_lifecycle
    runner,service,clock=setup_runtime(tmp_path)
    clock.at=TIP-timedelta(minutes=40)
    with service.store.connect()as db:
        db.execute("UPDATE generation_tasks SET metadata_json=? WHERE stage='FINAL_PREGAME'",
                   (json.dumps({'lifecycle':'early-final-v1'}),))
    called=[]
    def ready(_store,task,_collector,*,prewarm):
        assert prewarm is True
        called.append(task['task_id'])
        return {'ready':False,'reasons':['offline_unconfirmed']}
    monkeypatch.setattr(two_stage_lifecycle,'readiness_for_task',ready)
    for index in range(2):
        future=Future()
        if old_state=='completed':future.set_result({'ready':True})
        elif old_state=='failed':future.set_exception(ValueError('offline_old_prewarm'))
        runner.readiness_jobs['warm:finished-game-'+str(index)]=future
    try:
        runner._warm_final_information(clock.at)
        if old_state=='running':
            assert not called
            assert set(runner.readiness_jobs)=={'warm:finished-game-0','warm:finished-game-1'}
        else:
            for future in runner.readiness_jobs.values():future.result(timeout=2)
            expected={'warm:'+r['task_id']for r in task_rows(service)}
            assert set(runner.readiness_jobs)==expected
            assert set(called)=={r['task_id']for r in task_rows(service)}
            assert service.tasks.attempts_snapshot()==[]
    finally:runner.shutdown()


@publication_integration
def test_two_same_time_finals_publish_independent_versions_and_restore(tmp_path):
    runner, service, clock = setup_runtime(tmp_path)
    try:
        assert runner.tick()["dispatched"] == 2
        assert len(runner.active)==2
        wait_for(runner, lambda: not runner.active)
        rows=task_rows(service)
        assert [r["status"] for r in rows]==["succeeded", "succeeded"]
        assert len({r["published_version_id"] for r in rows})==2
        attempts=service.tasks.attempts_snapshot()
        assert len(attempts)==2 and all(a["status"]=="succeeded" for a in attempts)
        assert len({a["runtime_attempt_id"] for a in attempts})==2
        assert all(a["total_latency_seconds"]==pytest.approx(141.487) for a in attempts)
        assert all(a["queue_delay_seconds"]==0 for a in attempts)
        assert all(a["published_at"] and a["phase_times_json"] for a in attempts)
        clock.at=TIP
        data={"games":service.tasks.registered_games()}
        code, response=build_prediction_versions_response(data=data, version_service=service)
        assert code==200
        for row in rows:
            selected=response["predictions"][row["game_id"]]
            assert selected["status"]=="locked"
            assert selected["version_id"]==row["published_version_id"]
            assert selected["version"]["judgment"]["analysis_article"]=="OFFLINE "+row["game_id"]
    finally:
        runner.shutdown()


@publication_integration
def test_one_final_failure_does_not_block_other_game(tmp_path):
    runner, service, _=setup_runtime(tmp_path,[make_game(1,behavior={"fail":True}),make_game(2)])
    try:
        runner.tick()
        wait_for(runner,lambda:not runner.active)
        rows=task_rows(service)
        assert rows[0]["status"]=="expired"  # Remaining time/backoff cannot fund a full retry.
        assert rows[1]["status"]=="succeeded"
        assert {a["failure_stage"] for a in service.tasks.attempts_snapshot() if a["status"]!="succeeded"}=={"research_failure"}
    finally:runner.shutdown()


@publication_integration
def test_reserved_capacity_starts_two_finals_while_low_worker_is_busy(tmp_path):
    games=[make_game(1),make_game(2),make_game(3,tip=TIP+timedelta(hours=2),behavior={"research_sleep":8})]
    runner, service, clock=setup_runtime(tmp_path,games)
    # Make the FINALs become due one second after the lower-stage dispatch.
    # A five-minute jump would correctly expire the separate leader lease.
    with service.store.connect() as db:
        db.execute("UPDATE generation_tasks SET scheduled_for=?,next_attempt_at=? WHERE stage='FINAL_PREGAME' AND game_id IN (?,?)",
            ((clock.at+timedelta(seconds=1)).isoformat(),(clock.at+timedelta(seconds=1)).isoformat(),games[0]["id"],games[1]["id"]))
    try:
        runner.tick()
        wait_for(runner,lambda:any(w.owner_token for w in runner.active.values()))
        assert len(runner.active)==1 and next(iter(runner.active.values())).task["stage"]=="EARLY_PREVIEW"
        clock.at+=timedelta(seconds=1)
        assert runner.tick()["dispatched"]==2
        assert len(runner.active)==3
        wait_for(runner,lambda:all(row["status"]=="succeeded" for row in task_rows(service)[:2]))
        assert any(w.task["stage"]=="EARLY_PREVIEW" for w in runner.active.values())
    finally:runner.shutdown()


@publication_integration
def test_hard_timeout_stops_one_worker_without_stopping_other(tmp_path):
    runner,service,clock=setup_runtime(tmp_path,[make_game(1,behavior={"research_sleep":8}),make_game(2)])
    try:
        runner.tick()
        wait_for(runner,lambda:task_rows(service)[1]["status"]=="succeeded" and any(w.owner_token for w in runner.active.values()))
        clock.at=TIP-timedelta(seconds=14)
        runner._reap()
        assert task_rows(service)[0]["status"]=="expired"
        assert task_rows(service)[1]["status"]=="succeeded"
        assert not runner.active
    finally:runner.shutdown()


def test_worker_crash_is_quarantined_without_automatic_duplicate_calls(tmp_path):
    runner,service,_=setup_runtime(tmp_path,[make_game(1,behavior={"crash":True})])
    try:
        runner.tick();wait_for(runner,lambda:not runner.active)
        row=task_rows(service)[0]
        assert row["status"]=="cancelled" and "requires_review" in row["last_error"]
        assert runner.tick()["dispatched"]==0
        assert row["attempt_count"]==1
    finally:runner.shutdown()


def test_graceful_shutdown_fences_active_worker_and_records_stopped(tmp_path):
    runner,service,clock=setup_runtime(tmp_path,[make_game(1,behavior={"research_sleep":8})])
    runner.tick();wait_for(runner,lambda:any(w.owner_token for w in runner.active.values()))
    process=next(iter(runner.active.values())).process
    runner.shutdown()
    assert not process.is_alive()
    assert task_rows(service)[0]["status"]=="cancelled"
    status=status_report(service.store.path,config=runner.config,at=clock())
    assert status["scheduler"]["status"]=="stopped" and status["workers"]["busy"]==0


def test_second_dispatcher_cannot_claim_capacity_or_stop_first(tmp_path):
    runner,service,_=setup_runtime(tmp_path)
    second=PersistentScheduler(service,config=runner.config,data_source=lambda:{"games":[]},service_builder=fake_service)
    try:
        runner._leader()
        assert second.tick()["status"]=="leader_unavailable"
        assert not second.active
        second.shutdown()
        with service.store.connect() as db:
            assert db.execute("SELECT run_id,status FROM scheduler_runtime_leader").fetchone()[0]==runner.run_id
    finally:runner.shutdown()


def test_empty_queue_continuous_ticks_and_stop_file(tmp_path):
    runner,_,clock=setup_runtime(tmp_path)
    clock.at=TIP-timedelta(days=4)
    try:
        runner.run(max_ticks=3)
        assert not runner.active and runner.stop_event.is_set()
        stop=tmp_path/"stop";stop.touch()
        fresh=PersistentScheduler(runner.service,config=runner.config,data_source=lambda:{"games":[]})
        fresh.run(stop_file=stop)
        assert not fresh.active
    finally:runner.shutdown()


def test_tick_exception_recovers_next_tick(tmp_path):
    runner,_,clock=setup_runtime(tmp_path)
    clock.at=TIP-timedelta(days=4)
    def broken():raise OSError("offline_data_failure")
    runner.data_source=broken
    try:
        assert runner.tick()["status"]=="tick_error"
        runner.data_source=lambda:{"games":[]}
        assert runner.tick()["status"]=="running"
    finally:runner.shutdown()


def test_readonly_status_does_not_create_missing_database(tmp_path,capsys):
    missing=tmp_path/"missing.sqlite"
    assert main(["status","--database",str(missing)])==0
    assert json.loads(capsys.readouterr().out)["reason"]=="database_missing"
    assert not missing.exists()


def test_activation_gate_refuses_before_any_database_write(tmp_path,capsys):
    missing=tmp_path/"missing.sqlite"
    assert main(["--daemon","--database",str(missing)])==2
    assert not missing.exists()
    assert json.loads(capsys.readouterr().out)["status"]=="refused"


def test_activation_receipt_requires_actual_immutable_version(tmp_path):
    runner,service,_=setup_runtime(tmp_path)
    receipt=tmp_path/"activation.json"
    receipt.write_text(json.dumps({"authorized":True,"read_acceptance_passed":True,"validated_version_id":"missing"}))
    with pytest.raises(ValueError,match="missing_or_corrupt"):
        validate_activation(service.store.path,receipt,allow_real_execution=True)
    runner.shutdown()


def test_activation_binds_explicit_new_game_to_its_real_version_and_scope(tmp_path):
    runner, service, _ = setup_runtime(tmp_path, [make_game(1)])
    try:
        runner.tick()
        wait_for(runner, lambda: not runner.active)
        row = task_rows(service)[0]
        assert row["status"] == "succeeded"
        receipt = tmp_path / "new-game-activation.json"
        payload = {"authorized": True, "read_acceptance_passed": True,
                   "validated_game_id": row["game_id"], "validated_version_id": row["published_version_id"],
                   "allowed_game_ids": [row["game_id"]], "allowed_stages": ["FINAL_PREGAME"]}
        receipt.write_text(json.dumps(payload))
        validated = validate_activation(service.store.path, receipt, allow_real_execution=True)
        assert validated["allowed_game_ids"] == [row["game_id"]]
        assert validated["allowed_stages"] == ["FINAL_PREGAME"]
        assert validated["forecast_model"] == "deepseek-v4-flash"
        receipt.write_text(json.dumps({**payload, "forecast_model": "deepseek-v4-pro"}))
        assert validate_activation(service.store.path, receipt, allow_real_execution=True)["forecast_model"] == "deepseek-v4-pro"
        receipt.write_text(json.dumps({**payload, "forecast_model": "unqualified-model"}))
        with pytest.raises(ValueError, match="activation_forecast_model_invalid"):
            validate_activation(service.store.path, receipt, allow_real_execution=True)
        receipt.write_text(json.dumps(payload))
        with pytest.raises(ValueError, match="matching_future_game"):
            validate_activation(service.store.path, receipt, allow_real_execution=True, required_game_id="different-game")
        receipt.write_text(json.dumps({**payload, "allowed_game_ids": ["different-game"]}))
        with pytest.raises(ValueError, match="outside_scope"):
            validate_activation(service.store.path, receipt, allow_real_execution=True)
        # Old receipts still require the original Heat game unless an explicit
        # validated game is supplied; a non-Heat version cannot silently replace it.
        payload.pop("validated_game_id")
        receipt.write_text(json.dumps(payload))
        with pytest.raises(ValueError, match="matching_future_game"):
            validate_activation(service.store.path, receipt, allow_real_execution=True)
    finally:
        runner.shutdown()


def test_migration_idempotent_and_preserves_version_rows(tmp_path):
    runner,service,_=setup_runtime(tmp_path)
    try:
        first=migrate_runtime(service.store.path,runner.config)
        second=migrate_runtime(service.store.path,runner.config)
        assert first==second and first["immutable_versions_unchanged"]
    finally:runner.shutdown()


def test_changed_activation_is_validated_before_publication_and_without_restart(tmp_path,monkeypatch):
    from . import scheduler_runtime
    runner,service,clock=setup_runtime(tmp_path)
    receipt=tmp_path/'selection.json';receipt.write_text('first')
    runner.activation_receipt=receipt
    runner.allowed_stages=frozenset({'UPDATED_PREVIEW'})
    approved=[make_game(1)['id']]
    def validate(*args,**kwargs):
        if receipt.read_text()=='invalid':raise ValueError('activation_authorization_and_read_acceptance_required')
        return {'allowed_game_ids':approved,'allowed_stages':['UPDATED_PREVIEW'],'forecast_model':'deepseek-v4-flash'}
    monkeypatch.setattr(scheduler_runtime,'validate_activation',validate)
    seen=[];monkeypatch.setattr(runner,'_pump_publications',lambda:seen.append(runner.allowed_game_ids))
    try:
        runner.tick();assert seen==[frozenset(approved)] and not runner.active
        approved.append(make_game(2)['id']);receipt.write_text('second')
        runner.tick();assert seen[-1]==frozenset(approved) and not runner.active
        receipt.write_text('invalid');count=len(seen)
        runner.tick();assert len(seen)==count and not runner.active
    finally:runner.shutdown()


@pytest.mark.parametrize("error,phase,expected",[
    ("bad","claim","claim_failure"),("bad","research","research_failure"),
    ("bad","forecasting","forecasting_failure"),("bad","editorial","editorial_failure"),
    ("unsupported_current_status_claim","editorial","validation_failure"),
    ("bad","publish","publish_failure"),("runtime_lease_lost",None,"lease_failure"),
    ("runtime_deadline_expired",None,"deadline_expired"),("pregame_generation_closed",None,"tipoff_crossed"),
    ("generation_policy_revision_mismatch",None,"cancelled_by_policy"),
    ("schedule_revision_changed",None,"schedule_revision_changed"),("bad",None,"pre_execution_failure")])
def test_failure_taxonomy(error,phase,expected):
    assert failure_category(RuntimeError(error),phase)==expected


def test_configuration_is_explicit_and_validated():
    assert RuntimeConfig.from_env({"COURTVISION_SCHEDULER_MAX_CONCURRENCY":"4"}).max_concurrency==4
    with pytest.raises(ValueError):RuntimeConfig(max_concurrency=2,final_reserved=2)
    with pytest.raises(ValueError):RuntimeConfig(heartbeat_seconds=120)


def test_pre_execution_failure_is_terminal_and_observable_not_infinite_spawn(tmp_path):
    runner,service,clock=setup_runtime(tmp_path,[make_game(1)])
    runner.service_builder=broken_service
    try:
        runner.tick();wait_for(runner,lambda:not runner.active)
        assert task_rows(service)[0]["status"]=="cancelled"
        assert runner.tick()["dispatched"]==0
        report=status_report(service.store.path,at=clock())
        assert report["events"][0]["failure_stage"]=="pre_execution_failure"
    finally:runner.shutdown()


@publication_integration
def test_runtime_game_scope_does_not_execute_unapproved_match(tmp_path):
    runner,service,_=setup_runtime(tmp_path)
    runner.allowed_game_ids=frozenset([make_game(1)["id"]])
    try:
        assert runner.tick()["dispatched"]==1
        wait_for(runner,lambda:not runner.active)
        assert [row["status"] for row in task_rows(service)]==["succeeded","pending"]
    finally:runner.shutdown()


def test_leader_loss_stops_orphan_worker_without_another_tick(tmp_path):
    runner,service,_=setup_runtime(tmp_path,[make_game(1,behavior={"research_sleep":8})])
    try:
        runner.tick();wait_for(runner,lambda:any(w.owner_token for w in runner.active.values()))
        worker=next(iter(runner.active.values()))
        with service.store.connect() as db:
            db.execute("UPDATE scheduler_runtime_leader SET run_id='replacement-run' WHERE singleton_id=1")
        until=time.monotonic()+5
        while worker.process.is_alive() and time.monotonic()<until:time.sleep(.03)
        assert not worker.process.is_alive()
        assert task_rows(service)[0]["status"]=="cancelled"
        assert service.tasks.attempts_snapshot()[0]["failure_stage"]=="lease_failure"
    finally:runner.shutdown()


@pytest.mark.skipif(os.name!="nt",reason="Windows JobObject descendant acceptance")
def test_hard_stop_also_terminates_search_bridge_descendant(tmp_path):
    pid_file=tmp_path/"bridge.pid"
    runner,service,clock=setup_runtime(tmp_path,[make_game(1,behavior={"research_sleep":8,"bridge_pid_file":str(pid_file)})])
    try:
        runner.tick();wait_for(runner,lambda:pid_file.is_file())
        pid=int(pid_file.read_text())
        assert process_alive(pid)
        clock.at=TIP-timedelta(seconds=14)
        runner._reap()
        until=time.monotonic()+3
        while process_alive(pid) and time.monotonic()<until:time.sleep(.02)
        assert not process_alive(pid)
        assert task_rows(service)[0]["status"]=="expired"
    finally:runner.shutdown()


@publication_integration
def test_final_crossing_tipoff_preserves_previous_legal_version_and_article(tmp_path):
    first=make_game(1)
    runner,service,clock=setup_runtime(tmp_path,[first])
    clock.at=TIP-timedelta(minutes=20)
    try:
        runner.tick();wait_for(runner,lambda:not runner.active)
        before=service.selected(service.tasks.registered_game(first["id"]))
        assert before["version"]["stage"]=="EARLY_PREVIEW"
        old_hash=before["version"]["payload_sha256"]
        clock.at=TIP-timedelta(minutes=5)
        changed=make_game(1,behavior={"simulated_seconds":301})
        runner.data_source=lambda:{"games":[changed]}
        runner.last_plan_at=None
        runner.tick();wait_for(runner,lambda:not runner.active)
        assert task_rows(service)[0]["status"]=="expired"
        failures=[a for a in service.tasks.attempts_snapshot() if a["stage"]=="FINAL_PREGAME"]
        assert failures[0]["failure_stage"]=="tipoff_crossed"
        clock.at=TIP
        _,response=build_prediction_versions_response(data={"games":service.tasks.registered_games()},version_service=service)
        selected=response["predictions"][first["id"]]
        assert selected["status"]=="locked" and selected["version_id"]==before["version_id"]
        assert selected["version"]["payload_sha256"]==old_hash
        assert selected["version"]["stage"]=="EARLY_PREVIEW"
        assert selected["version"]["judgment"]["analysis_article"]==before["version"]["judgment"]["analysis_article"]
    finally:runner.shutdown()


@publication_integration
def test_recovery_after_commit_restores_attempt_version_lineage_without_republishing(tmp_path):
    runner,service,_=setup_runtime(tmp_path,[make_game(1)])
    try:
        runner.tick();wait_for(runner,lambda:not runner.active)
        before=task_rows(service)[0]["published_version_id"]
        with service.tasks.transaction() as db:
            db.execute("UPDATE scheduler_runtime_attempts SET status='running',published_version_id=NULL,published_at=NULL")
        assert service.tasks.list_due_tasks()==[]
        recovered=service.tasks.attempts_snapshot()[0]
        assert recovered["status"]=="succeeded" and recovered["published_version_id"]==before and recovered["published_at"]
        with service.store.connect() as db:assert db.execute("SELECT count(*) FROM prediction_versions").fetchone()[0]==1
    finally:runner.shutdown()


def test_two_same_time_finals_have_concurrent_claims_and_independent_lease_failures(tmp_path):
    # Both fake Research clients stop deliberately before any publication call.
    runner,service,_=setup_runtime(tmp_path,[make_game(1,behavior={"fail":True,"research_sleep":2}),make_game(2,behavior={"fail":True,"research_sleep":2})])
    try:
        assert runner.tick()["dispatched"]==2
        wait_for(runner,lambda:all(r["status"]=="running" for r in task_rows(service)))
        rows=task_rows(service)
        assert len({r["owner_token"] for r in rows})==2
        assert len(service.tasks.attempts_snapshot())==2
        assert runner.tick()["dispatched"]==0
        wait_for(runner,lambda:not runner.active)
        attempts=service.tasks.attempts_snapshot()
        assert all(a["failure_stage"]=="research_failure" for a in attempts)
        assert len({a["runtime_attempt_id"] for a in attempts})==2
        with service.store.connect() as db:assert db.execute("SELECT count(*) FROM prediction_versions").fetchone()[0]==0
    finally:runner.shutdown()


def test_low_stage_cannot_starve_two_final_slots_without_publication_dependency(tmp_path):
    games=[make_game(1,behavior={"fail":True,"research_sleep":2}),make_game(2,behavior={"fail":True,"research_sleep":2}),
           make_game(3,tip=TIP+timedelta(hours=2),behavior={"research_sleep":8})]
    runner,service,clock=setup_runtime(tmp_path,games)
    with service.store.connect() as db:
        db.execute("UPDATE generation_tasks SET scheduled_for=?,next_attempt_at=? WHERE stage='FINAL_PREGAME' AND game_id IN (?,?)",
                   ((clock.at+timedelta(seconds=1)).isoformat(),(clock.at+timedelta(seconds=1)).isoformat(),games[0]["id"],games[1]["id"]))
    try:
        runner.tick();wait_for(runner,lambda:any(w.owner_token for w in runner.active.values()))
        assert len(runner.active)==1
        clock.at+=timedelta(seconds=1)
        assert runner.tick()["dispatched"]==2
        wait_for(runner,lambda:all(r["status"]=="running" for r in task_rows(service)[:2]))
        assert len(runner.active)==3
        assert sum(w.task["stage"]=="FINAL_PREGAME" for w in runner.active.values())==2
    finally:runner.shutdown()


def test_timeout_isolated_from_another_completed_failure_without_publication(tmp_path):
    runner,service,clock=setup_runtime(tmp_path,[make_game(1,behavior={"research_sleep":8}),make_game(2,behavior={"fail":True})])
    try:
        runner.tick();wait_for(runner,lambda:task_rows(service)[1]["status"]=="expired" and any(w.owner_token for w in runner.active.values()))
        clock.at=TIP-timedelta(seconds=14)
        runner._reap()
        attempts={a["game_id"]:a for a in service.tasks.attempts_snapshot()}
        assert attempts[make_game(1)["id"]]["failure_stage"]=="deadline_expired"
        assert attempts[make_game(2)["id"]]["failure_stage"]=="research_failure"
        assert not runner.active
    finally:runner.shutdown()


def test_same_game_repeated_tick_does_not_dispatch_second_generation(tmp_path):
    runner,service,_=setup_runtime(tmp_path,[make_game(1,behavior={"research_sleep":8})])
    try:
        runner.tick();wait_for(runner,lambda:any(w.owner_token for w in runner.active.values()))
        for _ in range(5):assert runner.tick()["dispatched"]==0
        assert len(runner.active)==1 and len(service.tasks.attempts_snapshot())==1
    finally:runner.shutdown()


def test_restart_respects_preexisting_valid_claim_and_counts_reserved_capacity(tmp_path):
    runner,service,clock=setup_runtime(tmp_path,[make_game(1)])
    claim=service.tasks.claim(task_rows(service)[0]["task_id"])
    try:
        assert runner.tick()["dispatched"]==0
        assert service.tasks.get_task(claim["task_id"])["owner_token"]==claim["owner_token"]
        assert len(runner.inherited_running)==1
        report=status_report(service.store.path,at=clock())
        assert report["workers"]["busy"]==1
        assert report["workers"]["active"][0]["kind"]=="inherited_valid_claim"
    finally:runner.shutdown()


def test_tipoff_passed_dispatches_no_new_generation(tmp_path):
    runner,service,clock=setup_runtime(tmp_path,[make_game(1)])
    clock.at=TIP
    try:
        assert runner.tick()["dispatched"]==0
        assert not runner.active and service.tasks.attempts_snapshot()==[]
        assert task_rows(service)[0]["status"] in {"missed","expired"}
    finally:runner.shutdown()


def test_final_only_scope_waits_until_t5_and_preserves_early_version(tmp_path):
    runner, service, clock = setup_runtime(tmp_path, [make_game(1)])
    try:
        clock.at = TIP-timedelta(minutes=20)
        runner.tick(); wait_for(runner, lambda:not runner.active)
        early = service.selected(service.tasks.registered_game(make_game(1)["id"]))
        assert early["version"]["stage"] == "EARLY_PREVIEW"
        runner.allowed_stages = frozenset(["FINAL_PREGAME"])
        clock.at = TIP-timedelta(minutes=5, seconds=1)
        assert runner.tick()["dispatched"] == 0
        assert task_rows(service)[0]["attempt_count"] == 0
        clock.at = TIP-timedelta(minutes=5)
        assert runner.tick()["dispatched"] == 1
        wait_for(runner, lambda:not runner.active)
        final = service.selected(service.tasks.registered_game(make_game(1)["id"]))
        assert final["version"]["stage"] == "FINAL_PREGAME" and final["version_id"] != early["version_id"]
        assert runner.tick()["dispatched"] == 0
        with service.store.connect() as db:
            assert db.execute("SELECT count(*) FROM prediction_versions").fetchone()[0] == 2
            assert db.execute("SELECT payload_sha256 FROM prediction_versions WHERE version_id=?",(early["version_id"],)).fetchone()[0] == early["version"]["payload_sha256"]
        clock.at = TIP
        assert service.selected(service.tasks.registered_game(make_game(1)["id"]))["version_id"] == final["version_id"]
    finally:runner.shutdown()


@pytest.mark.parametrize('game_id',['espn:401902644','espn:499999999'])
def test_production_worker_uses_shared_current_roster_preflight(monkeypatch, tmp_path,game_id):
    from .scheduler_worker import production_service
    from v2.pregame_system import current_roster_input
    from v2.pregame_system import forecasting_workflow, editorial_contract
    monkeypatch.setattr(forecasting_workflow,"run_two_stage",forecasting_workflow.run_two_stage)
    if hasattr(editorial_contract,"validate_editorial_boundaries"):
        monkeypatch.setattr(editorial_contract,"validate_editorial_boundaries",editorial_contract.validate_editorial_boundaries)
    order=[]
    def refresh(game):
        order.append("roster")
        return {"status":"passed","teams":{
            str(game["away"]["id"]):{"team_id":game["away"]["id"],"roster_basis":"official_team_roster","count":1,
                "observed_at":"2026-10-01T00:00:00Z","source_type":"official_team_roster",
                "source_url":"https://www.nba.com/lal/roster","raw_response_sha256":"away-hash",
                "parser_version":"roster-v1","players":[{"name":"Luka Doncic"}]},
            str(game["home"]["id"]):{"team_id":game["home"]["id"],"roster_basis":"official_team_roster","count":1,
                "observed_at":"2026-10-01T00:00:00Z","source_type":"official_team_roster",
                "source_url":"https://www.nba.com/gs/roster","raw_response_sha256":"home-hash",
                "parser_version":"roster-v1","players":[{"name":"Stephen Curry"}]}}}
    monkeypatch.setattr(current_roster_input,"refresh_matchup_rosters",refresh)
    versions=PredictionVersionStore(tmp_path/"roster.sqlite")
    tasks=type("Tasks",(),{"active_runtime_claim":{"attempt_count":1}})()
    service=production_service(versions,tasks,{},lambda *args,**kwargs:order.append("phase") or 300)
    game={"id":game_id,"status":"scheduled","tipoff":"2026-10-20T23:00:00Z","registered_game":True,
        "away":{"id":1610612747,"name":"Los Angeles Lakers"},"home":{"id":1610612744,"name":"Golden State Warriors"}}
    prepared=service.pre_generation_preparer({"games":[game]},game_id)
    assert order == ["phase","roster"]
    assert prepared["games"][0]["current_roster_preflight"]["status"] == "ready"


def test_short_final_budget_declines_without_any_provider_phase(tmp_path):
    runner,service,clock=setup_runtime(tmp_path,[make_game(1)])
    clock.at=TIP-timedelta(seconds=20)
    try:
        runner.tick();wait_for(runner,lambda:not runner.active)
        row=task_rows(service)[0]
        assert row["status"]=="expired" and row["attempt_count"]==0
        assert row["last_error"]=="runtime_budget_insufficient"
        assert service.tasks.attempts_snapshot()==[]
    finally:runner.shutdown()

def test_publication_pump_recovers_and_dispatches_without_generation(tmp_path, monkeypatch):
    from . import opening_publish
    from . import publication_deployment
    class Checks:
        def backfill(self,scope):pass
        def claim(self):return None
    monkeypatch.setattr(publication_deployment,'DeploymentChecks',lambda queue:Checks())
    delivered = []
    class Queue:
        root = tmp_path/'delivery'
        claimed = False
        def discover(self, database, **scope):
            assert scope['game_ids'] == frozenset({'espn:401909089'})
            assert scope['stages'] == frozenset({'FINAL_PREGAME'})
            return {'eligible':1,'errors':[]}
        def claim(self, **scope):
            assert scope['game_ids'] == frozenset({'espn:401909089'})
            if self.claimed:return None
            self.claimed=True
            return {'version_id':'recovered-final'}
        def run(self, job, publisher):return publisher(job)
    queue=Queue()
    monkeypatch.setenv('COURTVISION_PUBLIC_PUBLISH_ROOT',str(tmp_path/'dirty-development'))
    monkeypatch.setattr(opening_publish,'publication_queue',lambda database:queue)
    def publish(job, **kwargs):
        delivered.append(job['version_id'])
        return {'status':'succeeded','version_id':job['version_id']}
    monkeypatch.setattr(opening_publish,'publish_saved',publish)
    runner,service,clock=setup_runtime(tmp_path)
    runner.allowed_game_ids=frozenset({'espn:401909089'});runner.allowed_stages=frozenset({'FINAL_PREGAME'})
    try:
        runner._pump_publications()
        runner.public_jobs['recovered-final'].result(timeout=2)
        runner._pump_publications()
        assert delivered==['recovered-final']
        assert runner.active=={} and runner.public_jobs=={}
    finally:runner.shutdown()


def test_cold_worker_limits_native_thread_pools_before_model_imports():
    import os,subprocess,sys
    names=('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS')
    env={k:v for k,v in os.environ.items()if k not in names}
    result=subprocess.run([sys.executable,'-c',
        'import os,json;import v2.frontend.scheduler_worker;print(json.dumps({k:os.environ.get(k)for k in '+repr(names)+'}))'],
        cwd=Path(__file__).resolve().parents[2],env=env,capture_output=True,text=True,timeout=30,check=True)
    assert json.loads(result.stdout)=={key:'1'for key in names}
