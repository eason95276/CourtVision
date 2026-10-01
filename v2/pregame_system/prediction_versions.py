"""Append-only product prediction versions with per-game current/lock state."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .analyst import FinalPrediction


DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "frontend" / "output" / "prediction_versions.sqlite"
VERSION_SCHEMA = "courtvision-prediction-version-v1"
_GAME_ID = re.compile(r"(?:espn:\d+|\d{10})\Z")


class VersionStoreError(RuntimeError):
    """A prediction version cannot be safely accepted or selected."""


def _instant(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise VersionStoreError(f"{field}_missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise VersionStoreError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VersionStoreError(f"{field}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise VersionStoreError("clock_timezone_required")
    return value.astimezone(timezone.utc).isoformat()


def _clock_instant(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise VersionStoreError("clock_timezone_required")
    return value.astimezone(timezone.utc)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise VersionStoreError("version_payload_not_json_safe") from exc


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def game_identity(game: dict[str, Any]) -> dict[str, Any]:
    """Keep only schedule identity fields that must match at publish time."""
    game_id = str(game.get("id") or game.get("game_id") or "")
    if not _GAME_ID.fullmatch(game_id):
        raise VersionStoreError("game_identity_invalid")
    home = game.get("home") or {}
    away = game.get("away") or {}
    home_id = game.get("home_team_id", home.get("id"))
    away_id = game.get("away_team_id", away.get("id"))
    try:
        home_id, away_id = int(home_id), int(away_id)
    except (TypeError, ValueError) as exc:
        raise VersionStoreError("team_identity_invalid") from exc
    if home_id == away_id:
        raise VersionStoreError("team_identity_invalid")
    tipoff = game.get("tipoff") or game.get("scheduled_tipoff")
    tipoff_utc = _utc(_instant(tipoff, "scheduled_tipoff"))
    return {
        "game_id": game_id,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "scheduled_tipoff": tipoff_utc,
    }


class PredictionVersionStore:
    """SQLite publication ledger. Published version rows are insert-only."""

    def __init__(self, path: str | Path = DEFAULT_DATABASE, *, clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def initialize(self) -> None:
        db = self.connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS prediction_versions (
                    version_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    request_seq INTEGER NOT NULL CHECK(request_seq > 0),
                    requested_at TEXT NOT NULL,
                    input_cutoff_at TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    UNIQUE(game_id, request_seq),
                    UNIQUE(game_id, version_id)
                );
                CREATE INDEX IF NOT EXISTS idx_prediction_versions_game_seq
                    ON prediction_versions(game_id, request_seq DESC);
                CREATE TABLE IF NOT EXISTS game_prediction_state (
                    game_id TEXT PRIMARY KEY,
                    game_identity_json TEXT NOT NULL,
                    last_issued_request_seq INTEGER NOT NULL DEFAULT 0 CHECK(last_issued_request_seq >= 0),
                    current_version_id TEXT,
                    locked_version_id TEXT,
                    locked_at TEXT,
                    lock_reason TEXT,
                    state_revision INTEGER NOT NULL DEFAULT 0 CHECK(state_revision >= 0),
                    FOREIGN KEY(game_id, current_version_id)
                        REFERENCES prediction_versions(game_id, version_id),
                    FOREIGN KEY(game_id, locked_version_id)
                        REFERENCES prediction_versions(game_id, version_id)
                );
            """)
        finally:
            db.close()

    @staticmethod
    def _validate_game(game: dict[str, Any]) -> tuple[dict[str, Any], datetime]:
        identity = game_identity(game)
        return identity, _instant(identity["scheduled_tipoff"], "scheduled_tipoff")

    @staticmethod
    def _assert_same_identity(stored_json: str, current: dict[str, Any]) -> None:
        try:
            stored = json.loads(stored_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise VersionStoreError("stored_game_identity_corrupt") from exc
        if stored != current:
            raise VersionStoreError("schedule_changed")

    @staticmethod
    def _decode_version(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload_text = row["payload_json"]
        if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != row["payload_sha256"]:
            raise VersionStoreError("version_payload_hash_mismatch")
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise VersionStoreError("version_payload_corrupt") from exc
        if (
            payload.get("schema_version") != row["schema_version"]
            or payload.get("version_id") != row["version_id"]
            or payload.get("game_id") != row["game_id"]
            or payload.get("request_id") != row["request_id"]
            or payload.get("request_seq") != row["request_seq"]
            or payload.get("requested_at") != row["requested_at"]
            or payload.get("input_cutoff_at") != row["input_cutoff_at"]
            or payload.get("generated_at") != row["generated_at"]
            or payload.get("published_at") != row["published_at"]
        ):
            raise VersionStoreError("version_identity_mismatch")
        payload["payload_sha256"] = row["payload_sha256"]
        return payload

    def _ensure_state(self, db: sqlite3.Connection, identity: dict[str, Any]) -> sqlite3.Row:
        db.execute(
            "INSERT OR IGNORE INTO game_prediction_state(game_id, game_identity_json) VALUES(?, ?)",
            (identity["game_id"], _json(identity)),
        )
        row = db.execute(
            "SELECT * FROM game_prediction_state WHERE game_id=?", (identity["game_id"],)
        ).fetchone()
        if row is None:
            raise VersionStoreError("game_state_unavailable")
        self._assert_same_identity(row["game_identity_json"], identity)
        return row

    def begin_request(self, game: dict[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
        identity, tipoff = self._validate_game(game)
        now = _clock_instant(self.clock)
        if game.get("status") != "scheduled" or now >= tipoff:
            raise VersionStoreError("pregame_generation_closed")
        request_id = request_id or str(uuid.uuid4())
        try:
            request_id = str(uuid.UUID(request_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise VersionStoreError("request_id_invalid") from exc
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                state = self._ensure_state(db, identity)
                if state["locked_version_id"] is not None:
                    raise VersionStoreError("pregame_generation_locked")
                existing = db.execute(
                    "SELECT * FROM prediction_versions WHERE game_id=? AND request_id=?",
                    (identity["game_id"], request_id),
                ).fetchone()
                if existing is not None:
                    db.execute("COMMIT")
                    return {"request_id": request_id, "request_seq": int(existing["request_seq"]),
                            "game_identity": identity, "requested_at": existing["requested_at"],
                            "already_published": self._decode_version(existing)}
                seq = int(state["last_issued_request_seq"]) + 1
                db.execute(
                    "UPDATE game_prediction_state SET last_issued_request_seq=? WHERE game_id=?",
                    (seq, identity["game_id"]),
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()
        return {"request_id": request_id, "request_seq": seq, "game_identity": identity,
                "requested_at": _utc(now)}

    def assert_request_open(self, game: dict[str, Any], request: dict[str, Any]) -> None:
        identity, tipoff = self._validate_game(game)
        if identity != request.get("game_identity") or game.get("status") != "scheduled":
            raise VersionStoreError("schedule_changed")
        if _clock_instant(self.clock) >= tipoff:
            raise VersionStoreError("pregame_generation_closed")
        db = self.connect()
        try:
            state = db.execute(
                "SELECT game_identity_json, locked_version_id FROM game_prediction_state WHERE game_id=?",
                (identity["game_id"],),
            ).fetchone()
        finally:
            db.close()
        if state is not None:
            self._assert_same_identity(state["game_identity_json"], identity)
            if state["locked_version_id"] is not None:
                raise VersionStoreError("pregame_generation_locked")

    def publish(self, game: dict[str, Any], request: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        identity, tipoff = self._validate_game(game)
        if identity != request.get("game_identity") or identity != payload.get("game_identity"):
            raise VersionStoreError("identity_mismatch")
        now = _clock_instant(self.clock)
        requested_at = _instant(request.get("requested_at"), "requested_at")
        input_cutoff = _instant(payload.get("input_cutoff_at"), "input_cutoff_at")
        input_frozen = _instant(payload.get("input_frozen_at"), "input_frozen_at")
        generated_at = _instant(payload.get("generated_at"), "generated_at")
        if not requested_at <= input_cutoff <= input_frozen <= generated_at <= now:
            raise VersionStoreError("version_time_order_invalid")
        if _instant(payload.get("requested_at"), "payload_requested_at") != requested_at:
            raise VersionStoreError("version_request_time_mismatch")
        if generated_at >= tipoff or now >= tipoff or game.get("status") != "scheduled":
            raise VersionStoreError("pregame_generation_closed")
        if payload.get("game_id") != identity["game_id"] or payload.get("request_id") != request["request_id"]:
            raise VersionStoreError("version_request_identity_mismatch")
        if game_identity(payload.get("input_snapshot") or {}) != identity:
            raise VersionStoreError("version_input_identity_mismatch")
        if payload.get("prediction") != (payload.get("judgment") or {}).get("final_prediction"):
            raise VersionStoreError("version_prediction_judgment_mismatch")
        final_prediction = (payload.get("judgment") or {}).get("final_prediction")
        if not final_prediction:
            raise VersionStoreError("product_prediction_missing")
        try:
            FinalPrediction.model_validate(final_prediction)
        except Exception as exc:
            raise VersionStoreError("product_prediction_invalid") from exc

        version_id = str(uuid.uuid4())
        body = {**payload, "schema_version": VERSION_SCHEMA, "version_id": version_id,
                "request_seq": int(request["request_seq"]), "published_at": _utc(now)}
        payload_text = _json(body)
        payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                state = self._ensure_state(db, identity)
                if state["locked_version_id"] is not None:
                    raise VersionStoreError("pregame_generation_locked")
                prior = None
                if state["current_version_id"] is not None:
                    prior = db.execute(
                        "SELECT request_seq FROM prediction_versions WHERE game_id=? AND version_id=?",
                        (identity["game_id"], state["current_version_id"]),
                    ).fetchone()
                if prior is not None and int(request["request_seq"]) <= int(prior["request_seq"]):
                    raise VersionStoreError("stale_request_seq")
                now = _clock_instant(self.clock)
                if now >= tipoff or game.get("status") != "scheduled":
                    raise VersionStoreError("pregame_generation_closed")
                published_at = _utc(now)
                body["published_at"] = published_at
                payload_text = _json(body)
                payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
                db.execute(
                    "INSERT INTO prediction_versions(version_id, schema_version, game_id, request_id, request_seq, "
                    "requested_at, input_cutoff_at, generated_at, published_at, payload_json, payload_sha256) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (version_id, VERSION_SCHEMA, identity["game_id"], request["request_id"],
                     int(request["request_seq"]), request["requested_at"], _utc(input_cutoff),
                     _utc(generated_at), published_at, payload_text, payload_hash),
                )
                revision = int(state["state_revision"]) + 1
                db.execute(
                    "UPDATE game_prediction_state SET current_version_id=?, state_revision=? WHERE game_id=?",
                    (version_id, revision, identity["game_id"]),
                )
                db.execute("COMMIT")
            except sqlite3.IntegrityError:
                db.execute("ROLLBACK")
                existing = db.execute(
                    "SELECT * FROM prediction_versions WHERE game_id=? AND request_id=?",
                    (identity["game_id"], request["request_id"]),
                ).fetchone()
                if existing is None:
                    raise
                return self._decode_version(existing) or {}
            except Exception:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()
        return {**body, "payload_sha256": payload_hash, "state_revision": revision,
                "selection_status": "current"}

    def select_current(self, game: dict[str, Any]) -> dict[str, Any]:
        identity, tipoff = self._validate_game(game)
        now = _clock_instant(self.clock)
        closed_status = game.get("status") != "scheduled"
        should_lock = closed_status or now >= tipoff
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                state = db.execute(
                    "SELECT * FROM game_prediction_state WHERE game_id=?", (identity["game_id"],)
                ).fetchone()
                if state is None:
                    state = self._ensure_state(db, identity)
                else:
                    stored_identity = json.loads(state["game_identity_json"])
                    if stored_identity != identity:
                        db.execute("COMMIT")
                        return {
                            "status": "schedule_changed", "game_id": identity["game_id"],
                            "version": None, "version_id": None,
                            "state_revision": int(state["state_revision"]),
                            "locked_at": state["locked_at"], "lock_reason": "schedule_changed",
                            "game_identity": identity,
                        }
                if should_lock and state["locked_at"] is None:
                    current_version_id = state["current_version_id"]
                    db.execute(
                        "UPDATE game_prediction_state SET locked_version_id=?, locked_at=?, lock_reason=?, "
                        "state_revision=state_revision+1 WHERE game_id=?",
                        (current_version_id, _utc(now), "game_status_started" if closed_status else "scheduled_tipoff_reached",
                         identity["game_id"]),
                    )
                    state = db.execute(
                        "SELECT * FROM game_prediction_state WHERE game_id=?", (identity["game_id"],)
                    ).fetchone()
                selected_id = state["locked_version_id"] if state["locked_at"] is not None else state["current_version_id"]
                selected = None
                if selected_id is not None:
                    row = db.execute(
                        "SELECT * FROM prediction_versions WHERE game_id=? AND version_id=?",
                        (identity["game_id"], selected_id),
                    ).fetchone()
                    selected = self._decode_version(row)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()
        if state["locked_at"] is not None:
            status = "locked" if selected is not None else "locked_unavailable"
        else:
            status = "current" if selected is not None else "unpublished"
        return {"status": status, "game_id": identity["game_id"], "version": selected,
                "version_id": selected["version_id"] if selected else None,
                "state_revision": int(state["state_revision"]), "locked_at": state["locked_at"],
                "lock_reason": state["lock_reason"],
                "game_identity": identity}

