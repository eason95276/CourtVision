from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from courtvision.config import HISTORY_START_SEASON, MODELING_START_SEASON
from courtvision.data import load_games, load_odds, load_team_statistics
from courtvision.features import build_game_dataset
from courtvision.quality import run_data_quality_checks, write_data_quality_report


def main() -> int:
    raw_dir = PROJECT_ROOT / "data" / "raw"
    games = load_games(
        raw_dir / "Games.csv", min_season_start=HISTORY_START_SEASON
    )
    excluded = games.attrs.get("excluded_invalid_results", [])
    eligible_team_keys = {
        (row.game_id, int(team_id))
        for row in games.itertuples(index=False)
        for team_id in (row.home_team_id, row.away_team_id)
    }
    team_stats = load_team_statistics(
        raw_dir / "TeamStatistics.csv",
        min_season_start=HISTORY_START_SEASON,
        eligible_game_ids=set(games["game_id"]),
        eligible_team_keys=eligible_team_keys,
    )
    odds = load_odds(raw_dir / "nba_2008-2026.csv")
    dataset, feature_columns = build_game_dataset(games, team_stats)
    dataset = dataset[
        dataset["season_start"] >= MODELING_START_SEASON
    ].reset_index(drop=True)
    result = run_data_quality_checks(
        games,
        team_stats,
        dataset,
        feature_columns,
        odds,
        excluded_invalid_results=excluded,
    )
    write_data_quality_report(
        result,
        PROJECT_ROOT / "artifacts" / "data_quality_report.json",
        PROJECT_ROOT / "reports" / "Data_Quality_Report.md",
    )
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
