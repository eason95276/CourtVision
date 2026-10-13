from __future__ import annotations

import pytest

from v2.pregame_system.prediction_versions import VersionStoreError
from .current_roster_preflight import prepare_current_roster_preflight
from .generation_input import _bind_roster_identity_preflight, prepare_generation_data
from v2.pregame_system.analyst import _compact_roster_context


GAME = {
    "id": "espn:fixture", "status": "scheduled", "registered_game": True,
    "tipoff": "2026-10-20T23:00:00Z",
    "away": {"id": 1610612747, "name": "Los Angeles Lakers"},
    "home": {"id": 1610612744, "name": "Golden State Warriors"},
}


def receipt():
    teams = {}
    for side in ("away", "home"):
        team_id = GAME[side]["id"]
        teams[str(team_id)] = {
            "team_id": team_id, "roster_basis": "official_team_roster", "count": 2,
            "observed_at": "2026-10-07T03:00:00+08:00", "source_type": "official_team_roster",
            "source_url": f"https://www.nba.com/{side}/roster", "raw_response_sha256": f"hash-{team_id}",
            "parser_version": "roster-parser-v1",
            "players": [{"name": f"Player {team_id} A"}, {"name": f"Player {team_id} B"}],
        }
    return {"status": "passed", "teams": teams}


def test_preflight_refreshes_both_teams_and_records_source_bound_identity():
    seen = []
    result = prepare_current_roster_preflight({"games": [GAME]}, GAME["id"],
        refresher=lambda game: seen.append(game["id"]) or receipt())
    value = result["games"][0]["current_roster_preflight"]
    assert seen == [GAME["id"]]
    assert value["status"] == "ready"
    assert {team["team_id"] for team in value["teams"].values()} == {1610612747, 1610612744}
    assert all(team["raw_response_sha256"] for team in value["teams"].values())
    assert "not game availability" in value["scope"]


def test_missing_team_roster_fails_before_generation_instead_of_using_history():
    invalid = receipt()
    invalid["teams"].pop(str(GAME["home"]["id"]))
    with pytest.raises(VersionStoreError, match="current_roster_preflight_team_identity_mismatch"):
        prepare_current_roster_preflight({"games": [GAME]}, GAME["id"], refresher=lambda _game: invalid)


def test_prepared_generation_roster_must_match_the_preflight_snapshot():
    preflight = prepare_current_roster_preflight({"games": [GAME]}, GAME["id"], refresher=lambda _game: receipt())
    proof = preflight["games"][0]["current_roster_preflight"]
    teams = {}
    for side in ("away", "home"):
        row = proof["teams"][side]
        teams[side] = {"team_id": row["team_id"], "official_team_roster": {
            "status": "available", "team_id": row["team_id"], "observed_at": row["observed_at"],
            "players": [{"name": "A"}, {"name": "B"}],
            "source": {"sha256": row["raw_response_sha256"], "captured_at": row["observed_at"],
                       "source_type": row["source_type"]},
        }}
    bound = _bind_roster_identity_preflight({"teams": teams}, proof, GAME)
    assert bound["current_roster_identity_coverage"]["status"] == "verified"
    assert all(team["current_roster_identity_coverage"]["status"] == "verified" for team in bound["teams"].values())


def test_stale_or_different_snapshot_is_not_silently_substituted():
    preflight = prepare_current_roster_preflight({"games": [GAME]}, GAME["id"], refresher=lambda _game: receipt())
    proof = preflight["games"][0]["current_roster_preflight"]
    wrong = {"teams": {side: {"team_id": GAME[side]["id"], "official_team_roster": {
        "status": "available", "team_id": GAME[side]["id"], "observed_at": "2026-10-06T00:00:00Z",
        "players": [{"name": "old"}], "source": {"sha256": "stale-hash", "captured_at": "2026-10-06T00:00:00Z",
        "source_type": "official_team_roster"}}} for side in ("away", "home")}}
    with pytest.raises(VersionStoreError, match="current_roster_preflight_snapshot_mismatch"):
        _bind_roster_identity_preflight(wrong, proof, GAME)


