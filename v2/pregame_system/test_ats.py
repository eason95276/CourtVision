from __future__ import annotations

import pytest

from .ats import build_spread_comparison, settle_ats


GAME = {
    "game_id": "espn:ats-fixture",
    "home": {"id": 1, "name": "勇士", "name_zh": "勇士"},
    "away": {"id": 2, "name": "湖人", "name_zh": "湖人"},
}


def quote(home_spread: float, away_spread: float | None = None):
    return {
        "status": "available",
        "game_identity": {"game_id": GAME["game_id"], "home_team_id": 1, "away_team_id": 2,
                          "home_team": "Warriors", "away_team": "Lakers"},
        "home_spread": home_spread,
        "away_spread": -home_spread if away_spread is None else away_spread,
        "expected_home_margin": -home_spread,
        "sportsbook": "Draft Kings",
        "source_url": "https://example.test/quote",
        "captured_at": "2026-10-07T00:00:00Z",
        "observed_at": "2026-10-07T00:00:00Z",
        "published_at": None,
        "raw_response_sha256": "a" * 64,
    }


def prediction(home_margin: float, side: str | None = None):
    return {"winner_side": side or ("home" if home_margin > 0 else "away"),
            "win_probability": .6, "expected_home_margin": home_margin, "expected_total": 220.0}


@pytest.mark.parametrize(
    "home_margin,home_spread,expected_edge,expected_lean,expected_label",
    [
        (3.5, -4.5, -1.0, "away", "湖人 +4.5"),
        (4.4, -4.5, -0.1, "away", "湖人 +4.5"),
        (4.7, -4.5, 0.2, "home", "勇士 -4.5"),
        (4.6, -4.5, 0.1, "home", "勇士 -4.5"),
        (6.0, -4.5, 1.5, "home", "勇士 -4.5"),
        (-2.5, -4.5, -7.0, "away", "湖人 +4.5"),
        (4.5, -4.5, 0.0, "no_edge", None),
        (1.0, 2.0, 3.0, "home", "勇士 +2"),
        (-1.0, 4.5, 3.5, "home", "勇士 +4.5"),
    ],
)
def test_spread_edge_is_deterministic_in_home_coordinate(
    home_margin, home_spread, expected_edge, expected_lean, expected_label
):
    result = build_spread_comparison(
        GAME, {"public_market_reference": quote(home_spread)},
        prediction(home_margin), prediction(home_margin),
    )
    market_margin = -home_spread
    assert result["market"]["market_implied_home_margin"] == market_margin
    assert result["independent"]["model_home_margin"] == home_margin
    assert result["independent"]["spread_edge"] == expected_edge
    assert result["independent"]["ats_lean"] == expected_lean
    label = (result["independent"].get("ats_lean_reference") or {}).get("display")
    assert label == expected_label
    assert result["final"] == result["independent"]


def test_independent_and_final_are_computed_against_one_immutable_quote():
    result = build_spread_comparison(
        GAME, {"public_market_reference": quote(-4.5)},
        prediction(-2.5), prediction(3.0),
    )
    assert result["market"]["market_quote_id"]
    assert result["independent"]["spread_edge"] == -7.0
    assert result["independent"]["ats_lean"] == "away"
    assert result["independent"]["ats_lean_reference"]["display"] == "湖人 +4.5"
    assert result["final"]["spread_edge"] == -1.5
    assert result["final"]["ats_lean"] == "away"
    assert result["final"]["ats_lean_reference"]["display"] == "湖人 +4.5"
    assert result["independent"]["winner_side"] == "away"
    assert result["final"]["winner_side"] == "home"


def test_market_identity_and_direction_conflicts_fail_closed():
    bad_identity = quote(-4.5)
    bad_identity["game_identity"]["game_id"] = "espn:wrong"
    rejected = build_spread_comparison(GAME, {"public_market_reference": bad_identity},
                                       prediction(3.5), prediction(3.5))
    assert rejected["market"]["reason"] == "spread_market_game_identity_mismatch"
    assert rejected["independent"]["ats_lean"] == "unavailable"
    bad_sign = quote(-4.5)
    bad_sign["expected_home_margin"] = -4.5
    rejected = build_spread_comparison(GAME, {"public_market_reference": bad_sign},
                                       prediction(3.5), prediction(3.5))
    assert rejected["market"]["reason"] == "spread_market_sign_conflict"
    assert rejected["final"]["ats_lean"] == "unavailable"


