"""Deprecated fixed-blend experiment retained for historical reproducibility.

The formal prediction path no longer imports or calls ``apply_market_method``.
Provider and time validation remain compatible through the shared market module.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from .storage import BASE, content_hash
from .storage import digest
from courtvision.models import predict_for_task


TASK_ANCHORS = {
    "win_probability": "home_probability_no_vig",
    "expected_margin": "expected_home_margin",
    "expected_total": "total",
}


def load_latest_market_method(root: Path | None = None) -> dict[str, Any]:
    fixed = BASE / "market_method.json"
    base = Path(root or BASE / "outputs/market_methods")
    paths = [fixed] if root is None and fixed.is_file() else sorted(base.glob("*/method.json"))
    if not paths:
        raise ValueError("missing_market_method")
    value = json.loads(paths[-1].read_text(encoding="utf-8"))
    if not value.get("version") or set(value.get("tasks") or {}) != set(TASK_ANCHORS):
        raise ValueError("invalid_market_method_artifact")
    return value


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def validate_eligible_market(
    market: dict[str, Any],
    game: dict[str, Any],
    cutoff: str,
) -> dict[str, Any]:
    """Return a copied legal quote or raise a stable contract error."""
    from .market import validate_prediction_market_quote

    return validate_prediction_market_quote(market,game,cutoff)


def _market_anchor(task: str, quote: dict[str, Any], config: dict[str, Any]) -> float | None:
    field = config.get("market_field") or TASK_ANCHORS[task]
    anchor = _finite(quote.get(field))
    if task == "win_probability" and anchor is None:
        margin = _finite(quote.get("expected_home_margin"))
        mapping = config.get("spread_probability") or {}
        coefficient = _finite(mapping.get("coef"))
        intercept = _finite(mapping.get("intercept"))
        if margin is not None and coefficient is not None and intercept is not None:
            anchor = _sigmoid(intercept + coefficient * margin)
    return anchor


def _apply_task(
    task: str,
    baseline: float,
    anchor: float,
    config: dict[str, Any],
    feature_values: dict[str, Any] | None = None,
) -> float:
    method = config.get("method")
    if method == "bounded_calibration":
        weight = _finite(config.get("weight"))
        if weight is None or not 0.0 <= weight <= 1.0:
            raise ValueError("invalid_market_calibration_weight")
        value = (1.0 - weight) * baseline + weight * anchor
    elif method == "stacked_fusion":
        intercept = _finite(config.get("intercept"))
        baseline_coefficient = _finite(config.get("basketball_coefficient"))
        market_coefficient = _finite(config.get("market_coefficient"))
        if None in (intercept, baseline_coefficient, market_coefficient):
            raise ValueError("invalid_market_fusion_coefficients")
        raw = intercept + baseline_coefficient * baseline + market_coefficient * anchor
        value = _sigmoid(raw) if task == "win_probability" else raw
    elif method == "direct_feature":
        model_path = config.get("model_path")
        columns = config.get("feature_columns")
        market_column = config.get("market_feature_column")
        if model_path and columns and market_column:
            path = Path(model_path)
            if not path.is_file() or digest(path) != config.get("model_sha256"):
                raise ValueError("market_direct_model_integrity")
            if feature_values is None:
                raise ValueError("market_direct_features_missing")
            row = dict(feature_values)
            row[market_column] = anchor
            if not set(columns) <= set(row):
                raise ValueError("market_direct_feature_schema")
            model = joblib.load(path)
            value = float(
                predict_for_task(model, task, pd.DataFrame([row])[columns])[0]
            )
        else:
            intercept = _finite(config.get("intercept"))
            baseline_coefficient = _finite(config.get("basketball_coefficient"))
            market_coefficient = _finite(config.get("market_coefficient"))
            if None in (intercept, baseline_coefficient, market_coefficient):
                raise ValueError("invalid_market_direct_coefficients")
            raw = intercept + baseline_coefficient * baseline + market_coefficient * anchor
            value = _sigmoid(raw) if task == "win_probability" else raw
    else:
        raise ValueError("unsupported_market_method")
    if task == "win_probability":
        value = min(max(value, 1e-6), 1.0 - 1e-6)
    if not math.isfinite(value):
        raise ValueError("nonfinite_market_informed_prediction")
    return float(value)


def _complete_scores(prediction: dict[str, Any]) -> None:
    probability = float(prediction["win_probability"])
    prediction["away_win_probability"] = 1.0 - probability
    total = float(prediction["expected_total"])
    margin = float(prediction["expected_margin"])
    prediction["expected_home_score"] = (total + margin) / 2.0
    prediction["expected_away_score"] = (total - margin) / 2.0


def apply_market_method(
    basketball_prediction: dict[str, Any],
    market: dict[str, Any] | None,
    method_artifact: dict[str, Any],
    *,
    game: dict[str, Any],
    cutoff: str,
    feature_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return baseline, market-informed and public forecast variants."""
    baseline = copy.deepcopy(basketball_prediction)
    version = str(method_artifact.get("version") or "unknown")
    if market is None:
        return {
            "basketball_only_prediction": baseline,
            "market_informed_prediction": None,
            "public_prediction": copy.deepcopy(baseline),
            "market_usage": {
                "status": "unavailable",
                "reason": "no_eligible_market",
                "method_version": version,
                "task_status": {},
            },
        }

    quote = validate_eligible_market(market, game, cutoff)
    informed = copy.deepcopy(baseline)
    task_status: dict[str, str] = {}
    used_fields: dict[str, float] = {}
    tasks = method_artifact.get("tasks") or {}
    enhanced = 0
    for task in TASK_ANCHORS:
        config = tasks.get(task)
        if not isinstance(config, dict):
            raise ValueError(f"market_method_missing_task:{task}")
        anchor = _market_anchor(task, quote, config)
        if anchor is None:
            task_status[task] = "market_anchor_missing"
            continue
        base_value = _finite(baseline.get(task))
        if base_value is None:
            raise ValueError(f"basketball_prediction_missing_task:{task}")
        informed[task] = _apply_task(
            task,
            base_value,
            anchor,
            config,
            feature_values=feature_values,
        )
        task_status[task] = "market_used"
        used_fields[task] = anchor
        enhanced += 1
    if enhanced == 0:
        raise ValueError("eligible_market_did_not_affect_prediction")
    informed["market_informed"] = True
    informed["market_method_version"] = version
    _complete_scores(informed)
    usage = {
        "status": "used",
        "reason": None,
        "method_version": version,
        "task_status": task_status,
        "used_market_anchors": used_fields,
        "quote_captured_at": quote["captured_at"],
        "quote_hash": content_hash(quote),
        "provider": quote.get("provider"),
        "source": quote.get("source"),
    }
    return {
        "basketball_only_prediction": baseline,
        "market_informed_prediction": informed,
        "public_prediction": copy.deepcopy(informed),
        "market_usage": usage,
    }
