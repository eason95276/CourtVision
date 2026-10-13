"""Shared current-team identity preparation before any registered-game forecast."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from v2.pregame_system.prediction_versions import VersionStoreError
from v2.pregame_system.storage import instant


def summarize_roster_receipt(game: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    if receipt.get("status") != "passed":
        raise VersionStoreError("current_roster_identity_preflight_failed")
    teams = receipt.get("teams") or {}
    summary_teams: dict[str, dict[str, Any]] = {}
    for side in ("away", "home"):
        team_id = int(game[side]["id"])
        row = teams.get(str(team_id), teams.get(team_id))
        if not isinstance(row, dict) or int(row.get("team_id", team_id)) != team_id:
            raise VersionStoreError("current_roster_preflight_team_identity_mismatch:" + side)
        players = row.get("players") or []
        names = [str(player.get("name") or "").strip() for player in players if isinstance(player, dict)]
        if not names or len(names) != len(players) or any(not name for name in names):
            raise VersionStoreError("current_roster_preflight_membership_missing:" + side)
        normalized = [" ".join(name.casefold().split()) for name in names]
        if len(set(normalized)) != len(normalized):
            raise VersionStoreError("current_roster_preflight_duplicate_member:" + side)
        observed_at = row.get("observed_at")
        source_url = row.get("source_url")
        source_type = row.get("source_type")
        source_hash = row.get("raw_response_sha256")
        try:
            observed = instant(observed_at)
            tipoff = instant(game.get("tipoff") or game.get("scheduled_tipoff"))
        except (TypeError, ValueError):
            raise VersionStoreError("current_roster_preflight_time_unverified:" + side) from None
        if observed >= tipoff or not source_url or not source_type or not source_hash:
            raise VersionStoreError("current_roster_preflight_provenance_incomplete:" + side)
        summary_teams[side] = {
            "status": "verified",
            "team_id": team_id,
            "roster_basis": row.get("roster_basis"),
            "player_count": len(players),
            "observed_at": observed_at,
            "source_type": source_type,
            "source_url": source_url,
            "raw_response_sha256": source_hash,
            "parser_version": row.get("parser_version") or receipt.get("parser_version"),
        }
    if {value["team_id"] for value in summary_teams.values()} != {
        int(game["home"]["id"]), int(game["away"]["id"])
    }:
        raise VersionStoreError("current_roster_preflight_matchup_mismatch")
    return {
        "schema_version": "current-roster-preflight-v1",
        "status": "ready",
        "game_id": game.get("id"),
        "teams": summary_teams,
        "scope": "verified current team-roster membership only; not game availability or confirmed starters",
    }


def prepare_current_roster_preflight(
    data: dict[str, Any],
    game_id: str,
    *,
    refresher: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Refresh both current rosters and return copied generation input or fail before cutoff creation."""
    result = deepcopy(data)
    game = next((item for item in result.get("games", []) if item.get("id") == game_id), None)
    if game is None:
        raise VersionStoreError("current_roster_preflight_game_missing")
    if game.get("status") != "scheduled":
        raise VersionStoreError("current_roster_preflight_game_not_scheduled")
    if refresher is None:
        from v2.pregame_system.current_roster_input import refresh_matchup_rosters
        refresher = refresh_matchup_rosters
    try:
        receipt = refresher(game)
        preflight = summarize_roster_receipt(game, receipt)
    except VersionStoreError:
        raise
    except Exception as exc:
        raise VersionStoreError("current_roster_identity_preflight_failed:" + type(exc).__name__) from exc
    game["current_roster_preflight"] = preflight
    snapshots = {}
    receipt_teams = receipt.get("teams") or {}
    for side in ("away", "home"):
        team_id = int(game[side]["id"])
        row = receipt_teams.get(str(team_id), receipt_teams.get(team_id)) or {}
        snapshot = row.get("snapshot")
        if isinstance(snapshot, dict):
            current = deepcopy(snapshot)
            # The immutable version needs source hashes and URLs, not temp-file paths.
            for key in ("roster", "official_team_roster", "current_team_roster", "league_team_assignment"):
                value = current.get(key)
                if isinstance(value, dict):
                    source = value.get("source")
                    if isinstance(source, dict):
                        source.pop("raw_path", None)
                    for supporting in value.get("supporting_sources") or []:
                        if isinstance(supporting, dict):
                            supporting.pop("raw_path", None)
            snapshots[side] = current
    if snapshots:
        game["current_roster_snapshots"] = snapshots
    return result
