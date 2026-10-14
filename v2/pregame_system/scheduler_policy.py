"""Policy V2 planning only. No collection, model calls, or execution."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from .prediction_versions import VersionStoreError, game_identity, _clock_instant, _instant, _json, _utc
from .storage import content_hash

POLICY_VERSION = "scheduler-v2"
CHINA = ZoneInfo("Asia/Shanghai")
FINAL_LEAD = timedelta(minutes=5)
LOW_STAGE_STOP = timedelta(minutes=15)
MATERIAL_TYPES = {"availability_change", "questionable_to_out", "out_to_available", "unknown_to_out",
                  "starters_change", "minutes_restriction", "personnel_change", "rotation_change", "schedule_change"}


def normalized_policy(value=None):
    value = value or {}
    result = {"policy_version": POLICY_VERSION, "report_enabled": value.get("report_enabled", True),
        "preheat_enabled": value.get("preheat_enabled", False), "preheat_reason": value.get("preheat_reason"),
        "preheat_lead_hours": value.get("preheat_lead_hours", 72), "preheat_start": value.get("preheat_start")}
    if not isinstance(result["report_enabled"], bool) or not isinstance(result["preheat_enabled"], bool):
        raise VersionStoreError("policy_flags_invalid")
    if result["preheat_enabled"]:
        hours = result["preheat_lead_hours"]
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 48 <= hours <= 72 or not result["preheat_reason"]:
            raise VersionStoreError("explicit_preheat_reason_and_48_72_hours_required")
        if result["preheat_start"]:
            _instant(result["preheat_start"], "preheat_start")
    if value.get('lifecycle')=='early-final-v1':
        requested=value.get('early_requested_at')
        _instant(requested,'early_requested_at')
        result.update(lifecycle='early-final-v1',early_requested_at=requested,
            prefer_off_peak=value.get('prefer_off_peak',True),price_holidays=value.get('price_holidays',[]))
        if not isinstance(result['prefer_off_peak'],bool):raise VersionStoreError('off_peak_flag_invalid')
    result["policy_identity"] = POLICY_VERSION + ":" + content_hash(result)[:16]
    return result


def window_start(game, policy):
    tip = _instant(game.get("tipoff") or game["scheduled_tipoff"], "scheduled_tipoff")
    normal = datetime.combine(tip.astimezone(CHINA).date() - timedelta(days=1), time(), CHINA)
    if policy.get('lifecycle')=='early-final-v1':
        start=_instant(policy['early_requested_at'],'early_requested_at')
        if start>=tip-timedelta(minutes=20):raise VersionStoreError('early_start_must_precede_final_window')
        return start,normal,'two_stage_window'
    if policy["preheat_enabled"]:
        start = (_instant(policy["preheat_start"], "preheat_start") if policy["preheat_start"]
                 else tip - timedelta(hours=policy["preheat_lead_hours"]))
        if not tip - timedelta(hours=72) <= start <= tip - timedelta(hours=48):
            raise VersionStoreError("preheat_start_outside_48_72_hours")
        return start, normal, "special_preheat"
    return normal, normal, "normal_window"


def product_category(game, at):
    tip = _instant(game.get("tipoff") or game["scheduled_tipoff"], "scheduled_tipoff")
    day, today = tip.astimezone(CHINA).date(), at.astimezone(CHINA).date()
    status = game.get("status", "scheduled")
    completed = status in {"completed", "final", "post", "finished"}
    if completed:
        category = "completed" if day == today else "history"
    elif status in {"live", "in_progress", "in"} or at >= tip:
        category = "live"  # Never infer completion from elapsed hours.
    elif day == today:
        category = "today"
    elif day == today + timedelta(days=1):
        category = "tomorrow"
    else:
        policy = normalized_policy(game.get("policy_config"))
        start, _, _ = window_start(game, policy)
        category = "special_preheat" if policy["preheat_enabled"] and at >= start else "future"
    return {"product_day": day.isoformat(), "product_category": category,
            "homepage_visible": category in {"today", "tomorrow", "special_preheat", "live", "completed"}}


def migrate_task_policy(db, at):
    """Additive, audited migration. Never update prediction_versions payloads."""
    state_cols = {r[1] for r in db.execute("PRAGMA table_info(game_prediction_state)")}
    for name, definition in {"schedule_revision": "INTEGER NOT NULL DEFAULT 1",
                             "policy_config_json": "TEXT NOT NULL DEFAULT '{}'"}.items():
        if name not in state_cols:
            db.execute(f"ALTER TABLE game_prediction_state ADD COLUMN {name} {definition}")
    cols = {r[1] for r in db.execute("PRAGMA table_info(generation_tasks)")}
    for name, definition in {"policy_version": "TEXT NOT NULL DEFAULT 'legacy-v1'",
        "schedule_revision": "INTEGER NOT NULL DEFAULT 1", "logical_key": "TEXT",
        "created_reason": "TEXT", "metadata_json": "TEXT NOT NULL DEFAULT '{}'"}.items():
        if name not in cols:
            db.execute(f"ALTER TABLE generation_tasks ADD COLUMN {name} {definition}")
    db.execute("UPDATE generation_tasks SET logical_key=task_id WHERE logical_key IS NULL")
    db.execute("DROP INDEX IF EXISTS idx_tasks_scheduled_stage")
    db.execute("DROP INDEX IF EXISTS idx_tasks_active_stage")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_policy_identity ON generation_tasks
        (game_id,schedule_revision,policy_version,logical_key)""")
    db.execute("""CREATE TABLE IF NOT EXISTS scheduler_migrations (
        migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, receipt_json TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS game_schedule_revisions (
        game_id TEXT NOT NULL, schedule_revision INTEGER NOT NULL, schedule_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL,
        PRIMARY KEY(game_id,schedule_revision), FOREIGN KEY(game_id) REFERENCES game_prediction_state(game_id))""")
    count = db.execute("SELECT COUNT(*) FROM generation_tasks WHERE policy_version='legacy-v1' AND status IN ('pending','failed','running')").fetchone()[0]
    db.execute("""UPDATE generation_tasks SET status='cancelled',last_error='policy_replaced',finished_at=?,
        owner_token=NULL,lease_until=NULL WHERE policy_version='legacy-v1' AND status IN ('pending','failed','running')""", (_utc(at),))
    legacy_versions = {row["version_id"]: 1 for row in db.execute("SELECT version_id,payload_json FROM prediction_versions")
                       if "schedule_revision" not in json.loads(row["payload_json"])}
    db.execute("INSERT OR IGNORE INTO scheduler_migrations VALUES(?,?,?)",
        (POLICY_VERSION, _utc(at), _json({"invalidated_legacy_tasks": count, "legacy_version_revision_refs": legacy_versions})))
    for row in db.execute("SELECT * FROM game_prediction_state WHERE registered_game_json IS NOT NULL").fetchall():
        db.execute("INSERT OR IGNORE INTO game_schedule_revisions VALUES(?,?,?,?,?,?)",
            (row["game_id"], row["schedule_revision"], row["registered_game_json"], _utc(at), "existing_registration_at_migration", "{}"))
        game = json.loads(row["registered_game_json"])
        game["schedule_revision"] = row["schedule_revision"]
        game["policy_config"] = normalized_policy(json.loads(row["policy_config_json"]))
        db.execute("UPDATE game_prediction_state SET registered_game_json=?,policy_config_json=? WHERE game_id=?",
                   (_json(game), _json(game["policy_config"]), row["game_id"]))


