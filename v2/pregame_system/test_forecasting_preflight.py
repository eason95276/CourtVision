from __future__ import annotations

from v2.pregame_system.forecasting_preflight import build_preflight
from v2.pregame_system.test_forecasting_pilot import collect_keys, make_rows


def test_offline_preflight_builds_all_82_and_reports_required_completeness():
    result = build_preflight(make_rows(), "a" * 64, enriched_packet_dir=None)

    assert len(result["packets"]) == 82
    assert [row["game_number"] for row in result["completeness"]] == list(range(1, 83))
    assert result["manifest"]["deepseek_called"] is False
    assert result["summary"]["market_reference_available_count"] == 82
    assert result["summary"]["moneyline_available_count"] == 0

    required_fields = {
        "basic_team_stats", "recent_form", "rest", "head_to_head", "injuries",
        "expected_starters", "rotation_information", "pregame_news", "pure",
        "residual", "official", "historical_market_reference", "moneyline",
        "game_identity", "scheduled_tipoff", "game_identity_and_tipoff",
    }
    for row, packet in zip(result["completeness"], result["packets"]):
        assert required_fields.issubset(row["fields"])
        assert row["fields"]["historical_market_reference"] == "available"
        assert row["fields"]["moneyline"] == "unavailable"
        assert row["fields"]["injuries"] == "unavailable"
        assert row["fields"]["expected_starters"] == "unavailable"
        assert row["fields"]["rotation_information"] == "unavailable"
        assert row["fields"]["pregame_news"] == "unavailable"
        assert row["fields"]["pure"] == "unavailable"
        assert row["fields"]["residual"] == "unavailable"
        assert row["fields"]["official"] == "unavailable"
        assert not collect_keys(packet).intersection({
            "score_home", "score_away", "home_score", "away_score",
            "actual_winner", "actual_margin", "actual_total", "ats_result", "ou_result",
        })


def test_preflight_summary_is_consistent_with_per_game_status_counts():
    result = build_preflight(make_rows(), "b" * 64, enriched_packet_dir=None)
    counts = result["summary"]["field_status_counts"]
    for field in result["completeness"][0]["fields"]:
        assert sum(counts[field].values()) == 82
    assert result["summary"]["input_tiers"] == {"historical_dataset_reconstruction": 82}
