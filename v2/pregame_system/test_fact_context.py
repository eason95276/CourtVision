from __future__ import annotations

from copy import deepcopy
import numpy as np
import pandas as pd

from .fact_context import build_fact_context, build_version_fact_view


HOME, AWAY = 1610612748, 1610612761
GAME = {
    "game_id": "espn:401902644", "home_team_id": HOME, "away_team_id": AWAY,
    "season_start": 2026, "scheduled_tipoff": "2026-10-03T23:00:00+00:00",
}
CUTOFF = "2026-10-03T19:31:45.635910+00:00"


def source_history():
    games = []
    stats = []
    types = ["Regular Season"] * 14 + ["PlayIn"]
    away_types = ["Regular Season"] * 8 + ["Playoffs"] * 7
    for side, team_id, competitions, opponent_base in (
        ("heat", HOME, types, 1700000001), ("raptors", AWAY, away_types, 1700000002),
    ):
        for index, competition in enumerate(competitions):
            game_id = f"{side}-historic-{index:02d}"
            date = pd.Timestamp("2026-03-01") + pd.Timedelta(days=index * 3)
            opponent_id = opponent_base + index
            games.append({
                "game_id": game_id, "game_date": date, "season_start": 2025,
                "gameType": competition,
                "home_team_id": team_id, "away_team_id": opponent_id,
            })
            for is_home, row_team, row_opponent in ((True, team_id, opponent_id), (False, opponent_id, team_id)):
                is_feature_team = row_team == team_id
                stats.append({
                    "game_id": game_id, "game_date": date, "season_start": 2025,
                    "team_id": row_team, "opponent_team_id": row_opponent,
                    "home": int(is_home), "win": float((index + int(is_home)) % 2),
                    "teamScore": 105.0 + index + int(is_home),
                    "opponentScore": 99.0 + index - int(is_home),
                    "reboundsTotal": 42.0 + index % 4, "reboundsOffensive": 9.0 + index % 3,
                    "threePointersMade": 10.0 + index % 6, "threePointersAttempted": 30.0,
                    "threePointersPercentage": (0.0 if index == 0 and is_feature_team else
                        (np.nan if team_id == HOME and is_feature_team and index in (1, 2) else 0.30 + index / 100)),
                    "fieldGoalsAttempted": 85.0, "freeThrowsAttempted": 20.0,
                    "freeThrowsPercentage": 0.75, "fieldGoalsPercentage": 0.45,
                    "turnovers": 12.0 + index % 5, "numMinutes": 240.0,
                })
    # Same-day and post-cutoff results are intentionally in the loaded files;
    # the feature cutoff policy must exclude them from this pregame window.
    for game_id, date in ((GAME["game_id"], pd.Timestamp("2026-10-03")),
                          ("after-cutoff", pd.Timestamp("2026-10-04"))):
        games.append({"game_id": game_id, "game_date": date, "season_start": 2026,
                      "gameType": "Regular Season", "home_team_id": HOME, "away_team_id": AWAY})
        for team_id, opponent_id in ((HOME, AWAY), (AWAY, HOME)):
            row = next(r.copy() for r in stats if r["team_id"] == team_id)
            row.update(game_id=game_id, game_date=date, season_start=2026, opponent_team_id=opponent_id)
            stats.append(row)
    return pd.DataFrame(games), pd.DataFrame(stats)


def existing_feature_values(stats):
    from .features import enrich

    enriched = enrich(stats)
    values = {}
    feature_date = pd.Timestamp("2026-10-03")
    for side, team_id in (("home", HOME), ("away", AWAY)):
        prior = enriched[(enriched.team_id == team_id) & (enriched.game_date.dt.normalize() < feature_date)]
        sample = prior.sort_values(["game_date", "game_id"]).tail(15)
        for public, source in (
            ("off_rating", "off_rating"), ("def_rating", "def_rating"),
            ("net_rating", "net_rating"), ("pace", "pace"), ("win_rate", "win_rate"),
            ("three_pct", "three_pct"), ("rebound_share", "rebound_share"),
            ("turnover_rate", "turnover_rate"),
        ):
            metric = pd.to_numeric(sample[source], errors="coerce")
            values[f"{side}_{public}_roll15"] = float(metric.mean()) if metric.count() >= 3 else None
    return values


