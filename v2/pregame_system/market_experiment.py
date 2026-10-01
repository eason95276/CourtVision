"""Minimal chronological experiment for choosing market incorporation method.

Historical lines have no verified quote timestamp.  Results select an
engineering method for future timestamp-qualified quotes; they do not prove a
historical T-5 edge or a tradable result.
"""
from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression, LogisticRegression

from courtvision.data import load_odds
from courtvision.market import transform_market_fields
from courtvision.metrics import classification_metrics, regression_metrics
from courtvision.models import build_model, predict_for_task

from . import ROOT
from .experiment import TASKS
from .features import history
from .prediction import latest
from .storage import BASE, clean, digest, new_run, now, save


EVALUATION_SEASONS = (2023, 2024, 2025)
ANCHOR_FIELDS = {
    "win_probability": "market_expected_home_margin",
    "expected_margin": "market_expected_home_margin",
    "expected_total": "market_total",
}


def _metrics(task: str, actual: pd.Series, prediction: np.ndarray) -> dict:
    if task == "win_probability":
        return classification_metrics(actual, np.asarray(prediction, dtype=float))
    return regression_metrics(actual, np.asarray(prediction, dtype=float))


def _primary(task: str, scores: dict) -> float:
    return float(scores["log_loss"] if task == "win_probability" else scores["mae"])


