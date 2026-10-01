"""Cutoff-safe direct-meeting and season-phase context for pregame analysis."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .storage import clean, content_hash, instant


def _game_id(value: Any) -> str:
    text = str(value)
    return text.zfill(10) if text.isdigit() else text


def _last_complete_game_day(cutoff_et: pd.Timestamp) -> pd.Timestamp:
    day = cutoff_et.normalize()
    # Historical box scores do not record when a game became final. Keep the
    # same conservative day boundary as the numeric feature pipeline.
    return day - pd.Timedelta(days=1) if cutoff_et.hour < 12 else day


def _team_context(team: dict[str, Any] | None) -> dict[str, Any]:
    cold = (team or {}).get("cold_start") or {}
    if cold.get("status") not in (None, "available"):
        return {"current_season_games": None}
    observed = cold.get("current_season_games_observed")
    if observed is None:
        observed = cold.get("rolling_15_current_season_games")
    return {"current_season_games": int(observed) if observed is not None else None}


def season_phase_context(
    home_context: dict[str, Any] | None,
    away_context: dict[str, Any] | None,
    season_form: dict[str, Any] | None = None,
) -> dict[str, Any]:
    home = _team_context(home_context)["current_season_games"]
    away = _team_context(away_context)["current_season_games"]
    if (season_form or {}).get("status") == "available":
        teams = season_form.get("teams") or {}
        home = int((teams.get("home") or {}).get("current_season_games") or 0)
        away = int((teams.get("away") or {}).get("current_season_games") or 0)
    if home is None or away is None:
        return {
            "phase": "unavailable",
            "home_current_season_games": home,
            "away_current_season_games": away,
            "comparison_sample_games": None,
            "primary_evidence": "season_game_counts_unavailable",
            "display_label": "赛季场次待核对",
        }
    sample = min(home, away)
    if sample == 0:
        phase = "opening_night"
        primary = "previous_season_playoffs_or_finish"
        label = "上赛季季后赛 / 上赛季末段"
    elif sample <= 4:
        phase = "very_early"
        primary = "current_season_early_signal_with_previous_baseline"
        label = "本赛季开局（小样本）"
    elif sample <= 9:
        phase = "early_sample"
        primary = "current_season_last5_with_previous_baseline"
        label = "本赛季近5场"
    elif sample <= 14:
        phase = "comparison_ready"
        primary = "current_season_first10_vs_previous_season"
        label = "本赛季前10场 vs 上赛季"
    else:
        phase = "current_season"
        primary = "current_season_rolling5_and_rolling15"
        label = "本赛季近15场"
    return {
        "phase": phase,
        "home_current_season_games": home,
        "away_current_season_games": away,
        "comparison_sample_games": sample,
        "primary_evidence": primary,
        "display_label": label,
    }


def _row_value(row: pd.Series, *names: str) -> Any:
    for name in names:
        if name in row and pd.notna(row[name]):
            return row[name]
    return None


def _team_stats(stats: pd.DataFrame | None, game_id: str, team_id: int) -> dict[str, Any] | None:
    if stats is None or stats.empty:
        return None
    game_col = "game_id" if "game_id" in stats.columns else "gameId"
    team_col = "team_id" if "team_id" in stats.columns else "teamId"
    rows = stats[(stats[game_col].map(_game_id) == game_id) & (stats[team_col].astype(int) == team_id)]
    if len(rows) != 1:
        return None
    row = rows.iloc[0]
    required = (
        "teamScore", "fieldGoalsAttempted", "fieldGoalsMade",
        "threePointersAttempted", "threePointersMade", "freeThrowsAttempted",
        "reboundsOffensive", "reboundsTotal", "turnovers",
    )
    if any(key not in row or pd.isna(row[key]) for key in required):
        return None
    fga = float(_row_value(row, "fieldGoalsAttempted") or 0)
    fgm = float(_row_value(row, "fieldGoalsMade") or 0)
    tpa = float(_row_value(row, "threePointersAttempted") or 0)
    tpm = float(_row_value(row, "threePointersMade") or 0)
    fta = float(_row_value(row, "freeThrowsAttempted") or 0)
    oreb = float(_row_value(row, "reboundsOffensive") or 0)
    rebounds = float(_row_value(row, "reboundsTotal") or 0)
    turnovers = float(_row_value(row, "turnovers") or 0)
    points = float(_row_value(row, "teamScore") or 0)
    possessions = fga - oreb + turnovers + 0.44 * fta
    return {
        "points": points,
        "field_goals_made": fgm,
        "field_goals_attempted": fga,
        "three_made": tpm,
        "three_attempted": tpa,
        "free_throw_attempted": fta,
        "offensive_rebounds": oreb,
        "total_rebounds": rebounds,
        "turnovers": turnovers,
        "estimated_possessions": possessions if possessions > 0 else None,
        "off_rating": 100 * points / possessions if possessions > 0 else None,
    }


def _pool(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    totals = {
        key: sum(float(row.get(key) or 0) for row in rows)
        for key in (
            "points", "three_made", "three_attempted", "free_throw_attempted",
            "offensive_rebounds", "total_rebounds", "turnovers", "estimated_possessions",
        )
    }
    games = len(rows)
    return {
        "games": games,
        "points_per_game": totals["points"] / games,
        "three_pct": totals["three_made"] / totals["three_attempted"] if totals["three_attempted"] else None,
        "three_attempts_per_game": totals["three_attempted"] / games,
        "offensive_rebounds_per_game": totals["offensive_rebounds"] / games,
        "rebounds_per_game": totals["total_rebounds"] / games,
        "turnovers_per_game": totals["turnovers"] / games,
        "free_throw_attempts_per_game": totals["free_throw_attempted"] / games,
        "off_rating": 100 * totals["points"] / totals["estimated_possessions"]
        if totals["estimated_possessions"] else None,
    }


def _form_window(rows: pd.DataFrame, limit: int) -> dict[str, Any]:
    selected = rows.tail(limit)
    if selected.empty:
        return {"status": "no_games", "games": 0, "game_ids": []}
    count = len(selected)

    def sum_column(name: str) -> float | None:
        if name not in selected.columns or selected[name].isna().any():
            return None
        return float(selected[name].sum())

    points = sum_column("teamScore")
    against = sum_column("opponentScore")
    made = sum_column("threePointersMade")
    attempts = sum_column("threePointersAttempted")
    rebounds = sum_column("reboundsTotal")
    turnovers = sum_column("turnovers")
    wins = sum_column("win")
    return clean({
        "status": "available",
        "games": count,
        "game_ids": selected["_game_id"].tolist(),
        "first_game_date": selected["_date"].iloc[0].isoformat(),
        "last_game_date": selected["_date"].iloc[-1].isoformat(),
        "win_rate": wins / count if wins is not None else None,
        "points_for_per_game": points / count if points is not None else None,
        "points_against_per_game": against / count if against is not None else None,
        "point_margin_per_game": (points - against) / count if points is not None and against is not None else None,
        "three_pct": made / attempts if made is not None and attempts else None,
        "rebounds_per_game": rebounds / count if rebounds is not None else None,
        "turnovers_per_game": turnovers / count if turnovers is not None else None,
    })


def build_season_form_context(
    game: dict[str, Any],
    cutoff: str,
    stats: pd.DataFrame | None,
) -> dict[str, Any]:
    """Separate current and previous-season team windows, without model usage."""
    if stats is None or stats.empty:
        return {"status": "unavailable", "reason": "historical_team_stats_unavailable", "numeric_model_usage": False}
    cutoff_et = pd.Timestamp(instant(cutoff)).tz_convert("America/New_York").tz_localize(None)
    last_complete_day = _last_complete_game_day(cutoff_et)
    target_id = str(game.get("game_id") or "")
    season = int(game.get("season_start") or cutoff_et.year)
    frame = stats.copy()
    game_col = "game_id" if "game_id" in frame.columns else "gameId"
    team_col = "team_id" if "team_id" in frame.columns else "teamId"
    date_col = "game_date" if "game_date" in frame.columns else "gameDate"
    frame["_game_id"] = frame[game_col].map(_game_id)
    frame["_date"] = pd.to_datetime(frame[date_col])
    frame = frame[
        (frame["_date"].dt.normalize() < last_complete_day)
        & (frame["_game_id"] != target_id)
    ]
    if frame.duplicated([team_col, "_game_id"]).any():
        return {"status": "unavailable", "reason": "duplicate_team_game_rows", "numeric_model_usage": False}
    teams: dict[str, Any] = {}
    for side in ("home", "away"):
        team_id = int(game[f"{side}_team_id"])
        own = frame[frame[team_col].astype(int) == team_id].sort_values(["_date", "_game_id"])
        current = own[own["season_start"].astype(int) == season]
        previous = own[own["season_start"].astype(int) == season - 1]
        playoffs = previous[previous["_game_id"].str.match(r"^004\d{7}$")]
        finals = playoffs[playoffs["_game_id"].str.match(r"^004\d{2}004\d{2}$")]
        wins_available = "win" in playoffs and not playoffs["win"].isna().any()
        playoff_wins = int(playoffs["win"].sum()) if wins_available and len(playoffs) else None
        finals_wins = int(finals["win"].sum()) if len(finals) and "win" in finals and not finals["win"].isna().any() else None
        teams[side] = {
            "team_id": team_id,
            "current_season_games": len(current),
            "previous_season_games": len(previous),
            "current_last5": _form_window(current, 5),
            "current_last10": _form_window(current, 10),
            "current_last15": _form_window(current, 15),
            "previous_last10": _form_window(previous, 10),
            "previous_playoffs": {
                "status": "available" if len(playoffs) else "no_local_games",
                "season_start": season - 1,
                "observed_games": len(playoffs),
                "observed_wins": playoff_wins,
                "observed_losses": len(playoffs) - playoff_wins if playoff_wins is not None else None,
                "last_observed_round": int(playoffs["_game_id"].iloc[-1][7]) if len(playoffs) else None,
                "finals_wins": finals_wins,
                "championship_clinched": True if finals_wins == 4 else None,
                "game_ids": playoffs["_game_id"].tolist(),
                "semantics": "observed pre-cutoff playoff games; title confirmed only by four recorded Finals wins",
            },
        }
    result = {
        "status": "available",
        "game_id": target_id,
        "cutoff": cutoff,
        "season_start": season,
        "competition_scope": "all competitive games in each season",
        "numeric_model_usage": False,
        "teams": teams,
    }
    result["context_hash"] = content_hash(result)
    return clean(result)


def _series_key(row: pd.Series) -> str | None:
    if not bool(_row_value(row, "is_playoffs") or False):
        return None
    game_id = _game_id(_row_value(row, "game_id", "gameId"))
    return f"{int(_row_value(row, 'season_start') or 0)}:{game_id[:9]}"


def _relevance(meetings: list[dict[str, Any]], target_season: int) -> tuple[str, list[str]]:
    if not meetings:
        return "unavailable", ["no_prior_meetings"]
    recent = [row for row in meetings if int(row.get("season_start") or -1) >= target_season - 1]
    playoff = [row for row in recent if row.get("is_playoffs")]
    series_counts: dict[str, int] = {}
    for row in playoff:
        key = str(row.get("series_key") or "")
        if key:
            series_counts[key] = series_counts.get(key, 0) + 1
    largest_series = max(series_counts.values(), default=0)
    if largest_series >= 2:
        return "high", ["recent_playoff_series", f"{largest_series}_series_games"]
    if len(recent) >= 2:
        return "medium", ["multiple_recent_regular_season_meetings"]
    if recent and abs(float(recent[-1].get("home_margin") or 0)) >= 15:
        return "medium", ["recent_large_margin_meeting"]
    return "low", ["single_or_older_meeting"]


def build_matchup_history(
    game: dict[str, Any],
    cutoff: str,
    games: pd.DataFrame | None,
    stats: pd.DataFrame | None = None,
    *,
    max_meetings: int = 10,
) -> dict[str, Any]:
    if games is None or games.empty:
        return {
            "status": "unavailable",
            "reason": "historical_games_unavailable",
            "numeric_model_usage": False,
        }
    # Historical game_date values are naive America/New_York wall times.
    cutoff_dt = pd.Timestamp(instant(cutoff)).tz_convert("America/New_York").tz_localize(None)
    last_complete_day = _last_complete_game_day(cutoff_dt)
    target_id = str(game.get("game_id") or "")
    home_id = int(game["home_team_id"])
    away_id = int(game["away_team_id"])
    target_season = int(game.get("season_start") or cutoff_dt.year)
    frame = games.copy()
    frame["_game_id"] = frame["game_id" if "game_id" in frame.columns else "gameId"].map(_game_id)
    frame["_date"] = pd.to_datetime(frame["game_date" if "game_date" in frame.columns else "gameDate"])
    pair = frame[
        (((frame["home_team_id"].astype(int) == home_id) & (frame["away_team_id"].astype(int) == away_id))
         | ((frame["home_team_id"].astype(int) == away_id) & (frame["away_team_id"].astype(int) == home_id)))
        & (frame["_date"].dt.normalize() < last_complete_day)
        & (frame["_game_id"] != target_id)
    ].sort_values(["_date", "_game_id"])
    meetings: list[dict[str, Any]] = []
    team_rows: dict[int, list[dict[str, Any]]] = {home_id: [], away_id: []}
    for _, row in pair.iterrows():
        row_home = int(row["home_team_id"])
        row_away = int(row["away_team_id"])
        home_score = int(_row_value(row, "homeScore", "home_score") or 0)
        away_score = int(_row_value(row, "awayScore", "away_score") or 0)
        winner = row_home if home_score > away_score else row_away
        game_id = str(row["_game_id"])
        series_key = _series_key(row)
        meetings.append(
            {
                "game_id": game_id,
                "played_at": row["_date"].isoformat(),
                "season_start": int(_row_value(row, "season_start") or row["_date"].year),
                "is_playoffs": bool(_row_value(row, "is_playoffs") or False),
                "series_key": series_key,
                "home_team_id": row_home,
                "away_team_id": row_away,
                "home_score": home_score,
                "away_score": away_score,
                "home_margin": home_score - away_score,
                "winner_team_id": winner,
            }
        )
        for team_id in (row_home, row_away):
            current = _team_stats(stats, game_id, team_id)
            if current:
                current["game_id"] = game_id
                current["win"] = int(winner == team_id)
                current["margin"] = (home_score - away_score) * (1 if team_id == row_home else -1)
                team_rows[team_id].append(current)

    relevance, relevance_reasons = _relevance(meetings, target_season)
    playoff_groups: dict[str, list[dict[str, Any]]] = {}
    for meeting in meetings:
        if meeting.get("series_key") and meeting["season_start"] >= target_season - 1:
            playoff_groups.setdefault(str(meeting["series_key"]), []).append(meeting)
    series = max(playoff_groups.values(), key=lambda rows: rows[-1]["played_at"], default=[])
    series_summary = None
    if len(series) >= 2:
        wins = {
            str(team_id): sum(1 for row in series if row["winner_team_id"] == team_id)
            for team_id in (home_id, away_id)
        }
        series_team_rows = {
            team_id: [row for row in team_rows[team_id] if row["game_id"] in {item["game_id"] for item in series}]
            for team_id in (home_id, away_id)
        }
        full_stat_coverage = all(len(rows) == len(series) for rows in series_team_rows.values())
        summaries = (
            {str(team_id): _pool(rows) for team_id, rows in series_team_rows.items()}
            if full_stat_coverage else {}
        )
        for team_id, rows in series_team_rows.items():
            if full_stat_coverage and rows:
                summaries[str(team_id)]["average_margin"] = float(np.mean([row["margin"] for row in rows]))
                summaries[str(team_id)]["wins"] = int(sum(row["win"] for row in rows))
        factors: list[dict[str, Any]] = []
        left, right = summaries.get(str(home_id), {}), summaries.get(str(away_id), {})
        for key, label, threshold in (
            ("three_pct", "三分命中率", 0.02),
            ("rebounds_per_game", "场均篮板", 3.0),
            ("turnovers_per_game", "场均失误", 2.0),
            ("off_rating", "估算进攻效率", 4.0),
        ):
            a, b = left.get(key), right.get(key)
            if a is not None and b is not None and abs(float(a) - float(b)) >= threshold:
                leader = home_id if (a > b) != (key == "turnovers_per_game") else away_id
                factors.append({"metric": key, "label": label, "home_value": a, "away_value": b, "leader_team_id": leader})
        series_summary = {
            "series_key": series[-1]["series_key"],
            "season_start": series[-1]["season_start"],
            "games": len(series),
            "wins": wins,
            "winner_team_id": max((home_id, away_id), key=lambda team_id: wins[str(team_id)]),
            "team_summaries": summaries,
            "deterministic_factors": factors[:3],
            "team_stats_status": "complete" if full_stat_coverage else "unavailable",
            "team_stat_rows": {str(team_id): len(rows) for team_id, rows in series_team_rows.items()},
            "efficiency_semantics": "100 * points / (FGA - OREB + TOV + 0.44 * FTA), estimated possessions",
            "game_ids": [row["game_id"] for row in series],
            "game_totals": [row["home_score"] + row["away_score"] for row in series],
            "average_total": float(np.mean([row["home_score"] + row["away_score"] for row in series])),
            "game_margins": [abs(row["home_score"] - row["away_score"]) for row in series],
        }

    payload = {
        "status": "available" if meetings else "unavailable",
        "game_id": target_id,
        "cutoff": cutoff,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "meeting_count": len(meetings),
        "meetings": meetings[-max_meetings:],
        "most_recent_meeting": meetings[-1] if meetings else None,
        "series_summary": series_summary,
        "matchup_relevance": relevance,
        "relevance_reasons": relevance_reasons,
        "numeric_model_usage": False,
        "source_semantics": "local historical games strictly before cutoff; team statistics when available",
    }
    payload["context_hash"] = content_hash(payload)
    return clean(payload)
