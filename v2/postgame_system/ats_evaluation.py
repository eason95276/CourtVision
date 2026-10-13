"""Small, deterministic aggregation for version-bound ATS Postgame results."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def aggregate_ats_evaluations(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate independent and final ATS separately; pushes are not hit-rate wins."""
    rows = [row for row in rows if isinstance(row, Mapping)]
    total_games = len(rows)

    def stage_summary(stage: str) -> dict[str, Any]:
        wins = losses = pushes = no_edge = unavailable = eligible = market_available = 0
        for row in rows:
            market = row.get("market") or {}
            result_row = row.get(stage) or {}
            settlement = result_row.get("settlement") or {}
            market_ok = market.get("spread_status", market.get("status")) == "available"
            if market_ok:
                market_available += 1
            if not market_ok or result_row.get("status") != "available":
                unavailable += 1
                continue
            eligible += 1
            result = settlement.get("result")
            if result == "no_edge":
                no_edge += 1
            elif result == "win":
                wins += 1
            elif result == "loss":
                losses += 1
            elif result == "push":
                pushes += 1
            else:
                unavailable += 1
                eligible -= 1
        evaluated = wins + losses + pushes
        decisions = wins + losses
        return {
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "no_edge": no_edge,
            "unavailable": unavailable,
            "market_unavailable_games": total_games - market_available,
            "market_available_games": market_available,
            "eligible_games": eligible,
            "evaluated_games": evaluated,
            "hit_rate": wins / decisions if decisions else None,
            "market_coverage": market_available / total_games if total_games else None,
            "coverage": evaluated / total_games if total_games else None,
            "eligible_coverage": evaluated / eligible if eligible else None,
            "hit_rate_definition": "wins / (wins + losses); pushes, no_edge and unavailable excluded",
        }

    return {
        "schema_version": "postgame-ats-summary-v1",
        "total_games": total_games,
        "independent": stage_summary("independent"),
        "final": stage_summary("final"),
    }
