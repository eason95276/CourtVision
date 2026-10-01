"""Explicit, evidence-bounded AI postgame review entrypoint."""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from v2.agent.generate_postgame_brief import request_deepseek
from v2.pregame_system.storage import clean, content_hash, now, save


FORBIDDEN_CAUSAL = (
    "证明了战术",
    "防守策略导致",
    "教练安排导致",
    "伤病导致",
    "对位导致",
)


@dataclass(frozen=True)
class AIReviewResult:
    status: str
    review: dict[str, Any] | None = None
    output_path: Path | None = None
    failure_path: Path | None = None
    usage: dict[str, Any] | None = None
    error_type: str | None = None


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_hash": record["record_hash"],
        "game": record["game"],
        "prediction_review": record["prediction_review"],
        "key_factors": record["key_factors"],
        "key_players": record["key_players"],
        "pregame_judgments": record["pregame_judgments"],
        "missing_data": record["missing_data"],
        "limitations": record["limitations"],
    }


def _messages(record: dict[str, Any]) -> list[dict[str, str]]:
    system = (
        "你只解释 CourtVision 已验证的赛后记录。不得生成新数字、球员、盘口、伤病或战术因果。"
        "返回 JSON，字段严格为 summary、deep_review、evidence_ids、limitations。"
        "evidence_ids 只能从 key_factors 中选择。"
    )
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(_record_payload(record), ensure_ascii=False),
        },
    ]


