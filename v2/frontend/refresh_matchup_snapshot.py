"""Refresh demo matchup/season context from existing local history assets.

This command writes display and Analyst context only. It does not retrain or
replace any numeric model or prediction layer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

from v2.pregame_system import ROOT
from v2.pregame_system.matchup_history import (
    build_matchup_history,
    build_season_form_context,
    season_phase_context,
)
from v2.pregame_system.storage import instant, now
from courtvision.data import load_games, load_team_statistics

from .export_data import DEFAULT_OUTPUT, MATCHUP_SNAPSHOT, VERSIONED_SNAPSHOT, read_json


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".matchup_context_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def refresh(
    games_csv: str | Path,
    team_stats_csv: str | Path,
    *,
    current_snapshot: str | Path = VERSIONED_SNAPSHOT,
    app_data: str | Path = DEFAULT_OUTPUT,
    matchup_snapshot: str | Path = MATCHUP_SNAPSHOT,
    cutoff: str | None = None,
) -> dict:
    games_path = Path(games_csv)
    stats_path = Path(team_stats_csv)
    if not games_path.is_file() or not stats_path.is_file():
        raise FileNotFoundError("Games.csv and TeamStatistics.csv are required to refresh matchup context")
    current_path = Path(current_snapshot)
    app_path = Path(app_data)
    snapshot_path = Path(matchup_snapshot)
    current = read_json(current_path)
    display = read_json(app_path)
    current_ids = [row["id"] for row in current.get("games") or []]
    if current_ids != [row["id"] for row in display.get("games") or []]:
        raise ValueError("matchup_snapshot_game_identity_mismatch")
    cutoff_value = cutoff or now()
    history_games = load_games(games_path)
    valid_keys = {
        (row.game_id, int(team_id))
        for row in history_games.itertuples()
        for team_id in (row.home_team_id, row.away_team_id)
    }
    history_stats = load_team_statistics(
        stats_path,
        eligible_game_ids=set(history_games.game_id),
        eligible_team_keys=valid_keys,
    )
    rows = []
    for index, row in enumerate(current["games"]):
        if row.get("status") != "scheduled" or instant(cutoff_value) >= instant(row["tipoff"]):
            raise ValueError(f"matchup_snapshot_requires_future_scheduled_game:{row['id']}")
        identity = {
            "game_id": row["id"],
            "home_team_id": row["home"]["id"],
            "away_team_id": row["away"]["id"],
            "season_start": (
                int(str(row["tipoff"])[:4])
                if int(str(row["tipoff"])[5:7]) >= 7
                else int(str(row["tipoff"])[:4]) - 1
            ),
        }
        matchup = build_matchup_history(identity, cutoff_value, history_games, history_stats)
        season_form = build_season_form_context(identity, cutoff_value, history_stats)
        teams = ((row.get("roster_context") or {}).get("teams") or {})
        season_phase = season_phase_context(teams.get("home"), teams.get("away"), season_form)
        overlay = {
            "matchup_history": matchup,
            "season_form": season_form,
            "season_phase": season_phase,
        }
        row.update(overlay)
        display["games"][index].update(overlay)
        rows.append({"id": row["id"], **overlay})
    snapshot = {
        "schema_version": "1.1",
        "generated_at": cutoff_value,
        "source_hashes": {
            "Games.csv": hashlib.sha256(games_path.read_bytes()).hexdigest(),
            "TeamStatistics.csv": hashlib.sha256(stats_path.read_bytes()).hexdigest(),
        },
        "games": rows,
    }
    _write_json(snapshot_path, snapshot)
    _write_json(current_path, current)
    _write_json(app_path, display)
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games-csv", type=Path, default=ROOT / "data/raw/Games.csv")
    parser.add_argument("--team-stats-csv", type=Path, default=ROOT / "data/raw/TeamStatistics.csv")
    args = parser.parse_args()
    result = refresh(args.games_csv, args.team_stats_csv)
    print(json.dumps({"generated_at": result["generated_at"], "games": len(result["games"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
