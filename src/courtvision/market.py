from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .metrics import classification_metrics, regression_metrics


def american_implied_probability(odds: pd.Series) -> pd.Series:
    values = pd.to_numeric(odds, errors="coerce")
    return pd.Series(
        np.where(
            values < 0,
            -values / (-values + 100.0),
            100.0 / (values + 100.0),
        ),
        index=odds.index,
        dtype=float,
    )


def transform_market_fields(odds: pd.DataFrame) -> pd.DataFrame:
    frame = odds.copy()
    frame["actual_home_margin"] = frame["score_home"] - frame["score_away"]
    frame["actual_total"] = frame["score_home"] + frame["score_away"]
    frame["market_expected_home_margin"] = np.where(
        frame["whos_favored"].eq("home"), frame["spread"], -frame["spread"]
    )
    frame["market_total"] = frame["total"]

    home_raw = american_implied_probability(frame["moneyline_home"])
    away_raw = american_implied_probability(frame["moneyline_away"])
    vig_sum = home_raw + away_raw
    frame["market_home_win_probability"] = home_raw / vig_sum

    favorite_margin = np.where(
        frame["whos_favored"].eq("home"),
        frame["actual_home_margin"],
        -frame["actual_home_margin"],
    )
    frame["inferred_id_spread"] = np.where(
        np.isclose(favorite_margin, frame["spread"], equal_nan=False),
        2,
        np.where(favorite_margin > frame["spread"], 1, 0),
    )
    frame["inferred_id_total"] = np.where(
        np.isclose(frame["actual_total"], frame["total"], equal_nan=False),
        2,
        np.where(frame["actual_total"] > frame["total"], 1, 0),
    )
    return frame


def _select_audit_samples(frame: pd.DataFrame) -> pd.DataFrame:
    recent = frame[frame["season"] >= 2020].sort_values("date", ascending=False)
    conditions = [
        ("home_favorite_covered", recent["whos_favored"].eq("home") & recent["id_spread"].eq(1)),
        ("home_favorite_failed", recent["whos_favored"].eq("home") & recent["id_spread"].eq(0)),
        ("away_favorite_covered", recent["whos_favored"].eq("away") & recent["id_spread"].eq(1)),
        ("away_favorite_failed", recent["whos_favored"].eq("away") & recent["id_spread"].eq(0)),
        ("spread_push", recent["id_spread"].eq(2)),
        ("total_push", recent["id_total"].eq(2)),
    ]
    samples = []
    for case, condition in conditions:
        matches = recent.loc[condition]
        if matches.empty:
            continue
        row = matches.iloc[0].copy()
        row["audit_case"] = case
        samples.append(row)
    result = pd.DataFrame(samples)
    result["spread_interpretation"] = (
        result["whos_favored"].astype(str)
        + " favored by "
        + result["spread"].astype(str)
    )
    result["total_interpretation"] = "game total line " + result["total"].astype(str)
    result["moneyline_interpretation"] = np.where(
        result["market_home_win_probability"].notna(),
        "two-way no-vig home probability calculated",
        "moneyline missing; probability unavailable",
    )
    columns = [
        "audit_case",
        "date",
        "away",
        "home",
        "score_away",
        "score_home",
        "whos_favored",
        "spread",
        "total",
        "moneyline_away",
        "moneyline_home",
        "id_spread",
        "id_total",
        "spread_interpretation",
        "total_interpretation",
        "moneyline_interpretation",
        "market_expected_home_margin",
        "market_total",
        "market_home_win_probability",
        "actual_home_margin",
        "actual_total",
        "inferred_id_spread",
        "inferred_id_total",
    ]
    return result[columns]