def _validate_review(content: str, record: dict[str, Any], secret: str) -> dict[str, Any]:
    if secret and secret in content:
        raise ValueError("credential_leak")
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("ai_review_invalid_json") from exc
    if set(value) != {"summary", "deep_review", "evidence_ids", "limitations"}:
        raise ValueError("ai_review_wrong_fields")
    for field in ("summary", "deep_review", "limitations"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError("ai_review_empty_text")
    prose = " ".join(value[field] for field in ("summary", "deep_review", "limitations"))
    if any(term in prose for term in FORBIDDEN_CAUSAL):
        raise ValueError("ai_review_forbidden_causal_claim")
    allowed_numbers = set()
    def collect(item):
        if isinstance(item, dict):
            for child in item.values():collect(child)
        elif isinstance(item, list):
            for child in item:collect(child)
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            allowed_numbers.add(round(float(item), 6))
    collect(_record_payload(record))
    mentioned = [float(token) for token in re.findall(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?", prose)]
    if any(round(number, 6) not in allowed_numbers for number in mentioned):
        raise ValueError("ai_review_unverified_number")
    allowed = {row["evidence_id"] for row in record["key_factors"]}
    ids = value["evidence_ids"]
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
        raise ValueError("ai_review_invalid_evidence_ids")
    if any(item not in allowed for item in ids):
        raise ValueError("ai_review_unknown_evidence_id")
    return clean(value)


def generate_ai_review(
    record: dict[str, Any],
    *,
    output_root: str | Path,
    api_client: Callable | None = None,
    api_key: str | None = None,
    model: str = "deepseek-v4-flash",
    timeout_seconds: float = 40.0,
) -> AIReviewResult:
    root = Path(output_root) / (now().replace(":", "").replace("+", "_") + "_" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    messages = _messages(record)
    key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
    attempt = {
        "created_at": now(),
        "record_hash": record["record_hash"],
        "input_hash": content_hash(messages),
        "model": model,
        "max_retries": 0,
        "timeout_seconds": timeout_seconds,
    }
    save(root / "attempt.json", attempt)
    if not key.strip() and api_client is None:
        failure = {
            "status": "failed",
            "error_type": "MissingAPIKey",
            "message": "DEEPSEEK_API_KEY is required only for an explicit AI review",
            "record_hash": record["record_hash"],
        }
        path = save(root / "failure.json", failure)
        return AIReviewResult(status="failed", failure_path=path, error_type="MissingAPIKey")
    try:
        if api_client is None:
            content = request_deepseek(messages, key, model, timeout_seconds)
            response = {"content": content, "usage": {}, "raw": None}
        else:
            raw = api_client(messages, key, model, timeout_seconds)
            response = raw if isinstance(raw, dict) else {"content": raw, "usage": {}, "raw": raw}
        save(
            root / "response.json",
            {
                "received_at": now(),
                "content": response.get("content"),
                "usage": clean(response.get("usage") or {}),
                "raw": clean(response.get("raw")),
            },
        )
        review = _validate_review(str(response.get("content") or ""), record, key)
        output = {
            "status": "success",
            "record_hash": record["record_hash"],
            "model": model,
            "review": review,
            "usage": clean(response.get("usage") or {}),
            "generated_at": now(),
        }
        output["output_hash"] = content_hash(output)
        path = save(root / "review.json", output)
        return AIReviewResult(
            status="success",
            review=review,
            output_path=path,
            usage=output["usage"],
        )
    except Exception as exc:
        message = str(exc).replace(key, "[REDACTED]")[:500] if key else str(exc)[:500]
        failure = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "message": message,
            "record_hash": record["record_hash"],
            "failed_at": now(),
        }
        path = save(root / "failure.json", failure)
        return AIReviewResult(
            status="failed",
            failure_path=path,
            error_type=type(exc).__name__,
        )


def generate_ai_brief(
    brief: dict[str, Any],
    *,
    output_root: str | Path,
    api_client: Callable | None = None,
    api_key: str | None = None,
    model: str = "deepseek-v4-flash",
    timeout_seconds: float = 40.0,
) -> AIReviewResult:
    """Organize an already computed brief without adding facts or selections."""
    root = Path(output_root) / (now().replace(":", "").replace("+", "_") + "_" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    allowed_refs = list(brief.get("source_record_hashes") or [])
    messages = [
        {
            "role": "system",
            "content": (
                "你只组织已经计算完成的 NBA 简报。不得增加比赛、球队、排名、数字或因果结论。"
                "返回 JSON，字段严格为 summary、highlights、evidence_refs、limitations。"
            ),
        },
        {"role": "user", "content": json.dumps(brief, ensure_ascii=False)},
    ]
    key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
    save(
        root / "attempt.json",
        {
            "created_at": now(),
            "brief_hash": brief["brief_hash"],
            "input_hash": content_hash(messages),
            "model": model,
            "max_retries": 0,
            "timeout_seconds": timeout_seconds,
        },
    )
    if not key.strip() and api_client is None:
        path = save(
            root / "failure.json",
            {"status": "failed", "error_type": "MissingAPIKey", "brief_hash": brief["brief_hash"]},
        )
        return AIReviewResult(status="failed", failure_path=path, error_type="MissingAPIKey")
    try:
        if api_client is None:
            content = request_deepseek(messages, key, model, timeout_seconds)
            response = {"content": content, "usage": {}, "raw": None}
        else:
            raw = api_client(messages, key, model, timeout_seconds)
            response = raw if isinstance(raw, dict) else {"content": raw, "usage": {}, "raw": raw}
        save(root / "response.json", {"received_at": now(), **clean(response)})
        value = json.loads(str(response.get("content") or ""))
        if set(value) != {"summary", "highlights", "evidence_refs", "limitations"}:
            raise ValueError("ai_brief_wrong_fields")
        if not isinstance(value["summary"], str) or not value["summary"].strip():
            raise ValueError("ai_brief_empty_summary")
        if not isinstance(value["limitations"], str) or not value["limitations"].strip():
            raise ValueError("ai_brief_empty_limitations")
        if not isinstance(value["highlights"], list) or not value["highlights"]:
            raise ValueError("ai_brief_invalid_highlights")
        if any(not isinstance(item, str) or not item.strip() for item in value["highlights"]):
            raise ValueError("ai_brief_invalid_highlights")
        refs = value["evidence_refs"]
        if not isinstance(refs, list) or any(item not in allowed_refs for item in refs):
            raise ValueError("ai_brief_unknown_evidence")
        allowed_numbers = set()
        def collect_brief_numbers(item):
            if isinstance(item, dict):
                for child in item.values():collect_brief_numbers(child)
            elif isinstance(item, list):
                for child in item:collect_brief_numbers(child)
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                allowed_numbers.add(round(float(item), 6))
        collect_brief_numbers(brief)
        prose = " ".join([value["summary"], *value["highlights"], value["limitations"]])
        mentioned = [float(token) for token in re.findall(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?", prose)]
        if any(round(number, 6) not in allowed_numbers for number in mentioned):
            raise ValueError("ai_brief_unverified_number")
        if key and key in json.dumps(value, ensure_ascii=False):
            raise ValueError("credential_leak")
        output = {
            "status": "success",
            "brief_hash": brief["brief_hash"],
            "model": model,
            "review": clean(value),
            "usage": clean(response.get("usage") or {}),
            "generated_at": now(),
        }
        output["output_hash"] = content_hash(output)
        path = save(root / "brief_review.json", output)
        return AIReviewResult(status="success", review=output["review"], output_path=path, usage=output["usage"])
    except Exception as exc:
        message = str(exc).replace(key, "[REDACTED]")[:500] if key else str(exc)[:500]
        path = save(
            root / "failure.json",
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "message": message,
                "brief_hash": brief["brief_hash"],
            },
        )
        return AIReviewResult(status="failed", failure_path=path, error_type=type(exc).__name__)
