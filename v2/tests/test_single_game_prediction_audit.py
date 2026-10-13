import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[2]
V2_DIR = PROJECT_DIR / "v2"
ANALYSIS_DATABASE_PATH = PROJECT_DIR / "analysis_v1" / "data" / "courtvision_analysis.sqlite"
DETAIL_DATABASE_PATH = V2_DIR / "db" / "courtvision_v2.sqlite"
BUILD_SCRIPT_PATH = V2_DIR / "scripts" / "build_single_game_prediction_audit.py"
OUTPUTS_DIR = V2_DIR / "outputs"
GAME_IDS = ("0022500166", "0022500564", "0022501099")
INVALID_GAME_ID = "9999999998"


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connect_read_only(path):
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def run_builder(game_id=None):
    command = [sys.executable, str(BUILD_SCRIPT_PATH)]
    if game_id is not None:
        command.extend(["--game-id", game_id])
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        command,
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def load_builder_module():
    spec = importlib.util.spec_from_file_location(
        "build_single_game_prediction_audit", BUILD_SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_expected_rows():
    with closing(connect_read_only(DETAIL_DATABASE_PATH)) as connection:
        detail_rows = {
            row["game_id"]: dict(row)
            for row in connection.execute(
                """
                SELECT
                    g.game_id,
                    g.game_date,
                    g.season,
                    g.season_type,
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
                WHERE g.game_id IN (?, ?, ?)
                """,
                GAME_IDS,
            )
        }
    with closing(connect_read_only(ANALYSIS_DATABASE_PATH)) as connection:
        analysis_rows = {
            row["game_id"]: dict(row)
            for row in connection.execute(
                """
                SELECT
                    g.game_id,
                    g.point_margin,
                    g.total_points,
                    p.evaluation_season,
                    p.win_probability,
                    p.predicted_home_win,
                    p.predicted_margin,
                    p.predicted_total,
                    p.model_scope,
                    p.prediction_time_status,
                    m.game_id AS market_game_id,
                    m.market_expected_home_margin,
                    m.market_total,
                    m.line_capture_time_status,
                    m.line_identity_status
                FROM dim_game AS g
                JOIN fact_prediction AS p ON p.game_id = g.game_id
                LEFT JOIN fact_market_benchmark AS m ON m.game_id = g.game_id
                WHERE g.game_id IN (?, ?, ?)
                """,
                GAME_IDS,
            )
        }
    return detail_rows, analysis_rows


class SingleGamePredictionAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analysis_hash_before = sha256_file(ANALYSIS_DATABASE_PATH)
        cls.detail_hash_before = sha256_file(DETAIL_DATABASE_PATH)
        cls.detail_rows, cls.analysis_rows = load_expected_rows()
        if set(cls.detail_rows) != set(GAME_IDS):
            raise RuntimeError("详细比赛数据库中的三场样例不完整")
        if set(cls.analysis_rows) != set(GAME_IDS):
            raise RuntimeError("历史预测数据库中的三场样例不完整")

        cls.generated = {}
        for game_id in GAME_IDS:
            result = run_builder(game_id)
            if result.returncode != 0:
                raise RuntimeError(
                    f"生成 {game_id} 失败：\n{result.stdout}{result.stderr}"
                )
            json_path = OUTPUTS_DIR / f"game_{game_id}_prediction_audit.json"
            markdown_path = OUTPUTS_DIR / f"game_{game_id}_prediction_audit.md"
            cls.generated[game_id] = {
                "result": result,
                "json_path": json_path,
                "markdown_path": markdown_path,
                "raw_json": json_path.read_text(encoding="utf-8"),
                "facts": json.loads(json_path.read_text(encoding="utf-8")),
                "markdown": markdown_path.read_text(encoding="utf-8"),
            }

    def test_01_three_games_generate_isolated_output_pairs(self):
        self.assertEqual(set(self.generated), set(GAME_IDS))
        for game_id, generated in self.generated.items():
            self.assertTrue(generated["json_path"].is_file())
            self.assertTrue(generated["markdown_path"].is_file())
            self.assertEqual(generated["facts"]["game"]["game_id"], game_id)
            self.assertIn(f"`{game_id}`", generated["markdown"])
            for other_game_id in set(GAME_IDS) - {game_id}:
                self.assertNotIn(other_game_id, generated["raw_json"])
                self.assertNotIn(other_game_id, generated["markdown"])

    def test_02_game_prediction_and_market_facts_match_databases(self):
        for game_id in GAME_IDS:
            facts = self.generated[game_id]["facts"]
            detail = self.detail_rows[game_id]
            analysis = self.analysis_rows[game_id]
            self.assertEqual(facts["game"]["actual_home_margin"], analysis["point_margin"])
            self.assertEqual(facts["game"]["actual_total"], analysis["total_points"])
            self.assertEqual(facts["game"]["home"]["points"], detail["home_points"])
            self.assertEqual(facts["game"]["away"]["points"], detail["away_points"])
            self.assertEqual(
                facts["historical_prediction"]["home_win_probability"],
                analysis["win_probability"],
            )
            self.assertEqual(
                facts["historical_prediction"]["predicted_home_margin"],
                analysis["predicted_margin"],
            )
            self.assertEqual(
                facts["historical_prediction"]["predicted_total"],
                analysis["predicted_total"],
            )
            self.assertEqual(
                facts["historical_market_benchmark"]["market_expected_home_margin"],
                analysis["market_expected_home_margin"],
            )
            self.assertEqual(
                facts["historical_market_benchmark"]["market_total"],
                analysis["market_total"],
            )

    def test_03_reference_game_has_required_values_and_formula_results(self):
        facts = self.generated["0022500564"]["facts"]
        prediction = facts["historical_prediction"]
        market = facts["historical_market_benchmark"]
        grading = facts["direction_evaluation"]
        disagreement = facts["model_market_disagreement"]

        self.assertEqual(facts["game"]["actual_home_margin"], 6)
        self.assertEqual(facts["game"]["actual_total"], 248)
        self.assertAlmostEqual(prediction["predicted_home_margin"], -1.5686, places=4)
        self.assertAlmostEqual(prediction["predicted_total"], 226.9332, places=4)
        self.assertEqual(market["market_expected_home_margin"], 1.5)
        self.assertEqual(market["market_total"], 233.0)
        self.assertAlmostEqual(
            disagreement["home_margin"],
            prediction["predicted_home_margin"]
            - market["market_expected_home_margin"],
        )
        self.assertAlmostEqual(
            disagreement["total"],
            prediction["predicted_total"] - market["market_total"],
        )
        self.assertEqual(grading["spread"]["model_direction"], "away")
        self.assertEqual(grading["spread"]["actual_result"], "home")
        self.assertFalse(grading["spread"]["spread_direction_hit"])
        self.assertEqual(grading["total"]["model_direction"], "under")
        self.assertEqual(grading["total"]["actual_result"], "over")
        self.assertFalse(grading["total"]["total_direction_hit"])

    def test_04_invalid_game_fails_before_writing_outputs(self):
        json_path = OUTPUTS_DIR / f"game_{INVALID_GAME_ID}_prediction_audit.json"
        markdown_path = OUTPUTS_DIR / f"game_{INVALID_GAME_ID}_prediction_audit.md"
        self.assertFalse(json_path.exists())
        self.assertFalse(markdown_path.exists())
        result = run_builder(INVALID_GAME_ID)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(INVALID_GAME_ID, result.stderr)
        self.assertIn("不存在", result.stderr)
        self.assertFalse(json_path.exists())
        self.assertFalse(markdown_path.exists())

    def test_05_missing_market_retains_prediction_and_actual_facts(self):
        module = load_builder_module()
        detail = dict(self.detail_rows["0022500564"])
        analysis = dict(self.analysis_rows["0022500564"])
        analysis.update(
            {
                "market_game_id": None,
                "market_expected_home_margin": None,
                "market_total": None,
                "line_capture_time_status": None,
                "line_identity_status": None,
            }
        )
        hashes = {"analysis": "a" * 64, "detail": "b" * 64}
        facts = module.make_audit_package(detail, analysis, hashes, hashes)
        spread = facts["direction_evaluation"]["spread"]
        total = facts["direction_evaluation"]["total"]
        self.assertEqual(facts["game"]["actual_home_margin"], 6)
        self.assertEqual(facts["game"]["actual_total"], 248)
        self.assertAlmostEqual(
            facts["historical_prediction"]["predicted_home_margin"], -1.5686, places=4
        )
        self.assertAlmostEqual(
            facts["historical_prediction"]["predicted_total"], 226.9332, places=4
        )
        self.assertIsNone(
            facts["historical_market_benchmark"]["market_expected_home_margin"]
        )
        self.assertIsNone(facts["historical_market_benchmark"]["market_total"])
        self.assertIsNone(facts["model_market_disagreement"]["home_margin"])
        self.assertIsNone(facts["model_market_disagreement"]["total"])
        self.assertIsNone(spread["model_direction"])
        self.assertIsNone(spread["actual_result"])
        self.assertIsNone(spread["spread_direction_hit"])
        self.assertEqual(spread["status"], "market_unavailable")
        self.assertTrue(spread["reason"])
        self.assertIsNone(total["model_direction"])
        self.assertIsNone(total["actual_result"])
        self.assertIsNone(total["total_direction_hit"])
        self.assertEqual(total["status"], "market_unavailable")
        self.assertTrue(total["reason"])

    def test_06_exact_prediction_market_equality_is_no_disagreement(self):
        module = load_builder_module()
        spread = module.grade_spread(1.5, 1.5, 6)
        total = module.grade_total(233.0, 233.0, 248)
        self.assertEqual(spread["model_direction"], "no_disagreement")
        self.assertIsNone(spread["spread_direction_hit"])
        self.assertEqual(spread["status"], "no_model_market_disagreement")
        self.assertEqual(total["model_direction"], "no_disagreement")
        self.assertIsNone(total["total_direction_hit"])
        self.assertEqual(total["status"], "no_model_market_disagreement")

    def test_07_exact_actual_market_equality_is_push(self):
        module = load_builder_module()
        spread = module.grade_spread(2.5, 1.5, 1.5)
        total = module.grade_total(235.0, 233.0, 233.0)
        self.assertEqual(spread["actual_result"], "push")
        self.assertIsNone(spread["spread_direction_hit"])
        self.assertEqual(spread["status"], "push")
        self.assertEqual(total["actual_result"], "push")
        self.assertIsNone(total["total_direction_hit"])
        self.assertEqual(total["status"], "push")

    def test_08_record_boundaries_and_source_labels_are_explicit(self):
        forbidden = "中国体彩官方盘口"
        for generated in self.generated.values():
            facts = generated["facts"]
            prediction = facts["historical_prediction"]
            market = facts["historical_market_benchmark"]
            self.assertEqual(prediction["prediction_record_type"], "historical_backtest")
            self.assertFalse(prediction["live_pregame_snapshot"])
            self.assertEqual(market["market_source_label"], "historical_market_benchmark")
            self.assertFalse(market["official_china_sports_lottery_verified"])
            self.assertFalse(
                market["eligible_for_public_china_sports_lottery_label"]
            )
            self.assertNotIn(forbidden, generated["raw_json"])
            self.assertNotIn("high_confidence", generated["raw_json"])
            self.assertNotIn("low_confidence", generated["raw_json"])

    def test_09_atomic_pair_failure_restores_existing_valid_outputs(self):
        module = load_builder_module()
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            json_path = directory_path / "audit.json"
            markdown_path = directory_path / "audit.md"
            json_path.write_text('{"valid": true}\n', encoding="utf-8")
            markdown_path.write_text("# 有效报告\n", encoding="utf-8")
            json_before = json_path.read_bytes()
            markdown_before = markdown_path.read_bytes()
            real_replace = os.replace
            calls = 0

            def fail_second_replace(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated second publish failure")
                return real_replace(source, destination)

            with mock.patch.object(module.os, "replace", side_effect=fail_second_replace):
                with self.assertRaises(OSError):
                    module.atomic_write_pair(
                        json_path,
                        '{"valid": false}\n',
                        markdown_path,
                        "# 无效报告\n",
                    )

            self.assertEqual(json_path.read_bytes(), json_before)
            self.assertEqual(markdown_path.read_bytes(), markdown_before)

    def test_10_successful_output_publication_uses_atomic_replace(self):
        module = load_builder_module()
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            json_path = directory_path / "audit.json"
            markdown_path = directory_path / "audit.md"
            with mock.patch.object(
                module.os, "replace", wraps=os.replace
            ) as replace_mock:
                module.atomic_write_pair(
                    json_path,
                    '{"ok": true}\n',
                    markdown_path,
                    "# 完成\n",
                )
            self.assertEqual(replace_mock.call_count, 2)
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), {"ok": True})
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), "# 完成\n")

    def test_11_two_database_hashes_are_unchanged(self):
        self.assertEqual(sha256_file(ANALYSIS_DATABASE_PATH), self.analysis_hash_before)
        self.assertEqual(sha256_file(DETAIL_DATABASE_PATH), self.detail_hash_before)

    def test_12_missing_game_id_returns_command_help(self):
        result = run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage:", result.stderr.lower())
        self.assertIn("--game-id", result.stderr)
        self.assertIn("required", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
