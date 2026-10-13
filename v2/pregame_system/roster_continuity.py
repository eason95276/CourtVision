"""Standard-library continuity calculation shared by roster and player knowledge."""
from __future__ import annotations

import math


def calculate_continuity(roster: dict | None, minutes: dict | None) -> dict:
    result = {
        "status": "unavailable",
        "retained_minutes_ratio": None,
        "top8_retained_count": None,
        "top8_retained_minutes_ratio": None,
        "top8_count": None,
        "roster_identity_coverage": None,
        "minute_rows_coverage": None,
        "roster_completeness": "unknown",
        "minute_season_completeness": "unknown",
        "newly_listed_players": None,
        "previous_participants_not_listed": None,
        "historical_top8": None,
        "confirmed_starters": None,
        "projected_starters": None,
        "current_rotation": None,
        "confirmed_transactions": None,
        "reason": "current_roster_or_previous_season_minutes_unavailable",
    }
    if roster and roster.get("status") == "available":
        result["roster_identity_coverage"] = (roster.get("coverage") or {}).get("roster_identity_coverage")
        result["roster_completeness"] = (roster.get("coverage") or {}).get(
            "source_roster_completeness", "unknown"
        )
    if minutes and minutes.get("status") == "available":
        result["minute_rows_coverage"] = (minutes.get("coverage") or {}).get("valid_minute_rows_ratio")
        result["minute_season_completeness"] = (minutes.get("coverage") or {}).get(
            "season_totals_completeness", "unknown"
        )
    if not roster or not minutes or any(item.get("status") != "available" for item in (roster, minutes)):
        return result
    if not roster.get("players") or not minutes.get("players"):
        result["reason"] = "empty_roster_or_minutes"
        return result
    if any(player.get("player_id") is None for player in roster["players"]):
        result["reason"] = "unresolved_current_roster_identity"
        return result
    if (
        roster["team_id"] != minutes["team_id"]
        or roster["season_start"] != minutes["season_start"] + 1
        or roster["id_namespace"] != minutes["id_namespace"]
        or minutes.get("per_mode") != "Totals"
    ):
        result["reason"] = "incompatible_team_season_or_player_identity"
        return result

    current = {str(player["player_id"]): player for player in roster["players"]}
    previous = {str(player["player_id"]): player for player in minutes["players"]}
    if len(current) != len(roster["players"]) or len(previous) != len(minutes["players"]):
        result["reason"] = "duplicate_player_identity"
        return result
    if any(not math.isfinite(player["minutes"]) or player["minutes"] < 0 for player in previous.values()):
        result["reason"] = "invalid_minutes"
        return result
    total = sum(player["minutes"] for player in previous.values())
    if not total:
        result["reason"] = "no_positive_minutes_denominator"
        return result

    ordered = sorted(previous.values(), key=lambda player: (-player["minutes"], str(player["player_id"])))[:8]
    top_total = sum(player["minutes"] for player in ordered)
    result.update(
        status="available",
        reason=None,
        retained_minutes_ratio=sum(
            player["minutes"] for player_id, player in previous.items() if player_id in current
        ) / total,
        top8_retained_count=sum(str(player["player_id"]) in current for player in ordered),
        top8_count=len(ordered),
        top8_retained_minutes_ratio=sum(
            player["minutes"] for player in ordered if str(player["player_id"]) in current
        ) / top_total if top_total else None,
        newly_listed_players=[player for player_id, player in current.items() if player_id not in previous],
        previous_participants_not_listed=[player for player_id, player in previous.items() if player_id not in current],
        historical_top8=ordered,
        change_semantics="membership versus previous-season participants; not confirmed transactions",
        rotation_semantics="previous-season team minutes ranking; not a current rotation or starting lineup",
    )
    return result
