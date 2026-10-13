from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_ROOT / "analysis_v1"
SCRIPT_PATH = ANALYSIS_DIR / "analyze_market_directions.py"
SQL_PATH = ANALYSIS_DIR / "sql" / "17_market_direction_game_grades.sql"
ANALYSIS_DATABASE = ANALYSIS_DIR / "data" / "courtvision_analysis.sqlite"
DETAIL_DATABASE = PROJECT_ROOT / "v2" / "db" / "courtvision_v2.sqlite"
SOURCE_CSV = PROJECT_ROOT / "artifacts" / "test_predictions_with_market.csv"

PROTECTED_FILES = (
    PROJECT_ROOT / "artifacts" / "error_slices.csv",
    PROJECT_ROOT / "artifacts" / "bad_cases.csv",
    PROJECT_ROOT / "artifacts" / "market_benchmark_summary.json",
    PROJECT_ROOT / "artifacts" / "market_uncertainty.json",
    PROJECT_ROOT / "reports" / "Baseline_Modeling_Report.md",
    PROJECT_ROOT / "reports" / "Stability_and_Uncertainty_Report.md",
    PROJECT_ROOT / "v2" / "outputs" / "game_0022500564_prediction_review_brief.md",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


PROTECTED_HASHES = {path: sha256_file(path) for path in PROTECTED_FILES}


def load_module():
    spec = importlib.util.spec_from_file_location("market_direction_analysis", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MarketDirectionAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.analysis = load_module()
        cls.grades = cls.analysis.read_game_grades(ANALYSIS_DATABASE, SQL_PATH)
        cls.analysis.validate_game_grades(cls.grades)

    def test_01_every_game_has_exactly_one_grade_row(self) -> None:
        self.assertEqual(len(self.grades), 1322)
        self.assertEqual(self.grades["game_id"].nunique(), 1322)
        self.assertFalse(self.grades["game_id"].duplicated().any())

    def test_02_sql_directions_and_statuses_match_python_formula(self) -> None:
        for row in self.grades.itertuples(index=False):
            with self.subTest(game_id=row.game_id, task="spread"):
                expected = self.analysis.grade_direction(
                    row.predicted_margin,
                    row.market_expected_home_margin,
                    row.point_margin,
                    positive_direction="home",
                    negative_direction="away",
                )
                self.assertAlmostEqual(row.model_spread_gap, expected["model_gap"])
                self.assertAlmostEqual(row.actual_spread_gap, expected["actual_gap"])
                self.assertEqual(row.model_spread_direction, expected["model_direction"])
                self.assertEqual(row.actual_spread_direction, expected["actual_direction"])
                self.assertEqual(row.spread_status, expected["status"])
                self.assertEqual(
                    None if pd.isna(row.spread_hit) else int(row.spread_hit),
                    expected["hit"],
                )
            with self.subTest(game_id=row.game_id, task="total"):
                expected = self.analysis.grade_direction(
                    row.predicted_total,
                    row.market_total,
                    row.total_points,
                    positive_direction="over",
                    negative_direction="under",
                )
                self.assertAlmostEqual(row.model_total_gap, expected["model_gap"])
                self.assertAlmostEqual(row.actual_total_gap, expected["actual_gap"])
                self.assertEqual(row.model_total_direction, expected["model_direction"])
                self.assertEqual(row.actual_total_direction, expected["actual_direction"])
                self.assertEqual(row.total_status, expected["status"])
                self.assertEqual(
                    None if pd.isna(row.total_hit) else int(row.total_hit),
                    expected["hit"],
                )

    def test_03_equal_model_and_market_is_no_disagreement(self) -> None:
        grade = self.analysis.grade_direction(
            3.0, 3.0 + 1e-10, -2.0, positive_direction="home", negative_direction="away"
        )
        self.assertEqual(grade["status"], "no_model_market_disagreement")
        self.assertEqual(grade["model_direction"], "no_disagreement")
        self.assertIsNone(grade["hit"])

    def test_04_equal_actual_and_market_is_push(self) -> None:
        grade = self.analysis.grade_direction(
            4.0, 3.0, 3.0 + 1e-10, positive_direction="home", negative_direction="away"
        )
        self.assertEqual(grade["status"], "push")
        self.assertEqual(grade["actual_direction"], "push")
        self.assertIsNone(grade["hit"])

    def test_05_missing_market_is_unavailable(self) -> None:
        grade = self.analysis.grade_direction(
            4.0, None, 3.0, positive_direction="home", negative_direction="away"
        )
        self.assertEqual(grade["status"], "market_unavailable")
        self.assertIsNone(grade["model_gap"])
        self.assertIsNone(grade["hit"])

    def test_06_snapshot_denominators_exclude_non_grades(self) -> None:
        slices = self.analysis.build_slice_analysis(self.grades, iterations=500, seed=42)
        overall = slices[slices["dimension"].eq("overall")].set_index("task")
        self.assertEqual(int(overall.loc["spread", "total_games"]), 1322)
        self.assertEqual(int(overall.loc["spread", "gradable_games"]), 1312)
        self.assertEqual(int(overall.loc["spread", "hits"]), 642)
        self.assertEqual(int(overall.loc["spread", "misses"]), 670)
        self.assertEqual(int(overall.loc["total", "gradable_games"]), 1312)
        self.assertEqual(int(overall.loc["total", "hits"]), 655)
        self.assertEqual(int(overall.loc["total", "misses"]), 657)
        for task in ("spread", "total"):
            row = overall.loc[task]
            excluded = row["pushes"] + row["no_disagreements"] + row["market_unavailable"]
            self.assertEqual(int(row["total_games"] - row["gradable_games"]), int(excluded))

    def test_07_disagreement_quartiles_restore_all_gradable_games(self) -> None:
        slices = self.analysis.build_slice_analysis(self.grades, iterations=500, seed=42)
        quartiles = slices[slices["dimension"].eq("absolute_model_market_gap_quartile")]
        for task in ("spread", "total"):
            task_rows = quartiles[quartiles["task"].eq(task)]
            self.assertEqual(len(task_rows), 4)
            self.assertEqual(int(task_rows["gradable_games"].sum()), 1312)

    def test_08_rates_and_intervals_are_legal(self) -> None:
        slices = self.analysis.build_slice_analysis(self.grades, iterations=500, seed=42)
        for column in ("hit_rate", "coverage_rate", "ci_low", "ci_high"):
            values = slices[column].dropna()
            self.assertTrue(values.between(0.0, 1.0).all(), column)
        self.assertTrue((slices["ci_low"].fillna(0) <= slices["ci_high"].fillna(1)).all())

    def test_09_fixed_seed_repeats_exactly(self) -> None:
        first = self.analysis.build_slice_analysis(self.grades, iterations=500, seed=42)
        second = self.analysis.build_slice_analysis(self.grades, iterations=500, seed=42)
        pd.testing.assert_frame_equal(first, second)

    def test_10_database_and_protected_files_are_unchanged(self) -> None:
        database_hashes = {
            ANALYSIS_DATABASE: sha256_file(ANALYSIS_DATABASE),
            DETAIL_DATABASE: sha256_file(DETAIL_DATABASE),
        }
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            result = self.analysis.generate_outputs(
                database_path=ANALYSIS_DATABASE,
                detail_database_path=DETAIL_DATABASE,
                source_csv_path=SOURCE_CSV,
                sql_path=SQL_PATH,
                summary_path=output_root / "summary.json",
                slices_path=output_root / "slices.csv",
                report_path=output_root / "report.md",
                iterations=500,
                seed=42,
            )
            self.assertEqual(result["database_hashes_before"], result["database_hashes_after"])
        for path, expected in database_hashes.items():
            self.assertEqual(sha256_file(path), expected)
        for path, expected in PROTECTED_HASHES.items():
            self.assertEqual(sha256_file(path), expected)

    def test_11_database_result_matches_fixed_prediction_csv(self) -> None:
        result = self.analysis.crosscheck_fixed_prediction_csv(self.grades, SOURCE_CSV)
        self.assertEqual(result["database_games"], 1322)
        self.assertEqual(result["csv_games"], 1322)
        self.assertEqual(result["mismatched_games"], 0)

    def test_12_sql_package_registers_seventeen_queries(self) -> None:
        package_text = (ANALYSIS_DIR / "run_sql_package.py").read_text(encoding="utf-8")
        self.assertIn('"17_market_direction_game_grades"', package_text)
        self.assertEqual(len(list((ANALYSIS_DIR / "sql").glob("*.sql"))), 17)

    def test_13_summary_and_report_preserve_market_boundaries(self) -> None:
        slices = self.analysis.build_slice_analysis(self.grades, iterations=500, seed=42)
        summary = self.analysis.build_summary(
            self.grades,
            slices,
            iterations=500,
            seed=42,
            crosscheck={"database_games": 1322, "csv_games": 1322, "mismatched_games": 0},
        )
        report = self.analysis.render_report(summary, slices)
        combined = json.dumps(summary, ensure_ascii=False) + report
        for task in ("spread_predicted_minus_actual", "total_predicted_minus_actual"):
            bias = summary["signed_error"][task]
            self.assertLess(bias["weekly_cluster_ci_low"], 0)
            self.assertGreater(bias["weekly_cluster_ci_high"], 0)
            self.assertEqual(bias["interpretation"], "no_supported_systematic_bias")
        self.assertIn("历史市场基准", combined)
        self.assertIn("缺少可靠采集时间", combined)
        self.assertIn("不计算收益、投注金额", combined)
        self.assertNotIn("中国体彩官方盘口", combined)
        self.assertNotIn("模型击败市场", combined)
        self.assertNotIn("建议投注金额", combined)
        self.assertNotIn("稳定优势", combined)


if __name__ == "__main__":
    unittest.main()
