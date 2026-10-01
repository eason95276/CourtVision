from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_ROOT = PROJECT_ROOT / "analysis_v1"
DEFAULT_SQLITE_PATH = ANALYSIS_ROOT / "data" / "courtvision_analysis.sqlite"
DEFAULT_DUCKDB_PATH = ANALYSIS_ROOT / "data" / "courtvision_analysis.duckdb"


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE dim_team (
        team_id INTEGER PRIMARY KEY,
        team_name TEXT NOT NULL,
        team_abbr TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE dim_season (
        season_start INTEGER PRIMARY KEY,
        season_label TEXT NOT NULL,
        first_game_date TEXT NOT NULL,
        last_game_date TEXT NOT NULL,
        game_count INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE dim_game (
        game_id TEXT PRIMARY KEY,
        game_date TEXT NOT NULL,
        season_start INTEGER NOT NULL,
        game_type TEXT NOT NULL,
        is_playoffs INTEGER NOT NULL,
        home_team_id INTEGER NOT NULL,
        away_team_id INTEGER NOT NULL,
        home_score INTEGER NOT NULL,
        away_score INTEGER NOT NULL,
        home_win INTEGER NOT NULL,
        point_margin INTEGER NOT NULL,
        total_points INTEGER NOT NULL,
        FOREIGN KEY (season_start) REFERENCES dim_season(season_start),
        FOREIGN KEY (home_team_id) REFERENCES dim_team(team_id),
        FOREIGN KEY (away_team_id) REFERENCES dim_team(team_id)
    )
    """,
    """
    CREATE TABLE fact_team_game (
        game_id TEXT NOT NULL,
        team_id INTEGER NOT NULL,
        opponent_team_id INTEGER NOT NULL,
        season_start INTEGER NOT NULL,
        game_date TEXT NOT NULL,
        is_home INTEGER NOT NULL,
        is_playoffs INTEGER NOT NULL,
        win INTEGER NOT NULL,
        points_for INTEGER NOT NULL,
        points_against INTEGER NOT NULL,
        point_margin INTEGER NOT NULL,
        pregame_elo REAL,
        pregame_rest_days REAL,
        pregame_back_to_back INTEGER,
        pregame_games_played_season REAL,
        pregame_win_rate_roll5 REAL,
        pregame_points_for_roll5 REAL,
        pregame_points_against_roll5 REAL,
        pregame_off_rating_roll5 REAL,
        pregame_def_rating_roll5 REAL,
        pregame_net_rating_roll5 REAL,
        pregame_pace_roll5 REAL,
        pregame_win_rate_roll15 REAL,
        pregame_net_rating_roll15 REAL,
        pregame_pace_roll15 REAL,
        PRIMARY KEY (game_id, team_id),
        FOREIGN KEY (game_id) REFERENCES dim_game(game_id),
        FOREIGN KEY (team_id) REFERENCES dim_team(team_id),
        FOREIGN KEY (opponent_team_id) REFERENCES dim_team(team_id)
    )
    """,
    """
    CREATE TABLE fact_prediction (
        game_id TEXT PRIMARY KEY,
        evaluation_season INTEGER NOT NULL,
        win_probability REAL NOT NULL,
        predicted_home_win INTEGER NOT NULL,
        predicted_margin REAL NOT NULL,
        predicted_total REAL NOT NULL,
        model_scope TEXT NOT NULL,
        prediction_time_status TEXT NOT NULL,
        FOREIGN KEY (game_id) REFERENCES dim_game(game_id)
    )
    """,
    """
    CREATE TABLE fact_prediction_error (
        game_id TEXT PRIMARY KEY,
        win_correct INTEGER NOT NULL,
        win_log_loss REAL NOT NULL,
        margin_absolute_error REAL NOT NULL,
        total_absolute_error REAL NOT NULL,
        maximum_error REAL NOT NULL,
        FOREIGN KEY (game_id) REFERENCES fact_prediction(game_id)
    )
    """,
    """
    CREATE TABLE fact_market_benchmark (
        game_id TEXT PRIMARY KEY,
        market_expected_home_margin REAL,
        market_total REAL,
        market_home_win_probability REAL,
        model_margin_market_divergence REAL,
        model_total_market_divergence REAL,
        model_market_divergence REAL,
        line_capture_time_status TEXT NOT NULL,
        line_identity_status TEXT NOT NULL,
        FOREIGN KEY (game_id) REFERENCES dim_game(game_id)
    )
    """,
    """
    CREATE TABLE fact_data_quality_event (
        quality_event_id INTEGER PRIMARY KEY,
        check_name TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        severity TEXT NOT NULL,
        detail TEXT NOT NULL,
        observed_at_utc TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE fact_model_stability (
        stability_id INTEGER PRIMARY KEY,
        analysis TEXT NOT NULL,
        task TEXT NOT NULL,
        model TEXT NOT NULL,
        window_years INTEGER NOT NULL,
        evaluation_season INTEGER NOT NULL,
        n_train INTEGER,
        n_eval INTEGER NOT NULL,
        log_loss REAL,
        brier_score REAL,
        accuracy REAL,
        ece REAL,
        mae REAL,
        rmse REAL
    )
    """,
]


def normalize_game_id(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(10)


def _records(frame: pd.DataFrame, columns: list[str]) -> Iterable[tuple[Any, ...]]:
    clean = frame[columns].where(pd.notna(frame[columns]), None)
    return clean.itertuples(index=False, name=None)


class Connection:
    def __init__(self, backend: str, path: Path):
        self.backend = backend
        self.path = path
        if backend == "sqlite":
            self.conn = sqlite3.connect(path)
            self.conn.execute("PRAGMA foreign_keys = ON")
        elif backend == "duckdb":
            try:
                import duckdb
            except ImportError as exc:
                raise RuntimeError(
                    "DuckDB is unavailable. Install the official duckdb Python package "
                    "or run with --backend sqlite."
                ) from exc
            self.conn = duckdb.connect(str(path))
        else:
            raise ValueError(f"unsupported backend: {backend}")

    def execute(self, sql: str, parameters: tuple[Any, ...] | None = None):
        return self.conn.execute(sql, parameters or ())

    def executemany(self, sql: str, rows: Iterable[tuple[Any, ...]]):
        return self.conn.executemany(sql, rows)

    def commit(self) -> None:
        if hasattr(self.conn, "commit"):
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def load_source_frames(project_root: Path) -> dict[str, pd.DataFrame]:
    games = pd.read_csv(
        project_root / "data" / "processed" / "basketball_only_games.csv",
        low_memory=False,
    )
    predictions = pd.read_csv(project_root / "artifacts" / "test_predictions.csv")
    market = pd.read_csv(
        project_root / "artifacts" / "test_predictions_with_market.csv",
        low_memory=False,
    )
    stability = pd.read_csv(project_root / "artifacts" / "stability_by_season.csv")
    quality = json.loads(
        (project_root / "artifacts" / "data_quality_report.json").read_text(
            encoding="utf-8"
        )
    )
    for frame in (games, predictions, market):
        frame["game_id"] = normalize_game_id(frame["game_id"])
    return {
        "games": games,
        "predictions": predictions,
        "market": market,
        "stability": stability,
        "quality": quality,
    }


def build_dimensions(games: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    home_teams = games[["home_team_id", "home_team", "home_abbr"]].rename(
        columns={
            "home_team_id": "team_id",
            "home_team": "team_name",
            "home_abbr": "team_abbr",
        }
    )
    away_teams = games[["away_team_id", "away_team", "away_abbr"]].rename(
        columns={
            "away_team_id": "team_id",
            "away_team": "team_name",
            "away_abbr": "team_abbr",
        }
    )
    teams = (
        pd.concat([home_teams, away_teams], ignore_index=True)
        .drop_duplicates("team_id", keep="last")
        .sort_values("team_id")
    )

    dates = pd.to_datetime(games["game_date"], errors="raise")
    season_source = games.assign(_date=dates)
    seasons = (
        season_source.groupby("season_start", as_index=False)
        .agg(
            first_game_date=("_date", "min"),
            last_game_date=("_date", "max"),
            game_count=("game_id", "size"),
        )
        .sort_values("season_start")
    )
    seasons["season_label"] = seasons["season_start"].map(
        lambda year: f"{int(year)}-{str(int(year) + 1)[-2:]}"
    )
    seasons["first_game_date"] = seasons["first_game_date"].dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    seasons["last_game_date"] = seasons["last_game_date"].dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    home_score = ((games["total_points"] + games["point_margin"]) / 2).round().astype(int)
    away_score = games["total_points"] - home_score
    dim_games = pd.DataFrame(
        {
            "game_id": games["game_id"],
            "game_date": pd.to_datetime(games["game_date"]).dt.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "season_start": games["season_start"].astype(int),
            "game_type": games["game_type"],
            "is_playoffs": games["is_playoffs"].astype(int),
            "home_team_id": games["home_team_id"].astype(int),
            "away_team_id": games["away_team_id"].astype(int),
            "home_score": home_score,
            "away_score": away_score,
            "home_win": games["home_win"].astype(int),
            "point_margin": games["point_margin"].astype(int),
            "total_points": games["total_points"].astype(int),
        }
    )
    return teams, seasons, dim_games


def build_team_games(games: pd.DataFrame) -> pd.DataFrame:
    home_score = ((games["total_points"] + games["point_margin"]) / 2).round().astype(int)
    away_score = games["total_points"] - home_score
    common = {
        "game_id": games["game_id"],
        "season_start": games["season_start"].astype(int),
        "game_date": pd.to_datetime(games["game_date"]).dt.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "is_playoffs": games["is_playoffs"].astype(int),
    }
    home = pd.DataFrame(
        {
            **common,
            "team_id": games["home_team_id"].astype(int),
            "opponent_team_id": games["away_team_id"].astype(int),
            "is_home": 1,
            "win": games["home_win"].astype(int),
            "points_for": home_score,
            "points_against": away_score,
            "point_margin": games["point_margin"].astype(int),
            "pregame_elo": games["elo_home"],
            "pregame_rest_days": games["home_rest_days"],
            "pregame_back_to_back": games["home_back_to_back"].astype(int),
            "pregame_games_played_season": games["home_games_played_season"],
            "pregame_win_rate_roll5": games["home_win_rate_roll5"],
            "pregame_points_for_roll5": games["home_points_for_roll5"],
            "pregame_points_against_roll5": games["home_points_against_roll5"],
            "pregame_off_rating_roll5": games["home_off_rating_roll5"],
            "pregame_def_rating_roll5": games["home_def_rating_roll5"],
            "pregame_net_rating_roll5": games["home_net_rating_roll5"],
            "pregame_pace_roll5": games["home_pace_roll5"],
            "pregame_win_rate_roll15": games["home_win_rate_roll15"],
            "pregame_net_rating_roll15": games["home_net_rating_roll15"],
            "pregame_pace_roll15": games["home_pace_roll15"],
        }
    )
    away = pd.DataFrame(
        {
            **common,
            "team_id": games["away_team_id"].astype(int),
            "opponent_team_id": games["home_team_id"].astype(int),
            "is_home": 0,
            "win": (1 - games["home_win"]).astype(int),
            "points_for": away_score,
            "points_against": home_score,
            "point_margin": (-games["point_margin"]).astype(int),
            "pregame_elo": games["elo_away"],
            "pregame_rest_days": games["away_rest_days"],
            "pregame_back_to_back": games["away_back_to_back"].astype(int),
            "pregame_games_played_season": games["away_games_played_season"],
            "pregame_win_rate_roll5": games["away_win_rate_roll5"],
            "pregame_points_for_roll5": games["away_points_for_roll5"],
            "pregame_points_against_roll5": games["away_points_against_roll5"],
            "pregame_off_rating_roll5": games["away_off_rating_roll5"],
            "pregame_def_rating_roll5": games["away_def_rating_roll5"],
            "pregame_net_rating_roll5": games["away_net_rating_roll5"],
            "pregame_pace_roll5": games["away_pace_roll5"],
            "pregame_win_rate_roll15": games["away_win_rate_roll15"],
            "pregame_net_rating_roll15": games["away_net_rating_roll15"],
            "pregame_pace_roll15": games["away_pace_roll15"],
        }
    )
    columns = [
        "game_id",
        "team_id",
        "opponent_team_id",
        "season_start",
        "game_date",
        "is_home",
        "is_playoffs",
        "win",
        "points_for",
        "points_against",
        "point_margin",
        "pregame_elo",
        "pregame_rest_days",
        "pregame_back_to_back",
        "pregame_games_played_season",
        "pregame_win_rate_roll5",
        "pregame_points_for_roll5",
        "pregame_points_against_roll5",
        "pregame_off_rating_roll5",
        "pregame_def_rating_roll5",
        "pregame_net_rating_roll5",
        "pregame_pace_roll5",
        "pregame_win_rate_roll15",
        "pregame_net_rating_roll15",
        "pregame_pace_roll15",
    ]
    return pd.concat([home, away], ignore_index=True)[columns].sort_values(
        ["game_date", "game_id", "is_home"], ascending=[True, True, False]
    )


def build_prediction_frames(
    predictions: pd.DataFrame, market: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    prediction = pd.DataFrame(
        {
            "game_id": predictions["game_id"],
            "evaluation_season": predictions["season_start"].astype(int),
            "win_probability": predictions["win_probability"],
            "predicted_home_win": (predictions["win_probability"] >= 0.5).astype(int),
            "predicted_margin": predictions["predicted_margin"],
            "predicted_total": predictions["predicted_total"],
            "model_scope": "basketball_only_baseline",
            "prediction_time_status": (
                "historical_holdout; no persisted generation/cutoff timestamp"
            ),
        }
    )
    error = pd.DataFrame(
        {
            "game_id": market["game_id"],
            "win_correct": (
                (market["win_probability"] >= 0.5).astype(int)
                == market["home_win"].astype(int)
            ).astype(int),
            "win_log_loss": market["win_individual_log_loss"],
            "margin_absolute_error": market["margin_absolute_error"],
            "total_absolute_error": market["total_absolute_error"],
        }
    )
    error["maximum_error"] = error[
        ["win_log_loss", "margin_absolute_error", "total_absolute_error"]
    ].max(axis=1)
    benchmark = pd.DataFrame(
        {
            "game_id": market["game_id"],
            "market_expected_home_margin": market["market_expected_home_margin"],
            "market_total": market["market_total"],
            "market_home_win_probability": market["market_home_win_probability"],
            "model_margin_market_divergence": market["margin_market_divergence"],
            "model_total_market_divergence": market["total_market_divergence"],
            "model_market_divergence": market["model_market_divergence"],
            "line_capture_time_status": "missing",
            "line_identity_status": "opening_or_closing_unverified",
        }
    )
    return prediction, error, benchmark


def insert_frame(
    connection: Connection, table: str, frame: pd.DataFrame, columns: list[str]
) -> None:
    placeholders = ", ".join(["?"] * len(columns))
    connection.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        _records(frame, columns),
    )


def build_database(backend: str, output_path: Path, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    source = load_source_frames(project_root)
    teams, seasons, games = build_dimensions(source["games"])
    team_games = build_team_games(source["games"])
    predictions, errors, market = build_prediction_frames(
        source["predictions"], source["market"]
    )
    quality = pd.DataFrame(source["quality"]["checks"])
    quality = quality.rename(columns={"name": "check_name"})
    quality.insert(0, "quality_event_id", range(1, len(quality) + 1))
    quality["observed_at_utc"] = source["quality"].get(
        "generated_at_utc", datetime.now(UTC).isoformat()
    )
    stability = source["stability"].copy()
    stability.insert(0, "stability_id", range(1, len(stability) + 1))

    frames = {
        "dim_team": teams,
        "dim_season": seasons,
        "dim_game": games,
        "fact_team_game": team_games,
        "fact_prediction": predictions,
        "fact_prediction_error": errors,
        "fact_market_benchmark": market,
        "fact_data_quality_event": quality,
        "fact_model_stability": stability,
    }
    columns = {name: list(frame.columns) for name, frame in frames.items()}

    connection = Connection(backend, output_path)
    try:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        for table, frame in frames.items():
            insert_frame(connection, table, frame, columns[table])
        connection.commit()
        table_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in frames
        }
        checks = {
            "duplicate_game_primary_keys": int(
                connection.execute(
                    "SELECT COUNT(*) FROM (SELECT game_id FROM dim_game GROUP BY game_id HAVING COUNT(*) > 1) x"
                ).fetchone()[0]
            ),
            "duplicate_team_game_primary_keys": int(
                connection.execute(
                    "SELECT COUNT(*) FROM (SELECT game_id, team_id FROM fact_team_game GROUP BY game_id, team_id HAVING COUNT(*) > 1) x"
                ).fetchone()[0]
            ),
            "prediction_game_join_rate": float(
                connection.execute(
                    "SELECT AVG(CASE WHEN g.game_id IS NOT NULL THEN 1.0 ELSE 0.0 END) FROM fact_prediction p LEFT JOIN dim_game g ON p.game_id = g.game_id"
                ).fetchone()[0]
            ),
            "market_prediction_same_sample_rate": float(
                connection.execute(
                    "SELECT AVG(CASE WHEN p.game_id IS NOT NULL THEN 1.0 ELSE 0.0 END) FROM fact_market_benchmark m LEFT JOIN fact_prediction p ON m.game_id = p.game_id"
                ).fetchone()[0]
            ),
            "invalid_scores": int(
                connection.execute(
                    "SELECT COUNT(*) FROM dim_game WHERE home_score <= 0 OR away_score <= 0 OR home_score = away_score"
                ).fetchone()[0]
            ),
            "failed_quality_errors": int(
                connection.execute(
                    "SELECT COUNT(*) FROM fact_data_quality_event WHERE status = 'failed' AND severity = 'error'"
                ).fetchone()[0]
            ),
            "failed_quality_warnings": int(
                connection.execute(
                    "SELECT COUNT(*) FROM fact_data_quality_event WHERE status = 'failed' AND severity = 'warning'"
                ).fetchone()[0]
            ),
        }
    finally:
        connection.close()

    try:
        recorded_database_path = str(output_path.relative_to(project_root))
    except ValueError:
        recorded_database_path = str(output_path)
    report = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "backend": backend,
        "database_path": recorded_database_path,
        "table_count": len(frames),
        "table_rows": table_counts,
        "quality_checks": checks,
        "source_boundaries": {
            "games": "15,472 historical completed games; actual scores are postgame fields",
            "team_game_pregame_fields": "rolling and Elo fields were computed with target-game leakage guards",
            "predictions": "2025 season historical holdout; no persisted generation or feature-cutoff timestamp",
            "market": "same-game benchmark only; capture time and opening/closing identity are absent",
        },
    }
    try:
        output_path.relative_to(ANALYSIS_ROOT)
        is_formal_analysis_output = True
    except ValueError:
        is_formal_analysis_output = False
    if is_formal_analysis_output:
        report_path = ANALYSIS_ROOT / "outputs" / "database_build_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the CourtVision V1 analysis database")
    parser.add_argument("--backend", choices=("duckdb", "sqlite"), default="duckdb")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = args.output
    if output is None:
        output = DEFAULT_DUCKDB_PATH if args.backend == "duckdb" else DEFAULT_SQLITE_PATH
    report = build_database(args.backend, output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
