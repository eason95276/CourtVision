"""Source-bound V1 player game facts and compact pregame knowledge."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .player_boxscore_normalizer import normalize_team_player_boxscores


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = ROOT / "v2" / "data" / "player_knowledge.sqlite"
SCHEMA_PATH = ROOT / "v2" / "sql" / "04_player_knowledge_schema.sql"
DATA_VERSION = "player-knowledge-v1"
PROVIDER = "NBA Stats"
ENDPOINT = "boxscoretraditionalv3"
SOURCE_URL = "https://stats.nba.com/stats/boxscoretraditionalv3"
NO_PREVIOUS_SEASON_RECORD = "no_previous_season_record"
HISTORY_UNAVAILABLE_DUE_TO_COVERAGE = "history_unavailable_due_to_league_coverage"
HISTORY_UNAVAILABLE_DUE_TO_IDENTITY = "history_unavailable_due_to_identity_ambiguity"

_STAT_FIELDS: dict[str, tuple[str, type]] = {
    "points": ("points", int),
    "fgm": ("fieldGoalsMade", int),
    "fga": ("fieldGoalsAttempted", int),
    "fg_pct": ("fieldGoalsPercentage", float),
    "three_pm": ("threePointersMade", int),
    "three_pa": ("threePointersAttempted", int),
    "three_pct": ("threePointersPercentage", float),
    "ftm": ("freeThrowsMade", int),
    "fta": ("freeThrowsAttempted", int),
    "ft_pct": ("freeThrowsPercentage", float),
    "offensive_rebounds": ("reboundsOffensive", int),
    "defensive_rebounds": ("reboundsDefensive", int),
    "rebounds": ("reboundsTotal", int),
    "assists": ("assists", int),
    "turnovers": ("turnovers", int),
    "steals": ("steals", int),
    "blocks": ("blocks", int),
    "fouls": ("foulsPersonal", int),
    "plus_minus": ("plusMinusPoints", float),
}
_COUNT_FIELDS = tuple(key for key, (_, value_type) in _STAT_FIELDS.items() if value_type is int)
_EXPLICIT_NONAPPEARANCE = re.compile(r"^\s*(?:DNP|DND|NWT)\b", re.IGNORECASE)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_iso(value: str | datetime | None) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("collection_time_must_include_timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _minutes_seconds(value: Any) -> tuple[int | None, str | None]:
    """Parse the source clock while preserving its original string separately."""
    if value is None or value == "":
        return None, None
    text = str(value).strip()
    if text.startswith("PT"):
        match = re.fullmatch(r"PT(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", text)
        if not match:
            return None, "invalid_minutes_format"
        minutes = int(match.group(1) or 0)
        seconds = float(match.group(2) or 0)
        total = round(minutes * 60 + seconds)
        return (total, "non_integer_second_rounded" if seconds % 1 else None)
    parts = text.split(":")
    if len(parts) != 2:
        return None, "invalid_minutes_format"
    try:
        minutes = int(parts[0])
        seconds = int(parts[1])
    except ValueError:
        return None, "invalid_minutes_format"
    if minutes < 0 or seconds < 0:
        return None, "negative_minutes"
    warnings = "seconds_component_normalized" if seconds >= 60 else None
    return minutes * 60 + seconds, warnings


def _number(value: Any, value_type: type) -> int | float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if value_type is int:
        if not number.is_integer():
            return None
        return int(number)
    return number


def normalize_boxscore_payload(
    game: dict[str, Any], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Convert one official BoxScoreTraditionalV3 response to V1 player facts."""
    box = payload.get("boxScoreTraditional") or {}
    response_game_id = str(box.get("gameId") or "")
    game_id = str(game["game_id"])
    if response_game_id != game_id:
        raise ValueError("boxscore_game_id_mismatch")

    expected_teams = {
        int(game["home_team_id"]): ("home", int(game["away_team_id"])),
        int(game["away_team_id"]): ("away", int(game["home_team_id"])),
    }
    teams = {side: box.get(side) or {} for side in ("homeTeam", "awayTeam")}
    returned_ids = {int(team["teamId"]) for team in teams.values() if team.get("teamId") is not None}
    if returned_ids != set(expected_teams):
        raise ValueError("boxscore_team_identity_mismatch")

    rows: list[dict[str, Any]] = []
    seen_player_ids: set[str] = set()
    for side_key in ("homeTeam", "awayTeam"):
        source_team = teams[side_key]
        team_id = int(source_team["teamId"])
        venue, opponent_team_id = expected_teams[team_id]
        for normalized in normalize_team_player_boxscores(game_id, source_team):
            player_id = str(normalized.get("person_id") or "").strip() or None
            if player_id is not None:
                if player_id in seen_player_ids:
                    raise ValueError("duplicate_player_id_in_game_boxscore")
                seen_player_ids.add(player_id)

            comment = str(normalized.get("comment") or "").strip()
            raw_minutes = normalized.get("minutes")
            seconds, minute_warning = _minutes_seconds(raw_minutes)
            source_stats = {
                db_key: _number(normalized.get(source_key), value_type)
                for db_key, (source_key, value_type) in _STAT_FIELDS.items()
            }
            positive_boxscore_evidence = any(
                source_stats.get(field) not in (None, 0)
                for field in ("points", "fgm", "fga", "three_pm", "three_pa", "ftm", "fta",
                              "offensive_rebounds", "defensive_rebounds", "rebounds", "assists",
                              "turnovers", "steals", "blocks", "fouls")
            )
            if _EXPLICIT_NONAPPEARANCE.search(comment):
                participation = "did_not_play_confirmed"
            elif seconds is not None:
                # Preserve the project's established definition: a boxscore row
                # with valid minutes, including 0:00, is an observed appearance.
                participation = "played"
            elif positive_boxscore_evidence:
                participation = "played"
            elif raw_minutes not in (None, ""):
                participation = "data_missing"
            else:
                participation = "unknown"

            position = normalized.get("position")
            position_text = str(position).strip() if position is not None else ""
            if participation == "did_not_play_confirmed":
                starter = None
                starter_source = "not_applicable_nonappearance"
            elif position_text:
                starter = 1
                starter_source = "BoxScoreTraditionalV3.position"
            elif participation == "played":
                starter = 0
                starter_source = "BoxScoreTraditionalV3.position"
            else:
                starter = None
                starter_source = "unknown"

            stats: dict[str, int | float | None] = {}
            missing_fields: list[str] = []
            field_status: dict[str, str] = {
                "minutes_seconds": (
                    "not_applicable" if participation == "did_not_play_confirmed"
                    else ("present" if seconds is not None else "source_missing")
                )
            }
            for db_key, (source_key, value_type) in _STAT_FIELDS.items():
                if participation == "did_not_play_confirmed":
                    stats[db_key] = None
                    field_status[db_key] = "not_applicable"
                elif participation == "unknown":
                    stats[db_key] = None
                    field_status[db_key] = "unknown_participation"
                    missing_fields.append(db_key)
                elif participation == "data_missing":
                    stats[db_key] = None
                    field_status[db_key] = "source_missing"
                    missing_fields.append(db_key)
                else:
                    parsed = source_stats[db_key]
                    stats[db_key] = parsed
                    field_status[db_key] = "present" if parsed is not None else "source_missing"
                    if parsed is None:
                        missing_fields.append(db_key)

            if participation == "played" and seconds is None:
                missing_fields.append("minutes_seconds")
            elif participation == "unknown":
                missing_fields.append("minutes_seconds")
                field_status["minutes_seconds"] = "unknown_participation"

            if participation == "did_not_play_confirmed":
                missing_status = "not_applicable"
            elif participation == "unknown":
                missing_status = "unknown"
            elif participation == "data_missing":
                missing_status = "data_missing"
            elif missing_fields:
                missing_status = "partial_fields"
            else:
                missing_status = "none"

            warnings = [minute_warning] if minute_warning else []
            if not player_id:
                warnings.append("player_id_missing_identity_unresolved")
                missing_status = "data_missing"

            rows.append({
                "game_id": game_id,
                "season": game["season"],
                "game_date": game["game_date"],
                "team_id": team_id,
                "team_tricode": str(source_team.get("teamTricode") or ""),
                "opponent_team_id": opponent_team_id,
                "opponent_tricode": str(teams["awayTeam" if side_key == "homeTeam" else "homeTeam"].get("teamTricode") or ""),
                "home_away": venue,
                "player_id": player_id,
                "player_name": normalized.get("full_name") or None,
                "position": position,
                "jersey_num": normalized.get("jersey_num"),
                "participation_status": participation,
                "starter": starter,
                "starter_source": starter_source,
                "minutes_raw": None if raw_minutes in (None, "") else str(raw_minutes),
                "minutes_seconds": seconds,
                **stats,
                "source_comment": comment or None,
                "missing_status": missing_status,
                "missing_fields_json": _json(missing_fields),
                "field_status_json": _json(field_status),
                "normalization_warnings_json": _json(warnings),
            })
    return rows


