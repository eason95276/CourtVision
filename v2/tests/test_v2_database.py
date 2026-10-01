import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
V2_DIR = PROJECT_DIR / "v2"
DATABASE_PATH = V2_DIR / "db" / "courtvision_v2.sqlite"
GAMES_JSON_PATH = V2_DIR / "data" / "normalized" / "games.json"
BUILD_SCRIPT_PATH = V2_DIR / "scripts" / "build_v2_database.py"

EXPECTED_COUNTS = {
    "games": 3,
    "team_boxscores": 6,
    "player_boxscores": 78,
    "play_by_play": 1641,
    "shots": 516,
}


class CourtVisionV2DatabaseTests(unittest.TestCase):
    def connect(self):
        connection = sqlite3.connect(DATABASE_PATH)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def table_counts(self):
        with closing(self.connect()) as connection:
            return {
                table_name: connection.execute(
                    f"SELECT COUNT(*) FROM {table_name}"
                ).fetchone()[0]
                for table_name in EXPECTED_COUNTS
            }

    def test_01_database_exists(self):
        self.assertTrue(DATABASE_PATH.is_file(), DATABASE_PATH)

    def test_02_five_required_tables_exist(self):
        with closing(self.connect()) as connection:
            actual_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue(set(EXPECTED_COUNTS).issubset(actual_tables))

    def test_03_expected_row_counts(self):
        self.assertEqual(self.table_counts(), EXPECTED_COUNTS)

    def test_04_game_ids_match_normalized_json(self):
        with GAMES_JSON_PATH.open("r", encoding="utf-8") as source_file:
            expected_ids = {row["game_id"] for row in json.load(source_file)}
        with closing(self.connect()) as connection:
            actual_ids = {
                row[0] for row in connection.execute("SELECT game_id FROM games")
            }
        self.assertEqual(actual_ids, expected_ids)
        self.assertEqual(len(actual_ids), 3)

    def test_05_team_scores_match_games(self):
        query = """
            SELECT COUNT(*)
            FROM team_boxscores AS t
            JOIN games AS g ON g.game_id = t.game_id
            WHERE
                (t.side = 'away' AND (
                    t.team_id <> g.away_team_id OR
                    t.team_tricode <> g.away_team_tricode OR
                    t.points <> g.away_points
                ))
                OR
                (t.side = 'home' AND (
                    t.team_id <> g.home_team_id OR
                    t.team_tricode <> g.home_team_tricode OR
                    t.points <> g.home_points
                ))
        """
        with closing(self.connect()) as connection:
            mismatch_count = connection.execute(query).fetchone()[0]
        self.assertEqual(mismatch_count, 0)

    def test_06_all_shots_join_to_games(self):
        query = """
            SELECT COUNT(*)
            FROM shots AS s
            LEFT JOIN games AS g ON g.game_id = s.game_id
            WHERE g.game_id IS NULL
        """
        with closing(self.connect()) as connection:
            orphan_count = connection.execute(query).fetchone()[0]
        self.assertEqual(orphan_count, 0)

    def test_07_all_team_boxscores_join_to_games(self):
        query = """
            SELECT COUNT(*)
            FROM team_boxscores AS t
            LEFT JOIN games AS g ON g.game_id = t.game_id
            WHERE g.game_id IS NULL
        """
        with closing(self.connect()) as connection:
            orphan_count = connection.execute(query).fetchone()[0]
        self.assertEqual(orphan_count, 0)

    def test_08_foreign_keys_are_enabled_and_valid(self):
        with closing(self.connect()) as connection:
            enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(enabled, 1)
        self.assertEqual(errors, [])

    def test_09_rebuilding_does_not_change_counts(self):
        counts_before = self.table_counts()
        result = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT_PATH)],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.table_counts(), counts_before)


if __name__ == "__main__":
    unittest.main()