def test_market_missing_or_no_edge_is_not_forced_into_a_side():
    unavailable = build_spread_comparison(GAME, None, prediction(2.5), prediction(2.5))
    assert unavailable["independent"]["ats_lean"] == "unavailable"
    assert unavailable["independent"]["spread_edge"] is None
    assert settle_ats(unavailable["independent"], 2, unavailable["market"])["result"] == "unavailable"

    no_edge = build_spread_comparison(GAME, {"public_market_reference": quote(-4.5)},
                                      prediction(4.5), prediction(4.5))
    assert no_edge["independent"]["ats_lean"] == "no_edge"
    assert no_edge["independent"]["spread_edge"] == 0.0
    assert no_edge["independent"]["ats_lean_reference"] is None
    assert settle_ats(no_edge["independent"], 2, no_edge["market"])["result"] == "no_edge"

    total_only = {"public_market_reference": {
        "status": "available", "total": 220.5, "sportsbook": "Draft Kings",
        "captured_at": "2026-10-07T00:00:00Z"}}
    comparison = build_spread_comparison(GAME, total_only, prediction(2.5), prediction(2.5))
    assert comparison["market"]["status"] == "available"
    assert comparison["market"]["spread_status"] == "unavailable"
    assert comparison["independent"]["ats_lean"] == "unavailable"


@pytest.mark.parametrize(
    "home_spread,lean,actual,result",
    [
        (-4.5, "home", 5, "win"),
        (-4.5, "home", 3, "loss"),
        (-4.5, "away", 3, "win"),
        (-4.5, "away", 5, "loss"),
        (-4.0, "home", 4, "push"),
    ],
)
def test_ats_settlement_uses_home_handicap_and_separates_lean_from_result(
    home_spread, lean, actual, result
):
    full = build_spread_comparison(
        GAME, {"public_market_reference": quote(home_spread)},
        prediction(6 if lean == "home" else -1), prediction(6 if lean == "home" else -1),
    )
    comparison = full["final"]
    # Use a declared lean for settlement cases independently of the center-value
    # example above; the settlement routine must only settle the frozen pick.
    comparison["ats_lean"] = lean
    comparison["ats_lean_reference"] = {"side": lean, "line": home_spread if lean == "home" else -home_spread}
    assert settle_ats(comparison, actual, full["market"])["result"] == result


def test_winner_and_ats_outcomes_are_independent():
    market = {"public_market_reference": quote(-4.5)}
    # Winner right / ATS wrong.
    full = build_spread_comparison(GAME, market, None, prediction(6))
    correct_winner_wrong_ats = full["final"]
    assert correct_winner_wrong_ats["winner_side"] == "home"
    assert settle_ats(correct_winner_wrong_ats, 3, full["market"])["result"] == "loss"
    # Winner wrong / ATS right.
    full = build_spread_comparison(GAME, market, None, prediction(-2.5))
    wrong_winner_correct_ats = full["final"]
    assert wrong_winner_correct_ats["winner_side"] == "away"
    assert settle_ats(wrong_winner_correct_ats, 3, full["market"])["result"] == "win"
    # Winner right / ATS right.
    full = build_spread_comparison(GAME, market, None, prediction(3))
    both_right = full["final"]
    assert both_right["winner_side"] == "home"
    assert settle_ats(both_right, 3, full["market"])["result"] == "win"
    # Winner wrong / ATS wrong.
    full = build_spread_comparison(GAME, market, None, prediction(-6))
    both_wrong = full["final"]
    assert both_wrong["winner_side"] == "away"
    assert settle_ats(both_wrong, 6, full["market"])["result"] == "loss"
