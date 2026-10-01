from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from analysis_v1.build_analysis_db import PROJECT_ROOT, build_database


class AnalysisDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temp_dir.name) / "analysis.sqlite"
        cls.report = build_database("sqlite", cls.database, PROJECT_ROOT)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def test_expected_table_counts(self) -> None:
        self.assertEqual(self.report["table_count"], 9)
        self.assertEqual(self.report["table_rows"]["dim_game"], 15472)
        self.assertEqual(self.report["table_rows"]["fact_team_game"], 30944)
        self.assertEqual(self.report["table_rows"]["fact_prediction"], 1322)

    def test_primary_keys_and_join_coverage(self) -> None:
        checks = self.report["quality_checks"]
        self.assertEqual(checks["duplicate_game_primary_keys"], 0)
        self.assertEqual(checks["duplicate_team_game_primary_keys"], 0)
        self.assertEqual(checks["prediction_game_join_rate"], 1.0)
        self.assertEqual(checks["market_prediction_same_sample_rate"], 1.0)

    def test_score_algebra_and_two_team_rows(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            invalid = connection.execute(
                "SELECT COUNT(*) FROM dim_game WHERE home_score - away_score <> point_margin OR home_score + away_score <> total_points"
            ).fetchone()[0]
            pair_errors = connection.execute(
                "SELECT COUNT(*) FROM (SELECT game_id FROM fact_team_game GROUP BY game_id HAVING COUNT(*) <> 2) x"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(invalid, 0)
        self.assertEqual(pair_errors, 0)

    def test_market_time_limit_is_persisted(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            statuses = connection.execute(
                "SELECT DISTINCT line_capture_time_status, line_identity_status FROM fact_market_benchmark"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(statuses, [("missing", "opening_or_closing_unverified")])

    def test_quality_warnings_are_preserved(self) -> None:
        self.assertEqual(self.report["quality_checks"]["failed_quality_errors"], 0)
        self.assertEqual(self.report["quality_checks"]["failed_quality_warnings"], 3)


class ResearchPackageTest(unittest.TestCase):
    def test_series_core_matches_verified_six_game_record(self) -> None:
        path = (
            PROJECT_ROOT
            / "analysis_v1"
            / "research"
            / "lakers_rockets_2026"
            / "series_data.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["final_series_score"], "LAL 4-2")
        self.assertEqual(len(data["games"]), 6)
        self.assertEqual(sum(game["away_score"] + game["home_score"] for game in data["games"]), 1199)
        self.assertGreaterEqual(len(data["unverified"]), 4)


if __name__ == "__main__":
    unittest.main()
