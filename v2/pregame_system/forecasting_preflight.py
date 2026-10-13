"""Offline packet construction and completeness reporting for the OKC pilot.

This module deliberately has no model client and never imports DeepSeek request
code. Its enriched-packet reuse is limited to the five locally saved pilot
packets after hash, identity, and result-field checks.
"""
from __future__ import annotations

import csv
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from .forecasting_pilot import (
    DEFAULT_CSV_PATH,
    DEFAULT_ENRICHED_PILOT_PACKET_DIR,
    EXPECTED_DATASET_SHA256,
    REPOSITORY_ROOT,
    TARGET_GAME_NUMBERS,
    TARGET_SEASON,
    TARGET_TEAM,
    _date,
    _game_identity,
    _game_number,
    _load_packets,
    _normalize_game_numbers,
    _packet_target,
    _read_verified_csv,
    _team,
    _team_abbr,
    _team_name,
    _write_new_json,
    build_packets_from_rows,
    packet_content_sha256,
)


PREFLIGHT_SCHEMA_VERSION = "25th-second-okc-forecast-preflight-v1"
FORBIDDEN_RESULT_KEYS = {
    "score_home", "score_away", "home_score", "away_score",
    "actual_winner", "actual_margin", "actual_home_margin", "actual_total",
    "ats_result", "ou_result", "quarter_scores", "postgame", "postgame_stats",
    "postgame_news", "recap", "boxscore", "final_score",
}


