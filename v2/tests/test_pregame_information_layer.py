from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
BUILD_PATH = PROJECT_DIR / "v2" / "scripts" / "build_pregame_information_db.py"
COLLECT_PATH = PROJECT_DIR / "v2" / "scripts" / "collect_pregame_information.py"
SCHEMA_PATH = PROJECT_DIR / "v2" / "sql" / "03_pregame_information_schema.sql"
ANALYSIS_DATABASE_PATH = (
    PROJECT_DIR / "analysis_v1" / "data" / "courtvision_analysis.sqlite"
)
DETAIL_DATABASE_PATH = PROJECT_DIR / "v2" / "db" / "courtvision_v2.sqlite"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_module("build_pregame_information_db", BUILD_PATH)
collector = load_module("collect_pregame_information", COLLECT_PATH)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PregameInformationLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analysis_hash = (
            sha256_file(ANALYSIS_DATABASE_PATH)
            if ANALYSIS_DATABASE_PATH.is_file() else None
        )
        cls.detail_hash = (
            sha256_file(DETAIL_DATABASE_PATH)
            if DETAIL_DATABASE_PATH.is_file() else None
        )

    @classmethod
    def tearDownClass(cls):
        if cls.analysis_hash is not None and sha256_file(ANALYSIS_DATABASE_PATH) != cls.analysis_hash:
            raise AssertionError("历史分析数据库在测试中发生变化")
        if cls.detail_hash is not None and sha256_file(DETAIL_DATABASE_PATH) != cls.detail_hash:
            raise AssertionError("详细比赛数据库在测试中发生变化")

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.database_path = self.root / "pregame.sqlite"
        builder.build_database(self.database_path, SCHEMA_PATH)

    def tearDown(self):
        self.temp_directory.cleanup()

    def connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def test_01_schema_has_three_snapshot_tables_and_fetch_log(self):
        with closing(self.connect()) as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue(
            {
                "injury_snapshots",
                "lineup_snapshots",
                "market_snapshots",
                "source_fetch_runs",
            }.issubset(names)
        )

    def test_02_every_snapshot_table_has_required_time_and_status_fields(self):
        required = {
            "game_id",
            "source",
            "source_record_id",
            "source_published_at",
            "captured_at",
            "cutoff_time",
            "raw_status",
            "normalized_status",
        }
        with closing(self.connect()) as connection:
            for table in (
                "injury_snapshots",
                "lineup_snapshots",
                "market_snapshots",
            ):
                columns = {
                    row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                }
                self.assertTrue(required.issubset(columns), (table, required - columns))

    def test_03_eligible_query_requires_both_timestamps_at_or_before_cutoff(self):
        cutoff = "2026-01-13T19:00:00Z"
        records = [
            collector.make_injury_record(
                game_id="g1",
                source_record_id="eligible",
                source_published_at="2026-01-13T17:00:00Z",
                captured_at="2026-01-13T18:00:00Z",
                cutoff_time=cutoff,
                player_name="Player A",
                team_name="Team A",
                raw_status="Questionable",
                normalized_status="available",
            ),
            collector.make_injury_record(
                game_id="g1",
                source_record_id="published-late",
                source_published_at="2026-01-13T19:01:00Z",
                captured_at="2026-01-13T18:00:00Z",
                cutoff_time=cutoff,
                player_name="Player B",
                team_name="Team A",
                raw_status="Out",
                normalized_status="available",
            ),
            collector.make_injury_record(
                game_id="g1",
                source_record_id="captured-late",
                source_published_at="2026-01-13T17:00:00Z",
                captured_at="2026-01-13T19:01:00Z",
                cutoff_time=cutoff,
                player_name="Player C",
                team_name="Team A",
                raw_status="Out",
                normalized_status="available",
            ),
            collector.make_injury_record(
                game_id="g1",
                source_record_id="missing-published",
                source_published_at=None,
                captured_at="2026-01-13T18:00:00Z",
                cutoff_time=cutoff,
                player_name="Player D",
                team_name="Team A",
                raw_status="Unknown",
                normalized_status="unavailable",
            ),
        ]
        collector.insert_records(self.database_path, "injury_snapshots", records)
        eligible = collector.load_agent_eligible_records(
            self.database_path, "injury_snapshots", "g1", cutoff
        )
        self.assertEqual([row["source_record_id"] for row in eligible], ["eligible"])

    def test_04_expected_and_confirmed_lineups_are_distinct(self):
        common = {
            "game_id": "g1",
            "source_published_at": "2026-01-13T18:00:00Z",
            "captured_at": "2026-01-13T18:05:00Z",
            "cutoff_time": "2026-01-13T19:00:00Z",
            "team_name": "Team A",
            "player_name": "Player A",
            "raw_status": "listed",
            "normalized_status": "available",
        }
        records = [
            collector.make_lineup_record(
                source_record_id="expected-1", lineup_kind="expected", **common
            ),
            collector.make_lineup_record(
                source_record_id="confirmed-1", lineup_kind="confirmed", **common
            ),
        ]
        collector.insert_records(self.database_path, "lineup_snapshots", records)
        with closing(self.connect()) as connection:
            kinds = [
                row[0]
                for row in connection.execute(
                    "SELECT lineup_kind FROM lineup_snapshots ORDER BY lineup_kind"
                )
            ]
        self.assertEqual(kinds, ["confirmed", "expected"])

    def test_05_market_record_keeps_bookmaker_spread_total_and_update_time(self):
        record = collector.make_market_record(
            game_id="g1",
            source_record_id="book-1",
            source_published_at="2026-01-13T18:00:00Z",
            captured_at="2026-01-13T18:05:00Z",
            cutoff_time="2026-01-13T19:00:00Z",
            bookmaker="Example Book",
            home_spread=-3.5,
            total=224.5,
            raw_status="open",
            normalized_status="available",
        )
        collector.insert_records(self.database_path, "market_snapshots", [record])
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT bookmaker, home_spread, total, source_published_at "
                "FROM market_snapshots"
            ).fetchone()
        self.assertEqual(dict(row), {
            "bookmaker": "Example Book",
            "home_spread": -3.5,
            "total": 224.5,
            "source_published_at": "2026-01-13T18:00:00Z",
        })

    def test_06_record_factory_rejects_missing_required_provenance(self):
        with self.assertRaisesRegex(collector.CollectionError, "source_record_id"):
            collector.make_market_record(
                game_id="g1",
                source_record_id="",
                source_published_at="2026-01-13T18:00:00Z",
                captured_at="2026-01-13T18:05:00Z",
                cutoff_time="2026-01-13T19:00:00Z",
                bookmaker="Example Book",
                home_spread=-3.5,
                total=224.5,
                raw_status="open",
                normalized_status="available",
            )

    def test_07_collection_with_fake_lineup_response_archives_raw_without_fabrication(self):
        raw_dir = self.root / "raw"
        log_path = self.root / "collection_log.json"

        def fake_fetch(url, timeout_seconds, api_key=None):
            return collector.FetchResponse(
                status_code=200,
                body=b"<html><title>NBA Daily Lineups</title></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )

        result = collector.collect_sources(
            game_id="g1",
            cutoff_time="2026-01-13T19:00:00Z",
            database_path=self.database_path,
            raw_dir=raw_dir,
            log_path=log_path,
            sources=["lineups"],
            fetcher=fake_fetch,
        )
        self.assertEqual(result["sources"]["lineups"]["fetch_status"], "success")
        self.assertEqual(result["sources"]["lineups"]["normalized_records"], 0)
        self.assertEqual(result["sources"]["lineups"]["availability"], "unavailable")
        self.assertTrue(Path(result["sources"]["lineups"]["raw_path"]).is_file())
        self.assertTrue(log_path.is_file())
        with closing(self.connect()) as connection:
            count = connection.execute("SELECT COUNT(*) FROM lineup_snapshots").fetchone()[0]
        self.assertEqual(count, 0)

    def test_08_collection_log_records_known_blockers_and_redacts_credentials(self):
        result = collector.collect_sources(
            game_id="g1",
            cutoff_time="2026-01-13T19:00:00Z",
            database_path=self.database_path,
            raw_dir=self.root / "raw",
            log_path=self.root / "collection_log.json",
            sources=[],
            injury_preflight_timeout=True,
            odds_api_key="",
        )
        self.assertEqual(result["sources"]["injuries"]["fetch_status"], "timeout")
        self.assertEqual(result["sources"]["market"]["fetch_status"], "credentials_missing")
        self.assertNotIn("apiKey", json.dumps(result))

    def test_09_build_refuses_to_overwrite_existing_database(self):
        with self.assertRaisesRegex(builder.DatabaseBuildError, "已经存在"):
            builder.build_database(self.database_path, SCHEMA_PATH)


if __name__ == "__main__":
    unittest.main()
