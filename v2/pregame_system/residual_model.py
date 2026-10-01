"""Market residual inference for CourtVision official numeric forecasts."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from .market import validate_prediction_market_quote
from .storage import BASE, clean, content_hash, digest, instant


BASKETBALL_CONTEXT_FEATURES = [
    "is_playoffs",
    "elo_diff",
    "diff_rest_days",
    "home_back_to_back",
    "away_back_to_back",
    "diff_games_played_season",
    "diff_win_rate_roll5",
    "diff_points_for_roll5",
    "diff_points_against_roll5",
    "diff_off_rating_roll5",
    "diff_def_rating_roll5",
    "diff_net_rating_roll5",
    "diff_pace_roll5",
    "diff_three_pct_roll5",
    "diff_rebound_share_roll5",
    "diff_turnover_rate_roll5",
    "diff_win_rate_roll15",
    "diff_net_rating_roll15",
    "diff_pace_roll15",
]

RESIDUAL_FEATURE_COLUMNS = [
    "pure_win_probability",
    "pure_expected_margin",
    "pure_expected_total",
    "market_implied_win_probability",
    "market_expected_margin",
    "market_total",
    "pure_market_win_probability_gap",
    "pure_market_margin_gap",
    "pure_market_total_gap",
    *BASKETBALL_CONTEXT_FEATURES,
]

DEFAULT_METADATA = BASE / "models/market_residual_v1/current.json"


def optional_residual(metadata_path=None):
    """Unavailable or corrupt optional models must not prevent pure inference."""
    path=Path(metadata_path or DEFAULT_METADATA)
    if metadata_path is None and not path.is_file():
        from .runtime_assets import DEMO_ROOT
        path=DEMO_ROOT/'residual/current.json'
    try:
        return ResidualPredictor(path)
    except (OSError,ValueError,KeyError,TypeError,EOFError) as exc:
        return UnavailableResidual(f'{type(exc).__name__}: {exc}')


class UnavailableResidual:
    def __init__(self,reason):
        self.reason=reason

    def predict(self,pure_prediction,market,feature_values,*,game,cutoff):
        pure=copy.deepcopy(pure_prediction)
        benchmark=None;divergence=None
        if market is not None:
            quote=validate_prediction_market_quote(market,game,cutoff)
            benchmark={'market_implied_win_probability':quote.get('home_probability_no_vig'),
                'market_expected_margin':quote.get('expected_home_margin'),'market_total':quote.get('total'),
                'provider_id':quote.get('provider_id'),'provider':quote.get('provider'),
                'captured_at':quote.get('captured_at'),'quote_hash':content_hash(quote)}
            row=build_model_features(pure,benchmark,feature_values)
            divergence={name:row[name] for name in ['pure_market_win_probability_gap','pure_market_margin_gap','pure_market_total_gap']}
        return {'prediction_method':'pure_basketball_fallback','pure_prediction':pure,
            'market_benchmark':benchmark,'divergence':divergence,'residual_prediction':None,
            'official_numeric_prediction':copy.deepcopy(pure),'official_market_adjustment':None,
            'residual_model':{'available':False,'used_for_prediction':False,'reason':self.reason},
            'market_usage':{'status':'benchmark_only' if benchmark else 'unavailable',
                'reason':'residual_unavailable','residual_model_version':None},
            'data_completeness':{'pure_prediction':True,'legal_market':bool(benchmark),
                'residual_model':False,'residual_unavailable_reason':self.reason},'legal_cutoff':cutoff}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def flatten_feature_values(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    nested = [item for item in value.values() if isinstance(item, dict)]
    if nested and len(nested) == len(value):
        result = {}
        for item in nested:
            result.update(item)
        return result
    return dict(value)


def build_model_features(
    pure: dict[str, Any],
    market: dict[str, Any],
    feature_values: dict[str, Any] | None,
) -> dict[str, float | None]:
    """Build the exact inference row without outcome or postgame fields."""
    basketball = flatten_feature_values(feature_values)
    market_probability = _finite(
        market.get("market_implied_win_probability", market.get("home_probability_no_vig"))
    )
    market_margin = _finite(
        market.get("market_expected_margin", market.get("expected_home_margin"))
    )
    market_total = _finite(market.get("market_total", market.get("total")))
    pure_probability = _finite(pure.get("win_probability"))
    pure_margin = _finite(pure.get("expected_margin"))
    pure_total = _finite(pure.get("expected_total"))
    row = {
        "pure_win_probability": pure_probability,
        "pure_expected_margin": pure_margin,
        "pure_expected_total": pure_total,
        "market_implied_win_probability": market_probability,
        "market_expected_margin": market_margin,
        "market_total": market_total,
        "pure_market_win_probability_gap": (
            pure_probability - market_probability
            if pure_probability is not None and market_probability is not None
            else None
        ),
        "pure_market_margin_gap": (
            pure_margin - market_margin
            if pure_margin is not None and market_margin is not None
            else None
        ),
        "pure_market_total_gap": (
            pure_total - market_total
            if pure_total is not None and market_total is not None
            else None
        ),
    }
    row.update({name: _finite(basketball.get(name)) for name in BASKETBALL_CONTEXT_FEATURES})
    if set(row) != set(RESIDUAL_FEATURE_COLUMNS):
        raise ValueError("residual_feature_contract_mismatch")
    return {name: row[name] for name in RESIDUAL_FEATURE_COLUMNS}


def _complete_scores(prediction: dict[str, Any]) -> None:
    prediction["away_win_probability"] = 1.0 - float(prediction["win_probability"])
    total = float(prediction["expected_total"])
    margin = float(prediction["expected_margin"])
    prediction["expected_home_score"] = (total + margin) / 2.0
    prediction["expected_away_score"] = (total - margin) / 2.0


class ResidualPredictor:
    def __init__(
        self,
        metadata_path: str | Path | None = None,
        *,
        metadata: dict[str, Any] | None = None,
        models: dict[str, Any] | None = None,
    ):
        if metadata_path is None:
            from .runtime_assets import DEMO_ROOT
            # Bootstrap's explicit demo runtime is self-contained; full local
            # training artifacts remain untouched and are selectable by path.
            if (DEMO_ROOT/'residual/current.json').is_file():
                metadata_path=DEMO_ROOT/'residual/current.json'
        self.metadata_path = Path(metadata_path or DEFAULT_METADATA)
        self.metadata = (
            copy.deepcopy(metadata)
            if metadata is not None
            else json.loads(self.metadata_path.read_text(encoding="utf-8"))
        )
        if self.metadata.get("prediction_method") != "market_residual_v1":
            raise ValueError("invalid_residual_model_method")
        expected_hash=self.metadata.get('metadata_content_hash')
        if expected_hash:
            unsigned={key:value for key,value in self.metadata.items() if key!='metadata_content_hash'}
            if content_hash(unsigned)!=expected_hash:raise ValueError('residual_metadata_integrity')
        if self.metadata.get("feature_columns") != RESIDUAL_FEATURE_COLUMNS:
            raise ValueError("invalid_residual_feature_contract")
        self.models = models or self._load_models()

    def _load_models(self) -> dict[str, Any]:
        result = {}
        root = self.metadata_path.parent
        for name, info in self.metadata.get("models", {}).items():
            path = Path(info["path"])
            if not path.is_absolute():
                path = root / path
            if not path.is_file() or digest(path) != info.get("sha256"):
                raise ValueError(f"residual_model_integrity:{name}")
            result[name] = joblib.load(path)
        if set(result) != {"win_probability", "margin_residual", "total_residual"}:
            raise ValueError("residual_model_set_invalid")
        return result

    def _market_probability(self, quote: dict[str, Any]) -> float | None:
        direct = _finite(quote.get("home_probability_no_vig"))
        if direct is not None:
            return direct
        margin = _finite(quote.get("expected_home_margin"))
        mapping = self.metadata.get("market_probability_mapping") or {}
        coefficient = _finite(mapping.get("coef"))
        intercept = _finite(mapping.get("intercept"))
        if margin is None or coefficient is None or intercept is None:
            return None
        return _sigmoid(intercept + coefficient * margin)

    def predict(
        self,
        pure_prediction: dict[str, Any],
        market: dict[str, Any] | None,
        feature_values: dict[str, Any] | None,
        *,
        game: dict[str, Any],
        cutoff: str,
    ) -> dict[str, Any]:
        pure = copy.deepcopy(pure_prediction)
        training_cutoff=self.metadata.get('training_cutoff')
        if market is None:
            return {
                "prediction_method": "pure_basketball_fallback",
                "residual_model": {
                    "version": self.metadata["version"],
                    "training_cutoff": training_cutoff,
                    "metadata_content_hash": self.metadata.get("metadata_content_hash"),
                    "feature_contract_hash": self.metadata.get("feature_contract_hash"),
                    "used_for_prediction": False,
                },
                "pure_prediction": pure,
                "market_benchmark": None,
                "divergence": None,
                "residual_prediction": None,
                "official_numeric_prediction": copy.deepcopy(pure),
                "official_market_adjustment": None,
                "market_usage": {
                    "status": "unavailable",
                    "reason": "no_eligible_market",
                    "residual_model_version": self.metadata["version"],
                },
                "data_completeness": {
                    "pure_prediction": True,
                    "legal_market": False,
                    "market_win_probability": False,
                    "market_margin": False,
                    "market_total": False,
                    "residual_model": False,
                },
            }

        if training_cutoff and instant(training_cutoff)>instant(cutoff):
            raise ValueError('residual_model_trained_after_cutoff')

        quote = validate_prediction_market_quote(market, game, cutoff)
        market_probability = self._market_probability(quote)
        benchmark = {
            "market_implied_win_probability": market_probability,
            "market_expected_margin": _finite(quote.get("expected_home_margin")),
            "market_total": _finite(quote.get("total")),
            "provider_id": quote.get("provider_id"),
            "provider": quote.get("provider"),
            "captured_at": quote.get("captured_at"),
            "source_published_at": quote.get("source_published_at"),
            "quote_hash": content_hash(quote),
        }
        row = build_model_features(pure, benchmark, feature_values)
        frame = pd.DataFrame([row], columns=RESIDUAL_FEATURE_COLUMNS)
        official = copy.deepcopy(pure)
        residual = {
            "win_probability_residual": None,
            "expected_margin_residual": None,
            "expected_total_residual": None,
        }
        adjustments = {
            "win_probability": None,
            "expected_margin": None,
            "expected_total": None,
        }
        if market_probability is not None:
            win_probability = float(self.models["win_probability"].predict_proba(frame)[:, 1][0])
            win_probability = min(max(win_probability, 1e-6), 1.0 - 1e-6)
            official["win_probability"] = win_probability
            residual["win_probability_residual"] = win_probability - market_probability
            adjustments["win_probability"] = residual["win_probability_residual"]
        if benchmark["market_expected_margin"] is not None:
            margin_residual = float(self.models["margin_residual"].predict(frame)[0])
            if not math.isfinite(margin_residual):
                raise ValueError("nonfinite_margin_residual")
            official["expected_margin"] = benchmark["market_expected_margin"] + margin_residual
            residual["expected_margin_residual"] = margin_residual
            adjustments["expected_margin"] = margin_residual
        if benchmark["market_total"] is not None:
            total_residual = float(self.models["total_residual"].predict(frame)[0])
            if not math.isfinite(total_residual):
                raise ValueError("nonfinite_total_residual")
            official["expected_total"] = benchmark["market_total"] + total_residual
            residual["expected_total_residual"] = total_residual
            adjustments["expected_total"] = total_residual
        _complete_scores(official)
        divergence = {
            "pure_market_win_probability_gap": row["pure_market_win_probability_gap"],
            "pure_market_margin_gap": row["pure_market_margin_gap"],
            "pure_market_total_gap": row["pure_market_total_gap"],
            "meaning": "Pure Basketball Prediction minus Market Benchmark",
        }
        return clean(
            {
                "prediction_method": "market_residual_v1",
                "residual_model": {
                    "version": self.metadata["version"],
                    "training_cutoff": training_cutoff,
                    "metadata_content_hash": self.metadata.get("metadata_content_hash"),
                    "feature_contract_hash": self.metadata.get("feature_contract_hash"),
                    "used_for_prediction": True,
                },
                "pure_prediction": pure,
                "market_benchmark": benchmark,
                "divergence": divergence,
                "residual_prediction": residual,
                "official_numeric_prediction": official,
                "official_market_adjustment": adjustments,
                "market_usage": {
                    "status": "used",
                    "reason": None,
                    "residual_model_version": self.metadata["version"],
                    "quote_hash": benchmark["quote_hash"],
                    "provider_id": benchmark["provider_id"],
                    "provider": benchmark["provider"],
                    "historical_training_market_time_verified": False,
                },
                "data_completeness": {
                    "pure_prediction": True,
                    "legal_market": True,
                    "market_win_probability": market_probability is not None,
                    "market_margin": benchmark["market_expected_margin"] is not None,
                    "market_total": benchmark["market_total"] is not None,
                    "residual_model": True,
                },
                "legal_cutoff": cutoff,
                "feature_contract_hash": content_hash(RESIDUAL_FEATURE_COLUMNS),
            }
        )
