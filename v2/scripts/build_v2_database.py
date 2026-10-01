"""Build the CourtVision V2 SQLite database from normalized JSON files."""

import json
import os
import sqlite3
from pathlib import Path


V2_DIR = Path(__file__).resolve().parents[1]
NORMALIZED_DIR = V2_DIR / "data" / "normalized"
SCHEMA_PATH = V2_DIR / "sql" / "01_schema.sql"
DATABASE_PATH = V2_DIR / "db" / "courtvision_v2.sqlite"
TEMP_DATABASE_PATH = V2_DIR / "db" / "courtvision_v2.sqlite.tmp"

SOURCE_FILES = {
    "games": "games.json",
    "team_boxscores": "team_boxscores.json",
    "player_boxscores": "player_boxscores.json",
    "play_by_play": "play_by_play.json",
    "shots": "shots.json",
}

EXPECTED_FIELDS = {
    "games": {
        "phase", "game_id", "game_date", "season", "season_type",
        "away_team_id", "away_team_tricode", "away_points",
        "home_team_id", "home_team_tricode", "home_points",
    },
    "team_boxscores": {
        "game_id", "side", "team_id", "team_city", "team_name",
        "team_tricode", "minutes", "fieldGoalsMade",
        "fieldGoalsAttempted", "fieldGoalsPercentage", "threePointersMade",
        "threePointersAttempted", "threePointersPercentage",
        "freeThrowsMade", "freeThrowsAttempted", "freeThrowsPercentage",
        "reboundsOffensive", "reboundsDefensive", "reboundsTotal",
        "assists", "steals", "blocks", "turnovers", "foulsPersonal",
        "points", "plusMinusPoints",
    },
    "player_boxscores": {
        "game_id", "team_id", "team_tricode", "person_id", "first_name",
        "family_name", "full_name", "name_i", "position", "starter",
        "comment", "jersey_num", "minutes", "fieldGoalsMade",
        "fieldGoalsAttempted", "fieldGoalsPercentage", "threePointersMade",
        "threePointersAttempted", "threePointersPercentage",
        "freeThrowsMade", "freeThrowsAttempted", "freeThrowsPercentage",
        "reboundsOffensive", "reboundsDefensive", "reboundsTotal",
        "assists", "steals", "blocks", "turnovers", "foulsPersonal",
        "points", "plusMinusPoints",
    },
    "play_by_play": {
        "game_id", "actionNumber", "clock", "period", "teamId",
        "teamTricode", "personId", "playerName", "playerNameI", "xLegacy",
        "yLegacy", "shotDistance", "shotResult", "isFieldGoal", "scoreHome",
        "scoreAway", "pointsTotal", "location", "description", "actionType",
        "subType", "videoAvailable", "shotValue", "actionId",
    },
    "shots": {
        "grid_type", "game_id", "game_event_id", "player_id", "player_name",
        "team_id", "team_name", "period", "minutes_remaining",
        "seconds_remaining", "event_type", "action_type", "shot_type",
        "shot_zone_basic", "shot_zone_area", "shot_zone_range",
        "shot_distance", "loc_x", "loc_y", "shot_attempted_flag",
        "shot_made_flag", "game_date", "htm", "vtm",
    },
}


def load_sources():
    sources = {}
    for table_name, file_name in SOURCE_FILES.items():
        path = NORMALIZED_DIR / file_name
        with path.open("r", encoding="utf-8") as source_file:
            rows = json.load(source_file)
        if not isinstance(rows, list):
            raise ValueError(f"{path} must contain a JSON array")
        expected = EXPECTED_FIELDS[table_name]
        for row_number, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise ValueError(f"{path} row {row_number} must be an object")
            actual = set(row)
            if actual != expected:
                missing = sorted(expected - actual)
                extra = sorted(actual - expected)
                raise ValueError(
                    f"{path} row {row_number} field mismatch; "
                    f"missing={missing}, extra={extra}"
                )
        sources[table_name] = rows
    return sources


def nullable_text(value):
    return None if value == "" else value


def nullable_id(value):
    return None if value in (0, "", None) else int(value)


def nullable_score(value):
    return None if value in ("", None) else int(value)


