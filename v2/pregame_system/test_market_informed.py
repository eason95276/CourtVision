from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from .market_informed import apply_market_method, validate_eligible_market
from .market import latest_legal_market, normalize_current_market, save_live_market_snapshot
from .official_final import OfficialFinalizer, validate_report
from .residual_model import BASKETBALL_CONTEXT_FEATURES, ResidualPredictor


GAME = {
    "game_id": "espn:401909088",
    "home_team_id": 1610612765,
    "away_team_id": 1610612738,
    "scheduled_tipoff": "2026-10-20T19:00:00+00:00",
}

BASELINE = {
    "win_probability": 0.55,
    "away_win_probability": 0.45,
    "expected_margin": 1.0,
    "expected_total": 220.0,
    "expected_home_score": 110.5,
    "expected_away_score": 109.5,
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
    "expected_home_margin": -2.5,
    "total": 226.5,
    "home_probability_no_vig": None,
    "provider_id": "100",
    "provider": "Draft Kings",
    "source": {"kind": "fixture"},
}

METHOD = {
    "version": "market-method-test",
    "tasks": {
        "win_probability": {
            "method": "bounded_calibration",
            "market_field": "home_probability_no_vig",
            "weight": 0.75,
            "spread_probability": {"coef": 0.12, "intercept": 0.0},
        },
        "expected_margin": {
            "method": "bounded_calibration",
            "market_field": "expected_home_margin",
            "weight": 0.75,
        },
        "expected_total": {
            "method": "bounded_calibration",
            "market_field": "total",
            "weight": 0.75,
        },
    },
}


def test_eligible_market_changes_public_prediction_and_retains_baseline():
    result = apply_market_method(
        BASELINE,
        MARKET,
        METHOD,
        game=GAME,
        cutoff="2026-10-20T18:55:00+00:00",
    )

    assert result["basketball_only_prediction"] == BASELINE
    assert result["public_prediction"] == result["market_informed_prediction"]
    assert result["public_prediction"]["expected_margin"] != BASELINE["expected_margin"]
    assert result["public_prediction"]["expected_total"] != BASELINE["expected_total"]
    assert result["market_usage"]["status"] == "used"
    assert result["market_usage"]["method_version"] == METHOD["version"]


def test_missing_market_uses_baseline_with_reason():
    result = apply_market_method(
        BASELINE,
        None,
        METHOD,
        game=GAME,
        cutoff="2026-10-20T18:55:00+00:00",
    )

    assert result["public_prediction"] == BASELINE
    assert result["market_informed_prediction"] is None
    assert result["market_usage"] == {
        "status": "unavailable",
        "reason": "no_eligible_market",
        "method_version": METHOD["version"],
        "task_status": {},
    }


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda row: row.update(game_id="espn:wrong"), "market_wrong_game"),
        (lambda row: row.update(home_team_id=1), "market_wrong_teams"),
        (
            lambda row: row.update(captured_at="2026-10-20T18:56:00+00:00"),
            "market_after_cutoff",
        ),
        (
            lambda row: row.update(eligible_prediction_input=False),
            "market_not_eligible",
        ),
    ],
)
def test_market_scope_and_time_are_strict(mutator, message):
    row = copy.deepcopy(MARKET)
    mutator(row)
    with pytest.raises(ValueError, match=message):
        validate_eligible_market(
            row,
            GAME,
            "2026-10-20T18:55:00+00:00",
        )


def test_partial_market_enhances_available_tasks_and_records_missing_anchor():
    row = copy.deepcopy(MARKET)
    row["total"] = None
    result = apply_market_method(
        BASELINE,
        row,
        METHOD,
        game=GAME,
        cutoff="2026-10-20T18:55:00+00:00",
    )

    assert result["public_prediction"]["expected_margin"] != BASELINE["expected_margin"]
    assert result["public_prediction"]["expected_total"] == BASELINE["expected_total"]
    assert result["market_usage"]["task_status"]["expected_total"] == "market_anchor_missing"


def test_source_publication_after_cutoff_is_rejected_when_present():
    row = copy.deepcopy(MARKET)
    row["source_published_at"] = "2026-10-20T18:55:30+00:00"
    with pytest.raises(ValueError, match="market_published_after_cutoff"):
        validate_eligible_market(
            row,
            GAME,
            "2026-10-20T18:55:00+00:00",
        )


