"""Register verified schedule facts through the existing registry, with generation paused."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from v2.pregame_system.market import events
from v2.pregame_system.prediction_versions import VersionStoreError
from v2.pregame_system.storage import instant


def verified_preseason_games(boards, verification):
    """Match ESPN identities against independently verified NBA schedule entries."""
    days = verification["official_game_days"]
    if len(days) != 2 or len(set(days)) != 2 or days != sorted(days):
        raise ValueError("exactly_two_ordered_official_game_days_required")
    if sorted(boards) != days:
        raise ValueError("schedule_day_coverage_mismatch")
    official = {item["provider_game_id"]: item for item in verification["games"]}
    if len(official) != len(verification["games"]):
        raise ValueError("duplicate_verified_schedule_identity")
    output = []
    for day in days:
        packet = boards[day]
        board, receipt = packet["board"], packet["receipt"]
        if receipt.get("status") != "success" or not receipt.get("sha256"):
            raise ValueError("schedule_source_unverified")
        instant(receipt["captured_at"])
        raw = {str(item["id"]): item for item in board.get("events", [])}
        for row in events(board, receipt):
            event = raw[row["event_id"]]
            expected = official.get(row["game_id"])
            comp = event["competitions"][0]
            if (event.get("season", {}).get("type") != 1 or event["season"].get("year") != verification["season_end_year"]
                    or row["status"] != "pre" or not comp.get("timeValid")):
                raise ValueError("not_verified_future_preseason")
            date = instant(row["scheduled_tipoff"])
            if date.astimezone(ZoneInfo("America/New_York")).date().isoformat() != day:
                raise ValueError("official_game_day_mismatch")
            if (not expected or expected["home_team_id"] != row["home_team_id"]
                    or expected["away_team_id"] != row["away_team_id"]
                    or instant(expected["scheduled_tipoff"]) != date
                    or expected.get("official_game_day") != day):
                raise ValueError("nba_espn_schedule_identity_mismatch")
            if not expected["source_url"].startswith("https://www.nba.com/game/"):
                raise ValueError("official_schedule_source_required")
            output.append({**row, "status": "scheduled", "game_type": "preseason",
                "season_start": verification["season_end_year"] - 1, "timezone": "UTC",
                "scheduled_tipoff": date.isoformat(), "scheduled_tipoff_utc": date.isoformat(),
                "scheduled_tipoff_beijing": date.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
                "official_game_day": day, "official_game_day_timezone": "America/New_York",
                "original_tipoff": event["date"], "provider_time_description": comp["status"]["type"].get("detail"),
                "provider": "espn", "nba_game_id": expected["nba_game_id"],
                "venue": comp.get("venue"), "neutral_site": comp.get("neutralSite"),
                "generation_hold_reason": "scheduler_policy_pending", "registered_game": True,
                "source": {**receipt, "source_type": "espn_scoreboard_schedule",
                    "official_verification": expected, "verification_captured_at": verification["captured_at"]}})
    if {row["game_id"] for row in output} != set(official) or len(output) != len(official):
        raise ValueError("schedule_game_coverage_mismatch")
    return output


def register_verified_schedule(boards, verification, task_store):
    games = verified_preseason_games(boards, verification)  # Validate the whole batch before writing.
    for game in games:
        existing = task_store.registered_game(game["game_id"])
        if existing and (existing["home"]["id"] != game["home_team_id"]
                         or existing["away"]["id"] != game["away_team_id"]
                         or instant(existing["tipoff"]) != instant(game["scheduled_tipoff"])):
            raise VersionStoreError("schedule_changed")
    return [task_store.register_game(game) for game in games]
