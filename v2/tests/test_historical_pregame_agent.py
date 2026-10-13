from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock


PROJECT_DIR = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_DIR / "v2" / "agent" / "generate_historical_pregame_analysis.py"
ANALYSIS_DATABASE_PATH = (
    PROJECT_DIR / "analysis_v1" / "data" / "courtvision_analysis.sqlite"
)
DETAIL_DATABASE_PATH = PROJECT_DIR / "v2" / "db" / "courtvision_v2.sqlite"
SNAPSHOT_DATABASE_PATH = (
    PROJECT_DIR / "v2" / "db" / "courtvision_pregame_snapshots.sqlite"
)
REJECTED_RESPONSE_PATH = (
    PROJECT_DIR
    / "v2"
    / "outputs"
    / "game_0022500564_historical_pregame_rejected_evidence_fixed_v2.json"
)
V6_REJECTED_RESPONSE_PATH = (
    PROJECT_DIR
    / "v2"
    / "outputs"
    / "game_0022500564_historical_pregame_rejected_player_evidence_integrated_v6.json"
)
V6_WRITER_INPUT_PATH = (
    PROJECT_DIR
    / "v2"
    / "outputs"
    / "game_0022500564_historical_pregame_writer_input_player_evidence_integrated_v6.json"
)
V6_REJECTED_SHA256 = "f3bcfbcdb893b064a38eb83df1dfefabfa8c513581caa9b8ce736f4607fa5edd"
GAME_ID = "0022500564"
CUTOFF = "2026-01-13T19:00:00"


