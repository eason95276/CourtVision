"""Normalize ESPN's structured NBA summary boxscore into Player Knowledge facts."""
from __future__ import annotations

import csv
import json
import math
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any


ESPN_TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams?limit=100"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
ESPN_PROVIDER = "ESPN"
ESPN_SUMMARY_ENDPOINT = "site.api.espn.com/nba/summary"
_REQUIRED_LABELS = ("MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF")


def normalize_identity_name(value: Any) -> str:
    """Accent-insensitive full-name key used only for unique crosswalk candidates."""
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_marks).split())


def _espn_team_records(payload: Any) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            team = value.get("team")
            candidate = team if isinstance(team, dict) else value
            if (candidate.get("id") is not None and candidate.get("name")
                    and candidate.get("abbreviation")):
                found[str(candidate["id"])] = candidate
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return list(found.values())


def build_espn_team_id_map(
    teams_payload: Any,
    games_csv: str | Path,
    season: str = "2025-26",
) -> dict[str, int]:
    """Map ESPN team IDs to NBA team IDs by unique schedule nickname."""
    start = int(season.split("-", 1)[0])
    lower, upper = f"{start}-07-01", f"{start + 1}-07-01"
    nickname_to_nba_ids: dict[str, set[int]] = defaultdict(set)
    with Path(games_csv).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("gameType") != "Regular Season":
                continue
            game_date = str(row.get("gameDateTimeEst") or row.get("gameDate") or "")[:10]
            if not lower <= game_date < upper:
                continue
            for name_key, team_key in (("hometeamName", "hometeamId"), ("awayteamName", "awayteamId")):
                nickname = normalize_identity_name(row.get(name_key))
                if nickname:
                    nickname_to_nba_ids[nickname].add(int(row[team_key]))

    result: dict[str, int] = {}
    mapped_nba: set[int] = set()
    for team in _espn_team_records(teams_payload):
        nickname = normalize_identity_name(team.get("name"))
        nba_ids = nickname_to_nba_ids.get(nickname, set())
        if len(nba_ids) != 1:
            continue
        nba_id = next(iter(nba_ids))
        if nba_id in mapped_nba:
            raise ValueError("espn_team_mapping_not_one_to_one")
        result[str(team["id"])] = nba_id
        mapped_nba.add(nba_id)
    if len(result) != 30 or len(mapped_nba) != 30:
        raise ValueError(f"espn_team_mapping_incomplete:{len(result)}")
    return result


def build_nba_team_nickname_map(games_csv: str | Path, season: str = "2025-26") -> dict[str, int]:
    """Create the 30-team nickname map from the explicit season schedule."""
    start = int(season.split("-", 1)[0])
    lower, upper = f"{start}-07-01", f"{start + 1}-07-01"
    nickname_to_ids: dict[str, set[int]] = defaultdict(set)
    with Path(games_csv).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("gameType") != "Regular Season":
                continue
            game_date = str(row.get("gameDateTimeEst") or row.get("gameDate") or "")[:10]
            if not lower <= game_date < upper:
                continue
            for name_key, team_key in (("hometeamName", "hometeamId"), ("awayteamName", "awayteamId")):
                nickname = normalize_identity_name(row.get(name_key))
                if nickname:
                    nickname_to_ids[nickname].add(int(row[team_key]))
    result = {name: next(iter(team_ids)) for name, team_ids in nickname_to_ids.items() if len(team_ids) == 1}
    if len(result) != 30 or len(set(result.values())) != 30:
        raise ValueError(f"nba_team_nickname_map_incomplete:{len(result)}")
    return result


