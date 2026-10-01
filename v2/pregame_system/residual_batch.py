"""Stable row-level historical residual prediction export."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .residual_model import RESIDUAL_FEATURE_COLUMNS, ResidualPredictor
from .train_residual_model import build_residual_training_frame


REQUIRED_COLUMNS = {
    "game_id",
    "game_date",
    "pure_win_probability",
    "pure_expected_margin",
    "pure_expected_total",
    "market_implied_win_probability",
    "market_expected_margin",
    "market_total",
    "pure_market_win_probability_gap",
    "pure_market_margin_gap",
    "pure_market_total_gap",
    "predicted_win_probability_residual",
    "predicted_margin_residual",
    "predicted_total_residual",
    "official_win_probability",
    "official_expected_margin",
    "official_expected_total",
    "actual_home_win",
    "actual_home_margin",
    "actual_total",
    "margin_residual_target",
    "total_residual_target",
    "historical_market_time_verified",
    "market_provider_eligible_for_official",
    "training_overlap",
    "evaluation_role",
    "complete_feature_contract",
    "residual_model_version",
}


def build_batch_rows(
    frame: pd.DataFrame,
    predictor: ResidualPredictor,
) -> pd.DataFrame:
    features = frame[RESIDUAL_FEATURE_COLUMNS]
    result = pd.DataFrame(
        {
            "game_id": frame["game_id"],
            "game_date": frame["game_date"],
            "pure_win_probability": frame["pure_win_probability"],
            "pure_expected_margin": frame["pure_expected_margin"],
            "pure_expected_total": frame["pure_expected_total"],
            "market_implied_win_probability": frame["market_implied_win_probability"],
            "market_expected_margin": frame["market_expected_margin"],
            "market_total": frame["market_total"],
            "pure_market_win_probability_gap": frame["pure_market_win_probability_gap"],
            "pure_market_margin_gap": frame["pure_market_margin_gap"],
            "pure_market_total_gap": frame["pure_market_total_gap"],
            "actual_home_win": frame["home_win"],
            "actual_home_margin": frame["point_margin"],
            "actual_total": frame["total_points"],
            "margin_residual_target": frame["margin_residual_target"],
            "total_residual_target": frame["total_residual_target"],
        }
    )
    result["official_win_probability"] = predictor.models["win_probability"].predict_proba(features)[:, 1]
    result["predicted_win_probability_residual"] = (
        result["official_win_probability"] - result["market_implied_win_probability"]
    )
    result["predicted_margin_residual"] = predictor.models["margin_residual"].predict(features)
    result["predicted_total_residual"] = predictor.models["total_residual"].predict(features)
    result["official_expected_margin"] = (
        result["market_expected_margin"] + result["predicted_margin_residual"]
    )
    result["official_expected_total"] = result["market_total"] + result["predicted_total_residual"]
    training_max = pd.Timestamp(predictor.metadata["training_max_game_date"])
    result["historical_market_time_verified"] = False
    result["market_provider_eligible_for_official"] = False
    result["training_overlap"] = pd.to_datetime(result["game_date"]) <= training_max
    result["evaluation_role"] = np.where(
        result["training_overlap"],
        "artifact_training_overlap_not_independent",
        "post_training_candidate_evaluation",
    )
    result["complete_feature_contract"] = ~features.isna().any(axis=1)
    result["residual_model_version"] = predictor.metadata["version"]
    if result["game_id"].duplicated().any():
        raise ValueError("residual_batch_game_id_not_unique")
    if not REQUIRED_COLUMNS <= set(result):
        raise ValueError("residual_batch_contract_missing")
    return result.sort_values(["game_date", "game_id"]).reset_index(drop=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--limit", type=int)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    frame, _ = build_residual_training_frame()
    if args.start_date:
        frame = frame[frame["game_date"] >= pd.Timestamp(args.start_date)]
    if args.end_date:
        frame = frame[frame["game_date"] < pd.Timestamp(args.end_date) + pd.Timedelta(days=1)]
    if args.limit is not None:
        if args.limit <= 0:
            raise ValueError("limit_must_be_positive")
        frame = frame.head(args.limit)
    rows = build_batch_rows(frame, ResidualPredictor())
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.casefold() == ".jsonl":
        with output.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows.to_dict("records"):
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    else:
        rows.to_csv(output, index=False)
    print(json.dumps({"rows": len(rows), "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
