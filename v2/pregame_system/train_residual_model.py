"""Train the first reproducible CourtVision market residual artifact."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from courtvision.data import load_odds
from courtvision.market import transform_market_fields
from courtvision.metrics import classification_metrics, regression_metrics

from . import ROOT
from .features import dataset, history
from .prediction import latest
from .residual_model import BASKETBALL_CONTEXT_FEATURES, RESIDUAL_FEATURE_COLUMNS
from .storage import BASE, clean, content_hash, digest, now, save


ANALYSIS_DB = ROOT / "analysis_v1/data/courtvision_analysis.sqlite"
ODDS_PATH = ROOT / "data/raw/nba_2008-2026.csv"
MODEL_ROOT = BASE / "models/market_residual_v1"


def _load_feature_frame() -> tuple[pd.DataFrame, dict]:
    try:
        path = latest("experiments", "dataset.pkl")
        frame = pd.read_pickle(path)
        source = {"path": str(path), "sha256": digest(path), "mode": "frozen_experiment_dataset"}
    except ValueError:
        games, stats = history()
        frame, _, _ = dataset(games, stats)
        source = {
            "path": None,
            "sha256": content_hash(frame[["game_id", *BASKETBALL_CONTEXT_FEATURES]].to_dict("records")),
            "mode": "deterministic_in_memory_rebuild",
        }
    missing = set(BASKETBALL_CONTEXT_FEATURES) - set(frame)
    if missing:
        raise ValueError("residual_basketball_features_missing:" + ",".join(sorted(missing)))
    return frame, source


def _market_probability_mapping(before_date: pd.Timestamp) -> tuple[dict, Pipeline]:
    market = transform_market_fields(load_odds(ODDS_PATH))
    sample = market[
        (market["date"] < before_date)
        & market["market_expected_home_margin"].notna()
        & market["actual_home_margin"].notna()
    ].copy()
    if len(sample) < 1000:
        raise ValueError("insufficient_prior_market_probability_rows")
    target = (sample["actual_home_margin"] > 0).astype(int)
    model = LogisticRegression(max_iter=1500, random_state=42)
    model.fit(sample[["market_expected_home_margin"]].to_numpy(), target)
    mapping = {
        "kind": "prior_history_logistic_spread_to_home_win_probability",
        "coef": float(model.coef_[0][0]),
        "intercept": float(model.intercept_[0]),
        "training_rows": len(sample),
        "training_max_date_exclusive": before_date.isoformat(),
        "source_path": str(ODDS_PATH),
        "source_sha256": digest(ODDS_PATH),
    }
    return mapping, model


def build_residual_training_frame() -> tuple[pd.DataFrame, dict]:
    uri = ANALYSIS_DB.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        audit = pd.read_sql_query(
            """
            SELECT
                g.game_id,
                g.game_date,
                g.home_win,
                g.point_margin,
                g.total_points,
                p.win_probability AS pure_win_probability,
                p.predicted_margin AS pure_expected_margin,
                p.predicted_total AS pure_expected_total,
                p.model_scope AS pure_model_scope,
                p.prediction_time_status,
                m.market_expected_home_margin AS market_expected_margin,
                m.market_total,
                m.market_home_win_probability,
                m.line_capture_time_status,
                m.line_identity_status
            FROM dim_game AS g
            JOIN fact_prediction AS p USING (game_id)
            JOIN fact_market_benchmark AS m USING (game_id)
            ORDER BY g.game_date, g.game_id
            """,
            connection,
        )
    finally:
        connection.close()
    audit["game_date"] = pd.to_datetime(audit["game_date"], errors="raise")
    if audit["game_id"].duplicated().any():
        raise ValueError("residual_training_game_id_not_unique")
    if not audit["prediction_time_status"].str.contains("historical_holdout", na=False).all():
        raise ValueError("residual_training_requires_holdout_pure_predictions")
    first_date = audit["game_date"].min().normalize()
    mapping, mapping_model = _market_probability_mapping(first_date)
    missing_probability = audit["market_home_win_probability"].isna()
    direct_probability_rows=int((~missing_probability).sum())
    proxy_probability_rows=int(missing_probability.sum())
    audit.loc[missing_probability, "market_home_win_probability"] = mapping_model.predict_proba(
        audit.loc[missing_probability, ["market_expected_margin"]].to_numpy()
    )[:, 1]

    feature_frame, feature_source = _load_feature_frame()
    joined = audit.merge(
        feature_frame[["game_id", *BASKETBALL_CONTEXT_FEATURES]],
        on="game_id",
        how="left",
        validate="one_to_one",
    )
    if joined[BASKETBALL_CONTEXT_FEATURES].isna().all(axis=1).any():
        raise ValueError("residual_training_feature_join_missing")
    joined["market_implied_win_probability"] = joined["market_home_win_probability"]
    joined["pure_market_win_probability_gap"] = (
        joined["pure_win_probability"] - joined["market_implied_win_probability"]
    )
    joined["pure_market_margin_gap"] = (
        joined["pure_expected_margin"] - joined["market_expected_margin"]
    )
    joined["pure_market_total_gap"] = joined["pure_expected_total"] - joined["market_total"]
    joined["margin_residual_target"] = joined["point_margin"] - joined["market_expected_margin"]
    joined["total_residual_target"] = joined["total_points"] - joined["market_total"]
    required = [
        "home_win",
        "margin_residual_target",
        "total_residual_target",
        *RESIDUAL_FEATURE_COLUMNS,
    ]
    complete = joined.dropna(
        subset=[
            "home_win",
            "market_expected_margin",
            "market_total",
            "market_implied_win_probability",
            "pure_win_probability",
            "pure_expected_margin",
            "pure_expected_total",
        ]
    ).copy()
    if len(complete) < 1000:
        raise ValueError("insufficient_residual_training_rows")
    forbidden = {
        "home_win",
        "point_margin",
        "total_points",
        "margin_residual_target",
        "total_residual_target",
    }
    if forbidden & set(RESIDUAL_FEATURE_COLUMNS):
        raise ValueError("residual_target_leakage_in_feature_contract")
    provenance = {
        "analysis_database": {"path": str(ANALYSIS_DB), "sha256": digest(ANALYSIS_DB)},
        "feature_source": feature_source,
        "market_probability_mapping": mapping,
        "row_count": len(complete),
        "game_date_min": complete["game_date"].min().isoformat(),
        "game_date_max": complete["game_date"].max().isoformat(),
        "pure_prediction_contract": "fact_prediction historical holdout only",
        "historical_market_time_semantics": "line capture time and opening or closing identity unverified",
        "market_probability_training_input": {
            "direct_moneyline_probability_rows": direct_probability_rows,
            "spread_proxy_probability_rows": proxy_probability_rows,
            "warning": "the residual win classifier was trained with spread-derived market probability when historical moneylines were absent",
        },
        "required_training_columns": required,
    }
    return complete.sort_values(["game_date", "game_id"]).reset_index(drop=True), provenance


def _regressor() -> Pipeline:
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", Ridge(alpha=10.0)),
        ]
    )


def _classifier() -> Pipeline:
    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", LogisticRegression(max_iter=1500, C=1.0, random_state=42)),
        ]
    )


def _fit_models(frame: pd.DataFrame) -> dict[str, Pipeline]:
    features = frame[RESIDUAL_FEATURE_COLUMNS]
    return {
        "win_probability": _classifier().fit(features, frame["home_win"]),
        "margin_residual": _regressor().fit(features, frame["margin_residual_target"]),
        "total_residual": _regressor().fit(features, frame["total_residual_target"]),
    }


def _predict_layers(models: dict[str, Pipeline], frame: pd.DataFrame) -> pd.DataFrame:
    features = frame[RESIDUAL_FEATURE_COLUMNS]
    result = frame[
        [
            "game_id",
            "game_date",
            "home_win",
            "point_margin",
            "total_points",
            "pure_win_probability",
            "pure_expected_margin",
            "pure_expected_total",
            "market_implied_win_probability",
            "market_expected_margin",
            "market_total",
            "pure_market_win_probability_gap",
            "pure_market_margin_gap",
            "pure_market_total_gap",
            "margin_residual_target",
            "total_residual_target",
        ]
    ].copy()
    result["predicted_win_probability"] = models["win_probability"].predict_proba(features)[:, 1]
    result["predicted_margin_residual"] = models["margin_residual"].predict(features)
    result["predicted_total_residual"] = models["total_residual"].predict(features)
    result["official_win_probability"] = result["predicted_win_probability"]
    result["official_expected_margin"] = result["market_expected_margin"] + result["predicted_margin_residual"]
    result["official_expected_total"] = result["market_total"] + result["predicted_total_residual"]
    return result


def _smoke_metrics(rows: pd.DataFrame) -> dict:
    return {
        "rows": len(rows),
        "win_probability": {
            "pure": classification_metrics(rows["home_win"], rows["pure_win_probability"].to_numpy()),
            "market": classification_metrics(rows["home_win"], rows["market_implied_win_probability"].to_numpy()),
            "residual_official": classification_metrics(rows["home_win"], rows["official_win_probability"].to_numpy()),
        },
        "margin": {
            "pure": regression_metrics(rows["point_margin"], rows["pure_expected_margin"].to_numpy()),
            "market": regression_metrics(rows["point_margin"], rows["market_expected_margin"].to_numpy()),
            "residual_official": regression_metrics(rows["point_margin"], rows["official_expected_margin"].to_numpy()),
        },
        "total": {
            "pure": regression_metrics(rows["total_points"], rows["pure_expected_total"].to_numpy()),
            "market": regression_metrics(rows["total_points"], rows["market_total"].to_numpy()),
            "residual_official": regression_metrics(rows["total_points"], rows["official_expected_total"].to_numpy()),
        },
    }


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".residual_current_", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(clean(value), handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def train_residual_artifact(output_root: str | Path = MODEL_ROOT) -> Path:
    frame, provenance = build_residual_training_frame()
    unique_days = sorted(frame["game_date"].dt.normalize().unique())
    split_day = pd.Timestamp(unique_days[int(len(unique_days) * 0.75)])
    development_train = frame[frame["game_date"].dt.normalize() < split_day]
    development_test = frame[frame["game_date"].dt.normalize() >= split_day]
    if len(development_train) < 500 or len(development_test) < 100:
        raise ValueError("residual_smoke_split_too_small")
    smoke_models = _fit_models(development_train)
    smoke_rows = _predict_layers(smoke_models, development_test)
    smoke_metrics = _smoke_metrics(smoke_rows)

    version = "market-residual-v1-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    output_root = Path(output_root)
    folder = output_root / version
    folder.mkdir(parents=True, exist_ok=False)
    smoke_rows.to_csv(folder / "development_smoke_rows.csv", index=False)
    save(
        folder / "development_smoke_metrics.json",
        {
            "evaluation_role": "chronological development smoke; not independent acceptance",
            "split_date": split_day.isoformat(),
            "training_rows": len(development_train),
            "test_rows": len(development_test),
            "metrics": smoke_metrics,
        },
    )

    production_models = _fit_models(frame)
    model_entries = {}
    for name, model in production_models.items():
        path = folder / f"{name}.joblib"
        joblib.dump(model, path)
        model_entries[name] = {
            "path": str(Path(version) / path.name),
            "sha256": digest(path),
            "kind": "classifier" if name == "win_probability" else "residual_regressor",
        }
    metadata = {
        "schema_version": "1.0",
        "version": version,
        "prediction_method": "market_residual_v1",
        "trained_at": now(),
        "training_rows": len(frame),
        "training_min_game_date": frame["game_date"].min().isoformat(),
        "training_max_game_date": frame["game_date"].max().isoformat(),
        "training_cutoff": (frame["game_date"].max().normalize() + pd.Timedelta(days=1, hours=12)).tz_localize("America/New_York").isoformat(),
        "feature_columns": RESIDUAL_FEATURE_COLUMNS,
        "feature_contract_hash": content_hash(RESIDUAL_FEATURE_COLUMNS),
        "target_definitions": {
            "margin_residual": "actual_home_margin - market_expected_home_margin",
            "total_residual": "actual_total - market_total",
            "win_probability": "direct home-win classifier using Pure, Market, divergence and time-safe basketball features",
        },
        "models": model_entries,
        "market_probability_mapping": provenance["market_probability_mapping"],
        "provenance": provenance,
        "development_smoke": {
            "rows_path": str(Path(version) / "development_smoke_rows.csv"),
            "metrics_path": str(Path(version) / "development_smoke_metrics.json"),
            "split_date": split_day.isoformat(),
        },
        "historical_market_time_semantics": "exact historical quote capture time and opening or closing identity unverified",
        "official_future_market_requirement": "provider allowlisted and captured by CourtVision before legal cutoff",
        "automatic_promotion": False,
    }
    metadata["metadata_content_hash"] = content_hash(metadata)
    save(folder / "metadata.json", metadata)
    _atomic_json(output_root / "current.json", metadata)
    return folder


def main() -> int:
    folder = train_residual_artifact()
    print(folder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
