from __future__ import annotations

import copy

import pytest

from .briefs import BriefQuery, build_daily_brief, build_series_brief, build_series_brief_from_manifest, build_team_brief, build_topic_brief


def record(game_id, date, away_id, home_id, away_points, home_points, error, market_gap):
    home_win = home_points > away_points
    return {
        "record_hash": f"hash-{game_id}",
        "status": "basic",
        "identity": {"canonical_game_id": f"cv:nba:{game_id}"},
        "game": {
            "game_id": game_id,
            "game_date": date,
            "away_team_id": away_id,
            "home_team_id": home_id,
            "away_team_tricode": f"T{away_id}",
            "home_team_tricode": f"T{home_id}",
            "away_points": away_points,
            "home_points": home_points,
            "actual_home_margin": home_points - away_points,
            "actual_total": home_points + away_points,
            "winner_team_id": home_id if home_win else away_id,
        },
        "prediction_review": {
            "public_prediction": {
                "prediction": {
                    "win_probability": 0.6,
                    "expected_margin": home_points - away_points + error,
                    "expected_total": home_points + away_points + error,
                },
                "errors": {
                    "win_correct": home_win,
                    "absolute_margin_error": abs(error),
                    "absolute_total_error": abs(error),
                },
            },
            "basketball_only": None,
            "market_informed": None,
            "market_actual_comparison": {
                "status": "available",
                "margin": {"market_expected_home_margin": home_points - away_points - market_gap},
                "total": {"market_total": home_points + away_points - market_gap},
            },
        },
        "data_completeness": {"result": "available", "team_boxscores": "available"},
        "missing_data": [],
        "key_factors": [],
    }


RECORDS = [
    record("g1", "2026-01-01", 1, 2, 100, 110, 2, 1),
    record("g2", "2026-01-01", 3, 4, 120, 105, 14, 8),
    record("g3", "2026-01-03", 2, 1, 108, 112, 5, 2),
    record("g4", "2026-01-05", 1, 2, 99, 101, 3, 5),
]


def test_daily_brief_ranks_largest_prediction_error():
    brief = build_daily_brief("2026-01-01", RECORDS)
    assert brief["game_count"] == 2
    assert brief["largest_prediction_errors"][0]["game_id"] == "g2"
    assert brief["model_market_disagreements"][0]["game_id"] == "g2"


def test_team_brief_requires_explicit_scope_and_computes_change():
    brief = build_team_brief(1, RECORDS, start_date="2026-01-01", end_date="2026-01-05")
    assert brief["games"] == 3
    assert brief["wins"] == 1
    assert brief["losses"] == 2
    assert brief["scope"]["start_date"] == "2026-01-01"
    assert brief["trend"]["status"] == "available"
    with pytest.raises(ValueError, match="team_brief_scope_required"):
        build_team_brief(1, RECORDS)


def test_recent_n_team_scope_is_explicit():
    brief = build_team_brief(1, RECORDS, recent_n=2)
    assert brief["games"] == 2
    assert brief["scope"]["recent_n"] == 2


def test_series_brief_uses_actual_complete_game_denominator():
    brief = build_series_brief(
        team_ids=[1, 2],
        ordered_game_ids=["g1", "g3", "g4", "missing"],
        records=RECORDS,
    )
    assert brief["complete_series"] is False
    assert brief["analyzed_games"] == 3
    assert brief["missing_game_ids"] == ["missing"]
    assert all(team["games"] == 3 for team in brief["teams"])


@pytest.mark.parametrize(
    "topic",
    [
        "largest_prediction_error",
        "largest_team_change",
        "largest_model_market_disagreement",
    ],
)
def test_supported_topics_are_deterministic(topic):
    brief = build_topic_brief(topic, RECORDS)
    assert brief["topic"] == topic
    assert brief["items"]


def test_brief_query_rejects_arbitrary_topic():
    with pytest.raises(ValueError):
        BriefQuery(mode="topic", topic="run arbitrary sql")


def test_day4_manifest_runs_through_generic_series_entrypoint():
    brief = build_series_brief_from_manifest("v2/day4/manifest.json")
    assert brief["source_mode"] == "validated_series_manifest"
    assert brief["complete_series"]
    assert brief["analyzed_games"] == 6
    assert {team["team"] for team in brief["teams"]} == {"LAL", "HOU"}
