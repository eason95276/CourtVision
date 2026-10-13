from __future__ import annotations

import json

from .ai_review import generate_ai_brief, generate_ai_review
from .briefs import build_daily_brief
from .test_briefs import RECORDS
from .test_record import refreshed_case
from .record import build_postgame_record


def record_case(tmp_path):
    _, refresh = refreshed_case(tmp_path)
    return build_postgame_record(
        refresh.bundle_path,
        historical_audit_path="v2/outputs/game_0022500564_prediction_audit.json",
        detailed_facts_path="v2/outputs/game_0022500564_facts.json",
        output_root=tmp_path / "records",
    )


def test_successful_ai_review_uses_only_record_evidence(tmp_path):
    record = record_case(tmp_path)
    evidence_ids = [row["evidence_id"] for row in record["key_factors"][:2]]

    def client(messages, key, model, timeout):
        assert record["record_hash"] in messages[-1]["content"]
        return {
            "content": json.dumps(
                {
                    "summary": "热火赢下比赛，结构化效率差异与结果同时出现。",
                    "deep_review": "有效投篮和罚球相关指标提供了主要解释线索，结论仍受单场样本限制。",
                    "evidence_ids": evidence_ids,
                    "limitations": "缺少录像、追踪和防守对位证据，无法确认战术因果。",
                },
                ensure_ascii=False,
            ),
            "usage": {"prompt_tokens": 100, "completion_tokens": 80},
            "raw": {"id": "fixture"},
        }

    result = generate_ai_review(
        record,
        api_client=client,
        api_key="fixture-key",
        output_root=tmp_path / "ai",
    )
    assert result.status == "success"
    assert result.review["evidence_ids"] == evidence_ids
    assert result.output_path.is_file()
    assert result.usage["completion_tokens"] == 80


def test_ai_failure_does_not_change_base_record(tmp_path):
    record = record_case(tmp_path)
    record_path = next((tmp_path / "records" / "0022500564").glob("*.json"))
    before = record_path.read_bytes()

    def client(*args, **kwargs):
        raise TimeoutError("fixture timeout")

    result = generate_ai_review(
        record,
        api_client=client,
        api_key="fixture-key",
        output_root=tmp_path / "ai",
    )
    assert result.status == "failed"
    assert record_path.read_bytes() == before
    assert result.failure_path.is_file()


def test_unknown_evidence_id_is_rejected(tmp_path):
    record = record_case(tmp_path)

    def client(*args, **kwargs):
        return json.dumps(
            {
                "summary": "结构化数据提供有限解释。",
                "deep_review": "本场数据关系需要结合证据阅读。",
                "evidence_ids": ["invented"],
                "limitations": "缺少录像和追踪数据。",
            },
            ensure_ascii=False,
        )

    result = generate_ai_review(
        record,
        api_client=client,
        api_key="fixture-key",
        output_root=tmp_path / "ai",
    )
    assert result.status == "failed"
    assert result.error_type == "ValueError"


def test_missing_key_fails_before_default_transport(tmp_path, monkeypatch):
    record = record_case(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = generate_ai_review(record, output_root=tmp_path / "ai")
    assert result.status == "failed"
    assert result.error_type == "MissingAPIKey"


def test_invented_number_is_rejected(tmp_path):
    record = record_case(tmp_path)
    evidence_id = record["key_factors"][0]["evidence_id"]

    def client(*args, **kwargs):
        return json.dumps(
            {
                "summary": "模型声称出现了 9999 分的差异。",
                "deep_review": "该数字没有结构化证据。",
                "evidence_ids": [evidence_id],
                "limitations": "缺少录像和追踪数据。",
            },
            ensure_ascii=False,
        )

    result = generate_ai_review(
        record,
        api_client=client,
        api_key="fixture-key",
        output_root=tmp_path / "ai",
    )
    assert result.status == "failed"
    assert result.error_type == "ValueError"


def test_ai_can_organize_a_deterministic_brief_on_demand(tmp_path):
    brief = build_daily_brief("2026-01-01", RECORDS)

    def client(*args, **kwargs):
        return {
            "content": json.dumps(
                {
                    "summary": "当天比赛结果与预测偏差已经由确定性流程整理。",
                    "highlights": ["优先查看预测误差最高的比赛。"],
                    "evidence_refs": brief["source_record_hashes"],
                    "limitations": "简报只覆盖当前可用记录。",
                },
                ensure_ascii=False,
            ),
            "usage": {"prompt_tokens": 50, "completion_tokens": 30},
        }

    result = generate_ai_brief(
        brief,
        api_client=client,
        api_key="fixture-key",
        output_root=tmp_path / "ai_brief",
    )
    assert result.status == "success"
    assert result.review["evidence_refs"] == brief["source_record_hashes"]
