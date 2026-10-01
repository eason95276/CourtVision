import hashlib
import importlib.util
import json
import math
import re
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
V2_DIR = PROJECT_DIR / "v2"
DATABASE_PATH = V2_DIR / "db" / "courtvision_v2.sqlite"
BUILD_SCRIPT_PATH = V2_DIR / "scripts" / "build_single_game_facts.py"
OUTPUTS_DIR = V2_DIR / "outputs"
CASE_REPORT_PATH = V2_DIR / "docs" / "03_SINGLE_GAME_FACTS.md"
SQL_PATH = V2_DIR / "sql" / "02_single_game_facts.sql"
INVALID_GAME_ID = "9999999999"


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connect_read_only():
    uri = f"{DATABASE_PATH.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def load_games():
    query = """
        SELECT
            g.game_id,
            g.game_date,
            g.away_team_id,
            g.away_team_tricode,
            away.team_city || ' ' || away.team_name AS away_team_name,
            g.away_points,
            g.home_team_id,
            g.home_team_tricode,
            home.team_city || ' ' || home.team_name AS home_team_name,
            g.home_points
        FROM games AS g
        JOIN team_boxscores AS away
            ON away.game_id = g.game_id AND away.side = 'away'
        JOIN team_boxscores AS home
            ON home.game_id = g.game_id AND home.side = 'home'
        ORDER BY g.game_date, g.game_id
    """
    with closing(connect_read_only()) as connection:
        return [dict(row) for row in connection.execute(query)]


