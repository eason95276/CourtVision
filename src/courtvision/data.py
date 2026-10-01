from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .config import HISTORY_START_SEASON


COMPETITIVE_GAME_TYPES = {
    "Regular Season",
    "Playoffs",
    "Play-in Tournament",
    "NBA Emirates Cup",
    "Emirates NBA Cup",
    "NBA Cup",
}

# NBA franchise IDs are stable across modern relocations. Values match the
# abbreviation convention used by the historical market dataset.
TEAM_ID_TO_ABBR = {
    1610612737: "atl",
    1610612738: "bos",
    1610612739: "cle",
    1610612740: "no",
    1610612741: "chi",
    1610612742: "dal",
    1610612743: "den",
    1610612744: "gs",
    1610612745: "hou",
    1610612746: "lac",
    1610612747: "lal",
    1610612748: "mia",
    1610612749: "mil",
    1610612750: "min",
    1610612751: "bkn",
    1610612752: "ny",
    1610612753: "orl",
    1610612754: "ind",
    1610612755: "phi",
    1610612756: "phx",
    1610612757: "por",
    1610612758: "sac",
    1610612759: "sa",
    1610612760: "okc",
    1610612761: "tor",
    1610612762: "utah",
    1610612763: "mem",
    1610612764: "wsh",
    1610612765: "det",
    1610612766: "cha",
}


def normalize_game_id(value: object) -> str:
    """Restore the leading zeros that spreadsheet-style CSV parsing removes."""
    if pd.isna(value):
        raise ValueError("gameId is missing")
    text = str(value).strip().split(".")[0]
    return text.zfill(10)


def season_start_from_game_id(game_id: str) -> int:
    """Return the modern NBA season start year encoded in an official game ID."""
    normalized = normalize_game_id(game_id)
    year_suffix = int(normalized[3:5])
    # NBA game IDs encode only two year digits. In this historical dataset,
    # suffixes 46..99 refer to 1946..1999; suffixes 00..45 refer to 2000..2045.
    return (1900 if year_suffix >= 46 else 2000) + year_suffix


def load_games(
    path: Path, *, min_season_start: int = HISTORY_START_SEASON
) -> pd.DataFrame:
    columns = [
        "gameId",
        "gameDate",
        "hometeamCity",
        "hometeamName",
        "hometeamId",
        "awayteamCity",
        "awayteamName",
        "awayteamId",
        "homeScore",
        "awayScore",
        "gameType",
    ]
    games = pd.read_csv(path, usecols=columns, low_memory=False)
    games["game_id"] = games["gameId"].map(normalize_game_id)
    games["game_date"] = pd.to_datetime(games["gameDate"], errors="raise")
    games["season_start"] = games["game_id"].map(season_start_from_game_id)
    games = games[
        games["gameType"].isin(COMPETITIVE_GAME_TYPES)
        & (games["season_start"] >= min_season_start)
    ].copy()

    for column in ["hometeamId", "awayteamId", "homeScore", "awayScore"]:
        games[column] = pd.to_numeric(games[column], errors="raise").astype(int)

    invalid_result = (
        (games["homeScore"] <= 0)
        | (games["awayScore"] <= 0)
        | (games["homeScore"] == games["awayScore"])
    )
    excluded_invalid_results = games.loc[
        invalid_result,
        ["game_id", "game_date", "homeScore", "awayScore"],
    ].to_dict("records")
    games = games.loc[~invalid_result].copy()

    games["home_team_id"] = games["hometeamId"]
    games["away_team_id"] = games["awayteamId"]
    games["home_team"] = games["hometeamCity"] + " " + games["hometeamName"]
    games["away_team"] = games["awayteamCity"] + " " + games["awayteamName"]
    games["home_abbr"] = games["home_team_id"].map(TEAM_ID_TO_ABBR)
    games["away_abbr"] = games["away_team_id"].map(TEAM_ID_TO_ABBR)
    games["home_win"] = (games["homeScore"] > games["awayScore"]).astype(int)
    games["point_margin"] = games["homeScore"] - games["awayScore"]
    games["total_points"] = games["homeScore"] + games["awayScore"]
    games["is_playoffs"] = games["gameType"].isin(
        {"Playoffs", "Play-in Tournament"}
    ).astype(int)

    if games["game_id"].duplicated().any():
        raise ValueError("Games.csv contains duplicate game IDs")
    if games[["home_abbr", "away_abbr"]].isna().any().any():
        unknown = games.loc[
            games["home_abbr"].isna() | games["away_abbr"].isna(),
            ["home_team_id", "away_team_id"],
        ].drop_duplicates()
        raise ValueError(f"unmapped modern franchise IDs: {unknown.to_dict('records')}")

    keep = [
        "game_id",
        "game_date",
        "season_start",
        "gameType",
        "home_team_id",
        "away_team_id",
        "home_team",
        "away_team",
        "home_abbr",
        "away_abbr",
        "homeScore",
        "awayScore",
        "home_win",
        "point_margin",
        "total_points",
        "is_playoffs",
    ]
    result = games[keep].sort_values(["game_date", "game_id"]).reset_index(drop=True)
    result.attrs["excluded_invalid_results"] = excluded_invalid_results
    return result


