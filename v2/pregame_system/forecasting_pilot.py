"""Result-isolated OKC Forecasting Pilot.

The packets are retrospective, date-only reconstructions. The source archive
does not provide the receipts needed to certify point-in-time snapshots.
"""
from __future__ import annotations
from pathlib import Path

import argparse
import csv
import hashlib
import json
import math
import marshal
import os
import shutil
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV_PATH = Path(
    str(Path(__file__).resolve().parents[1] / 'data/raw/nba_2008-2026.csv')
)
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "v2" / "pregame_system" / "outputs" / "forecasting_pilot_okc_2025_26"
DEFAULT_CHECKPOINT_DIR = REPOSITORY_ROOT / "v2" / "pregame_system" / "outputs" / "forecasting_checkpoints" / "okc_2025_26_v1"
DEFAULT_PREFLIGHT_DIR = REPOSITORY_ROOT / "v2" / "pregame_system" / "outputs" / "forecasting_preflight_okc_2025_26"
DEFAULT_ENRICHED_PILOT_PACKET_DIR = (
    REPOSITORY_ROOT / "v2" / "pregame_system" / "outputs"
    / "forecasting_pilot_okc_2025_26_two_stage_inputs_20260930" / "forecast_packets"
)
EXPECTED_DATASET_SHA256 = "729EB2EAD85C14AFFBB81AE9A4C611FEE9790B8DD3D1D7824E1DDBA14410A8C4"
TARGET_TEAM = "okc"
TARGET_SEASON_LABEL = "2026"
TARGET_SEASON = "2025-26"
TARGET_GAME_NUMBERS = (1, 21, 41, 61, 82)
PILOT_SCHEMA_VERSION = "25th-second-forecast-packet-v1"
PREDICTION_SCHEMA_VERSION = "25th-second-forecast-set-v2"
EVALUATION_SCHEMA_VERSION = "25th-second-forecast-evaluation-v1"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_TIMEOUT_SECONDS = 40.0
DEEPSEEK_MAX_TOKENS = 2400
FORECAST_WORKFLOW_VERSION = "independent_then_market_review-v1"
CHECKPOINT_SCHEMA_VERSION = "25th-second-pilot-game-checkpoint-v1"
EPSILON = 1e-9

TEAM_NAMES = {
    "atl": "Atlanta Hawks", "bkn": "Brooklyn Nets", "bos": "Boston Celtics",
    "cha": "Charlotte Hornets", "chi": "Chicago Bulls", "cle": "Cleveland Cavaliers",
    "dal": "Dallas Mavericks", "den": "Denver Nuggets", "det": "Detroit Pistons",
    "gsw": "Golden State Warriors", "hou": "Houston Rockets", "ind": "Indiana Pacers",
    "lac": "LA Clippers", "lal": "Los Angeles Lakers", "mem": "Memphis Grizzlies",
    "mia": "Miami Heat", "mil": "Milwaukee Bucks", "min": "Minnesota Timberwolves",
    "nop": "New Orleans Pelicans", "nyk": "New York Knicks", "okc": "Oklahoma City Thunder",
    "orl": "Orlando Magic", "phi": "Philadelphia 76ers", "phx": "Phoenix Suns",
    "por": "Portland Trail Blazers", "sac": "Sacramento Kings", "sas": "San Antonio Spurs",
    "tor": "Toronto Raptors", "uta": "Utah Jazz", "was": "Washington Wizards",
}
TEAM_ALIASES = {"sa": "sas", "pho": "phx", "no": "nop", "noh": "nop",
                "nj": "bkn", "njb": "bkn", "njn": "bkn", "sea": "okc",
                "gs": "gsw", "ny": "nyk", "utah": "uta", "wsh": "was"}
PREDICTION_KEYS = {"winner", "win_probability", "expected_margin", "expected_total"}


def _team(value: Any) -> str:
    code = str(value or "").strip().casefold()
    return TEAM_ALIASES.get(code, code)


def _team_name(code: str) -> str:
    return TEAM_NAMES.get(code, code.upper())