def load_agent_module():
    spec = importlib.util.spec_from_file_location("historical_pregame_agent", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = load_agent_module()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def planner_response(requested=None):
    return agent.ApiResult(
        content=json.dumps(
            {"optional_information_requests": requested or []},
            ensure_ascii=False,
        ),
        finish_reason="stop",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )


def writer_response():
    return agent.ApiResult(
        content=json.dumps(
            {
                "final_predicted_home_margin": -3.25,
                "final_predicted_total": 224.5,
                "margin_adjustment_reason": "客队近期状态与赛前评级更占优势，因此向客队方向小幅调整。",
                "total_adjustment_reason": "双方近期节奏和得失分信息方向并不完全一致，因此仅作克制调整。",
                "primary_evidence_refs": [
                    "home.pregame_win_rate_roll5",
                    "away.pregame_win_rate_roll5",
                    "home.pregame_net_rating_roll5",
                    "away.pregame_net_rating_roll5",
                ],
                "injury_impact_assessments": [
                    {
                        "record_ref": "injuries.7",
                        "impact_inference": (
                            "健康相关缺阵可能影响人员可用性，但缺少贡献与预计上场时间依据，"
                            "因此未单独调整预测。"
                        ),
                        "prediction_treatment": "not_separately_adjusted",
                    },
                    {
                        "record_ref": "injuries.8",
                        "impact_inference": (
                            "健康相关缺阵可能影响人员可用性，但缺少贡献与预计上场时间依据，"
                            "因此未单独调整预测。"
                        ),
                        "prediction_treatment": "not_separately_adjusted",
                    },
                    {
                        "record_ref": "injuries.2",
                        "impact_inference": (
                            "出赛存疑带来赛前可用性不确定，但无法确认最终能否出场，"
                            "因此未单独调整预测。"
                        ),
                        "prediction_treatment": "not_separately_adjusted",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        finish_reason="stop",
        usage={"prompt_tokens": 100, "completion_tokens": 50},
    )


def load_v6_writer_evidence():
    audit = json.loads(V6_WRITER_INPUT_PATH.read_text(encoding="utf-8"))
    evidence = agent._extract_writer_evidence_from_messages(audit["agent_messages"])
    # The persisted writer message contains the deterministic availability view
    # rather than the pre-render internal alias used by the validator. Restore
    # that alias from the same saved input so the regression remains fully
    # offline and does not read the snapshot database.
    evidence["verified_pregame_injuries"] = evidence["player_availability_context"]
    return evidence


class HistoricalPregameAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analysis_hash = sha256_file(ANALYSIS_DATABASE_PATH)
        cls.detail_hash = sha256_file(DETAIL_DATABASE_PATH)
        cls.snapshot_hash = sha256_file(SNAPSHOT_DATABASE_PATH)

    @classmethod
    def tearDownClass(cls):
        if sha256_file(ANALYSIS_DATABASE_PATH) != cls.analysis_hash:
            raise AssertionError("历史分析数据库在测试期间发生变化")
        if sha256_file(DETAIL_DATABASE_PATH) != cls.detail_hash:
            raise AssertionError("详细比赛数据库在测试期间发生变化")
        if sha256_file(SNAPSHOT_DATABASE_PATH) != cls.snapshot_hash:
            raise AssertionError("赛前信息快照数据库在测试期间发生变化")

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.outputs_dir = Path(self.temp_directory.name)

    def tearDown(self):
        self.temp_directory.cleanup()

    def test_01_cutoff_must_be_strictly_before_scheduled_time(self):
        identity = agent.load_target_identity(GAME_ID, ANALYSIS_DATABASE_PATH)
        checked = agent.validate_cutoff("2026-01-13T19:29:59", identity)
        self.assertEqual(checked.isoformat(timespec="seconds"), "2026-01-13T19:29:59")
        with self.assertRaisesRegex(agent.PregameAgentError, "早于比赛时间"):
            agent.validate_cutoff("2026-01-13T19:30:00", identity)

    def test_02_read_only_tools_exclude_target_outcome_and_postgame_fields(self):
        tools = agent.PregameReadOnlyTools(ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF)
        evidence, trace = tools.build_core_evidence()
        serialized = json.dumps(evidence, ensure_ascii=False)
        for forbidden in (
            "home_score",
            "away_score",
            "home_win",
            "point_margin",
            "total_points",
            "points_for",
            "points_against",
            "win",
        ):
            self.assertNotIn(f'"{forbidden}"', serialized)
        self.assertNotIn(GAME_ID, {row["game_id"] for row in evidence["recent_games"]["home"]})
        self.assertNotIn(GAME_ID, {row["game_id"] for row in evidence["recent_games"]["away"]})
        self.assertGreaterEqual(len(trace), 6)

    def test_03_all_prior_games_are_strictly_before_cutoff(self):
        tools = agent.PregameReadOnlyTools(ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF)
        evidence, _ = tools.build_core_evidence()
        for side in ("home", "away"):
            for game in evidence["recent_games"][side]:
                self.assertLess(game["game_date"], "2026-01-13 19:00:00")

    def test_04_mutating_target_postgame_values_does_not_change_evidence(self):
        original, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        copied = self.outputs_dir / "mutated.sqlite"
        shutil.copyfile(ANALYSIS_DATABASE_PATH, copied)
        connection = sqlite3.connect(copied)
        try:
            connection.execute(
                "UPDATE dim_game SET home_score=1, away_score=199, home_win=0, "
                "point_margin=-198, total_points=200 WHERE game_id=?",
                (GAME_ID,),
            )
            connection.execute(
                "UPDATE fact_team_game SET win=1-win, points_for=1, "
                "points_against=199, point_margin=-198 WHERE game_id=?",
                (GAME_ID,),
            )
            connection.commit()
        finally:
            connection.close()
        changed, _ = agent.PregameReadOnlyTools(
            copied, GAME_ID, CUTOFF
        ).build_core_evidence()
        self.assertEqual(original, changed)

    def test_05_optional_information_is_unavailable_without_timestamp_proof(self):
        availability = agent.assess_optional_information(
            ["injuries", "starters", "market"]
        )
        self.assertEqual(set(availability), {"injuries", "starters", "market"})
        self.assertTrue(all(item["status"] == "unavailable" for item in availability.values()))
        self.assertNotIn("market_total", json.dumps(availability))

    def test_05b_verified_official_archive_injuries_are_loaded_read_only(self):
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        injuries = availability["injuries"]
        self.assertEqual(injuries["status"], "available_historical_archive")
        self.assertEqual(len(injuries["records"]), 10)
        self.assertTrue(
            all(row["game_id"] == GAME_ID for row in injuries["records"])
        )
        self.assertTrue(
            all(
                row["source_published_at"] <= injuries["cutoff_time_utc"]
                for row in injuries["records"]
            )
        )
        self.assertIn(
            ("Norman Powell", "Questionable"),
            {
                (row["player_name"], row["availability_status"])
                for row in injuries["records"]
            },
        )

    def test_05c_program_exposes_all_report_records_without_prefilter(self):
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        records = availability["injuries"]["records"]
        candidates = agent.build_injury_analysis_candidates(records)
        self.assertEqual(len(records), 10)
        self.assertEqual(
            [candidate["record_ref"] for candidate in candidates],
            [f"injuries.{index}" for index in range(10)],
        )
        self.assertEqual(
            [candidate["player_name"] for candidate in candidates],
            [record["player_name"] for record in records],
        )
        self.assertEqual(
            len(candidates),
            10,
        )
        self.assertEqual(
            {candidate["selection_scope"] for candidate in candidates},
            {"all_reported_records_visible_to_agent"},
        )
        self.assertEqual(
            {candidate["fact_classification"] for candidate in candidates},
            {
                "health_related_out",
                "non_injury_out",
                "questionable_availability",
                "confirmed_available",
            },
        )

    def test_05d_program_builds_complete_player_availability_context(self):
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        records = availability["injuries"]["records"]
        context = agent.build_player_availability_context(records)
        summary = agent.summarize_player_availability(context)

        self.assertEqual(len(context), 10)
        self.assertEqual(summary["total_records"], 10)
        self.assertEqual(summary["confirmed_out_count"], 6)
        self.assertEqual(summary["questionable_count"], 1)
        self.assertEqual(summary["available_count"], 3)
        self.assertEqual(summary["health_related_out_count"], 2)
        self.assertEqual(summary["non_health_related_out_count"], 4)

        out_records = [
            item for item in context if item["availability_group"] == "confirmed_out"
        ]
        self.assertEqual(len(out_records), 6)
        self.assertEqual(
            {item["player_name"] for item in out_records},
            {
                "Jahmir Young",
                "Myron Gardner",
                "Terry Rozier",
                "Vladislav Goldin",
                "Jalen Green",
                "Jamaree Bouyea",
            },
        )
        self.assertEqual(
            {
                item["player_name"]
                for item in out_records
                if item["is_health_related_reason"]
            },
            {"Jalen Green", "Jamaree Bouyea"},
        )
        self.assertEqual(
            {
                item["player_name"]: item["reason_classification"]
                for item in out_records
                if item["is_health_related_reason"]
            },
            {
                "Jalen Green": "injury_illness",
                "Jamaree Bouyea": "concussion_protocol",
            },
        )
        self.assertEqual(
            {
                item["player_name"]: item["reason_classification"]
                for item in out_records
                if not item["is_health_related_reason"]
            },
            {
                "Jahmir Young": "g_league_two_way",
                "Myron Gardner": "g_league_two_way",
                "Terry Rozier": "not_with_team",
                "Vladislav Goldin": "g_league_two_way",
            },
        )
        self.assertEqual(
            [
                item["player_name"]
                for item in context
                if item["availability_group"] == "questionable"
            ],
            ["Norman Powell"],
        )
        self.assertEqual(
            {
                item["player_name"]
                for item in context
                if item["availability_group"] == "available"
            },
            {"Pelle Larsson", "Tyler Herro", "Jordan Goodwin"},
        )
        for record, item in zip(records, context, strict=True):
            self.assertEqual(item["availability_status"], record["availability_status"])
            self.assertEqual(item["reason"], record["reason"])
            self.assertEqual(item["source"], record["source"])
            self.assertEqual(item["source_url"], record["source_url"])
            self.assertEqual(item["source_published_at"], record["source_published_at"])
            self.assertEqual(item["captured_at"], record["captured_at"])

    def test_05e_reason_classifier_covers_explicit_out_reason_types(self):
        cases = {
            "Injury/Illness - Ankle; Sprain": "injury_illness",
            "Concussion Protocol": "concussion_protocol",
            "G League - On Assignment": "g_league",
            "Two-Way": "two_way",
            "Not With Team": "not_with_team",
            "Personal Reasons": "other_explicit_reason",
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                record = {"availability_status": "Out", "reason": reason}
                self.assertEqual(
                    agent.classify_availability_group(record), "confirmed_out"
                )
                self.assertEqual(
                    agent.classify_availability_reason(record), expected
                )

    def test_05f_writer_receives_complete_availability_and_all_detail_candidates(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        candidates = agent.build_injury_analysis_candidates(
            evidence["verified_pregame_injuries"]
        )
        role_context, role_source = agent.load_player_role_evidence(
            agent.PLAYER_ROLE_EVIDENCE_PATH,
            agent.PLAYER_ROLE_EVIDENCE_MANIFEST_PATH,
            evidence["verified_pregame_injuries"],
            GAME_ID,
            CUTOFF,
        )
        messages = agent.build_writer_messages(
            evidence,
            availability,
            candidates,
            role_context,
            role_source,
        )
        user_content = messages[1]["content"]
        serialized = user_content.removeprefix("<pregame_evidence>\n").removesuffix(
            "\n</pregame_evidence>"
        )
        writer_evidence = json.loads(serialized)
        self.assertNotIn("verified_pregame_injuries", writer_evidence)
        self.assertEqual(writer_evidence["injury_fact_record_count"], 10)
        availability_context = writer_evidence["player_availability_context"]
        self.assertEqual(len(availability_context), 10)
        self.assertEqual(
            [item["record_ref"] for item in availability_context],
            [f"injuries.{index}" for index in range(10)],
        )
        self.assertEqual(
            writer_evidence["player_availability_summary"],
            {
                "total_records": 10,
                "confirmed_out_count": 6,
                "questionable_count": 1,
                "available_count": 3,
                "other_reported_status_count": 0,
                "health_related_out_count": 2,
                "non_health_related_out_count": 4,
                "confirmed_out_refs": [
                    "injuries.0",
                    "injuries.1",
                    "injuries.4",
                    "injuries.6",
                    "injuries.7",
                    "injuries.8",
                ],
                "questionable_refs": ["injuries.2"],
                "available_refs": ["injuries.3", "injuries.5", "injuries.9"],
                "other_reported_status_refs": [],
            },
        )
        self.assertEqual(
            [item["record_ref"] for item in writer_evidence["injury_analysis_candidates"]],
            [f"injuries.{index}" for index in range(10)],
        )
        self.assertEqual(writer_evidence["injury_analysis_detail_limit"], 4)
        self.assertIn("全部官方报告记录", writer_evidence["injury_analysis_selection_policy"])
        self.assertTrue(
            all(
                item["selection_scope"] == "all_reported_records_visible_to_agent"
                for item in writer_evidence["injury_analysis_candidates"]
            )
        )
        self.assertNotIn(
            "records", writer_evidence["optional_information_availability"]["injuries"]
        )
        self.assertFalse(
            any(key.startswith("injuries.") for key in writer_evidence["evidence_reference_catalog"])
        )
        for required in (
            "Jahmir Young",
            "Myron Gardner",
            "Pelle Larsson",
            "Terry Rozier",
            "Tyler Herro",
            "Vladislav Goldin",
            "Jordan Goodwin",
            "G League - Two-Way",
            "Not With Team",
        ):
            self.assertIn(required, user_content)
        self.assertEqual(len(writer_evidence["injury_analysis_candidates"]), 10)
        self.assertIn(
            "未出现在官方报告中的球员不能据此视为确定出场或进入激活名单",
            writer_evidence["explicit_exclusions"],
        )
        self.assertIn("完整球员可用性上下文", messages[0]["content"])
        self.assertIn("确定无法出场", messages[0]["content"])
        self.assertIn("进入激活名单", messages[0]["content"])
        with self.assertRaisesRegex(agent.PregameAgentError, "完整球员角色证据"):
            agent.build_writer_messages(evidence, availability, candidates)
        with self.assertRaisesRegex(agent.PregameAgentError, "预筛选"):
            agent.build_writer_messages(
                evidence,
                availability,
                candidates[:3],
                role_context,
                role_source,
            )

    def test_05g_model_selects_subset_without_program_prefilter(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        candidates = agent.build_injury_analysis_candidates(
            evidence["verified_pregame_injuries"]
        )
        rejected = json.loads(REJECTED_RESPONSE_PATH.read_text(encoding="utf-8"))
        rejected_response = rejected["model_response"]["content"]
        rejected_value = json.loads(rejected_response)
        self.assertEqual(len(rejected_value["injury_impact_assessments"]), 7)
        with self.assertRaisesRegex(agent.PregameAgentError, "最多展开 4 条"):
            agent.validate_writer_response(rejected_response, evidence, candidates)

        by_ref = {
            item["record_ref"]: item
            for item in rejected_value["injury_impact_assessments"]
        }
        candidate_refs = [candidate["record_ref"] for candidate in candidates]
        selected_refs = ["injuries.7", "injuries.8", "injuries.2"]
        projected = [by_ref[record_ref] for record_ref in selected_refs]
        self.assertEqual(len(candidate_refs), 10)
        self.assertEqual(len(projected), 3)
        self.assertNotIn("常规轮换", json.dumps(projected, ensure_ascii=False))
        rejected_value["injury_impact_assessments"] = projected
        validated = agent.validate_writer_response(
            json.dumps(rejected_value, ensure_ascii=False), evidence, candidates
        )
        self.assertEqual(
            [item["record_ref"] for item in validated["injury_impact_assessments"]],
            selected_refs,
        )
        self.assertTrue(
            set(selected_refs).issubset(set(candidate_refs))
        )

    def test_05h_actual_agent_input_contains_all_role_evidence_and_no_postgame_rows(self):
        audit_path = agent.build_historical_pregame_agent_input_audit(
            GAME_ID,
            CUTOFF,
            outputs_dir=self.outputs_dir,
            output_tag="player_evidence_audit_test",
        )
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(audit["status"], "audit_only")
        self.assertFalse(audit["deepseek_called"])
        self.assertEqual(audit["api_request_count"], 0)
        self.assertEqual(audit["input_validation"]["complete_player_role_evidence_count"], 10)
        self.assertEqual(audit["input_validation"]["candidate_count"], 10)
        cutoff_audit = audit["cutoff_audit"]
        self.assertTrue(cutoff_audit["exact_timestamp_evidence"]["validated"])
        self.assertEqual(
            cutoff_audit["player_game_statistics"]["filter_granularity"],
            "calendar_date",
        )
        self.assertEqual(
            cutoff_audit["player_game_statistics"]["filter_rule"],
            "GAME_DATE < 2026-01-13 and GAME_ID != 0022500564",
        )
        self.assertFalse(
            cutoff_audit["player_game_statistics"]["exact_intraday_cutoff_validated"]
        )
        self.assertEqual(
            cutoff_audit["team_game_fields"]["source_timezone_status"],
            "unverified",
        )
        self.assertFalse(
            cutoff_audit["team_game_fields"]["exact_eastern_time_cutoff_validated"]
        )
        self.assertNotIn("strict_cutoff_validated", audit["input_validation"])

        writer_evidence = agent._extract_writer_evidence_from_messages(
            audit["agent_messages"]
        )
        role_context = writer_evidence["player_role_evidence_context"]
        role_players = {
            player["player_name"]: player for player in role_context["players"]
        }
        source_path = agent.PLAYER_ROLE_EVIDENCE_PATH
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source_players = {
            player["player_name"]: player for player in source["players"]
        }
        availability_records = {
            record["player_name"]: record
            for record in writer_evidence["player_availability_context"]
        }
        for name in ("Norman Powell", "Tyler Herro", "Jalen Green"):
            context_player = role_players[name]
            projected = {
                key: value
                for key, value in context_player.items()
                if key not in {"record_ref", "official_report_record"}
            }
            self.assertEqual(projected, source_players[name])
            self.assertEqual(
                context_player["official_availability"]["status"],
                availability_records[name]["availability_status"],
            )
            self.assertEqual(
                context_player["official_availability"]["reason"],
                availability_records[name]["reason"],
            )
            self.assertEqual(
                context_player["official_availability"]["source_url"],
                availability_records[name]["source_url"],
            )
            self.assertEqual(
                context_player["official_availability"]["source_report_time_local"],
                source["injury_report"]["source_report_time_local"],
            )

        powell = role_players["Norman Powell"]
        self.assertEqual(powell["official_availability"]["status"], "Questionable")
        self.assertEqual(
            powell["official_availability"]["reason"],
            "Injury/Illness - Low Back; Soreness",
        )
        self.assertEqual(powell["current_season"]["team_stints"][0]["games_played"], 33)
        self.assertEqual(powell["current_season"]["team_stints"][0]["starts"], 33)
        self.assertEqual(powell["recent_team_window"]["appearance_count_in_recent_team_window"], 9)

        herro = role_players["Tyler Herro"]
        self.assertEqual(herro["official_availability"]["status"], "Available")
        self.assertEqual(
            herro["recent_team_window"]["appearance_count_in_recent_team_window"],
            3,
        )
        self.assertIn("不代表之后实际登场", audit["agent_messages"][0]["content"])

        green = role_players["Jalen Green"]
        self.assertEqual(green["official_availability"]["status"], "Out")
        self.assertEqual(
            green["official_availability"]["reason"],
            "Injury/Illness - Right Hamstring; Strain",
        )
        self.assertEqual(green["recent_team_window"]["appearance_count_in_recent_team_window"], 0)
        self.assertEqual(
            green["recent_team_window"]["consecutive_report_team_games_without_appearance_record"],
            29,
        )
        self.assertIn("不要重复扣减", audit["agent_messages"][0]["content"])

        for side in ("home", "away"):
            self.assertNotIn(
                GAME_ID,
                {row["game_id"] for row in writer_evidence["recent_games"][side]},
            )
        serialized = json.dumps(audit["agent_messages"], ensure_ascii=False)
        for forbidden in (
            '"actual_score"',
            '"final_score"',
            '"final_home_score"',
            '"final_away_score"',
            '"target_outcome"',
            '"play_by_play"',
            '"postgame"',
            '"actual_home_margin"',
            '"actual_total"',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_06_planner_rejects_non_allowlisted_request(self):
        with self.assertRaisesRegex(agent.PregameAgentError, "不允许"):
            agent.validate_planner_response(
                json.dumps({"optional_information_requests": ["web_search"]})
            )

    def test_07_writer_rejects_inconsistent_or_unreferenced_content(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        invalid = json.loads(writer_response().content)
        invalid["primary_evidence_refs"] = ["target.actual_score"]
        with self.assertRaisesRegex(agent.PregameAgentError, "证据引用"):
            agent.validate_writer_response(json.dumps(invalid, ensure_ascii=False), evidence)
        invalid = json.loads(writer_response().content)
        invalid["final_predicted_total"] = float("nan")
        with self.assertRaisesRegex(agent.PregameAgentError, "有限数字"):
            agent.validate_writer_response(json.dumps(invalid), evidence)
        invalid = json.loads(writer_response().content)
        invalid["margin_adjustment_reason"] = (
            "主队主力后卫缺阵，因此对主队分差作出明显下调。"
        )
        with self.assertRaisesRegex(agent.PregameAgentError, "位置或角色推断"):
            agent.validate_writer_response(json.dumps(invalid, ensure_ascii=False), evidence)

    def test_07b_writer_rejects_role_claim_inside_injury_inference(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        invalid = json.loads(writer_response().content)
        invalid["injury_impact_assessments"][0]["impact_inference"] = (
            "Norman Powell 是主力后卫，其状态会决定球队表现。"
        )
        with self.assertRaisesRegex(agent.PregameAgentError, "位置或角色推断"):
            agent.validate_writer_response(json.dumps(invalid, ensure_ascii=False), evidence)

    def test_07c_writer_rejects_questionable_as_confirmed_absence(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        invalid = json.loads(writer_response().content)
        invalid["injury_impact_assessments"][2]["impact_inference"] = (
            "该候选已经确定缺阵，因此球队人员一定减少。"
        )
        with self.assertRaisesRegex(agent.PregameAgentError, "Questionable"):
            agent.validate_writer_response(json.dumps(invalid, ensure_ascii=False), evidence)

    def test_07ca_questionable_conditionals_are_not_confirmed_absence(self):
        self.assertFalse(
            agent._questionable_contains_definite_absence(
                "若 Norman Powell 无法出场，球队可用人员可能减少。"
            )
        )
        self.assertFalse(
            agent._questionable_contains_definite_absence(
                "如果 Norman Powell 最终缺席，影响仍然无法量化。"
            )
        )
        evidence = load_v6_writer_evidence()
        role_context = evidence["player_role_evidence_context"]
        conditional_inferences = (
            "出赛状态仍不确定；若无法出场，球队可用人员可能减少，但不单独调整预测。",
            "能否出赛尚未确定；如果最终缺席，球队需要调整人员安排，但影响未知。",
            "当前状态仍待确认；假如赛前未能出战，可能影响人员可用性，但不单独调整。",
            "出赛仍然存疑；即使最终缺阵，也缺少证据判断具体影响。",
        )
        for inference in conditional_inferences:
            with self.subTest(inference=inference):
                value = json.loads(writer_response().content)
                value["injury_impact_assessments"] = [
                    {
                        "record_ref": "injuries.2",
                        "impact_inference": inference,
                        "prediction_treatment": "not_separately_adjusted",
                    }
                ]
                validated = agent.validate_writer_response(
                    json.dumps(value, ensure_ascii=False),
                    evidence,
                    evidence["injury_analysis_candidates"],
                    player_role_evidence_context=role_context,
                )
                self.assertEqual(
                    validated["injury_impact_assessments"][0]["impact_inference"],
                    inference,
                )

    def test_07cb_questionable_definite_rewrites_are_rejected(self):
        self.assertTrue(
            agent._questionable_contains_definite_absence(
                "Norman Powell 将缺席本场比赛。"
            )
        )
        self.assertTrue(
            agent._questionable_contains_definite_absence(
                "Norman Powell 无法出场。"
            )
        )
        evidence = load_v6_writer_evidence()
        definite_inferences = (
            "报告中的状态仍有不确定字样，但该球员将缺席本场，球队人员确定减少。",
            "虽然此前状态不确定，但该球员无法出场，球队需要调整人员安排。",
            "该球员已经确认缺阵，先前的不确定状态已经结束。",
            "尽管报告状态存疑，该球员肯定不会出战，球队人员将会减少。",
            "即使报告状态仍然存疑，该球员也将缺席，球队人员确定减少。",
        )
        for inference in definite_inferences:
            with self.subTest(inference=inference):
                value = json.loads(writer_response().content)
                value["injury_impact_assessments"] = [
                    {
                        "record_ref": "injuries.2",
                        "impact_inference": inference,
                        "prediction_treatment": "not_separately_adjusted",
                    }
                ]
                with self.assertRaisesRegex(agent.PregameAgentError, "Questionable"):
                    agent.validate_writer_response(
                        json.dumps(value, ensure_ascii=False),
                        evidence,
                        evidence["injury_analysis_candidates"],
                        player_role_evidence_context=evidence[
                            "player_role_evidence_context"
                        ],
                    )

    def test_07cc_unobserved_absence_cause_is_rejected(self):
        evidence = load_v6_writer_evidence()
        unsupported_inferences = (
            "状态仍不确定，近期有因伤病缺阵记录；若无法出场可能影响人员可用性。",
            "状态仍不确定，最近曾因轮休未出场；如果最终缺席，影响仍然未知。",
            "状态仍不确定，此前停赛导致缺席；若无法出赛，影响仍然无法量化。",
            "状态仍不确定，过去因为个人原因未出战；假如最终缺席，影响仍未知。",
        )
        for inference in unsupported_inferences:
            with self.subTest(inference=inference):
                value = json.loads(writer_response().content)
                value["injury_impact_assessments"] = [
                    {
                        "record_ref": "injuries.2",
                        "impact_inference": inference,
                        "prediction_treatment": "not_separately_adjusted",
                    }
                ]
                with self.assertRaisesRegex(
                    agent.PregameAgentError, "无证据缺席原因"
                ):
                    agent.validate_writer_response(
                        json.dumps(value, ensure_ascii=False),
                        evidence,
                        evidence["injury_analysis_candidates"],
                        player_role_evidence_context=evidence[
                            "player_role_evidence_context"
                        ],
                    )

    def test_07cd_role_language_depends_on_player_usage_evidence(self):
        evidence = load_v6_writer_evidence()
        role_context = evidence["player_role_evidence_context"]
        supported = json.loads(writer_response().content)
        supported["injury_impact_assessments"] = [
            {
                "record_ref": "injuries.2",
                "impact_inference": (
                    "本赛季样本中的首发、分钟和出场频率呈现较高使用程度，"
                    "接近主力使用程度；当前状态仍不确定，若最终缺席仍无法量化影响。"
                ),
                "prediction_treatment": "not_separately_adjusted",
            }
        ]
        agent.validate_writer_response(
            json.dumps(supported, ensure_ascii=False),
            evidence,
            evidence["injury_analysis_candidates"],
            player_role_evidence_context=role_context,
        )

        unsupported = json.loads(writer_response().content)
        unsupported["injury_impact_assessments"] = [
            {
                "record_ref": "injuries.8",
                "impact_inference": (
                    "报告确认该球员无法出场，缺阵可能削弱球队轮换深度，"
                    "但影响程度未知。"
                ),
                "prediction_treatment": "not_separately_adjusted",
            }
        ]
        with self.assertRaisesRegex(agent.PregameAgentError, "位置或角色推断"):
            agent.validate_writer_response(
                json.dumps(unsupported, ensure_ascii=False),
                evidence,
                evidence["injury_analysis_candidates"],
                player_role_evidence_context=role_context,
            )

    def test_07ce_v6_real_rejected_response_reports_both_true_errors(self):
        self.assertEqual(sha256_file(V6_REJECTED_RESPONSE_PATH), V6_REJECTED_SHA256)
        rejected = json.loads(V6_REJECTED_RESPONSE_PATH.read_text(encoding="utf-8"))
        evidence = load_v6_writer_evidence()
        with self.assertRaises(agent.PregameAgentError) as captured:
            agent.validate_writer_response(
                rejected["model_response"]["content"],
                evidence,
                evidence["injury_analysis_candidates"],
                player_role_evidence_context=evidence["player_role_evidence_context"],
            )
        message = str(captured.exception)
        self.assertIn("injuries.2", message)
        self.assertIn("无证据缺席原因", message)
        self.assertIn("injuries.8", message)
        self.assertIn("位置或角色推断", message)
        self.assertNotIn("Questionable 只代表出赛存疑", message)
        self.assertEqual(sha256_file(V6_REJECTED_RESPONSE_PATH), V6_REJECTED_SHA256)

    def test_07d_writer_rejects_non_injury_out_record(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        invalid = json.loads(writer_response().content)
        invalid["injury_impact_assessments"][2]["record_ref"] = "injuries.4"
        invalid["injury_impact_assessments"][2]["impact_inference"] = (
            "该记录属于伤病缺阵，因此会削弱球队人员可用性。"
        )
        with self.assertRaisesRegex(agent.PregameAgentError, "非伤病"):
            agent.validate_writer_response(json.dumps(invalid, ensure_ascii=False), evidence)

    def test_07e_writer_rejects_separate_numeric_injury_adjustment(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        invalid = json.loads(writer_response().content)
        invalid["injury_impact_assessments"][1]["prediction_treatment"] = (
            "separate_numeric_adjustment"
        )
        with self.assertRaisesRegex(agent.PregameAgentError, "单独量化"):
            agent.validate_writer_response(json.dumps(invalid, ensure_ascii=False), evidence)

    def test_07f_writer_rejects_player_claim_inside_injury_inference(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        invalid = json.loads(writer_response().content)
        invalid["injury_impact_assessments"][0]["impact_inference"] = (
            "LeBron James 的情况可能改变球队人员可用性，但当前无法量化影响。"
        )
        with self.assertRaisesRegex(agent.PregameAgentError, "球员事实"):
            agent.validate_writer_response(json.dumps(invalid, ensure_ascii=False), evidence)

    def test_07g_writer_rejects_status_mismatch_inside_injury_inference(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        invalid = json.loads(writer_response().content)
        invalid["injury_impact_assessments"][0]["impact_inference"] = (
            "该候选被标记为 Questionable，人员可用性因此仍然不确定。"
        )
        with self.assertRaisesRegex(agent.PregameAgentError, "出赛状态"):
            agent.validate_writer_response(json.dumps(invalid, ensure_ascii=False), evidence)

    def test_07h_writer_rejects_external_source_claim(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        invalid = json.loads(writer_response().content)
        invalid["injury_impact_assessments"][0]["impact_inference"] = (
            "据 ESPN 报道，该候选会影响球队人员可用性，但当前无法量化影响。"
        )
        with self.assertRaisesRegex(agent.PregameAgentError, "外部来源"):
            agent.validate_writer_response(json.dumps(invalid, ensure_ascii=False), evidence)

    def test_07i_writer_rejects_chinese_numeric_injury_effect(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        invalid = json.loads(writer_response().content)
        invalid["injury_impact_assessments"][0]["impact_inference"] = (
            "该记录证明球队分差应下调三分，因此需要直接改变预测。"
        )
        with self.assertRaisesRegex(agent.PregameAgentError, "具体分数"):
            agent.validate_writer_response(json.dumps(invalid, ensure_ascii=False), evidence)

    def test_07j_writer_rejects_unlisted_player_activation_claim(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        invalid = json.loads(writer_response().content)
        invalid["margin_adjustment_reason"] = (
            "官方报告未列出的 Unlisted Player 一定进入激活名单，因此客队占优。"
        )
        with self.assertRaisesRegex(agent.PregameAgentError, "调整理由不得"):
            agent.validate_writer_response(json.dumps(invalid, ensure_ascii=False), evidence)

    def test_08_package_recomputes_adjustments_and_markdown_uses_same_numbers(self):
        evidence, _ = agent.PregameReadOnlyTools(
            ANALYSIS_DATABASE_PATH, GAME_ID, CUTOFF
        ).build_core_evidence()
        availability = agent.assess_optional_information(
            ["injuries"],
            game_id=GAME_ID,
            cutoff_time=CUTOFF,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
        )
        evidence["verified_pregame_injuries"] = availability["injuries"]["records"]
        final = agent.validate_writer_response(writer_response().content, evidence)
        package = agent.build_analysis_package(
            evidence,
            final,
            availability,
            artifact_set_id="test-artifact-set",
            generated_at_utc="2026-09-06T00:00:00Z",
            model="test-model",
            model_response_sha256="a" * 64,
        )
        baseline = evidence["base_prediction"]
        self.assertAlmostEqual(
            package["agent_prediction"]["home_margin_adjustment"],
            -3.25 - baseline["predicted_home_margin"],
        )
        self.assertAlmostEqual(
            package["agent_prediction"]["total_adjustment"],
            224.5 - baseline["predicted_total"],
        )
        markdown = agent.render_markdown(package)
        self.assertIn(agent.format_number(-3.25, signed=True), markdown)
        self.assertIn(agent.format_number(224.5), markdown)
        self.assertIn("test-artifact-set", markdown)
        self.assertIn("官方报告明确事实", markdown)
        self.assertIn("球员可用性汇总", markdown)
        self.assertIn("Agent 影响推断", markdown)
        self.assertIn("当前证据无法支持", markdown)
        self.assertEqual(
            package["injury_analysis"]["reported_facts"][2]["fact_classification"],
            "questionable_availability",
        )
        self.assertEqual(
            package["injury_analysis"]["reported_facts"][4]["fact_classification"],
            "non_injury_out",
        )
        for fact in package["injury_analysis"]["reported_facts"]:
            self.assertIn("source_published_at", fact)
            self.assertIn("captured_at", fact)
            self.assertIn("availability_group", fact)
            self.assertIn("reason_classification", fact)
        self.assertEqual(len(package["injury_analysis"]["reported_facts"]), 10)
        self.assertEqual(
            package["injury_analysis"]["availability_summary"]["confirmed_out_count"],
            6,
        )
        self.assertEqual(
            package["injury_analysis"]["availability_summary"]["questionable_count"],
            1,
        )
        self.assertEqual(
            package["injury_analysis"]["availability_summary"]["available_count"],
            3,
        )
        self.assertEqual(
            package["injury_analysis"]["availability_summary"]["health_related_out_count"],
            2,
        )
        self.assertEqual(
            package["injury_analysis"]["availability_summary"]["non_health_related_out_count"],
            4,
        )
        self.assertEqual(
            [
                item["record_ref"]
                for item in package["injury_analysis"]["analysis_candidates"]
            ],
            [f"injuries.{index}" for index in range(10)],
        )
        self.assertFalse(package["validation"]["injury_candidates_prefiltered_by_program"])
        self.assertTrue(package["validation"]["complete_player_role_evidence_provided_to_model"])

    def test_09_generation_uses_one_mock_api_call_and_writes_three_outputs(self):
        response = writer_response()
        attempt_path = agent.build_api_attempt_output_path(
            GAME_ID, self.outputs_dir, "evidence_fixed_v2"
        )
        writer_input_path = agent.build_writer_input_snapshot_path(
            GAME_ID, self.outputs_dir, "evidence_fixed_v2"
        )

        def assert_attempt_before_request(messages, *_args):
            self.assertTrue(attempt_path.is_file())
            self.assertTrue(writer_input_path.is_file())
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            writer_input = json.loads(writer_input_path.read_text(encoding="utf-8"))
            self.assertEqual(attempt["status"], "attempt_started")
            self.assertEqual(attempt["api_request_count"], 1)
            self.assertEqual(attempt["retry_count"], 0)
            self.assertEqual(writer_input["agent_messages"], messages)
            self.assertEqual(
                attempt["writer_input_artifact"]["sha256"],
                sha256_file(writer_input_path),
            )
            return response

        api_client = Mock(side_effect=assert_attempt_before_request)
        paths = agent.generate_historical_pregame_analysis(
            GAME_ID,
            CUTOFF,
            "test-key",
            database_path=ANALYSIS_DATABASE_PATH,
            snapshot_database_path=SNAPSHOT_DATABASE_PATH,
            outputs_dir=self.outputs_dir,
            api_client=api_client,
            output_tag="evidence_fixed_v2",
        )
        self.assertEqual(api_client.call_count, 1)
        self.assertEqual(set(paths), {"json", "markdown", "trace"})
        self.assertTrue(all(path.is_file() for path in paths.values()))
        package = json.loads(paths["json"].read_text(encoding="utf-8"))
        trace = json.loads(paths["trace"].read_text(encoding="utf-8"))
        self.assertEqual(package["game"]["game_id"], GAME_ID)
        self.assertEqual(trace["api_request_count"], 1)
        self.assertEqual(package["artifact_set_id"], trace["artifact_set_id"])
        self.assertEqual(trace["output_tag"], "evidence_fixed_v2")
        expected_response_hash = agent.sha256_text(response.content)
        self.assertEqual(
            package["generation"]["model_response_sha256"],
            expected_response_hash,
        )
        self.assertEqual(
            trace["api_requests"][0]["response_sha256"],
            expected_response_hash,
        )
        markdown = paths["markdown"].read_text(encoding="utf-8")
        self.assertIn(package["artifact_set_id"], markdown)
        self.assertEqual(
            {Path(path).name for path in trace["artifact_files"].values()},
            {path.name for path in paths.values()},
        )
        self.assertTrue(
            all("evidence_fixed_v2" in path.name for path in paths.values())
        )
        rejected_path = agent.build_rejected_output_path(
            GAME_ID, self.outputs_dir, "evidence_fixed_v2"
        )
        self.assertFalse(rejected_path.exists())
        self.assertEqual(
            trace["planner_decision"], "skipped_for_single_call_injury_integration"
        )
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        self.assertEqual(attempt["status"], "success")
        self.assertEqual(
            [event["status"] for event in attempt["status_history"]],
            ["attempt_started", "api_response_received", "success"],
        )
        self.assertEqual(trace["attempt_id"], attempt["attempt_id"])
        self.assertEqual(
            trace["writer_input_artifact"]["sha256"], sha256_file(writer_input_path)
        )
        self.assertEqual(
            package["information_availability"]["injuries"]["status"],
            "available_historical_archive",
        )
        self.assertTrue(all(call["read_only"] for call in trace["tool_calls"]))
        messages = api_client.call_args.args[0]
        serialized = json.dumps(messages, ensure_ascii=False)
        self.assertIn("Norman Powell", serialized)
        self.assertIn("Questionable", serialized)
        for unavailable_player in (
            "Terry Rozier",
            "Jahmir Young",
            "Myron Gardner",
            "Vladislav Goldin",
        ):
            self.assertIn(unavailable_player, serialized)
        writer_evidence = agent._extract_writer_evidence_from_messages(messages)
        self.assertEqual(
            len(writer_evidence["player_role_evidence_context"]["players"]),
            10,
        )
        self.assertEqual(len(writer_evidence["injury_analysis_candidates"]), 10)
        self.assertEqual(writer_evidence["injury_analysis_detail_limit"], 4)
        self.assertFalse(
            any(
                item["record_ref"] not in {
                    f"injuries.{index}" for index in range(10)
                }
                for item in writer_evidence["injury_analysis_candidates"]
            )
        )
        self.assertNotIn("actual_home_margin", serialized)

    def test_10_api_failure_persists_attempt_and_writer_input(self):
        secret = "test-key-must-not-be-persisted"
        api_client = Mock(side_effect=agent.PregameAgentError("API failure"))
        with self.assertRaisesRegex(agent.PregameAgentError, "调用失败证据已保存"):
            agent.generate_historical_pregame_analysis(
                GAME_ID,
                CUTOFF,
                secret,
                database_path=ANALYSIS_DATABASE_PATH,
                snapshot_database_path=SNAPSHOT_DATABASE_PATH,
                outputs_dir=self.outputs_dir,
                api_client=api_client,
                output_tag="api_failure",
            )
        attempt_path = agent.build_api_attempt_output_path(
            GAME_ID, self.outputs_dir, "api_failure"
        )
        writer_input_path = agent.build_writer_input_snapshot_path(
            GAME_ID, self.outputs_dir, "api_failure"
        )
        self.assertTrue(attempt_path.is_file())
        self.assertTrue(writer_input_path.is_file())
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        self.assertEqual(attempt["status"], "api_failure")
        self.assertEqual(attempt["api_request_count"], 1)
        self.assertEqual(attempt["retry_count"], 0)
        self.assertFalse(attempt["response_received"])
        self.assertEqual(attempt["error"]["type"], "api_client_error")
        self.assertEqual(
            attempt["writer_input_artifact"]["sha256"], sha256_file(writer_input_path)
        )
        serialized = "\n".join(
            path.read_text(encoding="utf-8") for path in self.outputs_dir.iterdir()
        )
        self.assertNotIn(secret, serialized)
        self.assertNotIn("Authorization", serialized)
        success_paths = agent.build_output_paths(
            GAME_ID, self.outputs_dir, "api_failure"
        )
        self.assertTrue(all(not path.exists() for path in success_paths.values()))

    def test_10b_validation_failure_preserves_raw_response_and_hash(self):
        invalid = json.loads(writer_response().content)
        invalid["injury_impact_assessments"][0]["impact_inference"] = (
            "该球员是主力后卫，其状态会决定球队表现。"
        )
        raw_content = json.dumps(invalid, ensure_ascii=False, separators=(",", ":"))
        response = agent.ApiResult(
            content=raw_content,
            finish_reason="stop",
            usage={"prompt_tokens": 120, "completion_tokens": 60},
        )
        api_client = Mock(return_value=response)
        with self.assertRaisesRegex(agent.PregameAgentError, "失败证据已保存"):
            agent.generate_historical_pregame_analysis(
                GAME_ID,
                CUTOFF,
                "test-key-must-not-be-persisted",
                database_path=ANALYSIS_DATABASE_PATH,
                snapshot_database_path=SNAPSHOT_DATABASE_PATH,
                outputs_dir=self.outputs_dir,
                api_client=api_client,
                output_tag="rejected_case",
            )
        self.assertEqual(api_client.call_count, 1)
        rejected_path = agent.build_rejected_output_path(
            GAME_ID, self.outputs_dir, "rejected_case"
        )
        self.assertTrue(rejected_path.is_file())
        rejected = json.loads(rejected_path.read_text(encoding="utf-8"))
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["artifact_type"], "rejected_model_response")
        self.assertEqual(rejected["game_id"], GAME_ID)
        self.assertEqual(rejected["cutoff_time"], CUTOFF)
        self.assertEqual(rejected["api_request_count"], 1)
        self.assertEqual(rejected["retry_count"], 0)
        self.assertEqual(rejected["model_response"]["content"], raw_content)
        self.assertEqual(
            rejected["model_response"]["sha256"], agent.sha256_text(raw_content)
        )
        self.assertIn("位置或角色推断", rejected["validation_error"])
        self.assertEqual(
            [item["record_ref"] for item in rejected["injury_analysis_candidates"]],
            [f"injuries.{index}" for index in range(10)],
        )
        self.assertEqual(len(rejected["player_availability_context"]), 10)
        self.assertEqual(
            rejected["player_availability_summary"]["confirmed_out_count"], 6
        )
        serialized = rejected_path.read_text(encoding="utf-8")
        self.assertNotIn("test-key-must-not-be-persisted", serialized)
        self.assertNotIn("Authorization", serialized)
        self.assertFalse(rejected["successful_analysis_artifacts_written"])
        attempt_path = agent.build_api_attempt_output_path(
            GAME_ID, self.outputs_dir, "rejected_case"
        )
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        self.assertEqual(attempt["status"], "semantic_rejected")
        self.assertEqual(
            [event["status"] for event in attempt["status_history"]],
            ["attempt_started", "api_response_received", "semantic_rejected"],
        )
        self.assertEqual(
            attempt["rejected_artifact"]["sha256"], sha256_file(rejected_path)
        )
        writer_input_path = agent.build_writer_input_snapshot_path(
            GAME_ID, self.outputs_dir, "rejected_case"
        )
        self.assertEqual(
            attempt["writer_input_artifact"]["sha256"],
            sha256_file(writer_input_path),
        )
        self.assertEqual(
            rejected["writer_input_artifact"]["sha256"],
            sha256_file(writer_input_path),
        )
        success_paths = agent.build_output_paths(
            GAME_ID, self.outputs_dir, "rejected_case"
        )
        self.assertTrue(all(not path.exists() for path in success_paths.values()))

    def _run_transport_failure(self, output_tag, opener, expected_error_type):
        secret = "test-key-must-not-be-persisted"

        def api_client(messages, api_key, model, max_tokens, timeout_seconds):
            return agent.request_deepseek(
                messages,
                api_key,
                model,
                max_tokens,
                timeout_seconds,
                opener=opener,
            )

        with self.assertRaisesRegex(agent.PregameAgentError, "调用失败证据已保存"):
            agent.generate_historical_pregame_analysis(
                GAME_ID,
                CUTOFF,
                secret,
                database_path=ANALYSIS_DATABASE_PATH,
                snapshot_database_path=SNAPSHOT_DATABASE_PATH,
                outputs_dir=self.outputs_dir,
                api_client=api_client,
                output_tag=output_tag,
            )
        attempt_path = agent.build_api_attempt_output_path(
            GAME_ID, self.outputs_dir, output_tag
        )
        writer_input_path = agent.build_writer_input_snapshot_path(
            GAME_ID, self.outputs_dir, output_tag
        )
        self.assertTrue(attempt_path.is_file())
        self.assertTrue(writer_input_path.is_file())
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        self.assertEqual(attempt["status"], "api_failure")
        self.assertEqual(attempt["error"]["type"], expected_error_type)
        self.assertEqual(attempt["api_request_count"], 1)
        self.assertEqual(attempt["retry_count"], 0)
        self.assertEqual(
            attempt["writer_input_artifact"]["sha256"],
            sha256_file(writer_input_path),
        )
        writer_input = json.loads(writer_input_path.read_text(encoding="utf-8"))
        self.assertEqual(writer_input["status"], "request_input_frozen")
        self.assertEqual(
            writer_input["input_validation"]["complete_player_role_evidence_count"],
            10,
        )
        self.assertEqual(writer_input["input_validation"]["candidate_count"], 10)
        serialized = "\n".join(
            path.read_text(encoding="utf-8") for path in self.outputs_dir.iterdir()
        )
        self.assertNotIn(secret, serialized)
        self.assertNotIn("Authorization", serialized)
        return attempt

    def test_10c_http_error_preserves_safe_raw_response(self):
        body = (
            b'{"error":"rate limited","Authorization":"Bearer '
            b'test-key-must-not-be-persisted"}'
        )

        def opener(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "Too Many Requests",
                {},
                io.BytesIO(body),
            )

        attempt = self._run_transport_failure("http_failure", opener, "http_error")
        self.assertTrue(attempt["response_received"])
        self.assertEqual(attempt["error"]["http_status"], 429)
        raw_path = agent.build_raw_response_output_path(
            GAME_ID, self.outputs_dir, "http_failure"
        )
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["raw_response"]["sha256"], hashlib.sha256(body).hexdigest())
        self.assertTrue(raw["raw_response"]["credential_redaction_applied"])
        self.assertNotIn(
            "test-key-must-not-be-persisted",
            raw_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            [event["status"] for event in attempt["status_history"]],
            ["attempt_started", "api_response_received", "api_failure"],
        )

    def test_10d_network_error_records_no_response(self):
        def opener(_request, timeout):
            raise urllib.error.URLError("offline")

        attempt = self._run_transport_failure(
            "network_failure", opener, "network_error"
        )
        self.assertFalse(attempt["response_received"])
        self.assertIsNone(attempt["raw_response_artifact"])
        self.assertFalse(
            agent.build_raw_response_output_path(
                GAME_ID, self.outputs_dir, "network_failure"
            ).exists()
        )
        self.assertEqual(
            [event["status"] for event in attempt["status_history"]],
            ["attempt_started", "api_failure"],
        )

    def test_10e_json_parse_failure_preserves_raw_response(self):
        body = b"this is not json"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return body

        attempt = self._run_transport_failure(
            "json_failure", lambda _request, timeout: FakeResponse(), "response_json_error"
        )
        self.assertTrue(attempt["response_received"])
        raw_path = agent.build_raw_response_output_path(
            GAME_ID, self.outputs_dir, "json_failure"
        )
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["raw_response"]["text"], body.decode("utf-8"))
        self.assertEqual(raw["raw_response"]["sha256"], hashlib.sha256(body).hexdigest())

    def test_10f_empty_response_preserves_explicit_empty_evidence(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b""

        attempt = self._run_transport_failure(
            "empty_failure", lambda _request, timeout: FakeResponse(), "empty_response"
        )
        self.assertEqual(attempt["error"]["empty_response_kind"], "http_body_empty")
        raw_path = agent.build_raw_response_output_path(
            GAME_ID, self.outputs_dir, "empty_failure"
        )
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["raw_response"]["text"], "")
        self.assertEqual(raw["raw_response"]["byte_count"], 0)
        self.assertTrue(raw["raw_response"]["explicitly_empty"])

    def test_10g_empty_model_content_preserves_received_json(self):
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": "   "},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 0},
            }
        ).encode("utf-8")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return body

        attempt = self._run_transport_failure(
            "empty_model_content",
            lambda _request, timeout: FakeResponse(),
            "empty_response",
        )
        self.assertEqual(
            attempt["error"]["empty_response_kind"], "model_content_empty"
        )
        raw_path = agent.build_raw_response_output_path(
            GAME_ID, self.outputs_dir, "empty_model_content"
        )
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        self.assertEqual(
            raw["raw_response"]["sha256"], hashlib.sha256(body).hexdigest()
        )
        self.assertGreater(raw["raw_response"]["byte_count"], 0)
        self.assertFalse(raw["raw_response"]["explicitly_empty"])

    def test_10h_existing_attempt_stops_before_api(self):
        existing = agent.build_api_attempt_output_path(
            GAME_ID, self.outputs_dir, "protected_attempt"
        )
        existing.write_text('{"status":"attempt_started"}\n', encoding="utf-8")
        original_hash = sha256_file(existing)
        api_client = Mock(side_effect=AssertionError("API must not run"))
        with self.assertRaisesRegex(agent.PregameAgentError, "已经存在"):
            agent.generate_historical_pregame_analysis(
                GAME_ID,
                CUTOFF,
                "test-key",
                database_path=ANALYSIS_DATABASE_PATH,
                snapshot_database_path=SNAPSHOT_DATABASE_PATH,
                outputs_dir=self.outputs_dir,
                api_client=api_client,
                output_tag="protected_attempt",
            )
        self.assertEqual(sha256_file(existing), original_hash)
        api_client.assert_not_called()

    def test_10i_existing_success_output_stops_before_api(self):
        existing = (
            self.outputs_dir
            / f"game_{GAME_ID}_historical_pregame_analysis_protected.md"
        )
        existing.write_text("protected", encoding="utf-8")
        api_client = Mock(side_effect=AssertionError("API must not run"))
        with self.assertRaisesRegex(agent.PregameAgentError, "已经存在"):
            agent.generate_historical_pregame_analysis(
                GAME_ID,
                CUTOFF,
                "test-key",
                database_path=ANALYSIS_DATABASE_PATH,
                snapshot_database_path=SNAPSHOT_DATABASE_PATH,
                outputs_dir=self.outputs_dir,
                api_client=api_client,
                output_tag="protected",
            )
        self.assertEqual(existing.read_text(encoding="utf-8"), "protected")
        api_client.assert_not_called()

    def test_10j_existing_rejected_output_stops_before_api(self):
        existing = agent.build_rejected_output_path(
            GAME_ID, self.outputs_dir, "protected_rejected"
        )
        existing.write_text("protected rejected evidence", encoding="utf-8")
        original_hash = sha256_file(existing)
        api_client = Mock(side_effect=AssertionError("API must not run"))
        with self.assertRaisesRegex(agent.PregameAgentError, "已经存在"):
            agent.generate_historical_pregame_analysis(
                GAME_ID,
                CUTOFF,
                "test-key",
                database_path=ANALYSIS_DATABASE_PATH,
                snapshot_database_path=SNAPSHOT_DATABASE_PATH,
                outputs_dir=self.outputs_dir,
                api_client=api_client,
                output_tag="protected_rejected",
            )
        self.assertEqual(sha256_file(existing), original_hash)
        self.assertEqual(
            existing.read_text(encoding="utf-8"), "protected rejected evidence"
        )
        api_client.assert_not_called()

    def test_11_missing_key_or_invalid_game_stops_before_api(self):
        api_client = Mock(side_effect=AssertionError("API must not run"))
        with self.assertRaisesRegex(agent.PregameAgentError, "DEEPSEEK_API_KEY"):
            agent.generate_historical_pregame_analysis(
                GAME_ID,
                CUTOFF,
                "",
                database_path=ANALYSIS_DATABASE_PATH,
                snapshot_database_path=SNAPSHOT_DATABASE_PATH,
                outputs_dir=self.outputs_dir,
                api_client=api_client,
            )
        with self.assertRaisesRegex(agent.PregameAgentError, "不存在"):
            agent.generate_historical_pregame_analysis(
                "9999999999",
                CUTOFF,
                "test-key",
                database_path=ANALYSIS_DATABASE_PATH,
                snapshot_database_path=SNAPSHOT_DATABASE_PATH,
                outputs_dir=self.outputs_dir,
                api_client=api_client,
            )
        api_client.assert_not_called()

    def test_12_cutoff_before_an_intervening_game_rejects_frozen_features(self):
        with self.assertRaisesRegex(agent.PregameAgentError, "截止时间之后仍有球队比赛"):
            agent.PregameReadOnlyTools(
                ANALYSIS_DATABASE_PATH,
                GAME_ID,
                "2026-01-01T12:00:00",
            )

    def test_13_deepseek_payload_has_bounded_parameters(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {"content": '{"optional_information_requests":[]}'},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"completion_tokens": 5},
                    }
                ).encode("utf-8")

        def opener(request: urllib.request.Request, timeout: float):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            captured["authorization"] = request.headers.get("Authorization")
            return FakeResponse()

        result = agent.request_deepseek(
            [{"role": "user", "content": "safe"}],
            "secret-test-key",
            agent.DEFAULT_MODEL,
            agent.PLANNER_MAX_TOKENS,
            opener=opener,
        )
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(captured["payload"]["thinking"], {"type": "disabled"})
        self.assertEqual(captured["payload"]["temperature"], 0.1)
        self.assertEqual(captured["payload"]["max_tokens"], 400)
        self.assertFalse(captured["payload"]["stream"])
        self.assertEqual(captured["timeout"], 60.0)


if __name__ == "__main__":
    unittest.main()