def insert_games(connection, rows):
    connection.executemany(
        """
        INSERT INTO games (
            phase, game_id, game_date, season, season_type,
            away_team_id, away_team_tricode, away_points,
            home_team_id, home_team_tricode, home_points
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["phase"], row["game_id"], row["game_date"], row["season"],
                row["season_type"], row["away_team_id"],
                row["away_team_tricode"], row["away_points"],
                row["home_team_id"], row["home_team_tricode"],
                row["home_points"],
            )
            for row in rows
        ],
    )


def team_statistics(row):
    return (
        row["fieldGoalsMade"], row["fieldGoalsAttempted"],
        row["fieldGoalsPercentage"], row["threePointersMade"],
        row["threePointersAttempted"], row["threePointersPercentage"],
        row["freeThrowsMade"], row["freeThrowsAttempted"],
        row["freeThrowsPercentage"], row["reboundsOffensive"],
        row["reboundsDefensive"], row["reboundsTotal"], row["assists"],
        row["steals"], row["blocks"], row["turnovers"],
        row["foulsPersonal"], row["points"], row["plusMinusPoints"],
    )


def insert_team_boxscores(connection, rows):
    connection.executemany(
        """
        INSERT INTO team_boxscores (
            game_id, side, team_id, team_city, team_name, team_tricode, minutes,
            field_goals_made, field_goals_attempted, field_goals_percentage,
            three_pointers_made, three_pointers_attempted,
            three_pointers_percentage, free_throws_made, free_throws_attempted,
            free_throws_percentage, rebounds_offensive, rebounds_defensive,
            rebounds_total, assists, steals, blocks, turnovers, fouls_personal,
            points, plus_minus_points
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?
        )
        """,
        [
            (
                row["game_id"], row["side"], row["team_id"], row["team_city"],
                row["team_name"], row["team_tricode"], row["minutes"],
                *team_statistics(row),
            )
            for row in rows
        ],
    )


def insert_player_boxscores(connection, rows):
    connection.executemany(
        """
        INSERT INTO player_boxscores (
            game_id, team_id, team_tricode, person_id, first_name, family_name,
            full_name, name_i, position, starter, comment, jersey_num, minutes,
            field_goals_made, field_goals_attempted, field_goals_percentage,
            three_pointers_made, three_pointers_attempted,
            three_pointers_percentage, free_throws_made, free_throws_attempted,
            free_throws_percentage, rebounds_offensive, rebounds_defensive,
            rebounds_total, assists, steals, blocks, turnovers, fouls_personal,
            points, plus_minus_points
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        [
            (
                row["game_id"], row["team_id"], row["team_tricode"],
                row["person_id"], row["first_name"], row["family_name"],
                row["full_name"], row["name_i"], nullable_text(row["position"]),
                int(row["starter"]), nullable_text(row["comment"]),
                nullable_text(row["jersey_num"]), nullable_text(row["minutes"]),
                *team_statistics(row),
            )
            for row in rows
        ],
    )


def insert_play_by_play(connection, rows):
    connection.executemany(
        """
        INSERT INTO play_by_play (
            game_id, action_id, action_number, clock, period, team_id,
            team_tricode, person_id, player_name, player_name_i, x_legacy,
            y_legacy, shot_distance, shot_result, is_field_goal, score_home,
            score_away, points_total, location, description, action_type,
            sub_type, video_available, shot_value
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?
        )
        """,
        [
            (
                row["game_id"], row["actionId"], row["actionNumber"],
                row["clock"], row["period"], nullable_id(row["teamId"]),
                nullable_text(row["teamTricode"]), nullable_id(row["personId"]),
                nullable_text(row["playerName"]),
                nullable_text(row["playerNameI"]), row["xLegacy"],
                row["yLegacy"], row["shotDistance"],
                nullable_text(row["shotResult"]), row["isFieldGoal"],
                nullable_score(row["scoreHome"]),
                nullable_score(row["scoreAway"]), row["pointsTotal"],
                nullable_text(row["location"]), row["description"],
                nullable_text(row["actionType"]),
                nullable_text(row["subType"]), row["videoAvailable"],
                row["shotValue"],
            )
            for row in rows
        ],
    )


def insert_shots(connection, rows):
    connection.executemany(
        """
        INSERT INTO shots (
            grid_type, game_id, game_event_id, player_id, player_name, team_id,
            team_name, period, minutes_remaining, seconds_remaining, event_type,
            action_type, shot_type, shot_zone_basic, shot_zone_area,
            shot_zone_range, shot_distance, loc_x, loc_y, shot_attempted_flag,
            shot_made_flag, game_date, htm, vtm
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?
        )
        """,
        [
            tuple(row[column] for column in (
                "grid_type", "game_id", "game_event_id", "player_id",
                "player_name", "team_id", "team_name", "period",
                "minutes_remaining", "seconds_remaining", "event_type",
                "action_type", "shot_type", "shot_zone_basic",
                "shot_zone_area", "shot_zone_range", "shot_distance", "loc_x",
                "loc_y", "shot_attempted_flag", "shot_made_flag", "game_date",
                "htm", "vtm",
            ))
            for row in rows
        ],
    )


def build_database():
    sources = load_sources()
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TEMP_DATABASE_PATH.exists():
        TEMP_DATABASE_PATH.unlink()

    connection = None
    try:
        connection = sqlite3.connect(TEMP_DATABASE_PATH)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            if enabled != 1:
                raise RuntimeError("SQLite foreign key enforcement is not enabled")

            schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
            connection.executescript(schema_sql)
            connection.execute("BEGIN")
            insert_games(connection, sources["games"])
            insert_team_boxscores(connection, sources["team_boxscores"])
            insert_player_boxscores(connection, sources["player_boxscores"])
            insert_play_by_play(connection, sources["play_by_play"])
            insert_shots(connection, sources["shots"])
            connection.commit()

            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_errors:
                raise RuntimeError(f"Foreign key errors: {foreign_key_errors}")

            for table_name, source_rows in sources.items():
                database_count = connection.execute(
                    f"SELECT COUNT(*) FROM {table_name}"
                ).fetchone()[0]
                if database_count != len(source_rows):
                    raise RuntimeError(
                        f"{table_name} count mismatch: "
                        f"database={database_count}, source={len(source_rows)}"
                    )
        finally:
            connection.close()
            connection = None

        os.replace(TEMP_DATABASE_PATH, DATABASE_PATH)
    except Exception:
        if connection is not None:
            connection.close()
        if TEMP_DATABASE_PATH.exists():
            TEMP_DATABASE_PATH.unlink()
        raise

    return {table_name: len(rows) for table_name, rows in sources.items()}


def main():
    counts = build_database()
    print(f"Built: {DATABASE_PATH}")
    for table_name in SOURCE_FILES:
        print(f"{table_name}: {counts[table_name]}")
    print("foreign_key_check: 0 errors")


if __name__ == "__main__":
    main()
