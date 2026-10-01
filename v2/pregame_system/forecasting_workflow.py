"""Persist a basketball-only judgment before exposing prices to a second call."""
from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .forecasting_skill import load_forecasting_skill
from .skill_provenance import prompt_skill_hashes
from .storage import content_hash, now, save


class ForecastNumbers(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    winner_side: Literal["home", "away"]
    # Preserve the existing product representation and ranges. None of these
    # bounds depends on a market price, and there is no distance-to-market rule.
    win_probability: float = Field(gt=0.5, le=0.99, multiple_of=0.01, strict=True)
    expected_home_margin: float = Field(ge=-40, le=40, multiple_of=0.5, strict=True)
    expected_total: float = Field(ge=160, le=300, multiple_of=0.5, strict=True)

    @model_validator(mode="after")
    def coherent_direction(self):
        if self.expected_home_margin == 0 or (
            (self.expected_home_margin > 0) != (self.winner_side == "home")
        ):
            raise ValueError("forecast_winner_margin_conflict")
        return self


class ForecastFactor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classification: Literal["source_fact", "model_output", "inference"]
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    effect: Literal["home", "away", "higher_total", "lower_total", "uncertainty", "no_change"]
    summary: str = Field(min_length=1, max_length=600)


class IndependentForecast(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prediction: ForecastNumbers
    factors: list[ForecastFactor] = Field(min_length=1, max_length=8)
    uncertainties: list[str] = Field(default_factory=list, max_length=8)


class MarketReviewForecast(IndependentForecast):
    decision: Literal["keep", "revise", "unavailable"]
    market_assessment: Literal["supports", "challenges", "unclear", "unavailable"]
    rationale: str = Field(min_length=1, max_length=4000)


_MARKET_TEXT = re.compile(
    r"\b(?:market|odds|spread|moneyline|handicap|bookmaker|sportsbook|favorite|favourite|"
    r"favored|favoured|over/under|O/U|ATS)\b|盘口|让分|大小分|赔率|水位|热门球队|看好.{0,12}让",
    re.I,
)
_EXTERNAL_MARKET_CLAIM = re.compile(
    r"\b(?:market|odds|moneyline|bookmaker|sportsbook|betting line|opening line|closing line|"
    r"consensus line|posted spread|quoted spread|O/U|ATS)\b|盘口|赔率|水位",
    re.I,
)
_DEFERRED_KEYS = {
    "favorite", "favorite_side", "favorite_team", "whos_favored", "spread",
    "spread_points", "home_handicap_points", "total_line_points", "line_movement",
    "prediction_layers", "basketball_market_adjustment", "public_betting_data",
    "official_numeric", "official_numeric_prediction", "official_market_adjustment",
}
_ANSWER_KEYS = {
    "score_home", "score_away", "actual_winner", "actual_margin", "actual_home_margin",
    "actual_total", "actual_results", "ats_result", "ou_result", "postgame", "quarter_scores",
}
_BASKETBALL_SECTIONS = {
    "availability", "key_players", "team_form", "matchup", "matchup_history",
    "roster_context", "season_form", "season_phase", "features", "team_stats", "rest",
}
_WEB_CATEGORIES = {"availability", "lineup", "roster_news", "game_story", "coach_comments", "player_comments"}


def _reject_answers(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _ANSWER_KEYS:
                raise ValueError("target_answer_field_in_forecast_input:" + str(key))
            _reject_answers(child)
    elif isinstance(value, list):
        for child in value:
            _reject_answers(child)


def _deferred_key(key: str) -> bool:
    return key.lower() in _DEFERRED_KEYS or key.lower().startswith(
        ("market", "residual", "divergence", "moneyline", "odds", "implied_")
    )


def _basketball_only(value, removed: list[str], path: str = ""):
    """Conservative projection; an omitted mixed sentence is never paraphrased."""
    if isinstance(value, dict):
        if any(value.get(key) is False for key in ("usable_for_prediction", "published_before_tipoff", "eligible_for_fact")):
            removed.append(path)
            return None
        result = {}
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if _deferred_key(str(key)):
                removed.append(child_path)
                continue
            item = _basketball_only(child, removed, child_path)
            if item is not None:
                result[key] = item
        return result
    if isinstance(value, list):
        return [v for i, child in enumerate(value)
                if (v := _basketball_only(child, removed, f"{path}[{i}]")) is not None]
    if isinstance(value, str) and _MARKET_TEXT.search(value):
        removed.append(path)
        return None
    return value


def _available(value) -> bool:
    if value is None or value == {} or value == []:
        return False
    if isinstance(value, dict):
        if any(value.get(key) is False for key in ("usable_for_prediction", "published_before_tipoff", "eligible_for_fact")):
            return False
        if value.get("status") in {"unavailable", "missing", "not_available"}:
            return False
        return any(_available(v) for k, v in value.items()
                   if k not in {"status", "reason", "source", "provenance"})
    return True


def _has_quote(value) -> bool:
    numeric_keys = {"spread", "spread_points", "home_spread", "away_spread", "spread_home",
        "spread_away", "home_handicap_points", "market_expected_home_margin", "expected_home_margin",
        "expected_margin", "total", "total_line_points", "expected_total", "total_points",
        "home_moneyline", "away_moneyline", "moneyline_home", "moneyline_away",
        "home_probability_no_vig", "home_win_probability"}
    numeric_keys.update({"market_expected_margin", "market_total", "market_implied_win_probability"})
    if isinstance(value, dict):
        if value.get("status") in {"unavailable", "missing"}:
            return False
        moneyline = value.get("moneyline")
        if isinstance(moneyline, dict) and any(
            isinstance(moneyline.get(side), (int, float)) and not isinstance(moneyline.get(side), bool)
            and math.isfinite(moneyline[side]) for side in ("home", "away")
        ):
            return True
        return any((k in numeric_keys and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v))
                   or _has_quote(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_quote(v) for v in value)
    return False


def _pure_baseline(value):
    """A Pure label cannot override an explicit declaration of price dependence."""
    if not isinstance(value, dict):
        return value
    usage = value.get("market_usage") or {}
    dependent = any(value.get(k) is True for k in ("uses_market", "market_informed", "uses_market_features"))
    dependent = dependent or (isinstance(usage, dict) and usage.get("used") is True)
    dependent = dependent or any(_MARKET_TEXT.search(str(value.get(k, "")))
                                 for k in ("prediction_method", "model_type"))
    return {"status": "unavailable", "reason": "Model does not satisfy independent-input provenance."} if dependent else value


def prepare_pilot(packet: dict) -> tuple[dict, dict, list[str]]:
    """Accept both saved pilot schemas; do not mutate or reseal source packets."""
    _reject_answers(packet)
    target = packet.get("target") or packet.get("game")
    if not isinstance(target, dict):
        raise ValueError("forecast_game_identity_missing")
    # Contradictory narrative in the original enriched Game 41 is deliberately
    # blocked, not silently corrected based on knowledge of the later result.
    h2h = packet.get("h2h") or packet.get("h2h_before") or {}
    meetings = h2h.get("recent_meetings", [])[:3]
    text = json.dumps([packet.get("pregame_news"), packet.get("important_context"), packet.get("sources")])
    if "San Antonio entered 0-3" in text and len(meetings) == 3 and all(
        m.get("prior_game_result_for_okc") == "L" for m in meetings
    ):
        raise ValueError("packet_h2h_narrative_conflict_requires_new_version")
    removed = []
    evidence = []
    fields = ("team_state", "team_state_before", "richer_team_data", "rest", "rest_before",
              "h2h", "h2h_before", "injuries", "roster_changes", "starters", "rotation_notes",
              "pregame_news", "coach_player_comments", "important_context", "missing_information",
              "pregame_context", "sources")
    for key in fields:
        if key not in packet:
            continue
        value = copy.deepcopy(packet[key])
        if key == "team_state" and "richer_team_data" in packet:
            value.pop("regular_season_venue_splits", None)
        evidence.append({"id": f"packet.{key}", "section": key, "value": value})
    baselines = packet.get("quantitative_baselines") or {}
    evidence.append({"id": "model.pure", "section": "pure_prediction",
                     "value": _pure_baseline(baselines.get("pure", {"status": "unavailable"}))})
    basketball = _basketball_only({"game": target, "evidence": evidence}, removed)
    deferred = []
    for key, value in (("reference", packet.get("market_reference")),
                       ("residual", baselines.get("residual")),
                       ("official", baselines.get("official_numeric"))):
        if _available(value):
            deferred.append({"id": f"market.{key}", "section": key, "value": value})
    reference = packet.get("market_reference")
    return basketball, {"evidence": deferred if _available(reference) and _has_quote(reference) else []}, removed


def prepare_analyst(context: dict) -> tuple[dict, dict, list[str]]:
    """Never pass a full AnalystContext to the independent forecasting call."""
    _reject_answers(context)
    removed, evidence, deferred = [], [], []
    identities = []
    for item in context.get("objective_evidence", []):
        section, value = item.get("section"), item.get("value") or {}
        identities.append({k: item[k] for k in ("game_id", "cutoff") if k in item})
        if section == "prediction":
            evidence.append({"id": item["id"], "section": "pure_prediction", "value": _pure_baseline(context["pure_prediction"])})
        elif section in _BASKETBALL_SECTIONS:
            evidence.append(item)
        elif section == "web_context" and value.get("category") in _WEB_CATEGORIES:
            evidence.append(item)
            if _available(value) and _MARKET_TEXT.search(json.dumps(value, ensure_ascii=False)):
                deferred.append({"id": "market.mixed." + item["id"], "section": "mixed_pregame_evidence", "value": value})
        elif section == "market" or (section == "web_context" and value.get("category") == "market"):
            if _available(value):
                deferred.append(item)
    if not any(e["section"] == "pure_prediction" for e in evidence):
        evidence.append({"id": "model.pure", "section": "pure_prediction", "value": _pure_baseline(context["pure_prediction"])})
    for key in ("roster_context", "matchup_history", "season_form", "season_phase"):
        if context.get(key) and not any(e["section"] == key for e in evidence):
            evidence.append({"id": f"context.{key}", "section": key, "value": context[key]})
    for key in ("market_benchmark", "residual_prediction", "official_numeric_prediction", "divergence"):
        if _available(context.get(key)):
            deferred.append({"id": f"market.{key}", "section": key, "value": context[key]})
    # An Official pure-fallback number alone is not a market observation.
    has_market = any(e.get("section") not in {"official_numeric_prediction", "residual_prediction", "divergence"}
                     and _has_quote(e.get("value")) for e in deferred)
    basketball = _basketball_only({"game": context.get("game"), "game_context": identities, "evidence": evidence,
                                   "missing_data": context.get("missing_data", [])}, removed)
    return basketball, {"evidence": deferred if has_market else []}, removed


def _ids(view: dict) -> set[str]:
    return {item["id"] for item in view["evidence"] if item.get("id")}


def _validate_evidence(value: IndependentForecast, allowed: set[str]):
    used = {ref for factor in value.factors for ref in factor.evidence_ids}
    if not used <= allowed:
        raise ValueError("unknown_forecast_evidence")


def _market_absence_only(text: str) -> bool:
    """Permit a missing-input disclosure, never a price or directional claim."""
    if re.search(r"\d|\b(?:but|however|nevertheless|home|away|favor\w*|favour\w*|over|under)\b|但|然而|主队|客队", text, re.I):
        return False
    if len(_MARKET_TEXT.findall(text)) != 1:
        return False
    return bool(re.search(
        r"^\s*(?:No|Without)\s+(?:market|odds|spread|moneyline)\s+(?:information|data|reference|prices?|quotes?)\b"
        r"|^\s*(?:Market|Odds|Spread|Moneyline)\s+(?:information|data|reference|prices?|quotes?).{0,30}\b(?:unavailable|missing|not provided)\b"
        r"|(?:本阶段|当前|本次)?(?:未提供|未给出|未获得|没有|缺少|未开放|暂无|无).{0,8}(?:盘口|赔率)(?:信息|数据|参考)?",
        text, re.I,
    ))


def validate_independent_market_boundary(value: IndependentForecast) -> None:
    for text in [f.summary for f in value.factors] + value.uncertainties:
        # Input projection removes prices and favorite labels conservatively.
        # Output may describe its own projected advantage using "favored" or
        # "spread"; those words alone do not assert an observed market quote.
        if _EXTERNAL_MARKET_CLAIM.search(text) and not _market_absence_only(text):
            raise ValueError("independent_output_invents_market_context")


def default_client(messages, schema):
    import os
    from v2.agent.generate_historical_pregame_analysis import request_deepseek
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise ValueError("deepseek_api_key_missing")
    response = request_deepseek(messages, key, "deepseek-v4-flash", 2400, timeout_seconds=40)
    return response.content, {"usage": response.usage, "finish_reason": response.finish_reason,
                              "provider": getattr(response, "response_metadata", {})}


def run_two_stage(basketball: dict, market: dict, folder: Path, *, client=None,
                  skill_loader=None, removed_paths=(), transport_label=None) -> dict:
    """Two independent requests. The second can only start after durable stage one."""
    client = client or default_client
    skill = (skill_loader or load_forecasting_skill)()
    if not isinstance(skill, str) or not skill.strip():
        raise ValueError("forecasting_skill_missing")
    basketball, market = copy.deepcopy(basketball), copy.deepcopy(market)
    check_removed = []
    if _basketball_only(basketball, check_removed) != basketball:
        raise ValueError("market_content_in_independent_input")
    _reject_answers(basketball)
    _reject_answers(market)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / "projection.json", {"omitted_paths": list(removed_paths),
        "basketball_hash": content_hash(basketball), "market_hash": content_hash(market)})
    calls = 0

    def call(stage, payload, schema):
        nonlocal calls
        request_schema = schema.model_json_schema()
        if schema is MarketReviewForecast:
            # Keep the requested brief format stable while accepting a bounded
            # longer explanation verbatim. Numeric/evidence rules stay strict.
            request_schema["properties"]["rationale"]["maxLength"] = 1000
        protocol = (
            f"当前阶段：{stage}。只使用本次输入的事实；允许据此作有边界的篮球推断，"
            "不得补造伤病、名单、分钟、首发、模型或新闻。unavailable 保持未知。"
            "模型概率若未另作说明按主队概率理解；输出 win_probability 为你选中的赢家概率。"
            "expected_home_margin 始终为主队减客队，expected_total 包含加时。"
            "factors 是可审计的简短依据，引用提供的 evidence id，区分事实、模型输出和推断。"
            "只输出符合以下 schema 的 JSON，不写文章，不请求搜索：\n"
            + json.dumps(request_schema, ensure_ascii=False)
        )
        if stage == "independent":
            protocol += "\n当前未开放市场信息。依据 Pure 和篮球证据生成独立预测，跳过 Skill 中尚需市场资料的步骤。"
        else:
            protocol += (
                "\n独立预测已保存为审计记录，其数字是待校准估计，不预设保持。"
                "盘口本身提供新增的外部定价信息；调整不要求出现新的伤病、首发或新闻。"
                "对比独立判断与市场参考的依据质量，分别复核分差和总分。"
                "自身模型或阵容依据不足时，要认真考虑市场校准；有更具体可靠的篮球依据时也可保留分歧。"
                "复核后可保持或修改，接近或远离市场均允许。"
                "不要设置固定权重或为了制造差异而调整。Residual/Official 可能已含市场，不能重复计权。"
                "报价无准确时间时降低对时点的把握，仍可有边界地用于校准；不认定已包含或未包含某条消息。"
                "keep 和 revise 都必须引用篮球及市场证据并解释两边依据的取舍。"
                "没有新增篮球事实或没有报价时间戳，均不能单独作为保持原预测的充分理由。"
                "keep 的数字须与独立预测一致；revise 说明改动依据，不要求固定调整幅度。"
            )
        messages = [{"role": "system", "content": skill + "\n\n" + protocol},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, allow_nan=False)}]
        save(folder / f"{stage}_request.json", {"created_at": now(), "messages": messages,
            "schema": request_schema, "validation_schema": schema.model_json_schema(), "input_hash": content_hash(messages),
            "skill_hashes": prompt_skill_hashes(skill), "model": "deepseek-v4-flash",
            "transport": transport_label or ("injected" if client is not default_client else "configured_deepseek"),
            "default_request_settings": {"temperature": 0.1, "thinking": "disabled",
                                         "max_tokens": 2400, "timeout_seconds": 40},
            "automatic_retries": False})
        calls += 1
        payload, usage = client(messages, schema)
        save(folder / f"{stage}_response.json", {"received_at": now(), "raw": payload, "usage": usage})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError("forecast_stage_json_invalid") from exc
        parsed = schema.model_validate(payload)
        save(folder / f"{stage}_validation.json", {"schema_validated": True,
            "accepted_long_rationale": schema is MarketReviewForecast and len(parsed.rationale) > 1000,
            "response_content_edited": False})
        return parsed

    try:
        independent = call("independent", basketball, IndependentForecast)
        _validate_evidence(independent, _ids(basketball))
        validate_independent_market_boundary(independent)
        independent_value = independent.model_dump()
        independent_hash = content_hash(independent_value)
        save(folder / "independent.json", {"prediction_and_basis": independent_value,
            "sha256": independent_hash, "saved_at": now()})
        saved = json.loads((folder / "independent.json").read_text(encoding="utf-8"))
        if saved["sha256"] != independent_hash or content_hash(saved["prediction_and_basis"]) != independent_hash:
            raise ValueError("independent_receipt_integrity_failure")
        independent_value = saved["prediction_and_basis"]
        if market["evidence"]:
            review = call("market_review", {"basketball": basketball,
                "independent": independent_value, "independent_sha256": independent_hash,
                "market_context": market}, MarketReviewForecast)
            _validate_evidence(review, _ids(basketball) | _ids(market))
            changed = review.prediction != independent.prediction
            if review.decision == "unavailable" or changed != (review.decision == "revise"):
                raise ValueError("market_review_decision_conflicts_with_numbers")
            used = {ref for f in review.factors for ref in f.evidence_ids}
            if not used.intersection(_ids(basketball)) or not used.intersection(_ids(market)):
                raise ValueError("review_requires_basketball_and_market_evidence")
        else:
            review = MarketReviewForecast(**independent_value, decision="unavailable",
                market_assessment="unavailable", rationale="未获得可用市场资料，保留独立预测。")
        final_value = review.model_dump()
        before, after = independent.prediction, review.prediction
        result = {"schema_version": "two-stage-forecast-v1", "independent": independent_value,
            "independent_sha256": independent_hash, "review": final_value,
            "adjustment": {"home_margin": after.expected_home_margin - before.expected_home_margin,
                "total": after.expected_total - before.expected_total,
                "home_win_probability": (after.win_probability if after.winner_side == "home" else 1-after.win_probability)
                    - (before.win_probability if before.winner_side == "home" else 1-before.win_probability)},
            "calls": calls, "final_prediction": after.model_dump(), "audit_path": str(folder),
            "skill_hashes": prompt_skill_hashes(skill)}
        save(folder / "final.json", result)
        return result
    except Exception as exc:
        save(folder / "failure.json", {"failed_at": now(), "error_type": type(exc).__name__,
                                       "calls": calls, "final_prediction_published": False})
        raise
