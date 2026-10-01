from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .config import MODELING_START_SEASON, TEST_SEASON
from .market import transform_market_fields


def _record(
    checks: list[dict[str, object]],
    name: str,
    passed: bool,
    detail: str,
    *,
    severity: str = "error",
) -> None:
    checks.append(
        {
            "name": name,
            "status": "passed" if passed else "failed",
            "severity": severity,
            "detail": detail,
        }
    )


def run_data_quality_checks(
    games: pd.DataFrame,
    team_stats: pd.DataFrame,
    dataset: pd.DataFrame,
    feature_columns: list[str],
    odds: pd.DataFrame,
    *,
    excluded_invalid_results: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Run checks that protect the credibility of the baseline experiment."""
    checks: list[dict[str, object]] = []
    excluded_invalid_results = excluded_invalid_results or []

    _record(
        checks,
        "unique_game_id",
        not games["game_id"].duplicated().any(),
        f"duplicate rows={int(games['game_id'].duplicated().sum())}",
    )
    valid_game_ids = games["game_id"].str.fullmatch(r"\d{10}", na=False)
    _record(
        checks,
        "stable_game_id_format",
        bool(valid_game_ids.all()),
        f"non-10-digit IDs={int((~valid_game_ids).sum())}",
    )
    _record(
        checks,
        "valid_final_scores",
        bool((games["homeScore"] > 0).all() and (games["awayScore"] > 0).all()),
        f"excluded invalid source rows={len(excluded_invalid_results)}",
    )
    _record(
        checks,
        "no_tied_final_scores",
        not (games["homeScore"] == games["awayScore"]).any(),
        "NBA final scores cannot be tied",
    )
    extreme_scores = (
        (games["homeScore"] > 200)
        | (games["awayScore"] > 200)
        | (games["point_margin"].abs() > 80)
        | (games["total_points"] > 400)
    )
    _record(
        checks,
        "plausible_score_ranges",
        not extreme_scores.any(),
        f"extreme rows={int(extreme_scores.sum())}; "
        f"score range={min(games['homeScore'].min(), games['awayScore'].min())}.."
        f"{max(games['homeScore'].max(), games['awayScore'].max())}; "
        f"max absolute margin={games['point_margin'].abs().max()}; "
        f"max total={games['total_points'].max()}",
    )
    _record(
        checks,
        "home_away_are_distinct",
        not (games["home_team_id"] == games["away_team_id"]).any(),
        "home and away franchise IDs differ for every game",
    )
    _record(
        checks,
        "stable_team_mapping",
        not games[["home_abbr", "away_abbr"]].isna().any().any(),
        "all franchise IDs map to market abbreviations",
    )

    date_floor = pd.to_datetime(games["season_start"].astype(str) + "-08-01")
    date_ceiling = pd.to_datetime((games["season_start"] + 1).astype(str) + "-11-01")
    bad_dates = (games["game_date"] < date_floor) | (games["game_date"] >= date_ceiling)
    _record(
        checks,
        "season_date_consistency",
        not bad_dates.any(),
        f"rows outside broad NBA season bounds={int(bad_dates.sum())}",
    )

    counts = team_stats.groupby("game_id").size()
    _record(
        checks,
        "two_team_rows_per_game",
        bool((counts == 2).all() and len(counts) == len(games)),
        f"games={len(games)}, paired team-stat games={len(counts)}, invalid pairs={int((counts != 2).sum())}",
    )
    _record(
        checks,
        "one_home_row_per_game",
        bool((team_stats.groupby("game_id")["home"].sum() == 1).all()),
        "each team-stat pair contains one home row",
    )
    joined = team_stats.merge(
        games[
            [
                "game_id",
                "game_date",
                "home_team_id",
                "away_team_id",
                "homeScore",
                "awayScore",
            ]
        ],
        on="game_id",
        how="left",
        validate="many_to_one",
        suffixes=("", "_game"),
    )
    id_mismatch = (
        ((joined["home"] == 1) & (joined["team_id"] != joined["home_team_id"]))
        | ((joined["home"] == 0) & (joined["team_id"] != joined["away_team_id"]))
        | (
            (joined["home"] == 1)
            & (joined["opponent_team_id"] != joined["away_team_id"])
        )
        | (
            (joined["home"] == 0)
            & (joined["opponent_team_id"] != joined["home_team_id"])
        )
    )
    _record(
        checks,
        "team_and_opponent_ids_match_games",
        not id_mismatch.any(),
        f"mismatched team rows={int(id_mismatch.sum())}",
    )
    score_mismatch = (
        (
            (joined["home"] == 1)
            & (
                (joined["teamScore"] != joined["homeScore"])
                | (joined["opponentScore"] != joined["awayScore"])
            )
        )
        | (
            (joined["home"] == 0)
            & (
                (joined["teamScore"] != joined["awayScore"])
                | (joined["opponentScore"] != joined["homeScore"])
            )
        )
    )
    _record(
        checks,
        "team_scores_match_game_scores",
        not score_mismatch.any(),
        f"mismatched team rows={int(score_mismatch.sum())}",
    )
    win_mismatch = joined["win"] != (joined["teamScore"] > joined["opponentScore"]).astype(float)
    _record(
        checks,
        "team_win_label_matches_score",
        not win_mismatch.any(),
        f"mismatched team rows={int(win_mismatch.sum())}",
    )
    date_mismatch = (
        joined["game_date"].dt.normalize() != joined["game_date_game"].dt.normalize()
    )
    _record(
        checks,
        "team_dates_match_game_dates",
        not date_mismatch.any(),
        f"mismatched team rows={int(date_mismatch.sum())}",
    )

    season_counts = dataset.groupby("season_start").size().astype(int).to_dict()
    relevant_counts = {
        int(season): int(count)
        for season, count in season_counts.items()
        if int(season) >= MODELING_START_SEASON
    }
    sparse = {season: count for season, count in relevant_counts.items() if count < 1000}
    _record(
        checks,
        "modeling_seasons_are_complete",
        not sparse,
        f"season counts={relevant_counts}; suspicious seasons={sparse}",
    )
    _record(
        checks,
        "unique_processed_game_id",
        not dataset["game_id"].duplicated().any(),
        f"duplicate rows={int(dataset['game_id'].duplicated().sum())}",
    )
    implied_home_score = (dataset["total_points"] + dataset["point_margin"]) / 2
    implied_away_score = (dataset["total_points"] - dataset["point_margin"]) / 2
    target_mismatch = (
        (dataset["home_win"] != (dataset["point_margin"] > 0).astype(int))
        | (implied_home_score <= 0)
        | (implied_away_score <= 0)
        | (implied_home_score % 1 != 0)
        | (implied_away_score % 1 != 0)
    )
    _record(
        checks,
        "target_labels_are_consistent",
        not target_mismatch.any(),
        f"inconsistent rows={int(target_mismatch.sum())}",
    )
    features = dataset[feature_columns].replace([np.inf, -np.inf], np.nan)
    _record(
        checks,
        "finite_model_features",
        not features.isna().any().any(),
        f"missing or infinite feature cells={int(features.isna().sum().sum())}",
    )
    percentage_features = [
        column
        for column in feature_columns
        if column.startswith(("home_", "away_"))
        and any(
            token in column
            for token in ("fg_pct", "three_pct", "ft_pct", "rebound_share", "turnover_rate")
        )
    ]
    out_of_range = sum(
        int(((dataset[column] < 0) | (dataset[column] > 1)).sum())
        for column in percentage_features
    )
    _record(
        checks,
        "bounded_rate_features",
        out_of_range == 0,
        f"rate feature cells outside 0..1={out_of_range}",
    )
    banned_tokens = ("score", "margin", "total", "market", "moneyline", "spread")
    suspicious_features = [
        column
        for column in feature_columns
        if any(token in column.lower() for token in banned_tokens)
    ]
    _record(
        checks,
        "no_target_or_market_columns_in_features",
        not suspicious_features,
        f"suspicious features={suspicious_features}",
    )
    relation_errors = 0
    checked_relations = 0
    for feature in feature_columns:
        if not feature.startswith("diff_"):
            continue
        suffix = feature.removeprefix("diff_")
        home_feature = f"home_{suffix}"
        away_feature = f"away_{suffix}"
        if home_feature not in feature_columns or away_feature not in feature_columns:
            continue
        checked_relations += 1
        relation_errors += int(
            (~np.isclose(
                dataset[feature],
                dataset[home_feature] - dataset[away_feature],
                equal_nan=True,
            )).sum()
        )
    if {"elo_home", "elo_away", "elo_diff"}.issubset(feature_columns):
        checked_relations += 1
        relation_errors += int(
            (~np.isclose(
                dataset["elo_diff"],
                dataset["elo_home"] - dataset["elo_away"],
                equal_nan=True,
            )).sum()
        )
    _record(
        checks,
        "derived_feature_relations_are_consistent",
        relation_errors == 0,
        f"relations checked={checked_relations}; inconsistent cells={relation_errors}",
    )
    chronological = dataset.sort_values(["game_date", "game_id"]).index.equals(dataset.index)
    _record(
        checks,
        "processed_rows_are_chronological",
        chronological,
        "dataset order is game_date then game_id",
    )

    odds_duplicates = odds.duplicated(["date", "home", "away"])
    _record(
        checks,
        "unique_market_game_key",
        not odds_duplicates.any(),
        f"duplicate date/home/away rows={int(odds_duplicates.sum())}",
    )
    valid_favorite = odds["whos_favored"].isin(["home", "away"])
    plausible_spread = odds["spread"].isna() | odds["spread"].between(
        0, 50, inclusive="both"
    )
    plausible_total = odds["total"].isna() | odds["total"].between(
        100, 350, inclusive="both"
    )
    plausible_market = plausible_spread & plausible_total
    _record(
        checks,
        "plausible_market_fields",
        bool(valid_favorite.all() and plausible_market.all()),
        f"invalid favorite sides={int((~valid_favorite).sum())}; "
        f"implausible spread/total rows={int((~plausible_market).sum())}",
    )
    market_game_keys = games.copy()
    market_game_keys["date"] = market_game_keys["game_date"].dt.normalize()
    market_keys = odds.rename(columns={"home": "home_abbr", "away": "away_abbr"})
    market_join = market_game_keys.merge(
        market_keys,
        on=["date", "home_abbr", "away_abbr"],
        how="inner",
        validate="one_to_one",
        suffixes=("", "_market"),
    )
    market_score_mismatch = (
        (market_join["homeScore"] != market_join["score_home"])
        | (market_join["awayScore"] != market_join["score_away"])
    )
    _record(
        checks,
        "market_join_scores_match_games",
        not market_score_mismatch.any(),
        f"joined games={len(market_join)}, score mismatches={int(market_score_mismatch.sum())}; "
        "market-source scores are never used as model labels",
        severity="warning",
    )
    market = transform_market_fields(odds)
    valid_spread = market["id_spread"].notna() & market["spread"].notna()
    spread_errors = (
        market.loc[valid_spread, "inferred_id_spread"].astype(int)
        != market.loc[valid_spread, "id_spread"].astype(int)
    )
    valid_total = market["id_total"].notna() & market["total"].notna()
    total_errors = (
        market.loc[valid_total, "inferred_id_total"].astype(int)
        != market.loc[valid_total, "id_total"].astype(int)
    )
    _record(
        checks,
        "spread_direction",
        not spread_errors.any(),
        f"checked={int(valid_spread.sum())}, mismatches={int(spread_errors.sum())}",
    )
    _record(
        checks,
        "total_direction",
        not total_errors.any(),
        f"checked={int(valid_total.sum())}, mismatches={int(total_errors.sum())}",
    )
    _record(
        checks,
        "market_source_has_capture_timestamp",
        False,
        "source has game date but no line capture timestamp or opening/closing label",
        severity="warning",
    )
    test_market = odds[
        (odds["date"] >= pd.Timestamp(f"{TEST_SEASON}-08-01"))
        & (odds["date"] < pd.Timestamp(f"{TEST_SEASON + 1}-08-01"))
    ]
    missing_moneylines = int(
        test_market[["moneyline_home", "moneyline_away"]].isna().any(axis=1).sum()
    )
    _record(
        checks,
        "test_market_moneylines_available",
        missing_moneylines == 0,
        f"test rows={len(test_market)}, rows missing either moneyline={missing_moneylines}",
        severity="warning",
    )

    failed_errors = [
        check
        for check in checks
        if check["status"] == "failed" and check["severity"] == "error"
    ]
    failed_warnings = [
        check
        for check in checks
        if check["status"] == "failed" and check["severity"] == "warning"
    ]
    return {
        "status": "passed" if not failed_errors else "failed",
        "summary": {
            "checks": len(checks),
            "passed": sum(check["status"] == "passed" for check in checks),
            "failed_errors": len(failed_errors),
            "failed_warnings": len(failed_warnings),
        },
        "excluded_invalid_results": excluded_invalid_results,
        "season_counts": relevant_counts,
        "checks": checks,
    }


def write_data_quality_report(
    result: dict[str, object], artifact_path: Path, report_path: Path
) -> None:
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    lines = [
        "# CourtVision Data Quality Report",
        "",
        f"Overall status: **{result['status']}**",
        "",
        "## Automated checks",
        "",
    ]
    for check in result["checks"]:
        marker = "PASS" if check["status"] == "passed" else check["severity"].upper()
        lines.append(f"1. **{marker}** `{check['name']}`: {check['detail']}")
    lines.extend(["", "## Excluded source rows", ""])
    excluded = result["excluded_invalid_results"]
    if excluded:
        for row in excluded:
            lines.append(
                f"1. `{row['game_id']}` on {row['game_date']}: "
                f"{row['homeScore']} to {row['awayScore']}. Excluded because the source result is impossible."
            )
    else:
        lines.append("No source rows were excluded.")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