def run_builder(game_id=None):
    command = [sys.executable, str(BUILD_SCRIPT_PATH)]
    if game_id is not None:
        command.extend(["--game-id", game_id])
    return subprocess.run(
        command,
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def load_builder_module():
    spec = importlib.util.spec_from_file_location(
        "build_single_game_facts", BUILD_SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SingleGameFactsGeneralizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.games = load_games()
        if len(cls.games) != 3:
            raise RuntimeError(f"Expected 3 database games, found {len(cls.games)}")

        cls.database_hash_before = sha256_file(DATABASE_PATH)
        cls.case_report_hash_before = sha256_file(CASE_REPORT_PATH)
        cls.generated = {}

        for game in cls.games:
            game_id = game["game_id"]
            result = run_builder(game_id)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Generation failed for {game_id}:\n{result.stdout}{result.stderr}"
                )
            json_path = OUTPUTS_DIR / f"game_{game_id}_facts.json"
            markdown_path = OUTPUTS_DIR / f"game_{game_id}_facts.md"
            raw_json = json_path.read_text(encoding="utf-8")
            cls.generated[game_id] = {
                "result": result,
                "json_path": json_path,
                "markdown_path": markdown_path,
                "raw_json": raw_json,
                "facts": json.loads(raw_json),
                "markdown": markdown_path.read_text(encoding="utf-8"),
            }

        cls.database_hash_after = sha256_file(DATABASE_PATH)
        cls.case_report_hash_after = sha256_file(CASE_REPORT_PATH)

    def test_01_all_database_games_have_distinct_output_pairs(self):
        expected_paths = set()
        for game in self.games:
            game_id = game["game_id"]
            expected_paths.add(OUTPUTS_DIR / f"game_{game_id}_facts.json")
            expected_paths.add(OUTPUTS_DIR / f"game_{game_id}_facts.md")
        self.assertEqual(len(expected_paths), 6)
        self.assertTrue(all(path.is_file() for path in expected_paths))

    def test_02_game_identity_and_scores_match_database(self):
        for game in self.games:
            facts = self.generated[game["game_id"]]["facts"]
            self.assertEqual(facts["game"]["game_id"], game["game_id"])
            self.assertEqual(facts["game"]["date"], game["game_date"])
            self.assertEqual(
                facts["game"]["away"],
                {
                    "team_id": game["away_team_id"],
                    "tricode": game["away_team_tricode"],
                    "name": game["away_team_name"],
                    "points": game["away_points"],
                },
            )
            self.assertEqual(
                facts["game"]["home"],
                {
                    "team_id": game["home_team_id"],
                    "tricode": game["home_team_tricode"],
                    "name": game["home_team_name"],
                    "points": game["home_points"],
                },
            )

    def test_03_margin_total_and_winner_are_dynamic(self):
        for game in self.games:
            facts = self.generated[game["game_id"]]["facts"]["game"]
            self.assertEqual(
                facts["final_margin"],
                abs(game["home_points"] - game["away_points"]),
            )
            self.assertEqual(
                facts["actual_total"],
                game["home_points"] + game["away_points"],
            )
            expected_winner = (
                game["home_team_tricode"]
                if game["home_points"] > game["away_points"]
                else game["away_team_tricode"]
            )
            self.assertEqual(facts["winner_tricode"], expected_winner)

    def test_04_period_scores_sum_to_each_final_score(self):
        for game in self.games:
            periods = self.generated[game["game_id"]]["facts"]["score_by_period"]
            self.assertEqual(len(periods), 4)
            self.assertEqual(
                sum(row["away_points"] for row in periods), game["away_points"]
            )
            self.assertEqual(
                sum(row["home_points"] for row in periods), game["home_points"]
            )

    def test_05_player_points_sum_to_each_team_score(self):
        for game in self.games:
            players = self.generated[game["game_id"]]["facts"]["player_facts"]["teams"]
            self.assertEqual(
                sum(row["points"] for row in players[game["away_team_tricode"]]),
                game["away_points"],
            )
            self.assertEqual(
                sum(row["points"] for row in players[game["home_team_tricode"]]),
                game["home_points"],
            )

    def test_06_shot_zone_totals_match_each_team_boxscore(self):
        for game in self.games:
            facts = self.generated[game["game_id"]]["facts"]
            zones = facts["shot_zone_facts"]["zones"]
            for team in (game["away_team_tricode"], game["home_team_tricode"]):
                attempts = sum(row["teams"][team]["attempts"] for row in zones)
                makes = sum(row["teams"][team]["makes"] for row in zones)
                boxscore = facts["four_factors"][team]["source_boxscore"]
                self.assertEqual(attempts, boxscore["fga"])
                self.assertEqual(makes, boxscore["fgm"])

    def test_07_json_is_standard_and_four_factors_are_finite_or_null(self):
        metric_names = (
            "effective_field_goal_percentage",
            "turnover_rate",
            "offensive_rebound_rate",
            "free_throw_rate",
        )
        for game in self.games:
            generated = self.generated[game["game_id"]]
            self.assertNotIn("NaN", generated["raw_json"])
            self.assertNotIn("Infinity", generated["raw_json"])
            json.loads(
                generated["raw_json"],
                parse_constant=lambda value: self.fail(
                    f"Non-standard JSON numeric constant: {value}"
                ),
            )
            for team in (game["away_team_tricode"], game["home_team_tricode"]):
                for metric_name in metric_names:
                    value = generated["facts"]["four_factors"][team][metric_name]["raw"]
                    self.assertTrue(value is None or math.isfinite(value))

    def test_08_markdown_matches_only_its_target_game(self):
        for game in self.games:
            markdown = self.generated[game["game_id"]]["markdown"]
            self.assertIn(f"比赛：`{game['game_id']}`", markdown)
            self.assertIn(game["away_team_name"], markdown)
            self.assertIn(game["home_team_name"], markdown)
            expected_score = (
                f"| 最终比分 | {game['away_team_tricode']} {game['away_points']}，"
                f"{game['home_team_tricode']} {game['home_points']} |"
            )
            self.assertIn(expected_score, markdown)

            participant_ids = {game["away_team_id"], game["home_team_id"]}
            for other in self.games:
                other_score = (
                    f"| 最终比分 | {other['away_team_tricode']} {other['away_points']}，"
                    f"{other['home_team_tricode']} {other['home_points']} |"
                )
                if other["game_id"] != game["game_id"]:
                    self.assertNotIn(other_score, markdown)
                for side in ("away", "home"):
                    team_id = other[f"{side}_team_id"]
                    if team_id not in participant_ids:
                        tricode = other[f"{side}_team_tricode"]
                        team_name = other[f"{side}_team_name"]
                        self.assertNotIn(team_name, markdown)
                        self.assertIsNone(
                            re.search(rf"(?<![A-Z]){re.escape(tricode)}(?![A-Z])", markdown)
                        )

    def test_09_key_run_action_ids_are_traceable_per_game(self):
        with closing(connect_read_only()) as connection:
            for game in self.games:
                key_runs = self.generated[game["game_id"]]["facts"]["key_runs"]
                self.assertGreater(len(key_runs), 0)
                action_ids = {
                    event["action_id"]
                    for run in key_runs
                    for event in run["events"]
                }
                self.assertTrue(
                    all(isinstance(action_id, int) and action_id > 0 for action_id in action_ids)
                )
                placeholders = ",".join("?" for _ in action_ids)
                found = connection.execute(
                    f"SELECT COUNT(*) FROM play_by_play WHERE game_id = ? AND action_id IN ({placeholders})",
                    (game["game_id"], *sorted(action_ids)),
                ).fetchone()[0]
                self.assertEqual(found, len(action_ids))
                for run in key_runs:
                    event_ids = [event["action_id"] for event in run["events"]]
                    self.assertEqual(run["start_action_id"], min(event_ids))
                    self.assertEqual(run["end_action_id"], max(event_ids))

    def test_10_limitations_and_read_only_evidence_are_preserved(self):
        for game in self.games:
            facts = self.generated[game["game_id"]]["facts"]
            limitations = " ".join(facts["limitations"])
            self.assertIn("full-game five-player lineup reconstruction", limitations)
            self.assertIn("cannot independently prove tactical causality", limitations)
            evidence = facts["evidence"]["database"]
            self.assertEqual(evidence["access_mode"], "SQLite URI mode=ro with PRAGMA query_only=ON")
            self.assertEqual(evidence["sha256_before"], self.database_hash_before)
            self.assertEqual(evidence["sha256_after"], self.database_hash_before)
            self.assertTrue(evidence["unchanged"])

    def test_11_database_sha256_is_unchanged_across_all_generations(self):
        self.assertEqual(self.database_hash_before, self.database_hash_after)

    def test_12_original_case_report_is_not_overwritten(self):
        self.assertEqual(self.case_report_hash_before, self.case_report_hash_after)

    def test_13_unknown_game_fails_without_creating_outputs(self):
        json_path = OUTPUTS_DIR / f"game_{INVALID_GAME_ID}_facts.json"
        markdown_path = OUTPUTS_DIR / f"game_{INVALID_GAME_ID}_facts.md"
        self.assertFalse(json_path.exists())
        self.assertFalse(markdown_path.exists())
        database_hash_before = sha256_file(DATABASE_PATH)
        case_report_hash_before = sha256_file(CASE_REPORT_PATH)

        result = run_builder(INVALID_GAME_ID)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(INVALID_GAME_ID, result.stderr)
        self.assertIn("not found", result.stderr.lower())
        self.assertFalse(json_path.exists())
        self.assertFalse(markdown_path.exists())
        self.assertEqual(sha256_file(DATABASE_PATH), database_hash_before)
        self.assertEqual(sha256_file(CASE_REPORT_PATH), case_report_hash_before)

    def test_14_missing_game_id_fails_with_command_help(self):
        database_hash_before = sha256_file(DATABASE_PATH)
        case_report_hash_before = sha256_file(CASE_REPORT_PATH)

        result = run_builder()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage:", result.stderr.lower())
        self.assertIn("--game-id", result.stderr)
        self.assertIn("required", result.stderr.lower())
        self.assertEqual(sha256_file(DATABASE_PATH), database_hash_before)
        self.assertEqual(sha256_file(CASE_REPORT_PATH), case_report_hash_before)

    def test_15_sql_queries_remain_named_and_parameterized(self):
        sql_text = SQL_PATH.read_text(encoding="utf-8")
        markers = list(re.finditer(r"(?m)^-- query: ([a-z0-9_]+)\s*$", sql_text))
        self.assertEqual(len(markers), 14)
        for index, marker in enumerate(markers):
            section_end = markers[index + 1].start() if index + 1 < len(markers) else len(sql_text)
            self.assertIn(":game_id", sql_text[marker.end():section_end])
        for game in self.games:
            self.assertNotIn(game["game_id"], sql_text)

    def test_16_generation_script_has_no_fixed_game_or_team_literals(self):
        script_text = BUILD_SCRIPT_PATH.read_text(encoding="utf-8")
        for game in self.games:
            self.assertNotIn(game["game_id"], script_text)
            self.assertNotIn(f'"{game["away_team_tricode"]}"', script_text)
            self.assertNotIn(f'"{game["home_team_tricode"]}"', script_text)

    def test_17_result_sentence_handles_an_away_win(self):
        module = load_builder_module()
        game = {
            "away": {"name": "Away Team", "points": 101, "tricode": "AWY"},
            "home": {"name": "Home Team", "points": 99, "tricode": "HME"},
            "winner_tricode": "AWY",
        }
        self.assertEqual(
            module.game_result_sentence(game),
            "Away Team客场 101 比 99 战胜Home Team。",
        )


if __name__ == "__main__":
    unittest.main()
