from __future__ import annotations

import copy

from v2.pregame_system.forecasting_pilot import (
    DEFAULT_ENRICHED_PILOT_PACKET_DIR,
    _load_packets,
    packet_content_sha256,
)
from v2.pregame_system.forecasting_workflow import prepare_pilot
from v2.pregame_system.historical_okc_enrichment import (
    PREFLIGHT_PACKET_DIR,
    _build_player_usage_context,
    _build_team_form,
    _contains_forbidden,
    _merge_game_enrichment,
    _official_schedule_match,
    _read_games_csv_schedule,
    _read_target_packets,
    _candidate_url,
    run_historical_enrichment,
)


def test_all_82_schedule_identities_validate_with_known_exceptions_and_cutoffs():
    packets = _read_target_packets(PREFLIGHT_PACKET_DIR)
    rows, _ = _read_games_csv_schedule(packets)
    statuses = {}
    for number in range(1, 83):
        status = _official_schedule_match(number, rows[number])["status"]
        statuses[status] = statuses.get(status, 0) + 1
    assert statuses == {
        "verified_against_official_august_schedule_pdf": 75,
        "verified_by_post_release_update": 3,
        "time_conflict_conservative_cutoff": 4,
    }
    assert rows[1]["scheduled_local_naive"] == "2025-10-21T19:30:00"
    assert rows[82]["scheduled_local_naive"] == "2026-04-12T20:30:00"


def test_candidate_filter_accepts_only_official_same_date_injury_pdfs():
    assert _candidate_url(
        "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-10-23_06PM.pdf",
        "2025-10-23",
    )
    assert not _candidate_url(
        "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-10-24_06PM.pdf",
        "2025-10-23",
    )
    assert not _candidate_url(
        "https://example.com/Injury-Report_2025-10-23_06PM.pdf",
        "2025-10-23",
    )


def test_team_form_excludes_target_day_and_future_rows():
    packet = {"target": {"game_number": 2, "date": "2025-10-23",
                         "home": {"abbr": "okc"}, "away": {"abbr": "ind"}}}
    schedule = {"date": "2025-10-23", "home_team_id": 1610612760,
                "away_team_id": 1610612754}

    def game(game_id, date, team_id, score, opponent_score):
        return {"game_id": game_id, "date": date, "team_id": team_id,
                "teamScore": score, "opponentScore": opponent_score,
                "fieldGoalsAttempted": 80, "freeThrowsAttempted": 20,
                "reboundsOffensive": 10, "turnovers": 10,
                "assists": 20, "fieldGoalsPercentage": 0.48,
                "threePointersPercentage": 0.36, "freeThrowsPercentage": 0.8,
                "reboundsTotal": 44}

    history = {
        1610612760: [game("prior", "2025-10-22", 1610612760, 101, 99),
                     game("target", "2025-10-23", 1610612760, 999, 1),
                     game("future", "2025-10-24", 1610612760, 888, 2)],
        1610612754: [game("prior-away", "2025-10-22", 1610612754, 98, 100),
                     game("target-away", "2025-10-23", 1610612754, 1, 999)],
    }
    form = _build_team_form(packet, schedule, history)
    home = form["regular_season_only_prior_form"]["home"]["last5_regular_across_available_history"]
    away = form["regular_season_only_prior_form"]["away"]["last5_regular_across_available_history"]
    assert home["games"] == away["games"] == 1
    assert home["points_for_per_game"] == 101
    assert away["points_for_per_game"] == 98
    assert "target" not in home["source_game_ids"]
    assert "future" not in home["source_game_ids"]
    assert "target-away" not in away["source_game_ids"]