def test_latest_preflight_snapshot_replaces_old_roster_without_reusing_continuity():
    preflight = prepare_current_roster_preflight({"games": [GAME]}, GAME["id"], refresher=lambda _game: receipt())
    proof = preflight["games"][0]["current_roster_preflight"]
    snapshots = {}
    old_teams = {}
    for side in ("away", "home"):
        row = proof["teams"][side]
        snapshot_roster = {"status": "available", "team_id": row["team_id"], "season_start": 2026,
            "roster_basis": "official_team_roster", "observed_at": row["observed_at"],
            "players": [{"name": f"Fresh {side} Player"}, {"name": f"Fresh {side} Reserve"}],
            "source": {"sha256": row["raw_response_sha256"], "captured_at": row["observed_at"],
                       "source_type": row["source_type"], "url": row["source_url"]}}
        snapshots[side] = {"team_id": row["team_id"], "roster_basis": "official_team_roster",
            "official_team_roster": snapshot_roster, "current_team_roster": None,
            "league_team_assignment": None, "minutes": None}
        old_teams[side] = {"team_id": row["team_id"], "roster_basis": "official_team_roster",
            "official_team_roster": {**snapshot_roster, "players": [{"name": "Old roster sentinel"}],
                "source": {**snapshot_roster["source"], "sha256": "old-hash"}},
            "continuity": {"status": "available", "retained_minutes_ratio": .8}}
    bound = _bind_roster_identity_preflight({"teams": old_teams}, proof, GAME, current_snapshots=snapshots)
    for side in ("away", "home"):
        team = bound["teams"][side]
        assert team["official_team_roster"]["players"][0]["name"] == f"Fresh {side} Player"
        assert team["continuity"]["status"] == "unavailable"
        assert team["continuity"]["reason"] == "current_roster_refreshed_without_matching_minute_snapshot"


def test_existing_report_update_consumes_fresh_roster_but_preserves_numeric_layers():
    data = {"games": [{**GAME, "relationship": {"final": {"expected_margin": 3.0},
            "basketball": {"expected_margin": 2.0}, "market": None},
        "roster_context": {"teams": {side: {"team_id": GAME[side]["id"], "roster_basis": "official_team_roster",
            "official_team_roster": {"status": "available", "team_id": GAME[side]["id"],
                "players": [{"name": "Old roster"}], "source": {"captured_at": "2026-10-01T00:00:00Z",
                    "sha256": "old", "source_type": "official_team_roster"}},
            "continuity": {"status": "available", "retained_minutes_ratio": .9}}
            for side in ("away", "home")}}}]}
    def refreshed(game):
        base = receipt()
        for side in ("away", "home"):
            row = base["teams"][str(game[side]["id"])]
            roster = {"status": "available", "team_id": row["team_id"], "season_start": 2026,
                "roster_basis": "official_team_roster", "observed_at": row["observed_at"],
                "players": [{"name": f"Fresh {side} Player"}, {"name": f"Fresh {side} Reserve"}],
                "source": {"sha256": row["raw_response_sha256"], "captured_at": row["observed_at"],
                    "source_type": row["source_type"], "url": row["source_url"]}}
            row["snapshot"] = {"team_id": row["team_id"], "roster_basis": "official_team_roster",
                "official_team_roster": roster, "current_team_roster": None, "league_team_assignment": None,
                "minutes": None}
        return base
    preflight_data = prepare_current_roster_preflight(data, GAME["id"], refresher=refreshed)
    before = preflight_data["games"][0]["relationship"]
    prepared = prepare_generation_data(preflight_data, GAME["id"], cutoff="2026-10-07T03:30:00Z")["games"][0]
    assert prepared["relationship"] == before
    assert prepared["roster_context"]["teams"]["home"]["official_team_roster"]["players"][0]["name"] == "Fresh home Player"
    assert prepared["roster_context"]["teams"]["home"]["continuity"]["status"] == "unavailable"
    assert prepared["current_roster_preflight"]["status"] == "ready"
    assert "current_roster_snapshots" not in prepared


def test_current_team_identity_and_player_history_coverage_are_separate():
    compact = _compact_roster_context({"teams": {"away": {
        "team_id": GAME["away"]["id"], "official_team_roster": {
            "status": "available", "players": [{"name": "Luka Doncic"}],
            "source": {"captured_at": "2026-10-07T03:00:00+08:00", "source_type": "official_team_roster"},
        },
        "current_roster_identity_coverage": {"status": "verified", "verified_player_count": 1},
        "player_knowledge_history_coverage": {"status": "available", "matched_history_count": 0,
            "meaning": "historical player-data match coverage; does not establish current team membership"},
    }}})
    team = compact["teams"]["away"]
    assert team["current_roster_identity_coverage"]["status"] == "verified"
    assert team["player_knowledge_history_coverage"]["matched_history_count"] == 0
    assert "current team membership" in team["player_knowledge_history_coverage"]["meaning"]


def test_manual_api_service_uses_the_shared_roster_preflight(monkeypatch):
    from . import server
    monkeypatch.setattr(server, "_PREDICTION_VERSION_SERVICE", None)
    service = server._prediction_version_service()
    assert service.pre_generation_preparer is prepare_current_roster_preflight
