from __future__ import annotations

from pathlib import Path

import pandas as pd


def build_bad_cases(joined: pd.DataFrame, artifact_dir: Path) -> pd.DataFrame:
    selectors = {
        "worst_win_probability": joined["win_individual_log_loss"].idxmax(),
        "largest_margin_error": joined["margin_absolute_error"].idxmax(),
        "largest_total_error": joined["total_absolute_error"].idxmax(),
    }
    market_rows = joined[joined["model_market_divergence"].notna()]
    if not market_rows.empty:
        selectors["largest_model_market_divergence"] = market_rows[
            "model_market_divergence"
        ].idxmax()

    labels_by_index: dict[int, list[str]] = {}
    for label, index in selectors.items():
        labels_by_index.setdefault(int(index), []).append(label)

    cases = joined.loc[list(labels_by_index)].copy()
    cases["bad_case_types"] = [
        ";".join(labels_by_index[int(index)]) for index in cases.index
    ]
    cases["actual_result"] = cases["home_win"].map(
        {1: "HOME_WIN", 0: "AWAY_WIN"}
    )
    cases["prediction_error"] = cases[
        ["win_individual_log_loss", "margin_absolute_error", "total_absolute_error"]
    ].max(axis=1)
    columns = [
        "bad_case_types",
        "game_id",
        "game_date",
        "home_team",
        "away_team",
        "win_probability",
        "predicted_margin",
        "predicted_total",
        "actual_result",
        "point_margin",
        "total_points",
        "market_expected_home_margin",
        "market_total",
        "market_home_win_probability",
        "win_individual_log_loss",
        "margin_absolute_error",
        "total_absolute_error",
        "prediction_error",
        "margin_market_divergence",
        "total_market_divergence",
        "model_market_divergence",
        "elo_home",
        "elo_away",
        "elo_diff",
        "home_rest_days",
        "away_rest_days",
        "home_back_to_back",
        "away_back_to_back",
        "diff_win_rate_roll5",
        "diff_net_rating_roll5",
        "diff_net_rating_roll15",
        "diff_pace_roll15",
    ]
    available_columns = [column for column in columns if column in cases]
    cases = cases[available_columns].sort_values("game_date")
    cases.to_csv(artifact_dir / "bad_cases.csv", index=False)
    return cases


def build_error_slices(joined: pd.DataFrame, artifact_dir: Path) -> pd.DataFrame:
    frame = joined.copy()
    frame["win_confidence_band"] = pd.cut(
        (frame["win_probability"] - 0.5).abs(),
        bins=[-0.001, 0.10, 0.20, 0.50],
        labels=["low_confidence", "medium_confidence", "high_confidence"],
    )
    frame["predicted_margin_band"] = pd.cut(
        frame["predicted_margin"].abs(),
        bins=[-0.001, 3.0, 8.0, float("inf")],
        labels=["projected_close", "projected_medium", "projected_lopsided"],
    )
    frame["schedule_band"] = "normal_rest"
    frame.loc[
        (frame["home_back_to_back"] == 1) | (frame["away_back_to_back"] == 1),
        "schedule_band",
    ] = "at_least_one_back_to_back"
    frame["competition_band"] = frame["is_playoffs"].map(
        {1: "playoffs_or_play_in", 0: "regular_or_cup"}
    )
    divergence = frame["model_market_divergence"]
    if divergence.notna().sum() >= 4:
        frame["market_divergence_band"] = pd.qcut(
            divergence,
            q=4,
            labels=["q1_low", "q2", "q3", "q4_high"],
            duplicates="drop",
        )

    rows = []
    dimensions = [
        "win_confidence_band",
        "predicted_margin_band",
        "schedule_band",
        "competition_band",
        "market_divergence_band",
    ]
    for dimension in dimensions:
        if dimension not in frame:
            continue
        for category, group in frame.groupby(dimension, observed=True):
            rows.append(
                {
                    "dimension": dimension,
                    "category": str(category),
                    "games": len(group),
                    "mean_win_log_loss": group["win_individual_log_loss"].mean(),
                    "margin_mae": group["margin_absolute_error"].mean(),
                    "total_mae": group["total_absolute_error"].mean(),
                    "mean_model_market_divergence": group[
                        "model_market_divergence"
                    ].mean(),
                }
            )
    slices = pd.DataFrame(rows)
    slices.to_csv(artifact_dir / "error_slices.csv", index=False)
    return slices