def test_prior_player_sample_does_not_use_same_day_or_future_boxscores():
    packet = {"target": {"game_number": 3, "date": "2025-10-25",
                         "home": {"abbr": "okc"}, "away": {"abbr": "atl"}}}
    schedule = {"date": "2025-10-25", "home_team_id": 1610612760,
                "away_team_id": 1610612737}
    history = {
        1610612760: [{"game_id": "before", "date": "2025-10-24"},
                     {"game_id": "same", "date": "2025-10-25"},
                     {"game_id": "after", "date": "2025-10-26"}],
        1610612737: [{"game_id": "before", "date": "2025-10-24"},
                     {"game_id": "same", "date": "2025-10-25"}],
    }
    player = {"player_id": "1", "player_name": "Example Player", "starter": True, "minutes": 31.5}
    samples = {
        "before": {"game_id": "before", "date": "2025-10-24", "home": "atl", "away": "okc",
                   "team_ids": {"atl": 1610612737, "okc": 1610612760},
                   "teams": {"atl": [player], "okc": [player]}, "source_sha256": "a" * 64},
        "same": {"game_id": "same", "date": "2025-10-25", "home": "atl", "away": "okc",
                 "team_ids": {"atl": 1610612737, "okc": 1610612760},
                 "teams": {"atl": [player], "okc": [player]}, "source_sha256": "b" * 64},
        "after": {"game_id": "after", "date": "2025-10-26", "home": "atl", "away": "okc",
                  "team_ids": {"atl": 1610612737, "okc": 1610612760},
                  "teams": {"atl": [player], "okc": [player]}, "source_sha256": "c" * 64},
    }
    result = _build_player_usage_context(packet, schedule, history, samples)
    for side in ("home", "away"):
        games = [item["game_id"] for item in result["teams"][side]["prior_game_samples"]]
        assert games == ["before"]
        assert result["teams"][side]["available_sample_games_last5"] == 1
    assert result["status"] == "partial"


def test_existing_enriched_packet_retains_every_pilot_fact_and_still_projects():
    packet = _load_packets(DEFAULT_ENRICHED_PILOT_PACKET_DIR, (1,))[0]
    original = copy.deepcopy(packet)
    all_packets = _read_target_packets(PREFLIGHT_PACKET_DIR)
    rows, _ = _read_games_csv_schedule(all_packets)
    row = rows[1]
    from v2.pregame_system.historical_okc_enrichment import _official_schedule_match
    validation = _official_schedule_match(1, row)
    schedule = dict(row, schedule_validation=validation,
                    scheduled_tipoff_aware="2025-10-21T19:30:00-04:00",
                    cutoff_aware="2025-10-21T19:30:00-04:00",
                    cutoff_local="2025-10-21T19:30:00",
                    cutoff_basis="official_listed_game_time",
                    cutoff_status="available_conservative_historical_cutoff",
                    tipoff_status="available",
                    tipoff_time_semantics="official listed time; actual first possession unverified")
    merged = _merge_game_enrichment(
        packet, schedule, validation,
        injury_result={"status": "preserved_enriched_pilot", "result": None,
                       "rejected_candidates": []},
        team_form={"status": "available"}, team_stats_source={"raw_sha256": "d" * 64,
                       "captured_at": "2026-10-02T00:00:00Z"},
        player_context={"status": "unavailable", "teams": {}}, player_samples={},
        player_capture_time=None, captured_at="2026-10-02T00:00:00Z",
    )
    for field in ("team_state", "richer_team_data", "injuries", "roster_changes", "starters",
                  "rotation_notes", "pregame_news", "coach_player_comments", "important_context"):
        assert merged[field] == original[field]
    assert all(source in merged["sources"] for source in original["sources"])
    basketball, market, _ = prepare_pilot(merged)
    assert basketball["game"]["game_id"] == original["game"]["game_id"]
    assert market["evidence"]
    assert not _contains_forbidden(basketball)
    assert merged["packet_sha256"] == packet_content_sha256(merged)


def test_offline_mocked_enrichment_builds_82_without_deepseek(tmp_path):
    class EmptyHistoricalSearch:
        def search_many(self, payloads):
            return [{"provider": "test-fixture", "results": [], "errors": None}
                    for _ in payloads]

    result = run_historical_enrichment(
        output_dir=tmp_path / "okc-enrichment",
        base_packet_dir=PREFLIGHT_PACKET_DIR,
        search_transport=EmptyHistoricalSearch(),
        fetch_bytes_fn=lambda url: (_ for _ in ()).throw(AssertionError("no URL should be fetched")),
        search_batch_size=8,
    )
    packets = result["packets"]
    assert len(packets) == 82
    assert [int((p.get("target") or p["game"])["game_number"]) for p in packets] == list(range(1, 83))
    assert result["manifest"]["deepseek_called"] is False
    assert result["manifest"]["actual_results_read_for_scoring"] is False
    assert len(result["summary"]["games_still_relying_on_basic_team_stats_only"]) <= 82
    assert all(not _contains_forbidden(packet) for packet in packets)
    assert all(packet.get("packet_sha256") == packet_content_sha256(packet) for packet in packets)