def load_team_statistics(
    path: Path,
    *,
    min_season_start: int = HISTORY_START_SEASON,
    eligible_game_ids: set[str] | None = None,
    eligible_team_keys: set[tuple[str, int]] | None = None,
) -> pd.DataFrame:
    columns = [
        "gameId",
        "gameDate",
        "teamId",
        "opponentTeamId",
        "home",
        "win",
        "teamScore",
        "opponentScore",
        "fieldGoalsAttempted",
        "fieldGoalsMade",
        "fieldGoalsPercentage",
        "threePointersAttempted",
        "threePointersMade",
        "threePointersPercentage",
        "freeThrowsAttempted",
        "freeThrowsMade",
        "freeThrowsPercentage",
        "reboundsOffensive",
        "reboundsTotal",
        "turnovers",
        "numMinutes",
        "gameType",
    ]
    stats = pd.read_csv(path, usecols=columns, low_memory=False)
    stats["game_id"] = stats["gameId"].map(normalize_game_id)
    stats["game_date"] = pd.to_datetime(stats["gameDate"], errors="raise")
    stats["season_start"] = stats["game_id"].map(season_start_from_game_id)
    if eligible_game_ids is None:
        stats = stats[
            stats["gameType"].isin(COMPETITIVE_GAME_TYPES)
            & (stats["season_start"] >= min_season_start)
        ].copy()
    else:
        stats = stats[stats["game_id"].isin(eligible_game_ids)].copy()

    if eligible_team_keys is not None:
        row_keys = zip(stats["game_id"], stats["teamId"], strict=False)
        keep_rows = [
            (game_id, int(team_id)) in eligible_team_keys
            if pd.notna(team_id)
            else False
            for game_id, team_id in row_keys
        ]
        stats = stats.loc[keep_rows].copy()

    numeric_columns = [
        column
        for column in columns
        if column not in {"gameId", "gameDate", "gameType"}
    ]
    for column in numeric_columns:
        stats[column] = pd.to_numeric(stats[column], errors="coerce")

    stats["team_id"] = stats["teamId"].astype("Int64")
    stats["opponent_team_id"] = stats["opponentTeamId"].astype("Int64")
    stats["home"] = stats["home"].astype("Int64")
    stats["win"] = stats["win"].astype(float)

    counts = stats.groupby("game_id").size()
    invalid = counts[counts != 2]
    if not invalid.empty:
        raise ValueError(
            f"expected two team rows per game; invalid game count={len(invalid)}"
        )
    home_counts = stats.groupby("game_id")["home"].sum()
    if not (home_counts == 1).all():
        raise ValueError("each game must have exactly one home team row")
    return stats.sort_values(["game_date", "game_id", "home"]).reset_index(drop=True)


def load_odds(path: Path) -> pd.DataFrame:
    odds = pd.read_csv(path, low_memory=False)
    odds["date"] = pd.to_datetime(odds["date"], errors="raise")
    odds["home"] = odds["home"].astype(str).str.lower().str.strip()
    odds["away"] = odds["away"].astype(str).str.lower().str.strip()
    return odds