def read_games_csv(path: str | Path, season: str = "2025-26") -> list[dict[str, Any]]:
    """Return the expected regular-season game index for the requested season."""
    import csv

    start = int(season.split("-", 1)[0])
    start_date = f"{start}-07-01"
    end_date = f"{start + 1}-07-01"
    games: dict[str, dict[str, Any]] = {}
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("gameType") != "Regular Season":
                continue
            raw_date = row.get("gameDateTimeEst") or row.get("gameDate") or ""
            game_date = str(raw_date)[:10]
            if not start_date <= game_date < end_date:
                continue
            game_id = str(row.get("gameId") or "").strip()
            if not game_id:
                raise ValueError("schedule_game_id_missing")
            if game_id.isdigit() and len(game_id) < 10:
                game_id = game_id.zfill(10)
            game = {
                "game_id": game_id,
                "season": season,
                "season_type": "Regular Season",
                "game_date": game_date,
                "home_team_id": int(row["hometeamId"]),
                "away_team_id": int(row["awayteamId"]),
            }
            prior = games.get(game_id)
            if prior is not None and prior != game:
                raise ValueError("duplicate_schedule_game_conflict")
            games[game_id] = game
    return sorted(games.values(), key=lambda game: (game["game_date"], game["game_id"]))


def _minutes_precision_metadata(rows: list[dict[str, Any]]) -> dict[str, Any]:
    precisions: set[str] = set()
    for row in rows:
        try:
            warnings = json.loads(row.get("normalization_warnings_json") or "[]")
        except (TypeError, ValueError):
            warnings = []
        precisions.add(
            "whole_minute_resolution"
            if "source_minutes_whole_minute_precision" in warnings
            else "source_minute_resolution"
        )
    return {
        "status": next(iter(precisions)) if len(precisions) == 1 else ("mixed" if precisions else "unavailable"),
        "observed_minute_rows": len(rows),
        "source_resolutions": sorted(precisions),
        "caveat": (
            "ESPN summary reports whole minutes; converted to seconds only as a minute-level value"
            if "whole_minute_resolution" in precisions else None
        ),
    }