def test_current_espn_quote_is_normalized_and_roundtrips_as_latest_legal():
    data = {
        "items": [
            {
                "provider": {"id": "100", "name": "Draft Kings"},
                "homeTeamOdds": {
                    "team": {"$ref": "https://example.test/teams/8"},
                    "current": {
                        "pointSpread": {"american": "-2.5"},
                        "moneyLine": {"american": "-130"},
                    },
                },
                "awayTeamOdds": {
                    "team": {"$ref": "https://example.test/teams/2"},
                    "current": {
                        "pointSpread": {"american": "+2.5"},
                        "moneyLine": {"american": "+110"},
                    },
                },
                "current": {"total": {"american": "226.5"}},
            }
        ]
    }
    event = dict(GAME, event_id="401909088", home_espn_id="8", away_espn_id="2")
    receipt = {
        "status": "success",
        "url": "https://example.test/odds",
        "captured_at": "2026-10-20T18:54:00+00:00",
        "raw_path": "fixture.json",
        "sha256": "abc",
        "http_status": 200,
    }
    quote = normalize_current_market(
        data,
        event,
        receipt,
        "2026-10-20T18:55:00+00:00",
    )
    assert quote["expected_home_margin"] == 2.5
    assert quote["provider_qualification"]["eligible"] is True
    assert quote["total"] == 226.5
    assert quote["home_probability_no_vig"] == pytest.approx(
        (130 / 230) / ((130 / 230) + (100 / 210))
    )
    with tempfile.TemporaryDirectory() as temporary:
        save_live_market_snapshot(quote, root=Path(temporary))
        loaded = latest_legal_market(
            GAME,
            "2026-10-20T18:55:00+00:00",
            root=Path(temporary),
        )
    assert loaded["as_of_cutoff"]["expected_home_margin"] == 2.5
    assert loaded["usage_mode"] == "latest_timestamp_qualified_local_quote"


def _current_market_payload(provider_id, provider_name):
    return {
        "items": [
            {
                "provider": {"id": provider_id, "name": provider_name},
                "homeTeamOdds": {
                    "team": {"$ref": "https://example.test/teams/8"},
                    "current": {
                        "pointSpread": {"american": "-2.5"},
                        "moneyLine": {"american": "-130"},
                    },
                },
                "awayTeamOdds": {
                    "team": {"$ref": "https://example.test/teams/2"},
                    "current": {
                        "pointSpread": {"american": "+2.5"},
                        "moneyLine": {"american": "+110"},
                    },
                },
                "current": {"total": {"american": "226.5"}},
            }
        ]
    }


@pytest.mark.parametrize(
    ("provider_id", "provider_name"),
    [
        ("1001", "accuscore"),
        ("1005", "betegy"),
        ("200", "Draft Kings Live Odds"),
        ("59", "ESPN BET Live Odds"),
        ("999", "Unknown Sportsbook"),
        ("53", "Titanbets"),
        ("100", "accuscore"),
        (None, None),
    ],
)
def test_prediction_services_live_and_unknown_providers_are_not_normalized(
    provider_id, provider_name
):
    event = dict(GAME, event_id="401909088", home_espn_id="8", away_espn_id="2")
    receipt = {
        "status": "success",
        "captured_at": "2026-10-20T18:54:00+00:00",
    }
    assert (
        normalize_current_market(
            _current_market_payload(provider_id, provider_name),
            event,
            receipt,
            "2026-10-20T18:55:00+00:00",
        )
        is None
    )


@pytest.mark.parametrize(
    ("provider_id", "provider_name"),
    [
        ("1001", "accuscore"),
        ("1005", "betegy"),
        ("200", "Draft Kings Live Odds"),
        ("999", "Unknown Sportsbook"),
        ("53", "Titanbets"),
        ("100", "Unknown Sportsbook"),
    ],
)
def test_manual_eligible_flag_cannot_bypass_provider_allowlist(
    provider_id, provider_name
):
    row = copy.deepcopy(MARKET)
    row["provider_id"] = provider_id
    row["provider"] = provider_name
    row["eligible_prediction_input"] = True
    with pytest.raises(ValueError, match="market_provider_not_allowed"):
        validate_eligible_market(row, GAME, "2026-10-20T18:55:00+00:00")


def test_allowed_book_is_selected_when_blocked_provider_is_also_present():
    blocked = _current_market_payload("1001", "accuscore")["items"][0]
    allowed = _current_market_payload("100", "Draft Kings")["items"][0]
    event = dict(GAME, event_id="401909088", home_espn_id="8", away_espn_id="2")
    quote = normalize_current_market(
        {"items": [blocked, allowed]},
        event,
        {"status": "success", "captured_at": "2026-10-20T18:54:00+00:00"},
        "2026-10-20T18:55:00+00:00",
    )
    assert quote["provider_id"] == "100"
    assert quote["provider"] == "Draft Kings"