def map_scoreboard_events_to_nba_games(
    game_date: str,
    nba_games: list[dict[str, Any]],
    scoreboard_payload: dict[str, Any],
    nba_team_nickname_to_id: dict[str, int],
) -> dict[str, dict[str, Any]]:
    """Join one ESPN local-date scoreboard to schedule games by ordered team IDs."""
    expected = {
        (int(game["home_team_id"]), int(game["away_team_id"])): game
        for game in nba_games if str(game["game_date"]) == game_date
    }
    if not expected:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for event in scoreboard_payload.get("events") or []:
        competition = (event.get("competitions") or [{}])[0]
        home_id = away_id = None
        for competitor in competition.get("competitors") or []:
            team = competitor.get("team") or {}
            nickname = normalize_identity_name(team.get("name") or "")
            nba_team_id = nba_team_nickname_to_id.get(nickname)
            if competitor.get("homeAway") == "home":
                home_id = nba_team_id
            elif competitor.get("homeAway") == "away":
                away_id = nba_team_id
        game = expected.get((home_id, away_id))
        if game is None:
            continue
        event_id = str(event.get("id") or "")
        if not event_id:
            raise ValueError("espn_event_id_missing_from_scoreboard")
        nba_game_id = str(game["game_id"])
        status = ((event.get("status") or {}).get("type") or {}).get("description")
        if not status or not str(status).casefold().startswith("final"):
            raise ValueError(f"espn_historical_game_not_final:{nba_game_id}:{status}")
        if nba_game_id in found and found[nba_game_id].get("event_id") != event_id:
            raise ValueError("multiple_espn_events_match_nba_game")
        found[nba_game_id] = {
            "game_id": nba_game_id,
            "event_id": event_id,
            "event_date": event.get("date"),
            "status": status,
            "home_team_id": home_id,
            "away_team_id": away_id,
            "home_name": next((c.get("team", {}).get("displayName") for c in competition.get("competitors", [])
                                if c.get("homeAway") == "home"), None),
            "away_name": next((c.get("team", {}).get("displayName") for c in competition.get("competitors", [])
                                if c.get("homeAway") == "away"), None),
        }
    missing = set(str(game["game_id"]) for game in expected.values()) - set(found)
    if missing:
        raise ValueError(f"espn_scoreboard_missing_expected_games:{','.join(sorted(missing))}")
    return found


def _parse_int(value: Any) -> int | None:
    if value in (None, "", "--", "DNP", "N/A"):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def _parse_split(value: Any) -> tuple[int | None, int | None]:
    if value in (None, "", "--", "DNP", "N/A"):
        return None, None
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(value))
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _parse_display_minutes(value: Any) -> tuple[int | None, str | None, bool]:
    """Convert ESPN whole-minute display to seconds while flagging its resolution."""
    if value in (None, "", "--", "DNP", "N/A"):
        return None, None, False
    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return int(text) * 60, text, True
    match = re.fullmatch(r"(\d+):(\d{1,2})", text)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2)), text, False
    try:
        numeric_minutes = float(text)
    except ValueError:
        return None, text, False
    if not math.isfinite(numeric_minutes) or numeric_minutes < 0:
        return None, text, False
    return round(numeric_minutes * 60), text, not numeric_minutes % 1


