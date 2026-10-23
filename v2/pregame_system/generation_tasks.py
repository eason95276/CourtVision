"""Durable single-game generation claims in the prediction-version database."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import timedelta
from typing import Any

from .official_final import FINAL_PREGAME_LEAD
from .prediction_versions import (
    PredictionVersionStore, VersionStoreError, _clock_instant, _instant, _json, _utc, game_identity,
)

STAGE_LEADS = {
    "EARLY_PREVIEW": timedelta(days=3),
    "UPDATED_PREVIEW": timedelta(hours=24),
    "FINAL_PREGAME": FINAL_PREGAME_LEAD,
}
STAGE_RANK = {"ON_DEMAND": 0, "EARLY_PREVIEW": 1, "UPDATED_PREVIEW": 2, "FINAL_PREGAME": 3}
GAME_TYPES = {"preseason", "regular_season", "playoffs"}


def stage_windows(tipoff: str) -> dict[str, tuple[str, str]]:
    tip = _instant(tipoff, "scheduled_tipoff")
    starts = {stage: tip - lead for stage, lead in STAGE_LEADS.items()}
    return {
        "EARLY_PREVIEW": (_utc(starts["EARLY_PREVIEW"]), _utc(starts["UPDATED_PREVIEW"])),
        "UPDATED_PREVIEW": (_utc(starts["UPDATED_PREVIEW"]), _utc(starts["FINAL_PREGAME"])),
        "FINAL_PREGAME": (_utc(starts["FINAL_PREGAME"]), _utc(tip)),
    }


def normalize_registration(game: dict[str, Any], *, require_game_type: bool = True) -> dict[str, Any]:
    identity = game_identity(game)
    from courtvision.data import TEAM_ID_TO_ABBR

    if any(identity[key] not in TEAM_ID_TO_ABBR for key in ("home_team_id", "away_team_id")):
        raise VersionStoreError("unknown_nba_team")
    kind = game.get("game_type")
    if kind is None and not require_game_type:
        kind = "regular_season"  # Existing saved frontend games predate the registry field.
    if kind not in GAME_TYPES:
        raise VersionStoreError("game_type_invalid")
    if game.get("status", "scheduled") != "scheduled":
        raise VersionStoreError("pregame_generation_closed")
    normalized = json.loads(_json(game))
    normalized.update(id=identity["game_id"], tipoff=identity["scheduled_tipoff"],
                      status="scheduled", game_type=kind)
    for side in ("home", "away"):
        top_id, side_id = game.get(f"{side}_team_id"), (game.get(side) or {}).get("id")
        if top_id is not None and side_id is not None and int(top_id) != int(side_id):
            raise VersionStoreError("team_identity_alias_mismatch")
        normalized[side] = {**(game.get(side) or {}), "id": identity[f"{side}_team_id"]}
    return normalized


def assert_publication_claim(db: sqlite3.Connection, claim: dict[str, Any], payload: dict[str, Any], at) -> None:
    row = db.execute("SELECT * FROM generation_tasks WHERE task_id=?", (claim["task_id"],)).fetchone()
    if (row is None or row["status"] != "running" or row["owner_token"] != claim["owner_token"]
            or _instant(row["lease_until"], "lease_until") <= at):
        raise VersionStoreError("generation_claim_lost")
    from .scheduler_policy import POLICY_VERSION, normalized_policy
    state = db.execute("SELECT * FROM game_prediction_state WHERE game_id=?", (row["game_id"],)).fetchone()
    policy = normalized_policy(json.loads(state["policy_config_json"]))
    if (row["policy_version"] != POLICY_VERSION or row["schedule_revision"] != state["schedule_revision"]
            or claim.get("schedule_revision") != row["schedule_revision"] or claim.get("policy_version") != row["policy_version"]
            or payload.get("schedule_revision") != row["schedule_revision"] or payload.get("policy_version") != row["policy_version"]
            or json.loads(row["metadata_json"]).get("policy", {}).get("policy_identity") != policy["policy_identity"]
            or not policy["report_enabled"]):
        raise VersionStoreError("generation_policy_revision_mismatch")
    if at >= _instant(row["deadline_at"], "deadline_at"):
        raise VersionStoreError("generation_stage_expired")
    if (payload.get("task_id") != row["task_id"] or payload.get("game_id") != row["game_id"]
            or payload.get("stage") != row["stage"] or payload.get("request_id") != row["request_id"]
            or payload.get("scheduled_for") != row["scheduled_for"]
            or (payload.get("trigger") or {}).get("type") != row["trigger"]):
        raise VersionStoreError("generation_task_payload_mismatch")
    if row["trigger"] == "scheduled" and at < _instant(row["scheduled_for"], "scheduled_for"):
        raise VersionStoreError("generation_not_due")


class GenerationTaskStore:
    def __init__(self, versions: PredictionVersionStore, *, lease_seconds: int = 120, max_attempts: int = 3):
        if lease_seconds < 3 or not 1 <= max_attempts <= 10:
            raise ValueError("invalid_task_limits")
        self.versions = versions
        self.clock = versions.clock
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        with self.transaction() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(game_prediction_state)")}
            for name in ("registered_game_json", "registered_at"):
                if name not in columns:
                    db.execute(f"ALTER TABLE game_prediction_state ADD COLUMN {name} TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS generation_tasks (
                task_id TEXT PRIMARY KEY, game_id TEXT NOT NULL, stage TEXT NOT NULL
                    CHECK(stage IN ('ON_DEMAND','EARLY_PREVIEW','UPDATED_PREVIEW','FINAL_PREGAME')),
                trigger TEXT NOT NULL CHECK(trigger IN ('scheduled','manual_refresh','evidence_update')),
                request_id TEXT NOT NULL UNIQUE, scheduled_for TEXT NOT NULL, deadline_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('pending','running','succeeded','failed','missed','expired','superseded','cancelled')),
                created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
                next_attempt_at TEXT NOT NULL, last_error TEXT, owner_token TEXT, lease_until TEXT,
                published_version_id TEXT,
                FOREIGN KEY(game_id) REFERENCES game_prediction_state(game_id),
                FOREIGN KEY(game_id,published_version_id) REFERENCES prediction_versions(game_id,version_id)
            )""")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_running_game ON generation_tasks(game_id) WHERE status='running'")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due ON generation_tasks(status,next_attempt_at,scheduled_for)")
            from .scheduler_policy import migrate_task_policy
            migrate_task_policy(db, _clock_instant(self.clock))

    @contextmanager
    def transaction(self):
        db = self.versions.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def register_game(self, game: dict[str, Any], *, require_game_type: bool = True) -> dict[str, Any]:
        normalized = normalize_registration(game, require_game_type=require_game_type)
        identity = game_identity(normalized)
        at = _clock_instant(self.clock)
        if at >= _instant(normalized["tipoff"], "scheduled_tipoff"):
            raise VersionStoreError("pregame_generation_closed")
        with self.transaction() as db:
            state = self.versions._ensure_state(db, identity)
            if game.get("schedule_revision", 1) != state["schedule_revision"]:
                raise VersionStoreError("schedule_revision_changed")
            if state["registered_game_json"]:
                previous = json.loads(state["registered_game_json"])
                if any(key in game and previous.get(key) is not None and game[key] != previous[key] for key in ("venue", "neutral_site")):
                    raise VersionStoreError("schedule_changed")
                normalized = {**previous, **normalized}
                for side in ('home','away'):
                    old_id=(previous.get(side)or{}).get('espn_team_id')
                    new_id=(normalized.get(side)or{}).get('espn_team_id')
                    if old_id is not None and new_id is not None and str(old_id)!=str(new_id):
                        raise VersionStoreError('registered_provider_identity_changed:'+side)
                    normalized[side]={**(previous.get(side)or{}),**normalized[side]}
                prior_type = previous["game_type"]
                if previous.get("generation_hold_reason"):
                    normalized["generation_hold_reason"] = previous["generation_hold_reason"]
                if not require_game_type and game.get("game_type") is None:
                    normalized["game_type"] = prior_type
                elif normalized["game_type"] != prior_type:
                    raise VersionStoreError("registered_game_type_changed")
            normalized["schedule_revision"] = state["schedule_revision"]
            from .scheduler_policy import normalized_policy
            normalized["policy_config"] = normalized_policy(json.loads(state["policy_config_json"]))
            db.execute("UPDATE game_prediction_state SET registered_game_json=?,registered_at=COALESCE(registered_at,?) WHERE game_id=?",
                       (_json(normalized), _utc(at), normalized["id"]))
            db.execute("INSERT OR IGNORE INTO game_schedule_revisions VALUES(?,?,?,?,?,?)",
                (normalized["id"], state["schedule_revision"], _json(normalized), _utc(at), "registration", "{}"))
            if normalized.get("generation_hold_reason"):
                db.execute("""UPDATE generation_tasks SET status='cancelled',last_error=?,finished_at=?,
                    owner_token=NULL,lease_until=NULL WHERE game_id=? AND trigger='scheduled'
                    AND status IN ('pending','running','failed','missed')""",
                    (normalized["generation_hold_reason"], _utc(at), normalized["id"]))
            rows = db.execute("SELECT * FROM generation_tasks WHERE game_id=? AND trigger='scheduled' ORDER BY scheduled_for",
                              (normalized["id"],)).fetchall()
        return {"game": normalized, "tasks": [dict(row) for row in rows]}

    def ensure_schedule(self, game):
        return self.register_game(game, require_game_type=False)

    def _insert(self, db, game_id, stage, trigger, due, deadline, at, *, status="pending", request_id=None,
                logical_key=None, metadata=None, created_reason=None):
        from .scheduler_policy import POLICY_VERSION
        task_id = str(uuid.uuid4())
        revision = db.execute("SELECT schedule_revision FROM game_prediction_state WHERE game_id=?", (game_id,)).fetchone()[0]
        db.execute("""INSERT INTO generation_tasks
            (task_id,game_id,stage,trigger,request_id,scheduled_for,deadline_at,status,created_at,
             finished_at,max_attempts,next_attempt_at,policy_version,schedule_revision,logical_key,metadata_json,created_reason)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, game_id, stage, trigger, request_id or task_id, due, deadline, status, _utc(at),
             _utc(at) if status == "missed" else None, self.max_attempts, due, POLICY_VERSION, revision,
             logical_key or task_id, _json(metadata or {}), created_reason))
        return dict(db.execute("SELECT * FROM generation_tasks WHERE task_id=?", (task_id,)).fetchone())

    def _recover(self, db, at):
        timestamp = _utc(at)
        db.execute("""UPDATE generation_tasks SET status='failed',last_error='lease_expired',
            owner_token=NULL,lease_until=NULL,finished_at=?,next_attempt_at=?
            WHERE status='running' AND lease_until<=?""", (timestamp, timestamp, timestamp))
        db.execute("""UPDATE generation_tasks SET status=CASE WHEN status='pending' THEN 'missed' ELSE 'expired' END,
            last_error='stage_window_closed',finished_at=?,owner_token=NULL,lease_until=NULL
            WHERE status IN ('pending','failed','running') AND deadline_at<=?""", (timestamp, timestamp))

    def request_manual(self, game: dict[str, Any], *, request_id: str | None = None):
        self.ensure_schedule(game)
        registered = self.registered_game(game_identity(game)["game_id"])
        if registered.get("generation_hold_reason"):
            raise VersionStoreError(registered["generation_hold_reason"])
        from .scheduler_policy import SchedulerPolicyPlanner, FINAL_LEAD
        if request_id:
            with closing(self.versions.connect()) as db:
                existing = db.execute("SELECT * FROM generation_tasks WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["game_id"] != registered["id"]:
                    raise VersionStoreError("request_id_wrong_game")
                return dict(existing)
        plans = SchedulerPolicyPlanner(self).plan_game(registered)
        at = _clock_instant(self.clock)
        stage = "FINAL_PREGAME" if at >= _instant(registered["tipoff"], "scheduled_tipoff") - FINAL_LEAD else "EARLY_PREVIEW"
        task = next((task for task in plans if task["stage"] == stage), None)
        if task is None:
            with closing(self.versions.connect()) as db:
                row = db.execute("SELECT * FROM generation_tasks WHERE game_id=? AND policy_version='scheduler-v2' AND schedule_revision=? AND stage=? ORDER BY created_at DESC LIMIT 1",
                    (registered["id"], registered["schedule_revision"], stage)).fetchone()
            if row is not None:
                return dict(row)
            raise VersionStoreError("report_window_not_open")
        return task

    def claim(self, task_id: str):
        at = _clock_instant(self.clock)
        with self.transaction() as db:
            self._recover(db, at)
            task = db.execute("SELECT * FROM generation_tasks WHERE task_id=?", (task_id,)).fetchone()
            if (task is None or task["status"] not in {"pending", "failed"}
                    or task["attempt_count"] >= task["max_attempts"]
                    or _instant(task["scheduled_for"], "scheduled_for") > at
                    or _instant(task["next_attempt_at"], "next_attempt_at") > at):
                return None
            state = db.execute("SELECT * FROM game_prediction_state WHERE game_id=?", (task["game_id"],)).fetchone()
            from .scheduler_policy import POLICY_VERSION, LOW_STAGE_STOP, normalized_policy
            registered = json.loads(state["registered_game_json"] or "{}")
            policy = normalized_policy(json.loads(state["policy_config_json"]))
            metadata = json.loads(task["metadata_json"])
            if (task["policy_version"] != POLICY_VERSION or task["schedule_revision"] != state["schedule_revision"]
                    or registered.get("generation_hold_reason") or not policy["report_enabled"]
                    or metadata.get("policy", {}).get("policy_identity") != policy["policy_identity"]):
                db.execute("UPDATE generation_tasks SET status='cancelled',last_error='policy_or_revision_changed',finished_at=? WHERE task_id=?", (_utc(at), task_id))
                return None
            if task["stage"] != "FINAL_PREGAME" and at >= _instant(registered["tipoff"], "scheduled_tipoff") - LOW_STAGE_STOP:
                db.execute("UPDATE generation_tasks SET status='superseded',last_error='final_priority_window',finished_at=? WHERE task_id=?", (_utc(at), task_id))
                return None
            if state["locked_at"] is not None:
                db.execute("UPDATE generation_tasks SET status='expired',finished_at=?,last_error='pregame_locked' WHERE task_id=?", (_utc(at), task_id))
                return None
            prior = db.execute("SELECT payload_json FROM prediction_versions WHERE version_id=?", (state["current_version_id"],)).fetchone()
            if prior and STAGE_RANK.get(json.loads(prior[0]).get("stage"), 0) > STAGE_RANK[task["stage"]]:
                db.execute("UPDATE generation_tasks SET status='superseded',finished_at=? WHERE task_id=?", (_utc(at), task_id))
                return None
            if task["stage"] == "FINAL_PREGAME":
                db.execute("""UPDATE generation_tasks SET status='superseded',last_error='final_priority',finished_at=?,
                    owner_token=NULL,lease_until=NULL WHERE game_id=? AND status='running' AND stage!='FINAL_PREGAME'""", (_utc(at), task["game_id"]))
            if db.execute("SELECT 1 FROM generation_tasks WHERE game_id=? AND status='running'", (task["game_id"],)).fetchone():
                return None
            token = str(uuid.uuid4())
            until = min(at + timedelta(seconds=self.lease_seconds), _instant(task["deadline_at"], "deadline_at"))
            db.execute("""UPDATE generation_tasks SET status='running',owner_token=?,lease_until=?,
                started_at=?,finished_at=NULL,attempt_count=attempt_count+1 WHERE task_id=?""",
                (token, _utc(until), _utc(at), task_id))
            return dict(db.execute("SELECT * FROM generation_tasks WHERE task_id=?", (task_id,)).fetchone())

    def heartbeat(self, claim):
        at = _clock_instant(self.clock)
        until = min(at + timedelta(seconds=self.lease_seconds), _instant(claim["deadline_at"], "deadline_at"))
        with self.transaction() as db:
            return db.execute("""UPDATE generation_tasks SET lease_until=? WHERE task_id=?
                AND status='running' AND owner_token=? AND lease_until>? AND deadline_at>?""",
                (_utc(until), claim["task_id"], claim["owner_token"], _utc(at), _utc(at))).rowcount == 1

    @contextmanager
    def keep_alive(self, claim):
        stop = threading.Event()
        def renew():
            while not stop.wait(min(30, self.lease_seconds / 3)):
                try:
                    if not self.heartbeat(claim):
                        return
                except sqlite3.Error:
                    # Publication rechecks ownership even if renewal fails.
                    return
        thread = threading.Thread(target=renew, name=f"generation-{claim['task_id']}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=16)

    def assert_claim(self, claim):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM generation_tasks WHERE task_id=?", (claim["task_id"],)).fetchone()
            at = _clock_instant(self.clock)
            if (row is None or row["status"] != "running" or row["owner_token"] != claim["owner_token"]
                    or _instant(row["lease_until"], "lease_until") <= at):
                raise VersionStoreError("generation_claim_lost")
            if at >= _instant(row["deadline_at"], "deadline_at"):
                raise VersionStoreError("generation_stage_expired")

    def fail(self, claim, error: str):
        at = _clock_instant(self.clock)
        retry_at = at + timedelta(seconds=min(120, 30 * 2 ** (claim["attempt_count"] - 1)))
        expired = retry_at >= _instant(claim["deadline_at"], "deadline_at")
        with self.transaction() as db:
            db.execute("""UPDATE generation_tasks SET status=?,last_error=?,finished_at=?,next_attempt_at=?,
                owner_token=NULL,lease_until=NULL WHERE task_id=? AND status='running' AND owner_token=?""",
                ("expired" if expired else "failed", error[:250], _utc(at), _utc(retry_at), claim["task_id"], claim["owner_token"]))

    def supersede(self, claim, reason):
        with self.transaction() as db:
            db.execute("""UPDATE generation_tasks SET status='superseded',last_error=?,finished_at=?,
                owner_token=NULL,lease_until=NULL WHERE task_id=? AND status='running' AND owner_token=?""",
                (reason, _utc(_clock_instant(self.clock)), claim["task_id"], claim["owner_token"]))

    def cancel_game(self, game):
        identity = game_identity(game)
        with self.transaction() as db:
            self.versions._ensure_state(db, identity)
            db.execute("""UPDATE generation_tasks SET status='cancelled',last_error='game_not_scheduled',
                finished_at=?,owner_token=NULL,lease_until=NULL
                WHERE game_id=? AND status IN ('pending','running','failed')""",
                (_utc(_clock_instant(self.clock)), identity["game_id"]))

    def list_due_tasks(self, *, limit=20):
        at = _clock_instant(self.clock)
        with self.transaction() as db:
            self._recover(db, at)
            rows = db.execute("""SELECT * FROM generation_tasks WHERE status IN ('pending','failed')
                AND policy_version='scheduler-v2' AND attempt_count<max_attempts AND scheduled_for<=? AND next_attempt_at<=?
                ORDER BY CASE stage WHEN 'FINAL_PREGAME' THEN 0 WHEN 'UPDATED_PREVIEW' THEN 1 ELSE 2 END,
                deadline_at,scheduled_for LIMIT ?""", (_utc(at), _utc(at), int(limit))).fetchall()
        return [dict(row) for row in rows]

    def get_task(self, task_id):
        with closing(self.versions.connect()) as db:
            row = db.execute("SELECT * FROM generation_tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def registered_games(self):
        with closing(self.versions.connect()) as db:
            rows = db.execute("SELECT registered_game_json FROM game_prediction_state WHERE registered_game_json IS NOT NULL").fetchall()
        return [json.loads(row[0]) for row in rows]

    def registered_game(self, game_id):
        with closing(self.versions.connect()) as db:
            row = db.execute("SELECT registered_game_json FROM game_prediction_state WHERE game_id=?", (game_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def current_task(self, game_id):
        with closing(self.versions.connect()) as db:
            row = db.execute("""SELECT * FROM generation_tasks WHERE game_id=?
                ORDER BY CASE WHEN status='running' THEN 0 WHEN started_at IS NOT NULL THEN 1
                    WHEN status='pending' THEN 2 ELSE 3 END,started_at DESC,scheduled_for ASC LIMIT 1""", (game_id,)).fetchone()
        return self.summary(dict(row)) if row else None

    @staticmethod
    def summary(task):
        summary = {key: task.get(key) for key in (
            "task_id", "game_id", "stage", "trigger", "scheduled_for", "status", "created_at",
            "started_at", "finished_at", "attempt_count", "last_error", "published_version_id",
            "policy_version", "schedule_revision", "logical_key", "created_reason",
        )}
        metadata = json.loads(task.get("metadata_json") or "{}")
        summary["display_name"] = {"EARLY_PREVIEW": "特别前瞻" if metadata.get("preview_kind") == "special_preheat" else "赛前预览",
            "UPDATED_PREVIEW": "重要更新", "FINAL_PREGAME": "最终赛前分析", "ON_DEMAND": "历史按需任务"}.get(task.get("stage"))
        return summary