def test_synthetic_heat_raptors_like_window_metadata_matches_supplied_feature_values():
    games, stats = source_history()
    values = existing_feature_values(stats)
    before_games, before_stats = games.copy(deep=True), stats.copy(deep=True)
    result = build_fact_context(GAME, CUTOFF, games, stats, values)

    assert result["schema_version"] == "pregame-fact-context-v1"
    assert result["game_id"] == GAME["game_id"]
    assert result["feature_cutoff"] == CUTOFF
    assert result["team_form"]["home"]["observed_games"] == 15
    assert result["team_form"]["home"]["season_counts"] == {"2025": 15}
    assert result["team_form"]["home"]["competition_counts"] == {"play_in": 1, "regular_season": 14}
    assert result["team_form"]["away"]["season_counts"] == {"2025": 15}
    assert result["team_form"]["away"]["competition_counts"] == {"playoffs": 7, "regular_season": 8}
    assert result["team_form"]["home"]["game_ids"] == [f"heat-historic-{i:02d}" for i in range(15)]
    assert result["team_form"]["home"]["date_timezone"] == "America/New_York"
    assert result["team_form"]["home"]["metrics"]["off_rating_15"]["validation"] == "matched"
    assert result["team_form"]["home"]["metrics"]["off_rating_15"]["value"] == values["home_off_rating_roll15"]
    pd.testing.assert_frame_equal(games, before_games)
    pd.testing.assert_frame_equal(stats, before_stats)


def test_requested_window_and_metric_valid_n_are_distinct_zero_is_valid():
    games, stats = source_history()
    context = build_fact_context(GAME, CUTOFF, games, stats, existing_feature_values(stats))
    metric = context["team_form"]["home"]["metrics"]["three_pct_15"]
    assert context["team_form"]["home"]["requested_games"] == 15
    assert context["team_form"]["home"]["observed_games"] == 15
    assert metric["valid_games"] == 13
    assert metric["missing_games"] == 2
    assert metric["unit"] == "fraction"
    assert metric["aggregation"] == "mean_of_per_game_values"
    assert metric["value"] == pytest_approx(sum([0.0, *[0.30 + i / 100 for i in range(3, 15)]]) / 13)


def pytest_approx(value):
    import pytest
    return pytest.approx(value)


def test_schedule_requires_cutoff_eligible_source_and_never_turns_missing_into_false():
    games, stats = source_history()
    planned = {
        "game_id": "espn:prior-day", "home_team_id": HOME, "away_team_id": 44,
        "season_start": 2026, "scheduled_tipoff": "2026-10-02T23:00:00+00:00", "status": "scheduled",
        "source": {"url": "https://league.example/schedule", "source_type": "official_schedule",
                   "captured_at": "2026-10-03T18:00:00+00:00"},
    }
    context = build_fact_context(GAME, CUTOFF, games, stats, existing_feature_values(stats), [planned])
    assert context["schedule"]["home"]["status"] == "available"
    assert context["schedule"]["home"]["basis"]["type"] == "planned_schedule"
    assert context["schedule"]["home"]["back_to_back"] is True
    assert context["schedule"]["home"]["source_refs"][0]["game_id"] == planned["game_id"]
    assert context["schedule"]["away"]["back_to_back"] is None

    recorded = deepcopy(planned)
    recorded["status"] = "final"
    recorded_context = build_fact_context(GAME, CUTOFF, games, stats, existing_feature_values(stats), [recorded])
    assert recorded_context["schedule"]["home"]["basis"]["type"] == "recorded_game"
    assert recorded_context["schedule"]["home"]["back_to_back"] is True

    for invalid_status in ("cancelled", "postponed", "rescheduled"):
        invalid = deepcopy(planned)
        invalid["status"] = invalid_status
        invalid_context = build_fact_context(GAME, CUTOFF, games, stats, existing_feature_values(stats), [invalid])
        assert invalid_context["schedule"]["home"]["back_to_back"] is None
        assert invalid_context["schedule"]["home"]["reason"] == "prior_day_schedule_cancelled_or_invalid"
        assert invalid_context["schedule"]["home"]["source_refs"][0]["source_status"] == invalid_status

    unknown = deepcopy(planned)
    unknown["status"] = "status unavailable"
    unknown_context = build_fact_context(GAME, CUTOFF, games, stats, existing_feature_values(stats), [unknown])
    assert unknown_context["schedule"]["home"]["back_to_back"] is None
    assert unknown_context["schedule"]["home"]["reason"] == "prior_day_schedule_status_unverified"

    late = deepcopy(planned)
    late["source"]["captured_at"] = "2026-10-03T20:00:00+00:00"
    late_context = build_fact_context(GAME, CUTOFF, games, stats, existing_feature_values(stats), [late])
    assert late_context["schedule"]["home"]["back_to_back"] is None
    assert late_context["schedule"]["home"]["source_refs"] == []
    assert late_context["schedule"]["home"]["reason"] == "schedule_evidence_after_feature_cutoff"

    no_calendar = build_fact_context(GAME, CUTOFF, games, stats, existing_feature_values(stats), [])
    assert no_calendar["schedule"]["home"]["back_to_back"] is None
    assert no_calendar["schedule"]["home"]["status"] == "unavailable"


