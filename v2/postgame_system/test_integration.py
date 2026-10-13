from __future__ import annotations

from .briefs import build_daily_brief, build_series_brief, build_team_brief, build_topic_brief
from .pipeline import load_records, run_postgame_pipeline
from .refresh import CachedRawClient
from .router import CourtVisionRouter


CASES = [
    ("0022500166", "2025-11-04", 1610612737, 1610612753),
    ("0022500564", "2026-01-13", 1610612748, 1610612756),
    ("0022501099", "2026-03-31", 1610612753, 1610612756),
]


def test_three_existing_games_run_through_unified_pipeline_and_briefs(tmp_path):
    for game_id, day, home, away in CASES:
        result = run_postgame_pipeline(
            nba_game_id=game_id,
            game_date=day,
            home_team_id=home,
            away_team_id=away,
            identity_path=tmp_path / "identities.json",
            output_root=tmp_path / "postgame",
            client=CachedRawClient(),
        )
        assert result.refresh.status == "enhanced_available"
        assert result.postgame_record["prediction_review"]["source_type"] == "historical_replay"
        assert result.learning_record["eligible_for_error_evaluation"]

    records = load_records(tmp_path / "postgame" / "records")
    assert len(records) == 3
    daily = build_daily_brief("2026-01-13", records)
    team = build_team_brief(1610612756, records, recent_n=2)
    topic = build_topic_brief("largest_prediction_error", records)
    assert daily["game_count"] == 1
    assert team["games"] == 2
    assert topic["items"]
    router = CourtVisionRouter(records=records)
    routed = router.ask("复盘 0022500564")
    assert routed["route"] == "postgame"


def test_official_prediction_links_through_espn_and_nba_identity(tmp_path):
    official = {
        "status": "official_pregame_final",
        "game_id": "espn:401810419",
        "fallback_used": False,
        "basketball_only_prediction": {
            "win_probability": 0.45,
            "expected_margin": -1.5,
            "expected_total": 227.0,
        },
        "market_informed_prediction": {
            "win_probability": 0.55,
            "expected_margin": 1.5,
            "expected_total": 233.0,
        },
        "public_prediction": {
            "win_probability": 0.55,
            "expected_margin": 1.5,
            "expected_total": 233.0,
        },
        "market_usage": {"status": "used", "method_version": "fixture"},
        "report": {
            "market": {
                "as_of_cutoff": {
                    "expected_home_margin": 1.5,
                    "total": 233.0,
                }
            }
        },
    }
    result = run_postgame_pipeline(
        nba_game_id="0022500564",
        espn_game_id="espn:401810419",
        game_date="2026-01-13",
        scheduled_tipoff="2026-01-14T00:30:00+00:00",
        home_team_id=1610612748,
        away_team_id=1610612756,
        identity_path=tmp_path / "identities.json",
        output_root=tmp_path / "postgame",
        client=CachedRawClient(),
        official_prediction=official,
    )
    record = result.postgame_record
    assert record["identity"]["espn_game_id"] == "espn:401810419"
    assert record["identity"]["nba_game_id"] == "0022500564"
    assert record["prediction_review"]["source_type"] == "official_pregame_final"
    assert record["prediction_review"]["public_prediction"]["prediction"]["expected_margin"] == 1.5
    assert record["prediction_review"]["basketball_only"]["prediction"]["expected_margin"] == -1.5


def test_generic_series_brief_uses_explicit_order_and_two_teams():
    from .test_briefs import RECORDS

    brief = build_series_brief(
        team_ids=[1, 2], ordered_game_ids=["g1", "g3", "g4"], records=RECORDS
    )
    assert brief["complete_series"]
    assert brief["analyzed_games"] == 3


def test_repeated_pipeline_keeps_record_and_learning_hashes_stable(tmp_path):
    kwargs = {
        "nba_game_id": "0022500564",
        "game_date": "2026-01-13",
        "home_team_id": 1610612748,
        "away_team_id": 1610612756,
        "identity_path": tmp_path / "identities.json",
        "output_root": tmp_path / "postgame",
        "client": CachedRawClient(),
    }
    first = run_postgame_pipeline(**kwargs)
    second = run_postgame_pipeline(**kwargs)
    assert first.postgame_record["record_hash"] == second.postgame_record["record_hash"]
    assert first.learning_record["learning_record_hash"] == second.learning_record["learning_record_hash"]