class SchedulerPolicyPlanner:
    def __init__(self, tasks):
        self.tasks = tasks

    def configure(self, game_id, configuration):
        policy = normalized_policy(configuration)
        at = _clock_instant(self.tasks.clock)
        with self.tasks.transaction() as db:
            state = db.execute("SELECT * FROM game_prediction_state WHERE game_id=?", (game_id,)).fetchone()
            if state is None or not state["registered_game_json"]:
                raise VersionStoreError("game_not_registered")
            game = json.loads(state["registered_game_json"])
            if state["locked_at"] or at >= _instant(game["tipoff"], "scheduled_tipoff"):
                raise VersionStoreError("pregame_generation_closed")
            window_start(game, policy)
            old = normalized_policy(json.loads(state["policy_config_json"]))
            if old != policy or not policy["report_enabled"]:
                if policy.get('lifecycle')=='early-final-v1':
                    for prior in db.execute("SELECT task_id,status,last_error,attempt_count,metadata_json FROM generation_tasks WHERE game_id=? AND policy_version=? AND status IN ('pending','failed','running')",(game_id,POLICY_VERSION)).fetchall():
                        metadata=json.loads(prior['metadata_json'])
                        metadata.setdefault('policy_transition_history',[]).append({'at':_utc(at),'previous_status':prior['status'],'previous_error':prior['last_error'],'attempt_count':prior['attempt_count']})
                        db.execute('UPDATE generation_tasks SET metadata_json=? WHERE task_id=?',(_json(metadata),prior['task_id']))
                db.execute("""UPDATE generation_tasks SET status='cancelled',last_error='policy_configuration_changed',
                    finished_at=?,owner_token=NULL,lease_until=NULL WHERE game_id=? AND policy_version=?
                    AND status IN ('pending','failed','running')""", (_utc(at), game_id, POLICY_VERSION))
            if game.get("generation_hold_reason") == "scheduler_policy_pending":
                game.pop("generation_hold_reason")
            game["policy_config"] = policy
            game["schedule_revision"] = state["schedule_revision"]
            db.execute("UPDATE game_prediction_state SET policy_config_json=?,registered_game_json=?,state_revision=state_revision+1 WHERE game_id=?",
                       (_json(policy), _json(game), game_id))
        return game

    def plan_game(self, game):
        at = _clock_instant(self.tasks.clock)
        with self.tasks.transaction() as db:
            state = self.tasks.versions._ensure_state(db, game_identity(game))
            registered = json.loads(state["registered_game_json"] or "{}")
            if game.get("schedule_revision", 1) != state["schedule_revision"]:
                raise VersionStoreError("schedule_revision_changed")
            game = registered or game
            policy = normalized_policy(json.loads(state["policy_config_json"]))
            tip = _instant(game["tipoff"], "scheduled_tipoff")
            if state["locked_at"] or at >= tip or game.get("status") != "scheduled" or registered.get("generation_hold_reason") or not policy["report_enabled"]:
                return []
            if policy.get('lifecycle')=='early-final-v1':
                from .two_stage_lifecycle import plan
                return plan(self,db,game,state,policy,at)
            start, normal_start, kind = window_start(game, policy)
            if at < start and not policy["preheat_enabled"]:
                return []
            revision = state["schedule_revision"]
            final_at = tip - FINAL_LEAD
            meta = {"policy": policy, "schedule_revision": revision, "preview_kind": kind,
                    "normal_window_start": _utc(normal_start), "preheat_start": _utc(start) if kind == "special_preheat" else None}
            def ensure(stage, due, deadline, reason):
                key = policy["policy_identity"] + ":" + stage
                row = db.execute("SELECT * FROM generation_tasks WHERE game_id=? AND schedule_revision=? AND policy_version=? AND logical_key=?",
                    (game["id"], revision, POLICY_VERSION, key)).fetchone()
                if row is None:
                    row = self.tasks._insert(db, game["id"], stage, "scheduled", _utc(due), _utc(deadline), at,
                        logical_key=key, metadata=meta, created_reason=reason)
                return dict(row)
            planned = [ensure("FINAL_PREGAME", final_at, tip, "t_minus_5_start")]
            if at >= tip - LOW_STAGE_STOP:
                db.execute("""UPDATE generation_tasks SET status='superseded',last_error='final_priority_window',finished_at=?
                    WHERE game_id=? AND policy_version=? AND schedule_revision=? AND stage!='FINAL_PREGAME'
                    AND status IN ('pending','failed')""", (_utc(at), game["id"], POLICY_VERSION, revision))
                return planned
            current = db.execute("SELECT payload_json FROM prediction_versions WHERE version_id=?", (state["current_version_id"],)).fetchone()
            if not current:
                planned.append(ensure("EARLY_PREVIEW", max(at, start), final_at, kind))
            return planned

    def material_update(self, event, *, admin_confirmed=False):
        if admin_confirmed is not True:
            raise VersionStoreError("material_update_requires_admin_confirmation")
        required = {"game_id", "schedule_revision", "event_type", "affected_object", "old_state", "new_state",
                    "evidence_batch_id", "evidence_ids", "observed_at", "reason"}
        if not required <= set(event) or event["event_type"] not in MATERIAL_TYPES or not event["evidence_batch_id"] or not event["evidence_ids"] or not event["reason"]:
            raise VersionStoreError("material_update_invalid")
        if event["old_state"] == event["new_state"]:
            raise VersionStoreError("material_update_no_change")
        at = _clock_instant(self.tasks.clock)
        if _instant(event["observed_at"], "observed_at") > at:
            raise VersionStoreError("material_update_after_now")
        game = self.tasks.registered_game(event["game_id"])
        if not game or event["schedule_revision"] != game["schedule_revision"]:
            raise VersionStoreError("schedule_revision_changed")
        if at >= _instant(game["tipoff"], "scheduled_tipoff"):
            raise VersionStoreError("pregame_generation_closed")
        planned = self.plan_game(game)
        if not planned:
            return {"status": "outside_report_window", "task": None}
        if (game.get('policy_config')or{}).get('lifecycle')=='early-final-v1':
            return {'status':'deferred_to_final','task':next(task for task in planned if task['stage']=='FINAL_PREGAME')}
        tip = _instant(game["tipoff"], "scheduled_tipoff")
        with self.tasks.transaction() as db:
            state = self.tasks.versions._ensure_state(db, game_identity(game))
            if state["schedule_revision"] != event["schedule_revision"]:
                raise VersionStoreError("schedule_revision_changed")
            if state["locked_at"] or at >= tip:
                raise VersionStoreError("pregame_generation_closed")
            policy = normalized_policy(json.loads(state["policy_config_json"]))
            if at >= tip - LOW_STAGE_STOP:
                final = next(task for task in planned if task["stage"] == "FINAL_PREGAME")
                row = db.execute("SELECT * FROM generation_tasks WHERE task_id=?", (final["task_id"],)).fetchone()
                meta = json.loads(row["metadata_json"])
                items = meta.setdefault("deferred_evidence", [])
                existing = next((item for item in items if item["evidence_batch_id"] == event["evidence_batch_id"]), None)
                if existing and existing != event:
                    raise VersionStoreError("evidence_batch_identity_conflict")
                if not existing:
                    items.append(event)
                    db.execute("UPDATE generation_tasks SET metadata_json=? WHERE task_id=?", (_json(meta), row["task_id"]))
                return {"status": "deferred_to_final", "task": dict(row)}
            key = policy["policy_identity"] + ":evidence:" + str(event["evidence_batch_id"])
            prior = db.execute("SELECT * FROM generation_tasks WHERE game_id=? AND schedule_revision=? AND policy_version=? AND logical_key=?",
                (game["id"], state["schedule_revision"], POLICY_VERSION, key)).fetchone()
            if prior:
                if json.loads(prior["metadata_json"])["material_event"] != event:
                    raise VersionStoreError("evidence_batch_identity_conflict")
                return {"status": "already_planned", "task": dict(prior)}
            task = self.tasks._insert(db, game["id"], "UPDATED_PREVIEW", "evidence_update", _utc(at), _utc(tip - LOW_STAGE_STOP), at,
                logical_key=key, created_reason=event["reason"],
                metadata={"policy": policy, "material_event": event, "research_refresh_required": True})
            return {"status": "planned", "task": task}

    def revise_schedule(self, game_id, changed_game, *, expected_revision, reason, evidence, admin_confirmed=False):
        if admin_confirmed is not True or not reason or not evidence:
            raise VersionStoreError("schedule_revision_requires_confirmed_reason_and_evidence")
        from .generation_tasks import normalize_registration
        changed = normalize_registration(changed_game)
        if changed["id"] != game_id:
            raise VersionStoreError("schedule_revision_wrong_game")
        at = _clock_instant(self.tasks.clock)
        with self.tasks.transaction() as db:
            state = db.execute("SELECT * FROM game_prediction_state WHERE game_id=?", (game_id,)).fetchone()
            if state is None or not state["registered_game_json"]:
                raise VersionStoreError("game_not_registered")
            if state["schedule_revision"] != expected_revision:
                raise VersionStoreError("schedule_revision_changed")
            old = json.loads(state["registered_game_json"])
            if state["locked_at"] or at >= _instant(old["tipoff"], "scheduled_tipoff") or at >= _instant(changed["tipoff"], "scheduled_tipoff"):
                raise VersionStoreError("pregame_generation_closed")
            # Identity revision must not carry a previous matchup's derived numeric/context snapshot.
            for key in ("prediction", "relationship", "technical", "background", "key_factors", "roster_context",
                        "matchup_history", "season_form", "season_phase", "generation_context"):
                changed.pop(key, None)
            changed["schedule_revision"] = state["schedule_revision"] + 1
            changed["scheduled_tipoff"] = changed["scheduled_tipoff_utc"] = changed["tipoff"]
            changed["scheduled_tipoff_beijing"] = _instant(changed["tipoff"], "scheduled_tipoff").astimezone(CHINA).isoformat()
            changed["schedule_revision_source"] = evidence
            changed["policy_config"] = json.loads(state["policy_config_json"])
            window_start(changed, normalized_policy(changed["policy_config"]))
            db.execute("INSERT INTO game_schedule_revisions VALUES(?,?,?,?,?,?)", (game_id, changed["schedule_revision"], _json(changed), _utc(at), reason, _json(evidence)))
            db.execute("""UPDATE generation_tasks SET status='cancelled',last_error='schedule_revision_replaced',finished_at=?,
                owner_token=NULL,lease_until=NULL WHERE game_id=? AND status IN ('pending','failed','running')""", (_utc(at), game_id))
            db.execute("""UPDATE game_prediction_state SET game_identity_json=?,registered_game_json=?,schedule_revision=?,
                current_version_id=NULL,state_revision=state_revision+1 WHERE game_id=?""",
                (_json(game_identity(changed)), _json(changed), changed["schedule_revision"], game_id))
        self.plan_game(changed)
        return changed
