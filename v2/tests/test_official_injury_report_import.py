from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_DIR / "v2" / "scripts"
IMPORT_PATH = SCRIPTS_DIR / "import_official_injury_report.py"
BUILD_PATH = SCRIPTS_DIR / "build_pregame_information_db.py"
SCHEMA_PATH = PROJECT_DIR / "v2" / "sql" / "03_pregame_information_schema.sql"
PDF_PATH = (
    PROJECT_DIR
    / "v2"
    / "data"
    / "pregame_snapshots"
    / "raw"
    / "Injury-Report_2026-01-13_02_45PM.pdf"
)
ANALYSIS_DATABASE = (
    PROJECT_DIR / "analysis_v1" / "data" / "courtvision_analysis.sqlite"
)
DETAIL_DATABASE = PROJECT_DIR / "v2" / "db" / "courtvision_v2.sqlite"
SOURCE_URL = (
    "https://ak-static.cms.nba.com/referee/injury/"
    "Injury-Report_2026-01-13_02_45PM.pdf"
)
GAME_ID = "0022500564"
CUTOFF = "2026-01-13T19:00:00"


def load_module(name: str, path: Path):
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


importer = load_module("official_injury_import", IMPORT_PATH)
builder = load_module("pregame_database_builder_for_injury", BUILD_PATH)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OfficialInjuryReportImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analysis_hash = sha256_file(ANALYSIS_DATABASE)
        cls.detail_hash = sha256_file(DETAIL_DATABASE)
        cls.pdf_hash = sha256_file(PDF_PATH)

    @classmethod
    def tearDownClass(cls):
        if sha256_file(ANALYSIS_DATABASE) != cls.analysis_hash:
            raise AssertionError("历史分析数据库在测试期间发生变化")
        if sha256_file(DETAIL_DATABASE) != cls.detail_hash:
            raise AssertionError("详细比赛数据库在测试期间发生变化")
        if sha256_file(PDF_PATH) != cls.pdf_hash:
            raise AssertionError("NBA 官方原始伤病报告在测试期间发生变化")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "pregame.sqlite"
        self.log = self.root / "import.json"
        builder.build_database(self.database, SCHEMA_PATH)

    def tearDown(self):
        self.temporary.cleanup()

    def import_once(self):
        return importer.import_report(
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            pdf_path=PDF_PATH,
            source_url=SOURCE_URL,
            analysis_database=ANALYSIS_DATABASE,
            snapshot_database=self.database,
            log_path=self.log,
        )

    def test_01_parses_and_matches_ten_target_game_rows(self):
        result = self.import_once()
        self.assertEqual(result["record_count"], 10)
        self.assertEqual(result["matchup"], "PHX@MIA")
        self.assertEqual(result["source_report_time_local"], "2026-01-13T14:45:00-05:00")
        self.assertEqual(result["source_published_at_utc"], "2026-01-13T19:45:00Z")
        self.assertEqual(result["cutoff_time_utc"], "2026-01-14T00:00:00Z")
        self.assertEqual(
            {row["team_name"] for row in result["records"]},
            {"Phoenix Suns", "Miami Heat"},
        )
        self.assertIn(
            ("Jalen Green", "Out", "Injury/Illness - Right Hamstring; Strain"),
            {
                (row["player_name"], row["availability_status"], row["reason"])
                for row in result["records"]
            },
        )

    def test_02_late_capture_is_transparently_labeled_historical_archive(self):
        result = self.import_once()
        self.assertGreater(result["captured_at_utc"], result["cutoff_time_utc"])
        self.assertEqual(result["normalized_status"], "available_historical_archive")
        self.assertEqual(result["raw_sha256"], sha256_file(PDF_PATH))
        with closing(sqlite3.connect(self.database)) as connection:
            eligible = connection.execute(
                "SELECT COUNT(*) FROM agent_eligible_injury_snapshots WHERE game_id = ?",
                (GAME_ID,),
            ).fetchone()[0]
            fetch = connection.execute(
                "SELECT response_sha256, normalized_status FROM source_fetch_runs "
                "WHERE game_id = ? AND source_type = 'injuries'",
                (GAME_ID,),
            ).fetchone()
        self.assertEqual(eligible, 10)
        self.assertEqual(fetch[0], result["raw_sha256"])
        self.assertEqual(fetch[1], "available_historical_archive")
        self.assertTrue(self.log.is_file())
        self.assertEqual(json.loads(self.log.read_text(encoding="utf-8"))["record_count"], 10)

    def test_03_report_after_cutoff_is_rejected_without_rows(self):
        with self.assertRaisesRegex(importer.InjuryImportError, "晚于赛前截止时间"):
            importer.import_report(
                game_id=GAME_ID,
                cutoff_time="2026-01-13T14:00:00",
                pdf_path=PDF_PATH,
                source_url=SOURCE_URL,
                analysis_database=ANALYSIS_DATABASE,
                snapshot_database=self.database,
            )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM injury_snapshots").fetchone()[0],
                0,
            )

    def test_04_non_official_url_is_rejected(self):
        with self.assertRaisesRegex(importer.InjuryImportError, "不是 NBA 官方"):
            importer.import_report(
                game_id=GAME_ID,
                cutoff_time=CUTOFF,
                pdf_path=PDF_PATH,
                source_url="https://example.com/report.pdf",
                analysis_database=ANALYSIS_DATABASE,
                snapshot_database=self.database,
            )


if __name__ == "__main__":
    unittest.main()