def audit_and_benchmark_market(
    odds: pd.DataFrame,
    test_predictions: pd.DataFrame,
    artifact_dir: Path,
) -> dict[str, object]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    market = transform_market_fields(odds)
    valid_spread = market["id_spread"].notna() & market["spread"].notna()
    valid_total = market["id_total"].notna() & market["total"].notna()
    spread_mismatches = int(
        (
            market.loc[valid_spread, "inferred_id_spread"].astype(int)
            != market.loc[valid_spread, "id_spread"].astype(int)
        ).sum()
    )
    total_mismatches = int(
        (
            market.loc[valid_total, "inferred_id_total"].astype(int)
            != market.loc[valid_total, "id_total"].astype(int)
        ).sum()
    )
    samples = _select_audit_samples(market)
    samples.to_csv(artifact_dir / "market_field_audit_samples.csv", index=False)

    audit = {
        "rows": len(market),
        "spread_rows_checked": int(valid_spread.sum()),
        "spread_direction_mismatches": spread_mismatches,
        "total_rows_checked": int(valid_total.sum()),
        "total_direction_mismatches": total_mismatches,
        "spread_semantics": (
            "spread is a positive favorite handicap; whos_favored supplies side; "
            "internal expected home margin is +spread for a home favorite and -spread for an away favorite"
        ),
        "id_spread_semantics": "1 favorite covered, 0 favorite did not cover, 2 push",
        "id_total_semantics": "1 over, 0 under, 2 push",
        "total_semantics": "full-game combined score line including overtime",
        "moneyline_semantics": "American odds converted to two-way no-vig probability where both sides exist",
        "closing_line_status": "unverified; source has no per-row capture timestamp or opening/closing label",
    }
    (artifact_dir / "market_field_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    prediction_join = test_predictions.copy()
    prediction_join["game_day"] = pd.to_datetime(
        prediction_join["game_date"]
    ).dt.normalize()
    market_join = market.rename(
        columns={"home": "home_abbr", "away": "away_abbr"}
    ).copy()
    market_join["game_day"] = pd.to_datetime(market_join["date"]).dt.normalize()
    joined = prediction_join.merge(
        market_join,
        on=["game_day", "home_abbr", "away_abbr"],
        how="left",
        suffixes=("", "_market"),
        validate="one_to_one",
    )
    joined["win_individual_log_loss"] = -(
        joined["home_win"] * np.log(joined["win_probability"].clip(1e-6, 1 - 1e-6))
        + (1 - joined["home_win"])
        * np.log((1 - joined["win_probability"]).clip(1e-6, 1 - 1e-6))
    )
    joined["margin_absolute_error"] = (
        joined["predicted_margin"] - joined["point_margin"]
    ).abs()
    joined["total_absolute_error"] = (
        joined["predicted_total"] - joined["total_points"]
    ).abs()
    joined["margin_market_divergence"] = (
        joined["predicted_margin"] - joined["market_expected_home_margin"]
    ).abs()
    joined["total_market_divergence"] = (
        joined["predicted_total"] - joined["market_total"]
    ).abs()
    joined["model_market_divergence"] = joined[
        ["margin_market_divergence", "total_market_divergence"]
    ].max(axis=1)
    joined.to_csv(artifact_dir / "test_predictions_with_market.csv", index=False)

    available = joined["market_total"].notna() & joined["market_expected_home_margin"].notna()
    same_sample = joined.loc[available]
    summary: dict[str, object] = {
        "test_games": len(joined),
        "market_joined_games": len(same_sample),
        "join_rate": float(len(same_sample) / len(joined)),
        "margin": {
            "basketball_model": regression_metrics(
                same_sample["point_margin"], same_sample["predicted_margin"].to_numpy()
            ),
            "market": regression_metrics(
                same_sample["point_margin"],
                same_sample["market_expected_home_margin"].to_numpy(),
            ),
        },
        "total": {
            "basketball_model": regression_metrics(
                same_sample["total_points"], same_sample["predicted_total"].to_numpy()
            ),
            "market": regression_metrics(
                same_sample["total_points"], same_sample["market_total"].to_numpy()
            ),
        },
    }
    win_available = joined["market_home_win_probability"].notna()
    if win_available.any():
        win_sample = joined.loc[win_available]
        summary["win_probability"] = {
            "games": len(win_sample),
            "basketball_model": classification_metrics(
                win_sample["home_win"], win_sample["win_probability"].to_numpy()
            ),
            "market": classification_metrics(
                win_sample["home_win"],
                win_sample["market_home_win_probability"].to_numpy(),
            ),
        }
    else:
        summary["win_probability"] = {
            "games": 0,
            "status": "unavailable on the 2025-26 holdout because the source has no moneylines",
        }
    (artifact_dir / "market_benchmark_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"audit": audit, "summary": summary, "joined": joined}
