"""Collect auditable player availability and recent-usage evidence.

This module is deliberately separate from the CourtVision databases and from
the pregame Agent.  It uses only official NBA Stats responses for the ten
players named in the archived injury report for game 0022500564.

The collector has no retry loop.  A failed request is persisted as a failed
raw artifact and the calculation keeps the resulting field unknown.  Existing
raw artifacts and formal outputs are never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INJURY_PATH = ROOT / "data" / "pregame_snapshots" / "injury_import_0022500564.json"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "player_evidence" / "0022500564_evidence_final_corrected"
GAME_ID = "0022500564"
CURRENT_SEASON = "2025-26"
PREVIOUS_SEASON = "2024-25"
CUTOFF_LOCAL = "2026-01-13T19:00:00-05:00"
CUTOFF = datetime.fromisoformat(CUTOFF_LOCAL)
CUTOFF_UTC = CUTOFF.astimezone(timezone.utc)
CUTOFF_DATE = CUTOFF.date()

OFFICIAL_STATS_ROOT = "https://stats.nba.com/stats"
NBA_API_PYPI = "https://pypi.org/project/nba-api/1.10.2/"
NBA_API_GITHUB = "https://github.com/swar/nba_api"
NBA_TERMS = "https://www.nba.com/termsofuse"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256_json(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def write_json_once(path: Path, payload: Any) -> bool:
    """Write once.  Returning False means an existing artifact was protected."""

    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


def parse_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text[:19] if fmt.startswith("%Y-%m-%dT") else text, fmt).date()
        except ValueError:
            continue
    if text.endswith("Z"):
        try:
            return datetime.fromisoformat(text[:-1]).date()
        except ValueError:
            return None
    return None


def parse_minutes(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("PT"):
        match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", text)
        if match:
            hours = float(match.group(1) or 0)
            minutes = float(match.group(2) or 0)
            seconds = float(match.group(3) or 0)
            return hours * 60 + minutes + seconds / 60
    if ":" in text:
        parts = text.split(":")
        try:
            if len(parts) == 2:
                return float(parts[0]) + float(parts[1]) / 60
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def extract_rows(payload: dict[str, Any], result_name: str) -> list[dict[str, Any]]:
    """Convert an nba_api result set to dictionaries without changing values."""

    result_sets = payload.get("resultSets") or payload.get("resultSet") or []
    if isinstance(result_sets, dict):
        result_sets = [result_sets]
    for result_set in result_sets:
        if result_set.get("name") != result_name:
            continue
        headers = result_set.get("headers", [])
        return [dict(zip(headers, row)) for row in result_set.get("rowSet", [])]
    return []


def request_and_store(
    *,
    path: Path,
    label: str,
    endpoint: str,
    parameters: dict[str, Any],
    call: Callable[[], dict[str, Any]],
    sleep_seconds: float = 0.0,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Make one request, save its response or failure, and never retry."""

    if path.exists():
        envelope = load_json(path)
        stored_status = envelope.get("status")
        return envelope.get("response"), {
            "label": label,
            "status": "cached_no_request" if stored_status == "success" else "cached_failed",
            "stored_status": stored_status,
            "path": str(path),
            "response_sha256": envelope.get("response_sha256"),
        }

    started = time.perf_counter()
    fetched_at = now_utc()
    try:
        payload = call()
        response_hash = sha256_json(payload)
        envelope = {
            "artifact_type": "raw_nba_stats_response",
            "status": "success",
            "source_provider": "NBA Stats",
            "source_url": f"{OFFICIAL_STATS_ROOT}/{endpoint}",
            "client": {
                "package": "nba_api",
                "version": "1.10.2",
                "pypi_url": NBA_API_PYPI,
                "github_url": NBA_API_GITHUB,
                "license": "MIT",
            },
            "request_parameters": parameters,
            "retrieved_at_utc": fetched_at,
            "capture_relation_to_cutoff": "later_acquired_historical_data",
            "response_sha256": response_hash,
            "response": payload,
        }
        written = write_json_once(path, envelope)
        status = "success" if written else "protected_existing_output"
        result = {
            "label": label,
            "status": status,
            "path": str(path),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "response_sha256": response_hash,
            "retrieved_at_utc": fetched_at,
        }
    except Exception as exc:  # noqa: BLE001 - failure is part of the evidence boundary
        envelope = {
            "artifact_type": "raw_nba_stats_response",
            "status": "failed",
            "source_provider": "NBA Stats",
            "source_url": f"{OFFICIAL_STATS_ROOT}/{endpoint}",
            "client": {
                "package": "nba_api",
                "version": "1.10.2",
                "pypi_url": NBA_API_PYPI,
                "github_url": NBA_API_GITHUB,
                "license": "MIT",
            },
            "request_parameters": parameters,
            "retrieved_at_utc": fetched_at,
            "capture_relation_to_cutoff": "later_acquired_historical_data",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        written = write_json_once(path, envelope)
        result = {
            "label": label,
            "status": "failed" if written else "protected_existing_output",
            "path": str(path),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "retrieved_at_utc": fetched_at,
        }
        payload = None
    if sleep_seconds:
        time.sleep(sleep_seconds)
    return payload, result


def classify_reason(reason: str) -> dict[str, Any]:
    text = (reason or "").casefold()
    tags: list[str] = []
    if "concussion protocol" in text:
        tags.append("concussion_protocol")
    if "injury/illness" in text:
        tags.append("health_related")
    if "g league" in text:
        tags.append("g_league")
    if "two-way" in text or "two way" in text:
        tags.append("two_way")
    if "not with team" in text:
        tags.append("not_with_team")
    if not tags:
        tags.append("other_reason")
    if "concussion_protocol" in tags:
        primary = "concussion_protocol"
    elif "health_related" in tags:
        primary = "health_related"
    elif "g_league" in tags and "two_way" in tags:
        primary = "g_league_two_way"
    elif "g_league" in tags:
        primary = "g_league"
    elif "not_with_team" in tags:
        primary = "not_with_team"
    else:
        primary = "other_non_health"
    return {
        "primary": primary,
        "group": "health_related"
        if "health_related" in tags or "concussion_protocol" in tags
        else "non_health",
        "tags": tags,
    }


def availability_group(status: str) -> str:
    normalized = (status or "").strip().casefold()
    if normalized == "out":
        return "confirmed_out"
    if normalized == "questionable":
        return "questionable"
    if normalized == "available":
        return "reported_available"
    return "unknown_status"


def load_report_records(path: Path = DEFAULT_INJURY_PATH) -> list[dict[str, Any]]:
    payload = load_json(path)
    records = payload.get("records") or []
    if len(records) != 10:
        raise ValueError(f"expected 10 injury-report records, got {len(records)}")
    return records


def resolve_identity(records: list[dict[str, Any]], common_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in common_rows:
        by_name[normalize_name(str(row.get("DISPLAY_FIRST_LAST") or ""))].append(row)
    resolved: list[dict[str, Any]] = []
    for record in records:
        name = record["player_name"]
        matches = by_name.get(normalize_name(name), [])
        identity = matches[0] if len(matches) == 1 else None
        resolved.append(
            {
                **record,
                "player_id": identity.get("PERSON_ID") if identity else None,
                "identity_match_status": "exact_unique" if identity else "unmatched_or_ambiguous",
                "identity_source_fields": (
                    {
                        "display_first_last": identity.get("DISPLAY_FIRST_LAST"),
                        "team_id_latest_common_all_players": identity.get("TEAM_ID"),
                        "team_abbreviation_latest_common_all_players": identity.get("TEAM_ABBREVIATION"),
                        "roster_status_latest_common_all_players": identity.get("ROSTERSTATUS"),
                    }
                    if identity
                    else None
                ),
                "availability_group": availability_group(record.get("availability_status", "")),
                "reason_classification": classify_reason(record.get("reason", "")),
            }
        )
    return resolved


def cutoff_player_rows(
    rows: list[dict[str, Any]],
    *,
    season: str,
    cutoff_date: date = CUTOFF_DATE,
    exclude_game_id: str = GAME_ID,
) -> list[dict[str, Any]]:
    """Keep only regular-season game rows strictly before the cutoff date."""

    filtered: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("SEASON_YEAR") or season) != season:
            continue
        game_id = str(row.get("GAME_ID") or "")
        game_date = parse_date(row.get("GAME_DATE"))
        if not game_id or game_id == exclude_game_id or game_date is None:
            continue
        if game_date >= cutoff_date:
            continue
        filtered.append(row)
    return filtered


def _number(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def row_is_appearance(row: dict[str, Any] | None) -> bool:
    if not row or not row.get("GAME_ID"):
        return False
    if row.get("MIN") is not None and row.get("MIN") != "":
        return parse_minutes(row.get("MIN")) is not None
    return bool(str(row.get("MIN_SEC") or "").strip())


def build_start_map(rows: list[dict[str, Any]]) -> dict[tuple[int, int], int]:
    starts: dict[tuple[int, int], int] = {}
    for row in rows:
        try:
            key = (int(row["PLAYER_ID"]), int(row["TEAM_ID"]))
            starts[key] = int(row.get("GP") or 0)
        except (KeyError, TypeError, ValueError):
            continue
    return starts


def aggregate_team_stints(
    rows: list[dict[str, Any]],
    *,
    season: str,
    starts: dict[tuple[int, int], int] | None,
    starts_available: bool,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row_is_appearance(row):
            continue
        try:
            team_id = int(row.get("TEAM_ID"))
        except (TypeError, ValueError):
            continue
        grouped[(team_id, str(row.get("TEAM_ABBREVIATION") or ""), str(row.get("TEAM_NAME") or ""))].append(row)

    output: list[dict[str, Any]] = []
    for (team_id, tricode, team_name), team_rows in sorted(grouped.items()):
        team_rows = sorted(team_rows, key=lambda r: (parse_date(r.get("GAME_DATE")) or date.min, str(r.get("GAME_ID"))))
        game_count = len({str(r.get("GAME_ID")) for r in team_rows})
        minutes_total = sum(_number(r, "MIN") for r in team_rows)
        starts_value = (
            starts.get((int(team_rows[0].get("PLAYER_ID")), team_id), 0)
            if starts_available and starts is not None
            else None
        )
        denominator = game_count
        def avg(key: str) -> float | None:
            if denominator == 0:
                return None
            return round(sum(_number(r, key) for r in team_rows) / denominator, 3)

        output.append(
            {
                "season": season,
                "team_id": team_id,
                "team_tricode": tricode,
                "team_name": team_name,
                "games_played": game_count,
                "starts": starts_value,
                "minutes_total": round(minutes_total, 3),
                "minutes_per_appearance": round(minutes_total / denominator, 3) if denominator else None,
                "points_per_appearance": avg("PTS"),
                "field_goals_attempted_per_appearance": avg("FGA"),
                "assists_per_appearance": avg("AST"),
                "rebounds_per_appearance": avg("REB"),
                "sample": {
                    "appearance_game_count": game_count,
                    "stat_denominator": "official PlayerGameLogs rows with a valid MIN or MIN_SEC",
                    "start_denominator": "official LeagueDashPlayerStats rows filtered to Starters"
                    if starts_available
                    else "unavailable because starters request failed",
                    "first_game_date": str(parse_date(team_rows[0].get("GAME_DATE"))) if team_rows else None,
                    "last_game_date": str(parse_date(team_rows[-1].get("GAME_DATE"))) if team_rows else None,
                },
                "missingness": None,
            }
        )
    return output


def empty_season_summary(season: str, reason: str, starts_available: bool) -> dict[str, Any]:
    return {
        "season": season,
        "team_stints": [],
        "games_played": 0,
        "starts": 0 if starts_available else None,
        "minutes_per_appearance": None,
        "points_per_appearance": None,
        "field_goals_attempted_per_appearance": None,
        "assists_per_appearance": None,
        "rebounds_per_appearance": None,
        "sample": {
            "appearance_game_count": 0,
            "stat_denominator": "official PlayerGameLogs returned no valid appearance rows",
            "start_denominator": "official LeagueDashPlayerStats Starters request succeeded with no matching row"
            if starts_available
            else "unavailable because starters request failed",
        },
        "missingness": reason,
    }


def select_team_window(
    team_rows: list[dict[str, Any]],
    *,
    cutoff_date: date = CUTOFF_DATE,
    requested: int = 10,
) -> list[dict[str, Any]]:
    filtered = []
    for row in team_rows:
        game_date = parse_date(row.get("GAME_DATE"))
        game_id = str(row.get("Game_ID") or row.get("GAME_ID") or "")
        if game_date is None or game_date >= cutoff_date or game_id == GAME_ID:
            continue
        filtered.append({**row, "_parsed_date": game_date, "_game_id": game_id})
    filtered.sort(key=lambda r: (r["_parsed_date"], r["_game_id"]), reverse=True)
    return filtered[:requested]


def _boxscore_players(payload: dict[str, Any] | None, team_id: int, player_id: int) -> dict[str, Any] | None:
    if not payload:
        return None
    box = payload.get("boxScoreTraditional") or {}
    for side in ("homeTeam", "awayTeam"):
        team = box.get(side) or {}
        try:
            if int(team.get("teamId")) != int(team_id):
                continue
        except (TypeError, ValueError):
            continue
        for player in team.get("players") or []:
            try:
                if int(player.get("personId")) == int(player_id):
                    return player
            except (TypeError, ValueError):
                continue
    return None


def _index_player_rows(rows: list[dict[str, Any]], cutoff_date: date = CUTOFF_DATE) -> dict[tuple[str, int], dict[str, Any]]:
    indexed: dict[tuple[str, int], dict[str, Any]] = {}
    for row in cutoff_player_rows(rows, season=str(rows[0].get("SEASON_YEAR") or "") if rows else CURRENT_SEASON, cutoff_date=cutoff_date):
        try:
            key = (str(row.get("GAME_ID")), int(row.get("TEAM_ID")))
        except (TypeError, ValueError):
            continue
        indexed[key] = row
    return indexed


def recent_window_for_player(
    *,
    player_id: int,
    report_team_id: int,
    team_window: list[dict[str, Any]],
    all_team_games: list[dict[str, Any]],
    current_player_rows: list[dict[str, Any]],
    boxscore_payloads: dict[str, dict[str, Any] | None],
    boxscore_status: dict[str, str],
    current_logs_available: bool,
    team_logs_available: bool,
) -> dict[str, Any]:
    current_rows = cutoff_player_rows(current_player_rows, season=CURRENT_SEASON)
    report_team_rows = [
        row for row in current_rows if str(row.get("TEAM_ID")) == str(report_team_id)
    ]
    any_dates = [parse_date(row.get("GAME_DATE")) for row in current_rows if row_is_appearance(row)]
    team_dates = [parse_date(row.get("GAME_DATE")) for row in report_team_rows if row_is_appearance(row)]
    log_index: dict[str, dict[str, Any]] = {}
    for row in current_rows:
        if str(row.get("TEAM_ID")) == str(report_team_id):
            log_index[str(row.get("GAME_ID"))] = row

    detail: list[dict[str, Any]] = []
    for game in team_window:
        game_id = game["_game_id"]
        log_row = log_index.get(game_id) if current_logs_available else None
        box_status = boxscore_status.get(game_id, "missing")
        box_available = box_status in {"success", "cached_no_request"}
        box_player = _boxscore_players(boxscore_payloads.get(game_id), report_team_id, player_id)
        log_present = log_row is not None
        if log_present and row_is_appearance(log_row):
            appeared: bool | None = True
            appearance_source = "NBA Stats PlayerGameLogs"
        elif log_present:
            appeared = False
            appearance_source = "NBA Stats PlayerGameLogs row without valid minutes"
        elif current_logs_available:
            appeared = False
            appearance_source = "no NBA Stats PlayerGameLogs row for this team game"
        else:
            appeared = None
            appearance_source = "PlayerGameLogs request failed"

        box_minutes = None
        box_comment = None
        starter = None
        starter_source_value = None
        if box_player is not None:
            box_comment = box_player.get("comment") or ""
            stats = box_player.get("statistics") or {}
            box_minutes = stats.get("minutes")
            starter_source_value = box_player.get("position")
            starter = bool(str(starter_source_value or "").strip())
            if appeared is False and parse_minutes(box_minutes) is not None:
                appeared = True
                appearance_source = "NBA Stats BoxScoreTraditionalV3 minutes; PlayerGameLogs discrepancy"

        missingness = None
        if appeared is False:
            if box_player is None and box_available:
                missingness = "no_player_boxscore_row; could be not rostered or feed missing; absence cause unknown"
            elif box_player is not None and box_comment:
                missingness = f"boxscore_comment={box_comment}; absence cause not inferred as injury"
            else:
                missingness = "no_appearance_record; absence cause unknown"
        elif appeared is None:
            missingness = "appearance_unknown_because_player_log_request_failed"

        detail.append(
            {
                "game_id": game_id,
                "game_date": str(game["_parsed_date"]),
                "team_id": report_team_id,
                "matchup": game.get("MATCHUP"),
                "player_game_log_record": "present" if log_present else ("unavailable" if not current_logs_available else "absent"),
                "appearance": appeared,
                "appearance_source": appearance_source,
                "minutes": log_row.get("MIN_SEC") if log_row else box_minutes,
                "minutes_decimal": parse_minutes(log_row.get("MIN") if log_row else box_minutes),
                "boxscore_player_row": "present" if box_player is not None else ("unavailable" if not box_available else "absent"),
                "boxscore_comment": box_comment,
                "starter_record": starter,
                "starter_source": "NBA Stats BoxScoreTraditionalV3 position field" if box_player is not None else None,
                "starter_source_value": starter_source_value,
                "missingness": missingness,
            }
        )

    appearance_rows = [row for row in detail if row["appearance"] is True]
    minutes_values = [row["minutes_decimal"] for row in appearance_rows if row["minutes_decimal"] is not None]
    consecutive_window = 0
    window_censored = False
    for row in detail:
        if row["appearance"] is True:
            break
        if row["appearance"] is None:
            window_censored = True
            break
        consecutive_window += 1
    if window_censored:
        consecutive_window_value: int | None = None
    else:
        consecutive_window_value = consecutive_window

    all_games = sorted(
        [row for row in all_team_games if row.get("_parsed_date") is not None],
        key=lambda r: (r["_parsed_date"], r["_game_id"]),
        reverse=True,
    )
    consecutive_all = 0
    all_censored = False
    if current_logs_available and team_logs_available:
        for game in all_games:
            if str(game["_game_id"]) in log_index:
                break
            consecutive_all += 1
    else:
        consecutive_all = None
        all_censored = True

    last_team = max(team_dates) if team_dates else None
    last_any = max(any_dates) if any_dates else None
    team_mismatch = bool(current_rows and not report_team_rows)
    return {
        "team_id": report_team_id,
        "team_window_size_requested": 10,
        "team_window_games_returned": len(team_window),
        "games": detail,
        "appearance_count_in_recent_team_window": len(appearance_rows),
        "minutes_average_when_appeared": round(sum(minutes_values) / len(minutes_values), 3) if minutes_values else None,
        "last_appearance_date_for_report_team": str(last_team) if last_team else None,
        "last_appearance_date_any_team_current_season": str(last_any) if last_any else None,
        "consecutive_report_team_games_without_appearance_record": consecutive_all,
        "consecutive_count_scope": "all report-team games before cutoff returned by TeamGameLog",
        "consecutive_count_censored_by_available_schedule": all_censored or (consecutive_all == len(all_games) if all_games else False),
        "consecutive_no_appearance_in_recent_window": consecutive_window_value,
        "recent_window_count_censored_at_window_size": (consecutive_window_value == len(detail) if detail else False),
        "team_mismatch_note": "current-season player logs contain rows only for another team; trade or team assignment requires separate roster evidence"
        if team_mismatch
        else None,
        "missingness": (
            "TeamGameLog request failed"
            if not team_logs_available
            else ("PlayerGameLogs request failed" if not current_logs_available else None)
        ),
    }


def _team_row_for_report(team_id: str, team_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in team_rows if str(row.get("Team_ID")) == str(team_id)]


def build_evidence(
    *,
    injury_records: list[dict[str, Any]],
    common_payload: dict[str, Any] | None,
    team_payloads: dict[int, dict[str, Any] | None],
    current_starters_payload: dict[str, Any] | None,
    previous_starters_payload: dict[str, Any] | None,
    player_payloads: dict[tuple[int, str], dict[str, Any] | None],
    raw_status: dict[str, str],
    boxscore_payloads: dict[str, dict[str, Any] | None],
    boxscore_status: dict[str, str],
    injury_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    common_rows = extract_rows(common_payload or {}, "CommonAllPlayers")
    resolved = resolve_identity(injury_records, common_rows)
    current_start_rows = extract_rows(current_starters_payload or {}, "LeagueDashPlayerStats")
    previous_start_rows = extract_rows(previous_starters_payload or {}, "LeagueDashPlayerStats")
    current_starts = build_start_map(current_start_rows)
    previous_starts = build_start_map(previous_start_rows)
    current_starts_available = raw_status.get("league_dash_starters_current") in {"success", "cached_no_request"}
    previous_starts_available = raw_status.get("league_dash_starters_previous") in {"success", "cached_no_request"}

    team_rows_by_id: dict[int, list[dict[str, Any]]] = {}
    for team_id, payload in team_payloads.items():
        team_rows_by_id[team_id] = extract_rows(payload or {}, "TeamGameLog")

    # Add parsed fields once so the same team-game list drives both recent and
    # all-schedule consecutive counts.
    parsed_team_rows: dict[int, list[dict[str, Any]]] = {}
    for team_id, rows in team_rows_by_id.items():
        parsed_team_rows[team_id] = []
        for row in rows:
            parsed = parse_date(row.get("GAME_DATE"))
            game_id = str(row.get("Game_ID") or row.get("GAME_ID") or "")
            if parsed is None or parsed >= CUTOFF_DATE or not game_id or game_id == GAME_ID:
                continue
            parsed_team_rows[team_id].append({**row, "_parsed_date": parsed, "_game_id": game_id})

    players: list[dict[str, Any]] = []
    for record in resolved:
        player_id = record.get("player_id")
        try:
            player_id_int = int(player_id)
            report_team_id = int(record["team_id"])
        except (TypeError, ValueError):
            player_id_int = None
            report_team_id = int(record["team_id"])

        current_payload = player_payloads.get((player_id_int, CURRENT_SEASON)) if player_id_int else None
        previous_payload = player_payloads.get((player_id_int, PREVIOUS_SEASON)) if player_id_int else None
        current_rows = cutoff_player_rows(
            extract_rows(current_payload or {}, "PlayerGameLogs"), season=CURRENT_SEASON
        )
        previous_rows = cutoff_player_rows(
            extract_rows(previous_payload or {}, "PlayerGameLogs"),
            season=PREVIOUS_SEASON,
            cutoff_date=date.max,
            exclude_game_id="",
        )
        current_available = raw_status.get(f"player_{player_id_int}_{CURRENT_SEASON}") in {
            "success",
            "cached_no_request",
        }
        previous_available = raw_status.get(f"player_{player_id_int}_{PREVIOUS_SEASON}") in {
            "success",
            "cached_no_request",
        }
        if current_rows:
            current_summary = {
                "season": CURRENT_SEASON,
                "date_scope": {
                    "api_date_to_inclusive": "2026-01-12",
                    "strict_local_cutoff_exclusive": CUTOFF_LOCAL,
                    "strict_game_date_rule": "GAME_DATE < 2026-01-13 and GAME_ID != 0022500564",
                },
                "team_stints": aggregate_team_stints(
                    current_rows,
                    season=CURRENT_SEASON,
                    starts=current_starts,
                    starts_available=current_starts_available,
                ),
                "missingness": None if current_available else "PlayerGameLogs request failed",
            }
        elif current_available:
            current_summary = {
                "season": CURRENT_SEASON,
                "date_scope": {
                    "api_date_to_inclusive": "2026-01-12",
                    "strict_local_cutoff_exclusive": CUTOFF_LOCAL,
                    "strict_game_date_rule": "GAME_DATE < 2026-01-13 and GAME_ID != 0022500564",
                },
                **empty_season_summary(CURRENT_SEASON, "no NBA PlayerGameLogs appearance rows returned; this does not identify the reason", current_starts_available),
            }
        else:
            current_summary = {
                "season": CURRENT_SEASON,
                "date_scope": {
                    "api_date_to_inclusive": "2026-01-12",
                    "strict_local_cutoff_exclusive": CUTOFF_LOCAL,
                },
                **empty_season_summary(CURRENT_SEASON, "PlayerGameLogs request failed", current_starts_available),
            }

        if previous_rows:
            previous_summary = {
                "season": PREVIOUS_SEASON,
                "date_scope": {
                    "regular_season_scope": "entire 2024-25 Regular Season",
                    "strict_game_date_rule": "season field equals 2024-25",
                },
                "team_stints": aggregate_team_stints(
                    previous_rows,
                    season=PREVIOUS_SEASON,
                    starts=previous_starts,
                    starts_available=previous_starts_available,
                ),
                "missingness": None if previous_available else "PlayerGameLogs request failed",
            }
        elif previous_available:
            previous_summary = {
                "season": PREVIOUS_SEASON,
                "date_scope": {"regular_season_scope": "entire 2024-25 Regular Season"},
                **empty_season_summary(PREVIOUS_SEASON, "no NBA PlayerGameLogs appearance rows returned", previous_starts_available),
            }
        else:
            previous_summary = {
                "season": PREVIOUS_SEASON,
                "date_scope": {"regular_season_scope": "entire 2024-25 Regular Season"},
                **empty_season_summary(PREVIOUS_SEASON, "PlayerGameLogs request failed", previous_starts_available),
            }

        current_summary["source"] = {
            "player_game_logs": f"raw/player_game_logs_{player_id_int}_{CURRENT_SEASON}.json" if player_id_int else None,
            "starter_aggregate": "raw/league_dash_player_stats_starters_2025-26_to_2026-01-12.json",
        }
        current_summary["sample_summary"] = {
            "appearance_game_count": sum(row.get("games_played", 0) for row in current_summary.get("team_stints", [])),
            "team_stint_count": len(current_summary.get("team_stints", [])),
        }
        previous_summary["source"] = {
            "player_game_logs": f"raw/player_game_logs_{player_id_int}_{PREVIOUS_SEASON}.json" if player_id_int else None,
            "starter_aggregate": "raw/league_dash_player_stats_starters_2024-25.json",
        }
        previous_summary["sample_summary"] = {
            "appearance_game_count": sum(row.get("games_played", 0) for row in previous_summary.get("team_stints", [])),
            "team_stint_count": len(previous_summary.get("team_stints", [])),
        }
        for summary in (current_summary, previous_summary):
            stints = summary.get("team_stints", [])
            summary["games_played"] = sum(row.get("games_played", 0) for row in stints)
            starts_values = [row.get("starts") for row in stints]
            summary["starts"] = sum(value for value in starts_values) if starts_values and all(value is not None for value in starts_values) else (0 if not stints and summary.get("starts") == 0 else None)

        team_games = parsed_team_rows.get(report_team_id, [])
        window = select_team_window(team_games)
        recent = (
            recent_window_for_player(
                player_id=player_id_int,
                report_team_id=report_team_id,
                team_window=window,
                all_team_games=team_games,
                current_player_rows=current_rows,
                boxscore_payloads=boxscore_payloads,
                boxscore_status=boxscore_status,
                current_logs_available=current_available,
                team_logs_available=raw_status.get(f"team_game_log_{report_team_id}")
                in {"success", "cached_no_request"},
            )
            if player_id_int is not None
            else {
                "team_id": report_team_id,
                "team_window_size_requested": 10,
                "team_window_games_returned": len(window),
                "games": [],
                "missingness": "player identity unresolved",
            }
        )
        recent["source"] = {
            "team_game_log": f"raw/team_game_log_{report_team_id}_2025-26_to_2026-01-12.json",
            "boxscore_template": "raw/boxscoretraditionalv3_{game_id}.json",
            "player_game_log": f"raw/player_game_logs_{player_id_int}_{CURRENT_SEASON}.json" if player_id_int else None,
        }
        players.append(
            {
                "player_name": record["player_name"],
                "player_id": player_id_int,
                "report_team": {
                    "team_id": report_team_id,
                    "team_name": record["team_name"],
                    "team_tricode": "PHX" if report_team_id == 1610612756 else "MIA",
                },
                "official_availability": {
                    "status": record["availability_status"],
                    "availability_group": record["availability_group"],
                    "reason": record["reason"],
                    "reason_classification": record["reason_classification"],
                    "source_record_id": record["source_record_id"],
                    "source_url": (injury_metadata or {}).get("source_url"),
                    "source_report_time_local": (injury_metadata or {}).get("source_report_time_local"),
                },
                "identity": {
                    "match_status": record["identity_match_status"],
                    "source": "NBA Stats CommonAllPlayers",
                    "source_fields": record["identity_source_fields"],
                },
                "current_season": current_summary,
                "previous_season": previous_summary,
                "recent_team_window": recent,
                "interpretation_boundary": "本条只提供官方出场、首发记录和统计样本，不据此推断主力、替补、位置、轮换地位或伤病原因。",
            }
        )

    success_statuses = {"success", "cached_no_request"}
    all_requests_successful = all(status in success_statuses for status in raw_status.values())
    identity_complete = all(p["identity"]["match_status"] == "exact_unique" for p in players)
    return {
        "artifact_type": "courtvision_player_role_evidence",
        "schema_version": "1.0",
        "status": "complete" if identity_complete and all_requests_successful else "partial",
        "game_id": GAME_ID,
        "matchup": {"away_team_id": 1610612756, "away_team_tricode": "PHX", "home_team_id": 1610612748, "home_team_tricode": "MIA"},
        "cutoff": {
            "source_local": CUTOFF_LOCAL,
            "source_timezone": "America/New_York",
            "utc": CUTOFF_UTC.isoformat().replace("+00:00", "Z"),
            "target_game_excluded": GAME_ID,
            "boundary_rule": "all new NBA game statistics must be strictly before the local cutoff",
        },
        "historical_acquisition": {
            "captured_at_utc": now_utc(),
            "capture_relation_to_cutoff": "later_acquired_historical_data",
            "no_claim_saved_at_cutoff": True,
        },
        "injury_report": {
            "source_url": (injury_metadata or {}).get("source_url"),
            "source_report_time_local": (injury_metadata or {}).get("source_report_time_local"),
            "source_published_at_utc": (injury_metadata or {}).get("source_published_at_utc"),
            "captured_at_utc": (injury_metadata or {}).get("captured_at_utc"),
            "capture_relation_to_cutoff": (injury_metadata or {}).get("capture_relation_to_cutoff"),
        },
        "source_boundary": {
            "official_injury_report": str(DEFAULT_INJURY_PATH),
            "official_stats_provider": "NBA Stats",
            "client": {"package": "nba_api", "version": "1.10.2", "license": "MIT", "github": NBA_API_GITHUB, "pypi": NBA_API_PYPI},
            "nba_terms": NBA_TERMS,
        },
        "players": players,
        "limitations": [
            "NBA Stats regular-season logs do not establish G League or other non-NBA appearances for players marked G League or Two-Way.",
            "A missing player-game row is retained as an unknown absence cause; it is not labeled injury.",
            "The latest CommonAllPlayers team field is an identity lookup captured later and does not override the injury report team at the cutoff.",
            "Starter records are preserved as feed fields and are not translated into roster-role claims.",
        ],
    }


def compact_stint_text(stints: list[dict[str, Any]]) -> str:
    if not stints:
        return "无官方 NBA 出场行（GP0）"
    parts = []
    for row in stints:
        gs = "未知" if row.get("starts") is None else str(row.get("starts"))
        parts.append(
            f"{row.get('team_tricode') or '未知球队'} GP{row.get('games_played')} GS{gs} "
            f"分钟{row.get('minutes_per_appearance')} 得分{row.get('points_per_appearance')} "
            f"FGA{row.get('field_goals_attempted_per_appearance')} AST{row.get('assists_per_appearance')} "
            f"REB{row.get('rebounds_per_appearance')}"
        )
    return "; ".join(parts)


def build_markdown(evidence: dict[str, Any], output_dir: Path) -> str:
    lines = [
        "# 0022500564 球员可用性与赛前出场证据",
        "",
        "本文件是后来获取的历史数据回放，不能证明 CourtVision 在赛前已经保存这些统计。赛前截止时间为 `2026-01-13 19:00 ET`，即 `2026-01-14 00:00 UTC`。目标比赛 `0022500564` 被从所有新统计中排除。",
        "",
        "## 汇总表",
        "",
        "| 球员 | 报告球队 | 官方状态 | 官方原因 | 原因分类 | 2025-26 截止前分段 | 2024-25 分段 | 最近10场出场数 | 出场时平均分钟 | 最近一次出场 | 当前球队连续无出场记录 | 缺失说明 |",
        "|---|---|---|---|---|---|---|---:|---:|---|---:|---|",
    ]
    for player in evidence["players"]:
        availability = player["official_availability"]
        current = player["current_season"]
        previous = player["previous_season"]
        recent = player["recent_team_window"]
        missing = recent.get("missingness") or current.get("missingness") or previous.get("missingness") or "无"
        consecutive = recent.get("consecutive_report_team_games_without_appearance_record")
        if consecutive is None:
            consecutive_text = "未知"
        else:
            consecutive_text = str(consecutive)
            if recent.get("consecutive_count_censored_by_available_schedule"):
                consecutive_text += "（窗口下限）"
        lines.append(
            "| {name} | {team} | {status} | {reason} | {classification} | {current} | {previous} | {count} | {minutes} | {last} | {consecutive} | {missing} |".format(
                name=player["player_name"],
                team=player["report_team"]["team_tricode"],
                status=availability["status"],
                reason=availability["reason"],
                classification=availability["reason_classification"]["primary"],
                current=compact_stint_text(current.get("team_stints", [])),
                previous=compact_stint_text(previous.get("team_stints", [])),
                count=recent.get("appearance_count_in_recent_team_window", "未知"),
                minutes=recent.get("minutes_average_when_appeared", "未知"),
                last=recent.get("last_appearance_date_for_report_team") or "无记录",
                consecutive=consecutive_text,
                missing=missing,
            )
        )
    lines.extend(
        [
            "",
            "## 口径和证据边界",
            "",
            "* `Out` 只表示报告时确定无法出场，`Questionable` 表示出赛状态不确定，`Available` 表示报告时可出场。官方原因原样保留，并另列确定性原因分类。",
            "* 当前赛季统计的统计分母是官方 `PlayerGameLogs` 中有有效分钟字段的比赛行。场均分钟、得分、FGA、助攻和篮板均为出场时平均值，不是球队最近十场的窗口平均。",
            "* 最近十场严格按报告球队在截止时间前的球队比赛窗口取得。没有球员行、DNP 注释或箱分请求缺失分别保留，任何一种都没有被改写成伤病。",
            "* 首发记录来自 `BoxScoreTraditionalV3` 的官方字段。这里只保存是否存在首发记录及原始字段，不把它解释成主力、位置或轮换地位。",
            "* 转队分段按官方逐场日志中的 `TEAM_ID`、球队简称和球队名分别聚合。CommonAllPlayers 的最新球队字段只用于身份核对，不能覆盖报告时球队。",
            "",
            "## 最近十场逐场记录",
            "",
        ]
    )
    for player in evidence["players"]:
        recent = player["recent_team_window"]
        lines.extend(
            [
                f"### {player['player_name']}（{player['report_team']['team_tricode']}，{player['official_availability']['status']}）",
                "",
                "| 比赛日期 | 比赛 ID | 球员日志 | 是否有出场记录 | 分钟 | 首发记录 | 箱分行 | 缺失或边界说明 |",
                "|---|---|---|---|---:|---|---|---|",
            ]
        )
        for game in recent.get("games", []):
            lines.append(
                "| {date} | {game_id} | {log} | {appearance} | {minutes} | {starter} | {box} | {missing} |".format(
                    date=game.get("game_date"),
                    game_id=game.get("game_id"),
                    log=game.get("player_game_log_record"),
                    appearance=game.get("appearance"),
                    minutes=game.get("minutes") or "",
                    starter=game.get("starter_record") if game.get("starter_record") is not None else "未知",
                    box=game.get("boxscore_player_row"),
                    missing=game.get("missingness") or "无",
                )
            )
        lines.extend(
            [
                "",
                f"最近十场出场数：`{recent.get('appearance_count_in_recent_team_window')}`；出场时平均分钟：`{recent.get('minutes_average_when_appeared')}`；报告球队最近一次出场：`{recent.get('last_appearance_date_for_report_team') or '无记录'}`；报告球队连续无出场记录：`{recent.get('consecutive_report_team_games_without_appearance_record')}`。",
                "",
            ]
        )
    lines.extend(
        [
            "## 可支持和仍缺失的判断",
            "",
            "当前赛季和上一季的球队分段、出场数、首发记录、出场时分钟及基础进攻篮板指标，可以支持对赛前使用记录的事实描述。最近十场窗口可以说明球员是否近期出现过以及出现时的分钟范围。",
            "",
            "这些数据仍不能单独支持主力、替补、位置、轮换地位或具体比赛影响判断。G League、Two-Way、Not With Team 等原因的 Out 记录只确认报告时无法出场，NBA 日志没有证明其非 NBA 比赛状态。",
            "",
            "原始响应和哈希位于同目录的 `raw/`，来源清单为 `source_manifest.json`。",
            "",
        ]
    )
    return "\n".join(lines)


def collect(
    *,
    injury_path: Path = DEFAULT_INJURY_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    timeout: int = 20,
    sleep_seconds: float = 0.75,
    reuse_raw_dir: Path | None = None,
) -> dict[str, Any]:
    if any(
        (output_dir / name).exists()
        for name in ("player_evidence.json", "player_evidence.md", "source_manifest.json")
    ):
        raise FileExistsError(f"formal output already exists; choose a new output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    if reuse_raw_dir is not None:
        for source in sorted(reuse_raw_dir.glob("*.json")):
            target = raw_dir / source.name
            if not target.exists():
                shutil.copy2(source, target)

    from nba_api.stats.endpoints import (
        boxscoretraditionalv3,
        commonallplayers,
        leaguedashplayerstats,
        playergamelogs,
        teamgamelog,
    )

    injury_payload = load_json(injury_path)
    records = injury_payload.get("records") or []
    if len(records) != 10:
        raise ValueError(f"expected 10 injury-report records, got {len(records)}")
    access_log: list[dict[str, Any]] = []
    raw_status: dict[str, str] = {}

    def store(label: str, endpoint: str, parameters: dict[str, Any], path: Path, call: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        payload, result = request_and_store(
            path=path,
            label=label,
            endpoint=endpoint,
            parameters=parameters,
            call=call,
            sleep_seconds=sleep_seconds,
        )
        access_log.append(result)
        raw_status[label] = result["status"]
        print(json.dumps({"label": label, "status": result["status"], "elapsed_seconds": result.get("elapsed_seconds")}, ensure_ascii=False), flush=True)
        return payload

    common_payload = store(
        "common_all_players",
        "commonallplayers",
        {"IsOnlyCurrentSeason": 0, "LeagueID": "00", "Season": CURRENT_SEASON},
        raw_dir / "common_all_players_2025-26.json",
        lambda: commonallplayers.CommonAllPlayers(is_only_current_season=0, league_id="00", season=CURRENT_SEASON, timeout=timeout).get_dict(),
    )

    team_ids = sorted({int(record["team_id"]) for record in records})
    team_payloads: dict[int, dict[str, Any] | None] = {}
    for team_id in team_ids:
        team_payloads[team_id] = store(
            f"team_game_log_{team_id}",
            "teamgamelog",
            {"TeamID": team_id, "Season": CURRENT_SEASON, "SeasonType": "Regular Season", "DateTo": "01/12/2026"},
            raw_dir / f"team_game_log_{team_id}_2025-26_to_2026-01-12.json",
            lambda team_id=team_id: teamgamelog.TeamGameLog(team_id=team_id, season=CURRENT_SEASON, season_type_all_star="Regular Season", date_to_nullable="01/12/2026", timeout=timeout).get_dict(),
        )

    current_starters_payload = store(
        "league_dash_starters_current",
        "leaguedashplayerstats",
        {"Season": CURRENT_SEASON, "SeasonType": "Regular Season", "DateTo": "01/12/2026", "MeasureType": "Base", "PerMode": "Totals", "StarterBench": "Starters"},
        raw_dir / "league_dash_player_stats_starters_2025-26_to_2026-01-12.json",
        lambda: leaguedashplayerstats.LeagueDashPlayerStats(season=CURRENT_SEASON, season_type_all_star="Regular Season", date_to_nullable="01/12/2026", measure_type_detailed_defense="Base", per_mode_detailed="Totals", starter_bench_nullable="Starters", timeout=timeout).get_dict(),
    )
    previous_starters_payload = store(
        "league_dash_starters_previous",
        "leaguedashplayerstats",
        {"Season": PREVIOUS_SEASON, "SeasonType": "Regular Season", "MeasureType": "Base", "PerMode": "Totals", "StarterBench": "Starters"},
        raw_dir / "league_dash_player_stats_starters_2024-25.json",
        lambda: leaguedashplayerstats.LeagueDashPlayerStats(season=PREVIOUS_SEASON, season_type_all_star="Regular Season", measure_type_detailed_defense="Base", per_mode_detailed="Totals", starter_bench_nullable="Starters", timeout=timeout).get_dict(),
    )

    common_rows = extract_rows(common_payload or {}, "CommonAllPlayers")
    resolved = resolve_identity(records, common_rows)
    player_payloads: dict[tuple[int, str], dict[str, Any] | None] = {}
    for record in resolved:
        player_id = record.get("player_id")
        if player_id is None:
            continue
        for season in (CURRENT_SEASON, PREVIOUS_SEASON):
            date_to = "01/12/2026" if season == CURRENT_SEASON else ""
            parameters = {
                "PlayerID": player_id,
                "Season": season,
                "SeasonType": "Regular Season",
                "DateTo": date_to,
                "MeasureType": "Base",
                "PerMode": "Totals",
            }
            player_payloads[(int(player_id), season)] = store(
                f"player_{player_id}_{season}",
                "playergamelogs",
                parameters,
                raw_dir / f"player_game_logs_{player_id}_{season}.json",
                lambda player_id=player_id, season=season, date_to=date_to: playergamelogs.PlayerGameLogs(player_id_nullable=str(player_id), season_nullable=season, season_type_nullable="Regular Season", date_to_nullable=date_to, measure_type_player_game_logs_nullable="Base", per_mode_simple_nullable="Totals", timeout=timeout).get_dict(),
            )

    parsed_team_rows: dict[int, list[dict[str, Any]]] = {}
    for team_id, payload in team_payloads.items():
        rows = extract_rows(payload or {}, "TeamGameLog")
        parsed_team_rows[team_id] = []
        for row in rows:
            game_date = parse_date(row.get("GAME_DATE"))
            game_id = str(row.get("Game_ID") or row.get("GAME_ID") or "")
            if game_date is not None and game_date < CUTOFF_DATE and game_id and game_id != GAME_ID:
                parsed_team_rows[team_id].append({**row, "_parsed_date": game_date, "_game_id": game_id})
    recent_game_ids = sorted({game["_game_id"] for rows in parsed_team_rows.values() for game in select_team_window(rows)})
    boxscore_payloads: dict[str, dict[str, Any] | None] = {}
    boxscore_status: dict[str, str] = {}
    for game_id in recent_game_ids:
        payload = store(
            f"boxscore_{game_id}",
            "boxscoretraditionalv3",
            {"GameID": game_id},
            raw_dir / f"boxscoretraditionalv3_{game_id}.json",
            lambda game_id=game_id: boxscoretraditionalv3.BoxScoreTraditionalV3(game_id=game_id, timeout=timeout).get_dict(),
        )
        boxscore_payloads[game_id] = payload
        boxscore_status[game_id] = raw_status.get(f"boxscore_{game_id}", "failed")

    evidence = build_evidence(
        injury_records=records,
        common_payload=common_payload,
        team_payloads=team_payloads,
        current_starters_payload=current_starters_payload,
        previous_starters_payload=previous_starters_payload,
        player_payloads=player_payloads,
        raw_status=raw_status,
        boxscore_payloads=boxscore_payloads,
        boxscore_status=boxscore_status,
        injury_metadata=injury_payload,
    )
    evidence_path = output_dir / "player_evidence.json"
    markdown_path = output_dir / "player_evidence.md"
    if not write_json_once(evidence_path, evidence):
        raise FileExistsError(f"protected existing output: {evidence_path}")
    if not write_json_once(markdown_path, build_markdown(evidence, output_dir)):
        raise FileExistsError(f"protected existing output: {markdown_path}")

    manifest = {
        "artifact_type": "courtvision_player_role_evidence_source_manifest",
        "schema_version": "1.0",
        "game_id": GAME_ID,
        "cutoff_time_local": CUTOFF_LOCAL,
        "cutoff_time_utc": CUTOFF_UTC.isoformat().replace("+00:00", "Z"),
        "captured_at_utc": now_utc(),
        "capture_relation_to_cutoff": "later_acquired_historical_data",
        "official_injury_report_path": str(injury_path),
        "client": {"package": "nba_api", "version": "1.10.2", "license": "MIT", "github": NBA_API_GITHUB, "pypi": NBA_API_PYPI},
        "official_data_provider": "NBA Stats",
        "nba_terms": NBA_TERMS,
        "requests": access_log,
        "request_count": len(access_log),
        "success_count": sum(item["status"] in {"success", "cached_no_request"} for item in access_log),
        "failure_count": sum(item["status"] == "failed" for item in access_log),
        "credentials_saved": False,
        "formal_outputs": ["player_evidence.json", "player_evidence.md"],
        "raw_directory": "raw",
        "reused_raw_directory": str(reuse_raw_dir) if reuse_raw_dir is not None else None,
    }
    manifest["formal_output_sha256"] = {
        "player_evidence.json": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "player_evidence.md": hashlib.sha256(markdown_path.read_bytes()).hexdigest(),
    }
    write_json_once(output_dir / "source_manifest.json", manifest)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--injury-path", type=Path, default=DEFAULT_INJURY_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--sleep-seconds", type=float, default=0.75)
    parser.add_argument("--reuse-raw-dir", type=Path, default=None)
    args = parser.parse_args()
    evidence = collect(
        injury_path=args.injury_path,
        output_dir=args.output_dir,
        timeout=args.timeout,
        sleep_seconds=args.sleep_seconds,
        reuse_raw_dir=args.reuse_raw_dir,
    )
    print(
        json.dumps(
            {
                "status": evidence["status"],
                "game_id": evidence["game_id"],
                "players": len(evidence["players"]),
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