def _find_forbidden_result_fields(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key).casefold()
            next_path = f"{path}.{key}" if path else str(key)
            if name in FORBIDDEN_RESULT_KEYS:
                found.append(next_path)
            found.extend(_find_forbidden_result_fields(child, next_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_find_forbidden_result_fields(child, f"{path}[{index}]"))
    return found


def _status(value: Any) -> str:
    if isinstance(value, Mapping):
        explicit = str(value.get("status") or "").casefold()
        if explicit in {"available", "partial", "unavailable"}:
            return explicit
        if value:
            states, has_data = [], False
            for child in value.values():
                if isinstance(child, (Mapping, list, tuple)):
                    states.append(_status(child))
                elif child not in (None, ""):
                    has_data = True
            if states:
                combined = _combine(states)
                return "partial" if has_data and combined == "unavailable" else combined
            return "available" if has_data else "unavailable"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "available" if value else "unavailable"
    return "unavailable"


def _combine(statuses: Sequence[str]) -> str:
    ranks = {"unavailable": 0, "partial": 1, "available": 2}
    known = [str(value) for value in statuses if str(value) in ranks]
    if not known or all(value == "unavailable" for value in known):
        return "unavailable"
    if all(value == "available" for value in known):
        return "available"
    return "partial"


def _game_fields(packet: Mapping[str, Any]) -> dict[str, Any]:
    target = _packet_target(packet)
    return {
        "game_id": str(target.get("game_id") or packet.get("packet_id") or ""),
        "game_number": _game_number(packet),
        "date": str(target.get("date") or ""),
        "home": _team_abbr(target.get("home")),
        "away": _team_abbr(target.get("away")),
    }


def _section(packet: Mapping[str, Any], enriched_name: str, base_name: str) -> Any:
    if enriched_name in packet:
        return packet.get(enriched_name)
    context = packet.get("pregame_context")
    return context.get(base_name) if isinstance(context, Mapping) else None


def _completeness(packet: Mapping[str, Any], input_tier: str) -> dict[str, Any]:
    identity = _game_identity(packet)
    enriched = "game" in packet
    state = packet.get("team_state") if enriched else packet.get("team_state_before")
    rest = packet.get("rest") if enriched else packet.get("rest_before")
    h2h = packet.get("h2h") if enriched else packet.get("h2h_before")
    richer = packet.get("richer_team_data")
    baselines = packet.get("quantitative_baselines") or {}
    market = packet.get("market_reference") or {}
    injuries = _section(packet, "injuries", "injury")
    starters = _section(packet, "starters", "starters")
    rotation = _section(packet, "rotation_notes", "rotation")
    news = _section(packet, "pregame_news", "news")
    sources = packet.get("sources") if isinstance(packet.get("sources"), list) else []
    usable_sources = [source for source in sources
                      if isinstance(source, Mapping) and source.get("usable_for_prediction") is True
                      and source.get("published_before_tipoff") is True]

    if enriched:
        record_data = state if isinstance(state, Mapping) else {}
        team_records = [record_data.get("okc"), record_data.get("opponent")]
        team_record_status = _combine([_status(value) for value in team_records])
        richer_form = (richer.get("regular_season_only_prior_form")
                       if isinstance(richer, Mapping) else None)
        form_status = _combine([
            _status(richer_form),
            team_record_status,
        ])
        rest_status = _combine([_status((rest or {}).get(team)) for team in ("okc", "opponent")]) \
            if isinstance(rest, Mapping) else "unavailable"
        h2h_status = _status(h2h)
        rich_stats_status = _status(richer)
    else:
        state_data = state if isinstance(state, Mapping) else {}
        record_statuses, form_statuses = [], []
        for team_key in ("okc", "opponent"):
            team_state = state_data.get(team_key)
            record_statuses.append(_status(team_state.get("record"))
                                   if isinstance(team_state, Mapping) else "unavailable")
            form_statuses.append(_status(team_state.get("recent_form"))
                                 if isinstance(team_state, Mapping) else "unavailable")
        team_record_status = _combine(record_statuses)
        form_status = _combine(form_statuses)
        rest_status = _combine([_status((rest or {}).get(team)) for team in ("okc", "opponent")]) \
            if isinstance(rest, Mapping) else "unavailable"
        h2h_status = _status(h2h)
        rich_stats_status = "partial" if team_record_status == "available" else "unavailable"

    target = _packet_target(packet)
    if enriched:
        listed_time = bool(target.get("venue_local_game_time") and target.get("venue_timezone"))
        exact_time = bool(target.get("exact_actual_tipoff_timestamp"))
        official_id = bool(target.get("official_nba_game_id"))
    else:
        tipoff = target.get("tipoff")
        listed_time = isinstance(tipoff, Mapping) and tipoff.get("status") == "available" and bool(tipoff.get("value"))
        exact_time = False
        official_id = False
    identity_complete = all([identity.get("game_id"), identity.get("date"), identity.get("home"),
                             identity.get("away"), TARGET_TEAM in {identity.get("home"), identity.get("away")}])
    target_time = "available" if exact_time else "partial" if listed_time else "unavailable"
    target_identity_time = "available" if identity_complete and exact_time else (
        "partial" if identity_complete else "unavailable")

    market_components = [
        market.get("favorite_side") in {"home", "away"},
        market.get("spread_points") is not None,
        market.get("total_line_points") is not None,
    ]
    market_status = "available" if all(market_components) else "partial" if any(market_components) else "unavailable"
    moneyline = market.get("moneyline") if isinstance(market.get("moneyline"), Mapping) else {}
    baseline_status = {
        "pure": _status(baselines.get("pure")),
        "residual": _status(baselines.get("residual")),
        "official": _status(baselines.get("official_numeric")),
    }
    if not enriched and "team_state_before" in packet:
        injuries = injuries or {"status": "unavailable"}
        starters = starters or {"status": "unavailable"}
        rotation = rotation or {"status": "unavailable"}
        news = news or {"status": "unavailable"}

    values = {
        "basic_team_stats": rich_stats_status,
        "recent_form": form_status,
        "rest": rest_status,
        "head_to_head": h2h_status,
        "injuries": _status(injuries),
        "expected_starters": _status(starters),
        "rotation_information": _status(rotation),
        "pregame_news": _status(news),
        "pure": baseline_status["pure"],
        "residual": baseline_status["residual"],
        "official": baseline_status["official"],
        "historical_market_reference": market_status,
        "moneyline": _status(moneyline),
        "game_identity": "available" if identity_complete else "unavailable",
        "official_game_id": "available" if official_id else "unavailable",
        "scheduled_tipoff": target_time,
        "game_identity_and_tipoff": target_identity_time,
        "point_in_time_snapshot_verified": "available" if (
            packet.get("data_provenance", {}).get("point_in_time_snapshot_verified") is True
            if isinstance(packet.get("data_provenance"), Mapping) else False) else "unavailable",
    }
    missing = [field for field, status in values.items()
               if status in {"unavailable", "partial"}]
    provenance = packet.get("data_provenance") if isinstance(packet.get("data_provenance"), Mapping) else {}
    return {
        **_game_fields(packet),
        "input_tier": input_tier,
        "packet_sha256": packet.get("packet_sha256"),
        "fields": values,
        "missing_or_partial_fields": missing,
        "usable_pregame_source_count": len(usable_sources),
        "source_count": len(sources),
        "historical_reconstruction": bool(packet.get("historical_reconstruction")
                                           or provenance.get("historical_reconstruction")),
        "point_in_time_snapshot_verified": provenance.get("point_in_time_snapshot_verified", False),
        "moneyline_detail": {
            "status": moneyline.get("status", "unavailable"),
            "home": moneyline.get("home"), "away": moneyline.get("away"),
        },
    }


def _identity_key(packet: Mapping[str, Any]) -> tuple[int, str, str, str]:
    identity = _game_identity(packet)
    return (identity["game_number"], identity["date"], identity["home"], identity["away"])


def _load_eligible_enriched_packets(
    enriched_packet_dir: str | Path | None,
    base_packets: Sequence[Mapping[str, Any]],
) -> tuple[dict[int, Mapping[str, Any]], dict[str, Any]]:
    if enriched_packet_dir is None:
        return {}, {"enabled": False, "reused_game_numbers": [], "rejected": []}
    directory = Path(enriched_packet_dir)
    if not directory.is_dir():
        return {}, {"enabled": True, "reused_game_numbers": [],
                    "rejected": [{"reason": "enriched_packet_directory_missing"}]}
    try:
        packets = _load_packets(directory)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        return {}, {"enabled": True, "reused_game_numbers": [],
                    "rejected": [{"reason": f"enriched_packets_invalid:{type(exc).__name__}"}]}
    base_by_number = {_game_number(packet): packet for packet in base_packets}
    accepted: dict[int, Mapping[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    for packet in packets:
        number = _game_number(packet)
        if number not in TARGET_GAME_NUMBERS or number not in base_by_number:
            rejected.append({"game_number": number, "reason": "not_an_approved_pilot_game"})
            continue
        base, identity = base_by_number[number], packet
        if _identity_key(base) != _identity_key(identity):
            rejected.append({"game_number": number, "reason": "game_identity_mismatch"})
            continue
        forbidden = _find_forbidden_result_fields(packet)
        if forbidden:
            rejected.append({"game_number": number,
                             "reason": "postgame_fields_present;packet_excluded"})
            continue
        if not isinstance(packet.get("sources"), list):
            rejected.append({"game_number": number, "reason": "source_records_missing"})
            continue
        accepted[number] = packet
    return accepted, {"enabled": True, "reused_game_numbers": sorted(accepted), "rejected": rejected}


def _write_new_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def build_preflight(
    rows: Sequence[Mapping[str, Any]],
    source_sha256: str,
    *,
    enriched_packet_dir: str | Path | None = DEFAULT_ENRICHED_PILOT_PACKET_DIR,
) -> dict[str, Any]:
    base_packets = build_packets_from_rows(rows, "historical_market_reference", source_sha256,
                                          range(1, 83))
    enriched_by_number, enriched_audit = _load_eligible_enriched_packets(enriched_packet_dir, base_packets)
    packets: list[dict[str, Any]] = []
    completeness: list[dict[str, Any]] = []
    for base in base_packets:
        number = _game_number(base)
        packet = dict(enriched_by_number.get(number, base))
        tier = "reconstructed_enriched_pilot" if number in enriched_by_number else "historical_dataset_reconstruction"
        if _find_forbidden_result_fields(packet):
            raise ValueError(f"forecast_packet_contains_postgame_field:game_{number:03d}")
        packets.append(packet)
        completeness.append(_completeness(packet, tier))
    if len(packets) != 82 or [_game_number(packet) for packet in packets] != list(range(1, 83)):
        raise ValueError("preflight_did_not_build_complete_82_game_schedule")
    section_names = list(completeness[0]["fields"])
    summary = {
        "game_count": len(packets),
        "input_tiers": dict(Counter(row["input_tier"] for row in completeness)),
        "field_status_counts": {
            name: dict(Counter(row["fields"][name] for row in completeness))
            for name in section_names
        },
        "market_reference_available_count": sum(
            row["fields"]["historical_market_reference"] == "available" for row in completeness),
        "moneyline_available_count": sum(row["fields"]["moneyline"] == "available" for row in completeness),
        "all_five_pilot_games_enriched": set(TARGET_GAME_NUMBERS).issubset(set(enriched_by_number)),
        "enriched_packet_reuse": enriched_audit,
    }
    manifest = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "target_team": TARGET_TEAM,
        "season": TARGET_SEASON,
        "competition": "NBA regular season",
        "selected_game_numbers": list(range(1, 83)),
        "source_sha256": source_sha256.upper(),
        "historical_reconstruction": True,
        "point_in_time_snapshot_verified": False,
        "deepseek_called": False,
        "actual_results_included": False,
        "enriched_packet_reuse": enriched_audit,
        "packet_hashes": {row["game_id"]: row["packet_sha256"] for row in completeness},
        "summary": summary,
    }
    return {"packets": packets, "completeness": completeness,
            "manifest": manifest, "summary": summary}


def write_preflight_artifacts(
    csv_path: str | Path = DEFAULT_CSV_PATH,
    output_dir: str | Path = REPOSITORY_ROOT / "v2" / "pregame_system" / "outputs" / "forecasting_preflight_okc_2025_26",
    *,
    enriched_packet_dir: str | Path | None = DEFAULT_ENRICHED_PILOT_PACKET_DIR,
) -> dict[str, Any]:
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError("preflight_output_directory_already_exists")
    rows, digest = _read_verified_csv(csv_path)
    result = build_preflight(rows, digest, enriched_packet_dir=enriched_packet_dir)
    output.mkdir(parents=True, exist_ok=False)
    packet_dir = output / "forecast_packets"
    packet_dir.mkdir()
    for packet in result["packets"]:
        number = _game_number(packet)
        _write_new_json(packet_dir / f"game_{number:03d}.json", packet)
    _write_new_json(output / "manifest.json", result["manifest"])
    _write_new_json(output / "completeness.json", {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "source_sha256": digest,
        "deepseek_called": False,
        "summary": result["summary"],
        "games": result["completeness"],
    })
    scalar_fields = ["game_id", "game_number", "date", "home", "away", "input_tier",
                     "packet_sha256", "usable_pregame_source_count", "source_count",
                     "historical_reconstruction", "point_in_time_snapshot_verified",
                     "missing_or_partial_fields"]
    status_fields = list(result["completeness"][0]["fields"])
    csv_rows = []
    for item in result["completeness"]:
        row = {key: json.dumps(item.get(key), ensure_ascii=False) if key == "missing_or_partial_fields"
               else item.get(key) for key in scalar_fields}
        row.update(item["fields"])
        row["moneyline_detail"] = json.dumps(item["moneyline_detail"], ensure_ascii=False)
        csv_rows.append(row)
    _write_new_csv(output / "completeness.csv", csv_rows,
                   scalar_fields + status_fields + ["moneyline_detail"])
    return {"output_dir": str(output.resolve()), "game_count": len(result["packets"]),
            "summary": result["summary"], "manifest": result["manifest"]}
