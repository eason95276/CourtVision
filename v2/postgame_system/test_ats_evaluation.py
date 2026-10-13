from v2.postgame_system.ats_evaluation import aggregate_ats_evaluations


def _row(independent, final, *, market_available=True):
    market = {"status": "available" if market_available else "unavailable",
              "spread_status": "available" if market_available else "unavailable"}
    return {
        "market": market,
        "independent": {"status": "available" if market_available else "unavailable",
            "settlement": {"result": independent}},
        "final": {"status": "available" if market_available else "unavailable",
            "settlement": {"result": final}},
    }


def test_ats_aggregation_keeps_hit_rate_sample_pushes_skip_and_coverage_separate():
    result = aggregate_ats_evaluations([
        _row("win", "loss"),
        _row("push", "win"),
        _row("no_edge", "no_edge"),
        _row("unavailable", "unavailable", market_available=False),
    ])
    assert result["total_games"] == 4
    independent = result["independent"]
    assert (independent["wins"], independent["losses"], independent["pushes"]) == (1, 0, 1)
    assert independent["no_edge"] == 1
    assert independent["unavailable"] == 1
    assert independent["market_unavailable_games"] == 1
    assert independent["eligible_games"] == 3
    assert independent["evaluated_games"] == 2
    assert independent["hit_rate"] == 1.0  # push excluded from wins/(wins+losses)
    assert independent["market_coverage"] == .75
    assert independent["coverage"] == .5
    final = result["final"]
    assert (final["wins"], final["losses"], final["pushes"]) == (1, 1, 0)
    assert final["hit_rate"] == .5
