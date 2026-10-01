from __future__ import annotations

import copy

import numpy as np
import pytest

from .residual_model import ResidualPredictor, build_model_features


GAME = {
    "game_id": "espn:401909088",
    "home_team_id": 1610612765,
    "away_team_id": 1610612738,
    "scheduled_tipoff": "2026-10-20T19:00:00+00:00",
}

PURE = {
    "win_probability": 0.62,
    "away_win_probability": 0.38,
    "expected_margin": 7.0,
    "expected_total": 240.0,
    "expected_home_score": 123.5,
    "expected_away_score": 116.5,
    "perspective": "home",
    "is_forecast": True,
}

MARKET = {
    "game_id": GAME["game_id"],
    "home_team_id": GAME["home_team_id"],
    "away_team_id": GAME["away_team_id"],
    "captured_at": "2026-10-20T18:54:00+00:00",
    "source_published_at": None,
    "eligible_prediction_input": True,
    "provider_id": "100",
    "provider": "Draft Kings",
    "expected_home_margin": 2.5,
    "total": 230.5,
    "home_probability_no_vig": 0.56,
    "source": {"kind": "fixture"},
}

FEATURES = {
    "is_playoffs": 0,
    "elo_diff": 25.0,
    "diff_rest_days": 1.0,
    "home_back_to_back": 0,
    "away_back_to_back": 1,
    "diff_games_played_season": 0,
    "diff_win_rate_roll5": 0.2,
    "diff_points_for_roll5": 4.0,
    "diff_points_against_roll5": -2.0,
    "diff_off_rating_roll5": 3.0,
    "diff_def_rating_roll5": -1.0,
    "diff_net_rating_roll5": 4.0,
    "diff_pace_roll5": 2.0,
    "diff_three_pct_roll5": 0.03,
    "diff_rebound_share_roll5": 0.02,
    "diff_turnover_rate_roll5": -0.01,
    "diff_win_rate_roll15": 0.1,
    "diff_net_rating_roll15": 2.0,
    "diff_pace_roll15": 1.0,
}


class FixedRegressor:
    def __init__(self, value):
        self.value = value

    def predict(self, frame):
        return np.full(len(frame), self.value, dtype=float)


class FixedClassifier:
    def __init__(self, probability):
        self.probability = probability

    def predict_proba(self, frame):
        return np.tile([1 - self.probability, self.probability], (len(frame), 1))


@pytest.fixture
def residual_predictor():
    metadata = {
        "schema_version": "1.0",
        "version": "market-residual-test",
        "prediction_method": "market_residual_v1",
        "feature_columns": list(build_model_features(PURE, MARKET, FEATURES).keys()),
        "market_probability_mapping": {"coef": 0.14, "intercept": 0.0},
        "models": {
            "win_probability": {"kind": "classifier"},
            "margin_residual": {"kind": "regressor"},
            "total_residual": {"kind": "regressor"},
        },
    }
    return ResidualPredictor(
        metadata=metadata,
        models={
            "win_probability": FixedClassifier(0.48),
            "margin_residual": FixedRegressor(-5.0),
            "total_residual": FixedRegressor(-2.5),
        },
    )


def test_residual_model_can_cross_market_line(residual_predictor):
    result = residual_predictor.predict(
        PURE,
        MARKET,
        FEATURES,
        game=GAME,
        cutoff="2026-10-20T18:55:00+00:00",
    )

    assert result["prediction_method"] == "market_residual_v1"
    assert result["pure_prediction"] == PURE
    assert result["market_benchmark"]["market_expected_margin"] == 2.5
    assert result["residual_prediction"]["expected_margin_residual"] == -5.0
    assert result["official_numeric_prediction"]["expected_margin"] == -2.5
    assert result["official_numeric_prediction"]["expected_total"] == 228.0
    assert result["official_market_adjustment"]["expected_margin"] == -5.0
    assert result["official_numeric_prediction"]["expected_margin"] < 0 < PURE["expected_margin"]