def _number(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError("game_date_invalid") from exc


def _regular(row: Mapping[str, Any]) -> bool:
    return str(row.get("regular", "")).strip().casefold() in {"true", "1", "yes"}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def packet_content_sha256(packet: Mapping[str, Any]) -> str:
    content = dict(packet)
    content.pop("packet_sha256", None)
    return hashlib.sha256(_canonical_json(content)).hexdigest()


def _seal_packet(packet: dict[str, Any]) -> dict[str, Any]:
    packet["packet_sha256"] = packet_content_sha256(packet)
    return packet


def _verify_packet(packet: Mapping[str, Any]) -> None:
    expected = str(packet.get("packet_sha256") or "")
    if len(expected) != 64 or packet_content_sha256(packet) != expected:
        raise ValueError("packet_hash_mismatch")


def _packet_target(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    target = packet.get("target") or packet.get("game")
    if not isinstance(target, Mapping):
        raise ValueError("forecast_game_identity_missing")
    return target


def _game_number(packet: Mapping[str, Any]) -> int:
    number = _packet_target(packet).get("game_number")
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 82:
        raise ValueError("forecast_game_number_invalid")
    return number


def _team_abbr(value: Any) -> str:
    return _team(value.get("abbr") if isinstance(value, Mapping) else value)


def _game_identity(packet: Mapping[str, Any]) -> dict[str, Any]:
    target = _packet_target(packet)
    home, away = _team_abbr(target.get("home")), _team_abbr(target.get("away"))
    game_id = str(target.get("game_id") or packet.get("packet_id") or "")
    if not game_id or not target.get("date") or not home or not away or home == away:
        raise ValueError("forecast_game_identity_invalid")
    if game_id != str(packet.get("packet_id") or ""):
        raise ValueError("forecast_packet_game_identity_mismatch")
    if TARGET_TEAM not in {home, away}:
        raise ValueError("forecast_game_identity_not_okc")
    return {"game_id": game_id, "game_number": _game_number(packet),
            "date": str(target["date"]), "home": home, "away": away}


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _model_settings() -> dict[str, Any]:
    return {
        "model": DEEPSEEK_MODEL,
        "temperature": 0.1,
        "thinking": "disabled",
        "max_tokens": DEEPSEEK_MAX_TOKENS,
        "timeout_seconds": DEEPSEEK_TIMEOUT_SECONDS,
        "workflow": FORECAST_WORKFLOW_VERSION,
    }


def _request_transport_identity(request_fn: Callable[..., Any]) -> dict[str, str]:
    target = getattr(request_fn, "__func__", request_fn)
    code = getattr(target, "__code__", None)
    if code is None:
        target = getattr(type(request_fn), "__call__", target)
        code = getattr(target, "__code__", None)
    module = str(getattr(request_fn, "__module__", type(request_fn).__module__))
    qualname = str(getattr(request_fn, "__qualname__", type(request_fn).__qualname__))
    code_hash = hashlib.sha256(marshal.dumps(code)).hexdigest() if code is not None else "unavailable"
    return {"module": module, "qualname": qualname, "callable_sha256": code_hash}


def _code_hashes() -> dict[str, str]:
    return {
        "pilot_runner_sha256": _file_sha256(Path(__file__).resolve()),
        "forecasting_workflow_sha256": _file_sha256(Path(__file__).with_name("forecasting_workflow.py")),
        "deepseek_transport_sha256": _file_sha256(
            REPOSITORY_ROOT / "v2" / "agent" / "generate_historical_pregame_analysis.py"),
    }


def _read_verified_csv(path: str | Path) -> tuple[list[dict[str, str]], str]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError("historical_market_csv_missing")
    digest = hashlib.sha256(source.read_bytes()).hexdigest().upper()
    if digest != EXPECTED_DATASET_SHA256:
        raise ValueError("historical_market_csv_sha256_mismatch")
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("historical_market_csv_empty")
    return rows, digest


def _normalize_game_numbers(game_numbers: Sequence[int] | None) -> tuple[int, ...]:
    numbers = TARGET_GAME_NUMBERS if game_numbers is None else tuple(game_numbers)
    if not numbers:
        raise ValueError("game_numbers_empty")
    normalized: list[int] = []
    for value in numbers:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 82:
            raise ValueError("game_number_out_of_range")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise ValueError("game_numbers_duplicate")
    return tuple(normalized)


def select_target_games(
    rows: Sequence[Mapping[str, Any]],
    game_numbers: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """Validate the 82-game schedule and select requested regular-season ordinals.

    The default remains the original five-game pilot for compatibility. Pass
    ``range(1, 83)`` to select the complete season.
    """
    requested = _normalize_game_numbers(game_numbers)
    ordered: list[tuple[date, int, Mapping[str, Any]]] = []
    for index, row in enumerate(rows):
        if str(row.get("season", "")).strip() != TARGET_SEASON_LABEL or not _regular(row):
            continue
        home, away = _team(row.get("home")), _team(row.get("away"))
        if TARGET_TEAM not in {home, away}:
            continue
        if home == away:
            raise ValueError("okc_schedule_team_identity_invalid")
        ordered.append((_date(row.get("date")), index, row))
    ordered.sort(key=lambda item: (item[0], item[1]))
    if len(ordered) != 82:
        raise ValueError(f"okc_regular_schedule_count_expected_82_got_{len(ordered)}")
    selected = []
    for number in requested:
        game_date, source_index, row = ordered[number - 1]
        home, away = _team(row.get("home")), _team(row.get("away"))
        favorite = str(row.get("whos_favored", "")).strip().casefold()
        if favorite not in {"home", "away"}:
            favorite = "unavailable"
        spread, total = _number(row.get("spread")), _number(row.get("total"))
        if spread is not None and spread < 0:
            raise ValueError("historical_market_spread_magnitude_negative")
        selected.append({
            "game_id": f"OKC-{TARGET_SEASON}-REG-{number:03d}",
            "game_number": number,
            "source_row_index": source_index,
            "date": game_date.isoformat(),
            "home": home,
            "away": away,
            "favorite_side": favorite,
            "spread_points": spread,
            "total_points": total,
            "moneyline_home": _number(row.get("moneyline_home")),
            "moneyline_away": _number(row.get("moneyline_away")),
        })
    return selected


def _scores(row: Mapping[str, Any]) -> tuple[int, int] | None:
    home, away = _number(row.get("score_home")), _number(row.get("score_away"))
    if home is None or away is None or home == away or not home.is_integer() or not away.is_integer():
        return None
    return int(home), int(away)


def _team_result(row: Mapping[str, Any], team: str) -> str | None:
    scores = _scores(row)
    if scores is None:
        return None
    home_score, away_score = scores
    home, away = _team(row.get("home")), _team(row.get("away"))
    if team == home:
        return "W" if home_score > away_score else "L"
    if team == away:
        return "W" if away_score > home_score else "L"
    return None


def _team_games_before(
    rows: Sequence[Mapping[str, Any]], team: str, target_date: date
) -> list[Mapping[str, Any]]:
    code = _team(team)
    prior = [
        row for row in rows
        if _regular(row)
        and str(row.get("season", "")).strip() == TARGET_SEASON_LABEL
        and _date(row.get("date")) < target_date
        and code in {_team(row.get("home")), _team(row.get("away"))}
    ]
    return sorted(prior, key=lambda row: _date(row.get("date")))


def _team_state_before(
    rows: Sequence[Mapping[str, Any]], team: str, target_date: date
) -> dict[str, Any]:
    prior = _team_games_before(rows, team, target_date)
    valid = [(row, result) for row in prior
             if (result := _team_result(row, _team(team))) is not None]
    wins = sum(result == "W" for _, result in valid)
    losses = sum(result == "L" for _, result in valid)
    recent = list(reversed(valid[-5:]))
    games = []
    for row, result in recent:
        home, away, code = _team(row.get("home")), _team(row.get("away")), _team(team)
        opponent = away if home == code else home
        games.append({
            "date": _date(row.get("date")).isoformat(),
            "opponent": {"abbr": opponent, "name": _team_name(opponent)},
            "location": "home" if home == code else "away",
            "prior_game_result": result,
        })
    return {
        "team": {"abbr": _team(team), "name": _team_name(_team(team))},
        "record": {
            "status": "available" if len(valid) == len(prior) else "partial",
            "games_played": len(valid),
            "scheduled_games_before_target_date": len(prior),
            "wins": wins,
            "losses": losses,
            "win_percentage": wins / len(valid) if valid else None,
        },
        "recent_form": {
            "status": "available" if games else "unavailable",
            "window": "up to last five regular-season team games before target date",
            "games": games,
            "record": {
                "wins": sum(result == "W" for _, result in recent),
                "losses": sum(result == "L" for _, result in recent),
                "sample_size": len(recent),
            },
            "unresolved_prior_results": len(prior) - len(valid),
        },
    }


def _rest_before(
    rows: Sequence[Mapping[str, Any]], team: str, target_date: date
) -> dict[str, Any]:
    prior = _team_games_before(rows, team, target_date)
    if not prior:
        return {"status": "unavailable", "reason": "no prior game in current season",
                "days_since_previous_game": None, "rest_days": None, "back_to_back": None}
    last = max(_date(row.get("date")) for row in prior)
    gap = (target_date - last).days
    return {
        "status": "available",
        "previous_game_date": last.isoformat(),
        "days_since_previous_game": gap,
        "rest_days": max(gap - 1, 0),
        "back_to_back": gap == 1,
        "date_granularity": "calendar days; source has no tipoff timestamp",
    }


def _h2h_before(
    rows: Sequence[Mapping[str, Any]], opponent: str, target_date: date
) -> dict[str, Any]:
    okc, other = TARGET_TEAM, _team(opponent)
    meetings = [
        row for row in rows
        if _regular(row)
        and _date(row.get("date")) < target_date
        and {_team(row.get("home")), _team(row.get("away"))} == {okc, other}
    ]
    meetings.sort(key=lambda row: _date(row.get("date")), reverse=True)
    games = []
    for row in meetings[:5]:
        result = _team_result(row, okc)
        if result is not None:
            games.append({
                "date": _date(row.get("date")).isoformat(),
                "okc_location": "home" if _team(row.get("home")) == okc else "away",
                "prior_game_result_for_okc": result,
            })
    wins = sum(game["prior_game_result_for_okc"] == "W" for game in games)
    losses = sum(game["prior_game_result_for_okc"] == "L" for game in games)
    return {
        "status": "available" if games else "unavailable",
        "scope": "prior regular-season meetings only, before target calendar date",
        "meetings_found": len(meetings),
        "meetings_shown": len(games),
        "record_for_okc": {"wins": wins, "losses": losses},
        "recent_meetings": games,
    }


def _phase(number: int) -> dict[str, str]:
    label = "early" if number <= 20 else "mid" if number <= 60 else "late"
    return {"label": label, "basis": "OKC regular-season game ordinal",
            "rule": "early 1-20, mid 21-60, late 61-82"}


def _market(target: Mapping[str, Any]) -> dict[str, Any]:
    favorite = target.get("favorite_side")
    spread, total = target.get("spread_points"), target.get("total_points")
    favorite_code = target.get(favorite) if favorite in {"home", "away"} else None
    home_margin = None
    if favorite_code and spread is not None:
        home_margin = float(spread) if favorite == "home" else -float(spread)
    home_ml, away_ml = target.get("moneyline_home"), target.get("moneyline_away")
    return {
        "classification": "historical_market_reference",
        "favorite_side": favorite,
        "favorite_team": ({"abbr": favorite_code, "name": _team_name(favorite_code)}
                          if favorite_code else None),
        "spread_points": spread,
        "spread_status": "available" if spread is not None else "unavailable",
        "market_expected_home_margin": home_margin,
        "home_handicap_points": -home_margin if home_margin is not None else None,
        "total_line_points": total,
        "total_status": "available" if total is not None else "unavailable",
        "moneyline": {
            "status": "available" if home_ml is not None and away_ml is not None else "unavailable",
            "home": home_ml,
            "away": away_ml,
        },
        "quote_timestamp": None,
        "quote_time_status": "unavailable; no exact quote timestamp in source",
        "source_semantics": "historical market reference; not opening, closing, or T-5",
    }


def build_forecast_packet(
    rows: Sequence[Mapping[str, Any]],
    target: Mapping[str, Any],
    game_number: int,
    source_sha256: str,
) -> dict[str, Any]:
    target_date = _date(target.get("date"))
    home, away = _team(target.get("home")), _team(target.get("away"))
    if TARGET_TEAM not in {home, away}:
        raise ValueError("target_game_does_not_include_okc")
    opponent = away if home == TARGET_TEAM else home
    side = "home" if home == TARGET_TEAM else "away"
    states = {home: _team_state_before(rows, home, target_date),
              away: _team_state_before(rows, away, target_date)}
    packet = {
        "schema_version": PILOT_SCHEMA_VERSION,
        "packet_id": target["game_id"],
        "target": {
            "game_id": target["game_id"],
            "game_number": game_number,
            "season": TARGET_SEASON,
            "season_phase": _phase(game_number),
            "date": target_date.isoformat(),
            "tipoff": {"status": "unavailable", "value": None},
            "home": {"abbr": home, "name": _team_name(home)},
            "away": {"abbr": away, "name": _team_name(away)},
            "okc_home_away": side,
        },
        "team_state_before": {"okc": states[TARGET_TEAM], "opponent": states[opponent]},
        "rest_before": {
            "okc": _rest_before(rows, TARGET_TEAM, target_date),
            "opponent": _rest_before(rows, opponent, target_date),
        },
        "h2h_before": _h2h_before(rows, opponent, target_date),
        "market_reference": _market(target),
        "quantitative_baselines": {
            "pure": {"status": "unavailable", "value": None,
                     "reason": "no verified saved forecast for this game and pregame cutoff"},
            "residual": {"status": "unavailable", "value": None,
                         "reason": "no verified saved forecast and cutoff-matched residual model artifact"},
            "official_numeric": {"status": "unavailable", "value": None,
                                 "reason": "no verified saved official numeric forecast for this game"},
        },
        "pregame_context": {
            "roster": {"status": "unavailable",
                       "reason": "no reliable historical roster record with verified pregame publication time"},
            "injury": {"status": "unavailable",
                       "reason": "no reliable historical injury record with verified pregame publication time"},
            "starters": {"status": "unavailable",
                         "reason": "no reliable historical starter record with verified pregame publication time"},
            "news": {"status": "unavailable",
                     "reason": "current and postgame news are excluded; no verified historical pregame news attached"},
        },
        "data_provenance": {
            "source_file": "nba_2008-2026.csv",
            "source_sha256": source_sha256.upper(),
            "season_label_in_source": TARGET_SEASON_LABEL,
            "historical_reconstruction": True,
            "point_in_time_snapshot_verified": False,
            "source_capture_timestamp": None,
            "time_granularity": "calendar date only; same-day games excluded from prior context",
            "limitation": "retrospective archive without original capture or publication receipts",
        },
        "forecast_contract": {
            "closed_world": "Use only this packet. Do not use current or outside information. Preserve unavailable values as unavailable.",
            "required_output_keys": sorted(PREDICTION_KEYS),
            "winner": "Choose one packet team; return its full team name or abbreviation.",
            "win_probability": "Probability the selected winner wins, from 0 to 1.",
            "expected_margin": "Expected home points minus away points. Positive means the home team is favored.",
            "expected_total": "Expected combined final points, including overtime.",
            "response_format": "Return one JSON object with exactly the four required keys and no prose.",
        },
    }
    return _seal_packet(packet)


def build_packets_from_rows(
    rows: Sequence[Mapping[str, Any]], source_path: str, source_sha256: str,
    game_numbers: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    del source_path
    targets = select_target_games(rows, game_numbers)
    return [build_forecast_packet(rows, target, target["game_number"], source_sha256)
            for target in targets]


def build_pilot_packets(
    csv_path: str | Path = DEFAULT_CSV_PATH,
    game_numbers: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    rows, digest = _read_verified_csv(csv_path)
    return build_packets_from_rows(rows, str(csv_path), digest, game_numbers)


def _write_new_json(path: str | Path, value: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    return target


def write_pilot_artifacts(
    csv_path: str | Path = DEFAULT_CSV_PATH,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    game_numbers: Sequence[int] | None = None,
) -> dict[str, Any]:
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError("pilot_output_directory_already_exists")
    rows, digest = _read_verified_csv(csv_path)
    packets = build_packets_from_rows(rows, str(csv_path), digest, game_numbers)
    packet_dir = output / "forecast_packets"
    packet_dir.mkdir(parents=True, exist_ok=False)
    entries = []
    for packet in packets:
        number = packet["target"]["game_number"]
        _write_new_json(packet_dir / f"game_{number:03d}.json", packet)
        market = packet["market_reference"]
        entries.append({
            "game_id": packet["packet_id"],
            "game_number": number,
            "date": packet["target"]["date"],
            "home": packet["target"]["home"]["abbr"],
            "away": packet["target"]["away"]["abbr"],
            "favorite_side": market["favorite_side"],
            "spread_points": market["spread_points"],
            "total_line_points": market["total_line_points"],
            "moneyline_status": market["moneyline"]["status"],
            "packet_sha256": packet["packet_sha256"],
        })
    manifest = {
        "schema_version": PILOT_SCHEMA_VERSION,
        "target_team": TARGET_TEAM,
        "season": TARGET_SEASON,
        "competition": "NBA regular season",
        "verified_schedule_count": 82,
        "selected_game_numbers": [_packet_target(packet)["game_number"] for packet in packets],
        "source_file": str(Path(csv_path).resolve()),
        "source_sha256": digest,
        "packet_files": entries,
        "point_in_time_snapshot_verified": False,
        "actual_results_included": False,
    }
    _write_new_json(output / "manifest.json", manifest)
    return {"output_dir": str(output.resolve()), "manifest": manifest, "packets": packets}


def build_forecast_messages(
    packet: Mapping[str, Any], skill_text: str
) -> list[dict[str, str]]:
    """Reproduce legacy one-call prompts for audits; not the active runner."""
    _verify_packet(packet)
    if not isinstance(skill_text, str) or not skill_text.strip():
        raise ValueError("forecasting_skill_missing")
    return [
        {"role": "system", "content": skill_text.strip()},
        {"role": "user", "content": json.dumps(packet, ensure_ascii=False,
                                               sort_keys=True, allow_nan=False)},
    ]


def _winner_code(target: Mapping[str, Any], winner: Any) -> str:
    value = str(winner or "").strip().casefold()
    for side in ("home", "away"):
        team = target[side]
        if value in {str(team["abbr"]).casefold(), str(team["name"]).casefold()}:
            return str(team["abbr"]).casefold()
    raise ValueError("forecast_winner_not_target_team")


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"forecast_{field}_invalid")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"forecast_{field}_invalid")
    return number


def validate_forecast(packet: Mapping[str, Any], output: Mapping[str, Any]) -> dict[str, Any]:
    _verify_packet(packet)
    if not isinstance(output, Mapping) or set(output) != PREDICTION_KEYS:
        raise ValueError("forecast_output_schema_invalid")
    winner = str(output.get("winner", "")).strip()
    _winner_code(_packet_target(packet), winner)
    probability = _finite(output.get("win_probability"), "win_probability")
    if not 0 <= probability <= 1:
        raise ValueError("forecast_probability_out_of_range")
    margin = _finite(output.get("expected_margin"), "expected_margin")
    total = _finite(output.get("expected_total"), "expected_total")
    if total < 0:
        raise ValueError("forecast_expected_total_negative")
    return {"winner": winner, "win_probability": probability,
            "expected_margin": margin, "expected_total": total}


def _normalize_two_stage_prediction(
    packet: Mapping[str, Any], numbers: Mapping[str, Any]
) -> dict[str, Any]:
    target = _packet_target(packet)
    side = str(numbers.get("winner_side") or "")
    if side not in {"home", "away"}:
        raise ValueError("forecast_stage_winner_side_invalid")
    winner = target[side]
    if not isinstance(winner, Mapping):
        raise ValueError("forecast_stage_team_identity_invalid")
    return validate_forecast(packet, {
        "winner": winner.get("name") or winner.get("abbr"),
        "win_probability": numbers.get("win_probability"),
        "expected_margin": numbers.get("expected_home_margin"),
        "expected_total": numbers.get("expected_total"),
    })


def _read_stage_prediction_bundle(
    stage_dir: Path, packet: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    independent_record = json.loads((stage_dir / "independent.json").read_text(encoding="utf-8"))
    independent_numbers = independent_record.get("prediction_and_basis", {}).get("prediction")
    if independent_record.get("sha256") != _sha256_value(independent_record.get("prediction_and_basis")):
        raise ValueError("checkpoint_independent_receipt_integrity_failure")
    final_record = json.loads((stage_dir / "final.json").read_text(encoding="utf-8"))
    return {
        "independent_prediction": _normalize_two_stage_prediction(packet, independent_numbers),
        "prediction": _normalize_two_stage_prediction(packet, final_record.get("final_prediction", {})),
    }


def _mirror_stage_receipts(stage_dir: Path, destination: Path) -> None:
    """Preserve the legacy output-local audit layout without using it for resume."""
    destination.mkdir(parents=True, exist_ok=True)
    for source in sorted(stage_dir.iterdir()):
        if not source.is_file():
            continue
        target = destination / source.name
        if target.is_file():
            if _file_sha256(target) != _file_sha256(source):
                raise FileExistsError("prediction_stage_mirror_conflicts_with_checkpoint")
            continue
        if target.exists():
            raise FileExistsError("prediction_stage_mirror_conflicts_with_checkpoint")
        with source.open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    raise ValueError("deepseek_response_content_missing")


def forecast_one(
    packet: Mapping[str, Any],
    request_fn: Callable[..., Any],
    api_key: str,
    skill_loader: Callable[[], str] | None = None,
    *,
    audit_dir: str | Path | None = None,
    prepared_inputs: tuple[dict[str, Any], dict[str, Any], list[str]] | None = None,
) -> dict[str, Any]:
    if not api_key:
        raise ValueError("deepseek_api_key_missing")
    if skill_loader is None:
        from .forecasting_skill import load_forecasting_skill
        skill_loader = load_forecasting_skill
    from .forecasting_workflow import prepare_pilot, run_two_stage
    _verify_packet(packet)
    basketball, market, removed = prepared_inputs or prepare_pilot(dict(packet))
    folder = Path(audit_dir) if audit_dir else DEFAULT_OUTPUT_DIR / "two_stage_calls" / uuid.uuid4().hex
    call_number = 0

    def client(messages, schema):
        nonlocal call_number
        call_number += 1
        stage_name = "independent" if getattr(schema, "__name__", "") == "IndependentForecast" else "market_review"
        _write_new_json(folder / f"pilot_request_{stage_name}.json", {
            "stage": stage_name,
            "model": DEEPSEEK_MODEL,
            "temperature": 0.1,
            "thinking": "disabled",
            "max_tokens": DEEPSEEK_MAX_TOKENS,
            "timeout_seconds": DEEPSEEK_TIMEOUT_SECONDS,
            "request_messages_sha256": _sha256_value(messages),
            "request_call_number": call_number,
        })
        response = request_fn(messages, api_key, DEEPSEEK_MODEL, DEEPSEEK_MAX_TOKENS,
                              timeout_seconds=DEEPSEEK_TIMEOUT_SECONDS)
        return _response_text(response), {"usage": getattr(response, "usage", {}),
                       "finish_reason": getattr(response, "finish_reason", None),
                       "provider": getattr(response, "response_metadata", {})}

    result = run_two_stage(basketball, market, folder, client=client,
                           skill_loader=skill_loader, removed_paths=removed,
                           transport_label="configured_deepseek" if getattr(request_fn, "__module__", "") == "v2.agent.generate_historical_pregame_analysis" else "injected")
    numbers = result["final_prediction"]
    target = _packet_target(packet)
    final = validate_forecast(packet, {
        "winner": target[numbers["winner_side"]]["name"],
        "win_probability": numbers["win_probability"],
        "expected_margin": numbers["expected_home_margin"],
        "expected_total": numbers["expected_total"],
    })
    _write_new_json(folder / "pilot_prediction.json", {"game_id": packet["packet_id"],
        "packet_sha256": packet["packet_sha256"], "prediction": final})
    return final


def _prepared_packet(
    packet: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str], str]:
    from .forecasting_workflow import prepare_pilot
    _verify_packet(packet)
    _game_identity(packet)
    prepared = prepare_pilot(dict(packet))
    if not isinstance(prepared, tuple) or len(prepared) != 3:
        raise ValueError("forecast_input_projection_invalid")
    basketball, market, removed = prepared
    if not isinstance(basketball, Mapping) or not isinstance(market, Mapping):
        raise ValueError("forecast_input_projection_invalid")
    input_sha256 = _sha256_value({"basketball": basketball, "market": market})
    return dict(basketball), dict(market), list(removed), input_sha256


def _checkpoint_fingerprint(
    packet: Mapping[str, Any], input_sha256: str, skill_sha256: str,
    model_settings: Mapping[str, Any], code_hashes: Mapping[str, str],
    request_transport: Mapping[str, str],
) -> str:
    return _sha256_value({
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "game_identity": _game_identity(packet),
        "packet_sha256": packet["packet_sha256"],
        "input_sha256": input_sha256,
        "forecasting_skill_sha256": skill_sha256,
        "model_settings": dict(model_settings),
        "code_hashes": dict(code_hashes),
        "request_transport": dict(request_transport),
    })


def _checkpoint_path(checkpoint_dir: Path, packet: Mapping[str, Any], fingerprint: str) -> Path:
    return checkpoint_dir / f"game_{_game_number(packet):03d}" / fingerprint


def _required_stage_files(market_required: bool) -> set[str]:
    required = {
        "projection.json", "independent_request.json", "independent_response.json",
        "independent_validation.json", "independent.json", "pilot_request_independent.json", "final.json",
        "pilot_prediction.json",
    }
    if market_required:
        required.update({"market_review_request.json", "market_review_response.json",
                        "market_review_validation.json", "pilot_request_market_review.json"})
    return required


def _load_success_checkpoint(
    checkpoint_path: Path,
    packet: Mapping[str, Any],
    *,
    fingerprint: str,
    input_sha256: str,
    skill_sha256: str,
    model_settings: Mapping[str, Any],
    code_hashes: Mapping[str, str],
    request_transport: Mapping[str, str],
    market_required: bool,
) -> dict[str, Any] | None:
    success_path = checkpoint_path / "success.json"
    if not success_path.is_file():
        return None
    try:
        record = json.loads(success_path.read_text(encoding="utf-8"))
        receipt_hash = record.get("receipt_sha256")
        unsigned = dict(record)
        unsigned.pop("receipt_sha256", None)
        if receipt_hash != _sha256_value(unsigned):
            raise ValueError("checkpoint_success_receipt_hash_mismatch")
        expected_values = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "status": "complete",
            "game_identity": _game_identity(packet),
            "packet_sha256": packet["packet_sha256"],
            "input_sha256": input_sha256,
            "forecasting_skill_sha256": skill_sha256,
            "model_settings": dict(model_settings),
            "code_hashes": dict(code_hashes),
            "request_transport": dict(request_transport),
            "checkpoint_fingerprint": fingerprint,
            "market_stage_required": market_required,
        }
        if any(record.get(key) != value for key, value in expected_values.items()):
            raise ValueError("checkpoint_success_metadata_mismatch")
        stage_relative = Path(str(record.get("stage_directory") or ""))
        if stage_relative.is_absolute() or ".." in stage_relative.parts:
            raise ValueError("checkpoint_stage_path_invalid")
        stage_dir = (checkpoint_path / stage_relative).resolve()
        if checkpoint_path.resolve() not in stage_dir.parents:
            raise ValueError("checkpoint_stage_path_invalid")
        artifacts = record.get("stage_artifacts")
        required = _required_stage_files(market_required)
        if not isinstance(artifacts, Mapping) or set(artifacts) != required:
            raise ValueError("checkpoint_stage_receipts_incomplete")
        for filename, expected_hash in artifacts.items():
            artifact = stage_dir / filename
            if not artifact.is_file() or _file_sha256(artifact) != expected_hash:
                raise ValueError("checkpoint_stage_receipt_hash_mismatch")
        for stage in ("independent", "market_review") if market_required else ("independent",):
            validation = json.loads((stage_dir / f"{stage}_validation.json").read_text(encoding="utf-8"))
            if validation.get("schema_validated") is not True:
                raise ValueError("checkpoint_stage_not_validated")
            request = json.loads((stage_dir / f"pilot_request_{stage}.json").read_text(encoding="utf-8"))
            expected_settings = {key: model_settings[key] for key in (
                "model", "temperature", "thinking", "max_tokens", "timeout_seconds")}
            if any(request.get(key) != value for key, value in expected_settings.items()):
                raise ValueError("checkpoint_model_settings_mismatch")
        final_stage = json.loads((stage_dir / "final.json").read_text(encoding="utf-8"))
        if int(final_stage.get("calls", -1)) != (2 if market_required else 1):
            raise ValueError("checkpoint_stage_call_count_mismatch")
        prediction = validate_forecast(packet, record.get("prediction"))
        independent_prediction = validate_forecast(packet, record.get("independent_prediction"))
        saved_prediction = json.loads((stage_dir / "pilot_prediction.json").read_text(encoding="utf-8"))
        if (saved_prediction.get("game_id") != packet["packet_id"]
                or saved_prediction.get("packet_sha256") != packet["packet_sha256"]
                or validate_forecast(packet, saved_prediction.get("prediction")) != prediction):
            raise ValueError("checkpoint_saved_prediction_mismatch")
        stage_predictions = _read_stage_prediction_bundle(stage_dir, packet)
        if (stage_predictions["prediction"] != prediction
                or stage_predictions["independent_prediction"] != independent_prediction):
            raise ValueError("checkpoint_stage_prediction_mismatch")
        stage_predictions["_stage_directory"] = str(stage_dir)
        return stage_predictions
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        raise ValueError("checkpoint_success_receipt_invalid") from exc


def _write_success_checkpoint(
    checkpoint_path: Path,
    stage_dir: Path,
    packet: Mapping[str, Any],
    prediction: Mapping[str, Any],
    independent_prediction: Mapping[str, Any],
    *,
    fingerprint: str,
    input_sha256: str,
    skill_sha256: str,
    model_settings: Mapping[str, Any],
    code_hashes: Mapping[str, str],
    request_transport: Mapping[str, str],
    market_required: bool,
) -> Path:
    required = _required_stage_files(market_required)
    missing = [filename for filename in sorted(required) if not (stage_dir / filename).is_file()]
    if missing:
        raise ValueError("checkpoint_stage_receipts_incomplete:" + ",".join(missing))
    record = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "status": "complete",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "game_identity": _game_identity(packet),
        "packet_sha256": packet["packet_sha256"],
        "input_sha256": input_sha256,
        "forecasting_skill_sha256": skill_sha256,
        "model_settings": dict(model_settings),
        "code_hashes": dict(code_hashes),
        "request_transport": dict(request_transport),
        "checkpoint_fingerprint": fingerprint,
        "prediction": validate_forecast(packet, prediction),
        "independent_prediction": validate_forecast(packet, independent_prediction),
        "stage_directory": stage_dir.relative_to(checkpoint_path).as_posix(),
        "market_stage_required": market_required,
        "stage_artifacts": {name: _file_sha256(stage_dir / name) for name in sorted(required)},
    }
    record["receipt_sha256"] = _sha256_value(record)
    return _write_new_json(checkpoint_path / "success.json", record)


def run_forecast_batch(
    packets: Sequence[Mapping[str, Any]],
    output_path: str | Path,
    request_fn: Callable[..., Any],
    api_key: str,
    skill_loader: Callable[[], str] | None = None,
    *,
    checkpoint_dir: str | Path = DEFAULT_CHECKPOINT_DIR,
) -> dict[str, Any]:
    packet_list = list(packets)
    if not packet_list:
        raise ValueError("forecast_packet_set_empty")
    identities = [_game_identity(packet) for packet in packet_list]
    game_ids = [identity["game_id"] for identity in identities]
    game_numbers = [identity["game_number"] for identity in identities]
    if len(set(game_ids)) != len(game_ids) or len(set(game_numbers)) != len(game_numbers):
        raise ValueError("forecast_packet_identity_duplicate")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_root = Path(checkpoint_dir)
    if skill_loader is None:
        from .forecasting_skill import load_forecasting_skill
        skill_loader = load_forecasting_skill
    skill_text = skill_loader()
    if not isinstance(skill_text, str) or not skill_text.strip():
        raise ValueError("forecasting_skill_missing")
    skill_sha256 = hashlib.sha256(skill_text.encode("utf-8")).hexdigest()
    settings, code_hashes = _model_settings(), _code_hashes()
    request_transport = _request_transport_identity(request_fn)
    prepared_items = []
    for packet in packet_list:
        basketball, market, removed, input_sha256 = _prepared_packet(packet)
        fingerprint = _checkpoint_fingerprint(
            packet, input_sha256, skill_sha256, settings, code_hashes, request_transport)
        prepared_items.append({
            "packet": packet, "basketball": basketball, "market": market, "removed": removed,
            "input_sha256": input_sha256, "fingerprint": fingerprint,
            "checkpoint_path": _checkpoint_path(checkpoint_root, packet, fingerprint),
            "market_required": bool(market.get("evidence")),
        })
    run_fingerprint = _sha256_value({
        "game_checkpoints": [item["fingerprint"] for item in prepared_items],
        "model_settings": settings,
    })
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        _validate_prediction_set(packet_list, existing)
        existing_by_id = {item["game_id"]: item for item in existing.get("predictions", [])}
        if existing.get("run_fingerprint") != run_fingerprint or any(
            existing_by_id.get(item["packet"]["packet_id"], {}).get("checkpoint_fingerprint")
            != item["fingerprint"] for item in prepared_items
        ):
            raise FileExistsError("prediction_output_conflicts_with_current_inputs")
        return existing

    completed = []
    for item in prepared_items:
        packet = item["packet"]
        checkpoint_path = item["checkpoint_path"]
        predictions = _load_success_checkpoint(
            checkpoint_path, packet, fingerprint=item["fingerprint"],
            input_sha256=item["input_sha256"], skill_sha256=skill_sha256,
            model_settings=settings, code_hashes=code_hashes,
            request_transport=request_transport,
            market_required=item["market_required"],
        )
        if predictions is None:
            checkpoint_path.mkdir(parents=True, exist_ok=True)
            stage_dir = checkpoint_path / f"attempt_{uuid.uuid4().hex}"
            prediction = forecast_one(
                packet, request_fn, api_key, skill_loader=lambda: skill_text,
                audit_dir=stage_dir,
                prepared_inputs=(item["basketball"], item["market"], item["removed"]),
            )
            predictions = _read_stage_prediction_bundle(stage_dir, packet)
            if predictions["prediction"] != prediction:
                raise ValueError("forecast_return_does_not_match_stage_receipt")
            predictions["_stage_directory"] = str(stage_dir)
            _write_success_checkpoint(
                checkpoint_path, stage_dir, packet, predictions["prediction"],
                predictions["independent_prediction"],
                fingerprint=item["fingerprint"], input_sha256=item["input_sha256"],
                skill_sha256=skill_sha256, model_settings=settings,
                code_hashes=code_hashes, request_transport=request_transport,
                market_required=item["market_required"],
            )
        _mirror_stage_receipts(
            Path(predictions["_stage_directory"]),
            output.with_suffix(".stages") / f"game_{_game_number(packet):03d}",
        )
        prediction = predictions["prediction"]
        completed.append({
            "game_id": packet["packet_id"],
            "packet_sha256": packet["packet_sha256"],
            "checkpoint_fingerprint": item["fingerprint"],
            "prediction": validate_forecast(packet, prediction),
            "independent_prediction": validate_forecast(
                packet, predictions["independent_prediction"]),
        })
    document = {
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "model": DEEPSEEK_MODEL,
        "model_settings": settings,
        "skill_sha256": skill_sha256,
        "code_hashes": code_hashes,
        "request_transport": request_transport,
        "run_fingerprint": run_fingerprint,
        "predictions": completed,
        "forecast_workflow": FORECAST_WORKFLOW_VERSION,
    }
    _write_new_json(output, document)
    return document


def _validate_prediction_set(
    packets: Sequence[Mapping[str, Any]],
    forecasts: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not packets:
        raise ValueError("evaluation_packet_set_empty")
    for packet in packets:
        _verify_packet(packet)
        _game_identity(packet)
    items = forecasts.get("predictions") if isinstance(forecasts, Mapping) else forecasts
    if (not isinstance(items, Sequence) or isinstance(items, (str, bytes))
            or len(items) != len(packets)):
        raise ValueError("forecast_set_size_mismatch")
    expected = {str(packet["packet_id"]): packet for packet in packets}
    if len(expected) != len(packets):
        raise ValueError("forecast_packet_identity_duplicate")
    found: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("forecast_item_invalid")
        game_id = str(item.get("game_id") or "")
        if game_id not in expected or game_id in found:
            raise ValueError("forecast_game_identity_invalid")
        packet = expected[game_id]
        if item.get("packet_sha256") != packet.get("packet_sha256"):
            raise ValueError("forecast_packet_hash_mismatch")
        found[game_id] = {
            "prediction": validate_forecast(packet, item.get("prediction")),
            "independent_prediction": (
                validate_forecast(packet, item.get("independent_prediction"))
                if item.get("independent_prediction") is not None else None
            ),
        }
    if set(found) != set(expected):
        raise ValueError("forecast_set_identity_mismatch")
    return [{"packet": packet, **found[str(packet["packet_id"])]}
            for packet in packets]


class CsvOutcomeProvider:
    """Lazily read results after every requested forecast validates."""
    def __init__(self, csv_path: str | Path = DEFAULT_CSV_PATH):
        self.csv_path = Path(csv_path)
        self._targets: dict[str, Mapping[str, Any]] | None = None

    def __call__(self, game_id: str) -> dict[str, Any]:
        if self._targets is None:
            rows, _ = _read_verified_csv(self.csv_path)
            targets = select_target_games(rows, range(1, 83))
            self._targets = {
                target["game_id"]: rows[target["source_row_index"]]
                for target in targets
            }
        row = self._targets.get(game_id)
        if row is None:
            raise ValueError("actual_result_game_id_missing")
        scores = _scores(row)
        if scores is None:
            raise ValueError("actual_result_score_invalid")
        return {"game_id": game_id, "date": row["date"],
                "home": _team(row["home"]), "away": _team(row["away"]),
                "home_score": scores[0], "away_score": scores[1]}


def _settlement(value: float, line: float, high: str, low: str) -> str:
    delta = value - line
    return "push" if abs(delta) <= EPSILON else high if delta > 0 else low


def _grade_pick(pick: str, settlement: str) -> str:
    if pick == "no_pick":
        return "no_pick"
    if settlement == "push":
        return "push"
    return "win" if pick == settlement else "loss"


def _ats(
    packet: Mapping[str, Any], predicted_home_margin: float, actual_home_margin: float
) -> dict[str, Any]:
    market = packet["market_reference"]
    handicap = market.get("home_handicap_points")
    if market.get("spread_points") is None or handicap is None:
        return {"status": "unavailable", "pick": None, "result": "unavailable"}
    edge = predicted_home_margin + float(handicap)
    pick = "home" if edge > EPSILON else "away" if edge < -EPSILON else "no_pick"
    settled = _settlement(actual_home_margin + float(handicap), 0.0, "home", "away")
    return {
        "status": "available",
        "pick": pick,
        "result": _grade_pick(pick, settled),
        "actual_settlement": "push" if settled == "push" else f"{settled}_cover",
        "model_edge_vs_spread": edge,
        "home_handicap_points": float(handicap),
    }


def _over_under(
    packet: Mapping[str, Any], predicted_total: float, actual_total: float
) -> dict[str, Any]:
    line = packet["market_reference"].get("total_line_points")
    if line is None:
        return {"status": "unavailable", "pick": None, "result": "unavailable"}
    line = float(line)
    edge = predicted_total - line
    pick = "over" if edge > EPSILON else "under" if edge < -EPSILON else "no_pick"
    settled = _settlement(actual_total, line, "over", "under")
    return {
        "status": "available",
        "pick": pick,
        "result": _grade_pick(pick, settled),
        "actual_settlement": settled,
        "model_edge_vs_total": edge,
        "market_total_points": line,
    }


def _prediction_metrics(
    packet: Mapping[str, Any], prediction: Mapping[str, Any],
    actual_winner: str, actual_home_margin: float, actual_total: float,
) -> dict[str, Any]:
    target = _packet_target(packet)
    return {
        "winner_correct": _winner_code(target, prediction["winner"]) == actual_winner,
        "margin_absolute_error": abs(prediction["expected_margin"] - actual_home_margin),
        "total_absolute_error": abs(prediction["expected_total"] - actual_total),
        "ats": _ats(packet, prediction["expected_margin"], actual_home_margin),
        "over_under": _over_under(packet, prediction["expected_total"], actual_total),
    }


def _market_reference_metrics(
    packet: Mapping[str, Any], actual_winner: str,
    actual_home_margin: float, actual_total: float,
) -> dict[str, Any]:
    target = _packet_target(packet)
    market = packet.get("market_reference") or {}
    favorite_side = market.get("favorite_side")
    favorite = _team_abbr(target.get(favorite_side)) if favorite_side in {"home", "away"} else None
    market_margin = market.get("market_expected_home_margin")
    total_line = market.get("total_line_points")
    handicap = market.get("home_handicap_points")
    ats_settlement = None
    favorite_ats_result = "unavailable"
    if handicap is not None and favorite_side:
        settled = _settlement(actual_home_margin + float(handicap), 0.0, "home", "away")
        ats_settlement = "push" if settled == "push" else f"{settled}_cover"
        favorite_ats_result = (
            "push" if settled == "push" else
            "win" if (settled == favorite_side) else "loss"
        )
    total_settlement = (_settlement(actual_total, float(total_line), "over", "under")
                        if total_line is not None else "unavailable")
    return {
        "favorite": favorite,
        "favorite_correct": favorite == actual_winner if favorite else None,
        "market_expected_home_margin": market_margin,
        "margin_absolute_error": (abs(float(market_margin) - actual_home_margin)
                                  if market_margin is not None else None),
        "market_total_line": total_line,
        "total_absolute_error": (abs(float(total_line) - actual_total)
                                 if total_line is not None else None),
        "favorite_ats_settlement": ats_settlement or "unavailable",
        "favorite_ats_result": favorite_ats_result,
        "actual_total_settlement": total_settlement,
        "classification": "historical_market_reference; no quote timestamp",
    }


def evaluate_forecasts(
    packets: Sequence[Mapping[str, Any]],
    forecasts: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    outcome_provider: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate all predictions before the first result read."""
    validated = _validate_prediction_set(packets, forecasts)
    actual_rows, scored_rows = [], []
    for item in validated:
        packet, prediction = item["packet"], item["prediction"]
        target = _packet_target(packet)
        game_id = target["game_id"]
        actual = outcome_provider(game_id)
        if str(actual.get("game_id") or "") != game_id:
            raise ValueError("actual_result_game_id_mismatch")
        home, away = _team(actual.get("home")), _team(actual.get("away"))
        if home != target["home"]["abbr"] or away != target["away"]["abbr"]:
            raise ValueError("actual_result_team_identity_mismatch")
        if actual.get("date") is not None and _date(actual.get("date")) != _date(target["date"]):
            raise ValueError("actual_result_date_mismatch")
        home_score, away_score = _number(actual.get("home_score")), _number(actual.get("away_score"))
        if (home_score is None or away_score is None or home_score == away_score
                or not home_score.is_integer() or not away_score.is_integer()):
            raise ValueError("actual_result_score_invalid")
        home_score, away_score = int(home_score), int(away_score)
        margin, total = home_score - away_score, home_score + away_score
        winner = home if margin > 0 else away
        predicted_winner = _winner_code(target, prediction["winner"])
        actual_rows.append({
            "game_id": game_id, "date": actual.get("date", target["date"]),
            "home": home, "away": away, "home_score": home_score,
            "away_score": away_score, "actual_winner": winner,
            "actual_home_margin": margin, "actual_total": total,
        })
        final_metrics = _prediction_metrics(packet, prediction, winner, margin, total)
        independent_prediction = item.get("independent_prediction")
        scored_rows.append({
            "game_id": game_id,
            "game_number": target["game_number"],
            "prediction": prediction,
            "independent_prediction": independent_prediction,
            "independent_metrics": (_prediction_metrics(
                packet, independent_prediction, winner, margin, total)
                if independent_prediction is not None else {"status": "unavailable"}),
            "metrics": final_metrics,
            "market_reference_metrics": _market_reference_metrics(packet, winner, margin, total),
        })
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "evaluation_order": "all_forecasts_validated_before_first_outcome_read",
        "games": scored_rows,
        "actual_results": {
            "schema_version": "25th-second-pilot-actual-results-v1",
            "source_file": "nba_2008-2026.csv",
            "source_sha256": EXPECTED_DATASET_SHA256,
            "games": actual_rows,
        },
    }


def write_evaluation_artifacts(
    report: Mapping[str, Any], output_dir: str | Path
) -> dict[str, Path]:
    output = Path(output_dir)
    actual_document = report.get("actual_results")
    actual_games = actual_document.get("games", []) if isinstance(actual_document, Mapping) else []
    evaluation_games = report.get("games", [])
    if (not isinstance(actual_games, Sequence) or isinstance(actual_games, (str, bytes))
            or not isinstance(evaluation_games, Sequence) or isinstance(evaluation_games, (str, bytes))
            or not actual_games or len(actual_games) != len(evaluation_games)):
        raise ValueError("evaluation_artifact_game_set_invalid")
    actual_ids = [str(game.get("game_id") or "") for game in actual_games if isinstance(game, Mapping)]
    evaluation_ids = [str(game.get("game_id") or "") for game in evaluation_games if isinstance(game, Mapping)]
    if (len(actual_ids) != len(actual_games) or len(evaluation_ids) != len(evaluation_games)
            or len(set(actual_ids)) != len(actual_ids) or set(actual_ids) != set(evaluation_ids)):
        raise ValueError("evaluation_artifact_game_identity_mismatch")
    actual_path = output / "actual_results.json"
    evaluation_path = output / "evaluation.json"
    if actual_path.exists() or evaluation_path.exists():
        raise FileExistsError("evaluation_output_already_exists")
    _write_new_json(actual_path, actual_document)
    _write_new_json(evaluation_path, {
        "schema_version": report["schema_version"],
        "evaluation_order": report["evaluation_order"],
        "games": report["games"],
    })
    return {"actual_results": actual_path, "evaluation": evaluation_path}


def _load_packets(
    path: str | Path,
    game_numbers: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    directory = Path(path)
    if not directory.is_dir():
        raise FileNotFoundError("forecast_packet_directory_missing")
    requested = _normalize_game_numbers(game_numbers) if game_numbers is not None else None
    if requested is None:
        files = sorted(directory.glob("game_*.json"))
        selected_files = []
        for file in files:
            stem = file.stem
            if len(stem) != 8 or not stem.startswith("game_") or not stem[5:].isdigit():
                continue
            number = int(stem[5:])
            if not 1 <= number <= 82:
                raise ValueError("forecast_packet_filename_game_number_invalid")
            selected_files.append((number, file))
    else:
        selected_files = [(number, directory / f"game_{number:03d}.json") for number in requested]
    packets = []
    seen_numbers: set[int] = set()
    for number, file in selected_files:
        if not file.is_file():
            raise FileNotFoundError("forecast_packet_missing")
        packet = json.loads(file.read_text(encoding="utf-8"))
        _verify_packet(packet)
        if _game_number(packet) != number or number in seen_numbers:
            raise ValueError("forecast_packet_filename_identity_mismatch")
        _game_identity(packet)
        seen_numbers.add(number)
        packets.append(packet)
    if not packets:
        raise ValueError("forecast_packet_set_empty")
    return packets


def _parse_game_numbers(value: str | None, default: Sequence[int] | None) -> tuple[int, ...] | None:
    if value is None:
        return tuple(default) if default is not None else None
    text = value.strip().casefold()
    if text == "all":
        return tuple(range(1, 83))
    if text == "pilot":
        return TARGET_GAME_NUMBERS
    try:
        numbers = tuple(int(part.strip()) for part in text.split(",") if part.strip())
    except ValueError as exc:
        raise ValueError("game_numbers_argument_invalid") from exc
    return _normalize_game_numbers(numbers)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OKC 2025-26 Forecasting Pilot")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--csv", default=str(DEFAULT_CSV_PATH))
    build.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    build.add_argument("--games", default="pilot", help="pilot, all, or comma-separated game ordinals")
    prepare = commands.add_parser("prepare", help="Build and report packets without calling DeepSeek")
    prepare.add_argument("--csv", default=str(DEFAULT_CSV_PATH))
    prepare.add_argument("--output-dir", default=str(DEFAULT_PREFLIGHT_DIR))
    prepare.add_argument("--enriched-pilot-packets-dir", default=str(DEFAULT_ENRICHED_PILOT_PACKET_DIR))
    prepare.add_argument("--no-reuse-enriched-pilot", action="store_true")
    forecast = commands.add_parser("forecast")
    forecast.add_argument("--packets-dir", default=str(DEFAULT_OUTPUT_DIR / "forecast_packets"))
    forecast.add_argument("--games", help="all packets in the directory, or selected comma-separated ordinals")
    forecast.add_argument("--output", required=True)
    forecast.add_argument("--call-deepseek", action="store_true")
    forecast.add_argument("--checkpoint-dir", default=str(DEFAULT_CHECKPOINT_DIR))
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--packets-dir", default=str(DEFAULT_OUTPUT_DIR / "forecast_packets"))
    evaluate.add_argument("--games", help="all packets in the directory, or selected comma-separated ordinals")
    evaluate.add_argument("--predictions", required=True)
    evaluate.add_argument("--csv", default=str(DEFAULT_CSV_PATH))
    evaluate.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    if args.command == "build":
        numbers = _parse_game_numbers(args.games, TARGET_GAME_NUMBERS)
        result = write_pilot_artifacts(args.csv, args.output_dir, numbers)
        print(json.dumps({"status": "built", "output_dir": result["output_dir"],
                          "game_count": len(result["packets"]),
                          "game_numbers": result["manifest"]["selected_game_numbers"]}, ensure_ascii=False))
        return 0
    if args.command == "prepare":
        from .forecasting_preflight import write_preflight_artifacts
        result = write_preflight_artifacts(
            args.csv, args.output_dir,
            enriched_packet_dir=None if args.no_reuse_enriched_pilot else args.enriched_pilot_packets_dir,
        )
        print(json.dumps({"status": "prepared", "deepseek_called": False,
                          "output_dir": result["output_dir"],
                          "game_count": result["game_count"],
                          "summary": result["summary"]}, ensure_ascii=False))
        return 0
    if args.command == "forecast":
        if not args.call_deepseek:
            parser.error("forecast requires explicit --call-deepseek")
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        game_numbers = _parse_game_numbers(args.games, None)
        packets = _load_packets(args.packets_dir, game_numbers)
        from v2.agent.generate_historical_pregame_analysis import request_deepseek
        result = run_forecast_batch(packets, args.output, request_deepseek, key,
                                    checkpoint_dir=args.checkpoint_dir)
        print(json.dumps({"status": "forecasted", "game_count": len(result["predictions"]),
                          "game_numbers": [_game_number(packet) for packet in packets]}, ensure_ascii=False))
        return 0
    game_numbers = _parse_game_numbers(args.games, None)
    packets = _load_packets(args.packets_dir, game_numbers)
    prediction_path = Path(args.predictions)
    if not prediction_path.is_file():
        raise FileNotFoundError("prediction_file_missing")
    forecasts = json.loads(prediction_path.read_text(encoding="utf-8"))
    report = evaluate_forecasts(packets, forecasts, CsvOutcomeProvider(args.csv))
    paths = write_evaluation_artifacts(report, args.output_dir)
    print(json.dumps({"status": "evaluated", "game_count": len(report["games"]),
                      "actual_results": str(paths["actual_results"].resolve()),
                      "evaluation": str(paths["evaluation"].resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
