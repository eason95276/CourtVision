from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import Mock

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
V2_DIR = PROJECT_DIR / "v2"
SCRIPT_PATH = V2_DIR / "agent" / "generate_postgame_brief.py"
DETAIL_DATABASE_PATH = V2_DIR / "db" / "courtvision_v2.sqlite"
ANALYSIS_DATABASE_PATH = PROJECT_DIR / "analysis_v1" / "data" / "courtvision_analysis.sqlite"
SOURCE_OUTPUTS_DIR = V2_DIR / "outputs"
VALID_GAME_IDS = ("0022500166", "0022500564", "0022501099")
INVALID_GAME_ID = "9999999999"


def load_agent_module():
    spec = importlib.util.spec_from_file_location("generate_postgame_brief", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = load_agent_module()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


PROTECTED_PATHS = (
    DETAIL_DATABASE_PATH,
    ANALYSIS_DATABASE_PATH,
    SOURCE_OUTPUTS_DIR / "game_0022500564_agent_brief.md",
    *(
        SOURCE_OUTPUTS_DIR / f"game_{game_id}_{suffix}.json"
        for game_id in VALID_GAME_IDS
        for suffix in ("facts", "prediction_audit")
    ),
)

MISSING_INTEGRATION_ASSETS = tuple(path for path in PROTECTED_PATHS if not path.is_file())
if MISSING_INTEGRATION_ASSETS:
    missing = ", ".join(str(path.relative_to(PROJECT_DIR)) for path in MISSING_INTEGRATION_ASSETS)
    pytest.skip(
        f"postgame full-data integration assets are not part of the Demo runtime closure: {missing}",
        allow_module_level=True,
    )

PROTECTED_HASHES = {path: sha256_file(path) for path in PROTECTED_PATHS}


def valid_model_response() -> str:
    return json.dumps(
        {
            "postgame_explanation": (
                "全场投篮效率差异最有解释价值。MIA有效命中率为59.2%，PHX为50.0%；"
                "限制区内MIA二十九投二十五中得到五十分，PHX二十八投十六中得到三十二分。"
                "以上属于与比赛结果同时出现的全场数据优势，可能帮助解释本场结果。"
                "PHX进攻篮板率为36.0%，高于MIA的23.1%，说明指标并非全部同向，"
                "单项数据无法证明确定的胜负原因。"
            ),
            "selected_player_ids": [1628389, 1626181, 1626164, 1631109],
            "data_boundary": (
                "本简报仅使用目标比赛的结构化证据，缺少比赛录像、伤病信息、可靠的完整阵容、"
                "追踪数据与长期样本，无法判断教练意图、防守对位、伤病影响或长期能力。"
                "历史回测预测缺少真实赛前持久化时间戳，历史市场基准也缺少可靠采集时间，"
                "且无法确认属于初盘或终盘。"
            ),
        },
        ensure_ascii=False,
    )


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class PostgameBriefAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.outputs_dir = Path(self.temp_directory.name)
        for game_id in VALID_GAME_IDS:
            for suffix in ("facts", "prediction_audit"):
                source = SOURCE_OUTPUTS_DIR / f"game_{game_id}_{suffix}.json"
                target = self.outputs_dir / source.name
                target.write_bytes(source.read_bytes())

    def tearDown(self):
        self.temp_directory.cleanup()

    def generate_with_mocks(
        self,
        game_id: str = VALID_GAME_IDS[1],
        fact_generator=None,
        prediction_audit_generator=None,
        api_client=None,
    ):
        return agent.generate_postgame_brief(
            game_id,
            "test-key",
            database_path=DETAIL_DATABASE_PATH,
            analysis_database_path=ANALYSIS_DATABASE_PATH,
            outputs_dir=self.outputs_dir,
            fact_generator=fact_generator or (lambda selected: None),
            prediction_audit_generator=(
                prediction_audit_generator or (lambda selected: None)
            ),
            api_client=api_client or (
                lambda messages, key, model, timeout: valid_model_response()
            ),
        )

    def build_valid_complete_brief(self, game_id=VALID_GAME_IDS[1]):
        facts = agent.load_target_facts(game_id, self.outputs_dir)
        audit = agent.load_target_prediction_audit(game_id, self.outputs_dir)
        flow = agent.build_game_flow_evidence(game_id, facts, DETAIL_DATABASE_PATH)
        response = agent.validate_model_response(
            valid_model_response(), game_id, "test-key"
        )
        selected = agent.validate_selected_players(
            response["selected_player_ids"],
            agent.build_key_player_candidates(facts),
            game_id,
            DETAIL_DATABASE_PATH,
        )
        return agent.compose_complete_brief(
            agent.build_prediction_review_section(audit),
            agent.build_game_flow_section(flow),
            response["postgame_explanation"],
            agent.build_key_players_section(selected),
            response["data_boundary"],
        )

    def test_01_all_three_games_load_both_target_fact_files(self):
        for game_id in VALID_GAME_IDS:
            with self.subTest(game_id=game_id):
                postgame = agent.load_target_facts(game_id, self.outputs_dir)
                audit = agent.load_target_prediction_audit(game_id, self.outputs_dir)
                self.assertEqual(postgame["game"]["game_id"], game_id)
                self.assertEqual(audit["game"]["game_id"], game_id)

    def test_02_both_generators_receive_the_same_game_id_in_order(self):
        calls = []

        def facts_generator(game_id):
            calls.append(("facts", game_id))

        def audit_generator(game_id):
            calls.append(("prediction_audit", game_id))

        output_path = self.generate_with_mocks(
            fact_generator=facts_generator,
            prediction_audit_generator=audit_generator,
        )

        self.assertEqual(
            calls,
            [
                ("facts", VALID_GAME_IDS[1]),
                ("prediction_audit", VALID_GAME_IDS[1]),
            ],
        )
        self.assertEqual(
            output_path.name,
            f"game_{VALID_GAME_IDS[1]}_prediction_review_brief.md",
        )

    def test_03_each_pair_of_json_files_contains_only_its_target_game(self):
        for game_id in VALID_GAME_IDS:
            postgame = agent.load_target_facts(game_id, self.outputs_dir)
            audit = agent.load_target_prediction_audit(game_id, self.outputs_dir)
            self.assertEqual(agent._collect_game_ids(postgame), {game_id})
            self.assertEqual(agent._collect_game_ids(audit), {game_id})

    def test_04_invalid_game_is_rejected_before_generators_and_api(self):
        facts_generator = Mock(side_effect=AssertionError("must not run"))
        audit_generator = Mock(side_effect=AssertionError("must not run"))
        api_client = Mock(side_effect=AssertionError("must not run"))

        with self.assertRaisesRegex(agent.PostgameBriefError, "不在.*数据库"):
            agent.generate_postgame_brief(
                INVALID_GAME_ID,
                "test-key",
                database_path=DETAIL_DATABASE_PATH,
                analysis_database_path=ANALYSIS_DATABASE_PATH,
                outputs_dir=self.outputs_dir,
                fact_generator=facts_generator,
                prediction_audit_generator=audit_generator,
                api_client=api_client,
            )

        facts_generator.assert_not_called()
        audit_generator.assert_not_called()
        api_client.assert_not_called()

    def test_05_missing_api_key_stops_before_generators(self):
        facts_generator = Mock(side_effect=AssertionError("must not run"))
        audit_generator = Mock(side_effect=AssertionError("must not run"))
        api_client = Mock(side_effect=AssertionError("must not run"))

        with self.assertRaisesRegex(agent.PostgameBriefError, "DEEPSEEK_API_KEY"):
            agent.generate_postgame_brief(
                VALID_GAME_IDS[0],
                "",
                database_path=DETAIL_DATABASE_PATH,
                analysis_database_path=ANALYSIS_DATABASE_PATH,
                outputs_dir=self.outputs_dir,
                fact_generator=facts_generator,
                prediction_audit_generator=audit_generator,
                api_client=api_client,
            )

        facts_generator.assert_not_called()
        audit_generator.assert_not_called()
        api_client.assert_not_called()

    def test_06_either_generator_failure_prevents_api_call(self):
        api_client = Mock(side_effect=AssertionError("must not run"))

        def fail_facts(game_id):
            raise agent.PostgameBriefError("赛后事实生成失败，未调用 DeepSeek。")

        with self.assertRaisesRegex(agent.PostgameBriefError, "赛后事实生成失败"):
            self.generate_with_mocks(
                fact_generator=fail_facts,
                prediction_audit_generator=Mock(),
                api_client=api_client,
            )
        api_client.assert_not_called()

        api_client.reset_mock()

        def fail_audit(game_id):
            raise agent.PostgameBriefError("预测验收事实生成失败，未调用 DeepSeek。")

        with self.assertRaisesRegex(agent.PostgameBriefError, "预测验收事实生成失败"):
            self.generate_with_mocks(
                fact_generator=Mock(),
                prediction_audit_generator=fail_audit,
                api_client=api_client,
            )
        api_client.assert_not_called()

    def test_07_missing_postgame_json_prevents_api_call(self):
        api_client = Mock(side_effect=AssertionError("must not run"))
        path = self.outputs_dir / f"game_{VALID_GAME_IDS[1]}_facts.json"
        path.unlink()
        with self.assertRaisesRegex(agent.PostgameBriefError, "赛后事实文件没有生成"):
            self.generate_with_mocks(api_client=api_client)
        api_client.assert_not_called()

    def test_08_missing_prediction_audit_json_prevents_api_call(self):
        api_client = Mock(side_effect=AssertionError("must not run"))
        path = self.outputs_dir / f"game_{VALID_GAME_IDS[1]}_prediction_audit.json"
        path.unlink()
        with self.assertRaisesRegex(agent.PostgameBriefError, "预测验收事实文件没有生成"):
            self.generate_with_mocks(api_client=api_client)
        api_client.assert_not_called()

    def test_09_unparseable_postgame_json_prevents_api_call(self):
        api_client = Mock(side_effect=AssertionError("must not run"))
        path = self.outputs_dir / f"game_{VALID_GAME_IDS[1]}_facts.json"
        path.write_text("{invalid", encoding="utf-8")
        with self.assertRaisesRegex(agent.PostgameBriefError, "赛后事实 JSON 无法"):
            self.generate_with_mocks(api_client=api_client)
        api_client.assert_not_called()

    def test_10_unparseable_prediction_audit_json_prevents_api_call(self):
        api_client = Mock(side_effect=AssertionError("must not run"))
        path = self.outputs_dir / f"game_{VALID_GAME_IDS[1]}_prediction_audit.json"
        path.write_text("{invalid", encoding="utf-8")
        with self.assertRaisesRegex(agent.PostgameBriefError, "预测验收事实 JSON 无法"):
            self.generate_with_mocks(api_client=api_client)
        api_client.assert_not_called()

    def test_11_mismatched_postgame_game_id_prevents_api_call(self):
        requested = VALID_GAME_IDS[0]
        path = self.outputs_dir / f"game_{requested}_facts.json"
        facts = json.loads(path.read_text(encoding="utf-8"))
        facts["game"]["game_id"] = VALID_GAME_IDS[1]
        path.write_text(json.dumps(facts), encoding="utf-8")
        api_client = Mock(side_effect=AssertionError("must not run"))

        with self.assertRaisesRegex(agent.PostgameBriefError, "不一致"):
            self.generate_with_mocks(requested, api_client=api_client)
        api_client.assert_not_called()

    def test_12_mismatched_prediction_audit_game_id_prevents_api_call(self):
        requested = VALID_GAME_IDS[0]
        path = self.outputs_dir / f"game_{requested}_prediction_audit.json"
        audit = json.loads(path.read_text(encoding="utf-8"))
        audit["game"]["game_id"] = VALID_GAME_IDS[1]
        path.write_text(json.dumps(audit), encoding="utf-8")
        api_client = Mock(side_effect=AssertionError("must not run"))

        with self.assertRaisesRegex(agent.PostgameBriefError, "不一致"):
            self.generate_with_mocks(requested, api_client=api_client)
        api_client.assert_not_called()

    def test_13_prompt_contains_only_selected_compact_evidence(self):
        selected = VALID_GAME_IDS[1]
        postgame = agent.load_target_facts(selected, self.outputs_dir)
        audit = agent.load_target_prediction_audit(selected, self.outputs_dir)
        flow = agent.build_game_flow_evidence(selected, postgame, DETAIL_DATABASE_PATH)
        compact = agent.build_compact_evidence(postgame, audit, flow)
        messages = agent.build_messages(compact)
        combined = "\n".join(message["content"] for message in messages)

        self.assertIn("<brief_evidence>", combined)
        self.assertNotIn("<single_game_facts>", combined)
        self.assertNotIn("<prediction_audit>", combined)
        self.assertIn(selected, combined)
        self.assertIn("只能使用", messages[0]["content"])
        self.assertIn("不得自行生成新的预测", messages[0]["content"])
        for other_game_id in set(VALID_GAME_IDS) - {selected}:
            self.assertNotIn(other_game_id, combined)

    def test_14_python_prediction_section_has_reference_game_values(self):
        audit = agent.load_target_prediction_audit(VALID_GAME_IDS[1], self.outputs_dir)
        section = agent.build_prediction_review_section(audit)

        self.assertTrue(section.startswith("## 1. 赛前预测与赛后验收"))
        self.assertIn("MIA 127，PHX 121", section)
        self.assertIn("实际主队分差 +6", section)
        self.assertIn("实际总分 248", section)
        self.assertIn("历史回测预测", section)
        self.assertIn("45.38%", section)
        self.assertIn("-1.5686", section)
        self.assertIn("226.9332", section)
        self.assertIn("+1.5", section)
        self.assertIn("233", section)
        self.assertIn("模型方向为客队方向", section)
        self.assertIn("实际方向为主队方向", section)
        self.assertIn("让分方向", section)
        self.assertIn("模型方向为小分", section)
        self.assertIn("实际方向为大分", section)
        self.assertIn("大小分方向", section)
        self.assertEqual(section.count("未命中"), 2)
        self.assertNotIn("预测误差", section)
        self.assertNotIn("普通胜负预测", section)

    def test_15_deepseek_returns_only_the_structured_fields(self):
        checked = agent.validate_model_response(
            valid_model_response(), VALID_GAME_IDS[1], "test-key"
        )
        self.assertEqual(
            set(checked),
            {"postgame_explanation", "selected_player_ids", "data_boundary"},
        )
        self.assertEqual(len(checked["selected_player_ids"]), 4)

    def test_16_model_cannot_supply_or_override_the_python_section(self):
        invalid = "## 1. 赛前预测与赛后验收\n\n伪造内容。\n" + valid_model_response()
        with self.assertRaisesRegex(agent.PostgameBriefError, "JSON"):
            agent.validate_model_response(invalid, VALID_GAME_IDS[1], "test-key")

        facts = agent.load_target_facts(VALID_GAME_IDS[1], self.outputs_dir)
        audit = agent.load_target_prediction_audit(VALID_GAME_IDS[1], self.outputs_dir)
        flow = agent.build_game_flow_evidence(
            VALID_GAME_IDS[1], facts, DETAIL_DATABASE_PATH
        )
        response = agent.validate_model_response(
            valid_model_response(), VALID_GAME_IDS[1], "test-key"
        )
        selected = agent.validate_selected_players(
            response["selected_player_ids"],
            agent.build_key_player_candidates(facts),
            VALID_GAME_IDS[1],
            DETAIL_DATABASE_PATH,
        )
        first = agent.build_prediction_review_section(audit)
        complete = agent.compose_complete_brief(
            first,
            agent.build_game_flow_section(flow),
            response["postgame_explanation"],
            agent.build_key_players_section(selected),
            response["data_boundary"],
        )
        self.assertEqual(complete.count("## 1. 赛前预测与赛后验收"), 1)
        self.assertEqual(complete.split("## 2. 比赛走势", 1)[0].strip(), first.strip())

    def test_17_complete_brief_has_exactly_five_headings_in_order(self):
        facts = agent.load_target_facts(VALID_GAME_IDS[1], self.outputs_dir)
        audit = agent.load_target_prediction_audit(VALID_GAME_IDS[1], self.outputs_dir)
        flow = agent.build_game_flow_evidence(
            VALID_GAME_IDS[1], facts, DETAIL_DATABASE_PATH
        )
        response = agent.validate_model_response(
            valid_model_response(), VALID_GAME_IDS[1], "test-key"
        )
        selected = agent.validate_selected_players(
            response["selected_player_ids"],
            agent.build_key_player_candidates(facts),
            VALID_GAME_IDS[1],
            DETAIL_DATABASE_PATH,
        )
        complete = agent.compose_complete_brief(
            agent.build_prediction_review_section(audit),
            agent.build_game_flow_section(flow),
            response["postgame_explanation"],
            agent.build_key_players_section(selected),
            response["data_boundary"],
        )
        checked = agent.validate_complete_brief(complete, VALID_GAME_IDS[1], "test-key")
        self.assertEqual(tuple(agent.extract_headings(checked)), agent.REQUIRED_HEADINGS)

    def test_18_spread_and_total_are_separate_paragraphs(self):
        audit = agent.load_target_prediction_audit(VALID_GAME_IDS[1], self.outputs_dir)
        section = agent.build_prediction_review_section(audit)
        paragraphs = [part for part in section.split("\n\n") if part]
        spread = [part for part in paragraphs if part.startswith("让分方向：")]
        total = [part for part in paragraphs if part.startswith("大小分方向：")]
        self.assertEqual(len(spread), 1)
        self.assertEqual(len(total), 1)

    def test_19_missing_market_shows_unavailable_without_fabrication(self):
        audit = agent.load_target_prediction_audit(VALID_GAME_IDS[1], self.outputs_dir)
        audit["historical_market_benchmark"].update(
            {
                "market_record_available": False,
                "market_expected_home_margin": None,
                "market_total": None,
            }
        )
        audit["direction_evaluation"]["spread"].update(
            {
                "model_direction": None,
                "actual_result": None,
                "spread_direction_hit": None,
                "status": "market_unavailable",
            }
        )
        audit["direction_evaluation"]["total"].update(
            {
                "model_direction": None,
                "actual_result": None,
                "total_direction_hit": None,
                "status": "market_unavailable",
            }
        )
        section = agent.build_prediction_review_section(audit)
        self.assertIn("-1.5686", section)
        self.assertIn("226.9332", section)
        self.assertEqual(section.count("缺少可靠市场基准，无法验收"), 2)
        self.assertNotIn("命中。", section)

    def test_20_equal_model_and_market_is_no_disagreement(self):
        audit = agent.load_target_prediction_audit(VALID_GAME_IDS[1], self.outputs_dir)
        audit["historical_prediction"]["predicted_home_margin"] = 1.5
        audit["direction_evaluation"]["spread"].update(
            {
                "model_direction": "no_disagreement",
                "spread_direction_hit": None,
                "status": "no_model_market_disagreement",
            }
        )
        section = agent.build_prediction_review_section(audit)
        spread = next(
            part for part in section.split("\n\n") if part.startswith("让分方向：")
        )
        self.assertIn("模型与市场无分歧", spread)
        self.assertIn("不判断命中或未命中", spread)
        self.assertNotIn("结果为未命中", spread)

    def test_21_equal_actual_and_market_is_push(self):
        audit = agent.load_target_prediction_audit(VALID_GAME_IDS[1], self.outputs_dir)
        audit["game"]["actual_home_margin"] = 1.5
        audit["direction_evaluation"]["spread"].update(
            {
                "actual_result": "push",
                "spread_direction_hit": None,
                "status": "push",
            }
        )
        section = agent.build_prediction_review_section(audit)
        spread = next(
            part for part in section.split("\n\n") if part.startswith("让分方向：")
        )
        self.assertIn("走盘", spread)
        self.assertIn("不计入命中或未命中", spread)
        self.assertNotIn("结果为未命中", spread)

    def test_22_forbidden_market_label_and_betting_language_are_rejected(self):
        complete = self.build_valid_complete_brief()
        self.assertNotIn("中国体彩官方盘口", complete)
        invalid = complete.replace("历史市场基准", "中国体彩官方盘口", 1)
        with self.assertRaisesRegex(agent.PostgameBriefError, "禁止的内容"):
            agent.validate_complete_brief(invalid, VALID_GAME_IDS[1], "test-key")

        invalid = complete.replace("本简报仅使用", "本简报提供投注建议，仅使用", 1)
        with self.assertRaisesRegex(agent.PostgameBriefError, "禁止的内容"):
            agent.validate_complete_brief(invalid, VALID_GAME_IDS[1], "test-key")

    def test_23_deepseek_payload_contains_compact_evidence_and_fixed_parameters(self):
        postgame = agent.load_target_facts(VALID_GAME_IDS[1], self.outputs_dir)
        audit = agent.load_target_prediction_audit(VALID_GAME_IDS[1], self.outputs_dir)
        flow = agent.build_game_flow_evidence(
            VALID_GAME_IDS[1], postgame, DETAIL_DATABASE_PATH
        )
        compact = agent.build_compact_evidence(postgame, audit, flow)
        messages = agent.build_messages(compact)
        captured = {}

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(
                {"choices": [{"message": {"content": valid_model_response()}}]}
            )

        content = agent.request_deepseek(
            messages,
            "test-key",
            "deepseek-v4-flash",
            timeout_seconds=12.0,
            opener=fake_open,
        )

        self.assertEqual(content, valid_model_response())
        self.assertEqual(captured["url"], agent.DEEPSEEK_API_URL)
        self.assertEqual(captured["timeout"], 12.0)
        self.assertEqual(captured["payload"]["model"], "deepseek-v4-flash")
        self.assertEqual(captured["payload"]["thinking"]["type"], "disabled")
        self.assertEqual(captured["payload"]["max_tokens"], 1800)
        self.assertEqual(captured["payload"]["temperature"], 0.1)
        sent = "\n".join(item["content"] for item in captured["payload"]["messages"])
        self.assertIn("<brief_evidence>", sent)
        self.assertNotIn("<single_game_facts>", sent)
        self.assertNotIn("<prediction_audit>", sent)

    def test_24_api_or_content_failure_does_not_overwrite_valid_review(self):
        game_id = VALID_GAME_IDS[1]
        output_path = self.outputs_dir / f"game_{game_id}_prediction_review_brief.md"
        output_path.write_text("# 已有有效完整简报\n", encoding="utf-8")
        hash_before = sha256_file(output_path)

        def api_failure(messages, key, model, timeout):
            raise agent.PostgameBriefError("DeepSeek 请求失败，未生成新简报。")

        with self.assertRaisesRegex(agent.PostgameBriefError, "DeepSeek 请求失败"):
            self.generate_with_mocks(api_client=api_failure)
        self.assertEqual(sha256_file(output_path), hash_before)

        with self.assertRaisesRegex(agent.PostgameBriefError, "JSON"):
            self.generate_with_mocks(
                api_client=lambda messages, key, model, timeout: "无效返回"
            )
        self.assertEqual(sha256_file(output_path), hash_before)

    def test_25_review_output_uses_atomic_replace_and_preserves_old_brief(self):
        game_id = VALID_GAME_IDS[1]
        old_brief = self.outputs_dir / f"game_{game_id}_agent_brief.md"
        old_brief.write_text("# 旧版赛后简报\n", encoding="utf-8")
        old_hash = sha256_file(old_brief)

        with mock.patch.object(agent.os, "replace", wraps=os.replace) as replace_mock:
            output_path = self.generate_with_mocks()

        self.assertEqual(replace_mock.call_count, 1)
        self.assertEqual(output_path.name, f"game_{game_id}_prediction_review_brief.md")
        self.assertEqual(sha256_file(old_brief), old_hash)
        self.assertFalse(any(self.outputs_dir.glob("*.tmp")))

    def test_26_two_databases_and_source_json_files_remain_unchanged(self):
        for path, expected_hash in PROTECTED_HASHES.items():
            with self.subTest(path=path.name):
                self.assertEqual(sha256_file(path), expected_hash)

    def test_27_missing_game_id_returns_command_help(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr.lower())
        self.assertIn("--game-id", result.stderr)
        self.assertIn("缺少", result.stderr)

    def test_28_empty_api_content_reports_only_safe_diagnostics(self):
        def fake_open(request, timeout):
            return FakeResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "content": "",
                                "reasoning_content": "private reasoning",
                            },
                        }
                    ],
                    "usage": {"completion_tokens": 1800},
                }
            )

        with self.assertRaisesRegex(
            agent.PostgameBriefError,
            "finish_reason=length，completion_tokens=1800",
        ) as context:
            agent.request_deepseek(
                [{"role": "user", "content": "test"}],
                "test-key",
                "deepseek-v4-flash",
                opener=fake_open,
            )

        self.assertNotIn("private reasoning", str(context.exception))
        self.assertNotIn("test-key", str(context.exception))

    def test_29_reference_game_third_quarter_ends_tied(self):
        facts = agent.load_target_facts(VALID_GAME_IDS[1], self.outputs_dir)
        evidence = agent.build_game_flow_evidence(
            VALID_GAME_IDS[1], facts, DETAIL_DATABASE_PATH
        )
        third = evidence["third_quarter_end"]
        self.assertEqual(third["period"], 3)
        self.assertEqual(third["score_away"], 92)
        self.assertEqual(third["score_home"], 92)
        self.assertEqual(third["status"], "tie")

    def test_30_reference_game_first_comeback_lead_is_fourth_quarter_913(self):
        facts = agent.load_target_facts(VALID_GAME_IDS[1], self.outputs_dir)
        evidence = agent.build_game_flow_evidence(
            VALID_GAME_IDS[1], facts, DETAIL_DATABASE_PATH
        )
        comeback = evidence["halftime_trailing_team_comeback"]
        event = comeback["first_lead_event"]
        self.assertEqual(comeback["team_tricode"], "PHX")
        self.assertTrue(comeback["tied_after_halftime"])
        self.assertEqual(event["action_id"], 424)
        self.assertEqual(event["period"], 4)
        self.assertEqual(event["clock"], "PT09M13.00S")
        self.assertEqual(event["score_away"], 101)
        self.assertEqual(event["score_home"], 99)

    def test_31_reference_game_away_maximum_lead_is_fourth_quarter_332(self):
        facts = agent.load_target_facts(VALID_GAME_IDS[1], self.outputs_dir)
        evidence = agent.build_game_flow_evidence(
            VALID_GAME_IDS[1], facts, DETAIL_DATABASE_PATH
        )
        maximum = evidence["maximum_leads"]["PHX"]
        self.assertEqual(maximum["maximum_lead"], 6)
        self.assertEqual(maximum["first_event"]["action_id"], 479)
        self.assertEqual(maximum["first_event"]["period"], 4)
        self.assertEqual(maximum["first_event"]["clock"], "PT03M32.00S")
        self.assertEqual(maximum["first_event"]["score_away"], 114)
        self.assertEqual(maximum["first_event"]["score_home"], 108)

    def test_32_reference_game_final_run_after_loser_last_maximum_lead(self):
        facts = agent.load_target_facts(VALID_GAME_IDS[1], self.outputs_dir)
        evidence = agent.build_game_flow_evidence(
            VALID_GAME_IDS[1], facts, DETAIL_DATABASE_PATH
        )
        segment = evidence["eventual_loser_last_maximum_lead"]
        self.assertEqual(segment["team_tricode"], "PHX")
        self.assertEqual(segment["event"]["score_away"], 114)
        self.assertEqual(segment["event"]["score_home"], 108)
        self.assertEqual(segment["scoring_after_event"]["PHX"], 7)
        self.assertEqual(segment["scoring_after_event"]["MIA"], 19)
        self.assertEqual(segment["final_score"]["score_away"], 121)
        self.assertEqual(segment["final_score"]["score_home"], 127)

    def test_33_deterministic_flow_section_places_first_phoenix_lead_in_fourth(self):
        facts = agent.load_target_facts(VALID_GAME_IDS[1], self.outputs_dir)
        evidence = agent.build_game_flow_evidence(
            VALID_GAME_IDS[1], facts, DETAIL_DATABASE_PATH
        )
        section = agent.build_game_flow_section(evidence)
        self.assertIn("第三节结束", section)
        self.assertIn("PHX 92，MIA 92", section)
        self.assertIn("第四节 9:13", section)
        self.assertIn("PHX 101，MIA 99", section)
        self.assertIn("第四节 3:32", section)
        self.assertIn("PHX 114，MIA 108", section)
        self.assertIn("MIA 得到 19 分，PHX 得到 7 分", section)
        self.assertIsNone(re.search(r"第三节.{0,20}反超", section))

    def test_34_selected_player_count_and_uniqueness_are_enforced(self):
        facts = agent.load_target_facts(VALID_GAME_IDS[1], self.outputs_dir)
        candidates = agent.build_key_player_candidates(facts)
        valid_ids = [row["person_id"] for row in candidates]
        with self.assertRaisesRegex(agent.PostgameBriefError, "2 至 4"):
            agent.validate_selected_players(
                valid_ids[:1], candidates, VALID_GAME_IDS[1], DETAIL_DATABASE_PATH
            )
        with self.assertRaisesRegex(agent.PostgameBriefError, "2 至 4"):
            agent.validate_selected_players(
                valid_ids[:5], candidates, VALID_GAME_IDS[1], DETAIL_DATABASE_PATH
            )
        with self.assertRaisesRegex(agent.PostgameBriefError, "重复"):
            agent.validate_selected_players(
                [valid_ids[0], valid_ids[0]],
                candidates,
                VALID_GAME_IDS[1],
                DETAIL_DATABASE_PATH,
            )

    def test_35_unknown_player_is_rejected_before_rendering(self):
        facts = agent.load_target_facts(VALID_GAME_IDS[1], self.outputs_dir)
        candidates = agent.build_key_player_candidates(facts)
        with self.assertRaisesRegex(agent.PostgameBriefError, "不在本场候选球员"):
            agent.validate_selected_players(
                [candidates[0]["person_id"], 999999999],
                candidates,
                VALID_GAME_IDS[1],
                DETAIL_DATABASE_PATH,
            )

    def test_36_other_two_games_build_general_flow_evidence(self):
        for game_id in (VALID_GAME_IDS[0], VALID_GAME_IDS[2]):
            with self.subTest(game_id=game_id):
                facts = agent.load_target_facts(game_id, self.outputs_dir)
                evidence = agent.build_game_flow_evidence(
                    game_id, facts, DETAIL_DATABASE_PATH
                )
                self.assertEqual(evidence["game_id"], game_id)
                self.assertEqual(len(evidence["period_end_scores"]), 4)
                self.assertEqual(
                    evidence["final_score"]["score_away"],
                    facts["game"]["away"]["points"],
                )
                self.assertEqual(
                    evidence["final_score"]["score_home"],
                    facts["game"]["home"]["points"],
                )

    def test_37_mutated_flow_event_is_rejected_against_play_by_play(self):
        facts = agent.load_target_facts(VALID_GAME_IDS[1], self.outputs_dir)
        evidence = agent.build_game_flow_evidence(
            VALID_GAME_IDS[1], facts, DETAIL_DATABASE_PATH
        )
        evidence["halftime_trailing_team_comeback"]["first_lead_event"]["period"] = 3
        with self.assertRaisesRegex(agent.PostgameBriefError, "逐回合证据不一致"):
            agent.validate_game_flow_evidence(
                evidence, VALID_GAME_IDS[1], DETAIL_DATABASE_PATH
            )

    def test_38_compact_evidence_is_smaller_and_keeps_required_sections(self):
        game_id = VALID_GAME_IDS[1]
        facts = agent.load_target_facts(game_id, self.outputs_dir)
        audit = agent.load_target_prediction_audit(game_id, self.outputs_dir)
        flow = agent.build_game_flow_evidence(game_id, facts, DETAIL_DATABASE_PATH)
        compact = agent.build_compact_evidence(facts, audit, flow)
        counts = agent.evidence_character_counts(facts, audit, compact)
        self.assertLess(counts["compact_characters"], counts["full_characters"])
        self.assertLess(counts["compact_characters"], counts["full_characters"] * 0.7)
        self.assertEqual(
            set(compact),
            {
                "game",
                "prediction_review",
                "game_flow_evidence",
                "four_factors",
                "shot_zones",
                "key_player_candidates",
                "limitations",
            },
        )

    def test_39_stage_specific_claim_from_full_game_aggregate_is_rejected(self):
        invalid = "太阳全场三分命中率有限，导致第三节反扑效果不佳。"
        with self.assertRaisesRegex(agent.PostgameBriefError, "阶段性"):
            agent.validate_postgame_explanation(invalid)

    def test_40_selected_player_data_must_match_player_boxscores(self):
        facts = agent.load_target_facts(VALID_GAME_IDS[1], self.outputs_dir)
        candidates = agent.build_key_player_candidates(facts)
        selected_ids = [candidates[0]["person_id"], candidates[1]["person_id"]]
        candidates[0]["points"] += 1
        with self.assertRaisesRegex(agent.PostgameBriefError, "球员数据.*不一致"):
            agent.validate_selected_players(
                selected_ids,
                candidates,
                VALID_GAME_IDS[1],
                DETAIL_DATABASE_PATH,
            )


if __name__ == "__main__":
    unittest.main()