def _join_market(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    games, _ = history()
    identity = games[["game_id", "game_date", "home_abbr", "away_abbr"]].copy()
    identity["game_day"] = identity["game_date"].dt.normalize()
    odds_path = ROOT / "data/raw/nba_2008-2026.csv"
    market = transform_market_fields(load_odds(odds_path)).rename(
        columns={"home": "home_abbr", "away": "away_abbr"}
    )
    market["game_day"] = market["date"].dt.normalize()
    market = market[
        [
            "game_day",
            "home_abbr",
            "away_abbr",
            "market_expected_home_margin",
            "market_total",
            "market_home_win_probability",
        ]
    ]
    if market.duplicated(["game_day", "home_abbr", "away_abbr"]).any():
        raise ValueError("historical_market_identity_not_unique")
    joined = frame.copy()
    missing_identity = [
        column
        for column in ("game_date", "home_abbr", "away_abbr")
        if column not in joined
    ]
    if missing_identity:
        joined = joined.merge(
            identity[["game_id", *missing_identity]],
            on="game_id",
            how="left",
            validate="one_to_one",
        )
    joined["game_day"] = pd.to_datetime(joined["game_date"]).dt.normalize()
    joined = joined.merge(
        market,
        on=["game_day", "home_abbr", "away_abbr"],
        how="left",
        validate="one_to_one",
    )
    coverage = {
        "rows": len(joined),
        "spread_rows": int(joined["market_expected_home_margin"].notna().sum()),
        "total_rows": int(joined["market_total"].notna().sum()),
        "moneyline_rows": int(joined["market_home_win_probability"].notna().sum()),
        "market_source": str(odds_path),
        "market_source_sha256": digest(odds_path),
        "identity_rule": "Eastern game day plus exact home and away abbreviations",
        "exact_quote_time_verified": False,
    }
    return joined, coverage


def _fit_base(task: str, train: pd.DataFrame, columns: list[str]):
    target, model_name, _, _ = TASKS[task]
    model = build_model(task, model_name)
    model.fit(train[columns], train[target])
    return model


def _market_probability_model(train: pd.DataFrame):
    sample = train.dropna(subset=["market_expected_home_margin", "home_win"])
    if len(sample) < 100:
        raise ValueError("insufficient_market_probability_training_rows")
    model = LogisticRegression(max_iter=1500, random_state=42)
    model.fit(sample[["market_expected_home_margin"]], sample["home_win"])
    return model


def _market_anchor_predictions(
    task: str,
    train: pd.DataFrame,
    test: pd.DataFrame,
) -> np.ndarray:
    field = ANCHOR_FIELDS[task]
    if task == "win_probability":
        model = _market_probability_model(train)
        return model.predict_proba(test[[field]])[:, 1]
    return test[field].to_numpy(dtype=float)


def _best_weight(task: str, actual: pd.Series, baseline, market) -> float:
    best = None
    for weight in np.linspace(0.0, 1.0, 21):
        prediction = (1.0 - weight) * baseline + weight * market
        score = _primary(task, _metrics(task, actual, prediction))
        candidate = (score, float(weight))
        if best is None or candidate < best:
            best = candidate
    return best[1]


def _fit_stack(task: str, baseline, market, actual):
    x = np.column_stack([baseline, market])
    if task == "win_probability":
        model = LogisticRegression(max_iter=1500, random_state=42)
        model.fit(x, actual)
    else:
        model = LinearRegression().fit(x, actual)
    return model


def _stack_predict(task: str, model, baseline, market) -> np.ndarray:
    x = np.column_stack([baseline, market])
    if task == "win_probability":
        return model.predict_proba(x)[:, 1]
    return model.predict(x)


def _meta_fold(
    frame: pd.DataFrame,
    task: str,
    season: int,
    columns: list[str],
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    validation_season = season - 1
    anchor = ANCHOR_FIELDS[task]
    train = frame[frame["season_start"] < validation_season]
    valid = frame[
        (frame["season_start"] == validation_season) & frame[anchor].notna()
    ]
    if len(train) < 500 or len(valid) < 100:
        raise ValueError("insufficient_meta_fold")
    base_model = _fit_base(task, train, columns)
    baseline = predict_for_task(base_model, task, valid[columns])
    market = _market_anchor_predictions(task, train, valid)
    return valid, np.asarray(baseline, dtype=float), np.asarray(market, dtype=float)


def _season_backtest(
    frame: pd.DataFrame,
    task: str,
    season: int,
    columns: list[str],
) -> tuple[list[dict], dict[str, dict[str, list[float]]]]:
    target, model_name, _, primary = TASKS[task]
    anchor = ANCHOR_FIELDS[task]
    train = frame[frame["season_start"] < season]
    test = frame[(frame["season_start"] == season) & frame[anchor].notna()]
    if len(test) < 100:
        raise ValueError(f"insufficient_market_test_rows:{season}:{task}")

    base_model = build_model(task, model_name).fit(train[columns], train[target])
    baseline = np.asarray(predict_for_task(base_model, task, test[columns]), dtype=float)
    market_anchor = _market_anchor_predictions(task, train, test)

    direct_columns = [*columns, anchor]
    direct_train = train[train[anchor].notna()]
    direct_model = build_model(task, model_name).fit(
        direct_train[direct_columns], direct_train[target]
    )
    direct = np.asarray(
        predict_for_task(direct_model, task, test[direct_columns]), dtype=float
    )

    meta, meta_baseline, meta_market = _meta_fold(frame, task, season, columns)
    weight = _best_weight(task, meta[target], meta_baseline, meta_market)
    calibrated = (1.0 - weight) * baseline + weight * market_anchor
    stack = _fit_stack(task, meta_baseline, meta_market, meta[target])
    fused = _stack_predict(task, stack, baseline, market_anchor)

    rows = []
    predictions = {
        "basketball_only": baseline,
        "direct_feature": direct,
        "bounded_calibration": calibrated,
        "stacked_fusion": fused,
    }
    for method, values in predictions.items():
        scores = _metrics(task, test[target], values)
        rows.append(
            {
                "task": task,
                "season": season,
                "method": method,
                "rows": len(test),
                "train_rows": len(train),
                "primary_metric": primary,
                "primary_value": _primary(task, scores),
                "calibration_weight": weight if method == "bounded_calibration" else None,
                **scores,
            }
        )

    recent_train = train[train["season_start"] >= season - 3]
    recent_model = build_model(task, model_name).fit(
        recent_train[columns], recent_train[target]
    )
    recent_prediction = predict_for_task(recent_model, task, test[columns])
    window_rows = [
        {
            "task": task,
            "season": season,
            "training_window": "all_available_prior_history",
            "train_rows": len(train),
            "rows": len(test),
            **_metrics(task, test[target], baseline),
        },
        {
            "task": task,
            "season": season,
            "training_window": "prior_three_complete_seasons",
            "train_rows": len(recent_train),
            "rows": len(test),
            **_metrics(task, test[target], recent_prediction),
        },
    ]
    oof = {
        task: {
            "actual": test[target].astype(float).tolist(),
            "baseline": baseline.tolist(),
            "market": market_anchor.tolist(),
        }
    }
    return rows, {"window_rows": window_rows, "oof": oof}


def _method_selection(metrics: pd.DataFrame) -> dict[str, str]:
    result = {}
    for task, part in metrics.groupby("task"):
        candidates = part[part["method"] != "basketball_only"]
        means = candidates.groupby("method")["primary_value"].mean()
        result[task] = str(means.sort_values(kind="stable").index[0])
    return result


def _spread_probability_config(frame: pd.DataFrame) -> dict:
    model = _market_probability_model(frame)
    return {
        "coef": float(model.coef_[0][0]),
        "intercept": float(model.intercept_[0]),
        "training_rows": int(frame["market_expected_home_margin"].notna().sum()),
    }


def _production_task_config(
    task: str,
    method: str,
    frame: pd.DataFrame,
    columns: list[str],
    pooled: dict[str, list[float]],
    folder: Path,
    spread_probability: dict,
) -> dict:
    target, model_name, _, _ = TASKS[task]
    anchor = ANCHOR_FIELDS[task]
    actual = np.asarray(pooled["actual"], dtype=float)
    baseline = np.asarray(pooled["baseline"], dtype=float)
    market = np.asarray(pooled["market"], dtype=float)
    config = {
        "method": method,
        "market_field": "home_probability_no_vig" if task == "win_probability" else (
            "expected_home_margin" if task == "expected_margin" else "total"
        ),
        "historical_experiment_anchor": anchor,
    }
    if task == "win_probability":
        config["spread_probability"] = spread_probability
    if method == "bounded_calibration":
        config["weight"] = _best_weight(task, pd.Series(actual), baseline, market)
    elif method == "stacked_fusion":
        model = _fit_stack(task, baseline, market, actual)
        config.update(
            {
                "intercept": float(model.intercept_[0] if task == "win_probability" else model.intercept_),
                "basketball_coefficient": float(model.coef_[0][0] if task == "win_probability" else model.coef_[0]),
                "market_coefficient": float(model.coef_[0][1] if task == "win_probability" else model.coef_[1]),
            }
        )
    elif method == "direct_feature":
        sample = frame[frame[anchor].notna()]
        direct_columns = [*columns, anchor]
        model = build_model(task, model_name).fit(sample[direct_columns], sample[target])
        path = folder / f"{task}_direct_feature.joblib"
        joblib.dump(model, path)
        config.update(
            {
                "model_path": str(path),
                "model_sha256": digest(path),
                "feature_columns": direct_columns,
                "market_feature_column": anchor,
            }
        )
    else:
        raise ValueError("unknown_selected_market_method")
    return config


def run_market_method_experiment(output_root: Path | None = None) -> Path:
    experiment_frame = pd.read_pickle(latest("experiments", "dataset.pkl"))
    groups = json.loads(
        latest("experiments", "feature_groups.json").read_text(encoding="utf-8")
    )
    columns = list(groups["baseline"])
    frame, coverage = _join_market(experiment_frame)
    folder = new_run("market_methods") if output_root is None else Path(output_root)
    folder.mkdir(parents=True, exist_ok=True)

    metric_rows = []
    window_rows = []
    pooled = {
        task: {"actual": [], "baseline": [], "market": []}
        for task in TASKS
    }
    for task in TASKS:
        for season in EVALUATION_SEASONS:
            rows, extra = _season_backtest(frame, task, season, columns)
            metric_rows.extend(rows)
            window_rows.extend(extra["window_rows"])
            values = extra["oof"][task]
            for key in pooled[task]:
                pooled[task][key].extend(values[key])

    metrics = pd.DataFrame(metric_rows)
    windows = pd.DataFrame(window_rows)
    selection = _method_selection(metrics)
    spread_probability = _spread_probability_config(frame.dropna(subset=["market_expected_home_margin"]))
    task_configs = {
        task: _production_task_config(
            task,
            selection[task],
            frame,
            columns,
            pooled[task],
            folder,
            spread_probability,
        )
        for task in TASKS
    }
    metrics.to_csv(folder / "metrics.csv", index=False)
    windows.to_csv(folder / "training_window_comparison.csv", index=False)
    method = {
        "version": "market-method-" + folder.name,
        "created_at": now(),
        "evaluation_seasons": list(EVALUATION_SEASONS),
        "selection_policy": "lowest mean task primary loss among market methods; market inclusion is mandatory when a legal quote exists",
        "selected_methods": selection,
        "tasks": task_configs,
        "coverage": coverage,
        "historical_market_time_semantics": "unknown exact quote time; method experiment only",
        "basketball_only_comparator_retained": True,
        "formal_model_training_window": "all locally available history through production cutoff",
        "candidate_training_window": "most recent three complete seasons",
        "automatic_model_promotion": False,
        "source_hashes": {
            str(latest("experiments", "dataset.pkl")): digest(latest("experiments", "dataset.pkl")),
            str(ROOT / "data/raw/Games.csv"): digest(ROOT / "data/raw/Games.csv"),
            str(ROOT / "data/raw/TeamStatistics.csv"): digest(ROOT / "data/raw/TeamStatistics.csv"),
        },
    }
    save(folder / "method.json", clean(method))
    save(
        folder / "summary.json",
        {
            "selected_methods": selection,
            "coverage": coverage,
            "metric_rows": len(metrics),
            "window_comparison_rows": len(windows),
        },
    )
    return folder


if __name__ == "__main__":
    print(run_market_method_experiment(), flush=True)
