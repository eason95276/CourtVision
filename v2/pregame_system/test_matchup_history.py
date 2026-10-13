from __future__ import annotations

import pandas as pd

from .matchup_history import build_matchup_history, build_season_form_context, season_phase_context


NYK = 1610612752
PHI = 1610612755


def games_frame():
    rows = [
        ("0042500211", "2026-05-04 20:00", NYK, PHI, 137, 98, 1),
        ("0042500212", "2026-05-06 19:00", NYK, PHI, 108, 102, 1),
        ("0042500213", "2026-05-08 19:00", PHI, NYK, 94, 108, 1),
        ("0042500214", "2026-05-10 15:30", PHI, NYK, 114, 144, 1),
        ("0022600001", "2026-10-22 19:00", NYK, PHI, 100, 99, 0),
    ]
    return pd.DataFrame([
        {
            "game_id": game_id,
            "game_date": pd.Timestamp(date),
            "season_start": 2025 if game_id.startswith("00425") else 2026,
            "home_team_id": home,
            "away_team_id": away,
            "homeScore": home_score,
            "awayScore": away_score,
            "is_playoffs": playoffs,
        }
        for game_id, date, home, away, home_score, away_score, playoffs in rows
    ])


def stats_frame():
    rows = []
    for game_id, nyk_points, phi_points, nyk_three, phi_three in (
        ("0042500211", 137, 98, 18, 10),
        ("0042500212", 108, 102, 13, 11),
        ("0042500213", 108, 94, 15, 9),
        ("0042500214", 144, 114, 20, 12),
    ):
        for team_id, opponent_id, points, threes in (
            (NYK, PHI, nyk_points, nyk_three),
            (PHI, NYK, phi_points, phi_three),
        ):
            rows.append({
                "game_id": game_id,
                "team_id": team_id,
                "opponent_team_id": opponent_id,
                "teamScore": points,
                "fieldGoalsAttempted": 90,
                "fieldGoalsMade": 42,
                "threePointersAttempted": 40,
                "threePointersMade": threes,
                "freeThrowsAttempted": 20,
                "reboundsOffensive": 10 if team_id == NYK else 7,
                "reboundsTotal": 48 if team_id == NYK else 40,
                "turnovers": 10 if team_id == NYK else 15,
            })
    return pd.DataFrame(rows)


def target():
    return {
        "game_id": "espn:401909089",
        "home_team_id": NYK,
        "away_team_id": PHI,
        "season_start": 2026,
    }


def test_playoff_series_is_grouped_and_scored_high_relevance():
    result = build_matchup_history(
        target(),
        "2026-09-20T00:00:00Z",
        games_frame(),
        stats_frame(),
    )
    assert result["meeting_count"] == 4
    assert result["matchup_relevance"] == "high"
    series = result["series_summary"]
    assert series["games"] == 4
    assert series["wins"] == {str(NYK): 4, str(PHI): 0}
    assert series["winner_team_id"] == NYK
    assert series["team_summaries"][str(NYK)]["average_margin"] == 22.25
    assert series["game_totals"] == [235, 210, 202, 258]
    assert series["average_total"] == 226.25
    assert series["game_margins"] == [39, 6, 14, 30]
    assert series["deterministic_factors"]


def test_cutoff_excludes_target_and_future_meetings():
    result = build_matchup_history(
        target(),
        "2026-05-07T00:00:00Z",
        games_frame(),
        stats_frame(),
    )
    assert [row["game_id"] for row in result["meetings"]] == ["0042500211"]


def test_utc_cutoff_is_converted_to_eastern_before_comparing_naive_game_dates():
    result = build_matchup_history(
        target(),
        "2026-05-06T22:00:00Z",  # 18:00 EDT, before the 19:00 Game 2
        games_frame(),
    )
    assert [row["game_id"] for row in result["meetings"]] == ["0042500211"]


def test_same_day_final_score_is_withheld_without_a_completion_timestamp():
    result = build_matchup_history(
        target(),
        "2026-05-10T22:00:00Z",  # 18:00 EDT, after tip but before a provable final time
        games_frame(),
        stats_frame(),
    )
    assert [row["game_id"] for row in result["meetings"]] == [
        "0042500211", "0042500212", "0042500213"
    ]


def test_team_order_does_not_change_meeting_identity():
    reversed_game = {**target(), "home_team_id": PHI, "away_team_id": NYK}
    left = build_matchup_history(target(), "2026-09-20T00:00:00Z", games_frame())
    right = build_matchup_history(reversed_game, "2026-09-20T00:00:00Z", games_frame())
    assert [row["game_id"] for row in left["meetings"]] == [row["game_id"] for row in right["meetings"]]


def test_missing_history_degrades_explicitly():
    result = build_matchup_history(target(), "2026-09-20T00:00:00Z", None)
    assert result == {
        "status": "unavailable",
        "reason": "historical_games_unavailable",
        "numeric_model_usage": False,
    }


def test_incomplete_team_stats_cannot_be_reported_as_full_series_metrics():
    incomplete = stats_frame()
    incomplete = incomplete[~((incomplete["game_id"] == "0042500214") & (incomplete["team_id"] == PHI))]
    result = build_matchup_history(target(), "2026-09-20T00:00:00Z", games_frame(), incomplete)
    series = result["series_summary"]
    assert series["wins"] == {str(NYK): 4, str(PHI): 0}
    assert series["team_stats_status"] == "unavailable"
    assert series["team_summaries"] == {}
    assert series["deterministic_factors"] == []