def test_official_final_publishes_market_informed_prediction_when_quote_is_eligible(tmp_path):
    game = dict(GAME, status="scheduled")
    residual_predictor=ResidualPredictor()
    layers = residual_predictor.predict(
        BASELINE,
        MARKET,
        {name:0.0 for name in BASKETBALL_CONTEXT_FEATURES},
        game=game,
        cutoff="2026-10-20T18:55:00+00:00",
    )
    report = {
        "game": game,
        "snapshot": {
            "historical_replay": False,
            "training_cutoff": "2026-06-14T16:00:00+00:00",
            "cutoff": "2026-10-20T18:55:00+00:00",
            "generated_at": "2026-10-20T18:55:00+00:00",
        },
        "prediction": layers["official_numeric_prediction"],
        "pure_prediction": layers["pure_prediction"],
        "market_benchmark": layers["market_benchmark"],
        "divergence": layers["divergence"],
        "residual_prediction": layers["residual_prediction"],
        "official_numeric_prediction": layers["official_numeric_prediction"],
        "official_market_adjustment": layers["official_market_adjustment"],
        "prediction_method": layers["prediction_method"],
        "prediction_layers": layers,
        "basketball_only_prediction": layers["pure_prediction"],
        "market_informed_prediction": layers["official_numeric_prediction"],
        "market_usage": layers["market_usage"],
        "market": {"as_of_cutoff": MARKET},
        "analysis": {},
        "evidence": [],
        "missing_data": [],
        "sources": [],
        "trace": [],
    }
    refresh_calls = []

    class Agent:
        schedule = [game]

        def refresh_market(self, selected_game, cutoff):
            refresh_calls.append((selected_game["game_id"], str(cutoff)))
            return {"status": "success"}

        def ask(self, *args, **kwargs):
            return report, SimpleNamespace()

    finalizer = OfficialFinalizer(
        Agent(),
        root=tmp_path / "official",
        clock=lambda: "2026-10-20T18:55:00+00:00",
    )
    result = finalizer.finalize(game["game_id"])
    assert refresh_calls
    assert result["public_prediction"] == result["official_numeric_prediction"]
    assert result["prediction_method"] == "market_residual_v1"
    assert result["prediction_layers"]["residual_prediction"]
    assert result["basketball_only_prediction"] == BASELINE
    assert result["market_usage"]["status"] == "used"


def test_official_report_rejects_display_only_eligible_market():
    game = dict(GAME, status="scheduled")
    report = {
        "game": game,
        "snapshot": {
            "historical_replay": False,
            "training_cutoff": "2026-06-14T16:00:00+00:00",
            "cutoff": "2026-10-20T18:55:00+00:00",
            "generated_at": "2026-10-20T18:55:00+00:00",
        },
        "prediction": BASELINE,
        "market": {"as_of_cutoff": MARKET},
        "analysis": {},
        "evidence": [],
        "missing_data": [],
        "sources": [],
        "trace": [],
    }
    with pytest.raises(ValueError, match="eligible_market_not_used"):
        validate_report(report, game, "2026-10-20T18:55:00+00:00")


def test_official_fallback_applies_latest_legal_market_to_last_basketball_snapshot(tmp_path, monkeypatch):
    game = dict(GAME, status="scheduled")
    fallback_report = {
        "game": game,
        "snapshot": {
            "historical_replay": False,
            "training_cutoff": "2026-06-14T16:00:00+00:00",
            "cutoff": "2026-10-20T18:40:00+00:00",
            "generated_at": "2026-10-20T18:40:00+00:00",
        },
        "prediction": BASELINE,
        "pure_prediction": BASELINE,
        "official_numeric_prediction": BASELINE,
        "prediction_method": "pure_basketball_fallback",
        "residual_feature_values": {name:0.0 for name in BASKETBALL_CONTEXT_FEATURES},
        "basketball_only_prediction": BASELINE,
        "market_informed_prediction": None,
        "market_usage": {"status": "unavailable"},
        "market": None,
        "analysis": {},
        "evidence": [],
        "missing_data": [],
        "sources": [],
        "trace": [],
    }
    monkeypatch.setitem(
        OfficialFinalizer.finalize.__globals__,
        "last_legal_snapshot",
        lambda *args, **kwargs: copy.deepcopy(fallback_report),
    )

    class Agent:
        schedule = [game]
        residual_predictor = ResidualPredictor()

        def refresh_market(self, *args, **kwargs):
            return {"status": "success"}

        def market(self, *args, **kwargs):
            return {"as_of_cutoff": MARKET, "historical_reference": None}

        def ask(self, *args, **kwargs):
            raise TimeoutError("fresh basketball data unavailable")

    result = OfficialFinalizer(
        Agent(),
        root=tmp_path / "official",
        clock=lambda: "2026-10-20T18:55:00+00:00",
    ).finalize(game["game_id"])
    assert result["fallback_used"]
    assert result["market_usage"]["status"] == "used"
    assert result["public_prediction"] == result["official_numeric_prediction"]
    assert result["prediction_method"] == "market_residual_v1"
    assert result["basketball_only_prediction"] == BASELINE


def test_formal_prediction_path_no_longer_imports_fixed_weight_blend():
    root=Path(__file__).resolve().parent
    formal=(root/'agent.py').read_text(encoding='utf-8')+(root/'official_final.py').read_text(encoding='utf-8')
    assert 'apply_market_method' not in formal
    assert 'market_method' not in formal