def _aggregate_player_rows(player_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate one player's observed game rows without filling missing values."""
    played = [row for row in rows if row.get("participation_status") == "played"]
    dnp = [row for row in rows if row.get("participation_status") == "did_not_play_confirmed"]
    minute_rows = [row for row in played if row.get("minutes_seconds") is not None]
    starts_known = [row for row in played if row.get("starter") is not None]
    stats: dict[str, Any] = {}
    for field in _STAT_FIELDS:
        values = [row[field] for row in played if row.get(field) is not None]
        stats[field] = {
            "total": sum(values) if values else None,
            "per_appearance": round(sum(values) / len(values), 3) if values else None,
            "observed_games": len(values),
            "missing_games": len(played) - len(values),
        }

    fgms, fgas = stats["fgm"]["total"], stats["fga"]["total"]
    threes_made, threes_attempted = stats["three_pm"]["total"], stats["three_pa"]["total"]
    ftms, ftas = stats["ftm"]["total"], stats["fta"]["total"]
    minutes_total_seconds = sum(int(row["minutes_seconds"]) for row in minute_rows) if minute_rows else None
    minutes_precision = _minutes_precision_metadata(minute_rows)

    teams: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        teams[int(row["team_id"])].append(row)
    stints = []
    for team_id, team_rows in sorted(teams.items(), key=lambda item: min(row["game_date"] for row in item[1])):
        team_played = [row for row in team_rows if row.get("participation_status") == "played"]
        team_dnp = [row for row in team_rows if row.get("participation_status") == "did_not_play_confirmed"]
        team_minutes = [row["minutes_seconds"] for row in team_played if row.get("minutes_seconds") is not None]
        stints.append({
            "team_id": team_id,
            "team_tricode": next((row.get("team_tricode") for row in team_rows if row.get("team_tricode")), None),
            "first_observed_game_date": min(row["game_date"] for row in team_rows),
            "last_observed_game_date": max(row["game_date"] for row in team_rows),
            "games_played": len({row["game_id"] for row in team_played}),
            "confirmed_dnp_games": len({row["game_id"] for row in team_dnp}),
            "minutes_total_seconds": sum(int(value) for value in team_minutes) if team_minutes else None,
        })

    return {
        "player_id": str(player_id),
        "player_id_namespace": next((str(row.get("source_provider")) for row in rows if row.get("source_provider")), None),
        "player_name": next((row.get("player_name") for row in rows if row.get("player_name")), None),
        "source_providers": sorted({str(row["source_provider"]) for row in rows if row.get("source_provider")}),
        "starter_sources": sorted({str(row["starter_source"]) for row in rows if row.get("starter_source")}),
        "games_played": len({row["game_id"] for row in played}),
        "confirmed_dnp_games": len({row["game_id"] for row in dnp}),
        "minutes_observed_games": len(minute_rows),
        "minutes_total_seconds": minutes_total_seconds,
        "minutes_total": round(minutes_total_seconds / 60, 3) if minutes_total_seconds is not None else None,
        "minutes_per_appearance": round(minutes_total_seconds / 60 / len(minute_rows), 3) if minute_rows else None,
        "minutes_precision": minutes_precision,
        "starts": sum(int(row["starter"] == 1) for row in starts_known) if starts_known else None,
        "starter_observed_games": len(starts_known),
        "starter_rate": round(sum(int(row["starter"] == 1) for row in starts_known) / len(starts_known), 4) if starts_known else None,
        "statistics": stats,
        "shooting_percentages": {
            "field_goal": round(fgms / fgas, 4) if fgms is not None and fgas else None,
            "three_point": round(threes_made / threes_attempted, 4) if threes_made is not None and threes_attempted else None,
            "free_throw": round(ftms / ftas, 4) if ftms is not None and ftas else None,
        },
        "team_stints": stints,
        "dnp_comments": sorted({row["source_comment"] for row in dnp if row.get("source_comment")}),
    }


def _compact_player_profile(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not profile:
        return None
    keys = (
        "player_id", "player_id_namespace", "player_name", "team_id", "team_tricode", "season",
        "source_providers", "starter_sources",
        "games_played", "confirmed_dnp_games", "minutes_observed_games",
        "minutes_total_seconds", "minutes_total", "minutes_per_appearance",
        "minutes_precision",
        "starts", "starter_observed_games", "starter_rate", "team_minutes_share",
        "team_minutes_denominator_seconds", "statistics", "shooting_percentages",
        "team_ranks", "team_stints", "observed_team_stints", "dnp_comments",
    )
    return {key: profile[key] for key in keys if key in profile}


def _has_observed_player_stats(profile: dict[str, Any] | None) -> bool:
    if not profile:
        return False
    if int(profile.get("minutes_observed_games") or 0) > 0:
        return True
    return any(
        int((stat or {}).get("observed_games") or 0) > 0
        for stat in (profile.get("statistics") or {}).values()
    )


def _normalized_identity_name(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain).split())


def _factual_role_evidence(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Emit measured role proxies, never an unsupported player grade."""
    if not profile:
        return []
    evidence = []
    minutes_rank = (profile.get("team_ranks") or {}).get("minutes_rank")
    rank_denominators = (profile.get("team_ranks") or {}).get("rank_denominators") or {}
    rank_denominator = rank_denominators.get("minutes_rank")
    if minutes_rank is not None:
        evidence.append({
            "measure": "team_minutes_volume",
            "team_rank": minutes_rank,
            "rank_denominator": rank_denominator,
            "minutes_total": profile.get("minutes_total"),
            "minutes_per_appearance": profile.get("minutes_per_appearance"),
            "minutes_precision": profile.get("minutes_precision"),
            "games_played": profile.get("games_played"),
            "statement": (
                f"上季该队出场 {profile.get('games_played')} 场、累计 {profile.get('minutes_total')} 分钟，"
                f"按分钟总量列队内第 {minutes_rank}/{rank_denominator}。"
            ),
        })
    if profile.get("starter_observed_games"):
        provider_label = ", ".join(profile.get("source_providers") or []) or "比赛数据"
        evidence.append({
            "measure": "starter_record",
            "starts": profile.get("starts"),
            "observed_games": profile.get("starter_observed_games"),
            "sample_games": profile.get("games_played"),
            "statement": (
                f"上季 {provider_label} 首发字段显示 {profile.get('starts')} 次首发，"
                f"有效首发状态样本 {profile.get('starter_observed_games')} 场。"
            ),
        })
    for metric, label, rank_key in (
        ("points", "points_per_game", "points_rank"),
        ("fga", "field_goal_attempts_per_game", "field_goal_attempts_rank"),
        ("three_pa", "three_point_attempts_per_game", "three_point_attempts_rank"),
        ("assists", "assists_per_game", "assists_rank"),
        ("rebounds", "rebounds_per_game", "rebounds_rank"),
        ("turnovers", "turnovers_per_game", "turnovers_rank"),
        ("steals", "steals_per_game", "steals_rank"),
        ("blocks", "blocks_per_game", "blocks_rank"),
    ):
        value = (profile.get("statistics") or {}).get(metric) or {}
        rank = (profile.get("team_ranks") or {}).get(rank_key) if rank_key else None
        rank_denominator = rank_denominators.get(rank_key) if rank_key else None
        if value.get("per_observed_game") is None:
            continue
        evidence.append({
            "measure": label,
            "value": value.get("per_observed_game"),
            "team_rank": rank,
            "rank_denominator": rank_denominator,
            "observed_games": value.get("observed_games"),
        })
    return evidence


class PlayerKnowledgeStore:
    """Explicit SQLite store; construction and reads never create a database."""

    def __init__(self, path: str | Path = DEFAULT_DATABASE):
        self.path = Path(path)

    def active_provider(self) -> str:
        if not self.path.is_file():
            return PROVIDER
        connection = self._connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT value FROM player_knowledge_metadata WHERE key='active_provider'"
            ).fetchone()
            return str(row[0]) if row else PROVIDER
        finally:
            connection.close()

    def set_active_provider(self, provider: str) -> None:
        if not provider.strip():
            raise ValueError("active_provider_missing")
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO player_knowledge_metadata(key, value) VALUES('active_provider', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (provider.strip(),),
                )
        finally:
            connection.close()

    def initialize(self, games: list[dict[str, Any]], season: str = "2025-26") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            with connection:
                existing_version = connection.execute(
                    "SELECT value FROM player_knowledge_metadata WHERE key='schema_version'"
                ).fetchone()
                if existing_version and existing_version[0] != "1.0":
                    raise ValueError("player_knowledge_schema_version_mismatch")
                existing_season = connection.execute(
                    "SELECT value FROM player_knowledge_metadata WHERE key='season'"
                ).fetchone()
                if existing_season and existing_season[0] != season:
                    raise ValueError("player_knowledge_season_mismatch")
                connection.execute(
                    "INSERT OR IGNORE INTO player_knowledge_metadata(key, value) VALUES('schema_version', '1.0')"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO player_knowledge_metadata(key, value) VALUES('data_version', ?)"
                    , (DATA_VERSION,)
                )
                connection.execute(
                    "INSERT OR IGNORE INTO player_knowledge_metadata(key, value) VALUES('season', ?)",
                    (season,),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO player_knowledge_metadata(key, value) VALUES('active_provider', ?)",
                    (PROVIDER,),
                )
                for game in games:
                    if game["season"] != season or game["season_type"] != "Regular Season":
                        raise ValueError("schedule_scope_mismatch")
                    current = connection.execute(
                        "SELECT season, season_type, game_date, home_team_id, away_team_id "
                        "FROM player_knowledge_games WHERE game_id=?", (game["game_id"],)
                    ).fetchone()
                    expected = (season, "Regular Season", game["game_date"], game["home_team_id"], game["away_team_id"])
                    if current is not None and tuple(current) != expected:
                        raise ValueError("existing_schedule_identity_conflict")
                    connection.execute(
                        "INSERT OR IGNORE INTO player_knowledge_games "
                        "(game_id, season, season_type, game_date, home_team_id, away_team_id, collection_status) "
                        "VALUES(?, ?, ?, ?, ?, ?, 'not_collected')",
                        (game["game_id"], season, game["season_type"], game["game_date"],
                         game["home_team_id"], game["away_team_id"]),
                    )
        finally:
            connection.close()

    def _connect(self, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError("player_knowledge_database_unavailable")
            uri = self.path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
        else:
            if not self.path.is_file():
                raise FileNotFoundError("player_knowledge_database_not_initialized")
            connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def ingest_boxscore(
        self,
        game: dict[str, Any],
        payload: dict[str, Any],
        *,
        normalized_rows: list[dict[str, Any]] | None = None,
        collection_time_utc: str | datetime | None,
        source_path: str | Path | None = None,
        response_sha256: str | None = None,
        response_bytes: int | None = None,
        provider: str = PROVIDER,
        endpoint: str = ENDPOINT,
        source_url: str = SOURCE_URL,
        client_version: str | None = "nba_api 1.10.2",
        request_parameters: dict[str, Any] | None = None,
        data_version: str = DATA_VERSION,
    ) -> dict[str, Any]:
        captured_at = _utc_iso(collection_time_utc)
        if response_sha256 is None:
            response_sha256 = _sha256_json(payload)
        rows = normalized_rows if normalized_rows is not None else normalize_boxscore_payload(game, payload)
        request_parameters = request_parameters or {
            "game_id": str(game["game_id"]), "season": game["season"],
            "season_type": game["season_type"],
        }
        connection = self._connect()
        try:
            with connection:
                schedule = connection.execute(
                    "SELECT season, season_type, game_date, home_team_id, away_team_id, collection_status "
                    "FROM player_knowledge_games WHERE game_id=?", (str(game["game_id"]),)
                ).fetchone()
                if schedule is None:
                    raise ValueError("game_not_in_expected_schedule")
                identity = (game["season"], game["season_type"], game["game_date"],
                            int(game["home_team_id"]), int(game["away_team_id"]))
                if tuple(schedule[:5]) != identity:
                    raise ValueError("player_knowledge_game_identity_mismatch")
                old_receipt = connection.execute(
                    "SELECT receipt_id FROM player_knowledge_source_receipts "
                    "WHERE game_id=? AND response_sha256=? AND status='success' AND provider=? AND data_version=? "
                    "ORDER BY receipt_id DESC LIMIT 1",
                    (str(game["game_id"]), response_sha256, provider, data_version),
                ).fetchone()
                if old_receipt is not None:
                    connection.execute(
                        "UPDATE player_knowledge_games SET collection_status='success', latest_success_receipt_id=? "
                        "WHERE game_id=?", (old_receipt[0], str(game["game_id"]))
                    )
                    return {"status": "cached_success", "receipt_id": old_receipt[0],
                            "player_rows": connection.execute(
                                "SELECT COUNT(*) FROM player_game_facts WHERE receipt_id=?", (old_receipt[0],)
                            ).fetchone()[0]}

                cursor = connection.execute(
                    "INSERT INTO player_knowledge_source_receipts "
                    "(game_id, provider, endpoint, source_url, client_version, data_version, collection_time_utc, status, "
                    "request_parameters_json, response_sha256, source_path, response_bytes) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, 'success', ?, ?, ?, ?)",
                    (str(game["game_id"]), provider, endpoint, source_url, client_version, data_version, captured_at,
                     _json(request_parameters), response_sha256,
                     str(source_path) if source_path is not None else None, response_bytes),
                )
                receipt_id = int(cursor.lastrowid)
                if rows:
                    keys = list(rows[0].keys())
                    columns = ["receipt_id", "source_provider", "source_endpoint", "source_url", "collection_time_utc",
                               "data_version", *keys]
                    sql = ("INSERT INTO player_game_facts (" + ",".join(columns) + ") VALUES ("
                           + ",".join("?" for _ in columns) + ")")
                    values = []
                    for row in rows:
                        if int(row["team_id"]) not in {int(game["home_team_id"]), int(game["away_team_id"])}:
                            raise ValueError("player_boxscore_team_identity_mismatch")
                        values.append((receipt_id, provider, endpoint, source_url, captured_at, data_version,
                                       *(row[key] for key in keys)))
                    connection.executemany(sql, values)
                connection.execute(
                    "UPDATE player_knowledge_games SET collection_status='success', latest_success_receipt_id=? "
                    "WHERE game_id=?", (receipt_id, str(game["game_id"]))
                )
                return {"status": "success", "receipt_id": receipt_id, "player_rows": len(rows),
                        "response_sha256": response_sha256, "collection_time_utc": captured_at}
        finally:
            connection.close()

    def record_failure(
        self,
        game_id: str,
        *,
        error_type: str,
        error_message: str,
        collection_time_utc: str | datetime,
        request_parameters: dict[str, Any] | None = None,
        source_url: str = SOURCE_URL,
        provider: str = PROVIDER,
        endpoint: str = ENDPOINT,
        client_version: str = "nba_api 1.10.2",
        data_version: str = DATA_VERSION,
    ) -> int:
        connection = self._connect()
        try:
            with connection:
                if connection.execute("SELECT 1 FROM player_knowledge_games WHERE game_id=?", (game_id,)).fetchone() is None:
                    raise ValueError("game_not_in_expected_schedule")
                cursor = connection.execute(
                    "INSERT INTO player_knowledge_source_receipts "
                    "(game_id, provider, endpoint, source_url, client_version, data_version, collection_time_utc, status, "
                    "request_parameters_json, error_type, error_message) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?)",
                    (game_id, provider, endpoint, source_url, client_version, data_version,
                     _utc_iso(collection_time_utc), _json(request_parameters or {"game_id": game_id}),
                     error_type, error_message[:1000]),
                )
                connection.execute(
                    "UPDATE player_knowledge_games SET collection_status='failed' "
                    "WHERE game_id=? AND latest_success_receipt_id IS NULL", (game_id,)
                )
                return int(cursor.lastrowid)
        finally:
            connection.close()

    def successful_game_ids(self, season: str = "2025-26", provider: str | None = None) -> set[str]:
        if not self.path.is_file():
            return set()
        provider = provider or self.active_provider()
        connection = self._connect(read_only=True)
        try:
            return {
                str(row[0]) for row in connection.execute(
                    "SELECT DISTINCT r.game_id FROM player_knowledge_source_receipts r "
                    "JOIN player_knowledge_games g ON g.game_id=r.game_id "
                    "WHERE g.season=? AND r.status='success' AND r.provider=?", (season, provider)
                )
            }
        finally:
            connection.close()

    def coverage(
        self, season: str = "2025-26", cutoff: str | datetime | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        if not self.path.is_file():
            return {"status": "unavailable", "reason": "player_knowledge_database_unavailable", "season": season}
        provider = provider or self.active_provider()
        cutoff_utc = _utc_iso(cutoff) if cutoff is not None else None
        cutoff_date = None
        if cutoff_utc is not None:
            cutoff_date = datetime.fromisoformat(cutoff_utc.replace("Z", "+00:00")).astimezone(
                ZoneInfo("America/New_York")
            ).date().isoformat()
        connection = self._connect(read_only=True)
        try:
            expected_rows = connection.execute(
                "SELECT game_id, game_date, home_team_id, away_team_id "
                "FROM player_knowledge_games WHERE season=?"
                + (" AND game_date<?" if cutoff_date is not None else ""),
                [season] + ([cutoff_date] if cutoff_date is not None else []),
            ).fetchall()
            expected_ids = {str(row["game_id"]) for row in expected_rows}
            eligible_receipts = self._latest_success_receipts(connection, season, cutoff_utc, provider)
            collected_ids = set(eligible_receipts) & expected_ids
            failed_requests = int(connection.execute(
                "SELECT COUNT(*) FROM player_knowledge_source_receipts r "
                "JOIN player_knowledge_games g ON g.game_id=r.game_id "
                "WHERE g.season=? AND r.status='failed' AND r.provider=?"
                + (" AND r.collection_time_utc IS NOT NULL AND r.collection_time_utc<=? AND g.game_date<?" if cutoff_utc else ""),
                [season, provider] + ([cutoff_utc, cutoff_date] if cutoff_utc else []),
            ).fetchone()[0])
            failed_ids_query = (
                "SELECT DISTINCT r.game_id FROM player_knowledge_source_receipts r "
                "JOIN player_knowledge_games g ON g.game_id=r.game_id "
                "WHERE g.season=? AND r.status='failed' AND r.provider=?"
                + (" AND r.collection_time_utc IS NOT NULL AND r.collection_time_utc<=? AND g.game_date<?" if cutoff_utc else "")
            )
            failed_game_ids = {
                str(row[0]) for row in connection.execute(
                    failed_ids_query,
                    [season, provider] + ([cutoff_utc, cutoff_date] if cutoff_utc else []),
                )
            }
            unresolved_failed_game_ids = sorted((failed_game_ids & expected_ids) - collected_ids)
            source_filters = "WHERE g.season=? AND r.status='success' AND r.provider=?"
            source_args: list[Any] = [season, provider]
            if cutoff_utc is not None:
                source_filters += " AND r.collection_time_utc IS NOT NULL AND r.collection_time_utc<=? AND g.game_date<?"
                source_args.extend([cutoff_utc, cutoff_date])
            source_row = connection.execute(
                "SELECT MIN(r.collection_time_utc), MAX(r.collection_time_utc), "
                "COUNT(DISTINCT r.response_sha256), GROUP_CONCAT(DISTINCT r.provider), "
                "GROUP_CONCAT(DISTINCT r.endpoint), COUNT(DISTINCT r.source_url), MIN(r.source_url), "
                "GROUP_CONCAT(DISTINCT r.client_version), GROUP_CONCAT(DISTINCT r.data_version) "
                "FROM player_knowledge_source_receipts r "
                "JOIN player_knowledge_games g ON g.game_id=r.game_id "
                + source_filters,
                source_args,
            ).fetchone()
            missing_ids = sorted(expected_ids - collected_ids)
            row_time_filter = " AND r.provider=?"
            row_args: list[Any] = [season, provider]
            if cutoff_utc is not None:
                row_time_filter = " AND r.collection_time_utc IS NOT NULL AND r.collection_time_utc<=? AND g.game_date<?"
                row_time_filter += " AND r.provider=?"
                row_args = [season, cutoff_utc, cutoff_date, provider]
            row_query = (
                "WITH ranked AS ("
                " SELECT r.receipt_id, r.game_id, ROW_NUMBER() OVER ("
                "  PARTITION BY r.game_id ORDER BY r.collection_time_utc DESC, r.receipt_id DESC"
                " ) AS row_rank"
                " FROM player_knowledge_source_receipts r"
                " JOIN player_knowledge_games g ON g.game_id=r.game_id"
                " WHERE g.season=? AND r.status='success' "
                + row_time_filter
                + ") SELECT COUNT(*) FROM player_game_facts f JOIN ranked x "
                "ON x.receipt_id=f.receipt_id AND x.game_id=f.game_id WHERE x.row_rank=1"
            )
            row_count = int(connection.execute(row_query, row_args).fetchone()[0])

            team_expected: dict[int, set[str]] = defaultdict(set)
            for row in expected_rows:
                team_expected[int(row["home_team_id"])].add(str(row["game_id"]))
                team_expected[int(row["away_team_id"])].add(str(row["game_id"]))
            team_coverage = {}
            for team_id, ids in sorted(team_expected.items()):
                count = len(ids & collected_ids)
                team_coverage[str(team_id)] = {
                    "expected_games": len(ids),
                    "collected_games": count,
                    "coverage_ratio": count / len(ids) if ids else None,
                    "complete": bool(ids) and count == len(ids),
                }
            complete = bool(expected_ids) and expected_ids <= collected_ids
            return {
                "status": "complete" if complete else "partial",
                "season": season,
                "season_type": "Regular Season",
                "provider": provider,
                "expected_games": len(expected_ids),
                "collected_games": len(expected_ids & collected_ids),
                "failed_requests": failed_requests,
                "failed_request_attempts": failed_requests,
                "failed_games": len(unresolved_failed_game_ids),
                "failed_game_ids_sample": unresolved_failed_game_ids[:20],
                "not_collected_games": len(missing_ids),
                "not_collected_game_ids_sample": missing_ids[:20],
                "player_game_rows": row_count,
                "coverage_ratio": len(expected_ids & collected_ids) / len(expected_ids) if expected_ids else None,
                "team_coverage": team_coverage,
                "source": {
                    "collection_time_min": source_row[0],
                    "collection_time_max": source_row[1],
                    "response_hash_count": int(source_row[2] or 0),
                    "providers": sorted((source_row[3] or "").split(",")) if source_row[3] else [],
                    "endpoints": sorted((source_row[4] or "").split(",")) if source_row[4] else [],
                    "source_url_count": int(source_row[5] or 0),
                    "source_url_samples": [source_row[6]] if source_row[6] else [],
                    "client_versions": sorted((source_row[7] or "").split(",")) if source_row[7] else [],
                    "data_versions": sorted((source_row[8] or "").split(",")) if source_row[8] else [],
                    "data_version": DATA_VERSION,
                },
                "cutoff": cutoff_utc,
                "cutoff_local_date_exclusive": cutoff_date,
                "usable_as_complete_season": complete,
            }
        finally:
            connection.close()

    @staticmethod
    def _latest_success_receipts(
        connection: sqlite3.Connection, season: str, cutoff_utc: str | None,
        provider: str | None = None,
    ) -> dict[str, int]:
        query = (
            "SELECT r.receipt_id, r.game_id, r.collection_time_utc "
            "FROM player_knowledge_source_receipts r "
            "JOIN player_knowledge_games g ON g.game_id=r.game_id "
            "WHERE g.season=? AND r.status='success'"
        )
        args: list[Any] = [season]
        if provider is not None:
            query += " AND r.provider=?"
            args.append(provider)
        if cutoff_utc is not None:
            query += " AND r.collection_time_utc IS NOT NULL AND r.collection_time_utc<=?"
            args.append(cutoff_utc)
        query += " ORDER BY r.game_id, r.collection_time_utc DESC, r.receipt_id DESC"
        selected: dict[str, int] = {}
        for row in connection.execute(query, args):
            selected.setdefault(str(row["game_id"]), int(row["receipt_id"]))
        return selected

    def _facts_as_of(
        self, season: str, cutoff: str | datetime, provider: str | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        cutoff_utc = _utc_iso(cutoff)
        assert cutoff_utc is not None
        provider = provider or self.active_provider()
        cutoff_date = datetime.fromisoformat(cutoff_utc.replace("Z", "+00:00")).astimezone(
            ZoneInfo("America/New_York")
        ).date().isoformat()
        connection = self._connect(read_only=True)
        try:
            selected = self._latest_success_receipts(connection, season, cutoff_utc, provider)
            if not selected:
                return [], selected
            rows = connection.execute(
                "WITH ranked AS ("
                " SELECT r.receipt_id, r.game_id, r.collection_time_utc, r.response_sha256, r.source_path, "
                " ROW_NUMBER() OVER (PARTITION BY r.game_id ORDER BY r.collection_time_utc DESC, r.receipt_id DESC) AS row_rank "
                " FROM player_knowledge_source_receipts r "
                " JOIN player_knowledge_games g ON g.game_id=r.game_id "
                " WHERE g.season=? AND r.status='success' AND r.provider=? AND r.collection_time_utc IS NOT NULL "
                " AND r.collection_time_utc<=? AND g.game_date<?"
                ") SELECT f.*, ranked.collection_time_utc AS receipt_collection_time_utc, "
                "ranked.response_sha256, ranked.source_path, g.home_team_id, g.away_team_id "
                "FROM player_game_facts f JOIN ranked ON ranked.receipt_id=f.receipt_id "
                "JOIN player_knowledge_games g ON g.game_id=f.game_id "
                "WHERE ranked.row_rank=1 "
                "ORDER BY f.game_date, f.game_id, f.team_id, f.player_id",
                (season, provider, cutoff_utc, cutoff_date),
            ).fetchall()
            return [dict(row) for row in rows], selected
        finally:
            connection.close()

    def player_team_history(
        self, player_id: str | int, season: str = "2025-26", cutoff: str | datetime | None = None
    ) -> dict[str, Any]:
        if cutoff is None:
            cutoff = datetime.now(timezone.utc)
        facts, _ = self._facts_as_of(season, cutoff)
        pid = str(player_id)
        player_rows = [row for row in facts if row.get("player_id") is not None and str(row["player_id"]) == pid]
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in player_rows:
            grouped[int(row["team_id"])].append(row)
        stints = []
        for team_id, rows in sorted(grouped.items(), key=lambda item: min(x["game_date"] for x in item[1])):
            played = [row for row in rows if row["participation_status"] == "played"]
            minutes = [row["minutes_seconds"] for row in played if row["minutes_seconds"] is not None]
            stints.append({
                "player_id": pid,
                "season": season,
                "team_id": team_id,
                "team_tricode": next((row["team_tricode"] for row in rows if row.get("team_tricode")), None),
                "first_observed_game_date": min(row["game_date"] for row in rows),
                "last_observed_game_date": max(row["game_date"] for row in rows),
                "observed_games": len({row["game_id"] for row in played}),
                "confirmed_dnp_games": len({row["game_id"] for row in rows if row["participation_status"] == "did_not_play_confirmed"}),
                "minutes_total_seconds": sum(minutes) if minutes else None,
                "history_semantics": "team observed in boxscore; dates are appearance evidence, not transaction effective dates",
            })
        return {
            "status": "available" if stints else "unavailable",
            "player_id": pid,
            "season": season,
            "stints": stints,
            "source_receipts": len({row["receipt_id"] for row in player_rows}),
            "reason": None if stints else "no_eligible_player_game_rows",
        }

    def team_player_summaries(
        self, team_id: int, season: str = "2025-26", cutoff: str | datetime | None = None
    ) -> dict[str, Any]:
        if cutoff is None:
            cutoff = datetime.now(timezone.utc)
        facts, _ = self._facts_as_of(season, cutoff)
        team_rows = [row for row in facts if int(row["team_id"]) == int(team_id)]
        played_rows = [row for row in team_rows if row["participation_status"] == "played"]
        dnp_rows = [row for row in team_rows if row["participation_status"] == "did_not_play_confirmed"]
        team_minutes = sum(int(row["minutes_seconds"]) for row in played_rows if row.get("minutes_seconds") is not None)
        per_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in team_rows:
            if row.get("player_id") is not None:
                per_player[str(row["player_id"])].append(row)

        profiles: list[dict[str, Any]] = []
        for player_id, rows in per_player.items():
            appearances = [row for row in rows if row["participation_status"] == "played"]
            if not appearances:
                continue
            minutes_values = [int(row["minutes_seconds"]) for row in appearances if row.get("minutes_seconds") is not None]
            minutes_precision = _minutes_precision_metadata(
                [row for row in appearances if row.get("minutes_seconds") is not None]
            )
            starts_known = [row for row in appearances if row.get("starter") is not None]
            starts = sum(int(row["starter"] == 1) for row in starts_known)
            field_totals: dict[str, Any] = {}
            for field in _STAT_FIELDS:
                values = [row[field] for row in appearances if row.get(field) is not None]
                field_totals[field] = {
                    "total": sum(values) if values else None,
                    "per_observed_game": round(sum(values) / len(values), 3) if values else None,
                    "observed_games": len(values),
                    "missing_games": len(appearances) - len(values),
                }
            fgms, fgas = field_totals["fgm"]["total"], field_totals["fga"]["total"]
            threes_made, threes_attempted = field_totals["three_pm"]["total"], field_totals["three_pa"]["total"]
            ftms, ftas = field_totals["ftm"]["total"], field_totals["fta"]["total"]
            dnp = [row for row in rows if row["participation_status"] == "did_not_play_confirmed"]
            player_name = next((row.get("player_name") for row in rows if row.get("player_name")), None)
            profile = {
                "player_id": player_id,
                "player_id_namespace": next((str(row.get("source_provider")) for row in rows if row.get("source_provider")), None),
                "player_name": player_name,
                "source_providers": sorted({str(row["source_provider"]) for row in rows if row.get("source_provider")}),
                "starter_sources": sorted({str(row["starter_source"]) for row in rows if row.get("starter_source")}),
                "team_id": int(team_id),
                "season": season,
                "games_played": len({row["game_id"] for row in appearances}),
                "confirmed_dnp_games": len({row["game_id"] for row in dnp}),
                "minutes_total_seconds": sum(minutes_values) if minutes_values else None,
                "minutes_total": round(sum(minutes_values) / 60, 3) if minutes_values else None,
                "minutes_per_appearance": round(sum(minutes_values) / 60 / len(minutes_values), 3) if minutes_values else None,
                "minutes_precision": minutes_precision,
                "starts": starts if starts_known else None,
                "starter_observed_games": len(starts_known),
                "starter_rate": round(starts / len(starts_known), 4) if starts_known else None,
                "team_minutes_share": round(sum(minutes_values) / team_minutes, 6) if team_minutes else None,
                "team_minutes_denominator_seconds": team_minutes if team_minutes else None,
                "statistics": field_totals,
                "shooting_percentages": {
                    "field_goal": round(fgms / fgas, 4) if fgms is not None and fgas else None,
                    "three_point": round(threes_made / threes_attempted, 4) if threes_made is not None and threes_attempted else None,
                    "free_throw": round(ftms / ftas, 4) if ftms is not None and ftas else None,
                },
                "sample_game_ids": sorted({row["game_id"] for row in appearances}),
                "observed_team_stints": [{
                    "team_id": int(team_id),
                    "first_game_date": min(row["game_date"] for row in rows),
                    "last_game_date": max(row["game_date"] for row in rows),
                    "history_semantics": "observed appearance/boxscore dates only",
                }],
                "dnp_comments": sorted({row["source_comment"] for row in dnp if row.get("source_comment")}),
            }
            profiles.append(profile)

        profiles.sort(key=lambda row: (
            -(row["minutes_total_seconds"] if row.get("minutes_total_seconds") is not None else -1),
            row["player_id"],
        ))
        for rank_field, metric in (
            ("minutes_rank", "minutes_total_seconds"),
            ("points_rank", "points"),
            ("field_goal_attempts_rank", "fga"),
            ("three_point_attempts_rank", "three_pa"),
            ("assists_rank", "assists"),
            ("rebounds_rank", "rebounds"),
            ("turnovers_rank", "turnovers"),
            ("steals_rank", "steals"),
            ("blocks_rank", "blocks"),
        ):
            sorted_profiles = sorted(
                [row for row in profiles if (row.get("minutes_total_seconds") if metric == "minutes_total_seconds" else (row.get("statistics", {}).get(metric, {}).get("per_observed_game"))) is not None],
                key=lambda row: -(
                    row["minutes_total_seconds"] if metric == "minutes_total_seconds"
                    else row["statistics"][metric]["per_observed_game"]
                ),
            )
            for rank, row in enumerate(sorted_profiles, start=1):
                row.setdefault("team_ranks", {})[rank_field] = rank
                row["team_ranks"].setdefault("rank_denominators", {})[rank_field] = len(sorted_profiles)

        coverage = self.coverage(season, cutoff)
        team_coverage = coverage.get("team_coverage", {}).get(str(team_id), {})
        team_receipt_times = [row["receipt_collection_time_utc"] for row in team_rows
                              if row.get("receipt_collection_time_utc")]
        return {
            "status": "available" if profiles else "unavailable",
            "season": season,
            "team_id": int(team_id),
            "team_coverage": team_coverage,
            "team_minutes_denominator_seconds": team_minutes if team_minutes else None,
            "player_count": len(profiles),
            "profiles": profiles,
            "source_game_receipts": len({row["receipt_id"] for row in team_rows}),
            "collection_time_range": [min(team_receipt_times), max(team_receipt_times)] if team_receipt_times else None,
            "reason": None if profiles else "no_eligible_team_player_rows",
        }

    @staticmethod
    def _target_season_start(game: dict[str, Any], roster_context: dict[str, Any] | None) -> int | None:
        candidates = [game.get("season_start"), (game.get("season_phase") or {}).get("season_start")]
        teams = (roster_context or {}).get("teams") or {}
        candidates.extend(
            ((team.get("cold_start") or {}).get("season_start")) for team in teams.values()
        )
        for candidate in candidates:
            try:
                if candidate is not None:
                    return int(candidate)
            except (TypeError, ValueError):
                continue
        raw_tipoff = game.get("tipoff") or game.get("scheduled_tipoff")
        if raw_tipoff:
            try:
                parsed = datetime.fromisoformat(str(raw_tipoff).replace("Z", "+00:00"))
                return parsed.year if parsed.month >= 7 else parsed.year - 1
            except ValueError:
                return None
        return None

    @staticmethod
    def _roster_players(team: dict[str, Any] | None, cutoff_utc: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
        if not team:
            return None, [], "team_roster_context_missing"
        roster = team.get("official_team_roster") or team.get("roster")
        if not roster or roster.get("status") != "available":
            return roster, [], "official_team_roster_unavailable"
        source = roster.get("source") or {}
        captured_at = source.get("captured_at")
        if not captured_at:
            return roster, [], "roster_capture_time_missing"
        try:
            captured_utc = _utc_iso(captured_at)
        except (TypeError, ValueError):
            return roster, [], "roster_capture_time_invalid"
        if captured_utc is None or captured_utc > cutoff_utc:
            return roster, [], "roster_captured_after_cutoff"
        players = [p for p in roster.get("players") or [] if p.get("player_id") not in (None, "")]
        return roster, players, None

    @staticmethod
    def _replacement_observations(
        team_rows: list[dict[str, Any]], absent_player_id: str, current_player_ids: set[str]
    ) -> list[dict[str, Any]]:
        absent_games = {
            row["game_id"] for row in team_rows
            if str(row.get("player_id")) == absent_player_id
            and row.get("participation_status") == "did_not_play_confirmed"
        }
        if not absent_games:
            return []
        key_played_games = {
            row["game_id"] for row in team_rows
            if str(row.get("player_id")) == absent_player_id
            and row.get("participation_status") == "played"
        }
        teammate_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in team_rows:
            pid = row.get("player_id")
            if pid is None or str(pid) not in current_player_ids or str(pid) == absent_player_id:
                continue
            if row.get("participation_status") != "played" or row.get("minutes_seconds") is None:
                continue
            teammate_rows[str(pid)].append(row)

        results = []
        for candidate_id, rows in teammate_rows.items():
            absence_rows = [row for row in rows if row["game_id"] in absent_games]
            baseline_rows = [row for row in rows if row["game_id"] in key_played_games]
            if not absence_rows:
                continue
            absence_avg = sum(int(row["minutes_seconds"]) for row in absence_rows) / len(absence_rows) / 60
            baseline_avg = (
                sum(int(row["minutes_seconds"]) for row in baseline_rows) / len(baseline_rows) / 60
                if baseline_rows else None
            )
            delta = absence_avg - baseline_avg if baseline_avg is not None else None
            results.append({
                "player_id": candidate_id,
                "player_name": next((row.get("player_name") for row in rows if row.get("player_name")), None),
                "games_with_key_player_dnp": len(absent_games),
                "candidate_appearances_in_dnp_games": len(absence_rows),
                "average_minutes_in_dnp_games": round(absence_avg, 2),
                "average_minutes_when_key_player_played": round(baseline_avg, 2) if baseline_avg is not None else None,
                "minutes_difference": round(delta, 2) if delta is not None else None,
                "sample_game_ids": sorted({row["game_id"] for row in absence_rows})[:5],
                "interpretation": "descriptive historical association; DNP comment is not an inferred injury cause",
            })
        results.sort(key=lambda row: (
            -(row["minutes_difference"] if row["minutes_difference"] is not None else row["average_minutes_in_dnp_games"]),
            row["player_id"],
        ))
        return results[:3]

    def matchup_context(
        self,
        game: dict[str, Any],
        cutoff: str | datetime,
        roster_context: dict[str, Any] | None,
        *,
        availability_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a compact, time-bounded player packet for exactly two teams."""
        cutoff_utc = _utc_iso(cutoff)
        if cutoff_utc is None:
            return {"status": "unavailable", "reason": "player_context_cutoff_missing"}
        target_start = self._target_season_start(game, roster_context)
        if target_start is None:
            return {"status": "unavailable", "reason": "target_season_unavailable"}
        prior_season = f"{target_start - 1}-{str(target_start)[-2:]}"
        coverage = self.coverage(prior_season, cutoff_utc)
        teams_in = (roster_context or {}).get("teams") or {}
        target_ids = set()
        for side in ("home", "away"):
            identity_id = (game.get(side) or {}).get("id") or game.get(f"{side}_team_id")
            identity_id = identity_id or (teams_in.get(side) or {}).get("team_id")
            if identity_id is not None:
                target_ids.add(str(identity_id))
        result: dict[str, Any] = {
            "status": "partial" if coverage.get("status") == "partial" else "available",
            "game_id": game.get("game_id") or game.get("id"),
            "cutoff": cutoff_utc,
            "season": prior_season,
            "data_version": DATA_VERSION,
            "coverage": {**coverage, "team_coverage": {
                key: value for key, value in (coverage.get("team_coverage") or {}).items() if key in target_ids
            }},
            "numeric_model_usage": False,
            "teams": {},
            "limitations": [
                "player boxscore roles are descriptive, not causal player impact estimates",
                "team membership stints are based on observed game boxscores, not transaction effective dates",
                "coverage.status is league-wide; matchup usability is determined independently for each team's history",
            ],
        }
        if coverage.get("status") == "unavailable":
            result["status"] = "unavailable"
            result["reason"] = coverage.get("reason", "player_knowledge_database_unavailable")
            return result
        all_facts, _ = self._facts_as_of(prior_season, cutoff_utc)
        provider = coverage.get("provider") or self.active_provider()
        all_rows_by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
        all_player_ids_by_normalized_name: dict[str, set[str]] = defaultdict(set)
        for fact in all_facts:
            if fact.get("player_id") is not None:
                fact_player_id = str(fact["player_id"])
                all_rows_by_player[fact_player_id].append(fact)
                normalized_name = _normalized_identity_name(fact.get("player_name"))
                if normalized_name:
                    all_player_ids_by_normalized_name[normalized_name].add(fact_player_id)
        for side in ("home", "away"):
            team = teams_in.get(side) or {}
            side_identity = game.get(side) or {}
            team_id_raw = side_identity.get("id") or game.get(f"{side}_team_id") or team.get("team_id")
            try:
                team_id = int(team_id_raw)
            except (TypeError, ValueError):
                result["teams"][side] = {"status": "unavailable", "reason": "team_id_missing"}
                result["status"] = "partial"
                continue

            team_coverage = coverage.get("team_coverage", {}).get(str(team_id), {})
            if not team_coverage.get("complete"):
                result["teams"][side] = {
                    "team_id": team_id,
                    "status": "unavailable",
                    "reason": "team_player_history_incomplete",
                    "coverage": team_coverage,
                }
                result["status"] = "partial"
                continue

            roster, roster_players, roster_reason = self._roster_players(team, cutoff_utc)
            try:
                if any(int(value) != team_id for value in (team.get("team_id"), (roster or {}).get("team_id")) if value is not None):
                    roster_reason = "roster_team_identity_mismatch"
            except (TypeError, ValueError):
                roster_reason = "roster_team_identity_mismatch"
            team_summary = self.team_player_summaries(team_id, prior_season, cutoff_utc)
            if roster_reason:
                # Historical leaders remain explicitly separate from current-roster facts.
                result["teams"][side] = {
                    "team_id": team_id,
                    "status": "roster_unverified",
                    "reason": roster_reason,
                    "coverage": team_coverage,
                    "historical_team_leaders": team_summary.get("profiles", [])[:8],
                    "current_roster_match_count": None,
                    "continuity": {"status": "unavailable", "reason": roster_reason},
                }
                result["status"] = "partial"
                continue

            roster_by_id = {str(player["player_id"]): player for player in roster_players}
            by_player = {str(p["player_id"]): p for p in team_summary.get("profiles", [])}
            source_id_by_roster_id: dict[str, str] = {}
            identity_method_by_roster_id: dict[str, str] = {}
            identity_issue_by_roster_id: dict[str, str] = {}
            for roster_id, roster_player in roster_by_id.items():
                provider_aliases = roster_player.get("player_id_aliases") or {}
                explicit_source_id = provider_aliases.get(provider)
                if (explicit_source_id is None
                        and str(roster_player.get("source_player_id_provider") or "") == provider):
                    explicit_source_id = roster_player.get("source_player_id")
                if explicit_source_id is not None and str(explicit_source_id) in all_rows_by_player:
                    source_id_by_roster_id[roster_id] = str(explicit_source_id)
                    identity_method_by_roster_id[roster_id] = "explicit_provider_id_alias"
                    continue
                if provider == PROVIDER and (roster_id in all_rows_by_player or roster_id in by_player):
                    source_id_by_roster_id[roster_id] = roster_id
                    identity_method_by_roster_id[roster_id] = "same_nba_person_id"
                    continue
                if provider == "ESPN" and coverage.get("status") == "complete":
                    normalized_name = _normalized_identity_name(roster_player.get("name"))
                    candidates = sorted(all_player_ids_by_normalized_name.get(normalized_name, set())) if normalized_name else []
                    if len(candidates) == 1:
                        source_id_by_roster_id[roster_id] = candidates[0]
                        identity_method_by_roster_id[roster_id] = "unique_normalized_full_name_in_complete_league_dataset"
                    elif len(candidates) > 1:
                        identity_issue_by_roster_id[roster_id] = "ambiguous_provider_player_id_crosswalk"
                    else:
                        identity_issue_by_roster_id[roster_id] = "not_found_in_complete_previous_season_dataset"
                    continue
                identity_issue_by_roster_id[roster_id] = (
                    "provider_identity_crosswalk_requires_complete_league_coverage"
                    if provider == "ESPN" else "no_player_id_match"
                )
            current_source_ids = set(source_id_by_roster_id.values())
            roster_id_by_source_id = {
                source_id: roster_id for roster_id, source_id in source_id_by_roster_id.items()
            }
            player_rows = []
            for player_id, roster_player in roster_by_id.items():
                source_player_id = source_id_by_roster_id.get(player_id)
                identity_method = identity_method_by_roster_id.get(player_id)
                identity_issue = identity_issue_by_roster_id.get(player_id)
                current_team_profile = by_player.get(source_player_id) if source_player_id else None
                player_facts = all_rows_by_player.get(source_player_id, []) if source_player_id else []
                all_team_stints = _aggregate_player_rows(source_player_id, player_facts) if player_facts else None
                current_availability = self._availability_for_player(
                    player_id, roster_player, team_id, availability_records or [], cutoff_utc
                )
                prior_stints = all_team_stints.get("team_stints", []) if all_team_stints else []
                previous_total = (
                    dict(all_team_stints)
                    if all_team_stints and coverage.get("status") == "complete"
                    else None
                )
                if previous_total is not None:
                    previous_total.pop("team_stints", None)
                has_player_history = bool(player_facts)
                observed_on_current_team = any(int(row["team_id"]) == team_id for row in player_facts)
                observed_stats_profile = current_team_profile or all_team_stints
                has_observed_stats = _has_observed_player_stats(observed_stats_profile)
                if has_observed_stats and (
                    current_team_profile is not None or coverage.get("status") == "complete"
                ):
                    previous_stats_status = "available"
                elif has_observed_stats:
                    previous_stats_status = "incomplete_coverage"
                else:
                    previous_stats_status = "unavailable"
                if has_player_history:
                    previous_status = "observed_on_current_team" if observed_on_current_team else "observed_for_other_team"
                    history_reason = None
                elif identity_issue == "ambiguous_provider_player_id_crosswalk":
                    previous_status = HISTORY_UNAVAILABLE_DUE_TO_IDENTITY
                    history_reason = identity_issue
                elif identity_issue == "not_found_in_complete_previous_season_dataset":
                    previous_status = NO_PREVIOUS_SEASON_RECORD
                    history_reason = identity_issue
                elif identity_issue == "no_player_id_match":
                    if coverage.get("status") == "complete":
                        previous_status = NO_PREVIOUS_SEASON_RECORD
                        history_reason = "not_found_in_complete_previous_season_dataset"
                    else:
                        previous_status = HISTORY_UNAVAILABLE_DUE_TO_COVERAGE
                        history_reason = "not_observed_in_partial_previous_season_dataset"
                elif identity_issue == "provider_identity_crosswalk_requires_complete_league_coverage":
                    previous_status = HISTORY_UNAVAILABLE_DUE_TO_COVERAGE
                    history_reason = identity_issue
                elif coverage.get("status") == "complete":
                    previous_status = NO_PREVIOUS_SEASON_RECORD
                    history_reason = "not_found_in_complete_previous_season_dataset"
                else:
                    previous_status = HISTORY_UNAVAILABLE_DUE_TO_COVERAGE
                    history_reason = "not_observed_in_partial_previous_season_dataset"
                profile = {
                    "player_id": player_id,
                    "player_id_namespace": "NBA",
                    "source_player_id": source_player_id,
                    "source_player_id_namespace": provider if source_player_id else None,
                    "identity_match_method": identity_method,
                    "identity_resolution_status": "matched" if source_player_id else identity_issue,
                    "player_name": roster_player.get("name") or (current_team_profile or {}).get("player_name"),
                    "team_id": team_id,
                    "position": roster_player.get("position"),
                    "jersey_num": roster_player.get("number"),
                    "current_roster_status": "listed_by_captured_roster",
                    "previous_season_status": previous_status,
                    "previous_season_history_status": (
                        "unavailable" if not has_player_history
                        else ("available" if coverage.get("status") == "complete" else "partial")
                    ),
                    "previous_season_history_reason": history_reason,
                    "previous_season_stats_status": previous_stats_status,
                    "previous_season_total": _compact_player_profile(previous_total),
                    "current_team_last_season_stint": _compact_player_profile(current_team_profile),
                    "last_season_games_played": (previous_total or {}).get("games_played"),
                    "last_season_confirmed_dnp_games": (previous_total or {}).get("confirmed_dnp_games"),
                    "last_season_minutes_total": (previous_total or {}).get("minutes_total"),
                    "role_basis": (
                        "current_team_last_season_stint" if current_team_profile
                        else ("all_previous_team_stints" if previous_total else "unavailable")
                    ),
                    "factual_role_evidence": _factual_role_evidence(current_team_profile or previous_total),
                    "previous_team_stints": prior_stints if coverage.get("status") == "complete" else [],
                    "previous_team_stints_status": (
                        "available" if coverage.get("status") == "complete"
                        else ("partial" if has_player_history else "unavailable")
                    ),
                    "current_availability": current_availability,
                    "replacement_observations": self._replacement_observations(
                        [row for row in all_facts if int(row["team_id"]) == team_id],
                        source_player_id or player_id,
                        current_source_ids,
                    ),
                }
                player_rows.append(profile)

            previous_minutes = [
                {"player_id": roster_id_by_source_id.get(str(profile["player_id"]), str(profile["player_id"])),
                 "name": profile.get("player_name"),
                 "minutes": profile["minutes_total"]}
                for profile in team_summary.get("profiles", [])
                if profile.get("minutes_total") is not None
            ]
            continuity: dict[str, Any]
            if provider == "ESPN" and coverage.get("status") != "complete":
                continuity = {
                    "status": "unavailable",
                    "reason": "provider_identity_crosswalk_requires_complete_league_coverage",
                    "identity_crosswalk_status": "incomplete",
                }
            else:
                try:
                    from .roster_continuity import calculate_continuity

                    minutes = {
                        "status": "available" if team_coverage.get("complete") else "unavailable",
                        "team_id": team_id,
                        "season_start": target_start - 1,
                        "id_namespace": "nba",
                        "season_type": "Regular Season",
                        "per_mode": "Totals",
                        "players": previous_minutes,
                        "coverage": {"valid_minute_rows_ratio": 1.0,
                                     "season_totals_completeness": "computed_from_boxscore_game_rows"},
                    }
                    official_roster = dict(roster)
                    official_roster.setdefault("team_id", team_id)
                    official_roster.setdefault("season_start", target_start)
                    official_roster.setdefault("id_namespace", "nba")
                    continuity = calculate_continuity(official_roster, minutes)
                    continuity["roster_completeness_caveat"] = (
                        (roster.get("coverage") or {}).get("source_roster_completeness")
                        or "not_independently_verified"
                    )
                    unresolved_identity_count = sum(
                        value == "ambiguous_provider_player_id_crosswalk"
                        for value in identity_issue_by_roster_id.values()
                    )
                    if unresolved_identity_count:
                        continuity["status"] = "provisional_identity_crosswalk"
                        continuity["identity_crosswalk_unresolved_players"] = unresolved_identity_count
                        continuity["retained_minutes_ratio_interpretation"] = (
                            "identity-unresolved players are not counted as retained; ratio is a lower bound"
                        )
                    if continuity["roster_completeness_caveat"] != "verified_complete":
                        if unresolved_identity_count:
                            continuity["status"] = "provisional_roster_and_identity_crosswalk"
                        else:
                            continuity["status"] = "provisional_roster_membership"
                        continuity["retained_minutes_ratio_interpretation"] = (
                            "listed-roster lower bound; roster completeness is not independently verified"
                        )
                except Exception as exc:
                    continuity = {"status": "unavailable", "reason": f"continuity_calculation_failed:{type(exc).__name__}"}

            previous_top = sorted(
                team_summary.get("profiles", []),
                key=lambda profile: -(profile["minutes_total_seconds"] if profile.get("minutes_total_seconds") is not None else -1),
            )[:8]
            player_rows.sort(key=lambda profile: (
                -(((profile.get("current_team_last_season_stint") or {}).get("minutes_total_seconds"))
                  if (profile.get("current_team_last_season_stint") or {}).get("minutes_total_seconds") is not None
                  else (((profile.get("previous_season_total") or {}).get("minutes_total_seconds"))
                        if (profile.get("previous_season_total") or {}).get("minutes_total_seconds") is not None
                        else -1)),
                str(profile.get("player_id") or ""),
            ))
            result["teams"][side] = {
                "team_id": team_id,
                "status": "available",
                "coverage": team_coverage,
                "roster_source": {
                    "source_type": (roster.get("source") or {}).get("source_type"),
                    "source_url": (roster.get("source") or {}).get("url"),
                    "captured_at": (roster.get("source") or {}).get("captured_at"),
                    "roster_completeness": (roster.get("coverage") or {}).get("source_roster_completeness"),
                },
                "continuity": continuity,
                "historical_team_leaders": team_summary.get("profiles", [])[:8],
                "previous_season_players": player_rows[:15],
                "current_roster_player_count": len(roster_by_id),
                "current_roster_match_count": len(source_id_by_roster_id),
                "current_roster_identity_unresolved_count": len(roster_by_id) - len(source_id_by_roster_id),
                "players_with_previous_season_stats": sum(
                    p.get("previous_season_stats_status") in {"available", "incomplete_coverage"}
                    for p in player_rows
                ),
                "players_with_complete_previous_season_stats": sum(
                    p.get("previous_season_stats_status") == "available" for p in player_rows
                ),
                "players_without_previous_season_record": sum(
                    p.get("previous_season_status") == NO_PREVIOUS_SEASON_RECORD for p in player_rows
                ),
                "players_with_history_unavailable_due_to_coverage": sum(
                    p.get("previous_season_status") == HISTORY_UNAVAILABLE_DUE_TO_COVERAGE
                    for p in player_rows
                ),
                "players_with_identity_crosswalk_unresolved": sum(
                    p.get("previous_season_status") == HISTORY_UNAVAILABLE_DUE_TO_IDENTITY
                    for p in player_rows
                ),
                "players_with_current_team_stints": sum(p.get("previous_season_status") == "observed_on_current_team" for p in player_rows),
                "unmatched_current_roster_players": [
                    {"player_id": p["player_id"], "player_name": p.get("player_name"),
                     "team_id": p.get("team_id"), "position": p.get("position"),
                     "status": p.get("previous_season_status"),
                     "source_player_id": p.get("source_player_id"),
                     "identity_resolution_status": p.get("identity_resolution_status"),
                     "previous_season_history_status": p.get("previous_season_history_status"),
                     "current_availability": p.get("current_availability")}
                    for p in player_rows if p.get("previous_season_history_status") == "unavailable"
                ],
                "previous_team_top8_minutes": [
                    {"player_id": p["player_id"], "player_id_namespace": provider,
                     "player_name": p.get("player_name"), "minutes_total": p.get("minutes_total"),
                     "currently_listed": str(p["player_id"]) in current_source_ids}
                    for p in previous_top
                ],
                "role_semantics": "factual season statistics and within-team ranks; no automatic player grade",
            }
        matchup_team_status = {
            side: {
                "status": (result["teams"].get(side) or {}).get("status", "unavailable"),
                "coverage": (result["teams"].get(side) or {}).get("coverage", {}),
            }
            for side in ("home", "away")
        }
        result["matchup_coverage"] = {
            "status": "complete" if all(item["status"] == "available" for item in matchup_team_status.values()) else "partial",
            "teams": matchup_team_status,
            "league_wide_status": coverage.get("status"),
        }
        result["status"] = (
            "available" if result["matchup_coverage"]["status"] == "complete" else "partial"
        )
        return result

    @staticmethod
    def _availability_for_player(
        player_id: str, roster_player: dict[str, Any], team_id: int,
        records: list[dict[str, Any]], analysis_cutoff: str,
    ) -> dict[str, Any] | None:
        candidates = []
        target_name = " ".join(str(roster_player.get("name") or "").casefold().split())
        for record in records:
            record_team = record.get("team_id")
            if record_team is not None and str(record_team) != str(team_id):
                continue
            record_id = record.get("player_id") or record.get("person_id")
            if record_id is not None and str(record_id) == player_id:
                candidates.append(record)
                continue
            name = " ".join(str(record.get("player_name") or record.get("player") or "").casefold().split())
            if name and name == target_name:
                candidates.append(record)
        if len(candidates) != 1:
            return None
        record = candidates[0]
        raw_status = record.get("availability_status") or record.get("status")
        published_raw = record.get("source_published_at") or record.get("published_at")
        captured_raw = record.get("captured_at")
        try:
            published_at = _utc_iso(published_raw)
            captured_at = _utc_iso(captured_raw)
        except (TypeError, ValueError):
            published_at = None
            captured_at = None
        cutoff = _utc_iso(analysis_cutoff)
        eligible = bool(
            cutoff and published_at and captured_at
            and published_at <= cutoff and captured_at <= cutoff
        )
        after_cutoff = bool(
            cutoff and ((published_at and published_at > cutoff) or (captured_at and captured_at > cutoff))
        )
        return {
            "status": raw_status if eligible else "unavailable",
            "reported_status": raw_status,
            "reason": record.get("reason"),
            "source_url": record.get("source_url") or record.get("url"),
            "source_published_at": published_at,
            "captured_at": captured_at,
            "source_cutoff": record.get("source_cutoff") or record.get("cutoff_time_utc") or record.get("cutoff_time"),
            "analysis_cutoff": cutoff,
            "time_eligibility": (
                "eligible_as_of_cutoff" if eligible
                else ("after_cutoff" if after_cutoff else "missing_or_invalid_time_provenance")
            ),
            "identity_match": "player_id" if str(record.get("player_id") or record.get("person_id") or "") == player_id else "unique_team_name",
        }
