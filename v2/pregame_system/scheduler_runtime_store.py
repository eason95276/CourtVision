"""Opt-in runtime claims and attempt receipts; never starts a worker or model."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import timedelta
from typing import Any

from .generation_tasks import GenerationTaskStore, STAGE_RANK
from .prediction_versions import PredictionVersionStore, VersionStoreError, _clock_instant, _instant, _json, _utc
from .scheduler_policy import LOW_STAGE_STOP, POLICY_VERSION, normalized_policy


PHASES = {"pre_execution", "research", "forecasting", "editorial", "validation", "publish"}
TERMINAL = {"succeeded", "failed", "expired", "cancelled", "superseded", "missed"}


def safe_error_code(error):
    if error is None:
        return None
    prefix = str(error).split(":",1)[0]
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,120}",prefix) and not prefix.startswith(("sk-","Bearer","eyJ")):
        return prefix
    return type(error).__name__ if isinstance(error,BaseException) else "runtime_error"


def classify_failure(error: Any, phase: str | None = None) -> str:
    """Classify explicit error codes, retaining unknown pipeline stages honestly."""
    code = str(error or "")
    if any(word in code for word in ("tipoff", "pregame_generation_closed", "pregame_locked")):
        return "tipoff_crossed"
    if any(word in code for word in ("deadline", "budget", "stage_expired", "stage_window_closed")):
        return "deadline_expired"
    if any(word in code for word in ("lease", "claim_lost", "heartbeat")):
        return "lease_failure"
    if any(word in code for word in ("schedule_changed", "schedule_revision")):
        return "schedule_revision_changed"
    if any(word in code for word in ("policy", "revision_mismatch", "cancelled")):
        return "cancelled_by_policy"
    if code.startswith(("research_", "research_agent_")):
        return "research_failure"
    if code.startswith(("forecast_", "forecasting_")):
        return "forecasting_failure"
    if any(word in code for word in ("validation", "unsupported_", "judgment_", "invalid", "mismatch")):
        return "validation_failure"
    if code.startswith(("editorial_", "analyst_")):
        return "editorial_failure"
    if phase in PHASES:
        return {"pre_execution": "pre_execution_failure", "research": "research_failure",
                "forecasting": "forecasting_failure", "editorial": "editorial_failure",
                "validation": "validation_failure", "publish": "publish_failure"}[phase]
    return "pre_execution_failure"


class RuntimeTaskStore(GenerationTaskStore):
    """Explicit runtime activation adds a journal without rewriting saved versions.

    Existing V2 task tables are used unchanged. A new empty database may bootstrap
    the base schema, but a partially migrated existing database must be migrated
    separately before runtime activation.
    """

    def __init__(self, versions: PredictionVersionStore, *, lease_seconds: int = 120,
                 max_attempts: int = 3, heartbeat_seconds: float = 30,
                 final_min_budget_seconds: float = 180, publish_margin_seconds: float = 15,
                 run_id: str | None = None):
        if lease_seconds < 3 or not 1 <= max_attempts <= 10:
            raise ValueError("invalid_task_limits")
        if not 0 < heartbeat_seconds < lease_seconds or final_min_budget_seconds < 0 or publish_margin_seconds < 0:
            raise ValueError("invalid_runtime_limits")
        self.versions, self.clock = versions, versions.clock
        self.lease_seconds, self.max_attempts = lease_seconds, max_attempts
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.final_min_budget_seconds = float(final_min_budget_seconds)
        self.publish_margin_seconds = float(publish_margin_seconds)
        self.run_id = run_id or str(uuid.uuid4())
        self._lease_health: dict[str, threading.Event] = {}
        self._health_lock = threading.Lock()
        with closing(versions.connect()) as db:
            cols = {row[1] for row in db.execute("PRAGMA table_info(generation_tasks)")}
        if not cols:
            super().__init__(versions, lease_seconds=lease_seconds, max_attempts=max_attempts)
        elif not {"policy_version", "schedule_revision", "metadata_json", "logical_key"} <= cols:
            raise VersionStoreError("runtime_requires_scheduler_v2_migration")
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS scheduler_runtime_attempts (
                runtime_attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                task_id TEXT NOT NULL, game_id TEXT NOT NULL, policy_version TEXT NOT NULL,
                policy_identity TEXT, schedule_revision INTEGER NOT NULL,
                stage TEXT NOT NULL, trigger TEXT NOT NULL, request_id TEXT NOT NULL,
                attempt_count INTEGER NOT NULL, owner_token TEXT NOT NULL UNIQUE,
                scheduled_for TEXT NOT NULL, claimed_at TEXT NOT NULL, started_at TEXT,
                finished_at TEXT, published_at TEXT, deadline_at TEXT NOT NULL,
                effective_deadline_at TEXT NOT NULL, queue_delay_seconds REAL NOT NULL,
                remaining_budget_seconds REAL NOT NULL, total_latency_seconds REAL,
                phase TEXT NOT NULL DEFAULT 'pre_execution', phase_times_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'running', heartbeat_health TEXT NOT NULL DEFAULT 'healthy',
                last_heartbeat_at TEXT, failure_stage TEXT, error_code TEXT, published_version_id TEXT,
                FOREIGN KEY(task_id) REFERENCES generation_tasks(task_id),
                UNIQUE(task_id,attempt_count)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_runtime_attempts_task ON scheduler_runtime_attempts(task_id,attempt_count)")

    def _effective_deadline(self, task, registered=None):
        end = _instant(task["deadline_at"], "deadline_at")
        if registered and registered.get("tipoff"):
            end = min(end, _instant(registered["tipoff"], "scheduled_tipoff"))
        return end - timedelta(seconds=self.publish_margin_seconds)

    @contextmanager
    def transaction(self):
        db=self.versions.connect()
        try:
            db.execute("PRAGMA busy_timeout=1000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def _finish_row(self, db, row, at, *, status, error=None, failure_stage=None):
        elapsed = max(0, (at - _instant(row["claimed_at"], "claimed_at")).total_seconds())
        db.execute("""UPDATE scheduler_runtime_attempts SET status=?,finished_at=?,
            total_latency_seconds=?,failure_stage=?,error_code=? WHERE runtime_attempt_id=? AND status='running'""",
            (status, _utc(at), elapsed, failure_stage, safe_error_code(error), row["runtime_attempt_id"]))

    def _recover(self, db, at):
        # A crashed runtime worker may already have paid an external provider.
        # Never automatically repeat that ambiguous attempt after its lease dies.
        rows = db.execute("""SELECT a.* FROM generation_tasks t JOIN scheduler_runtime_attempts a
            ON a.task_id=t.task_id AND a.owner_token=t.owner_token
            WHERE t.status='running' AND t.lease_until<=? AND a.status='running'""", (_utc(at),)).fetchall()
        for row in rows:
            expired = at >= _instant(row["effective_deadline_at"], "effective_deadline_at")
            reason = "runtime_deadline_expired" if expired else "runtime_recovery_requires_review"
            stage = "deadline_expired" if expired else "lease_failure"
            if expired and row['stage']=='FINAL_PREGAME'and at>=_instant(row['deadline_at'],'deadline_at'):
                reason,stage='pregame_generation_closed','tipoff_crossed'
            db.execute("""UPDATE generation_tasks SET status=?,last_error=?,finished_at=?,owner_token=NULL,
                lease_until=NULL WHERE task_id=? AND status='running' AND owner_token=?""",
                ("expired" if expired else "cancelled", reason, _utc(at), row["task_id"], row["owner_token"]))
            self._finish_row(db, row, at, status="expired" if expired else "cancelled", error=reason, failure_stage=stage)
            db.execute("UPDATE scheduler_runtime_attempts SET heartbeat_health='lost' WHERE runtime_attempt_id=?",
                       (row["runtime_attempt_id"],))
        super()._recover(db, at)
        # Reconcile policy cancellation and legacy recoveries without changing ledger rows.
        for row in db.execute("""SELECT a.*,t.status AS task_status,t.last_error,t.published_version_id AS task_version_id,
                t.finished_at AS task_finished_at FROM scheduler_runtime_attempts a
                JOIN generation_tasks t ON t.task_id=a.task_id WHERE a.status='running' AND t.status!='running'""").fetchall():
            self._finish_row(db, row, at, status=row["task_status"], error=row["last_error"],
                             failure_stage=classify_failure(row["last_error"], row["phase"]) if row["last_error"] else None)
            if row["task_status"]=="succeeded" and row["task_version_id"]:
                version=db.execute("SELECT published_at FROM prediction_versions WHERE version_id=?",(row["task_version_id"],)).fetchone()
                db.execute("UPDATE scheduler_runtime_attempts SET published_version_id=?,published_at=?,finished_at=?,failure_stage=NULL,error_code=NULL WHERE runtime_attempt_id=?",
                    (row["task_version_id"],version[0],row["task_finished_at"] or _utc(at),row["runtime_attempt_id"]))

    def claim(self, task_id: str):
        at = _clock_instant(self.clock)
        with self.transaction() as db:
            self._recover(db, at)
            task = db.execute("SELECT * FROM generation_tasks WHERE task_id=?", (task_id,)).fetchone()
            if (task is None or task["status"] not in {"pending", "failed"}
                    or task["attempt_count"] >= min(self.max_attempts,task["max_attempts"])
                    or _instant(task["scheduled_for"], "scheduled_for") > at
                    or _instant(task["next_attempt_at"], "next_attempt_at") > at):
                return None
            state = db.execute("SELECT * FROM game_prediction_state WHERE game_id=?", (task["game_id"],)).fetchone()
            registered = json.loads(state["registered_game_json"] or "{}")
            policy = normalized_policy(json.loads(state["policy_config_json"]))
            metadata = json.loads(task["metadata_json"])
            if (task["policy_version"] != POLICY_VERSION or task["schedule_revision"] != state["schedule_revision"]
                    or registered.get("generation_hold_reason") or not policy["report_enabled"]
                    or metadata.get("policy", {}).get("policy_identity") != policy["policy_identity"]):
                reason="schedule_revision_changed" if task["schedule_revision"]!=state["schedule_revision"] else "policy_changed"
                db.execute("UPDATE generation_tasks SET status='cancelled',last_error=?,finished_at=? WHERE task_id=?",
                           (reason,_utc(at), task_id))
                return None
            tip = _instant(registered["tipoff"], "scheduled_tipoff")
            if state["locked_at"] is not None or registered.get("status") != "scheduled" or at >= tip:
                db.execute("UPDATE generation_tasks SET status='expired',last_error='pregame_locked',finished_at=? WHERE task_id=?",
                           (_utc(at), task_id))
                return None
            if task["stage"] != "FINAL_PREGAME" and at >= tip - LOW_STAGE_STOP:
                db.execute("UPDATE generation_tasks SET status='superseded',last_error='final_priority_window',finished_at=? WHERE task_id=?",
                           (_utc(at), task_id))
                return None
            prior = db.execute("SELECT payload_json FROM prediction_versions WHERE version_id=?", (state["current_version_id"],)).fetchone()
            if prior and STAGE_RANK.get(json.loads(prior[0]).get("stage"), 0) > STAGE_RANK[task["stage"]]:
                db.execute("UPDATE generation_tasks SET status='superseded',last_error='prediction_stage_regression',finished_at=? WHERE task_id=?",
                           (_utc(at), task_id))
                return None
            effective = self._effective_deadline(task, registered)
            budget = (effective - at).total_seconds()
            if budget <= 0 or (task["stage"] == "FINAL_PREGAME" and budget < self.final_min_budget_seconds):
                db.execute("UPDATE generation_tasks SET status='expired',last_error='runtime_budget_insufficient',finished_at=? WHERE task_id=?",
                           (_utc(at), task_id))
                return None
            if task["stage"] == "FINAL_PREGAME":
                db.execute("""UPDATE generation_tasks SET status='superseded',last_error='final_priority',finished_at=?,
                    owner_token=NULL,lease_until=NULL WHERE game_id=? AND status='running' AND stage!='FINAL_PREGAME'""",
                    (_utc(at), task["game_id"]))
                self._recover(db, at)
            if db.execute("SELECT 1 FROM generation_tasks WHERE game_id=? AND status='running'", (task["game_id"],)).fetchone():
                return None
            token, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
            until = min(at + timedelta(seconds=self.lease_seconds), effective)
            db.execute("""UPDATE generation_tasks SET status='running',owner_token=?,lease_until=?,started_at=?,
                finished_at=NULL,attempt_count=attempt_count+1 WHERE task_id=?""", (token, _utc(until), _utc(at), task_id))
            claimed = dict(db.execute("SELECT * FROM generation_tasks WHERE task_id=?", (task_id,)).fetchone())
            queue_delay = max(0, (at - _instant(task["scheduled_for"], "scheduled_for")).total_seconds())
            db.execute("""INSERT INTO scheduler_runtime_attempts
                (runtime_attempt_id,run_id,task_id,game_id,policy_version,policy_identity,schedule_revision,
                stage,trigger,request_id,attempt_count,owner_token,scheduled_for,claimed_at,deadline_at,
                effective_deadline_at,queue_delay_seconds,remaining_budget_seconds,last_heartbeat_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (attempt_id,self.run_id,task_id,task["game_id"],task["policy_version"],policy["policy_identity"],
                 task["schedule_revision"],task["stage"],task["trigger"],task["request_id"],claimed["attempt_count"],
                 token,task["scheduled_for"],_utc(at),task["deadline_at"],_utc(effective),queue_delay,budget,_utc(at)))
            claimed.update(runtime_attempt_id=attempt_id,effective_deadline_at=_utc(effective))
            return claimed

    def heartbeat(self, claim):
        at = _clock_instant(self.clock)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM scheduler_runtime_attempts WHERE owner_token=?", (claim["owner_token"],)).fetchone()
            if row is None or row["heartbeat_health"] != "healthy" or row["status"] != "running":
                return False
            effective = _instant(row["effective_deadline_at"], "effective_deadline_at")
            until = min(at + timedelta(seconds=self.lease_seconds), effective)
            changed = db.execute("""UPDATE generation_tasks SET lease_until=? WHERE task_id=? AND status='running'
                AND owner_token=? AND lease_until>? AND deadline_at>?""",
                (_utc(until),claim["task_id"],claim["owner_token"],_utc(at),_utc(at))).rowcount
            if not changed or at >= effective:
                return False
            db.execute("UPDATE scheduler_runtime_attempts SET last_heartbeat_at=? WHERE runtime_attempt_id=?",
                       (_utc(at),row["runtime_attempt_id"]))
            return True

    def _lose_lease(self, claim, reason):
        expired=_clock_instant(self.clock)>=_instant(claim['effective_deadline_at'],'effective_deadline_at')
        stage='deadline_expired'if expired else'lease_failure'
        if expired:reason='runtime_deadline_expired'
        if expired and claim['stage']=='FINAL_PREGAME'and _clock_instant(self.clock)>=_instant(claim['deadline_at'],'deadline_at'):
            reason,stage='pregame_generation_closed','tipoff_crossed'
        with self._health_lock:
            event = self._lease_health.setdefault(claim["owner_token"], threading.Event())
            event.set()
        try:
            with self.transaction() as db:
                db.execute("UPDATE scheduler_runtime_attempts SET heartbeat_health='lost',error_code=?,failure_stage=? WHERE owner_token=? AND status='running'",
                           (reason,stage,claim["owner_token"]))
            self.stop_task(claim["task_id"],reason,stage,owner_token=claim["owner_token"],status='expired'if expired else'cancelled')
        except sqlite3.Error:
            # Local guard stays lost; runner must treat failure to persist health as unsafe.
            pass

    @contextmanager
    def keep_alive(self, claim):
        stop = threading.Event()
        with self._health_lock:
            self._lease_health.setdefault(claim["owner_token"], threading.Event())
        def renew():
            while not stop.wait(self.heartbeat_seconds):
                try:
                    if not self.heartbeat(claim):
                        current = self.get_task(claim["task_id"])
                        if current and current["status"] == "succeeded" and current["attempt_count"] == claim["attempt_count"]:
                            return
                        self._lose_lease(claim,"runtime_heartbeat_lost")
                        return
                except sqlite3.Error as exc:
                    self._lose_lease(claim,"runtime_heartbeat_"+type(exc).__name__)
                    return
        thread = threading.Thread(target=renew,name=f"runtime-heartbeat-{claim['task_id']}",daemon=True)
        thread.start()
        try:
            yield
            with self._health_lock:
                if self._lease_health[claim["owner_token"]].is_set():
                    raise VersionStoreError("runtime_lease_lost")
        finally:
            stop.set()
            thread.join(timeout=16)
            with self._health_lock:
                self._lease_health.pop(claim["owner_token"],None)

    def assert_claim(self, claim):
        with self._health_lock:
            event = self._lease_health.get(claim["owner_token"])
            if event is not None and event.is_set():
                raise VersionStoreError("runtime_lease_lost")
        with self.transaction() as db:
            at = _clock_instant(self.clock)
            row = db.execute("SELECT * FROM generation_tasks WHERE task_id=?", (claim["task_id"],)).fetchone()
            attempt = db.execute("SELECT * FROM scheduler_runtime_attempts WHERE owner_token=?", (claim["owner_token"],)).fetchone()
            if (row is None or row["status"]!='running' or row["owner_token"]!=claim["owner_token"]
                    or _instant(row["lease_until"],"lease_until")<=at or attempt is None
                    or attempt["heartbeat_health"]!='healthy' or attempt["status"]!='running'):
                raise VersionStoreError("generation_claim_lost")
            if at >= _instant(attempt["effective_deadline_at"],"effective_deadline_at"):
                raise VersionStoreError("runtime_deadline_expired")
            state = db.execute("SELECT * FROM game_prediction_state WHERE game_id=?", (row["game_id"],)).fetchone()
            policy = normalized_policy(json.loads(state["policy_config_json"]))
            registered = json.loads(state["registered_game_json"] or "{}")
            if state["schedule_revision"]!=claim["schedule_revision"]:
                raise VersionStoreError("schedule_revision_changed")
            if (row["policy_version"]!=POLICY_VERSION
                    or policy["policy_identity"]!=attempt["policy_identity"] or not policy["report_enabled"]
                    or registered.get("generation_hold_reason")):
                raise VersionStoreError("generation_policy_revision_mismatch")
            if state["locked_at"] or registered.get("status")!='scheduled' or at>=_instant(registered["tipoff"],"scheduled_tipoff"):
                raise VersionStoreError("pregame_generation_closed")

    def record_phase(self, claim, phase):
        if phase not in PHASES:
            raise ValueError("runtime_phase_invalid")
        self.assert_claim(claim)
        at = _clock_instant(self.clock)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM scheduler_runtime_attempts WHERE owner_token=?", (claim["owner_token"],)).fetchone()
            phases = json.loads(row["phase_times_json"])
            phases.setdefault(phase,_utc(at))
            db.execute("UPDATE scheduler_runtime_attempts SET phase=?,phase_times_json=?,started_at=COALESCE(started_at,?) WHERE owner_token=? AND status='running'",
                       (phase,_json(phases),_utc(at),claim["owner_token"]))

    def fail(self, claim, error: str):
        at = _clock_instant(self.clock)
        retry_at = at + timedelta(seconds=min(120,30*2**(claim["attempt_count"]-1)))
        effective = _instant(claim.get("effective_deadline_at") or _utc(self._effective_deadline(claim)),"effective_deadline_at")
        insufficient = (retry_at>=effective or
                        (claim["stage"]=="FINAL_PREGAME" and (effective-retry_at).total_seconds()<self.final_min_budget_seconds))
        status = "expired" if insufficient else "failed"
        with self.transaction() as db:
            row = db.execute("SELECT * FROM scheduler_runtime_attempts WHERE owner_token=?", (claim["owner_token"],)).fetchone()
            changed = db.execute("""UPDATE generation_tasks SET status=?,last_error=?,finished_at=?,next_attempt_at=?,
                owner_token=NULL,lease_until=NULL WHERE task_id=? AND status='running' AND owner_token=?""",
                (status,"runtime_retry_budget_insufficient" if insufficient else safe_error_code(error),_utc(at),_utc(retry_at),claim["task_id"],claim["owner_token"])).rowcount
            if changed and row:
                self._finish_row(db,row,at,status=status,error=error,failure_stage=classify_failure(error,row["phase"]))
        return bool(changed)

    def finish_attempt(self, claim, result=None, error=None, failure_stage=None):
        """Journal completion; a result can never manufacture a successful ledger task."""
        at = _clock_instant(self.clock)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM scheduler_runtime_attempts WHERE owner_token=?", (claim["owner_token"],)).fetchone()
            task = db.execute("SELECT * FROM generation_tasks WHERE task_id=?", (claim["task_id"],)).fetchone()
            if row is None or task is None:
                return False
            if task["attempt_count"] != row["attempt_count"]:
                return False
            if task["status"]=="succeeded" and task["published_version_id"]:
                version = db.execute("SELECT published_at FROM prediction_versions WHERE version_id=?",(task["published_version_id"],)).fetchone()
                db.execute("""UPDATE scheduler_runtime_attempts SET status='succeeded',finished_at=?,published_at=?,
                    published_version_id=?,total_latency_seconds=?,error_code=NULL,failure_stage=NULL WHERE owner_token=?""",
                    (_utc(at),version[0],task["published_version_id"],max(0,(at-_instant(row["claimed_at"],"claimed_at")).total_seconds()),claim["owner_token"]))
            else:
                if error is not None and task["status"]=="running" and task["owner_token"]==claim["owner_token"]:
                    # A BaseException may escape the coordinator's Exception
                    # handler. It is ambiguous external work, not a safe retry.
                    db.execute("UPDATE generation_tasks SET status='cancelled',last_error='runtime_recovery_requires_review',finished_at=?,owner_token=NULL,lease_until=NULL WHERE task_id=? AND owner_token=?",
                               (_utc(at),claim["task_id"],claim["owner_token"]))
                    task=db.execute("SELECT * FROM generation_tasks WHERE task_id=?",(claim["task_id"],)).fetchone()
                status = task["status"] if task["status"] in TERMINAL else ("failed" if error else row["status"])
                reason = error or task["last_error"]
                self._finish_row(db,row,at,status=status,error=reason,
                                 failure_stage=failure_stage or (classify_failure(reason,row["phase"]) if reason else None))
                if (failure_stage or error is not None) and row["status"] in TERMINAL and row["status"]!='succeeded':
                    db.execute("UPDATE scheduler_runtime_attempts SET failure_stage=?,error_code=? WHERE owner_token=?",
                        (failure_stage or classify_failure(error,row["phase"]),safe_error_code(error) or row["error_code"],claim["owner_token"]))
        return True

    def stop_task(self, task_id, reason, failure_stage, owner_token=None, status="expired"):
        if status not in {"expired","cancelled","superseded","failed"}:
            raise ValueError("runtime_stop_status_invalid")
        at = _clock_instant(self.clock)
        with self.transaction() as db:
            task = db.execute("SELECT * FROM generation_tasks WHERE task_id=?",(task_id,)).fetchone()
            if task is None or task["status"] not in {"pending","failed","running"}:
                return False
            if task["status"]=="running":
                if owner_token is None or task["owner_token"]!=owner_token:
                    return False
            elif owner_token is not None:
                return False
            changed = db.execute("""UPDATE generation_tasks SET status=?,last_error=?,finished_at=?,owner_token=NULL,
                lease_until=NULL WHERE task_id=? AND status=? AND owner_token IS ?""",
                (status,str(reason)[:250],_utc(at),task_id,task["status"],task["owner_token"])).rowcount
            if changed and owner_token:
                row = db.execute("SELECT * FROM scheduler_runtime_attempts WHERE owner_token=?",(owner_token,)).fetchone()
                if row:
                    self._finish_row(db,row,at,status=status,error=reason,failure_stage=failure_stage)
        return bool(changed)

    def attempts_snapshot(self, *, limit=100, task_id=None):
        with closing(self.versions.connect()) as db:
            rows = db.execute("SELECT * FROM scheduler_runtime_attempts" + (" WHERE task_id=?" if task_id else "") +
                              " ORDER BY claimed_at DESC,attempt_count DESC LIMIT ?",
                              (task_id,int(limit)) if task_id else (int(limit),)).fetchall()
        return [{key:row[key] for key in row.keys() if key!="owner_token"} for row in rows]

    def list_due_tasks(self, *, limit=20):
        at=_clock_instant(self.clock)
        with self.transaction() as db:
            self._recover(db,at)
            rows=db.execute("""SELECT * FROM generation_tasks WHERE status IN ('pending','failed')
                AND policy_version=? AND attempt_count<max_attempts AND attempt_count<?
                AND scheduled_for<=? AND next_attempt_at<=?
                ORDER BY CASE stage WHEN 'FINAL_PREGAME' THEN 0 WHEN 'UPDATED_PREVIEW' THEN 1 ELSE 2 END,
                    deadline_at,scheduled_for LIMIT ?""",
                (POLICY_VERSION,self.max_attempts,_utc(at),_utc(at),int(limit))).fetchall()
        return [dict(row) for row in rows]