def test_mismatched_metadata_is_flagged_without_changing_existing_feature_value():
    games, stats = source_history()
    values = existing_feature_values(stats)
    original = values["home_off_rating_roll15"]
    values["home_off_rating_roll15"] = original + 4
    context = build_fact_context(GAME, CUTOFF, games, stats, values)
    metric = context["team_form"]["home"]["metrics"]["off_rating_15"]
    assert metric["value"] == original + 4
    assert metric["validation"] == "mismatch"
    assert "rolling_value_mismatch:off_rating_15" in context["team_form"]["home"]["reason"]


def test_new_and_legacy_fact_views_are_outer_bound_and_keep_prediction_version_unchanged():
    fact_context = {
        "schema_version": "pregame-fact-context-v1", "game_id": GAME["game_id"],
        "feature_cutoff": CUTOFF, "team_form": {}, "schedule": {},
    }
    new_version = {
        "game_id": GAME["game_id"], "version_id": "version-1", "payload_sha256": "a" * 64,
        "input_cutoff_at": "2026-10-03T19:32:00+00:00",
        "game_identity": {"game_id": GAME["game_id"], "scheduled_tipoff": GAME["scheduled_tipoff"]},
        "input_snapshot": {"id": GAME["game_id"], "tipoff": GAME["scheduled_tipoff"], "fact_context": fact_context,
                           "background": {"schedule": {"home_rest_days": 7}}},
    }
    frozen_copy = deepcopy(new_version)
    view = build_version_fact_view(new_version)
    assert view["origin"] == "new_frozen"
    assert view["source_payload_sha256"] == "a" * 64
    assert view["context"] == fact_context
    assert new_version == frozen_copy

    legacy = deepcopy(new_version)
    legacy["version_id"] = "version-legacy"
    legacy["input_snapshot"].pop("fact_context")
    legacy["input_snapshot"]["background"]["recent_form"] = {
        "home": {"net_rating_15": 2.5}, "away": {"net_rating_15": -1.0},
    }
    legacy["input_snapshot"]["roster_context"] = {"teams": {"home": {"cold_start": {
        "rolling_15_observed_games": 15, "rolling_15_game_ids": [f"old-{i}" for i in range(15)],
        "rolling_15_previous_season_games": 15, "rolling_15_current_season_games": 0,
    }}}}
    legacy_view = build_version_fact_view(legacy)
    assert legacy_view["origin"] == "legacy_frozen"
    assert legacy_view["context"]["team_form"]["home"]["observed_games"] == 15
    assert legacy_view["context"]["team_form"]["home"]["competition_counts"] is None
    assert legacy_view["context"]["team_form"]["home"]["metrics"]["net_rating_15"]["valid_games"] is None
    assert legacy_view["context"]["schedule"]["home"]["back_to_back"] is None


def test_fact_view_rejects_wrong_frozen_game_identity_or_missing_source_hash():
    version = {
        "game_id": GAME["game_id"], "version_id": "version-1", "payload_sha256": "b" * 64,
        "input_snapshot": {"id": "espn:other", "fact_context": {"game_id": GAME["game_id"]}},
    }
    assert build_version_fact_view(version) is None
    version["input_snapshot"]["id"] = GAME["game_id"]
    version.pop("payload_sha256")
    assert build_version_fact_view(version) is None
