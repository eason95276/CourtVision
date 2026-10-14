"""Gated local scheduler dispatcher, independent of the web server.

Importing or reading status never starts execution. Real activation is a separate
operator action, requiring a published-version/read-acceptance receipt and flag.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import multiprocessing
import os
import pickle
import signal
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from v2.pregame_system.prediction_versions import DEFAULT_DATABASE, _instant, _utc
from .export_data import DEFAULT_OUTPUT, read_json
from .scheduler_runtime_config import RuntimeConfig
from .scheduler_worker import generation_worker


def readonly_connection(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def validate_activation(database, receipt_path, *, allow_real_execution=False,
                        required_game_id=None):
    """Fail before constructing any writable store or loading model credentials."""
    if not allow_real_execution or receipt_path is None:
        raise ValueError("real_execution_requires_explicit_flag_and_activation_receipt")
    receipt = read_json(Path(receipt_path))
    required_game_id = required_game_id or receipt.get("validated_game_id") or "espn:401902644"
    if not isinstance(required_game_id, str) or not required_game_id:
        raise ValueError("activation_validated_game_id_invalid")
    if receipt.get("authorized") is not True or receipt.get("read_acceptance_passed") is not True:
        raise ValueError("activation_authorization_and_read_acceptance_required")
    version_id = receipt.get("validated_version_id")
    if not isinstance(version_id, str) or not version_id:
        raise ValueError("activation_successful_version_required")
    with closing(readonly_connection(database)) as db:
        row = db.execute("SELECT payload_json,payload_sha256 FROM prediction_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None or hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise ValueError("activation_version_missing_or_corrupt")
        payload = json.loads(row[0])
        if (payload.get("game_id") != required_game_id or payload.get("policy_version") != "scheduler-v2"
                or payload.get("stage") not in {"EARLY_PREVIEW", "UPDATED_PREVIEW", "FINAL_PREGAME"}):
            raise ValueError("activation_requires_matching_future_game_acceptance")
    scope = receipt.get("allowed_game_ids", [required_game_id])
    if not isinstance(scope, list) or not scope or any(not isinstance(item, str) or not item for item in scope):
        raise ValueError("activation_game_scope_invalid")
    if required_game_id not in scope:
        raise ValueError("activation_validated_game_outside_scope")
    stages = receipt.get("allowed_stages")
    if stages is not None and (not isinstance(stages,list) or not stages or
            any(s not in {"EARLY_PREVIEW","UPDATED_PREVIEW","FINAL_PREGAME"} for s in stages)):
        raise ValueError("activation_stage_scope_invalid")
    return {"validated_version_id": version_id, "authorization": "explicit_operator_receipt",
            "allowed_game_ids": sorted(set(scope)), "allowed_stages":stages}


def status_report(database, *, config=None, at=None):
    """Read-only even for a missing DB/schema. No store/service constructors."""
    config = config or RuntimeConfig()
    at = at or datetime.now(timezone.utc)
    report = {"status": "unavailable", "as_of": _utc(at), "database": str(Path(database).resolve()),
              "scheduler": None, "workers": {"total": config.max_concurrency, "busy": 0},
              "tasks": {}, "upcoming_final": [], "failed_tasks": [], "attempts": []}
    if not Path(database).is_file():
        report["reason"] = "database_missing"
        return report
    try:
        with closing(readonly_connection(database)) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "generation_tasks" not in names:
                report["reason"] = "task_schema_unavailable"
                return report
            report["tasks"] = {row[0]: row[1] for row in db.execute("SELECT status,count(*) FROM generation_tasks GROUP BY status")}
            report["failed_tasks"] = [dict(row) for row in db.execute("SELECT task_id,game_id,stage,status,attempt_count,last_error FROM generation_tasks WHERE status IN ('failed','expired') ORDER BY finished_at DESC LIMIT 20")]
            report["quarantined_tasks"] = [dict(row) for row in db.execute("SELECT task_id,game_id,stage,status,attempt_count,last_error FROM generation_tasks WHERE status='cancelled' AND (last_error LIKE 'runtime_recovery_requires_review%' OR last_error LIKE 'runtime_heartbeat%') ORDER BY finished_at DESC LIMIT 20")]
            for row in db.execute("SELECT task_id,game_id,stage,status,scheduled_for,deadline_at FROM generation_tasks WHERE stage='FINAL_PREGAME' AND status IN ('pending','failed','running') ORDER BY scheduled_for LIMIT 20"):
                item = dict(row)
                remaining = (_instant(row["deadline_at"], "deadline_at") - at).total_seconds()
                item.update(remaining_seconds=round(remaining, 3), deadline_risk=remaining < config.final_min_budget_seconds + config.publish_margin_seconds)
                report["upcoming_final"].append(item)
            if "scheduler_runtime_leader" in names:
                row = db.execute("SELECT * FROM scheduler_runtime_leader WHERE singleton_id=1").fetchone()
                if row:
                    stored = dict(row)
                    workers = json.loads(stored.pop("workers_json"))
                    stored["healthy"] = stored["status"] == "running" and _instant(stored["lease_until"], "lease_until") > at
                    if stored["status"] == "running" and not stored["healthy"]:
                        stored["status"] = "stale"
                    report["scheduler"] = stored
                    report["workers"] = {"total": json.loads(stored["config_json"])["max_concurrency"], "busy": len(workers), "active": workers}
            if "scheduler_runtime_attempts" in names:
                columns = [row[1] for row in db.execute("PRAGMA table_info(scheduler_runtime_attempts)") if row[1] != "owner_token"]
                report["attempts"] = [dict(row) for row in db.execute("SELECT " + ",".join(columns) + " FROM scheduler_runtime_attempts ORDER BY rowid DESC LIMIT 20")]
            if "scheduler_runtime_events" in names:
                report["events"] = [dict(row) for row in db.execute("SELECT * FROM scheduler_runtime_events ORDER BY rowid DESC LIMIT 20")]
            report["status"] = "available"
    except (sqlite3.Error, ValueError, json.JSONDecodeError) as error:
        report.update(status="unavailable", reason=type(error).__name__)
    return report


@dataclass
class ActiveWorker:
    task: dict[str, Any]
    process: Any
    pipe: Any
    spawned_at: datetime
    owner_token: str | None = None
    attempt_id: str | None = None
    phase: str = "pre_execution"
    terminal: dict[str, Any] | None = None


class PersistentScheduler:
    """A bounded dispatcher. No unbounded executor queue, no shared AI clients."""
    def __init__(self, service, *, config=None, data_source=None, process_context=None,
                 worker_target=generation_worker, service_builder=None, allowed_game_ids=None, allowed_stages=None, health_clock=None):
        self.service = service
        self.tasks = service.tasks
        self.config = config or RuntimeConfig()
        self.data_source = data_source or (lambda: read_json(DEFAULT_OUTPUT) if DEFAULT_OUTPUT.is_file() else {"games": []})
        self.context = process_context or multiprocessing.get_context("spawn")
        self.worker_target = worker_target
        self.service_builder = service_builder
        self.final_readiness_collector = None
        self.readiness_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='final-http-readiness')
        self.readiness_jobs={}
        self.public_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='saved-final-publication')
        self.public_jobs={}
        self.allowed_game_ids = None if allowed_game_ids is None else frozenset(allowed_game_ids)
        self.allowed_stages = None if allowed_stages is None else frozenset(allowed_stages)
        # Process liveness is real wall time; offline business clocks may compress
        # a 141-second generation into a fraction of a second.
        self.health_clock = health_clock or (lambda: datetime.now(timezone.utc))
        self.run_id = str(uuid.uuid4())
        self.stop_event = threading.Event()
        self.active: dict[str, ActiveWorker] = {}
        self.inherited_running = []
        self.last_error = None
        self.last_plan_at = None
        self.runtime_data = {"games": []}
        self.owns_leader = False
        try:
            pickle.dumps(self.service.store.clock)
            self.worker_clock = self.service.store.clock
        except (pickle.PickleError, AttributeError, TypeError):
            # The standard store clock is a local lambda; production children
            # construct their own UTC clock instead of pickling that closure.
            self.worker_clock = None
        with self.tasks.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS scheduler_runtime_leader (
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),run_id TEXT NOT NULL,pid INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,last_tick_at TEXT NOT NULL,lease_until TEXT NOT NULL,status TEXT NOT NULL,
                config_json TEXT NOT NULL,workers_json TEXT NOT NULL,last_error TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS scheduler_runtime_events (
                event_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,task_id TEXT,game_id TEXT,happened_at TEXT NOT NULL,
                event_type TEXT NOT NULL,failure_stage TEXT,detail_json TEXT NOT NULL)""")

    def _event(self, kind, task=None, *, failure_stage=None, detail=None):
        with self.tasks.transaction() as db:
            db.execute("INSERT INTO scheduler_runtime_events VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()),self.run_id,(task or {}).get("task_id"),(task or {}).get("game_id"),
                 _utc(self._now()),kind,failure_stage,json.dumps(detail or {})))

    def _now(self):
        return self.service.store.clock()

    def _leader(self):
        at = self.health_clock()
        with self.tasks.transaction() as db:
            row = db.execute("SELECT * FROM scheduler_runtime_leader WHERE singleton_id=1").fetchone()
            if row and row["run_id"] != self.run_id and _instant(row["lease_until"], "lease_until") > at and row["status"] == "running":
                return False
            if row and row["run_id"] == self.run_id and self.owns_leader and _instant(row["lease_until"], "lease_until") <= at:
                return False  # An expired leader cannot quietly revive its old workers.
            workers = [{"task_id": w.task["task_id"], "game_id": w.task["game_id"], "stage": w.task["stage"],
                        "pid": w.process.pid, "phase": w.phase, "attempt_id": w.attempt_id} for w in self.active.values()]
            workers.extend({**row,"kind":"inherited_valid_claim","pid":None} for row in self.inherited_running)
            acquired = row["acquired_at"] if row and row["run_id"] == self.run_id else _utc(at)
            db.execute("""INSERT INTO scheduler_runtime_leader VALUES(1,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(singleton_id) DO UPDATE SET run_id=excluded.run_id,pid=excluded.pid,
                acquired_at=excluded.acquired_at,last_tick_at=excluded.last_tick_at,lease_until=excluded.lease_until,
                status=excluded.status,config_json=excluded.config_json,workers_json=excluded.workers_json,last_error=excluded.last_error""",
                (self.run_id, os.getpid(), acquired, _utc(at), _utc(at+timedelta(seconds=self.config.leader_lease_seconds)),
                 "running", json.dumps(self.config.public()), json.dumps(workers), self.last_error))
        self.owns_leader = True
        return True

    def _read_messages(self, worker):
        try:
            while worker.pipe.poll():
                message = worker.pipe.recv()
                if message["kind"] == "claimed":
                    worker.owner_token = message["owner_token"]
                    worker.attempt_id = message["attempt_id"]
                elif message["kind"] == "phase":
                    worker.phase = message["phase"]
                else:
                    worker.terminal = message
        except (EOFError, OSError):
            pass

    def _stop_worker(self, worker, reason, failure_stage, *, status="expired"):
        try:
            row = self.tasks.get_task(worker.task["task_id"])
            # Successful publication wins a timeout/reaping race; never overwrite it.
            if row and row["status"] != "succeeded":
                if row["status"] == "running" and worker.owner_token is None:
                    with closing(self.service.store.connect()) as db:
                        own = db.execute("SELECT 1 FROM scheduler_runtime_attempts WHERE task_id=? AND run_id=? AND owner_token=?",
                                         (row["task_id"], self.run_id, row["owner_token"])).fetchone()
                    if own:
                        worker.owner_token = row["owner_token"]
                if row["status"] != "running" or row["owner_token"] == worker.owner_token:
                    self.tasks.stop_task(row["task_id"], reason, failure_stage, owner_token=worker.owner_token, status=status)
        finally:
            # A busy DB must not leave a provider call running past its hard budget.
            # Unreconciled journal rows are quarantined after lease expiry.
            if worker.process.is_alive():
                if os.name != "nt":
                    try:
                        os.killpg(worker.process.pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        worker.process.terminate()
                else:
                    worker.process.terminate()
            worker.process.join(timeout=2)
            if worker.process.is_alive():
                worker.process.kill()
                worker.process.join(timeout=2)

    def _reap(self):
        at = self._now()
        for identity, worker in list(self.active.items()):
            self._read_messages(worker)
            row = self.tasks.get_task(identity)
            stop_at = _instant(worker.task["deadline_at"], "deadline_at") - timedelta(seconds=self.config.publish_margin_seconds)
            if worker.process.is_alive() and at >= stop_at:
                crossed=worker.task['stage']=='FINAL_PREGAME'and at>=_instant(worker.task['deadline_at'],'deadline_at')
                self._stop_worker(worker, 'pregame_generation_closed'if crossed else'runtime_deadline_expired',
                    'tipoff_crossed'if crossed else'deadline_expired')
            elif worker.process.is_alive() and worker.owner_token and (row["status"] != "running" or row["owner_token"] != worker.owner_token):
                if row["status"] != "succeeded":
                    self._stop_worker(worker, "generation_claim_lost", "lease_failure")
            if not worker.process.is_alive():
                worker.process.join(timeout=0)
                self._read_messages(worker)
                row = self.tasks.get_task(identity)
                if worker.terminal and worker.terminal.get("kind")=="failed" and row["status"]!="succeeded":
                    self._event("worker_failure",worker.task,failure_stage=worker.terminal.get("failure_stage"),
                                detail={k:v for k,v in worker.terminal.items() if k not in {"kind","task_id"}})
                if row["status"] == "running" and worker.terminal is None:
                    self._stop_worker(worker, "runtime_recovery_requires_review:worker_exit", "lease_failure", status="cancelled")
                elif row["status"] == "pending" and (worker.terminal is None or worker.terminal.get("kind")=="failed"):
                    category=(worker.terminal or {}).get("failure_stage","pre_execution_failure")
                    self.tasks.stop_task(identity, "pre_execution_failure:worker_exit", category, status="cancelled")
                    self._event("worker_failed_before_claim",worker.task,failure_stage=category,
                                detail={"error_type":(worker.terminal or {}).get("error_type"),"exit_code":worker.process.exitcode})
                worker.pipe.close()
                if (row['status']=='succeeded'and worker.task['stage']=='FINAL_PREGAME'
                        and (worker.terminal or{}).get('version_id')and os.environ.get('COURTVISION_PUBLIC_PUBLISH_ROOT')):
                    from .opening_publish import publish_final
                    version_id=worker.terminal['version_id']
                    self.public_jobs[version_id]=self.public_pool.submit(publish_final,worker.task['game_id'],version_id,database=self.service.store.path)
                del self.active[identity]

    def tick(self):
        if self.stop_event.is_set():
            return {"status": "stopping", "dispatched": 0}
        try:
            if not self._leader():
                self.stop_event.set()
                return {"status": "leader_unavailable", "dispatched": 0}
            self._reap()
            for version_id,job in list(self.public_jobs.items()):
                if not job.done():continue
                try:self._event('saved_final_publication',detail=job.result())
                except Exception as error:self._event('saved_final_publication_failed',detail={'version_id':version_id,'error_type':type(error).__name__})
                del self.public_jobs[version_id]
            at = self._now()
            if self.last_plan_at is None or (at-self.last_plan_at).total_seconds() >= self.config.planner_interval_seconds:
                from .pregame_scheduler import PregameScheduler
                from .generation_input import merge_registered_games
                data = self.data_source()
                if self.allowed_game_ids is None:
                    plan = PregameScheduler(self.service).run_due_tasks(data, plan_only=True)
                else:
                    from v2.pregame_system.scheduler_policy import SchedulerPolicyPlanner
                    scoped = merge_registered_games(data, self.tasks)
                    plan = {"registration_errors": []}
                    for game in scoped["games"]:
                        if game["id"] not in self.allowed_game_ids:
                            continue
                        try:
                            if game.get("status") != "scheduled":
                                self.tasks.cancel_game(game)
                                self.service.selected(game)
                            elif _instant(game["tipoff"], "scheduled_tipoff") <= at:
                                self.service.selected(game)
                            else:
                                self.tasks.ensure_schedule(game)
                                SchedulerPolicyPlanner(self.tasks).plan_game(game)
                        except Exception as error:
                            plan["registration_errors"].append({"game_id": game["id"], "error_type": type(error).__name__})
                if plan["registration_errors"]:
                    self.last_error = "planning_registration_error"
                self.runtime_data = merge_registered_games(data, self.tasks)
                self.last_plan_at = at
            dispatched = 0
            games_busy = {w.task["game_id"] for w in self.active.values()}
            with closing(self.service.store.connect()) as db:
                running = [dict(row) for row in db.execute("SELECT task_id,game_id,stage FROM generation_tasks WHERE status='running'")]
            foreign_running = [row for row in running if row["task_id"] not in self.active]
            self.inherited_running = foreign_running
            games_busy.update(row["game_id"] for row in foreign_running)
            for task in self.tasks.list_due_tasks(limit=100):
                if len(self.active)+len(foreign_running) >= self.config.max_concurrency:
                    break
                if task["task_id"] in self.active or task["game_id"] in games_busy:
                    continue
                if self.allowed_game_ids is not None and task["game_id"] not in self.allowed_game_ids:
                    continue
                if self.allowed_stages is not None and task["stage"] not in self.allowed_stages:
                    continue
                low_busy = sum(w.task["stage"] != "FINAL_PREGAME" for w in self.active.values()) + sum(row["stage"]!="FINAL_PREGAME" for row in foreign_running)
                if task["stage"] != "FINAL_PREGAME" and low_busy >= self.config.max_concurrency-self.config.final_reserved:
                    continue
                from v2.pregame_system.two_stage_lifecycle import readiness_for_task
                readiness=None
                metadata=json.loads(task.get('metadata_json')or'{}')
                if task['stage']=='FINAL_PREGAME'and metadata.get('lifecycle')=='early-final-v1':
                    future=self.readiness_jobs.get(task['task_id'])
                    if future is None:
                        self.readiness_jobs[task['task_id']]=self.readiness_pool.submit(readiness_for_task,self.service.store,task,self.final_readiness_collector)
                        continue
                    if not future.done():continue
                    try:readiness=future.result()
                    except Exception as error:
                        readiness={'ready':False,'next_check_at':_utc(at+timedelta(minutes=1)),
                            'reasons':['readiness_error:'+type(error).__name__],'llm_calls':0}
                    del self.readiness_jobs[task['task_id']]
                if readiness is not None and not readiness['ready']:
                    if readiness.get('next_check_at'):
                        with self.tasks.transaction()as db:
                            db.execute("UPDATE generation_tasks SET next_attempt_at=?,last_error='waiting_final_ready' WHERE task_id=? AND status IN ('pending','failed')",
                                (readiness['next_check_at'],task['task_id']))
                    continue
                receive, send = self.context.Pipe(duplex=False)
                process = self.context.Process(target=self.worker_target, args=(str(self.service.store.path), task["task_id"],
                    copy.deepcopy(self.runtime_data), self.config, self.run_id, send, self.service_builder, self.worker_clock),
                    name="courtvision-task-"+task["task_id"][:8])
                try:
                    process.start()
                except Exception:
                    receive.close(); send.close()
                    self.tasks.stop_task(task["task_id"], "pre_execution_failure:spawn", "pre_execution_failure", status="cancelled")
                    self.last_error = "worker_spawn_failure"
                    self._event("worker_spawn_failure",task,failure_stage="pre_execution_failure")
                    continue
                send.close()
                self.active[task["task_id"]] = ActiveWorker(task, process, receive, at)
                games_busy.add(task["game_id"])
                dispatched += 1
            self._leader()
            return {"status": "running", "dispatched": dispatched, "busy": len(self.active)}
        except Exception as error:
            self.last_error = type(error).__name__
            try:
                self._event("tick_failure",failure_stage="pre_execution_failure",detail={"error_type":type(error).__name__})
            except sqlite3.Error:
                pass
            return {"status": "tick_error", "error_type": type(error).__name__, "dispatched": 0}

    def run(self, *, stop_file=None, max_ticks=None):
        ticks = 0
        try:
            while not self.stop_event.is_set():
                if stop_file and Path(stop_file).exists():
                    break
                result = self.tick()
                ticks += 1
                if result["status"] == "leader_unavailable" or max_ticks is not None and ticks >= max_ticks:
                    break
                self.stop_event.wait(self.config.tick_seconds)
        finally:
            self.shutdown()

    def shutdown(self):
        self.stop_event.set()
        self.readiness_pool.shutdown(wait=False,cancel_futures=True)
        self.public_pool.shutdown(wait=False,cancel_futures=False)
        deadline = time.monotonic()+self.config.shutdown_grace_seconds
        while self.active and time.monotonic() < deadline:
            try:
                self._reap()
            except sqlite3.Error:
                pass
            if self.active:
                time.sleep(min(.05, max(0, deadline-time.monotonic())))
        for worker in list(self.active.values()):
            try:
                self._stop_worker(worker, "runtime_recovery_requires_review:shutdown", "lease_failure", status="cancelled")
            except sqlite3.Error:
                if worker.process.is_alive():
                    worker.process.terminate(); worker.process.join(timeout=2)
            worker.pipe.close()
        self.active.clear()
        if self.owns_leader:
            try:
                with self.tasks.transaction() as db:
                    db.execute("UPDATE scheduler_runtime_leader SET status='stopped',lease_until=?,workers_json='[]' WHERE run_id=?",
                               (_utc(self.health_clock()), self.run_id))
            except sqlite3.Error:
                pass


def build_service(database, config):
    from v2.pregame_system.prediction_versions import PredictionVersionStore
    from v2.pregame_system.scheduler_runtime_store import RuntimeTaskStore
    from .prediction_version_service import PredictionVersionService
    versions = PredictionVersionStore(database)
    tasks = RuntimeTaskStore(versions, lease_seconds=config.lease_seconds, max_attempts=config.max_attempts,
        heartbeat_seconds=config.heartbeat_seconds, final_min_budget_seconds=config.final_min_budget_seconds,
        publish_margin_seconds=config.publish_margin_seconds)
    return PredictionVersionService(store=versions, task_store=tasks)


def migrate_runtime(database, config):
    def old_rows():
        with closing(readonly_connection(database)) as db:
            return [tuple(row) for row in db.execute("SELECT * FROM prediction_versions ORDER BY version_id")]
    before = old_rows()
    service = build_service(database, config)
    PersistentScheduler(service, config=config)  # Schema only; never tick/claim/spawn.
    after = old_rows()
    if before != after:
        raise RuntimeError("runtime_migration_changed_immutable_versions")
    return {"status": "migrated", "immutable_versions_unchanged": True, "version_count": len(after)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("status", "migrate"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--data", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--once", action="store_true", help="One concurrent dispatch batch, then stop")
    parser.add_argument("--allow-real-execution", action="store_true")
    parser.add_argument("--activation-receipt", type=Path)
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args(argv)
    try:
        config = RuntimeConfig.from_env()
        if args.command == "status":
            print(json.dumps(status_report(args.database, config=config), ensure_ascii=False, indent=2))
            return 0
        if args.command == "migrate":
            print(json.dumps(migrate_runtime(args.database, config), indent=2))
            return 0
        if not (args.daemon ^ args.once):
            parser.error("Choose status, migrate, --daemon or --once")
        activation = validate_activation(args.database, args.activation_receipt, allow_real_execution=args.allow_real_execution)
        from .server import load_local_env
        load_local_env()
        config = RuntimeConfig.from_env()
        service = build_service(args.database, config)
        runner = PersistentScheduler(service, config=config,
            data_source=lambda: read_json(args.data) if args.data.is_file() else {"games": []},
            allowed_game_ids=activation["allowed_game_ids"], allowed_stages=activation["allowed_stages"])
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: runner.stop_event.set())
        if args.once:
            try:
                runner.tick()
                while runner.active and not runner.stop_event.wait(.1):
                    runner._reap()
                    runner._leader()
            finally:
                runner.shutdown()
        else:
            runner.run(stop_file=args.stop_file)
        return 0
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        print(json.dumps({"status": "refused", "reason": type(error).__name__, "code": str(error)[:200]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