def test_missing_market_returns_pure_without_loading_residual_models():
    class MustNotRun:
        def predict(self, frame):
            raise AssertionError("residual model must not run")

        def predict_proba(self, frame):
            raise AssertionError("residual model must not run")

    predictor = ResidualPredictor(
        metadata={
            "schema_version": "1.0",
            "version": "test",
            "prediction_method": "market_residual_v1",
            "feature_columns": list(build_model_features(PURE, MARKET, FEATURES).keys()),
            "market_probability_mapping": {"coef": 0.14, "intercept": 0.0},
            "models": {
                "win_probability": {},
                "margin_residual": {},
                "total_residual": {},
            },
        },
        models={
            "win_probability": MustNotRun(),
            "margin_residual": MustNotRun(),
            "total_residual": MustNotRun(),
        },
    )
    result = predictor.predict(
        PURE,
        None,
        FEATURES,
        game=GAME,
        cutoff="2026-10-20T18:55:00+00:00",
    )
    assert result["official_numeric_prediction"] == PURE
    assert result["residual_prediction"] is None
    assert result["market_benchmark"] is None
    assert result["market_usage"]["status"] == "unavailable"


def test_partial_market_falls_back_by_task(residual_predictor):
    market = copy.deepcopy(MARKET)
    market["total"] = None
    result = residual_predictor.predict(
        PURE,
        market,
        FEATURES,
        game=GAME,
        cutoff="2026-10-20T18:55:00+00:00",
    )
    assert result["official_numeric_prediction"]["expected_total"] == PURE["expected_total"]
    assert result["residual_prediction"]["expected_total_residual"] is None
    assert result["data_completeness"]["market_total"] is False


def test_feature_contract_contains_divergence_and_no_outcome_fields():
    row = build_model_features(PURE, MARKET, FEATURES)
    assert row["pure_market_margin_gap"] == 4.5
    assert row["pure_market_total_gap"] == 9.5
    assert row["pure_market_win_probability_gap"] == pytest.approx(0.06)
    assert not any(
        token in key
        for key in row
        for token in ("actual", "score", "result", "target")
    )


def test_formal_residual_output_contains_no_fixed_weight(residual_predictor):
    result = residual_predictor.predict(
        PURE,
        MARKET,
        FEATURES,
        game=GAME,
        cutoff="2026-10-20T18:55:00+00:00",
    )
    import json

    assert "weight" not in json.dumps(result)


def test_illegal_provider_is_rejected_before_residual_inference(residual_predictor):
    market = copy.deepcopy(MARKET)
    market.update(provider_id="1001", provider="accuscore")
    with pytest.raises(ValueError, match="market_provider_not_allowed"):
        residual_predictor.predict(
            PURE,
            market,
            FEATURES,
            game=GAME,
            cutoff="2026-10-20T18:55:00+00:00",
        )


def test_residual_artifact_after_cutoff_is_rejected(residual_predictor):
    residual_predictor.metadata['training_cutoff']='2027-01-01T00:00:00+00:00'
    with pytest.raises(ValueError,match='residual_model_trained_after_cutoff'):
        residual_predictor.predict(
            PURE,MARKET,FEATURES,game=GAME,cutoff='2026-10-20T18:55:00+00:00')


def test_real_training_frame_uses_holdout_predictions_and_explicit_residual_targets():
    from .train_residual_model import build_residual_training_frame

    frame, provenance = build_residual_training_frame()
    assert len(frame) == 1322
    assert frame["game_id"].is_unique
    assert frame["margin_residual_target"].equals(
        frame["point_margin"] - frame["market_expected_margin"]
    )
    assert frame["total_residual_target"].equals(
        frame["total_points"] - frame["market_total"]
    )
    assert provenance["pure_prediction_contract"] == "fact_prediction historical holdout only"
    assert not provenance["historical_market_time_semantics"].startswith("verified")


def test_default_artifact_loads_with_integrity_and_batch_contract():
    from .residual_batch import REQUIRED_COLUMNS, build_batch_rows
    from .train_residual_model import build_residual_training_frame

    predictor = ResidualPredictor()
    frame, _ = build_residual_training_frame()
    rows = build_batch_rows(frame.head(5), predictor)
    assert len(rows) == 5
    assert REQUIRED_COLUMNS <= set(rows)
    assert rows["game_id"].is_unique
    assert np.allclose(
        rows["official_expected_margin"],
        rows["market_expected_margin"] + rows["predicted_margin_residual"],
    )
    assert rows["training_overlap"].all()
