from __future__ import annotations

import json

from .identity import IdentityRegistry
from .refresh import CachedRawClient, refresh_postgame


def identity(registry):
    return registry.attach_nba_game(
        {
            "game_id": "0022500564",
            "game_date": "2026-01-13",
            "scheduled_tipoff": "2026-01-14T00:30:00+00:00",
            "home_team_id": 1610612748,
            "away_team_id": 1610612756,
            "source": {"kind": "fixture"},
        }
    )


def test_cached_three_game_source_refreshes_full_enhanced_bundle(tmp_path):
    registry = IdentityRegistry(tmp_path / "identities.json")
    game_identity = identity(registry)
    client = CachedRawClient()

    result = refresh_postgame(
        game_identity.nba_game_id,
        registry=registry,
        client=client,
        output_root=tmp_path / "refresh",
    )

    assert result.status == "enhanced_available"
    assert result.components == {
        "result": "available",
        "team_boxscores": "available",
        "player_boxscores": "available",
        "play_by_play": "available",
        "shots": "available",
    }
    bundle = json.loads(result.bundle_path.read_text(encoding="utf-8"))
    assert bundle["game"]["home_points"] == 127
    assert bundle["game"]["away_points"] == 121
    assert len(bundle["team_boxscores"]) == 2
    assert bundle["identity"]["identity_hash"] == game_identity.identity_hash


def test_team_result_publishes_when_enhancement_is_missing(tmp_path):
    registry = IdentityRegistry(tmp_path / "identities.json")
    game_identity = identity(registry)
    cached = CachedRawClient()

    class BasicClient:
        def fetch_boxscore(self, game_id):
            payload, receipt = cached.fetch_boxscore(game_id)
            payload = json.loads(json.dumps(payload))
            payload["boxScoreTraditional"]["awayTeam"]["players"] = []
            payload["boxScoreTraditional"]["homeTeam"]["players"] = []
            return payload, receipt

        def fetch_play_by_play(self, game_id):
            raise RuntimeError("pbp unavailable")

        def fetch_shots(self, game_id, season, season_type):
            raise RuntimeError("shots unavailable")

    result = refresh_postgame(
        game_identity.nba_game_id,
        registry=registry,
        client=BasicClient(),
        output_root=tmp_path / "refresh",
    )

    assert result.status == "basic_available"
    assert result.components["team_boxscores"] == "available"
    assert result.components["play_by_play"] == "failed"
    assert result.components["player_boxscores"] == "missing"
    assert result.components["shots"] == "failed"


def test_repeated_refresh_is_idempotent(tmp_path):
    registry = IdentityRegistry(tmp_path / "identities.json")
    game_identity = identity(registry)
    client = CachedRawClient()
    first = refresh_postgame(
        game_identity.nba_game_id,
        registry=registry,
        client=client,
        output_root=tmp_path / "refresh",
    )
    second = refresh_postgame(
        game_identity.nba_game_id,
        registry=registry,
        client=client,
        output_root=tmp_path / "refresh",
    )

    assert first.bundle_hash == second.bundle_hash
    assert first.bundle_path == second.bundle_path
    assert len(list(first.bundle_path.parent.glob("*.json"))) == 1


def test_wrong_boxscore_identity_is_rejected(tmp_path):
    registry = IdentityRegistry(tmp_path / "identities.json")
    game_identity = identity(registry)
    cached = CachedRawClient()

    class WrongClient(CachedRawClient):
        def fetch_boxscore(self, game_id):
            payload, receipt = super().fetch_boxscore(game_id)
            payload = json.loads(json.dumps(payload))
            payload["boxScoreTraditional"]["homeTeamId"] = 1
            return payload, receipt

    import pytest

    with pytest.raises(ValueError, match="refresh_identity_mismatch"):
        refresh_postgame(
            game_identity.nba_game_id,
            registry=registry,
            client=WrongClient(),
            output_root=tmp_path / "refresh",
        )
