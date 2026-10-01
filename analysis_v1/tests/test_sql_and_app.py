from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

import pandas as pd

from analysis_v1.app.export_app_data import export_app_data
from analysis_v1.build_analysis_db import DEFAULT_SQLITE_PATH


ROOT = Path(__file__).resolve().parents[1]


class SqlPackageTest(unittest.TestCase):
    def test_all_seventeen_queries_execute_and_return_rows(self) -> None:
        sql_files = sorted((ROOT / "sql").glob("*.sql"))
        self.assertEqual(len(sql_files), 17)
        connection = sqlite3.connect(DEFAULT_SQLITE_PATH)
        try:
            for path in sql_files:
                with self.subTest(query=path.name):
                    frame = pd.read_sql_query(path.read_text(encoding="utf-8"), connection)
                    self.assertGreater(len(frame), 0)
        finally:
            connection.close()

    def test_sql_catalog_has_evidence_fields_for_every_query(self) -> None:
        catalog = (ROOT / "SQL_QUERY_CATALOG.md").read_text(encoding="utf-8")
        self.assertEqual(catalog.count("适用边界："), 17)
        self.assertEqual(catalog.count("面试追问："), 17)
        self.assertEqual(catalog.count("实际结果："), 17)


class StaticAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app_data_path = ROOT / "app" / "static" / "app_data.json"
        cls.original_app_data = (
            app_data_path.read_bytes() if app_data_path.exists() else None
        )
        cls.data_path = export_app_data()
        cls.payload = json.loads(cls.data_path.read_text(encoding="utf-8"))
        cls.html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.original_app_data is None:
            cls.data_path.unlink(missing_ok=True)
        else:
            cls.data_path.write_bytes(cls.original_app_data)

    def test_app_uses_real_database_counts(self) -> None:
        rows = self.payload["database"]["table_rows"]
        self.assertEqual(rows["dim_game"], 15472)
        self.assertEqual(rows["fact_prediction"], 1322)
        self.assertEqual(self.payload["model_summary"][0]["accuracy"], 0.6732)

    def test_app_exposes_scope_and_market_limits(self) -> None:
        combined = " ".join(self.payload["limits"])
        self.assertIn("没有真实新赛季前向验证", combined)
        self.assertIn("市场线缺少可靠采集时间戳", combined)
        self.assertIn("不提供投注建议", combined)
        self.assertEqual(self.payload["product"]["database_backend"], "SQLite fallback")

    def test_app_has_required_pages_and_accessibility_hooks(self) -> None:
        for page in ("home", "overview", "predictions", "bad-cases", "methodology"):
            self.assertIn(f'data-page="{page}"', self.html)
        self.assertIn("skip-link", self.html)
        self.assertIn("aria-label=", self.html)


if __name__ == "__main__":
    unittest.main()
