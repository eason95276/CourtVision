from __future__ import annotations

import pandas as pd


def expected_home_win_probability(
    home_rating: float, away_rating: float, home_advantage: float = 100.0
) -> float:
    return 1.0 / (1.0 + 10.0 ** (-(home_rating + home_advantage - away_rating) / 400.0))


def build_pregame_elo(
    games: pd.DataFrame,
    *,
    initial_rating: float = 1500.0,
    home_advantage: float = 100.0,
    k_factor: float = 20.0,
    season_regression: float = 0.25,
) -> pd.DataFrame:
    """Compute pregame Elo values, then update ratings after each result."""
    ratings: dict[int, float] = {}
    last_season: dict[int, int] = {}
    rows: list[dict[str, float | int | str]] = []

    ordered = games.sort_values(["game_date", "game_id"])
    for game in ordered.itertuples(index=False):
        season = int(game.season_start)
        home_id = int(game.home_team_id)
        away_id = int(game.away_team_id)

        for team_id in (home_id, away_id):
            if team_id not in ratings:
                ratings[team_id] = initial_rating
            if team_id in last_season and last_season[team_id] != season:
                ratings[team_id] = (
                    (1.0 - season_regression) * ratings[team_id]
                    + season_regression * initial_rating
                )
            last_season[team_id] = season

        home_elo = ratings[home_id]
        away_elo = ratings[away_id]
        probability = expected_home_win_probability(
            home_elo, away_elo, home_advantage
        )
        rows.append(
            {
                "game_id": game.game_id,
                "elo_home": home_elo,
                "elo_away": away_elo,
                "elo_diff": home_elo - away_elo,
                "elo_home_win_probability": probability,
            }
        )

        outcome = float(game.home_win)
        delta = k_factor * (outcome - probability)
        ratings[home_id] += delta
        ratings[away_id] -= delta

    return pd.DataFrame(rows)
