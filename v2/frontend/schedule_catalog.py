"""SQLite facts catalog for ESPN NBA schedules; independent of AI task registration."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from v2.pregame_system.prediction_versions import DEFAULT_DATABASE
from v2.pregame_system.market import team_id as nba_team_id


CHINA = ZoneInfo("Asia/Shanghai")
NBA_TEAM_SCHEDULE_ROOT = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
GAME_TYPE_BY_ESPN = {1: "preseason", 2: "regular_season", 3: "playoffs"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: str) -> str:
    return _parse_instant(value).isoformat()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _season_type(event: dict[str, Any]) -> int | None:
    season_type = event.get("seasonType") or {}
    value = season_type.get("type", season_type.get("id"))
    if value is None:
        value = (event.get("season") or {}).get("type")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_status(competition: dict[str, Any]) -> str:
    status_type = ((competition.get("status") or {}).get("type") or {})
    name = str(status_type.get("name") or status_type.get("description") or "").casefold()
    if "cancel" in name:
        return "cancelled"
    if "postpon" in name or "suspend" in name or "delay" in name:
        return "postponed"
    state = str(status_type.get("state") or "").casefold()
    if state == "pre":
        return "scheduled"
    if state == "in":
        return "in_progress"
    if status_type.get("completed") is True and state == "post":
        return "final"
    return "unknown"


def _cup_stage(competition: dict[str, Any]) -> str | None:
    evidence = []
    for note in competition.get("notes") or []:
        evidence.append(" ".join(str(value) for value in note.values() if value is not None)
                        if isinstance(note, dict) else str(note))
    for item in competition.get("competitors") or []:
        for note in item.get("notes") or []:
            evidence.append(" ".join(str(value) for value in note.values() if value is not None)
                            if isinstance(note, dict) else str(note))
    text = " ".join(evidence).casefold()
    if "nba cup" not in text and "emirates cup" not in text:
        return None
    if "quarterfinal" in text or "quarter-final" in text:
        return "quarterfinal"
    if "semifinal" in text or "semi-final" in text:
        return "semifinal"
    if "championship" in text or "final" in text:
        return "championship"
    return "group_play"


class ScheduleValidationError(ValueError):
    def __init__(self, reason: str, *, pending: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.pending = pending


def normalize_espn_event(
    event: dict[str, Any],
    *,
    source_url: str,
    captured_at: str,
    response_sha256: str,
    requested_season: dict[str, Any] | None = None,
    expected_game_type: str | None = None,
) -> dict[str, Any]:
    """Normalize one event using event-level season/type and explicit team identities."""
    if not isinstance(event, dict):
        raise ScheduleValidationError("event_not_object")
    event_id = str(event.get("id") or "").strip()
    if not event_id.isdigit():
        raise ScheduleValidationError("provider_game_id_missing", pending=True)
    season = (event.get("season") or {}).get("year")
    try:
        season = int(season)
    except (TypeError, ValueError):
        raise ScheduleValidationError("event_season_missing", pending=True)
    season_type = _season_type(event)
    game_type = GAME_TYPE_BY_ESPN.get(season_type)
    if game_type is None:
        raise ScheduleValidationError("event_game_type_unknown", pending=True)
    if expected_game_type and game_type != expected_game_type:
        raise ScheduleValidationError("event_game_type_mismatch")
    if requested_season:
        expected_year = requested_season.get("year")
        expected_type = requested_season.get("type")
        try:
            expected_year = int(expected_year) if expected_year is not None else None
            expected_type = int(expected_type) if expected_type is not None else None
        except (TypeError, ValueError):
            raise ScheduleValidationError("requested_season_invalid")
        if expected_year is not None and expected_year != season:
            raise ScheduleValidationError("requested_season_event_season_mismatch")
        if expected_type is not None and expected_type != season_type:
            raise ScheduleValidationError("requested_season_event_type_mismatch")
    date_text = event.get("date") or ((event.get("competitions") or [{}])[0].get("date"))
    if not date_text:
        raise ScheduleValidationError("scheduled_tipoff_missing", pending=True)
    try:
        scheduled_tipoff = _utc_text(date_text)
    except (TypeError, ValueError):
        raise ScheduleValidationError("scheduled_tipoff_invalid", pending=True)
    competitions = event.get("competitions") or []
    if not competitions or not isinstance(competitions[0], dict):
        raise ScheduleValidationError("competition_missing", pending=True)
    competition = competitions[0]
    by_side = {str(item.get("homeAway")): item for item in competition.get("competitors") or []}
    if set(by_side) != {"home", "away"}:
        raise ScheduleValidationError("opponent_or_home_away_pending", pending=True)
    try:
        home_team_id = int(nba_team_id(by_side["home"]))
        away_team_id = int(nba_team_id(by_side["away"]))
    except (KeyError, TypeError, ValueError):
        raise ScheduleValidationError("team_identity_unmapped", pending=True)
    if home_team_id == away_team_id:
        raise ScheduleValidationError("duplicate_team_identity")
    status = _normalize_status(competition)
    scores: dict[str, float | None] = {"home": None, "away": None}
    if status in {"in_progress", "final"}:
        for side in ("home", "away"):
            scores[side] = _number(by_side[side].get("score"))
    venue = competition.get("venue") or {}
    address = venue.get("address") or {}
    neutral = competition.get("neutralSite")
    neutral = neutral if isinstance(neutral, bool) else None
    raw_event = _json(event)
    source_type = "espn_scoreboard" if "/scoreboard" in source_url else "espn_team_schedule" if "/schedule" in source_url else "espn_schedule"
    return {
        "game_id": f"espn:{event_id}",
        "provider_namespace": "espn",
        "provider_game_id": event_id,
        "nba_game_id": None,
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "home_espn_team_id": str((by_side["home"].get("team") or {}).get("id") or by_side["home"].get("id") or ""),
        "away_espn_team_id": str((by_side["away"].get("team") or {}).get("id") or by_side["away"].get("id") or ""),
        "home_team_name": str((by_side["home"].get("team") or {}).get("displayName") or ""),
        "away_team_name": str((by_side["away"].get("team") or {}).get("displayName") or ""),
        "season": season,
        "season_label": (event.get("season") or {}).get("displayName"),
        "game_type": game_type,
        "cup_stage": _cup_stage(competition),
        "scheduled_tipoff": scheduled_tipoff,
        "timezone": "UTC",
        "venue": venue.get("fullName"),
        "venue_city": address.get("city"),
        "neutral_site": neutral,
        "status": status,
        "home_score": scores["home"],
        "away_score": scores["away"],
        "source_type": source_type,
        "source_url": source_url,
        "captured_at": _utc_text(captured_at),
        "response_sha256": response_sha256,
        "raw_event_json": raw_event,
        "event_sha256": hashlib.sha256(raw_event.encode("utf-8")).hexdigest(),
        "validation_json": _json({
            "normalizer_version": "espn-schedule-v1",
            "game_id": "provider_event_id",
            "season": "event_level_season",
            "game_type": "event_level_season_type",
            "team_identity": "mapped_from_espn_team_abbreviation",
            "tipoff": "timezone_aware_utc",
            "scores": "only_in_progress_or_final",
        }),
        "source_ref": {"url": source_url, "captured_at": _utc_text(captured_at), "response_sha256": response_sha256},
    }


class ScheduleCatalog:
    """Current schedule facts plus import/run receipts in Prediction Version SQLite."""

    def __init__(self, path: str | Path = DEFAULT_DATABASE, *, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def initialize(self) -> None:
        db = self.connect()
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schedule_catalog_games (
                    game_id TEXT PRIMARY KEY,
                    provider_namespace TEXT NOT NULL,
                    provider_game_id TEXT NOT NULL,
                    nba_game_id TEXT,
                    home_team_id INTEGER NOT NULL,
                    away_team_id INTEGER NOT NULL,
                    home_espn_team_id TEXT,
                    away_espn_team_id TEXT,
                    home_team_name TEXT,
                    away_team_name TEXT,
                    season INTEGER NOT NULL,
                    season_label TEXT,
                    game_type TEXT NOT NULL,
                    cup_stage TEXT,
                    scheduled_tipoff TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    venue TEXT,
                    venue_city TEXT,
                    neutral_site INTEGER,
                    status TEXT NOT NULL,
                    home_score REAL,
                    away_score REAL,
                    source_url TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'espn_schedule',
                    captured_at TEXT NOT NULL,
                    response_sha256 TEXT NOT NULL,
                    event_sha256 TEXT NOT NULL,
                    raw_event_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    schedule_revision INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    last_sync_run_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_schedule_catalog_tipoff
                    ON schedule_catalog_games(scheduled_tipoff);
                CREATE INDEX IF NOT EXISTS idx_schedule_catalog_season_type
                    ON schedule_catalog_games(season, game_type);
                CREATE TABLE IF NOT EXISTS schedule_catalog_runs (
                    run_id TEXT PRIMARY KEY,
                    run_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    requested_start_date TEXT,
                    requested_end_date TEXT,
                    source_receipts_json TEXT NOT NULL,
                    source_count INTEGER NOT NULL,
                    response_count INTEGER NOT NULL,
                    event_observations INTEGER NOT NULL,
                    normalized_count INTEGER NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_schedule_catalog_runs_completed
                    ON schedule_catalog_runs(completed_at DESC);
                CREATE TABLE IF NOT EXISTS schedule_catalog_pending (
                    pending_id TEXT PRIMARY KEY,
                    provider_namespace TEXT NOT NULL,
                    provider_item_id TEXT,
                    game_id TEXT,
                    season INTEGER,
                    game_type TEXT,
                    known_team_id INTEGER,
                    scheduled_tipoff TEXT,
                    reason TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    response_sha256 TEXT NOT NULL,
                    event_sha256 TEXT NOT NULL,
                    raw_event_json TEXT NOT NULL,
                    last_sync_run_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedule_catalog_conflicts (
                    conflict_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    game_id TEXT,
                    conflict_type TEXT NOT NULL,
                    action TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    existing_json TEXT,
                    incoming_json TEXT NOT NULL
                );
            """)
            columns={row[1] for row in db.execute("PRAGMA table_info(schedule_catalog_games)")}
            if "source_type" not in columns:
                db.execute("ALTER TABLE schedule_catalog_games ADD COLUMN source_type TEXT NOT NULL DEFAULT 'espn_schedule'")
        finally:
            db.close()

    def _record_pending(self, db: sqlite3.Connection, event: dict[str, Any], receipt: dict[str, Any], reason: str, run_id: str) -> None:
        raw_event = _json(event)
        provider_item_id = str(event.get("id") or "") or None
        seed = f"espn|{provider_item_id or hashlib.sha256(raw_event.encode()).hexdigest()}"
        pending_id = "pending:" + hashlib.sha256(seed.encode()).hexdigest()[:24]
        season = (event.get("season") or {}).get("year")
        try:
            season = int(season) if season is not None else None
        except (TypeError, ValueError):
            season = None
        season_type = _season_type(event)
        game_type = GAME_TYPE_BY_ESPN.get(season_type)
        event_date = event.get("date")
        scheduled_tipoff = None
        if event_date:
            try:
                scheduled_tipoff = _utc_text(event_date)
            except (TypeError, ValueError):
                pass
        known_team_id = None
        for competitor in ((event.get("competitions") or [{}])[0].get("competitors") or []):
            try:
                known_team_id = int(nba_team_id(competitor))
                break
            except (KeyError, TypeError, ValueError):
                continue
        db.execute("""INSERT INTO schedule_catalog_pending
            (pending_id,provider_namespace,provider_item_id,game_id,season,game_type,known_team_id,scheduled_tipoff,
             reason,source_url,captured_at,response_sha256,event_sha256,raw_event_json,last_sync_run_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(pending_id) DO UPDATE SET reason=excluded.reason,source_url=excluded.source_url,
              captured_at=excluded.captured_at,response_sha256=excluded.response_sha256,
              event_sha256=excluded.event_sha256,raw_event_json=excluded.raw_event_json,last_sync_run_id=excluded.last_sync_run_id""",
            (pending_id, "espn", provider_item_id, None, season, game_type, known_team_id, scheduled_tipoff,
             reason, receipt["url"], receipt["captured_at"], receipt["raw_sha256"],
             hashlib.sha256(raw_event.encode("utf-8")).hexdigest(), raw_event, run_id))

    @staticmethod
    def _core_identity(row: dict[str, Any]) -> tuple[Any, ...]:
        return (row["provider_namespace"], row["provider_game_id"], row["season"], row["game_type"],
                row["home_team_id"], row["away_team_id"])

    def _record_conflict(self, db, run_id, game_id, conflict_type, action, existing, incoming) -> None:
        db.execute("""INSERT INTO schedule_catalog_conflicts
            (conflict_id,run_id,game_id,conflict_type,action,recorded_at,existing_json,incoming_json)
            VALUES(?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), run_id, game_id, conflict_type, action, self.clock().astimezone(timezone.utc).isoformat(),
             _json(existing) if existing is not None else None, _json(incoming)))

    def _is_prediction_registered(self, db: sqlite3.Connection, game_id: str) -> bool:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "game_prediction_state" in tables and db.execute(
            "SELECT 1 FROM game_prediction_state WHERE game_id=?", (game_id,)
        ).fetchone():
            return True
        if "prediction_versions" in tables and db.execute(
            "SELECT 1 FROM prediction_versions WHERE game_id=? LIMIT 1", (game_id,)
        ).fetchone():
            return True
        return False

    def _upsert_game(self, db: sqlite3.Connection, row: dict[str, Any], run_id: str) -> str:
        old = db.execute("SELECT * FROM schedule_catalog_games WHERE game_id=?", (row["game_id"],)).fetchone()
        now = self.clock().astimezone(timezone.utc).isoformat()
        if old is None:
            cols = [key for key in row if key not in {"source_ref", "source_refs"}]
            values = [row[key] for key in cols]
            cols.extend(["source_refs_json", "schedule_revision", "updated_at", "last_sync_run_id"])
            values.extend([_json(row.get("source_refs") or [row["source_ref"]]), 1, now, run_id])
            db.execute(f"INSERT INTO schedule_catalog_games ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", values)
            return "inserted"

        old_dict = dict(old)
        if self._core_identity(old_dict) != self._core_identity(row):
            self._record_conflict(db, run_id, row["game_id"], "provider_identity_conflict", "held_existing",
                                  old_dict, row)
            return "conflict"
        same_tipoff = old_dict["scheduled_tipoff"] == row["scheduled_tipoff"]
        is_registered = self._is_prediction_registered(db, row["game_id"])
        if not same_tipoff and is_registered:
            self._record_conflict(db, run_id, row["game_id"], "registered_schedule_change", "held_registered_game",
                                  old_dict, row)
            return "conflict"
        revision = int(old_dict["schedule_revision"])
        if not same_tipoff:
            revision += 1
            self._record_conflict(db, run_id, row["game_id"], "schedule_time_revision", "updated_unregistered",
                                  old_dict, row)

        old_status = old_dict["status"]
        incoming_status = row["status"]
        protected_terminal = old_status == "final" and incoming_status in {"unknown", "scheduled", "in_progress"}
        if protected_terminal or incoming_status == "unknown":
            effective_status = old_status
            effective_home_score, effective_away_score = old_dict["home_score"], old_dict["away_score"]
        else:
            effective_status = incoming_status
            effective_home_score, effective_away_score = row["home_score"], row["away_score"]
        existing_refs = json.loads(old_dict["source_refs_json"] or "[]")
        refs = existing_refs + [row["source_ref"]]
        # Bound the mutable current receipt list; complete responses remain represented by immutable run rows.
        refs = refs[-30:]
        updates = {
            **row,
            "status": effective_status,
            "home_score": effective_home_score,
            "away_score": effective_away_score,
            "source_refs_json": _json(refs),
            "schedule_revision": revision,
            "updated_at": now,
            "last_sync_run_id": run_id,
            "source_refs_json": _json(refs),
        }
        updates.pop("source_ref", None)
        updates.pop("source_refs", None)
        db.execute("UPDATE schedule_catalog_games SET " + ",".join(f"{key}=?" for key in updates) + " WHERE game_id=?",
                   [*updates.values(), row["game_id"]])
        return "updated" if (not same_tipoff or any(old_dict.get(k) != updates.get(k) for k in
            ("status", "home_score", "away_score", "source_url", "captured_at", "event_sha256"))) else "unchanged"

    def ingest(
        self,
        run_type: str,
        responses: list[dict[str, Any]],
        *,
        requested_start_date: str | None = None,
        requested_end_date: str | None = None,
        include_game_types: set[str] | None = None,
        requested_season: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        observations: dict[str, dict[str, Any]] = {}
        batch_conflicts: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        batch_conflict_ids: set[str] = set()
        pending: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        invalid: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        event_observations = 0
        successful_responses = 0
        normalized_count = 0
        response_receipts = []
        for response in responses:
            receipt = {
                "url": str(response.get("url") or ""),
                "captured_at": str(response.get("captured_at") or self.clock().astimezone(timezone.utc).isoformat()),
                "status": str(response.get("status") or "unavailable"),
                "http_status": response.get("http_status"),
                "raw_sha256": str(response.get("raw_sha256") or hashlib.sha256(b"").hexdigest()),
                "error": response.get("error"),
                "transport": response.get("transport"),
            }
            response_receipts.append(receipt)
            if receipt["status"] != "success" or not isinstance(response.get("payload"), dict):
                continue
            successful_responses += 1
            payload = response["payload"]
            events = payload.get("events") or []
            if not isinstance(events, list):
                invalid.append(("events_not_list", {}, receipt))
                continue
            event_observations += len(events)
            requested = response.get("requested_season") or requested_season
            expected_type = response.get("expected_game_type")
            for event in events:
                if isinstance(event, dict) and event.get("_schedule_validation_error"):
                    invalid.append((str(event["_schedule_validation_error"]), event, receipt))
                    continue
                expected_team_id = response.get("expected_espn_team_id")
                if expected_team_id and isinstance(event, dict):
                    competitions = event.get("competitions") or []
                    competitors = competitions[0].get("competitors") or [] if competitions else []
                    observed_team_ids = {
                        str(item.get("team", {}).get("id") or item.get("id") or "")
                        for item in competitors
                    }
                    if str(expected_team_id) not in observed_team_ids:
                        invalid.append(("team_schedule_membership_mismatch", event, receipt))
                        continue
                try:
                    row = normalize_espn_event(
                        event,
                        source_url=receipt["url"],
                        captured_at=receipt["captured_at"],
                        response_sha256=receipt["raw_sha256"],
                        requested_season=requested,
                        expected_game_type=expected_type,
                    )
                except ScheduleValidationError as exc:
                    if exc.pending:
                        pending.append((event if isinstance(event, dict) else {"raw": event}, receipt, exc.reason))
                    else:
                        invalid.append((exc.reason, event if isinstance(event, dict) else {"raw": event}, receipt))
                    continue
                if include_game_types and row["game_type"] not in include_game_types:
                    continue
                normalized_count += 1
                if row["game_id"] in batch_conflict_ids:
                    continue
                prior = observations.get(row["game_id"])
                if prior is None:
                    row["source_refs"] = [row["source_ref"]]
                    observations[row["game_id"]] = row
                    continue
                if self._core_identity(prior) != self._core_identity(row) or prior["scheduled_tipoff"] != row["scheduled_tipoff"]:
                    batch_conflicts.append((row["game_id"], prior, row))
                    observations.pop(row["game_id"], None)
                    batch_conflict_ids.add(row["game_id"])
                    continue
                refs = prior.setdefault("source_refs", [prior["source_ref"]])
                refs.append(row["source_ref"])
                # In the same collection, prefer the more recent event state; never regress a final to a schedule shell.
                if prior["status"] != "final" and row["status"] != "unknown":
                    prior.update({key: row[key] for key in ("status", "home_score", "away_score", "source_url", "captured_at", "response_sha256", "event_sha256", "raw_event_json")})
                    prior["source_ref"] = row["source_ref"]

        all_success = successful_responses == len(responses) and bool(responses)
        run_status = "failed" if successful_responses == 0 else "success" if all_success and not invalid and not batch_conflicts else "partial"
        result = {"status": run_status, "run_id": run_id, "source_count": len(responses),
                  "successful_sources": successful_responses, "event_observations": event_observations,
                  "normalized": len(observations), "inserted": 0, "updated": 0, "unchanged": 0,
                  "conflicts": len(batch_conflicts) + len(invalid), "pending": len(pending),
                  "observed_game_ids": sorted(observations)}
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            completed = self.clock().astimezone(timezone.utc).isoformat()
            db.execute("""INSERT INTO schedule_catalog_runs
                (run_id,run_type,status,requested_at,completed_at,requested_start_date,requested_end_date,
                 source_receipts_json,source_count,response_count,event_observations,normalized_count,result_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, run_type, run_status, completed, completed, requested_start_date, requested_end_date,
                 _json(response_receipts), len(responses), successful_responses, event_observations,
                 len(observations), _json(result)))
            for game_id, old, new in batch_conflicts:
                self._record_conflict(db, run_id, game_id, "batch_duplicate_conflict", "held_batch", old, new)
            for reason, event, receipt in invalid:
                self._record_conflict(db, run_id, None, reason, "held_invalid_event", None,
                                      {"event": event, "source": receipt})
            for event, receipt, reason in pending:
                self._record_pending(db, event, receipt, reason, run_id)
            for game_id, row in observations.items():
                action = self._upsert_game(db, row, run_id)
                if action == "inserted": result["inserted"] += 1
                elif action == "updated": result["updated"] += 1
                elif action == "unchanged": result["unchanged"] += 1
                else: result["conflicts"] += 1
            result["normalized"] = len(observations)
            result["status"] = run_status if result["conflicts"] == 0 else "partial"
            db.execute("UPDATE schedule_catalog_runs SET status=?,result_json=? WHERE run_id=?",
                       (result["status"], _json(result), run_id))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        return result

    def get_game(self, game_id: str) -> dict[str, Any] | None:
        db = self.connect()
        try:
            row = db.execute("SELECT * FROM schedule_catalog_games WHERE game_id=?", (game_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            for key in ("source_refs_json", "validation_json", "raw_event_json"):
                result[key.removesuffix("_json")] = json.loads(result.pop(key))
            return result
        finally:
            db.close()

    def completed_team_games_before(
        self, *, team_id: int, season: int, cutoff: str, exclude_game_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return completed same-season games before a timezone-aware cutoff, newest first."""
        team_id = int(team_id)
        season = int(season)
        cutoff_utc = _utc_text(cutoff)
        clauses = [
            "season=?", "status='final'", "scheduled_tipoff<?",
            "home_score IS NOT NULL", "away_score IS NOT NULL",
            "(home_team_id=? OR away_team_id=?)",
        ]
        values: list[Any] = [season, cutoff_utc, team_id, team_id]
        if exclude_game_id:
            clauses.append("game_id<>?")
            values.append(str(exclude_game_id))
        db = self.connect()
        try:
            rows = db.execute(
                "SELECT game_id, season, game_type, scheduled_tipoff, status, "
                "home_team_id, away_team_id, home_team_name, away_team_name, home_score, away_score, "
                "captured_at, source_url "
                "FROM schedule_catalog_games WHERE " + " AND ".join(clauses) +
                " ORDER BY scheduled_tipoff DESC, game_id DESC",
                values,
            )
            return [dict(row) for row in rows]
        finally:
            db.close()

    def count_games(self, *, season: int | None = None, game_type: str | None = None) -> int:
        filters, values = [], []
        if season is not None:
            filters.append("season=?"); values.append(season)
        if game_type is not None:
            filters.append("game_type=?"); values.append(game_type)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        db = self.connect()
        try:
            return int(db.execute("SELECT COUNT(*) FROM schedule_catalog_games" + where, values).fetchone()[0])
        finally:
            db.close()

    def count_generation_tasks(self) -> int:
        db = self.connect()
        try:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_tasks'").fetchone()
            return int(db.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0]) if exists else 0
        finally:
            db.close()

    def conflict_count(self) -> int:
        db = self.connect()
        try:
            return int(db.execute("SELECT COUNT(*) FROM schedule_catalog_conflicts").fetchone()[0])
        finally:
            db.close()

    def last_conflict(self) -> dict[str, Any] | None:
        db = self.connect()
        try:
            row = db.execute("SELECT * FROM schedule_catalog_conflicts ORDER BY recorded_at DESC LIMIT 1").fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    def pending_items(self) -> list[dict[str, Any]]:
        db = self.connect()
        try:
            return [dict(row) for row in db.execute("SELECT * FROM schedule_catalog_pending ORDER BY pending_id")]
        finally:
            db.close()

    def update_run_result(self, run_id: str, additions: dict[str, Any]) -> dict[str, Any]:
        db=self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT result_json FROM schedule_catalog_runs WHERE run_id=?",(run_id,)).fetchone()
            if not row:
                raise KeyError("schedule_run_not_found")
            result=json.loads(row[0])
            result.update(additions)
            db.execute("UPDATE schedule_catalog_runs SET result_json=? WHERE run_id=?",(_json(result),run_id))
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise
        finally:db.close()

    def all_games(self, *, season: int | None = None, game_type: str | None = None) -> list[dict[str, Any]]:
        filters, values = [], []
        if season is not None:
            filters.append("season=?"); values.append(season)
        if game_type is not None:
            filters.append("game_type=?"); values.append(game_type)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        db = self.connect()
        try:
            rows = db.execute("SELECT * FROM schedule_catalog_games" + where + " ORDER BY scheduled_tipoff,game_id", values)
            output=[]
            for row in rows:
                item=dict(row)
                for key in ("source_refs_json", "validation_json", "raw_event_json"):
                    item[key.removesuffix("_json")] = json.loads(item.pop(key))
                output.append(item)
            return output
        finally:
            db.close()

    def homepage_window(self, at: datetime | None = None) -> dict[str, Any]:
        instant = at or self.clock()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("timezone_required")
        now = instant.astimezone(CHINA)
        today = now.date()
        tomorrow = today.fromordinal(today.toordinal() + 1)
        all_rows = self.all_games()
        games=[]
        for row in all_rows:
            try:
                tipoff = _parse_instant(row["scheduled_tipoff"])
            except (TypeError, ValueError):
                continue
            local_day = tipoff.astimezone(CHINA).date()
            if local_day not in {today, tomorrow}:
                continue
            games.append({**row, "beijing_date": local_day.isoformat()})
        db=self.connect()
        try:
            latest=db.execute("SELECT * FROM schedule_catalog_runs ORDER BY completed_at DESC LIMIT 1").fetchone()
            last_success=db.execute("SELECT MAX(completed_at) FROM schedule_catalog_runs WHERE status IN ('success','partial')").fetchone()[0]
        finally: db.close()
        if not last_success:
            status = "unavailable"
        elif latest and latest["status"] in {"failed", "partial"}:
            status = "stale"
        else:
            age=(instant.astimezone(timezone.utc)-_parse_instant(last_success)).total_seconds()
            status = "stale" if age > 6*60*60 else "ready"
        if status == "ready" and not games:
            status = "empty"
        if status == "ready" and latest and games:
            try:
                latest_result = json.loads(latest["result_json"])
                observed_ids = set(latest_result.get("observed_game_ids") or [])
                start_date = date.fromisoformat(latest["requested_start_date"]) if latest["requested_start_date"] else None
                end_date = date.fromisoformat(latest["requested_end_date"]) if latest["requested_end_date"] else None
                for game in games:
                    source_day = _parse_instant(game["scheduled_tipoff"]).date()
                    in_source_window = (start_date is None or source_day >= start_date) and (end_date is None or source_day <= end_date)
                    if in_source_window and game["game_id"] not in observed_ids:
                        status = "stale"
                        break
            except (TypeError, ValueError, json.JSONDecodeError):
                status = "stale"
        latest_error = None
        if latest and latest["status"] in {"failed", "partial"}:
            try:
                latest_receipts = json.loads(latest["source_receipts_json"])
                latest_error = next((item.get("error") for item in latest_receipts if item.get("error")), None)
            except (TypeError, json.JSONDecodeError):
                latest_error = "schedule_sync_incomplete"
        return {
            "status": status,
            "today": today.isoformat(),
            "tomorrow": tomorrow.isoformat(),
            "as_of": self.clock().astimezone(timezone.utc).isoformat(),
            "last_success_at": last_success,
            "last_attempt_at": latest["completed_at"] if latest else None,
            "last_error": latest_error,
            "today_count": sum(row["beijing_date"] == today.isoformat() for row in games),
            "tomorrow_count": sum(row["beijing_date"] == tomorrow.isoformat() for row in games),
            "games": games,
        }
