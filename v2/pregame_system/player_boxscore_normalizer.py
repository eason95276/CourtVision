"""Canonical player-row mapping shared by V2 sample normalization and V1 knowledge."""
from __future__ import annotations

from typing import Any


def normalize_team_player_boxscores(game_id: str, team: dict[str, Any]) -> list[dict[str, Any]]:
    """Map one BoxScoreTraditionalV3 team roster to the existing normalized fields.

    This function intentionally preserves source values, including empty minutes,
    comments, and source position. Consumers decide whether a row is a played
    appearance, a confirmed DNP, or unresolved.
    """
    team_id = int(team["teamId"])
    team_tricode = team["teamTricode"]
    rows: list[dict[str, Any]] = []
    for player in team.get("players") or []:
        stats = player.get("statistics") or {}
        rows.append({
            "game_id": str(game_id),
            "team_id": team_id,
            "team_tricode": team_tricode,
            "person_id": int(player["personId"]),
            "first_name": player["firstName"],
            "family_name": player["familyName"],
            "full_name": f"{player['firstName']} {player['familyName']}",
            "name_i": player["nameI"],
            "position": player["position"],
            "starter": bool(player["position"]),
            "comment": player["comment"],
            "jersey_num": player["jerseyNum"],
            "minutes": stats.get("minutes"),
            "fieldGoalsMade": stats.get("fieldGoalsMade"),
            "fieldGoalsAttempted": stats.get("fieldGoalsAttempted"),
            "fieldGoalsPercentage": stats.get("fieldGoalsPercentage"),
            "threePointersMade": stats.get("threePointersMade"),
            "threePointersAttempted": stats.get("threePointersAttempted"),
            "threePointersPercentage": stats.get("threePointersPercentage"),
            "freeThrowsMade": stats.get("freeThrowsMade"),
            "freeThrowsAttempted": stats.get("freeThrowsAttempted"),
            "freeThrowsPercentage": stats.get("freeThrowsPercentage"),
            "reboundsOffensive": stats.get("reboundsOffensive"),
            "reboundsDefensive": stats.get("reboundsDefensive"),
            "reboundsTotal": stats.get("reboundsTotal"),
            "assists": stats.get("assists"),
            "steals": stats.get("steals"),
            "blocks": stats.get("blocks"),
            "turnovers": stats.get("turnovers"),
            "foulsPersonal": stats.get("foulsPersonal"),
            "points": stats.get("points"),
            "plusMinusPoints": stats.get("plusMinusPoints"),
        })
    return rows