def test_missing_stat_column_cannot_be_coerced_to_zero():
    incomplete = stats_frame().drop(columns=["reboundsTotal"])
    result = build_matchup_history(target(), "2026-09-20T00:00:00Z", games_frame(), incomplete)
    assert result["series_summary"]["team_stats_status"] == "unavailable"
    assert result["series_summary"]["deterministic_factors"] == []


def test_old_playoff_series_does_not_override_recent_regular_season_meetings():
    frame = games_frame()
    frame.loc[frame["is_playoffs"] == 1, "season_start"] = 2023
    result = build_matchup_history(target(), "2026-10-25T00:00:00Z", frame)
    assert result["matchup_relevance"] == "low"
    assert result["series_summary"] is None
    assert result["most_recent_meeting"]["game_id"] == "0022600001"


def test_season_phase_boundaries():
    def context(games):
        return {"cold_start": {"current_season_games_observed": games}}

    expected = {
        0: "opening_night",
        1: "very_early",
        5: "early_sample",
        10: "comparison_ready",
        15: "current_season",
    }
    for games, phase in expected.items():
        result = season_phase_context(context(games), context(games + 1))
        assert result["phase"] == phase
        assert result["comparison_sample_games"] == games


def test_unavailable_season_counts_do_not_look_like_opening_night():
    result = season_phase_context(
        {"cold_start": {"status": "unavailable"}},
        {"cold_start": {"status": "available", "current_season_games_observed": 10}},
    )
    assert result["phase"] == "unavailable"


def test_current_season_team_rows_take_priority_over_stale_roster_window_counts():
    stale = {"cold_start": {"status": "available", "current_season_games_observed": 0}}
    form = {
        "status": "available",
        "teams": {
            "home": {"current_season_games": 11},
            "away": {"current_season_games": 12},
        },
    }
    result = season_phase_context(stale, stale, form)
    assert result["phase"] == "comparison_ready"
    assert result["comparison_sample_games"] == 11


def test_season_form_uses_exact_current_and_previous_windows():
    rows = []
    for season, count in ((2025, 12), (2026, 11)):
        for index in range(count):
            for team_id in (NYK, PHI):
                points = 100 + index + (10 if team_id == NYK else 0)
                rows.append({
                    "game_id": f"{season}{index:05d}",
                    "game_date": pd.Timestamp(f"{season}-11-01") + pd.Timedelta(days=index),
                    "season_start": season,
                    "team_id": team_id,
                    "teamScore": points,
                    "opponentScore": 95,
                    "threePointersMade": 12,
                    "threePointersAttempted": 30,
                    "reboundsTotal": 42,
                    "turnovers": 11,
                    "win": int(points > 95),
                })
    result = build_season_form_context(target(), "2026-11-20T20:00:00Z", pd.DataFrame(rows))
    home = result["teams"]["home"]
    assert home["current_last10"]["games"] == 10
    assert home["previous_last10"]["games"] == 10
    assert home["current_last10"]["three_pct"] == 0.4
    assert home["current_last10"]["game_ids"][-1] == "0202600010"
    assert home["previous_last10"]["game_ids"][-1] == "0202500011"
    assert result["numeric_model_usage"] is False


def test_season_form_respects_eastern_cutoff_and_missing_assets():
    row = pd.DataFrame([{
        "game_id": "fixture",
        "game_date": pd.Timestamp("2026-11-06 19:00:00"),
        "season_start": 2026,
        "team_id": NYK,
        "teamScore": 110,
        "opponentScore": 100,
        "win": 1,
    }])
    before = build_season_form_context(target(), "2026-11-06T22:00:00Z", row)
    still_same_game_day = build_season_form_context(target(), "2026-11-07T01:00:00Z", row)
    after = build_season_form_context(target(), "2026-11-07T17:00:00Z", row)
    assert before["teams"]["home"]["current_last10"]["games"] == 0
    assert still_same_game_day["teams"]["home"]["current_last10"]["games"] == 0
    assert after["teams"]["home"]["current_last10"]["games"] == 1
    assert build_season_form_context(target(), "2026-11-06T22:00:00Z", None)["status"] == "unavailable"


def test_previous_title_requires_four_pre_cutoff_finals_wins():
    rows = pd.DataFrame([{
        "game_id": f"004250040{index + 1}",
        "game_date": pd.Timestamp("2026-06-01 20:00") + pd.Timedelta(days=index * 2),
        "season_start": 2025, "team_id": NYK, "teamScore": 110, "opponentScore": 100, "win": 1,
    } for index in range(4)])
    early = build_season_form_context(target(), "2026-06-06T00:00:00Z", rows)
    complete = build_season_form_context(target(), "2026-09-27T00:00:00Z", rows)
    assert early["teams"]["home"]["previous_playoffs"]["championship_clinched"] is None
    champion = complete["teams"]["home"]["previous_playoffs"]
    assert champion["championship_clinched"] is True
    assert champion["finals_wins"] == 4
    assert champion["observed_wins"] == 4
    assert complete["teams"]["away"]["previous_playoffs"]["championship_clinched"] is None
