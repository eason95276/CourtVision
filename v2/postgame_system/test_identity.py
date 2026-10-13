from __future__ import annotations

import json

import pytest

from .identity import IdentityRegistry


ESPN_GAME = {
    "game_id": "espn:401909088",
    "event_id": "401909088",
    "scheduled_tipoff": "2026-10-20T19:00:00+00:00",
    "home_team_id": 1610612765,
    "away_team_id": 1610612738,
    "source": {"url": "https://example.test/schedule", "captured_at": "2026-09-12T14:15:00+00:00"},
}

NBA_GAME = {
    "game_id": "0022600001",
    "game_date": "2026-10-20",
    "scheduled_tipoff": "2026-10-20T19:00:00+00:00",
    "home_team_id": 1610612765,
    "away_team_id": 1610612738,
    "source": {"kind": "nba_boxscore"},
}


def test_exact_date_and_team_identity_links_espn_and_nba(tmp_path):
    registry = IdentityRegistry(tmp_path / "identities.json")
    scheduled = registry.register_schedule(ESPN_GAME)
    linked = registry.attach_nba_game(NBA_GAME)

    assert linked.canonical_game_id == scheduled.canonical_game_id
    assert linked.espn_game_id == "espn:401909088"
    assert linked.nba_game_id == "0022600001"
    assert registry.resolve("0022600001").canonical_game_id == linked.canonical_game_id
    assert registry.resolve("espn:401909088").nba_game_id == "0022600001"


def test_reversed_home_away_is_rejected(tmp_path):
    registry = IdentityRegistry(tmp_path / "identities.json")
    registry.register_schedule(ESPN_GAME)
    reversed_game = dict(
        NBA_GAME,
        home_team_id=NBA_GAME["away_team_id"],
        away_team_id=NBA_GAME["home_team_id"],
    )
    with pytest.raises(ValueError, match="identity_team_mismatch"):
        registry.attach_nba_game(reversed_game)


def test_date_mismatch_does_not_attach_to_schedule(tmp_path):
    registry = IdentityRegistry(tmp_path / "identities.json")
    scheduled = registry.register_schedule(ESPN_GAME)
    other = dict(NBA_GAME, game_date="2026-10-21")
    registered = registry.attach_nba_game(other)
    assert registered.canonical_game_id != scheduled.canonical_game_id
    assert registry.resolve("espn:401909088").nba_game_id is None


def test_ambiguous_exact_candidates_are_rejected(tmp_path):
    path = tmp_path / "identities.json"
    first = IdentityRegistry(path)
    record = first.register_schedule(ESPN_GAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    duplicate = dict(payload["identities"][0])
    duplicate["canonical_game_id"] = "cv:espn:duplicate"
    duplicate["espn_game_id"] = "espn:duplicate"
    duplicate.pop("identity_hash")
    from .identity import identity_hash

    duplicate["identity_hash"] = identity_hash(duplicate)
    payload["identities"].append(duplicate)
    path.write_text(json.dumps(payload), encoding="utf-8")
    registry = IdentityRegistry(path)
    with pytest.raises(ValueError, match="identity_ambiguous"):
        registry.attach_nba_game(NBA_GAME)
    assert record.nba_game_id is None


def test_tampered_registry_record_is_rejected(tmp_path):
    path = tmp_path / "identities.json"
    registry = IdentityRegistry(path)
    registry.register_schedule(ESPN_GAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["identities"][0]["home_team_id"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="identity_hash_mismatch"):
        IdentityRegistry(path)
