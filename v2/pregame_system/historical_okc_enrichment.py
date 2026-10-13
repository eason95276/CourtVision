"""Controlled, result-isolated 2025-26 OKC historical pregame enrichment.

This pilot-only pipeline reuses the existing FreeSearch MCP transport, official
NBA injury-report parser, read-only game identity database, and saved historical
boxscore samples. It never calls a forecasting model or modifies the public
Research, roster, or prediction modules.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
from v2.agent import historical_injury_lookup as historical

from .forecasting_pilot import (
    DEFAULT_ENRICHED_PILOT_PACKET_DIR,
    REPOSITORY_ROOT,
    TARGET_GAME_NUMBERS,
    TARGET_TEAM,
    TEAM_NAMES,
    _canonical_json,
    _game_identity,
    _game_number,
    _load_packets,
    _normalize_game_numbers,
    _packet_target,
    _team,
    _team_abbr,
    _team_name,
    _verify_packet,
    _write_new_json,
    packet_content_sha256,
)
from .forecasting_preflight import _completeness
from .free_search_mcp_client import FreeSearchMCPTransport
from .storage import now as _now_utc


PREFLIGHT_PACKET_DIR = REPOSITORY_ROOT / "v2" / "pregame_system" / "outputs" / "forecasting_preflight_okc_2025_26" / "forecast_packets"
GAMES_CSV = REPOSITORY_ROOT.parent / "courtvision" / "data" / "raw" / "Games.csv"
TEAM_STATISTICS_CSV = REPOSITORY_ROOT.parent / "courtvision" / "data" / "raw" / "TeamStatistics.csv"
COURTVISION_ROOT = REPOSITORY_ROOT.parent / "courtvision"
NBA_V2_DATA = COURTVISION_ROOT / "v2" / "data"
ANALYSIS_DATABASE = COURTVISION_ROOT / "analysis_v1" / "data" / "courtvision_analysis.sqlite"
SOURCE_SNAPSHOT_DATABASE = COURTVISION_ROOT / "v2" / "db" / "courtvision_pregame_snapshots.sqlite"
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "v2" / "pregame_system" / "outputs" / "okc_historical_enrichment_20261002"
TARGET_TEAM_ID = 1610612760
EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc
official = historical.official

PDF_SCHEDULE_URL = "https://on.nba.com/45yy64P"
PDF_SCHEDULE_TITLE = "2025-26 NBA Schedule by Team PDF"
PDF_SCHEDULE_PUBLISHED_AT = "2025-08-14"
CUP_QF_URL = "https://www.nba.com/news/starting-5-nba-cup-quarterfinals-set-jdub-returns-as-okc-wins-11th-straight"
CUP_QF_TITLE = "Starting 5: NBA Cup Quarterfinals Set, JDub Returns As OKC Wins 11th Straight"
CUP_SEMI_URL = "https://www.nba.com/news/starting-5-dec-11-2025"
CUP_SEMI_TITLE = "Starting 5: Semifinals set in Emirates NBA Cup"
GAME_027_PREVIEW_URL = "https://www.nba.com/clippers/news/game-preview-5-things-you-should-know-about-clippers-vs-thunder-7"
GAME_027_PREVIEW_TITLE = "GAME PREVIEW: 5 Things You Should Know About Clippers vs. Thunder"
GAME_027_DAILY_SCHEDULE_URL = "https://www.nba.com/news/starting-5-dec-18-2025"
GAME_027_DAILY_SCHEDULE_TITLE = "Starting 5: Gearing up for a 12-game Thursday slate"

# Transcribed from the Thunder row of the official NBA schedule PDF. The two
# cup slots were intentionally unassigned in the August release; Game 27's
# date was later updated. Times are the PDF's ET "game time" (tip follows +10m).
PDF_OKC_ROWS = [
    (1, "2025-10-21", "hou", "home", "19:30"),
    (2, "2025-10-23", "ind", "away", "19:30"),
    (3, "2025-10-25", "atl", "away", "19:30"),
    (4, "2025-10-27", "dal", "away", "20:30"),
    (5, "2025-10-28", "sac", "home", "20:00"),
    (6, "2025-10-30", "was", "home", "20:00"),
    (7, "2025-11-02", "nop", "home", "15:30"),
    (8, "2025-11-04", "lac", "away", "23:00"),
    (9, "2025-11-05", "por", "away", "22:00"),
    (10, "2025-11-07", "sac", "away", "22:00"),
    (11, "2025-11-09", "mem", "away", "18:00"),
    (12, "2025-11-11", "gsw", "home", "20:00"),
    (13, "2025-11-12", "lal", "home", "21:30"),
    (14, "2025-11-15", "cha", "away", "19:00"),
    (15, "2025-11-17", "nop", "away", "20:00"),
    (16, "2025-11-19", "sac", "home", "20:00"),
    (17, "2025-11-21", "uta", "away", "22:00"),
    (18, "2025-11-23", "por", "home", "19:00"),
    (19, "2025-11-26", "min", "home", "19:30"),
    (20, "2025-11-28", "phx", "home", "21:30"),
    (21, "2025-11-30", "por", "away", "18:00"),
    (22, "2025-12-02", "gsw", "away", "23:00"),
    (23, "2025-12-05", "dal", "home", "21:30"),
    (24, "2025-12-07", "uta", "away", "20:00"),
    (25, None, None, None, None),
    (26, None, None, None, None),
    (27, "2025-12-17", "lac", "home", "20:00"),
    (28, "2025-12-19", "min", "away", "21:30"),
    (29, "2025-12-22", "mem", "home", "21:30"),
    (30, "2025-12-23", "sas", "away", "20:30"),
    (31, "2025-12-25", "sas", "home", "14:30"),
    (32, "2025-12-28", "phi", "home", "15:30"),
    (33, "2025-12-29", "atl", "home", "20:00"),
    (34, "2025-12-31", "por", "home", "20:00"),
    (35, "2026-01-02", "gsw", "away", "22:00"),
    (36, "2026-01-04", "phx", "away", "20:00"),
    (37, "2026-01-05", "cha", "home", "20:00"),
    (38, "2026-01-07", "uta", "home", "20:00"),
    (39, "2026-01-09", "mem", "away", "20:00"),
    (40, "2026-01-11", "mia", "home", "19:00"),
    (41, "2026-01-13", "sas", "home", "20:00"),
    (42, "2026-01-15", "hou", "away", "19:30"),
    (43, "2026-01-17", "mia", "away", "20:00"),
    (44, "2026-01-19", "cle", "away", "14:30"),
    (45, "2026-01-21", "mil", "away", "21:30"),
    (46, "2026-01-23", "ind", "home", "21:30"),
    (47, "2026-01-25", "tor", "home", "19:00"),
    (48, "2026-01-27", "nop", "home", "20:00"),
    (49, "2026-01-29", "min", "away", "21:30"),
    (50, "2026-02-01", "den", "away", "21:30"),
    (51, "2026-02-03", "orl", "home", "20:00"),
    (52, "2026-02-04", "sas", "away", "21:30"),
    (53, "2026-02-07", "hou", "home", "15:30"),
    (54, "2026-02-09", "lal", "away", "22:00"),
    (55, "2026-02-11", "phx", "away", "21:00"),
    (56, "2026-02-12", "mil", "home", "19:30"),
    (57, "2026-02-20", "bkn", "home", "20:00"),
    (58, "2026-02-22", "cle", "home", "13:00"),
    (59, "2026-02-24", "tor", "away", "19:30"),
    (60, "2026-02-25", "det", "away", "19:00"),
    (61, "2026-02-27", "den", "home", "21:30"),
    (62, "2026-03-01", "dal", "away", "20:00"),
    (63, "2026-03-03", "chi", "away", "20:00"),
    (64, "2026-03-04", "nyk", "away", "19:00"),
    (65, "2026-03-07", "gsw", "home", "20:30"),
    (66, "2026-03-09", "den", "home", "19:30"),
    (67, "2026-03-12", "bos", "home", "21:30"),
    (68, "2026-03-15", "min", "home", "13:00"),
    (69, "2026-03-17", "orl", "away", "19:00"),
    (70, "2026-03-18", "bkn", "away", "19:30"),
    (71, "2026-03-21", "was", "away", "17:00"),
    (72, "2026-03-23", "phi", "away", "19:00"),
    (73, "2026-03-25", "bos", "away", "19:30"),
    (74, "2026-03-27", "chi", "home", "20:00"),
    (75, "2026-03-29", "nyk", "home", "19:30"),
    (76, "2026-03-30", "det", "home", "21:30"),
    (77, "2026-04-02", "lal", "home", "19:30"),
    (78, "2026-04-05", "uta", "home", "19:00"),
    (79, "2026-04-07", "lal", "away", "22:30"),
    (80, "2026-04-08", "lac", "away", "22:00"),
    (81, "2026-04-10", "den", "away", "22:00"),
    (82, "2026-04-12", "phx", "home", "20:30"),
]

CUP_SCHEDULE = {
    25: {
        "date": "2025-12-10", "home": "okc", "away": "phx", "listed_time_et": "19:30",
        "source_name": "NBA.com Starting 5", "title": CUP_QF_TITLE, "url": CUP_QF_URL,
        "published_at": "2025-11-29 15:08 (timezone not displayed)",
        "information_type": "official_nba_cup_quarterfinal_schedule",
        "summary": "NBA's Nov. 29 schedule item identifies Phoenix at Oklahoma City on Dec. 10 at 7:30 p.m. ET.",
        "published_before_tipoff": True, "usable_for_prediction": True,
        "cup_stage": "quarterfinal", "site": "Oklahoma City",
    },
    26: {
        "date": "2025-12-13", "home": "okc", "away": "sas", "listed_time_et": "21:00",
        "source_name": "NBA.com Starting 5", "title": "Starting 5: Semifinals set in Emirates NBA Cup",
        "url": CUP_SEMI_URL,
        "published_at": "2025-12-11 15:49 (timezone not displayed)",
        "information_type": "official_nba_cup_semifinal_schedule",
        "summary": "NBA's Dec. 11 schedule item identifies San Antonio vs. Oklahoma City on Dec. 13 at 9 p.m. ET.",
        "published_before_tipoff": True, "usable_for_prediction": True,
        "cup_stage": "semifinal", "site": "Las Vegas (neutral site)",
    },
    27: {
        "date": "2025-12-18", "home": "okc", "away": "lac", "listed_time_et": "20:00",
        "source_name": "LA Clippers official site", "title": GAME_027_PREVIEW_TITLE,
        "url": GAME_027_PREVIEW_URL,
        "published_at": "2025-12-18T13:00:00-08:00",
        "information_type": "official_same_day_pregame_schedule_correction",
        "summary": "The official Clippers site indexed a same-day Clippers vs. Thunder game preview at 1 p.m. PST. The article reader exposed only an iframe, so no player/news facts were taken from the body. The historical game database lists an 8 p.m. ET start, but the listed time is not independently verified from the pregame page; the conservative information cutoff is 4 p.m. ET.",
        "published_before_tipoff": True, "usable_for_prediction": True,
        "cup_stage": None, "site": "Oklahoma City",
        "conservative_cutoff_local": "2025-12-18T16:00:00",
        "cutoff_basis": "official pregame preview published 1 p.m. PST; earlier than the 8 p.m. ET schedule listing",
    },
}

# The historical Games.csv timestamps disagree with the August PDF for these
# four games. Keep the original official PDF time as the authenticated value,
# and use an earlier cutoff than both documented times. Do not publish the
# later database timestamp as a verified pregame tipoff.
TIME_CONFLICTS = {
    46: {"pdf_time_et": "21:30", "history_time_et": "20:00", "conservative_cutoff_et": "19:30"},
    60: {"pdf_time_et": "19:00", "history_time_et": "19:30", "conservative_cutoff_et": "18:30"},
    77: {"pdf_time_et": "19:30", "history_time_et": "21:30", "conservative_cutoff_et": "19:00"},
    81: {"pdf_time_et": "22:00", "history_time_et": "21:00", "conservative_cutoff_et": "20:30"},
}

PREFLIGHT_SCHEMA_VERSION = "okc-2025-26-historical-enrichment-v1"
SEARCH_BATCH_SIZE = 8
FORBIDDEN_PACKET_KEYS = {
    "score_home", "score_away", "home_score", "away_score", "actual_winner",
    "actual_margin", "actual_home_margin", "actual_total", "ats_result", "ou_result",
    "quarter_scores", "postgame", "postgame_stats", "postgame_news", "final_score",
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _date_string(value: Any) -> str:
    return datetime.fromisoformat(str(value)).date().isoformat()


def _game_number(target_or_packet: Mapping[str, Any]) -> int:
    if "packet_id" in target_or_packet:
        return _game_number_packet(target_or_packet)
    return int(target_or_packet["game_number"])


def _game_number_packet(packet: Mapping[str, Any]) -> int:
    return int(_packet_target(packet)["game_number"])


def _packet_team(value: Any) -> str:
    return _team(value.get("abbr") if isinstance(value, Mapping) else value)


def _contains_forbidden(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = str(key)
            if str(key).casefold() in FORBIDDEN_PACKET_KEYS:
                found.append(path)
            found.extend(f"{path}.{item}" for item in _contains_forbidden(child))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            found.extend(f"[{i}].{item}" for item in _contains_forbidden(child))
    return found


def _read_target_packets(packet_dir: Path) -> list[dict[str, Any]]:
    packets = _load_packets(packet_dir, range(1, 83))
    if len(packets) != 82:
        raise ValueError("okc_packet_count_not_82")
    return packets


def _read_games_csv_schedule(packets: Sequence[Mapping[str, Any]]) -> tuple[dict[int, dict[str, Any]], dict[str, str]]:
    if not GAMES_CSV.is_file():
        raise FileNotFoundError("historical_games_csv_missing")
    required = ["gameId", "gameDateTimeEst", "hometeamId", "awayteamId", "gameType"]
    frame = pd.read_csv(GAMES_CSV, usecols=required, low_memory=False)
    by_date: dict[str, list[dict[str, Any]]] = {}
    for raw in frame.to_dict("records"):
        if str(raw.get("gameType")) != "Regular Season":
            continue
        home_id, away_id = int(raw["hometeamId"]), int(raw["awayteamId"])
        if TARGET_TEAM_ID not in {home_id, away_id}:
            continue
        scheduled = datetime.fromisoformat(str(raw["gameDateTimeEst"]))
        key = scheduled.date().isoformat()
        by_date.setdefault(key, []).append({
            "game_id": f"{int(raw['gameId']):010d}",
            "game_id_source_value": str(raw["gameId"]),
            "scheduled_local_naive": scheduled.isoformat(timespec="seconds"),
            "date": key,
            "home_team_id": home_id,
            "away_team_id": away_id,
        })
    official.DEFAULT_ANALYSIS_DATABASE = ANALYSIS_DATABASE
    result: dict[int, dict[str, Any]] = {}
    for packet in packets:
        number = _game_number_packet(packet)
        target = _packet_target(packet)
        date_key = _date_string(target["date"])
        rows = by_date.get(date_key, [])
        if len(rows) != 1:
            raise ValueError(f"schedule_identity_not_unique:game_{number:03d}:matches={len(rows)}")
        row = rows[0]
        identity = official.load_game_identity(row["game_id"], ANALYSIS_DATABASE)
        home, away = _packet_team(target["home"]), _packet_team(target["away"])
        if (_team(identity["home_team_abbr"]) != home or _team(identity["away_team_abbr"]) != away
                or str(identity["game_date"])[:10] != date_key):
            raise ValueError(f"schedule_game_identity_mismatch:game_{number:03d}")
        if TARGET_TEAM_ID not in {row["home_team_id"], row["away_team_id"]}:
            raise ValueError(f"schedule_not_okc_game:game_{number:03d}")
        row.update({
            "game_number": number,
            "home": home,
            "away": away,
            "official_home_team_name": identity["home_team_name"],
            "official_away_team_name": identity["away_team_name"],
        })
        result[number] = row
    if len(result) != 82 or [result[n]["date"] for n in range(1, 83)] != sorted(
            result[n]["date"] for n in range(1, 83)):
        raise ValueError("schedule_rows_incomplete_or_not_chronological")
    return result, {"games_csv_sha256": _sha256_file(GAMES_CSV),
                    "analysis_database_sha256": _sha256_file(ANALYSIS_DATABASE)}


def _official_schedule_match(number: int, schedule_row: Mapping[str, Any]) -> dict[str, Any]:
    row = PDF_OKC_ROWS[number - 1]
    if row[0] != number:
        raise ValueError("official_pdf_game_number_sequence_invalid")
    if number in CUP_SCHEDULE:
        update = CUP_SCHEDULE[number]
        actual_home, actual_away = schedule_row["home"], schedule_row["away"]
        listed = schedule_row["scheduled_local_naive"][11:16]
        if (schedule_row["date"], actual_home, actual_away, listed) != (
                update["date"], update["home"], update["away"], update["listed_time_et"]):
            raise ValueError(f"post_group_schedule_update_mismatch:game_{number:03d}")
        return {"status": "verified_by_post_release_update", **update}
    expected_date, opponent, okc_side, listed_time = row[1:]
    if not expected_date or not opponent or not okc_side or not listed_time:
        raise ValueError(f"official_pdf_static_schedule_row_missing:game_{number:03d}")
    expected_home, expected_away = (("okc", opponent) if okc_side == "home"
                                    else (opponent, "okc"))
    actual_time = schedule_row["scheduled_local_naive"][11:16]
    expected = (expected_date, expected_home, expected_away, listed_time)
    actual = (schedule_row["date"], schedule_row["home"], schedule_row["away"], actual_time)
    if number == 27:
        # The Aug. 14 PDF listed this game on Dec. 17; official same-day
        # pregame sources later confirm the updated Dec. 18 listing.
        if actual != ("2025-12-18", "okc", "lac", "20:00"):
            raise ValueError("game_027_schedule_update_identity_mismatch")
        return {"status": "verified_by_same_day_pregame_schedule_update",
                "initial_pdf_date": expected_date, "updated_date": "2025-12-18",
                "updated_cutoff_local": CUP_SCHEDULE[27]["conservative_cutoff_local"],
                "updated_time_et": CUP_SCHEDULE[27]["listed_time_et"],
                "source": CUP_SCHEDULE[27]}
    if number in TIME_CONFLICTS:
        details = TIME_CONFLICTS[number]
        if (schedule_row["date"], schedule_row["home"], schedule_row["away"]) != (
                expected_date, expected_home, expected_away):
            raise ValueError(f"official_pdf_schedule_identity_mismatch:game_{number:03d}")
        if actual_time not in {details["pdf_time_et"], details["history_time_et"]}:
            raise ValueError(f"schedule_time_outside_known_candidates:game_{number:03d}")
        return {"status": "time_conflict_conservative_cutoff",
                "pdf_listed_time_et": details["pdf_time_et"],
                "historical_database_time_et": details["history_time_et"],
                "conservative_cutoff_et": details["conservative_cutoff_et"],
                "schedule_time_verified": False,
                "source": {
                    "source_name": "NBA Communications",
                    "title": PDF_SCHEDULE_TITLE,
                    "url": PDF_SCHEDULE_URL,
                    "published_at": PDF_SCHEDULE_PUBLISHED_AT,
                    "information_type": "official_initial_schedule_identity_and_time",
                    "summary": ("The official preseason PDF identifies this date and matchup, but its listed ET time conflicts with the later historical Games.csv time. The forecast packet will not expose an unverified scheduled time; the research cutoff is earlier than both recorded times."),
                    "published_before_tipoff": True,
                    "usable_for_prediction": False,
                    "classification": "source_fact_with_conflict",
                }}
    if actual != expected:
        raise ValueError(f"official_pdf_schedule_identity_time_mismatch:game_{number:03d}")
    return {"status": "verified_against_official_august_schedule_pdf",
            "source_name": "NBA Communications", "title": PDF_SCHEDULE_TITLE,
            "url": PDF_SCHEDULE_URL, "published_at": PDF_SCHEDULE_PUBLISHED_AT,
            "information_type": "official_scheduled_game_identity_and_time",
            "summary": "Target date, opponent, home/away and Eastern listed game time match the OKC row in the official team schedule PDF. The PDF says tip-off is 10 minutes after listed game time; the listed time is used as a conservative information cutoff.",
            "published_before_tipoff": True, "usable_for_prediction": True,
            "listed_game_time_et": listed_time,
            "actual_tipoff_verified": False}


def _cutoff_for_game(number: int, row: Mapping[str, Any]) -> tuple[str, str, bool]:
    if number == 27:
        # Use the precise same-day official preview publication, not the later
        # time-zone-ambiguous schedule page timestamp, to stay conservative.
        local = datetime.fromisoformat(CUP_SCHEDULE[27]["conservative_cutoff_local"])
        return local.replace(tzinfo=EASTERN).isoformat(timespec="seconds"), "same_day_official_preview", False
    if number in TIME_CONFLICTS:
        local = datetime.fromisoformat(f"{row['date']}T{TIME_CONFLICTS[number]['conservative_cutoff_et']}:00")
        return local.replace(tzinfo=EASTERN).isoformat(timespec="seconds"), "earliest_documented_schedule_time_minus_30_minutes", False
    if number in TIME_CONFLICTS:
        local = datetime.fromisoformat(f"{row['date']}T{TIME_CONFLICTS[number]['conservative_cutoff_et']}:00")
        return local.replace(tzinfo=EASTERN).isoformat(timespec="seconds"), "earliest_schedule_candidate_minus_30_minutes", False
    local = datetime.fromisoformat(row["scheduled_local_naive"]).replace(tzinfo=EASTERN)
    return local.isoformat(timespec="seconds"), "official_listed_game_time", False


def _write_once_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != payload:
            raise FileExistsError(f"protected_enrichment_artifact_conflict:{path.name}")
        return path
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
    return path


def _write_once_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _sha256_file(path) != hashlib.sha256(payload).hexdigest():
            raise FileExistsError(f"protected_enrichment_source_conflict:{path.name}")
        return path
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
    return path


def _source_for_schedule(
    number: int, schedule: Mapping[str, Any], captured_at: str,
) -> list[dict[str, Any]]:
    if number in CUP_SCHEDULE:
        update = dict(CUP_SCHEDULE[number])
        source = {key: update[key] for key in (
            "source_name", "title", "url", "published_at", "information_type",
            "summary", "published_before_tipoff", "usable_for_prediction")
        }
        source.update({"captured_at": captured_at,
                       "historical_reconstruction": True,
                       "persisted_before_cutoff_proven": False,
                       "classification": "source_fact"})
        if number == 27:
            source["date_only_schedule_feed_rejected"] = True
            source["source_note"] = (
                "The Aug. 14 schedule PDF listed Dec. 17. An official same-day Clippers pregame page confirms the updated Dec. 18 matchup. Its article body was not readable through the fetcher, so no player/news facts are taken from it. The cutoff is 4 p.m. ET, converted from the page's 1 p.m. PST publication time."
            )
        return [source]
    if number in TIME_CONFLICTS:
        details = TIME_CONFLICTS[number]
        return [{
            "source_name": "NBA Communications",
            "title": PDF_SCHEDULE_TITLE,
            "url": PDF_SCHEDULE_URL,
            "published_at": PDF_SCHEDULE_PUBLISHED_AT,
            "information_type": "official_initial_schedule_identity",
            "summary": (f"The official preseason PDF matches this game's date and matchup but lists {details['pdf_time_et']} ET; the later historical database shows a conflicting time. This PDF time is retained as a pregame schedule record, not used as the target's verified scheduled start."),
            "published_before_tipoff": True,
            "usable_for_prediction": False,
            "captured_at": captured_at,
            "historical_reconstruction": True,
            "persisted_before_cutoff_proven": False,
            "classification": "source_fact_with_schedule_conflict",
        }]
    return [{
        "source_name": "NBA Communications",
        "title": PDF_SCHEDULE_TITLE,
        "url": PDF_SCHEDULE_URL,
        "published_at": PDF_SCHEDULE_PUBLISHED_AT,
        "information_type": "official_scheduled_game_identity_and_time",
        "summary": (
            f"OKC Game {number} row matched by date, opponent, home/away and ET listed time in the official 2025-26 team schedule PDF. "
            "The PDF says tip-off follows the listed game time by 10 minutes; the listed time is used as a conservative cutoff."
        ),
        "published_before_tipoff": True,
        "usable_for_prediction": True,
        "captured_at": captured_at,
        "historical_reconstruction": True,
        "persisted_before_cutoff_proven": False,
        "classification": "source_fact",
    }]


def _search_receipt_path(output_dir: Path, game_number: int) -> Path:
    return output_dir / "injury_search" / f"game_{game_number:03d}.json"


def _candidate_url(value: Any, target_date: str) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "ak-static.cms.nba.com":
        return False
    filename = Path(parsed.path).name
    match = re.fullmatch(r"Injury-Report_(\d{4}-\d{2}-\d{2})_[A-Za-z0-9_]+\.pdf", filename)
    return bool(match and match.group(1) == target_date)


def _injury_search_payload(schedule_row: Mapping[str, Any]) -> dict[str, Any]:
    date_value = schedule_row["date"]
    home = schedule_row["official_home_team_name"]
    away = schedule_row["official_away_team_name"]
    return {
        "query": (f"site:ak-static.cms.nba.com/referee/injury/ Injury-Report_{date_value} "
                  f"{away} {home} NBA injury report"),
        "topic": "news",
        "search_depth": "advanced",
        "max_results": 8,
        "include_answer": False,
        "include_raw_content": False,
        "include_domains": ["nba.com"],
    }


def discover_official_injury_pdfs(
    schedules: Mapping[int, Mapping[str, Any]],
    output_dir: str | Path,
    *,
    search_transport: Any | None = None,
    batch_size: int = SEARCH_BATCH_SIZE,
) -> dict[int, dict[str, Any]]:
    """Search only the official NBA injury-report archive; keep no search snippets."""
    output = Path(output_dir)
    transport = search_transport or FreeSearchMCPTransport()
    todo: list[tuple[int, dict[str, Any]]] = []
    receipts: dict[int, dict[str, Any]] = {}
    for number in range(1, 83):
        path = _search_receipt_path(output, number)
        if path.is_file():
            receipt = json.loads(path.read_text(encoding="utf-8"))
            expected_query = None if number in TARGET_GAME_NUMBERS else _injury_search_payload(schedules[number])["query"]
            if (receipt.get("game_number") != number
                    or receipt.get("game_id") != schedules[number]["game_id"]
                    or receipt.get("query") != expected_query):
                raise ValueError(f"injury_search_receipt_identity_mismatch:game_{number:03d}")
            receipts[number] = receipt
            continue
        if number in TARGET_GAME_NUMBERS:
            receipt = {
                "schema_version": "okc-historical-injury-search-v1",
                "game_number": number,
                "game_id": schedules[number]["game_id"],
                "date": schedules[number]["date"],
                "cutoff_local": schedules[number]["cutoff_local"],
                "query": None,
                "provider": "preserved_enriched_pilot_packet",
                "captured_at": _now_utc(),
                "raw_response_sha256": None,
                "search_error": None,
                "official_same_date_pdf_candidates": [],
                "nonofficial_or_other_date_results_discarded": 0,
                "search_snippets_retained": False,
                "deepseek_called": False,
                "status": "skipped_existing_enriched_pilot",
            }
            _write_once_json(path, receipt)
            receipts[number] = receipt
            continue
        todo.append((number, _injury_search_payload(schedules[number])))
    for start in range(0, len(todo), batch_size):
        chunk = todo[start:start + batch_size]
        captured_at = _now_utc()
        try:
            responses = transport.search_many([payload for _, payload in chunk])
            if len(responses) != len(chunk):
                raise ValueError("injury_search_response_count_mismatch")
        except Exception as exc:
            responses = [{"provider": "free-search-mcp", "errors": {"batch": type(exc).__name__},
                          "results": []} for _ in chunk]
        for (number, payload), response in zip(chunk, responses, strict=True):
            raw_results = response.get("results") if isinstance(response, Mapping) else []
            raw_results = raw_results if isinstance(raw_results, list) else []
            urls = list(dict.fromkeys(str(item.get("url") or "") for item in raw_results
                                      if isinstance(item, Mapping)
                                      and _candidate_url(item.get("url"), schedules[number]["date"])))
            receipt = {
                "schema_version": "okc-historical-injury-search-v1",
                "game_number": number,
                "game_id": schedules[number]["game_id"],
                "date": schedules[number]["date"],
                "cutoff_local": schedules[number]["cutoff_local"],
                "query": payload["query"],
                "provider": (response.get("provider") if isinstance(response, Mapping) else None),
                "captured_at": captured_at,
                "raw_response_sha256": _hash(response),
                "search_error": response.get("errors") if isinstance(response, Mapping) else "invalid_response",
                "official_same_date_pdf_candidates": urls,
                "nonofficial_or_other_date_results_discarded": max(0, len(raw_results) - len(urls)),
                "search_snippets_retained": False,
                "deepseek_called": False,
            }
            _write_once_json(_search_receipt_path(output, number), receipt)
            receipts[number] = receipt
    return receipts


def _selected_pdf_rows(report_rows: Sequence[Mapping[str, Any]], identity: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    matchup = f"{identity['away_team_abbr']}@{identity['home_team_abbr']}"
    expected_day = datetime.fromisoformat(str(identity["game_date"])).strftime("%m/%d/%Y")
    expected_teams = {
        official._compact(identity[f"{side}_team_name"])
        for side in ("away", "home")
    }
    selected = [row for row in report_rows
                if official._compact(str(row.get("matchup") or "")) == official._compact(matchup)
                and str(row.get("game_date") or "") == expected_day]
    if not selected or {official._compact(str(row.get("team") or "")) for row in selected} != expected_teams:
        return []
    keys = [(official._compact(str(row.get("team") or "")),
             official._compact(str(row.get("player_name") or ""))) for row in selected]
    if len(set(keys)) != len(keys):
        return []
    allowed = {"Out", "Questionable", "Available", "Doubtful", "Probable"}
    if any(row.get("availability_status") not in allowed for row in selected):
        return []
    return selected


def _copy_snapshot_database(output_dir: Path) -> Path:
    target = output_dir / "snapshot_db" / "courtvision_pregame_snapshots.sqlite"
    if target.is_file():
        check = sqlite3.connect(target)
        try:
            row = check.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                raise ValueError("enrichment_snapshot_database_corrupt")
        finally:
            check.close()
        return target
    if not SOURCE_SNAPSHOT_DATABASE.is_file():
        raise FileNotFoundError("source_historical_snapshot_database_missing")
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(SOURCE_SNAPSHOT_DATABASE.resolve().as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(target)
    try:
        source.execute("PRAGMA query_only=ON")
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    return target


def _existing_injury_result(output_dir: Path, game_id: str) -> dict[str, Any] | None:
    # Reuse only a prior official-importer artifact with intact raw bytes.
    base = output_dir / "injury_packets" / f"{game_id}.json"
    candidates = [base] if base.is_file() else sorted((output_dir / "injury_packets").glob(f"{game_id}_*.json"))
    for path in candidates:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            raw = Path(result["raw_path"])
            if (result.get("game_id") == game_id and raw.is_file()
                    and _sha256_file(raw) == result.get("raw_sha256")
                    and result.get("normalized_status") == "available_historical_archive"):
                return result
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            continue
    return None


def collect_official_injuries(
    schedules: Mapping[int, Mapping[str, Any]],
    search_receipts: Mapping[int, Mapping[str, Any]],
    output_dir: str | Path,
    *,
    fetch_bytes_fn=None,
) -> dict[int, dict[str, Any]]:
    """Download official same-day PDFs, reject post-cutoff reports, import once to an isolated DB."""
    from v2.agent import historical_injury_lookup as historical

    output = Path(output_dir)
    official.DEFAULT_ANALYSIS_DATABASE = ANALYSIS_DATABASE
    snapshot_db = _copy_snapshot_database(output)
    official.DEFAULT_SNAPSHOT_DATABASE = snapshot_db
    fetch_bytes_fn = fetch_bytes_fn or historical.fetch_bytes
    results: dict[int, dict[str, Any]] = {}
    for number in range(1, 83):
        schedule = schedules[number]
        game_id = schedule["game_id"]
        audit_path = output / "injury_audit" / f"game_{number:03d}.json"
        if audit_path.is_file():
            saved = json.loads(audit_path.read_text(encoding="utf-8"))
            if saved.get("game_id") != game_id or saved.get("cutoff_local") != schedule["cutoff_local"]:
                raise ValueError(f"injury_audit_identity_or_cutoff_mismatch:game_{number:03d}")
            if saved.get("status") == "available":
                result_path = Path(saved["result_path"])
                result = json.loads(result_path.read_text(encoding="utf-8"))
                raw = Path(result["raw_path"])
                if not raw.is_file() or _sha256_file(raw) != result.get("raw_sha256"):
                    raise ValueError(f"injury_audit_raw_hash_mismatch:game_{number:03d}")
                if result.get("game_id") != game_id:
                    raise ValueError(f"injury_audit_game_id_mismatch:game_{number:03d}")
                results[number] = {"status": "available", "result": result,
                                   "reused_existing_receipt": True,
                                   "latest_report_among_returned_candidates": saved.get("latest_report_among_returned_candidates"),
                                   "rejected_candidates": saved.get("rejected_candidate_reasons", [])}
            else:
                results[number] = {"status": saved.get("status"),
                                   "reason": saved.get("reason"),
                                   "result": None,
                                   "reused_existing_receipt": True,
                                   "candidate_count": saved.get("candidate_count", 0),
                                   "rejected_candidates": saved.get("rejected_candidate_reasons", [])}
            continue
        existing = _existing_injury_result(output, game_id)
        if existing is not None:
            issue = datetime.fromisoformat(existing["source_report_time_local"])
            cutoff, _ = official._cutoff_to_utc(schedule["cutoff_local"])
            if issue <= cutoff and existing.get("cutoff_time_utc") == official._utc_text(cutoff):
                result_path = next((path for path in (output / "injury_packets").glob(f"{game_id}*.json")
                                    if path.is_file() and json.loads(path.read_text(encoding="utf-8")).get("raw_sha256") == existing.get("raw_sha256")), None)
                if result_path is None:
                    raise ValueError(f"existing_injury_result_path_missing:game_{number:03d}")
                audit = {"schema_version": "okc-historical-injury-audit-v1",
                         "game_number": number, "game_id": game_id,
                         "cutoff_local": schedule["cutoff_local"], "status": "available",
                         "source_url": existing.get("source_url"),
                         "published_at": existing.get("source_published_at_utc"),
                         "captured_at": existing.get("captured_at_utc"),
                         "raw_sha256": existing.get("raw_sha256"),
                         "record_count": existing.get("record_count"),
                         "result_path": str(result_path.resolve()),
                         "latest_report_among_returned_candidates": False,
                         "candidate_count": 0,
                         "rejected_candidate_reasons": [],
                         "historical_reconstruction": True,
                         "persisted_before_cutoff_proven": False,
                         "deepseek_called": False}
                _write_once_json(audit_path, audit)
                results[number] = {"status": "available", "result": existing,
                                   "reused_existing_receipt": True,
                                   "latest_report_among_returned_candidates": False,
                                   "rejected_candidates": []}
                continue
        receipt = search_receipts.get(number, {})
        urls = list(receipt.get("official_same_date_pdf_candidates") or [])
        # The five high-quality pilot packets already carry their vetted injury
        # reports; do not replace them with a lower-confidence search result.
        if number in TARGET_GAME_NUMBERS:
            audit = {"schema_version": "okc-historical-injury-audit-v1",
                     "game_number": number, "game_id": game_id,
                     "cutoff_local": schedule["cutoff_local"],
                     "status": "preserved_enriched_pilot", "candidate_count": 0,
                     "rejected_candidate_reasons": [], "historical_reconstruction": True,
                     "persisted_before_cutoff_proven": False, "deepseek_called": False}
            _write_once_json(audit_path, audit)
            results[number] = {"status": "preserved_enriched_pilot", "result": None,
                               "reused_existing_receipt": True, "rejected_candidates": []}
            continue
        temp_candidates = []
        rejections = []
        identity = official.load_game_identity(game_id, ANALYSIS_DATABASE)
        cutoff, _ = official._cutoff_to_utc(schedule["cutoff_local"])
        for url in urls[:8]:
            if not url.startswith(official.OFFICIAL_PDF_PREFIX) or not url.endswith(".pdf"):
                rejections.append({"reason": "untrusted_source"})
                continue
            try:
                body = fetch_bytes_fn(url)
                temp_path = None
                from tempfile import NamedTemporaryFile
                with NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                    handle.write(body)
                    temp_path = Path(handle.name)
                try:
                    report_time, rows = official._extract_rows(temp_path)
                    if report_time.date().isoformat() != schedule["date"]:
                        rejections.append({"source_url": url, "reason": "report_date_mismatch"})
                        continue
                    if report_time > cutoff:
                        rejections.append({"source_url": url, "reason": "published_after_cutoff",
                                           "source_published_at": report_time.isoformat()})
                        continue
                    selected = _selected_pdf_rows(rows, identity)
                    if not selected:
                        rejections.append({"source_url": url, "reason": "target_matchup_or_team_rows_missing"})
                        continue
                    temp_candidates.append({
                        "source_url": url,
                        "report_time": report_time,
                        "payload": body,
                        "sha256": hashlib.sha256(body).hexdigest(),
                    })
                finally:
                    temp_path.unlink(missing_ok=True)
            except Exception as exc:
                rejections.append({"source_url": url, "reason": type(exc).__name__})
        if not temp_candidates:
            reason = ("no_official_pdf_candidate" if not urls
                      else "no_identity_matched_report_published_by_cutoff")
            audit = {"schema_version": "okc-historical-injury-audit-v1",
                     "game_number": number, "game_id": game_id,
                     "cutoff_local": schedule["cutoff_local"], "status": "unavailable",
                     "reason": reason, "candidate_count": len(urls),
                     "rejected_candidate_reasons": [{"reason": row.get("reason"),
                                                     "source_published_at": row.get("source_published_at")}
                                                    for row in rejections],
                     "historical_reconstruction": True,
                     "persisted_before_cutoff_proven": False,
                     "deepseek_called": False}
            _write_once_json(audit_path, audit)
            results[number] = {"status": "unavailable", "reason": reason,
                               "reused_existing_receipt": False,
                               "candidate_count": len(urls), "rejected_candidates": rejections}
            continue
        selected_pdf = max(temp_candidates, key=lambda item: item["report_time"])
        raw_dir = output / "raw" / "official_injury_reports" / f"game_{number:03d}"
        raw_path = raw_dir / f"{selected_pdf['sha256']}.pdf"
        _write_once_bytes(raw_path, selected_pdf["payload"])
        issue_key = selected_pdf["report_time"].astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        result_path = output / "injury_packets" / f"game_{number:03d}_{game_id}_{issue_key}.json"
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("raw_sha256") != selected_pdf["sha256"]:
                raise FileExistsError(f"injury_result_receipt_conflict:game_{number:03d}")
        else:
            result = official.import_report(
                game_id=game_id,
                cutoff_time=schedule["cutoff_local"],
                pdf_path=raw_path,
                source_url=selected_pdf["source_url"],
                analysis_database=ANALYSIS_DATABASE,
                snapshot_database=snapshot_db,
                log_path=result_path,
            )
        audit = {"schema_version": "okc-historical-injury-audit-v1",
                 "game_number": number, "game_id": game_id,
                 "cutoff_local": schedule["cutoff_local"], "status": "available",
                 "source_url": result.get("source_url"),
                 "published_at": result.get("source_published_at_utc"),
                 "captured_at": result.get("captured_at_utc"),
                 "raw_sha256": result.get("raw_sha256"),
                 "record_count": result.get("record_count"),
                 "result_path": str(result_path.resolve()),
                 "latest_report_among_returned_candidates": True,
                 "candidate_count": len(urls),
                 "selected_candidate_count": len(temp_candidates),
                 "rejected_candidate_reasons": [{"reason": row.get("reason"),
                                                 "source_published_at": row.get("source_published_at")}
                                                for row in rejections],
                 "historical_reconstruction": True,
                 "persisted_before_cutoff_proven": False,
                 "deepseek_called": False}
        _write_once_json(audit_path, audit)
        results[number] = {"status": "available", "result": result,
                           "reused_existing_receipt": False,
                           "latest_report_among_returned_candidates": True,
                           "candidate_count": len(urls),
                           "selected_candidate_count": len(temp_candidates),
                           "rejected_candidates": rejections}
    return results


TEAM_STAT_COLUMNS = [
    "gameId", "gameDateTimeEst", "gameType", "teamId", "teamName", "opponentTeamId",
    "teamScore", "opponentScore", "assists", "fieldGoalsAttempted", "fieldGoalsMade",
    "fieldGoalsPercentage", "threePointersAttempted", "threePointersMade",
    "threePointersPercentage", "freeThrowsAttempted", "freeThrowsMade",
    "freeThrowsPercentage", "reboundsTotal", "reboundsOffensive", "reboundsDefensive",
    "turnovers", "foulsPersonal", "home",
]


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if pd.notna(result) else None


def _load_team_stat_rows(path: Path = TEAM_STATISTICS_CSV) -> tuple[dict[int, list[dict[str, Any]]], str]:
    if not path.is_file():
        raise FileNotFoundError("historical_team_statistics_csv_missing")
    frame = pd.read_csv(path, usecols=TEAM_STAT_COLUMNS, low_memory=False)
    result: dict[int, list[dict[str, Any]]] = {}
    for raw in frame.to_dict("records"):
        if str(raw.get("gameType")) != "Regular Season":
            continue
        try:
            game_date = datetime.fromisoformat(str(raw["gameDateTimeEst"])).date().isoformat()
            team_id = int(raw["teamId"])
        except (KeyError, TypeError, ValueError):
            continue
        # Keep only the stat fields used below; target/future score columns are
        # never copied into the per-game history objects.
        item = {"game_id": f"{int(raw['gameId']):010d}", "date": game_date,
                "team_id": team_id, "team_name": str(raw.get("teamName") or ""),
                "home": str(raw.get("home") or "").casefold() in {"true", "1", "yes"}}
        for key in TEAM_STAT_COLUMNS:
            if key in {"gameId", "gameDateTimeEst", "gameType", "teamId", "teamName", "home"}:
                continue
            item[key] = _finite(raw.get(key))
        result.setdefault(team_id, []).append(item)
    for rows in result.values():
        rows.sort(key=lambda row: (row["date"], row["game_id"]))
    return result, _sha256_file(path)


def _team_window(rows: Sequence[Mapping[str, Any]], cutoff_date: str, count: int) -> list[Mapping[str, Any]]:
    prior = [row for row in rows if str(row["date"]) < cutoff_date]
    return prior[-count:]


def _window_summary(rows: Sequence[Mapping[str, Any]], window: int) -> dict[str, Any]:
    selected = list(rows[-window:])
    if not selected:
        return {"status": "unavailable", "games": 0, "reason": "no prior regular-season games"}

    def mean(field: str) -> float | None:
        values = [float(row[field]) for row in selected if row.get(field) is not None]
        return round(sum(values) / len(values), 3) if values else None

    points_for = mean("teamScore")
    points_against = mean("opponentScore")
    margins = [float(row["teamScore"]) - float(row["opponentScore"])
               for row in selected if row.get("teamScore") is not None
               and row.get("opponentScore") is not None]
    wins = sum(float(row["teamScore"]) > float(row["opponentScore"])
               for row in selected if row.get("teamScore") is not None
               and row.get("opponentScore") is not None)
    losses = sum(float(row["teamScore"]) < float(row["opponentScore"])
                 for row in selected if row.get("teamScore") is not None
                 and row.get("opponentScore") is not None)
    possessions = []
    for row in selected:
        values = [row.get("fieldGoalsAttempted"), row.get("freeThrowsAttempted"),
                  row.get("reboundsOffensive"), row.get("turnovers")]
        if all(value is not None for value in values):
            possessions.append(float(values[0]) + 0.44 * float(values[1])
                               - float(values[2]) + float(values[3]))
    off_rating = None
    if possessions and points_for is not None and sum(possessions) > 0:
        total_points = sum(float(row["teamScore"]) for row in selected
                           if row.get("teamScore") is not None)
        off_rating = round(total_points / sum(possessions) * 100, 3)
    return {
        "status": "available" if len(selected) >= window else "partial",
        "window_games_requested": window,
        "games": len(selected),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / (wins + losses), 3) if wins + losses else None,
        "points_for_per_game": points_for,
        "points_against_per_game": points_against,
        "average_margin": round(sum(margins) / len(margins), 3) if margins else None,
        "field_goal_percentage": mean("fieldGoalsPercentage"),
        "three_point_percentage": mean("threePointersPercentage"),
        "free_throw_percentage": mean("freeThrowsPercentage"),
        "rebounds_per_game": mean("reboundsTotal"),
        "assists_per_game": mean("assists"),
        "turnovers_per_game": mean("turnovers"),
        "estimated_possessions_per_game": round(sum(possessions) / len(possessions), 3) if possessions else None,
        "estimated_offensive_rating": off_rating,
        "source_game_ids": [row["game_id"] for row in selected],
        "content_dates": [row["date"] for row in selected],
        "boundary": "all source games have local calendar dates strictly before the target game date",
    }


def _build_team_form(packet: Mapping[str, Any], schedule: Mapping[str, Any],
                     team_stats: Mapping[int, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    target = _packet_target(packet)
    fields = {}
    for side in ("home", "away"):
        team_id = int(schedule[f"{side}_team_id"])
        prior = [row for row in team_stats.get(team_id, ()) if row["date"] < schedule["date"]]
        last5 = _window_summary(prior, 5)
        last15 = _window_summary(prior, 15)
        # Current-season record is a separate sample; previous-season form
        # remains available for early-season games without being called current.
        current = [row for row in prior if row["date"] >= "2025-10-21"]
        current_summary = _window_summary(current, max(1, len(current))) if current else {
            "status": "unavailable", "games": 0, "reason": "no target-season game completed before cutoff"}
        fields[side] = {
            "team": _team_abbr(target[side]),
            "target_season_to_date": current_summary,
            "last5_regular_across_available_history": last5,
            "last15_regular_across_available_history": last15,
        }
    statuses = [fields[side]["last5_regular_across_available_history"]["status"]
                for side in ("home", "away")]
    overall = "available" if all(value == "available" for value in statuses) else "partial"
    return {
        "status": overall,
        "regular_season_only_prior_form": fields,
        "metric_semantics": {
            "points_for_against_and_boxscore_averages": "per-game means of NBA TeamStatistics rows before target date",
            "estimated_possessions": "FGA + 0.44*FTA - offensive rebounds + turnovers; approximation, not official pace",
            "estimated_offensive_rating": "total points divided by estimated possessions times 100",
            "capture_relation": "historical dataset read after target cutoff; source events strictly precede target date",
        },
        "data_source": "courtvision/data/raw/TeamStatistics.csv",
        "data_classification": "structured_calculated_fact",
        "historical_reconstruction": True,
        "point_in_time_snapshot_verified": False,
    }


def _minutes_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if result >= 0 else None
    text = str(value).strip()
    if ":" in text:
        try:
            minutes, seconds = text.split(":", 1)
            return float(minutes) + float(seconds) / 60
        except ValueError:
            return None
    try:
        result = float(text)
        return result if result >= 0 else None
    except ValueError:
        return None


def _load_local_player_samples() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    manifest_path = NBA_V2_DATA / "source_manifest.json"
    players_path = NBA_V2_DATA / "normalized" / "player_boxscores.json"
    if not manifest_path.is_file() or not players_path.is_file():
        return {}, {"status": "unavailable", "reason": "local player boxscore sample files missing"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    player_rows = json.loads(players_path.read_text(encoding="utf-8"))
    game_rows = manifest.get("selected_games") or []
    raw_index = {str(row.get("path")): row for row in manifest.get("raw_files") or []}
    samples: dict[str, dict[str, Any]] = {}
    for game in game_rows:
        game_id = str(game.get("game_id") or "")
        date_value = str(game.get("date") or "")[:10]
        if not game_id or not date_value:
            continue
        raw_rel = f"data/raw/nba_stats/{game_id}_boxscore_traditional_v3.json"
        raw_meta = raw_index.get(raw_rel)
        raw_path = NBA_V2_DATA / "raw" / "nba_stats" / f"{game_id}_boxscore_traditional_v3.json"
        if not raw_meta or not raw_path.is_file() or _sha256_file(raw_path) != raw_meta.get("sha256"):
            continue
        team_players: dict[str, list[dict[str, Any]]] = {}
        team_ids: dict[str, int] = {}
        for row in player_rows:
            if str(row.get("game_id")) != game_id:
                continue
            team = _team(row.get("team_tricode"))
            try:
                team_ids[team] = int(row["team_id"])
            except (KeyError, TypeError, ValueError):
                continue
            minutes = _minutes_value(row.get("minutes"))
            team_players.setdefault(team, []).append({
                "player_id": str(row.get("person_id") or ""),
                "player_name": str(row.get("full_name") or ""),
                "starter": row.get("starter") if isinstance(row.get("starter"), bool) else None,
                "minutes": round(minutes, 3) if minutes is not None else None,
            })
        for values in team_players.values():
            values.sort(key=lambda item: (not bool(item["starter"]), -(item["minutes"] or 0), item["player_id"]))
        samples[game_id] = {
            "game_id": game_id, "date": date_value,
            "home": str(game.get("home_tricode") or "").casefold(),
            "away": str(game.get("away_tricode") or "").casefold(),
            "teams": team_players,
            "team_ids": team_ids,
            "source_sha256": raw_meta["sha256"],
            "source_name": "NBA Stats BoxScoreTraditionalV3 via nba_api",
            "source_capture_relation": "later_acquired_historical_data",
        }
    return samples, {
        "status": "available" if samples else "unavailable",
        "sampled_game_count": len(samples),
        "player_boxscore_rows": len(player_rows),
        "source_manifest_generated_at": manifest.get("generated_at"),
        "normalized_player_boxscore_sha256": _sha256_file(players_path),
        "selected_game_ids": sorted(samples),
        "limitation": "three-game sample only; not a season-complete player log or roster",
    }


def _team_last5_game_ids(schedule_history: Mapping[int, Sequence[Mapping[str, Any]]],
                         team_id: int, target_date: str) -> set[str]:
    games = [row for row in schedule_history.get(team_id, ()) if row["date"] < target_date]
    return {row["game_id"] for row in sorted(games, key=lambda r: (r["date"], r["game_id"]))[-5:]}


def _build_player_usage_context(
    packet: Mapping[str, Any],
    schedule: Mapping[str, Any],
    schedule_history: Mapping[int, Sequence[Mapping[str, Any]]],
    player_samples: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    target = _packet_target(packet)
    sides = {}
    for side in ("home", "away"):
        team_id = int(schedule[f"{side}_team_id"])
        last5_ids = _team_last5_game_ids(schedule_history, team_id, schedule["date"])
        last15_ids = {row["game_id"] for row in sorted(
            (row for row in schedule_history.get(team_id, ()) if row["date"] < schedule["date"]),
            key=lambda row: (row["date"], row["game_id"]))[-15:]}
        sampled = []
        for game_id, sample in player_samples.items():
            if game_id not in last15_ids:
                continue
            team_code = next((code for code, source_team_id in sample.get("team_ids", {}).items()
                              if int(source_team_id) == team_id), None)
            if team_code:
                sampled.append((sample, team_code))
        # The three saved samples include only selected NBA games. This object
        # reports observed prior appearances; it never extrapolates a target
        # starter or calls a partial sample a complete rotation.
        items = []
        for sample, team_code in sorted(sampled, key=lambda item: (item[0]["date"], item[0]["game_id"])):
            team_rows = sample["teams"].get(team_code, [])
            if not team_rows:
                continue
            items.append({
                "game_id": sample["game_id"], "game_date": sample["date"],
                "team": team_code, "target_team_side": side, "players": team_rows,
                "starter_field_source": "NBA Stats BoxScoreTraditionalV3 player starter flag",
                "minutes_source": "NBA Stats BoxScoreTraditionalV3 player minutes",
                "source_sha256": sample["source_sha256"],
                "content_date_before_target": True,
                "captured_before_target_cutoff": False,
                "window_relation": "last5" if sample["game_id"] in last5_ids else "last15_only",
            })
        sides[side] = {
            "status": "partial" if items else "unavailable",
            "last5_team_game_ids": sorted(last5_ids),
            "last15_team_game_ids": sorted(last15_ids),
            "available_sample_games_last5": sum(item["window_relation"] == "last5" for item in items),
            "available_sample_games_last15": len(items),
            "requested_sample_games_for_complete_window": 5,
            "prior_game_samples": items,
            "target_expected_starters": "unavailable",
            "target_rotation_projection": "unavailable",
            "reason": "saved player boxscores cover only three league games; current-roster or season-end data excluded",
        }
    available = sum(sides[s]["available_sample_games_last15"] for s in ("home", "away"))
    full = all(sides[s]["available_sample_games_last5"] >= 5 for s in ("home", "away"))
    return {
        "status": "available" if full else "partial" if available else "unavailable",
        "classification": "structured_calculated_fact" if available else "unknown",
        "teams": sides,
        "full_five_game_starter_window_available": full,
        "full_five_game_rotation_window_available": full,
        "historical_reconstruction": True,
        "point_in_time_snapshot_verified": False,
    }


def _load_schedule_history() -> tuple[dict[int, list[dict[str, Any]]], str]:
    columns = ["gameId", "gameDateTimeEst", "hometeamId", "awayteamId", "gameType"]
    frame = pd.read_csv(GAMES_CSV, usecols=columns, low_memory=False)
    history: dict[int, list[dict[str, Any]]] = {}
    for raw in frame.to_dict("records"):
        if str(raw.get("gameType")) != "Regular Season":
            continue
        try:
            game_date = datetime.fromisoformat(str(raw["gameDateTimeEst"])).date().isoformat()
            game_id = f"{int(raw['gameId']):010d}"
            home_id, away_id = int(raw["hometeamId"]), int(raw["awayteamId"])
        except (KeyError, TypeError, ValueError):
            continue
        for team_id in (home_id, away_id):
            history.setdefault(team_id, []).append({"game_id": game_id, "date": game_date})
    for rows in history.values():
        rows.sort(key=lambda item: (item["date"], item["game_id"]))
    return history, _sha256_file(GAMES_CSV)


def _information_classification(packet: Mapping[str, Any], player_context: Mapping[str, Any]) -> dict[str, Any]:
    injuries = packet.get("injuries") or (packet.get("pregame_context") or {}).get("injury") or {}
    news = packet.get("pregame_news") or (packet.get("pregame_context") or {}).get("news") or {}
    richer = packet.get("richer_team_data")
    return {
        "source_supported_facts": {
            "schedule_identity_and_cutoff": "available",
            "official_injury_and_availability": _status_from_value(injuries),
            "pregame_news_and_comments": _status_from_value(news),
        },
        "structured_calculated_facts": {
            "prior_team_form_and_boxscore_metrics": _team_form_status(richer),
            "prior_game_starter_and_minutes_samples": player_context.get("status", "unavailable"),
        },
        "inference": [],
        "unconfirmed": [
            "full official roster membership at the target cutoff",
            "target-game expected or confirmed starters unless separately sourced",
            "target-game projected rotation and player-minute limits unless separately sourced",
            "strict same-time project capture for retrospective source material",
        ],
    }


def _status_from_value(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "available" if value else "unavailable"
    status = str(value.get("status") or "").casefold()
    if status in {"available", "partial", "unavailable"}:
        return status
    if status in {"available_historical_archive", "available_official_historical_snapshot"}:
        return "available"
    return "partial" if value else "unavailable"


def _team_form_status(value: Any) -> str:
    """Read both new team-stat sections and the five legacy enriched packets."""
    if not isinstance(value, Mapping):
        return "unavailable"
    explicit = str(value.get("status") or "").casefold()
    if explicit in {"available", "partial", "unavailable"}:
        return explicit

    # The original five packets predate the top-level `status` field. Their
    # regular-season windows are explicit and date-cutoff safe, so classify
    # from the two teams' recorded sample counts instead of treating the whole
    # mapping as an opaque partial blob.
    windows = value.get("regular_season_only_prior_form")
    if not isinstance(windows, Mapping):
        return "partial" if value else "unavailable"
    games: list[tuple[int, int]] = []
    for side in ("home", "away"):
        side_data = windows.get(side)
        if not isinstance(side_data, Mapping):
            return "unavailable"
        last5 = side_data.get("last5_regular")
        last15 = side_data.get("last15_regular")
        if not isinstance(last5, Mapping) or not isinstance(last15, Mapping):
            return "partial"
        try:
            games.append((int(last5.get("games") or 0), int(last15.get("games") or 0)))
        except (TypeError, ValueError):
            return "partial"
    if all(last5 >= 5 and last15 >= 15 for last5, last15 in games):
        return "available"
    if any(last5 > 0 or last15 > 0 for last5, last15 in games):
        return "partial"
    return "partial"  # season-opening cold start: the windows are known, but empty


def _source_classification(source: Mapping[str, Any]) -> str:
    existing = str(source.get("classification") or "").casefold()
    if existing in {"source_fact", "structured_calculated_fact", "inference", "unconfirmed"}:
        return existing
    information_type = str(source.get("information_type") or "").casefold()
    if information_type.startswith("structured_"):
        return "structured_calculated_fact"
    if information_type.startswith("official_"):
        return "source_fact"
    if source.get("usable_for_prediction") is True and source.get("published_before_tipoff") is True:
        return "source_fact"
    return "unconfirmed"


def _source_classification_counts(packet: Mapping[str, Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for source in packet.get("sources") or []:
        if isinstance(source, Mapping):
            counts[_source_classification(source)] += 1
    return dict(counts)


def _injury_source(result: Mapping[str, Any]) -> dict[str, Any]:
    issue = str(result["source_report_time_local"])
    return {
        "source_name": "NBA Official Injury Report",
        "title": f"NBA Injury Report, {issue}",
        "url": result["source_url"],
        "published_at": result["source_published_at_utc"],
        "information_type": "official_injury_and_availability",
        "summary": (
            f"Official PDF issue time and matchup were parsed and checked against this game. "
            f"It contains {result['record_count']} player availability rows; statuses and reasons are stored in the injuries section."
        ),
        "published_before_tipoff": True,
        "usable_for_prediction": True,
        "captured_at": result["captured_at_utc"],
        "capture_relation_to_cutoff": result["capture_relation_to_cutoff"],
        "historical_reconstruction": True,
        "persisted_before_cutoff_proven": False,
        "raw_sha256": result["raw_sha256"],
        "classification": "source_fact",
    }


def _team_statistics_source(path_hash: str, captured_at: str, cutoff_date: str) -> dict[str, Any]:
    return {
        "source_name": "NBA TeamStatistics.csv",
        "title": "Prior regular-season team boxscore history",
        "url": None,
        "published_at": None,
        "information_type": "structured_historical_team_statistics",
        "summary": ("Deterministic last-five/last-fifteen team scoring, efficiency and boxscore aggregates; "
                    "only regular-season rows with game dates strictly earlier than this target date were used."),
        "published_before_tipoff": None,
        "content_time_before_cutoff": True,
        "usable_for_prediction": True,
        "captured_at": captured_at,
        "capture_relation_to_cutoff": "later_acquired_historical_data",
        "historical_reconstruction": True,
        "point_in_time_snapshot_verified": False,
        "cutoff_exclusive_date": cutoff_date,
        "raw_sha256": path_hash,
        "classification": "structured_calculated_fact",
    }


def _prior_player_source(sample: Mapping[str, Any], captured_at: str) -> dict[str, Any]:
    game_date = sample.get("date") or sample.get("game_date")
    return {
        "source_name": "NBA Stats BoxScoreTraditionalV3 via nba_api",
        "title": f"Prior-game player starter and minutes records, {game_date}",
        "url": f"https://stats.nba.com/game/{sample['game_id']}/box-score",
        "published_at": None,
        "information_type": "structured_prior_game_starters_and_minutes",
        "summary": ("Starter flags and minutes from one completed game before the target date. "
                    "This is a prior-game observation, not a target-game lineup projection or full roster."),
        "published_before_tipoff": None,
        "content_time_before_cutoff": True,
        "usable_for_prediction": True,
        "captured_at": captured_at,
        "capture_relation_to_cutoff": "later_acquired_historical_data",
        "historical_reconstruction": True,
        "point_in_time_snapshot_verified": False,
        "classification": "structured_calculated_fact",
        "raw_sha256": sample.get("source_sha256"),
    }


def _append_source(packet: dict[str, Any], source: Mapping[str, Any]) -> None:
    values = packet.get("sources")
    if not isinstance(values, list):
        values = []
        packet["sources"] = values
    key = (source.get("source_name"), source.get("url"), source.get("published_at"))
    if not any((item.get("source_name"), item.get("url"), item.get("published_at")) == key
               for item in values if isinstance(item, Mapping)):
        values.append(dict(source))


def _set_cutoff_on_packet(
    packet: dict[str, Any], schedule: Mapping[str, Any], schedule_validation: Mapping[str, Any]
) -> None:
    target = _packet_target(packet)
    cutoff_aware = datetime.fromisoformat(str(schedule["cutoff_aware"]))
    if "target" in packet:
        target["official_nba_game_id"] = schedule["game_id"]
        target["tipoff"] = {
            "status": schedule["tipoff_status"],
            "value": schedule["scheduled_tipoff_aware"] if schedule["tipoff_status"] == "available" else None,
            "listed_game_time_et": schedule["scheduled_local_naive"].split("T", 1)[1],
            "timezone": "America/New_York",
            "actual_tipoff_timestamp": None,
            "actual_tipoff_verified": False,
            "time_semantics": schedule["tipoff_time_semantics"],
        }
        target["source_eligibility_cutoff"] = schedule["cutoff_aware"]
        target["source_eligibility_cutoff_basis"] = schedule["cutoff_basis"]
    else:
        target["official_nba_game_id"] = schedule["game_id"]
        target.setdefault("exact_actual_tipoff_timestamp", None)
        target["scheduled_tipoff_estimate_by_NBA_plus_10_rule"] = (
            datetime.fromisoformat(schedule["scheduled_tipoff_aware"]) .isoformat()
        )
        target["source_eligibility_cutoff"] = cutoff_aware.isoformat()
        target["source_eligibility_cutoff_basis"] = schedule["cutoff_basis"]
    validation = dict(schedule_validation)
    if validation.get("status") == "time_conflict_conservative_cutoff":
        validation.pop("source", None)
    packet.setdefault("historical_enrichment", {})
    packet["historical_enrichment"].update({
        "historical_reconstruction": True,
        "point_in_time_snapshot_verified": False,
        "game_identity_verified": True,
        "official_nba_game_id": schedule["game_id"],
        "scheduled_tipoff_aware": schedule["scheduled_tipoff_aware"],
        "historical_database_schedule_time": schedule["scheduled_local_naive"],
        "conservative_information_cutoff": schedule["cutoff_aware"],
        "cutoff_basis": schedule["cutoff_basis"],
        "cutoff_status": schedule["cutoff_status"],
        "actual_tipoff_verified": False,
        "schedule_validation": validation,
    })


def _merge_game_enrichment(
    packet: Mapping[str, Any],
    schedule: Mapping[str, Any],
    schedule_validation: Mapping[str, Any],
    *,
    injury_result: Mapping[str, Any],
    team_form: Mapping[str, Any],
    team_stats_source: Mapping[str, Any],
    player_context: Mapping[str, Any],
    player_samples: Mapping[str, Mapping[str, Any]],
    player_capture_time: str | None,
    captured_at: str,
) -> dict[str, Any]:
    """Add historically gated evidence while retaining every existing field."""
    result = json.loads(json.dumps(packet, ensure_ascii=False))
    _verify_packet(result)
    _set_cutoff_on_packet(result, schedule, schedule_validation)
    number = _game_number_packet(result)
    enriched_schema = "game" in result
    target = _packet_target(result)
    cup = CUP_SCHEDULE.get(number)
    if cup and cup.get("cup_stage"):
        target["special_competition"] = {
            "name": "Emirates NBA Cup", "stage": cup["cup_stage"],
            "site": cup.get("site"), "source_basis": "official post-group-play NBA schedule update",
        }

    if not enriched_schema:
        result["richer_team_data"] = dict(team_form)
        result["richer_team_data"].update({
            "team_statistics_sha256": team_stats_source.get("raw_sha256"),
            "captured_at": team_stats_source.get("captured_at"),
            "capture_relation_to_cutoff": "later_acquired_historical_data",
        })
        result.setdefault("sources", [])
        _append_source(result, team_stats_source)

        if injury_result.get("status") == "available":
            report = injury_result["result"]
            records = [{key: record.get(key) for key in (
                "team_id", "team_name", "player_name", "availability_status", "reason", "source_record_id")}
                for record in report.get("records") or []]
            result["injuries"] = {
                "status": "available", "source_status": "available_historical_archive",
                "report_time_local": report.get("source_report_time_local"),
                "published_at": report.get("source_published_at_utc"),
                "records": records,
                "reason": "Official PDF issue time and target matchup validated before the conservative cutoff.",
                "historical_reconstruction": True,
                "persisted_before_cutoff_proven": False,
                "classification": "source_fact",
            }
            _append_source(result, _injury_source(report))
        else:
            result["injuries"] = {
                "status": "unavailable",
                "reason": str(injury_result.get("reason") or
                              "no official target-matched PDF with a verified pre-cutoff issue time"),
                "classification": "unknown", "historical_reconstruction": True,
            }

        prior_samples = []
        for side in ("home", "away"):
            for sample in player_context.get("teams", {}).get(side, {}).get("prior_game_samples", []):
                prior_samples.append((side, sample))
                _append_source(result, _prior_player_source(
                    sample, player_capture_time or captured_at))
        if prior_samples:
            result["pregame_context"] = result.get("pregame_context") or {}
            result["pregame_context"]["roster"] = {
                "status": "partial",
                "basis": "players observed in selected prior-game boxscores only",
                "players": sorted({player["player_name"] for _, sample in prior_samples
                                   for player in sample.get("players", [])}),
                "official_roster_completeness": "unverified",
                "not_target_game_lineup": True,
            }
        else:
            result.setdefault("pregame_context", {}).setdefault(
                "roster", {"status": "unavailable", "reason": "no eligible target-date historical roster source"})

        usage_items = []
        for side, sample in prior_samples:
            usage_items.append({
                "team": side, "game_date": sample["game_date"], "game_id": sample["game_id"],
                "players": sample["players"],
                "interpretation": "observed prior-game starters and minutes; not a target-game lineup or rotation projection",
                "classification": "structured_calculated_fact",
            })
        result["rotation_notes"] = ({
            "status": "partial", "items": usage_items, "target_game_rotation": "unavailable",
            "reason": "The local sample covers only selected prior games, not a complete rolling five-game window.",
        } if usage_items else {
            "status": "unavailable", "items": [], "target_game_rotation": "unavailable",
            "reason": "No recent player game-log sequence is saved for this target team pair.",
        })
        result["starters"] = {
            "status": "unavailable", "expected": [], "confirmed": [],
            "reason": "No target-game expected or announced starters with verified pregame timing.",
        }
        result["roster_changes"] = {
            "status": "unavailable", "items": [],
            "reason": "No historical team-membership or transaction source verified for this cutoff.",
        }
        result["pregame_news"] = {
            "status": "unavailable", "items": [],
            "reason": "No additional target-specific pregame article passed publication-time and full-content checks.",
        }
        result["coach_player_comments"] = {
            "status": "unavailable", "items": [],
            "reason": "No new coach/player comment with verified pregame publication time was selected.",
        }
        result["pregame_context"] = result.get("pregame_context") or {}
        result["pregame_context"]["injury"] = result["injuries"]
        result["pregame_context"]["starters"] = result["starters"]
        result["pregame_context"]["rotation"] = result["rotation_notes"]
        result["pregame_context"]["news"] = result["pregame_news"]
        if number in CUP_SCHEDULE:
            result["important_context"] = result.get("important_context") or []
            if not isinstance(result["important_context"], list):
                result["important_context"] = []
            stage = CUP_SCHEDULE[number].get("cup_stage")
            if stage:
                result["important_context"].append(
                    f"Official schedule update identifies the target as an NBA Cup {stage}; this is prior-to-tip schedule context."
                )
            elif number == 27:
                result["important_context"].append(
                    "An official same-day pregame preview confirms the corrected Clippers-Thunder matchup; the exact scheduled tipoff time remains partially verified."
                )

    # Attach only eligible schedule records to the model-facing source list.
    # Conflicting initial schedule times remain in the separate completeness
    # audit and are not passed as facts to the forecaster.
    for source in _source_for_schedule(number, schedule, captured_at):
        if source.get("usable_for_prediction") is True:
            _append_source(result, source)
    for source in result.get("sources") or []:
        if isinstance(source, dict):
            source["classification"] = _source_classification(source)
    provenance = result.get("data_provenance")
    if not isinstance(provenance, dict):
        provenance = {}
        result["data_provenance"] = provenance
    provenance.update({
        "historical_reconstruction": True,
        "point_in_time_snapshot_verified": False,
        "source_capture_timestamp": None,
        "enrichment_captured_at": captured_at,
    })
    result["information_classification"] = _information_classification(result, player_context)
    result.setdefault("historical_enrichment", {})
    result["historical_enrichment"].update({
        "input_tier": "preserved_enriched_pilot" if enriched_schema else "historical_dataset_plus_verified_archive",
        "injury_research": {
            "status": injury_result.get("status"),
            "reason": injury_result.get("reason"),
            "source_url": ((injury_result.get("result") or {}).get("source_url")
                           if isinstance(injury_result.get("result"), Mapping) else None),
            "source_published_at": ((injury_result.get("result") or {}).get("source_published_at_utc")
                                    if isinstance(injury_result.get("result"), Mapping) else None),
            "record_count": ((injury_result.get("result") or {}).get("record_count")
                             if isinstance(injury_result.get("result"), Mapping) else None),
            "rejected_candidate_count": len(injury_result.get("rejected_candidates") or []),
        },
        "team_statistics_source_sha256": team_stats_source.get("raw_sha256"),
        "prior_player_sample_status": player_context.get("status"),
    })
    result["packet_sha256"] = packet_content_sha256(result)
    _verify_packet(result)
    if _contains_forbidden(result):
        raise ValueError(f"postgame_field_in_historical_enriched_packet:game_{number:03d}")
    return result

def _safe_injury_search_status(value: Mapping[str, Any]) -> dict[str, Any]:
    # Search content is intentionally dropped; retain only routing, counts,
    # official archive URLs, and a hash of the response for audit.
    return {
        "status": value.get("status", "unavailable"),
        "provider": value.get("provider"),
        "candidate_count": len(value.get("official_same_date_pdf_candidates") or []),
        "search_error": value.get("search_error"),
        "search_response_sha256": value.get("raw_response_sha256"),
        "search_captured_at": value.get("captured_at"),
    }


def _complete_value(value: Any) -> bool:
    return _status_from_value(value) == "available"


def _prior_sample_counts(player_context: Mapping[str, Any]) -> dict[str, int]:
    teams = player_context.get("teams") if isinstance(player_context.get("teams"), Mapping) else {}
    total_games = sum(int((teams.get(side) or {}).get("available_sample_games_last15", 0))
                      for side in ("home", "away"))
    covered_teams = sum(bool((teams.get(side) or {}).get("available_sample_games_last15", 0))
                        for side in ("home", "away"))
    last5_samples = sum(int((teams.get(side) or {}).get("available_sample_games_last5", 0))
                        for side in ("home", "away"))
    full_window = all(int((teams.get(side) or {}).get("available_sample_games_last5", 0)) >= 5
                      for side in ("home", "away"))
    return {"prior_team_boxscore_samples": total_games,
            "teams_with_any_prior_boxscore_sample": covered_teams,
            "prior_boxscore_samples_within_last5": last5_samples,
            "complete_five_game_lineup_window": int(full_window),
            "complete_five_game_rotation_window": int(full_window)}


def _completeness_row(
    packet: Mapping[str, Any], input_tier: str,
    schedule: Mapping[str, Any], injury_result: Mapping[str, Any],
    player_context: Mapping[str, Any],
) -> dict[str, Any]:
    base = _completeness(packet, input_tier)
    injury_status = "available" if injury_result.get("status") in {
        "available", "preserved_enriched_pilot"} else "unavailable"
    injury_info = injury_result.get("result") if isinstance(injury_result.get("result"), Mapping) else {}
    packet_injuries = packet.get("injuries") if isinstance(packet.get("injuries"), Mapping) else {}
    if "game" in packet and _complete_value(packet_injuries):
        injury_status = "available"
    if injury_result.get("status") == "preserved_enriched_pilot":
        injury_status = "available" if _complete_value(packet_injuries) else "unavailable"
    samples = _prior_sample_counts(player_context)
    full_window = bool(samples["complete_five_game_lineup_window"])
    partial_last5 = bool(samples["prior_boxscore_samples_within_last5"] and not full_window)
    partial_last15 = bool(samples["prior_team_boxscore_samples"])
    schedule_status = schedule.get("tipoff_status", "unavailable")
    fields = dict(base.get("fields") or {})
    fields.update({
        "official_historical_injury": injury_status,
        "full_official_team_roster_at_cutoff": "unavailable",
        "prior_observed_roster_subset": "partial" if partial_last15 else "unavailable",
        "recent_starters_last5_games": "available" if full_window else "partial" if partial_last5 else "unavailable",
        "rotation_minutes_last5_games": "available" if full_window else "partial" if partial_last5 else "unavailable",
        "observed_prior_starter_minutes_in_last15": "partial" if partial_last15 else "unavailable",
        "important_pregame_news": base.get("fields", {}).get("pregame_news", "unavailable"),
        "prior_team_form_last5_last15": _team_form_status(packet.get("richer_team_data"))
            if isinstance(packet.get("richer_team_data"), Mapping) else "unavailable",
        "game_identity_source_verified": "available" if schedule.get("schedule_validation", {}).get("status") else "unavailable",
        "official_listed_tipoff_time": schedule_status,
        "conservative_cutoff_time": "available" if schedule.get("cutoff_aware") else "unavailable",
        "strict_point_in_time_snapshot": "unavailable",
    })
    fields["basic_team_stats"] = "available" if (
        isinstance(packet.get("richer_team_data"), Mapping)
        and packet["richer_team_data"].get("status") == "available") else fields.get("basic_team_stats", "partial")
    missing = [key for key, value in fields.items() if value in {"unavailable", "partial"}]
    return {
        **base,
        "fields": fields,
        "missing_or_partial_fields": missing,
        "official_game_id": schedule.get("game_id"),
        "scheduled_tipoff": schedule.get("scheduled_tipoff_aware"),
        "conservative_cutoff": schedule.get("cutoff_aware"),
        "cutoff_basis": schedule.get("cutoff_basis"),
        "schedule_validation": schedule.get("schedule_validation"),
        "injury_record_count": (injury_info.get("record_count")
                                 or len(injury_info.get("records") or [])),
        **samples,
    }


def _schedule_bundle(
    packets: Sequence[Mapping[str, Any]],
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[str, str],
           dict[int, dict[str, list[str]]], dict[int, list[dict[str, Any]]]]:
    target_rows, target_hashes = _read_games_csv_schedule(packets)
    schedule_history, history_hash = _load_schedule_history()
    schedule_by_number: dict[int, dict[str, Any]] = {}
    validation_by_number: dict[int, dict[str, Any]] = {}
    prior_five_ids: dict[int, dict[str, list[str]]] = {}
    for packet in packets:
        number = _game_number_packet(packet)
        row = dict(target_rows[number])
        validation = _official_schedule_match(number, row)
        cutoff_aware, cutoff_basis, _ = _cutoff_for_game(number, row)
        listed = datetime.fromisoformat(row["scheduled_local_naive"]).replace(tzinfo=EASTERN)
        cutoff = datetime.fromisoformat(cutoff_aware).astimezone(EASTERN)
        affected_time = number in TIME_CONFLICTS or number == 27
        row.update({
            "scheduled_tipoff_aware": listed.isoformat(timespec="seconds"),
            "cutoff_aware": cutoff.isoformat(timespec="seconds"),
            "cutoff_local": cutoff.replace(tzinfo=None).isoformat(timespec="seconds"),
            "cutoff_basis": cutoff_basis,
            "cutoff_status": "available_conservative_historical_cutoff",
            "tipoff_status": "partial" if affected_time else "available",
            "tipoff_time_semantics": (
                "historical scheduled time is conflicting or lacks a clean update receipt; withheld from the prediction input, with an earlier cutoff used"
                if affected_time else
                "official listed game time; NBA schedule PDF notes tip-off is listed time plus 10 minutes"
            ),
            "schedule_validation": validation,
        })
        schedule_by_number[number] = row
        validation_by_number[number] = validation
        prior_five_ids[number] = {
            side: sorted(_team_last5_game_ids(schedule_history, int(row[f"{side}_team_id"]), row["date"]))
            for side in ("home", "away")
        }
        if number in TARGET_GAME_NUMBERS:
            target = _packet_target(packet)
            recorded_id = target.get("official_nba_game_id")
            if recorded_id and str(recorded_id) != row["game_id"]:
                raise ValueError(f"enriched_pilot_official_game_id_mismatch:game_{number:03d}")
            if "game" in packet and target.get("eastern_game_time"):
                if target["eastern_game_time"] != row["scheduled_local_naive"][11:16]:
                    raise ValueError(f"enriched_pilot_pdf_time_anchor_mismatch:game_{number:03d}")
    hashes = {**target_hashes, "schedule_history_sha256": history_hash}
    return schedule_by_number, validation_by_number, hashes, prior_five_ids, schedule_history



def _write_once_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> Path:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    data = buffer.getvalue().encode("utf-8-sig")
    return _write_once_bytes(path, data)


def _existing_run_config(
    output_dir: Path,
    packets: Sequence[Mapping[str, Any]],
    source_hashes: Mapping[str, str],
) -> dict[str, Any]:
    path = output_dir / "run_config.json"
    expected = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "target_team": TARGET_TEAM,
        "season": "2025-26",
        "game_numbers": list(range(1, 83)),
        "base_packet_hashes": {str(packet["packet_id"]): packet["packet_sha256"] for packet in packets},
        "source_hashes": dict(source_hashes),
        "historical_reconstruction": True,
        "deepseek_called": False,
        "forecasting_method_modified": False,
        "public_research_core_modified": False,
    }
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if any(saved.get(key) != value for key, value in expected.items()):
            raise ValueError("historical_enrichment_run_config_mismatch")
        return saved
    value = {**expected, "run_started_at_utc": _now_utc()}
    _write_once_json(path, value)
    return value


def _safe_source_types(packet: Mapping[str, Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for source in packet.get("sources") or []:
        if isinstance(source, Mapping) and source.get("usable_for_prediction") is True:
            counts[str(source.get("information_type") or "unspecified")] += 1
    return dict(counts)


def _field_status_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    names = list(rows[0]["fields"]) if rows else []
    return {name: dict(Counter(str(row["fields"].get(name) or "unavailable") for row in rows))
            for name in names}


def _write_packet_file(packet_dir: Path, packet: Mapping[str, Any]) -> Path:
    path = packet_dir / f"game_{_game_number_packet(packet):03d}.json"
    return _write_once_json(path, packet)


def run_historical_enrichment(
    *,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    base_packet_dir: str | Path = PREFLIGHT_PACKET_DIR,
    search_transport: Any | None = None,
    fetch_bytes_fn=None,
    search_batch_size: int = SEARCH_BATCH_SIZE,
) -> dict[str, Any]:
    """Build 82 history-reconstructed packets, with per-game time and source gates.

    Search receipts and injury source records are immutable. The only model-call
    statement in this artifact is a hard-coded false audit flag.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    packets = _read_target_packets(Path(base_packet_dir))
    if [_game_number_packet(packet) for packet in packets] != list(range(1, 83)):
        raise ValueError("historical_enrichment_requires_ordered_82_packets")
    schedules, schedule_validations, schedule_hashes, last5_by_game, schedule_history = _schedule_bundle(packets)
    team_stats, team_stats_sha = _load_team_stat_rows()
    player_samples, player_source_manifest = _load_local_player_samples()
    input_hashes = {
        **schedule_hashes,
        "team_statistics_sha256": team_stats_sha,
        "player_source_manifest_sha256": _sha256_file(NBA_V2_DATA / "source_manifest.json"),
        "normalized_player_boxscores_sha256": player_source_manifest["normalized_player_boxscore_sha256"],
        "source_snapshot_database_sha256": _sha256_file(SOURCE_SNAPSHOT_DATABASE),
    }
    run_config = _existing_run_config(output, packets, input_hashes)
    run_started_at = str(run_config["run_started_at_utc"])
    for number in range(1, 83):
        schedules[number]["last5_team_game_ids_by_team"] = last5_by_game[number]
        schedules[number]["last5_schedule_game_ids_union"] = sorted(
            set(last5_by_game[number]["home"]) | set(last5_by_game[number]["away"]))
    search_receipts = discover_official_injury_pdfs(
        schedules, output, search_transport=search_transport, batch_size=search_batch_size)
    injury_results = collect_official_injuries(
        schedules, search_receipts, output, fetch_bytes_fn=fetch_bytes_fn)

    capture_stamp = run_started_at
    team_stats_source = _team_statistics_source(team_stats_sha, capture_stamp, "per-target-date")
    final_packets, completeness_rows = [], []
    for packet in packets:
        number = _game_number_packet(packet)
        schedule = schedules[number]
        team_form = _build_team_form(packet, schedule, team_stats)
        player_context = _build_player_usage_context(packet, schedule, schedule_history, player_samples)
        enriched = _merge_game_enrichment(
            packet, schedule, schedule_validations[number],
            injury_result=injury_results[number], team_form=team_form,
            team_stats_source=team_stats_source, player_context=player_context,
            player_samples=player_samples,
            player_capture_time=str(player_source_manifest.get("source_manifest_generated_at") or ""),
            captured_at=capture_stamp,
        )
        # Run the same two-stage input projector before saving to catch answer
        # fields, incomplete projections, and unsupported market text.
        from .forecasting_workflow import prepare_pilot
        basketball_input, market_input, _removed = prepare_pilot(enriched)
        if _contains_forbidden(basketball_input) or _contains_forbidden(market_input):
            raise ValueError(f"postgame_field_in_projected_input:game_{number:03d}")
        _write_packet_file(output / "forecast_packets", enriched)
        tier = "preserved_enriched_pilot" if "game" in packet else "historical_dataset_plus_verified_archive"
        row = _completeness_row(enriched, tier, schedule, injury_results[number], player_context)
        row["packet_sha256"] = enriched["packet_sha256"]
        row["usable_source_types"] = _safe_source_types(enriched)
        row["source_classification_counts"] = _source_classification_counts(enriched)
        row["injury_search"] = _safe_injury_search_status(search_receipts[number])
        row["official_injury_source_status"] = injury_results[number].get("status")
        row["scheduled_identity_source_status"] = schedule["schedule_validation"]["status"]
        row["historical_roster_status"] = "unavailable"
        completeness_rows.append(row)
        final_packets.append(enriched)

    if len(final_packets) != 82:
        raise ValueError("historical_enrichment_packet_count_not_82")
    field_counts = _field_status_counts(completeness_rows)
    injury_count = sum(row["fields"]["official_historical_injury"] == "available" for row in completeness_rows)
    roster_count = sum(row["fields"]["full_official_team_roster_at_cutoff"] == "available" for row in completeness_rows)
    starter_complete_count = sum(row["fields"]["recent_starters_last5_games"] == "available" for row in completeness_rows)
    starter_partial_count = sum(row["fields"]["recent_starters_last5_games"] == "partial" for row in completeness_rows)
    rotation_complete_count = sum(row["fields"]["rotation_minutes_last5_games"] == "available" for row in completeness_rows)
    rotation_partial_count = sum(row["fields"]["rotation_minutes_last5_games"] == "partial" for row in completeness_rows)
    news_count = sum(row["fields"]["important_pregame_news"] == "available" for row in completeness_rows)
    schedule_time_counts = dict(Counter(row["fields"]["official_listed_tipoff_time"] for row in completeness_rows))
    source_capture_counts = {
        "preserved_enriched_pilot_packets": sum(row["input_tier"] == "preserved_enriched_pilot" for row in completeness_rows),
        "official_archived_injury_reports": sum(result.get("status") == "available"
                                                  and not result.get("reused_existing_receipt")
                                                  for result in injury_results.values()),
        "existing_injury_receipts_reused": sum(result.get("status") == "available"
                                                 and result.get("reused_existing_receipt")
                                                 for result in injury_results.values()),
        "nba_stats_player_sample_games": len(player_samples),
        "team_statistics_rows_source": 1,
        "new_general_news_records": 0,
    }
    basic_only = [row["game_number"] for row in completeness_rows
                  if row["fields"]["official_historical_injury"] == "unavailable"
                  and row["fields"]["recent_starters_last5_games"] == "unavailable"
                  and row["fields"]["rotation_minutes_last5_games"] == "unavailable"
                  and row["fields"]["prior_observed_roster_subset"] == "unavailable"
                  and row["fields"]["observed_prior_starter_minutes_in_last15"] == "unavailable"
                  and row["fields"]["important_pregame_news"] == "unavailable"
                  and row["fields"]["full_official_team_roster_at_cutoff"] == "unavailable"]
    schedule_source_counts = dict(Counter(row["scheduled_identity_source_status"] for row in completeness_rows))
    usable_source_types = Counter()
    for row in completeness_rows:
        usable_source_types.update(row.get("usable_source_types") or {})
    summary = {
        "game_count": len(final_packets),
        "input_tiers": dict(Counter(row["input_tier"] for row in completeness_rows)),
        "field_status_counts": field_counts,
        "coverage": {
            "reliable_injury_or_availability_information": injury_count,
            "full_official_roster_at_target_cutoff": roster_count,
            "last_five_starters_reconstructed_complete": starter_complete_count,
            "last_five_starters_reconstructed_partial": starter_partial_count,
            "last_five_rotation_minutes_reconstructed_complete": rotation_complete_count,
            "last_five_rotation_minutes_reconstructed_partial": rotation_partial_count,
            "important_pregame_news": news_count,
            "prior_team_stats_last5_last15": sum(row["fields"]["prior_team_form_last5_last15"] == "available"
                                                 for row in completeness_rows),
            "game_identity_verified": sum(row["fields"]["game_identity_source_verified"] == "available"
                                           for row in completeness_rows),
            "official_scheduled_tipoff_time": dict(Counter(row["fields"]["official_listed_tipoff_time"]
                                                           for row in completeness_rows)),
            "strict_point_in_time_snapshots": sum(row["fields"]["strict_point_in_time_snapshot"] == "available"
                                                  for row in completeness_rows),
        },
        "data_origin_coverage": {
            "structured_historical_reconstruction": {
                "team_form_last5_last15": dict(Counter(
                    row["fields"]["prior_team_form_last5_last15"] for row in completeness_rows)),
                "games_with_any_prior_player_boxscore_sample": sum(
                    row["prior_team_boxscore_samples"] > 0 for row in completeness_rows),
                "complete_last5_starter_windows": starter_complete_count,
                "complete_last5_rotation_windows": rotation_complete_count,
                "local_player_boxscore_games": len(player_samples),
            },
            "historical_web_reconstruction": {
                "reliable_injury_or_availability_information": injury_count,
                "injury_information_from_preserved_enriched_pilot": sum(
                    row["input_tier"] == "preserved_enriched_pilot"
                    and row["fields"]["official_historical_injury"] == "available"
                    for row in completeness_rows),
                "injury_information_from_official_archive_search": sum(
                    result.get("status") == "available" for result in injury_results.values()),
                "important_pregame_news": news_count,
                "full_official_roster_at_target_cutoff": roster_count,
                "strict_point_in_time_snapshots": 0,
            },
        },
        "source_classification_counts": dict(sum(
            (Counter(row.get("source_classification_counts") or {}) for row in completeness_rows),
            Counter(),
        )),
        "source_counts": source_capture_counts,
        "schedule_source_status_counts": schedule_source_counts,
        "usable_source_types": dict(usable_source_types),
        "games_still_relying_on_basic_team_stats_only": basic_only,
        "clearly_insufficient_game_count": len(basic_only),
        "historical_player_data_limitation": {
            "local_nba_player_boxscore_games": len(player_samples),
            "local_player_boxscore_rows": player_source_manifest.get("player_boxscore_rows"),
            "complete_team_last5_player_starter_windows": starter_complete_count,
            "complete_team_last5_rotation_minute_windows": rotation_complete_count,
        },
        "official_report_search_provider": "project FreeSearchMCPTransport; URL allowlist limited to official NBA injury-report PDF archive",
        "web_research_model_calls": 0,
        "deepseek_calls": 0,
    }
    manifest = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "target_team": TARGET_TEAM,
        "season": "2025-26",
        "competition": "NBA regular season, including Cup games counted as regular-season games",
        "selected_game_numbers": list(range(1, 83)),
        "run_started_at_utc": run_started_at,
        "historical_reconstruction": True,
        "point_in_time_snapshot_verified": False,
        "deepseek_called": False,
        "actual_results_read_for_scoring": False,
        "forecasting_method_modified": False,
        "forecasting_skill_modified": False,
        "editorial_skill_loaded": False,
        "public_research_core_modified": False,
        "current_roster_used": False,
        "postgame_sources_used": False,
        "post_event_content_encountered_in_search_results": True,
        "post_event_content_used_as_evidence": False,
        "post_event_content_entered_packets": False,
        "source_hashes": input_hashes,
        "schedule_caveats": {
            "official_august_schedule_pdf_fixed_rows": 80,
            "cup_slots_unassigned_in_august_pdf": 2,
            "historical_database_schedule_conflicts": sorted(TIME_CONFLICTS),
            "game_27_date_update": "official same-day pregame preview; the August PDF listed Dec. 17",
            "actual_tipoff_timestamp_verified": False,
        },
        "summary": summary,
        "packet_hashes": {packet["packet_id"]: packet["packet_sha256"] for packet in final_packets},
        "game_numbers": list(range(1, 83)),
    }
    _write_once_json(output / "manifest.json", manifest)
    _write_once_json(output / "completeness.json", {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "deepseek_called": False,
        "summary": summary,
        "games": completeness_rows,
    })
    scalar_fields = ["game_id", "game_number", "date", "home", "away", "input_tier",
                     "packet_sha256", "official_game_id", "conservative_cutoff",
                     "official_injury_source_status", "scheduled_identity_source_status",
                     "historical_roster_status", "usable_pregame_source_count",
                     "prior_team_boxscore_samples", "prior_boxscore_samples_within_last5",
                     "complete_five_game_lineup_window", "complete_five_game_rotation_window",
                     "missing_or_partial_fields"]
    fieldnames = scalar_fields + list(completeness_rows[0]["fields"]) + [
        "usable_source_types", "source_classification_counts"]
    flat_rows = []
    for row in completeness_rows:
        output_row = {key: row.get(key) for key in scalar_fields if key != "missing_or_partial_fields"}
        output_row["missing_or_partial_fields"] = json.dumps(row.get("missing_or_partial_fields"), ensure_ascii=False)
        for key, value in row["fields"].items():
            output_row[key] = value
        output_row["usable_source_types"] = json.dumps(row.get("usable_source_types"), ensure_ascii=False)
        output_row["source_classification_counts"] = json.dumps(
            row.get("source_classification_counts"), ensure_ascii=False)
        flat_rows.append(output_row)
    _write_once_csv(output / "completeness.csv", flat_rows, fieldnames)
    return {"output_dir": str(output.resolve()), "game_count": len(final_packets),
            "summary": summary, "manifest": manifest, "packets": final_packets}


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--base-packets-dir", default=str(PREFLIGHT_PACKET_DIR))
    parser.add_argument("--search-batch-size", type=int, default=SEARCH_BATCH_SIZE)
    args = parser.parse_args(argv)
    result = run_historical_enrichment(
        output_dir=args.output_dir,
        base_packet_dir=args.base_packets_dir,
        search_batch_size=args.search_batch_size,
    )
    print(json.dumps({"status": "enriched", "deepseek_called": False,
                      "game_count": result["game_count"],
                      "output_dir": result["output_dir"],
                      "coverage": result["summary"]["coverage"],
                      "clearly_insufficient_game_count": result["summary"]["clearly_insufficient_game_count"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
