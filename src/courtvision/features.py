from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from .elo import build_pregame_elo
from .config import ROLLING_WINDOWS

ROLLING_STATS = (
    "win_rate",
    "points_for",
    "points_against",
    "off_rating",
    "def_rating",
    "net_rating",
    "pace",
    "fg_pct",
    "three_pct",
    "ft_pct",
    "rebound_share",
    "turnover_rate",
)

METADATA_COLUMNS = [
    "game_id",
    "game_date",
    "season_start",
    "game_type",
    "home_team_id",
    "away_team_id",
    "home_team",
    "away_team",
    "home_abbr",
    "away_abbr",
]
TARGET_COLUMNS = ["home_win", "point_margin", "total_points"]


def add_derived_team_stats(stats: pd.DataFrame) -> pd.DataFrame:
    frame = stats.copy()
    own_possessions = (
        frame["fieldGoalsAttempted"]
        - frame["reboundsOffensive"]
        + frame["turnovers"]
        + 0.44 * frame["freeThrowsAttempted"]
    )
    frame["game_possessions"] = own_possessions.groupby(frame["game_id"]).transform(
        "mean"
    )
    valid_possessions = frame["game_possessions"].replace(0, np.nan)
    valid_minutes = frame["numMinutes"].replace(0, np.nan)
    game_rebounds = frame.groupby("game_id")["reboundsTotal"].transform("sum").replace(
        0, np.nan
    )

    frame["win_rate"] = frame["win"].astype(float)
    frame["points_for"] = frame["teamScore"].astype(float)
    frame["points_against"] = frame["opponentScore"].astype(float)
    frame["off_rating"] = 100.0 * frame["teamScore"] / valid_possessions
    frame["def_rating"] = 100.0 * frame["opponentScore"] / valid_possessions
    frame["net_rating"] = frame["off_rating"] - frame["def_rating"]
    frame["pace"] = 240.0 * frame["game_possessions"] / valid_minutes
    frame["fg_pct"] = frame["fieldGoalsPercentage"]
    frame["three_pct"] = frame["threePointersPercentage"]
    frame["ft_pct"] = frame["freeThrowsPercentage"]
    frame["rebound_share"] = frame["reboundsTotal"] / game_rebounds
    frame["turnover_rate"] = frame["turnovers"] / valid_possessions
    return frame


def add_rolling_team_features(
    stats: pd.DataFrame,
    *,
    windows: Iterable[int] = ROLLING_WINDOWS,
    min_periods: int = 3,
) -> pd.DataFrame:
    """Add only pregame features by shifting every team history one game."""
    frame = add_derived_team_stats(stats)
    frame = frame.sort_values(["team_id", "game_date", "game_id"]).reset_index(
        drop=True
    )

    previous_day = frame.groupby("team_id")["game_date"].shift(1).dt.normalize()
    current_day = frame["game_date"].dt.normalize()
    rest_days = (current_day - previous_day).dt.days.astype(float)
    frame["rest_days"] = rest_days.clip(lower=0, upper=7).fillna(7.0)
    frame["back_to_back"] = (frame["rest_days"] <= 1.0).astype(int)
    frame["games_played_season"] = frame.groupby(
        ["team_id", "season_start"]
    ).cumcount()

    grouped = frame.groupby("team_id", sort=False)
    for window in windows:
        for stat in ROLLING_STATS:
            frame[f"{stat}_roll{window}"] = grouped[stat].transform(
                lambda values, w=window: values.shift(1).rolling(
                    window=w, min_periods=min_periods
                ).mean()
            )
    return frame


def _rename_team_side(frame: pd.DataFrame, side: str, columns: list[str]) -> pd.DataFrame:
    renamed = frame[["game_id", "team_id", *columns]].copy()
    return renamed.rename(
        columns={
            "team_id": f"{side}_team_id_from_stats",
            **{column: f"{side}_{column}" for column in columns},
        }
    )


def build_game_dataset(
    games: pd.DataFrame,
    team_stats: pd.DataFrame,
    *,
    windows: Iterable[int] = ROLLING_WINDOWS,
) -> tuple[pd.DataFrame, list[str]]:
    rolling = add_rolling_team_features(team_stats, windows=windows)
    rolling_columns = [
        f"{stat}_roll{window}" for window in windows for stat in ROLLING_STATS
    ]
    team_feature_columns = [
        "rest_days",
        "back_to_back",
        "games_played_season",
        *rolling_columns,
    ]

    home = _rename_team_side(
        rolling.loc[rolling["home"] == 1], "home", team_feature_columns
    )
    away = _rename_team_side(
        rolling.loc[rolling["home"] == 0], "away", team_feature_columns
    )
    team_features = home.merge(away, on="game_id", how="inner", validate="one_to_one")

    base = games.rename(columns={"gameType": "game_type"}).merge(
        team_features, on="game_id", how="inner", validate="one_to_one"
    )
    if not (
        base["home_team_id"] == base["home_team_id_from_stats"].astype(int)
    ).all():
        raise ValueError("home team ID mismatch between Games and TeamStatistics")
    if not (
        base["away_team_id"] == base["away_team_id_from_stats"].astype(int)
    ).all():
        raise ValueError("away team ID mismatch between Games and TeamStatistics")

    for column in team_feature_columns:
        base[f"diff_{column}"] = base[f"home_{column}"] - base[f"away_{column}"]

    elo = build_pregame_elo(games)
    base = base.merge(elo, on="game_id", how="left", validate="one_to_one")

    feature_columns = ["is_playoffs", "elo_home", "elo_away", "elo_diff"]
    for column in team_feature_columns:
        feature_columns.extend([f"home_{column}", f"away_{column}", f"diff_{column}"])

    banned_exact = {
        "homeScore",
        "awayScore",
        "home_win",
        "point_margin",
        "total_points",
        "teamScore",
        "opponentScore",
        "win",
        "plusMinusPoints",
        "seasonWins",
        "seasonLosses",
    }
    leaked = sorted(set(feature_columns) & banned_exact)
    if leaked:
        raise ValueError(f"target or postgame columns leaked into features: {leaked}")

    output_columns = [
        *METADATA_COLUMNS,
        *TARGET_COLUMNS,
        "elo_home_win_probability",
        *feature_columns,
    ]
    dataset = base[output_columns].sort_values(["game_date", "game_id"])
    dataset = dataset.reset_index(drop=True)
    return dataset, feature_columns


def feature_semantics(feature_columns: list[str]) -> pd.DataFrame:
    rows = []
    for feature in feature_columns:
        if feature.startswith("elo_"):
            source = "all completed prior games"
            timing = "pregame Elo stored before updating with the target result"
        elif "roll" in feature:
            window = feature.rsplit("roll", 1)[-1]
            source = f"previous {window} completed team games"
            timing = "team series shift(1), then rolling mean"
        elif "rest_days" in feature:
            source = "target date minus previous completed game date"
            timing = "known before tipoff, capped at 7"
        elif "back_to_back" in feature:
            source = "pregame rest-day calculation"
            timing = "known before tipoff"
        elif "games_played_season" in feature:
            source = "count of prior games in the same season"
            timing = "cumcount before target game"
        elif feature == "is_playoffs":
            source = "official game type"
            timing = "known when the game is scheduled"
        else:
            source = "derived pregame context"
            timing = "known before tipoff"
        rows.append(
            {
                "feature": feature,
                "source_history": source,
                "time_semantics": timing,
                "uses_target_game_result": False,
            }
        )
    return pd.DataFrame(rows)
