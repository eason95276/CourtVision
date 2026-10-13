"""Frozen, non-predictive facts about rolling samples and schedule evidence."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import math
import re
from typing import Any

import pandas as pd

from .storage import clean, instant


FACT_CONTEXT_SCHEMA = "pregame-fact-context-v1"
FACT_VIEW_SCHEMA = "pregame-fact-view-v1"
WINDOW_GAMES = 15
DATE_TIMEZONE = "America/New_York"

METRICS = {
    "off_rating_15": ("off_rating", "points_per_100_possessions"),
    "def_rating_15": ("def_rating", "points_allowed_per_100_possessions"),
    "net_rating_15": ("net_rating", "points_per_100_possessions"),
    "pace_15": ("pace", "possessions_per_48_minutes"),
    "win_rate_15": ("win_rate", "win_fraction"),
    "three_pct_15": ("three_pct", "fraction"),
    "rebound_share_15": ("rebound_share", "fraction"),
    "turnover_rate_15": ("turnover_rate", "turnovers_per_possession"),
}
METRIC_METHODS = {
    "off_rating_15": "v1_per_game_mean_shift_1_rolling_15",
    "def_rating_15": "v1_per_game_mean_shift_1_rolling_15",
    "net_rating_15": "v1_per_game_mean_shift_1_rolling_15",
    "pace_15": "v1_per_game_mean_shift_1_rolling_15",
    "win_rate_15": "v1_per_game_mean_shift_1_rolling_15",
    "three_pct_15": "v1_per_game_mean_shift_1_rolling_15",
    "rebound_share_15": "v1_per_game_mean_shift_1_rolling_15",
    "turnover_rate_15": "v1_per_game_mean_shift_1_rolling_15",
}


def _get(game: dict[str, Any], side: str, key: str, default=None):
    direct = game.get(f"{side}_{key}")
    if direct is not None:
        return direct
    nested = game.get(side)
    if isinstance(nested, dict):
        return nested.get(key, default)
    return default


def _season_label(value: Any) -> str | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return f"{year}-{(year + 1) % 100:02d}"


def _normal_competition(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().casefold().replace("_", " ").replace("-", " ")
    compact = " ".join(raw.split())
    aliases = {
        "regular season": "regular_season", "regularseason": "regular_season",
        "play in": "play_in", "playin": "play_in", "play in tournament": "play_in",
        "playoffs": "playoffs", "playoff": "playoffs", "postseason": "playoffs",
        "preseason": "preseason", "pre season": "preseason",
    }
    return aliases.get(compact)


def _date_series(frame: pd.DataFrame) -> pd.Series:
    values = pd.to_datetime(frame["game_date"], errors="coerce")
    if getattr(values.dt, "tz", None) is not None:
        values = values.dt.tz_convert(DATE_TIMEZONE).dt.tz_localize(None)
    return values


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _cutoff_date(cutoff: str) -> pd.Timestamp:
    """Match the exact conservative history-day policy used by feature_row."""
    local = pd.Timestamp(instant(cutoff)).tz_convert(DATE_TIMEZONE)
    day = local.normalize().tz_localize(None)
    return day - pd.Timedelta(days=1) if local.hour < 12 else day


def _metric_window(team_rows: pd.DataFrame, enriched: pd.DataFrame,
                   rolling_values: dict[str, Any], side: str) -> dict[str, Any]:
    game_ids = [str(value) for value in team_rows.get("game_id", pd.Series(dtype=str)).tolist()]
    requested = WINDOW_GAMES
    observed = len(game_ids)
    metrics: dict[str, Any] = {}
    status = "complete" if observed == requested else "partial"
    reasons: list[str] = []
    if observed < requested:
        reasons.append("fewer_than_requested_window_games")
    if observed > requested:
        reasons.append("window_selection_exceeded_requested_size")
        status = "partial"

    indexed = enriched[enriched["team_id"].astype(str).isin(team_rows["team_id"].astype(str))]
    # Keep only the selected team and selected game rows. Use the same normalized
    # per-game columns that the V1 feature builder rolls after shift(1).
    indexed = indexed[(indexed["team_id"].astype(str) == str(team_rows.iloc[-1]["team_id"])) &
                      (indexed["game_id"].astype(str).isin(game_ids))]
    for public_name, (column, unit) in METRICS.items():
        series = pd.to_numeric(indexed.get(column, pd.Series(dtype=float)), errors="coerce")
        finite = series.map(lambda item: _finite(item) is not None)
        valid_games = int(finite.sum())
        missing_games = max(0, observed - valid_games)
        computed = float(series[finite].mean()) if valid_games >= 3 else None
        reported = _finite(rolling_values.get(f"{side}_{column}_roll15"))
        if public_name == "win_rate_15":
            reported = _finite(rolling_values.get(f"{side}_win_rate_roll15"))
        validation = "not_comparable"
        if reported is not None and computed is not None:
            validation = "matched" if math.isclose(reported, computed, rel_tol=1e-9, abs_tol=1e-8) else "mismatch"
            if validation == "mismatch":
                status = "partial"
                reasons.append(f"rolling_value_mismatch:{public_name}")
        elif reported is None and computed is None:
            validation = "both_unavailable"
        elif reported is None or computed is None:
            validation = "mismatch"
            status = "partial"
            reasons.append(f"rolling_value_mismatch:{public_name}")
        metrics[public_name] = {
            "value": reported,
            "valid_games": valid_games,
            "missing_games": missing_games,
            "unit": unit,
            "aggregation": "mean_of_per_game_values",
            "method": METRIC_METHODS[public_name],
            "validation": validation,
        }
    return {
        "requested_games": requested,
        "observed_games": observed,
        "game_ids": game_ids,
        "metrics": metrics,
        "status": status,
        "reason": ";".join(dict.fromkeys(reasons)) or None,
    }


def _team_window(game: dict[str, Any], games: pd.DataFrame, stats: pd.DataFrame,
                 cutoff_day: pd.Timestamp, rolling_values: dict[str, Any], side: str) -> dict[str, Any]:
    team_id = _get(game, side, "team_id", _get(game, side, "id"))
    if team_id is None or games.empty or stats.empty:
        return {
            "status": "unavailable", "reason": "team_history_unavailable", "requested_games": WINDOW_GAMES,
            "observed_games": 0, "game_ids": [], "season_counts": {}, "competition_counts": {},
            "first_game_date": None, "last_game_date": None, "date_timezone": DATE_TIMEZONE, "metrics": {},
        }
    g = games.copy(deep=True)
    s = stats.copy(deep=True)
    if "game_date" not in g or "game_id" not in g or "team_id" not in s or "game_id" not in s:
        return {
            "status": "unavailable", "reason": "history_identity_or_date_columns_missing", "requested_games": WINDOW_GAMES,
            "observed_games": 0, "game_ids": [], "season_counts": {}, "competition_counts": {},
            "first_game_date": None, "last_game_date": None, "date_timezone": DATE_TIMEZONE, "metrics": {},
        }
    g["_fact_day"] = _date_series(g).dt.normalize()
    prior = g[(g["_fact_day"] < cutoff_day) & (g["game_id"].astype(str) != str(game.get("game_id", game.get("id"))))]
    prior_ids = set(prior["game_id"].astype(str))
    s = s[s["game_id"].astype(str).isin(prior_ids)].copy()
    selected = s[s["team_id"].astype(str) == str(team_id)].copy()
    selected["_fact_day"] = _date_series(selected).dt.normalize()
    selected = selected.sort_values(["_fact_day", "game_id"], kind="stable").tail(WINDOW_GAMES)
    # The original feature path expects one team row per game. Duplicates make
    # an exact sample description ambiguous, so keep the numeric report but mark
    # its provenance unavailable rather than silently deduplicating.
    duplicate_rows = selected["game_id"].astype(str).duplicated().any()
    selected_ids = selected["game_id"].astype(str).tolist()
    game_index = {str(row["game_id"]): row for row in prior.to_dict("records")}
    seasons: Counter[str] = Counter()
    competitions: Counter[str] = Counter()
    unknown_composition = 0
    dates: list[pd.Timestamp] = []
    for record in selected.to_dict("records"):
        gid = str(record["game_id"])
        source_game = game_index.get(gid)
        if source_game is None:
            unknown_composition += 1
            continue
        season = source_game.get("season_start", record.get("season_start"))
        try:
            seasons[str(int(season))] += 1
        except (TypeError, ValueError):
            unknown_composition += 1
        competition = _normal_competition(
            source_game.get("gameType", source_game.get("game_type", source_game.get("competition")))
        )
        if competition is None:
            unknown_composition += 1
        else:
            competitions[competition] += 1
        date = record.get("game_date")
        try:
            stamp = pd.Timestamp(date)
            if stamp.tzinfo is not None:
                stamp = stamp.tz_convert(DATE_TIMEZONE).tz_localize(None)
            dates.append(stamp.normalize())
        except (TypeError, ValueError):
            unknown_composition += 1
    try:
        from .features import enrich
        prior_stats = s.copy(deep=True)
        enriched = enrich(prior_stats)
        window = _metric_window(selected, enriched, rolling_values, side)
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        window = {
            "requested_games": WINDOW_GAMES, "observed_games": len(selected), "game_ids": selected_ids,
            "metrics": {}, "status": "unavailable", "reason": "metric_source_rows_unavailable",
        }
    reasons = [window.get("reason")] if window.get("reason") else []
    if duplicate_rows:
        reasons.append("duplicate_team_stat_rows_for_game")
    if unknown_composition:
        reasons.append("season_competition_or_date_metadata_incomplete")
    if duplicate_rows or unknown_composition or window.get("status") == "unavailable":
        status = "partial" if selected_ids else "unavailable"
    else:
        status = window.get("status", "partial")
    return {
        "status": status,
        "reason": ";".join(dict.fromkeys(reasons)) or None,
        "requested_games": WINDOW_GAMES,
        "observed_games": len(selected_ids),
        "game_ids": selected_ids,
        "season_counts": dict(sorted(seasons.items())),
        "competition_counts": dict(sorted(competitions.items())),
        "first_game_date": min(dates).date().isoformat() if dates else None,
        "last_game_date": max(dates).date().isoformat() if dates else None,
        "date_timezone": DATE_TIMEZONE,
        "metrics": window.get("metrics", {}),
    }


def _schedule_team(game: dict[str, Any], feature_cutoff: str, schedule: list[dict[str, Any]], side: str) -> dict[str, Any]:
    team_id = _get(game, side, "team_id", _get(game, side, "id"))
    game_id = str(game.get("game_id", game.get("id", "")))
    tipoff = instant(game["scheduled_tipoff"])
    tip_local_day = pd.Timestamp(tipoff).tz_convert(DATE_TIMEZONE).normalize()
    eligible_refs: list[dict[str, Any]] = []
    previous_day: list[tuple[dict[str, Any], str]] = []
    saw_late = False
    prior_day_invalid = False
    prior_day_unknown = False
    for item in schedule or []:
        if not isinstance(item, dict) or str(item.get("game_id", item.get("id", ""))) == game_id:
            continue
        if team_id not in (item.get("home_team_id"), item.get("away_team_id")):
            continue
        if item.get("season_start") is not None and game.get("season_start") is not None:
            try:
                if int(item["season_start"]) != int(game["season_start"]):
                    continue
            except (TypeError, ValueError):
                continue
        raw_source = item.get("source") or {}
        captured = raw_source.get("captured_at") or item.get("schedule_captured_at")
        source_ref = raw_source.get("url") or raw_source.get("path") or raw_source.get("source_ref")
        if not captured or not source_ref:
            continue
        try:
            if instant(captured) > instant(feature_cutoff):
                saw_late = True
                continue
            prior_tip = item.get("scheduled_tipoff") or item.get("tipoff")
            prior_local = pd.Timestamp(instant(prior_tip)).tz_convert(DATE_TIMEZONE)
        except (KeyError, TypeError, ValueError):
            continue
        raw_status = " ".join(str(item.get(key) or "") for key in ("status", "status_detail", "status_text")).strip()
        normalized_status = " ".join(raw_status.casefold().replace("_", " ").replace("-", " ").split())
        invalid_status = any(token in normalized_status for token in (
            "cancel", "postpon", "reschedul", "delayed", "suspend", "abandon", "aborted", "no contest", "void",
        ))
        if invalid_status:
            status_kind = "invalid"
        elif normalized_status in {"final", "completed", "complete", "finished", "post", "status final"}:
            status_kind = "recorded_game"
        elif normalized_status in {"scheduled", "pre", "upcoming", "pending", "not started", "status scheduled"}:
            status_kind = "planned_schedule"
        else:
            status_kind = "unknown"
        ref = {
            "game_id": str(item.get("game_id", item.get("id"))),
            "source_ref": str(source_ref),
            "source_type": raw_source.get("source_type") or raw_source.get("kind"),
            "captured_at": str(captured),
            "scheduled_tipoff": str(prior_tip),
            "source_status": raw_status or None,
        }
        eligible_refs.append(ref)
        if prior_local.normalize() == tip_local_day - pd.Timedelta(days=1):
            if status_kind == "invalid":
                prior_day_invalid = True
            elif status_kind == "unknown":
                prior_day_unknown = True
            else:
                previous_day.append((ref, status_kind))
    if previous_day:
        refs = [item[0] for item in previous_day]
        bases = {item[1] for item in previous_day}
        basis = "recorded_game" if bases == {"recorded_game"} else "planned_schedule"
        return {
            "status": "available", "reason": None,
            "basis": {"type": basis, "game_ids": [item["game_id"] for item in refs]},
            "back_to_back": True, "source_refs": refs,
        }
    if saw_late:
        reason = "schedule_evidence_after_feature_cutoff"
    elif prior_day_invalid:
        reason = "prior_day_schedule_cancelled_or_invalid"
    elif prior_day_unknown:
        reason = "prior_day_schedule_status_unverified"
    elif eligible_refs:
        reason = "prior_day_schedule_not_proven_or_calendar_coverage_incomplete"
    else:
        reason = "no_cutoff_eligible_schedule_evidence"
    return {
        "status": "unavailable", "reason": reason,
        "basis": {"type": "partial_schedule", "game_ids": [item["game_id"] for item in eligible_refs]} if eligible_refs else None,
        "back_to_back": None,
        "source_refs": eligible_refs,
    }


def build_fact_context(game: dict[str, Any], feature_cutoff: str, games: pd.DataFrame | None,
                       stats: pd.DataFrame | None, rolling_values: dict[str, Any] | None = None,
                       schedule: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Describe the exact local rolling window and eligible schedule evidence.

    Does not mutate input frames, calculate or alter a prediction, access the
    network, or persist data. Numeric values are copied from the existing feature
    row and independently checked against the same per-game source rows.
    """
    cutoff = datetime.fromisoformat(str(feature_cutoff).replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise ValueError("fact_context_cutoff_timezone_missing")
    if instant(feature_cutoff) >= instant(game["scheduled_tipoff"]):
        raise ValueError("fact_context_cutoff_not_pregame")
    game_id = str(game.get("game_id", game.get("id", "")))
    if not game_id:
        raise ValueError("fact_context_game_id_missing")
    game_frame = games if isinstance(games, pd.DataFrame) else pd.DataFrame()
    stats_frame = stats if isinstance(stats, pd.DataFrame) else pd.DataFrame()
    values = rolling_values or {}
    cutoff_day = _cutoff_date(feature_cutoff)
    team_form = {
        side: _team_window(game, game_frame, stats_frame, cutoff_day, values, side)
        for side in ("home", "away")
    }
    schedule_context = {
        side: _schedule_team(game, feature_cutoff, schedule or [], side)
        for side in ("home", "away")
    }
    return clean({
        "schema_version": FACT_CONTEXT_SCHEMA,
        "game_id": game_id,
        "feature_cutoff": cutoff.isoformat(),
        "team_form": team_form,
        "schedule": schedule_context,
    })


def valid_fact_context(context: Any, game_id: str, input_cutoff: str,
                       scheduled_tipoff: str) -> dict[str, Any] | None:
    """Return a copied context only when its identity and temporal scope match."""
    if not isinstance(context, dict) or context.get("schema_version") != FACT_CONTEXT_SCHEMA:
        return None
    if context.get("game_id") != game_id:
        return None
    try:
        feature_cutoff = context.get("feature_cutoff")
        if (not feature_cutoff or instant(feature_cutoff) > instant(input_cutoff)
                or instant(feature_cutoff) >= instant(scheduled_tipoff)):
            return None
    except (TypeError, ValueError):
        return None
    safe = deepcopy(context)
    for side in ("home", "away"):
        schedule = (safe.get("schedule") or {}).get(side)
        if not isinstance(schedule, dict):
            continue
        refs = schedule.get("source_refs") or []
        basis = (schedule.get("basis") or {}).get("type")
        valid_refs = []
        for ref in refs:
            try:
                if (ref.get("source_ref") and ref.get("game_id")
                        and instant(ref.get("captured_at")) <= instant(feature_cutoff)):
                    valid_refs.append(ref)
            except (TypeError, ValueError, AttributeError):
                continue
        if schedule.get("back_to_back") is True and (not valid_refs or basis not in {"planned_schedule", "recorded_game"}):
            safe["schedule"][side] = {
                "status": "unavailable", "reason": "schedule_source_reference_invalid",
                "basis": None, "back_to_back": None, "source_refs": [],
            }
        elif schedule.get("back_to_back") is False:
            # The local calendar is not complete enough to prove absence.
            schedule["back_to_back"] = None
            schedule["reason"] = schedule.get("reason") or "calendar_coverage_not_verified"
        else:
            schedule["source_refs"] = valid_refs
    return safe


def _legacy_team_form(snapshot: dict[str, Any], side: str) -> dict[str, Any]:
    cold = (((snapshot.get("roster_context") or {}).get("teams") or {}).get(side) or {}).get("cold_start") or {}
    raw_metrics = (((snapshot.get("background") or {}).get("recent_form") or {}).get(side) or {})
    game_ids = cold.get("rolling_15_game_ids")
    observed = cold.get("rolling_15_observed_games")
    if observed is None and isinstance(game_ids, list):
        observed = len(game_ids)
    counts = {}
    season_start = cold.get("season_start") or snapshot.get("season_start")
    for key, target in (("rolling_15_current_season_games", "current"),
                        ("rolling_15_previous_season_games", "previous"),
                        ("rolling_15_older_season_games", "older")):
        count = cold.get(key)
        if count is not None:
            label_year = season_start if target == "current" else int(season_start) - 1 if target == "previous" and season_start is not None else None
            counts[str(label_year) if label_year is not None else target] = int(count)
    metrics = {}
    for name, (_, unit) in METRICS.items():
        metrics[name] = {
            "value": _finite(raw_metrics.get(name)), "valid_games": None, "missing_games": None,
            "unit": unit, "aggregation": None, "method": "not_recorded_in_legacy_version",
        }
    explicit_window = observed is not None or bool(game_ids) or any(name in raw_metrics for name in METRICS)
    return {
        "status": "partial" if explicit_window else "unavailable",
        "reason": "legacy_valid_n_competition_and_date_range_not_recorded",
        "requested_games": WINDOW_GAMES if explicit_window else None,
        "observed_games": int(observed) if observed is not None else None,
        "game_ids": list(game_ids) if isinstance(game_ids, list) else [],
        "season_counts": counts,
        "competition_counts": None,
        "first_game_date": None,
        "last_game_date": None,
        "date_timezone": DATE_TIMEZONE,
        "metrics": metrics,
    }


def _unknown_schedule(reason: str) -> dict[str, Any]:
    return {side: {"status": "unavailable", "reason": reason, "basis": None,
                   "back_to_back": None, "source_refs": []} for side in ("home", "away")}


def _legacy_fact_context(version: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    technical = snapshot.get("technical") or {}
    feature_cutoff = technical.get("cutoff")
    try:
        if feature_cutoff and instant(feature_cutoff) > instant(version.get("input_cutoff_at")):
            feature_cutoff = None
    except (TypeError, ValueError):
        feature_cutoff = None
    return {
        "schema_version": FACT_CONTEXT_SCHEMA,
        "game_id": version.get("game_id"),
        "feature_cutoff": feature_cutoff,
        "team_form": {side: _legacy_team_form(snapshot, side) for side in ("home", "away")},
        # Legacy home/away_rest_days and back_to_back could have come from
        # capped features, so they are never upgraded into schedule facts.
        "schedule": _unknown_schedule("legacy_version_has_no_verified_schedule_basis"),
    }


def build_version_fact_view(version: dict[str, Any] | None) -> dict[str, Any] | None:
    """Make a bound, read-only view of facts frozen in the selected version.

    This is an outer API view. It never edits or re-hashes ``version`` and never
    consults current roster, result, or another version.
    """
    if not isinstance(version, dict):
        return None
    version_id = version.get("version_id")
    game_id = version.get("game_id")
    payload_hash = version.get("payload_sha256")
    if not all(isinstance(value, str) and value for value in (version_id, game_id)):
        return None
    if not isinstance(payload_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", payload_hash):
        return None
    snapshot = version.get("input_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}
    snapshot_game_id = snapshot.get("id") or snapshot.get("game_id")
    if snapshot_game_id and str(snapshot_game_id) != game_id:
        return None
    identity = version.get("game_identity") or {}
    if identity.get("game_id") and str(identity["game_id"]) != game_id:
        return None
    for side in ("home", "away"):
        expected_team = identity.get(f"{side}_team_id")
        snapshot_team = (snapshot.get(side) or {}).get("id")
        if expected_team is not None and snapshot_team is not None:
            try:
                if int(expected_team) != int(snapshot_team):
                    return None
            except (TypeError, ValueError):
                return None
    identity_tipoff = identity.get("scheduled_tipoff")
    snapshot_tipoff = snapshot.get("tipoff")
    if identity_tipoff and snapshot_tipoff:
        try:
            if instant(identity_tipoff) != instant(snapshot_tipoff):
                return None
        except (TypeError, ValueError):
            return None
    frozen = snapshot.get("fact_context")
    origin = "new_frozen"
    if (not isinstance(frozen, dict) or frozen.get("schema_version") != FACT_CONTEXT_SCHEMA
            or frozen.get("game_id") != game_id):
        frozen = _legacy_fact_context(version, snapshot)
        origin = "legacy_frozen"
    else:
        tipoff = identity_tipoff or snapshot_tipoff
        validated = valid_fact_context(frozen, game_id, version.get("input_cutoff_at"), tipoff)
        if validated is None:
            frozen = _legacy_fact_context(version, snapshot)
            origin = "legacy_frozen"
        else:
            frozen = validated
    return clean({
        "schema_version": FACT_VIEW_SCHEMA,
        "game_id": game_id,
        "version_id": version_id,
        "source_payload_sha256": payload_hash,
        "origin": origin,
        "context": deepcopy(frozen),
    })