def normalize_espn_summary_payload(
    game: dict[str, Any],
    payload: dict[str, Any],
    espn_team_id_to_nba_team_id: dict[str, int] | None = None,
    *,
    expected_event_id: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize one ESPN summary response without collapsing ESPN IDs into NBA IDs."""
    header = payload.get("header") or {}
    event_id = str(header.get("id") or "")
    if expected_event_id is not None and event_id != str(expected_event_id):
        raise ValueError("espn_event_id_mismatch")
    if not event_id:
        raise ValueError("espn_event_id_missing")

    boxscore = payload.get("boxscore") or {}
    groups = boxscore.get("players") or []
    if len(groups) != 2:
        raise ValueError("espn_boxscore_team_group_count_mismatch")
    expected_teams = {int(game["home_team_id"]), int(game["away_team_id"])}
    team_id_map = dict(espn_team_id_to_nba_team_id or {})
    competition = ((header.get("competitions") or [{}])[0])
    for competitor in competition.get("competitors") or []:
        source_team_id = str((competitor.get("team") or {}).get("id") or competitor.get("id") or "")
        if source_team_id not in team_id_map and competitor.get("homeAway") in {"home", "away"}:
            team_id_map[source_team_id] = (
                int(game["home_team_id"]) if competitor["homeAway"] == "home"
                else int(game["away_team_id"])
            )
    mapped_teams = {
        str((group.get("team") or {}).get("id")): team_id_map.get(str((group.get("team") or {}).get("id")))
        for group in groups
    }
    if None in mapped_teams.values() or {int(value) for value in mapped_teams.values()} != expected_teams:
        raise ValueError("espn_boxscore_team_identity_mismatch")

    venue_by_team: dict[int, str] = {}
    for competitor in competition.get("competitors") or []:
        source_team_id = str((competitor.get("team") or {}).get("id") or competitor.get("id") or "")
        nba_team_id = team_id_map.get(source_team_id)
        if nba_team_id is not None and competitor.get("homeAway") in {"home", "away"}:
            venue_by_team[nba_team_id] = str(competitor["homeAway"])
    if venue_by_team and set(venue_by_team) != expected_teams:
        raise ValueError("espn_header_team_identity_mismatch")

    rows: list[dict[str, Any]] = []
    seen_player_ids: set[str] = set()
    for group in groups:
        team = group.get("team") or {}
        source_team_id = str(team.get("id") or "")
        team_id = int(team_id_map[source_team_id])
        opponent_team_id = next(item for item in expected_teams if item != team_id)
        home_away = venue_by_team.get(team_id) or ("home" if team_id == int(game["home_team_id"]) else "away")
        opponent_abbreviation = next(
            str((other.get("team") or {}).get("abbreviation") or "")
            for other in groups if str((other.get("team") or {}).get("id") or "") != source_team_id
        )
        statistics_groups = group.get("statistics") or []
        if len(statistics_groups) != 1:
            raise ValueError("espn_boxscore_statistics_group_count_mismatch")
        source_stats = statistics_groups[0]
        labels = source_stats.get("labels") or []
        if not set(_REQUIRED_LABELS) <= set(labels):
            raise ValueError("espn_boxscore_required_labels_missing")
        label_index = {str(label): index for index, label in enumerate(labels)}

        for item in source_stats.get("athletes") or []:
            athlete = item.get("athlete") or {}
            player_id = str(athlete.get("id") or "").strip() or None
            if player_id is not None and player_id in seen_player_ids:
                raise ValueError("duplicate_player_id_in_espn_game")
            if player_id is not None:
                seen_player_ids.add(player_id)
            stats_list = item.get("stats") or []

            def raw(label: str) -> Any:
                index = label_index.get(label)
                return stats_list[index] if index is not None and index < len(stats_list) else None

            comment = str(item.get("reason") or "").strip()
            did_not_play = item.get("didNotPlay")
            if did_not_play is True:
                participation = "did_not_play_confirmed"
            else:
                minutes_seconds, minutes_raw, _ = _parse_display_minutes(raw("MIN"))
                positive_stat = any(
                    (_parse_int(raw(label)) or 0) > 0
                    for label in ("PTS", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF")
                )
                participation = (
                    "played" if did_not_play is False and (minutes_seconds is not None or positive_stat)
                    else ("data_missing" if did_not_play is False else "unknown")
                )

            minutes_seconds, minutes_raw, minutes_whole_precision = _parse_display_minutes(raw("MIN"))
            if participation != "played":
                minutes_seconds = None
            starter_source_value = item.get("starter")
            starter = (
                None if participation != "played" or not isinstance(starter_source_value, bool)
                else int(starter_source_value)
            )

            fg_m, fg_a = _parse_split(raw("FG"))
            three_m, three_a = _parse_split(raw("3PT"))
            ft_m, ft_a = _parse_split(raw("FT"))
            count_values: dict[str, int | None] = {
                "points": _parse_int(raw("PTS")),
                "fgm": fg_m, "fga": fg_a,
                "three_pm": three_m, "three_pa": three_a,
                "ftm": ft_m, "fta": ft_a,
                "rebounds": _parse_int(raw("REB")),
                "offensive_rebounds": _parse_int(raw("OREB")),
                "defensive_rebounds": _parse_int(raw("DREB")),
                "assists": _parse_int(raw("AST")),
                "turnovers": _parse_int(raw("TO")),
                "steals": _parse_int(raw("STL")),
                "blocks": _parse_int(raw("BLK")),
                "fouls": _parse_int(raw("PF")),
            }
            plus_minus_raw = raw("+/-")
            plus_minus = _parse_int(plus_minus_raw) if plus_minus_raw not in (None, "") else None
            field_status: dict[str, str] = {}
            missing_fields: list[str] = []
            stats: dict[str, Any] = {}
            for name, value in count_values.items():
                if participation == "did_not_play_confirmed":
                    stats[name] = None
                    field_status[name] = "not_applicable"
                elif participation == "unknown":
                    stats[name] = None
                    field_status[name] = "unknown_participation"
                    missing_fields.append(name)
                else:
                    stats[name] = value
                    field_status[name] = "present" if value is not None else "source_missing"
                    if value is None:
                        missing_fields.append(name)

            if participation == "did_not_play_confirmed":
                field_status["minutes_seconds"] = "not_applicable"
            elif minutes_seconds is None:
                field_status["minutes_seconds"] = (
                    "unknown_participation" if participation == "unknown" else "source_missing"
                )
                missing_fields.append("minutes_seconds")
            else:
                field_status["minutes_seconds"] = (
                    "present_whole_minute_precision" if minutes_whole_precision else "present_source_precision"
                )

            fg_pct = round(fg_m / fg_a, 4) if fg_m is not None and fg_a else None
            three_pct = round(three_m / three_a, 4) if three_m is not None and three_a else None
            ft_pct = round(ft_m / ft_a, 4) if ft_m is not None and ft_a else None
            for key, value, attempts in (("fg_pct", fg_pct, fg_a), ("three_pct", three_pct, three_a), ("ft_pct", ft_pct, ft_a)):
                field_status[key] = (
                    "not_applicable_zero_attempts" if attempts == 0
                    else ("derived_from_makes_attempts" if value is not None else "source_missing")
                )
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

            warnings = ["source_minutes_whole_minute_precision"] if minutes_whole_precision else []
            if did_not_play is None:
                warnings.append("source_did_not_play_flag_missing")
            if player_id is None:
                missing_fields.append("player_id")
                field_status["player_id"] = "source_missing_identity_unresolved"
                warnings.append("player_id_missing_identity_unresolved")
                missing_status = "data_missing"
            else:
                field_status["player_id"] = "present"
            position = (athlete.get("position") or {}).get("abbreviation") if isinstance(athlete.get("position"), dict) else athlete.get("position")
            rows.append({
                "game_id": str(game["game_id"]),
                "season": str(game["season"]),
                "game_date": str(game["game_date"]),
                "team_id": team_id,
                "team_tricode": str(team.get("abbreviation") or ""),
                "opponent_team_id": opponent_team_id,
                "opponent_tricode": opponent_abbreviation,
                "home_away": home_away,
                "player_id": player_id,
                "player_name": str(athlete.get("displayName") or athlete.get("fullName") or athlete.get("shortName") or "").strip() or None,
                "position": position,
                "jersey_num": athlete.get("jersey"),
                "participation_status": participation,
                "starter": starter,
                "starter_source": "ESPN.summary.boxscore.players.athletes.starter",
                "minutes_raw": minutes_raw,
                "minutes_seconds": minutes_seconds,
                **stats,
                "fg_pct": fg_pct,
                "three_pct": three_pct,
                "ft_pct": ft_pct,
                "plus_minus": float(plus_minus) if plus_minus is not None else None,
                "source_comment": comment or None,
                "missing_status": missing_status,
                "missing_fields_json": json.dumps(missing_fields, ensure_ascii=False, separators=(",", ":")),
                "field_status_json": json.dumps(field_status, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "normalization_warnings_json": json.dumps(warnings, ensure_ascii=False, separators=(",", ":")),
            })
    if not rows:
        raise ValueError("espn_boxscore_player_rows_missing")
    return rows
