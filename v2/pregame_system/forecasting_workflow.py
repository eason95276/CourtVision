"""Persist a basketball-only judgment before exposing prices to a second call."""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .forecasting_skill import load_forecasting_skill
from .skill_provenance import prompt_skill_hashes
from .storage import content_hash, now, save
from .pregame_estimates import PregamePlan, validate_pregame_plan
from .ats import ATS_EPSILON, build_spread_comparison


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
    pregame_plan: PregamePlan | None = None


class MarketReviewForecast(IndependentForecast):
    decision: Literal["keep", "revise", "unavailable"]
    market_assessment: Literal["supports", "challenges", "unclear", "unavailable"]
    rationale: str = Field(min_length=1, max_length=4000)


class ForecastClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=300,
                           description="一到两句，给出本场最关键的篮球判断；不要复述 factors。")
    classification: Literal["source_fact", "inference"]
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    confidence: Literal["low", "medium", "high"]


class KeyMatchup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=120)
    opponent_or_scheme: str = Field(min_length=1, max_length=160)
    why_it_matters: str = Field(min_length=1, max_length=220)
    expected_effect: str = Field(min_length=1, max_length=220)
    classification: Literal["source_fact", "inference"]
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    confidence: Literal["low", "medium", "high"]
    uncertainty: str | None = Field(default=None, max_length=160)


class FlipCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition: str = Field(min_length=1, max_length=220)
    judgment_effect: str = Field(min_length=1, max_length=220)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)


class AnalysisCore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    central_question: str = Field(min_length=1, max_length=180,
                                  description="一句简洁问题，点出本场最关键的篮球矛盾。")
    central_thesis: ForecastClaim
    matchup_status: Literal["identified", "no_clear_matchup"]
    key_matchups: list[KeyMatchup] = Field(default_factory=list, max_length=3)
    expected_game_script: str = Field(min_length=1, max_length=480,
                                      description="一个短段落，说明最可能的比赛路径；不复述 thesis 或 factors。")
    flip_conditions: list[FlipCondition] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def matchup_status_matches_content(self):
        if self.matchup_status == "identified" and not self.key_matchups:
            raise ValueError("analysis_core_matchup_required")
        if self.matchup_status == "no_clear_matchup" and self.key_matchups:
            raise ValueError("analysis_core_matchup_status_conflict")
        return self


class VersionedIndependentForecast(IndependentForecast):
    """Strict analysis contract for new Prediction Version generation only."""
    factors: list[ForecastFactor] = Field(min_length=1, max_length=6,
        description="最多六条彼此独立、简短的数值依据；不要重述 analysis_core。")
    uncertainties: list[str] = Field(default_factory=list, max_length=5,
        description="只列尚未被 flip_conditions 或 pregame_plan 表达的关键未知。")
    analysis_core: AnalysisCore


class MarketRevisionReason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market_signal_factor_indices: list[int] = Field(min_length=1, max_length=2)
    basketball_reweighting_factor_indices: list[int] = Field(default_factory=list, max_length=4)


class VersionedMarketReviewForecast(VersionedIndependentForecast):
    decision: Literal["keep", "revise", "unavailable"]
    market_assessment: Literal["supports", "challenges", "unclear", "unavailable"]
    rationale: str = Field(min_length=1, max_length=4000)
    revision_reason: MarketRevisionReason | None = None

    @model_validator(mode="after")
    def revision_reason_is_present_for_market_decisions(self):
        if self.decision in {"keep", "revise"} and self.revision_reason is None:
            raise ValueError("market_revision_reason_required")
        if self.decision == "revise" and not self.revision_reason.basketball_reweighting_factor_indices:
            raise ValueError("market_revision_requires_basketball_reweighting")
        return self


V2_INDEPENDENT_MAX_TOKENS = 4800
V2_MARKET_REVIEW_MAX_TOKENS = 3600


def _forecast_max_tokens(schema, game_type: str | None) -> int:
    """Use larger budgets only for the new versioned contract; keep v1 unchanged."""
    if schema is VersionedIndependentForecast:
        return V2_INDEPENDENT_MAX_TOKENS
    if schema is VersionedMarketReviewForecast:
        return V2_MARKET_REVIEW_MAX_TOKENS
    return 4200 if game_type == "preseason" else 2400


def _compact_v2_request_schema(request_schema: dict[str, Any], schema, stage: str) -> None:
    """Constrain only the V2 request schema; runtime validation and v1 stay compatible."""
    if schema not in {VersionedIndependentForecast, VersionedMarketReviewForecast}:
        return
    definitions = request_schema.get("$defs") or {}
    properties = request_schema.get("properties") or {}

    factor_field = properties.get("factors") or {}
    factor_field["maxItems"] = 6
    factor_field["description"] = "只列最重要、彼此独立的数值依据；每条简短，不复述 analysis_core。"
    factor_definition = definitions.get("ForecastFactor") or {}
    factor_summary = (factor_definition.get("properties") or {}).get("summary")
    if isinstance(factor_summary, dict):
        factor_summary["maxLength"] = 280
        factor_summary["description"] = "一句话说明独立依据及其方向。"

    uncertainty_field = properties.get("uncertainties") or {}
    uncertainty_field["maxItems"] = 5
    uncertainty_field["description"] = "只保留未在 flip_conditions 或 pregame_plan 中表达的关键未知。"
    uncertainty_items = uncertainty_field.get("items")
    if isinstance(uncertainty_items, dict):
        uncertainty_items["maxLength"] = 220

    core_definition = definitions.get("AnalysisCore") or {}
    core = core_definition.get("properties") or {}
    core_definition["description"] = (
        "完整但精简的结构化篮球判断；每个字段各自承担不同信息，不相互复述。"
    )
    thesis = definitions.get("ForecastClaim") or {}
    thesis_properties = thesis.get("properties") or {}
    thesis_field = core.get("central_thesis")
    if isinstance(thesis_field, dict):
        thesis_field["description"] = (
            "必须是 ForecastClaim 对象，不得返回字符串；自然语言判断只写入 statement。"
        )
    thesis_statement = thesis_properties.get("statement")
    if isinstance(thesis_statement, dict):
        thesis_statement["maxLength"] = 300
        thesis_statement["description"] = "一到两句，表达本场核心判断；不要重述 factors。"
    for name, description in {
        "classification": "source_fact 表示来源直接支持；否则使用 inference。",
        "evidence_ids": "只引用本次输入中直接支持 statement 的真实 evidence ID。",
        "confidence": "对该 thesis 的判断把握：low、medium 或 high。",
    }.items():
        value = thesis_properties.get(name)
        if isinstance(value, dict):
            value["description"] = description
    question = core.get("central_question")
    if isinstance(question, dict):
        question["maxLength"] = 180
        question["description"] = "一句话提出本场核心篮球问题。"
    matchups = core.get("key_matchups") or {}
    matchup_items = (matchups.get("items") or {}).get("$ref", "").rsplit("/", 1)[-1]
    matchup_definition = definitions.get(matchup_items) or {}
    matchup_properties = matchup_definition.get("properties") or {}
    matchup_limits = {"subject": 120, "opponent_or_scheme": 160,
                      "why_it_matters": 220, "expected_effect": 220,
                      "uncertainty": 160}
    for name, limit in matchup_limits.items():
        value = matchup_properties.get(name)
        if isinstance(value, dict):
            value["maxLength"] = limit
    if isinstance(matchups, dict):
        matchups["description"] = "零到三条真正不同的关键对位；不要重复 factors 或比赛脚本。"
    script = core.get("expected_game_script")
    if isinstance(script, dict):
        script["maxLength"] = 480
        script["description"] = "一个短段落，描述最可能的比赛路径。"
    flips = core.get("flip_conditions") or {}
    if isinstance(flips, dict):
        flips["description"] = "零到四条真正可能改变判断的条件；不要复述一般性未知。"
    flip_ref = (flips.get("items") or {}).get("$ref", "").rsplit("/", 1)[-1]
    flip_properties = (definitions.get(flip_ref) or {}).get("properties") or {}
    for name in ("condition", "judgment_effect"):
        value = flip_properties.get(name)
        if isinstance(value, dict):
            value["maxLength"] = 220

    # PregamePlan keeps the existing fields/schema, but the V2 request makes its
    # role operational: lineups and material player-load estimates, not a second essay.
    plan_definition = definitions.get("PregamePlan") or {}
    plan_properties = plan_definition.get("properties") or {}
    plan_field = properties.get("pregame_plan")
    if isinstance(plan_field, dict):
        plan_field["description"] = (
            "只提供首发依据和会影响预测的球员负荷估计；不得复述 analysis_core、factors 或 game script。"
        )
    key_matchup = plan_properties.get("key_matchup")
    if isinstance(key_matchup, dict):
        key_matchup["maxLength"] = 120
        key_matchup["description"] = "简短操作性标签，不写第二段对位分析。"
    plan_uncertainty = plan_properties.get("uncertainty") or {}
    if isinstance(plan_uncertainty, dict):
        plan_uncertainty["maxItems"] = 4
        plan_uncertainty["description"] = "只写首发/球员负荷计划本身尚未确认的事项。"
        items = plan_uncertainty.get("items")
        if isinstance(items, dict):
            items["maxLength"] = 180
    if stage.startswith("independent"):
        usage = plan_properties.get("player_usage") or {}
        if isinstance(usage, dict):
            usage["maxItems"] = 6
        usage_ref = (usage.get("items") or {}).get("$ref", "").rsplit("/", 1)[-1]
        usage_properties = (definitions.get(usage_ref) or {}).get("properties") or {}
        rationale = usage_properties.get("rationale")
        if isinstance(rationale, dict):
            rationale["maxLength"] = 220
        lineup_ref = (plan_properties.get("lineups") or {}).get("items", {}).get("$ref", "").rsplit("/", 1)[-1]
        lineup_properties = (definitions.get(lineup_ref) or {}).get("properties") or {}
        rationale = lineup_properties.get("rationale")
        if isinstance(rationale, dict):
            rationale["maxLength"] = 220


def _preserve_existing_repair_values(original: Any, repaired: Any, path: str) -> None:
    """A schema repair may add missing metadata, but cannot rewrite supplied content."""
    if isinstance(original, dict):
        if not isinstance(repaired, dict):
            raise ValueError("schema_repair_changed_object:" + path)
        for key, value in original.items():
            if key not in repaired:
                raise ValueError("schema_repair_dropped_existing_field:" + path + "." + key)
            _preserve_existing_repair_values(value, repaired[key], path + "." + key)
        return
    if isinstance(original, list):
        if not isinstance(repaired, list) or len(original) != len(repaired):
            raise ValueError("schema_repair_changed_list:" + path)
        for index, (before, after) in enumerate(zip(original, repaired)):
            _preserve_existing_repair_values(before, after, f"{path}[{index}]")
        return
    if original != repaired:
        raise ValueError("schema_repair_changed_existing_value:" + path)


def _validate_schema_repair_preserves(original: dict[str, Any], repaired: VersionedIndependentForecast) -> None:
    """Lock prediction and existing analysis text; only thesis metadata can be added."""
    allowed_top_level = set(VersionedIndependentForecast.model_fields)
    if set(original) - allowed_top_level:
        raise ValueError("schema_repair_would_drop_unknown_top_level_content")
    if "prediction" not in original:
        raise ValueError("schema_repair_original_prediction_missing")
    locked_prediction = ForecastNumbers.model_validate(original["prediction"]).model_dump()
    if repaired.prediction.model_dump() != locked_prediction:
        raise ValueError("schema_repair_changed_prediction")

    before_core = original.get("analysis_core")
    if not isinstance(before_core, dict):
        raise ValueError("schema_repair_original_analysis_core_missing")
    required_content = ("central_question", "central_thesis", "matchup_status", "expected_game_script")
    if any(key not in before_core for key in required_content):
        raise ValueError("schema_repair_core_content_missing")
    after_core = repaired.analysis_core.model_dump()
    before_thesis = before_core["central_thesis"]
    after_thesis = after_core["central_thesis"]
    if isinstance(before_thesis, str):
        # This is the one allowed shape conversion: retain the model's sentence verbatim.
        if after_thesis.get("statement") != before_thesis:
            raise ValueError("schema_repair_changed_thesis_statement")
    elif isinstance(before_thesis, dict):
        if not isinstance(before_thesis.get("statement"), str) or not before_thesis["statement"].strip():
            raise ValueError("schema_repair_original_thesis_statement_missing")
        if after_thesis.get("statement") != before_thesis["statement"]:
            raise ValueError("schema_repair_changed_thesis_statement")
        if set(before_thesis) - {"statement", "classification", "evidence_ids", "confidence"}:
            raise ValueError("schema_repair_thesis_has_unknown_content")
        allowed_classifications = {"source_fact", "inference"}
        if before_thesis.get("classification") in allowed_classifications and (
            after_thesis.get("classification") != before_thesis["classification"]
        ):
            raise ValueError("schema_repair_changed_thesis_classification")
        if before_thesis.get("confidence") in {"low", "medium", "high"} and (
            after_thesis.get("confidence") != before_thesis["confidence"]
        ):
            raise ValueError("schema_repair_changed_thesis_confidence")
        original_refs = before_thesis.get("evidence_ids")
        if isinstance(original_refs, list) and original_refs and all(isinstance(ref, str) and ref for ref in original_refs):
            if after_thesis.get("evidence_ids") != original_refs:
                raise ValueError("schema_repair_changed_existing_thesis_evidence")
    else:
        raise ValueError("schema_repair_original_thesis_content_missing")

    for key, value in before_core.items():
        if key == "central_thesis":
            continue
        if key not in after_core:
            raise ValueError("schema_repair_dropped_core_field:" + key)
        _preserve_existing_repair_values(value, after_core[key], "analysis_core." + key)
    # Empty optional arrays may be materialized from their schema defaults; new
    # matchup/flip content may not be invented during a schema-only repair.
    for key in ("key_matchups", "flip_conditions"):
        if key not in before_core and after_core.get(key):
            raise ValueError("schema_repair_added_analysis_content:" + key)
    for key in ("factors", "uncertainties", "pregame_plan"):
        if key not in original:
            if key == "factors" and repaired.factors:
                raise ValueError("schema_repair_added_factors")
            if key == "uncertainties" and repaired.uncertainties:
                raise ValueError("schema_repair_added_uncertainties")
            if key == "pregame_plan" and repaired.pregame_plan is not None:
                raise ValueError("schema_repair_added_pregame_plan")
            continue
        after = getattr(repaired, key)
        if isinstance(after, list):
            after_value = [item.model_dump() if hasattr(item, "model_dump") else item for item in after]
        else:
            after_value = after.model_dump() if hasattr(after, "model_dump") else after
        _preserve_existing_repair_values(original[key], after_value, key)


_UNSUPPORTED_MARKET_OVERLAP = re.compile(
    r"(?:market|odds|bookmaker|sportsbook).{0,100}(?:same public information|same information|"
    r"already knew|had already priced|already factored|fully incorporated)|"
    r"(?:盘口|市场|博彩公司).{0,100}(?:同样的?公开信息|已经知道|已计入|已反映)", re.I,
)


def market_timing_status(collection: dict[str, Any] | None) -> str:
    """Publication time and observation time are not interchangeable."""
    collection = collection or {}
    candidates = [collection.get("public_market_reference") or {}]
    if any(collection.get(key) for key in ("published_at", "observed_at", "captured_at")):
        candidates.insert(0, collection)
    candidates += list(collection.get("records") or []) + list(collection.get("sources") or [])
    observed = False
    date_only = False
    for item in candidates:
        published = item.get("published_at")
        if published:
            raw = str(published).strip()
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None and parsed.tzinfo is not None and ("T" in raw or " " in raw):
                return "published_at_verified"
            date_only = True
        observed = observed or bool(item.get("observed_at") or item.get("captured_at"))
    if date_only:
        return "published_date_only"
    return "observed_only" if observed else "unknown"


def build_revision_delta(independent: dict[str, Any], final: dict[str, Any]) -> dict[str, Any]:
    """Compare predictions deterministically in a common home-team perspective."""
    before = independent.model_dump() if hasattr(independent, "model_dump") else dict(independent)
    after = final.model_dump() if hasattr(final, "model_dump") else dict(final)

    def home_probability(value):
        return value["win_probability"] if value["winner_side"] == "home" else 1 - value["win_probability"]

    probability_delta = home_probability(after) - home_probability(before)
    margin_delta = after["expected_home_margin"] - before["expected_home_margin"]
    total_delta = after["expected_total"] - before["expected_total"]
    winner_changed = before["winner_side"] != after["winner_side"]
    changed_axes = []
    if winner_changed:
        changed_axes.append("winner")
    if abs(probability_delta) > 1e-9:
        changed_axes.append("win_probability")
    if abs(margin_delta) > 1e-9:
        changed_axes.append("expected_home_margin")
    if abs(total_delta) > 1e-9:
        changed_axes.append("expected_total")
    return {"winner_changed": winner_changed,
            "home_win_probability_delta": round(probability_delta, 10),
            "expected_home_margin_delta": round(margin_delta, 10),
            "expected_total_delta": round(total_delta, 10),
            "changed_axes": changed_axes}


def validate_market_tiebreak_repair(previous: VersionedMarketReviewForecast,
                                    candidate: VersionedMarketReviewForecast,
                                    independent: IndependentForecast,
                                    market_implied_home_margin: float) -> None:
    """Allow only a basketball-chosen side of the line, not a second free rewrite."""
    before, after = previous.model_dump(), candidate.model_dump()
    mutable = {"prediction", "decision", "rationale", "revision_reason"}
    for key in before.keys() - mutable:
        if before.get(key) != after.get(key):
            raise ValueError("market_tie_break_changed_locked_field:" + key)

    if candidate.prediction.expected_total != previous.prediction.expected_total:
        raise ValueError("market_tie_break_changed_total")
    old_home_probability = (previous.prediction.win_probability
                           if previous.prediction.winner_side == "home"
                           else 1 - previous.prediction.win_probability)
    new_home_probability = (candidate.prediction.win_probability
                            if candidate.prediction.winner_side == "home"
                            else 1 - candidate.prediction.win_probability)
    if abs(old_home_probability - new_home_probability) > 1e-9:
        raise ValueError("market_tie_break_changed_probability_beyond_side_conversion")
    if abs(candidate.prediction.expected_home_margin - market_implied_home_margin) <= ATS_EPSILON:
        raise ValueError("market_review_tiebreak_final_spread_edge_zero")
    changed = candidate.prediction != independent.prediction
    if candidate.decision == "unavailable" or changed != (candidate.decision == "revise"):
        raise ValueError("market_review_decision_conflicts_with_numbers")


def validate_market_revision_reason(review: VersionedMarketReviewForecast,
                                    basketball_ids: set[str], market_ids: set[str]) -> None:
    reason = review.revision_reason
    if review.decision == "unavailable":
        return
    if reason is None:
        raise ValueError("market_revision_reason_required")
    factors = review.factors
    market_indices = reason.market_signal_factor_indices
    basketball_indices = reason.basketball_reweighting_factor_indices
    if any(index < 0 or index >= len(factors) for index in market_indices + basketball_indices):
        raise ValueError("market_revision_factor_index_invalid")
    if not any(set(factors[index].evidence_ids) & market_ids for index in market_indices):
        raise ValueError("market_signal_requires_market_evidence")
    if any(_INTERNAL_MODEL_LABEL.search(factors[index].summary) for index in basketball_indices):
        raise ValueError("market_reweighting_public_reason_contains_internal_model_label")
    if review.decision == "revise" and not any(
            set(factors[index].evidence_ids) & basketball_ids for index in basketball_indices):
        raise ValueError("market_revision_requires_basketball_evidence")
def validate_market_timing_claims(review, timing_status: str) -> None:
    """A timestamped observation alone cannot prove what information a book had."""
    if timing_status == "published_at_verified":
        return
    statements = []
    if getattr(review, "rationale", None):
        statements.append(review.rationale)
    statements.extend(factor.summary for factor in review.factors)
    statements.extend(review.uncertainties or [])
    core = getattr(review, "analysis_core", None)
    if core is not None:
        statements.extend((core.central_question, core.central_thesis.statement, core.expected_game_script))
        for matchup in core.key_matchups:
            statements.extend((matchup.subject, matchup.opponent_or_scheme,
                matchup.why_it_matters, matchup.expected_effect))
        for condition in core.flip_conditions:
            statements.extend((condition.condition, condition.judgment_effect))
    if any(_UNSUPPORTED_MARKET_OVERLAP.search(text) for text in statements):
        raise ValueError("market_information_overlap_unverified")


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
_INTERNAL_MODEL_LABEL = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:Pure|Residual|ML|Official Numeric Forecast)(?![A-Za-z0-9])|纯篮球预测|纯篮球模型|内部模型"
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
    "roster_context", "season_form", "season_phase", "fact_context", "features", "team_stats", "rest",
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
    for key in ("roster_context", "matchup_history", "season_form", "season_phase", "fact_context"):
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
    core = getattr(value, "analysis_core", None)
    if core is not None:
        used.update(core.central_thesis.evidence_ids)
        for matchup in core.key_matchups:
            used.update(matchup.evidence_ids)
        for condition in core.flip_conditions:
            used.update(condition.evidence_ids)
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
    texts = [f.summary for f in value.factors] + value.uncertainties
    core = getattr(value, "analysis_core", None)
    if core is not None:
        texts.extend([core.central_question, core.central_thesis.statement, core.expected_game_script])
        texts.extend(text for matchup in core.key_matchups
                     for text in (matchup.subject, matchup.opponent_or_scheme,
                                  matchup.why_it_matters, matchup.expected_effect))
        texts.extend(text for condition in core.flip_conditions
                     for text in (condition.condition, condition.judgment_effect))
    for text in texts:
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
    payload = json.loads(next(m["content"] for m in messages if m["role"] == "user"))
    game = (payload.get("basketball") or payload).get("game") or {}
    tokens = _forecast_max_tokens(schema, game.get("game_type"))
    timeout=60 if game.get('game_type')=='preseason' else 40
    if schema in {MarketReviewForecast, VersionedMarketReviewForecast} and game.get('registered_game'):
        from .storage import instant
        remaining=(instant(game['scheduled_tipoff'])-instant(now())).total_seconds()
        timeout=min(timeout,remaining-40)
        if timeout<5:raise ValueError('market_review_budget_insufficient')
    response = request_deepseek(messages, key, "deepseek-v4-flash", tokens,
                               timeout_seconds=timeout)
    return response.content, {"usage": response.usage, "finish_reason": response.finish_reason,
                              "provider": getattr(response, "response_metadata", {})}


def _run_two_stage_impl(basketball: dict, market: dict, folder: Path, *, client=None,
                        skill_loader=None, removed_paths=(), transport_label=None, market_collector=None,
                        market_guard=None, require_analysis_core=False) -> dict:
    """Two independent requests. The second can only start after durable stage one."""
    client = client or default_client
    skill = (skill_loader or load_forecasting_skill)()
    if not isinstance(skill, str) or not skill.strip():
        raise ValueError("forecasting_skill_missing")
    independent_schema = VersionedIndependentForecast if require_analysis_core else IndependentForecast
    market_review_schema = VersionedMarketReviewForecast if require_analysis_core else MarketReviewForecast
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

    def call(stage, payload, schema, *, correction=None, correction_index=1, ats_tiebreak=False,
             messages_override=None):
        nonlocal calls
        request_schema = schema.model_json_schema()
        if schema in {MarketReviewForecast, VersionedMarketReviewForecast}:
            # Keep the requested brief format stable while accepting a bounded
            # longer explanation verbatim. Numeric/evidence rules stay strict.
            request_schema["properties"]["rationale"]["maxLength"] = (
                900 if schema is VersionedMarketReviewForecast else 1000
            )
        if require_analysis_core and stage in {"market_review", "market_review_tiebreak"}:
            # The independent stage already froze the potentially long lineup
            # and player-load plan. The market stage revises numeric judgment,
            # not that plan; repeating it exhausted the real preseason response
            # budget and can truncate otherwise valid JSON.
            request_schema.get("properties", {}).pop("pregame_plan", None)
            request_schema["required"] = [key for key in request_schema.get("required", [])
                if key != "pregame_plan"]
            for name in ("PregamePlan", "LineupPlan", "PlayerUsagePlan"):
                request_schema.get("$defs", {}).pop(name, None)
        _compact_v2_request_schema(request_schema, schema, stage)
        protocol = (
            f"当前阶段：{stage}。只使用本次输入的事实；允许据此作有边界的篮球推断，"
            "不得补造已确认的伤病、名单、分钟限制、首发、模型或新闻。未确认状态保持未知，分析性首发与分钟估计放入 pregame_plan 并标明 basis。"
            "模型概率若未另作说明按主队概率理解；输出 win_probability 为你选中的赢家概率。"
            "expected_home_margin 始终为主队减客队，expected_total 包含加时。"
            "factors 是可审计的简短依据，引用提供的 evidence id，区分事实、模型输出和推断。"
            "判断主线字段须表达本场特有的核心问题、主要关系对位、预期比赛路径及可能翻转判断的条件；"
            "不要把独立判断与市场复核后的判断混成一份。只输出符合以下 schema 的 JSON，不写文章，不请求搜索：\n"
            + json.dumps(request_schema, ensure_ascii=False)
        )
        if stage == "independent":
            protocol += "\n当前未开放市场信息。依据 Pure 和篮球证据生成独立预测，跳过 Skill 中尚需市场资料的步骤。"
        else:
            protocol += (
                "\n独立预测已保存为审计记录，其数字是待校准估计，不预设保持。"
                "盘口本身提供新增的外部定价信息；调整不要求出现新的伤病、首发或新闻。"
                "对比独立判断与市场参考的依据质量，分别复核分差和总分。"
                "输入中的 deterministic_spread_comparison 是程序按冻结盘口和主队视角分差计算的权威数学结果；"
                "不得重算或改写其中的 spread_edge / ats_lean，也不要把 winner relationship 当成 ATS relationship。"
                "你的职责是解释分歧并决定是否修正最终篮球预测；最终 ATS 将在输出后由程序按同一盘口重新计算。"
                "自身模型或阵容依据不足时，要认真考虑市场校准；有更具体可靠的篮球依据时也可保留分歧。"
                "复核后可保持或修改，接近或远离市场均允许。"
                "不要设置固定权重或为了制造差异而调整。Residual/Official 可能已含市场，不能重复计权。"
                "报价无准确时间时降低对时点的把握，仍可有边界地用于校准；不认定已包含或未包含某条消息。"
                "keep 和 revise 都必须引用篮球及市场证据并解释两边依据的取舍。"
                "没有新增篮球事实或没有报价时间戳，均不能单独作为保持原预测的充分理由。"
                "keep 的数字须与独立预测一致；revise 说明改动依据，不要求固定调整幅度。"
                "revision_reason 必须把market_signal_factor_indices与basketball_reweighting_factor_indices分开引用；"
                "盘口只有 observed_at/captured_at 而无可验证发布时间时，不能断言市场掌握、已纳入或未纳入与我们相同的最新信息。"
                "若 deterministic_spread_comparison.tie_break_required 为 true，必须在当前 schema 允许的分差精度上给出有篮球依据的非零 spread_edge；"
                "不能保留精确重合，也不能由程序或你用固定 ±0.1 机械避开。"
            )
        if ats_tiebreak:
            protocol += (
                "\n这是唯一一次 deterministic ATS tie-break repair。上一版 final margin 正好等于 market_implied_home_margin，"
                "因此必须根据已给篮球分析和证据明确选择合理分差在线的上方或下方。"
                "不要由程序或你机械地加减固定小数，不增加精度，不得再输出与盘口相同的分差。"
                "只允许修改 expected_home_margin；若修改跨过零点，只能相应转换 winner_side，并保持主队胜率不变。"
                "expected_total、因素、证据、uncertainties、analysis_core 和 pregame_plan 必须原样保留；"
                "按实际方向更新 decision / revision_reason，并在 rationale 解释基于现有篮球分析为何选择该侧。"
            )
        if (basketball.get("game") or {}).get("game_type") == "preseason":
            if require_analysis_core and stage in {"market_review", "market_review_tiebreak"}:
                protocol += "\n独立阶段的 pregame_plan 已冻结并会被原样复用；本阶段不输出、不改写首发或分钟计划。"
            else:
                protocol += ("\n本場为 preseason，必须填写 pregame_plan 的双方 lineup 和关键球员负荷。"
                    "confirmed/source_expected/agent_expected 分别表示确认来源、来源预测和你的预计；"
                    "player_name 使用当前名单或合格当前名单/首发来源中的完整名字，历史球员知识不能证明当前归属。"
                    "首发没有确认时允许 agent_expected；名单身份无法建立时用 unavailable，不补造人名。"
                    "minutes 的 agent_estimate 是分析范围；source_confirmed 必须有本场 minutes_restriction 来源。")
        if schema in {VersionedIndependentForecast, VersionedMarketReviewForecast}:
            protocol += (
                "\nV2 输出压缩要求（不可删去 schema 字段）：central_question 一句，central_thesis 一到两句；"
                "key_matchups 只列真正有区分度的零到三条；expected_game_script 一个短段落；"
                "flip_conditions 只列确实可能改变判断的条件。factors 应彼此独立且简短，不重述 analysis_core；"
                "uncertainties 不重复 flip_conditions 或 pregame_plan。列表按实际信息量填写，不为凑数而添加。"
            )
            protocol += (
                "\n结构硬约束：analysis_core.central_thesis 必须是 ForecastClaim 对象，不能是字符串；"
                "自然语言判断写在其 statement 子字段。key_matchups 和 flip_conditions 必须是对象数组；"
                "expected_game_script 是字符串。以下仅展示结构，不是篮球内容范文："
                '{"central_thesis":{"statement":"...","classification":"<source_fact|inference>",'
                '"evidence_ids":["<本次输入中支持该判断的 ID>"],"confidence":"<low|medium|high>"},'
                '"key_matchups":[{"subject":"...","opponent_or_scheme":"...",'
                '"why_it_matters":"...","expected_effect":"...","classification":"<source_fact|inference>",'
                '"evidence_ids":["<valid-id>"],"confidence":"<low|medium|high>"}],'
                '"expected_game_script":"...","flip_conditions":[{"condition":"...",'
                '"judgment_effect":"...","evidence_ids":["<valid-id>"]}]}。'
                "尖括号内容是占位说明，必须用真实 enum 值及本次输入中实际存在的 evidence ID 替换，不可原样输出。"
            )
            if stage == "independent":
                protocol += (
                    "\npregame_plan 只承担操作性阵容信息：首发依据、重要球员负荷和会影响判断的 lineup assumption。"
                    "player_usage 只列负荷会实质影响判断的球员；key_matchup 是短标签；uncertainty 只列计划层尚未确认项。"
                    "不得把 pregame_plan 写成第二份对位/比赛脚本散文，也不要复述 thesis、factors 或 analysis_core。"
                )
            else:
                protocol += (
                    "\nMarket Review 不要扩写既有 analysis_core，也不要在 rationale 重复它；"
                    "只有当市场复核实质改变篮球判断时，才调整相关 analysis_core 字段。"
                )
        messages = messages_override or [
            {"role": "system", "content": skill + "\n\n" + protocol},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, allow_nan=False)},
        ]
        label = stage if correction is None else stage + "_protocol_revision"
        if correction is not None and correction_index>1:label+='_'+str(correction_index)
        if correction is not None:
            messages.append({"role":"user", "content":json.dumps({
                "protocol_correction":correction, "allowed_evidence_ids":sorted(_ids(basketball)),
                "instruction":"修正本次引用/首发basis协议错误，逐字复制允许的 evidence ID。source_expected/confirmed 需要对应首发来源覆盖列出的球员；只有部分来自报道、其余由你推断的阵容，应标为 agent_expected，不能声称来源确认。不能修改 prediction 数字，不得新增事实。返回完整 schema JSON。"
            },ensure_ascii=False)})
        save(folder / f"{label}_request.json", {"created_at": now(), "messages": messages,
            "schema": request_schema, "validation_schema": schema.model_json_schema(), "input_hash": content_hash(messages),
            "skill_hashes": prompt_skill_hashes(skill) if messages_override is None else {}, "model": "deepseek-v4-flash",
            "transport": transport_label or ("injected" if client is not default_client else "configured_deepseek"),
            "default_request_settings": {"temperature": 0.1, "thinking": "disabled",
                                         "max_tokens": _forecast_max_tokens(schema, (basketball.get("game") or {}).get("game_type")),
                                         "timeout_seconds": 60 if (basketball.get("game") or {}).get("game_type") == "preseason" else 40},
            "schema_repair_call": messages_override is not None,
            "automatic_retries": False})
        calls += 1
        payload, usage = client(messages, schema)
        save(folder / f"{label}_response.json", {"received_at": now(), "raw": payload, "usage": usage})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError("forecast_stage_json_invalid") from exc
        try:
            parsed = schema.model_validate(payload)
        except ValidationError as exc:
            errors = [{"loc": list(error.get("loc", ())), "type": error.get("type"),
                       "msg": error.get("msg")} for error in exc.errors(include_url=False)]
            save(folder / f"{label}_validation_error.json", {
                "error_type": type(exc).__name__, "errors": errors,
                "message": str(exc).splitlines()[0][:400],
            })
            raise
        save(folder / f"{label}_validation.json", {"schema_validated": True,
            "accepted_long_rationale": schema in {MarketReviewForecast, VersionedMarketReviewForecast}
                and bool(getattr(parsed, "rationale", None)) and len(parsed.rationale) > 1000,
            "schema_repair_used": stage == "independent_schema_repair",
            "response_content_edited": False})
        return parsed

    schema_repair_used = False
    schema_repair_provenance = None
    try:
        independent = call("independent", basketball, independent_schema)
    except ValidationError as original_error:
        if not require_analysis_core or independent_schema is not VersionedIndependentForecast:
            raise
        response_path = folder / "independent_response.json"
        response_record = json.loads(response_path.read_text(encoding="utf-8"))
        original_response = response_record.get("raw")
        if isinstance(original_response, str):
            # call() already parsed JSON before Pydantic validation; parse again
            # only to supply the original structured object to the repair call.
            original_response = json.loads(original_response)
        error_record = json.loads((folder / "independent_validation_error.json").read_text(encoding="utf-8"))
        precondition_error = None
        locked_prediction = None
        if not isinstance(original_response, dict):
            precondition_error = "original_response_not_object"
        else:
            try:
                locked_prediction = ForecastNumbers.model_validate(
                    original_response.get("prediction")).model_dump()
            except ValidationError:
                precondition_error = "original_numeric_prediction_not_lockable"
            core = original_response.get("analysis_core")
            thesis = core.get("central_thesis") if isinstance(core, dict) else None
            if precondition_error is None and not (
                isinstance(thesis, str) and thesis.strip()
                or isinstance(thesis, dict) and isinstance(thesis.get("statement"), str)
                and thesis["statement"].strip()
            ):
                precondition_error = "original_thesis_content_not_lockable"
            if precondition_error is None and any(key not in core for key in (
                "central_question", "matchup_status", "expected_game_script"
            )):
                precondition_error = "original_analysis_core_content_not_lockable"
            if precondition_error is None and not isinstance(original_response.get("factors"), list):
                precondition_error = "original_factors_not_lockable"

        if precondition_error is not None:
            schema_repair_provenance = {
                "schema_repair_used": False, "attempt_count": 0, "status": "not_repairable",
                "reason": precondition_error, "original_response_path": "independent_response.json",
                "original_validation_error_path": "independent_validation_error.json",
            }
            save(folder / "schema_repair_provenance.json", schema_repair_provenance)
            raise ValueError("forecast_schema_repair_not_repairable:" + precondition_error) from original_error

        request_schema = independent_schema.model_json_schema()
        _compact_v2_request_schema(request_schema, independent_schema, "independent_schema_repair")
        repair_payload = {
            "repair_mode": "schema_only",
            "basketball": basketball,
            "original_response": original_response,
            "original_validation_errors": error_record.get("errors", []),
            "locked_prediction": locked_prediction,
            "allowed_evidence_ids": sorted(_ids(basketball)),
            "allowed_evidence": basketball.get("evidence", []),
        }
        repair_system = (
            "你是第25秒 V2 Forecasting 的 schema-only repairer，不是新的篮球分析师。"
            "只把 original_response 整理成给定 schema；不重新预测、不搜索、不添加事实。"
            "winner_side、win_probability、expected_home_margin、expected_total 必须与 locked_prediction 完全一致。"
            "已有 thesis、matchup、script、flip、factor、uncertainty 和 pregame_plan 的文字/数值必须原样保留；"
            "只允许补齐 schema 必需但原响应缺少的结构元数据。"
            "analysis_core.central_thesis 必须是对象，句子原样放在 statement，不能返回字符串。"
            "classification、confidence 必须按原判断和现有证据选择；evidence_ids 只能逐字选自 allowed_evidence_ids，"
            "且只能选择实际支持该 statement 的 evidence。不得编造、猜默认值、填 fake-id 或空引用。"
            "若无法在不改写原判断、不改变数字且不伪造引用的前提下符合 schema，就保留冲突让校验失败。"
            "结构示例（仅类型占位，不是篮球范文）："
            '{"central_thesis":{"statement":"<原句>","classification":"<source_fact|inference>",'
            '"evidence_ids":["<allowed evidence id>"],"confidence":"<low|medium|high>"}}。'
            "尖括号内容不得原样输出。只返回完整 JSON，不加 Markdown。\nSchema:\n"
            + json.dumps(request_schema, ensure_ascii=False)
        )
        repair_messages = [
            {"role": "system", "content": repair_system},
            {"role": "user", "content": json.dumps(repair_payload, ensure_ascii=False, allow_nan=False)},
        ]
        schema_repair_used = True
        try:
            independent = call("independent_schema_repair", repair_payload, independent_schema,
                               messages_override=repair_messages)
            _validate_schema_repair_preserves(original_response, independent)
            schema_repair_provenance = {
                "schema_repair_used": True, "attempt_count": 1, "status": "schema_validated",
                "original_response_path": "independent_response.json",
                "original_response_sha256": content_hash(original_response),
                "original_validation_error_path": "independent_validation_error.json",
                "repair_request_path": "independent_schema_repair_request.json",
                "repair_response_path": "independent_schema_repair_response.json",
                "repaired_validation_result_path": "independent_schema_repair_acceptance.json",
                "locked_prediction": locked_prediction,
            }
        except Exception as repair_error:
            save(folder / "independent_schema_repair_acceptance.json", {
                "schema_validated": (folder / "independent_schema_repair_validation.json").is_file(),
                "content_lock_validated": False, "evidence_validated": False,
                "status": "failed", "error_type": type(repair_error).__name__,
                "error": str(repair_error)[:500],
            })
            schema_repair_provenance = {
                "schema_repair_used": True, "attempt_count": 1, "status": "failed",
                "original_response_path": "independent_response.json",
                "original_validation_error_path": "independent_validation_error.json",
                "repair_request_path": "independent_schema_repair_request.json",
                "repair_response_path": "independent_schema_repair_response.json",
                "repair_error_type": type(repair_error).__name__,
                "repair_error": str(repair_error)[:500],
            }
            save(folder / "schema_repair_provenance.json", schema_repair_provenance)
            raise ValueError("forecast_schema_repair_failed") from repair_error
    except ValueError as original_error:
        # Syntax-invalid/truncated JSON remains fail-closed and never enters schema repair.
        if require_analysis_core and str(original_error) == "forecast_stage_json_invalid":
            schema_repair_provenance = {
                "schema_repair_used": False, "attempt_count": 0, "status": "not_attempted_invalid_json",
                "original_response_path": "independent_response.json",
            }
            save(folder / "schema_repair_provenance.json", schema_repair_provenance)
        raise

    def validate_independent(candidate):
        _validate_evidence(candidate, _ids(basketball))
        validate_independent_market_boundary(candidate)
        validate_pregame_plan(candidate.pregame_plan, basketball)

    original_prediction = independent.prediction.model_dump()
    repaired=set()
    while True:
        try:
            validate_independent(independent)
            break
        except ValueError as exc:
            if schema_repair_used:
                save(folder / "independent_schema_repair_acceptance.json", {
                    "schema_validated": True, "content_lock_validated": True,
                    "evidence_validated": False, "status": "failed", "error": str(exc)[:500],
                })
                schema_repair_provenance.update({
                    "status": "failed", "independent_validation_error": str(exc)[:500],
                })
                save(folder / "schema_repair_provenance.json", schema_repair_provenance)
                raise ValueError("forecast_schema_repair_output_invalid") from exc
            category='references' if str(exc) in {'unknown_plan_evidence','unknown_forecast_evidence'} else 'lineup_basis' if str(exc).startswith('lineup_basis_source_unverified:') else None
            # At most once per protocol class. A reference repair must not
            # consume the separate basis repair; repeated failures stop.
            if category is None or category in repaired:raise
            repaired.add(category)
            independent = call('independent',basketball,independent_schema,correction_index=len(repaired),
                correction={'error':str(exc),'candidate':independent.model_dump()})
            if independent.prediction.model_dump()!=original_prediction:
                raise ValueError('protocol_revision_changed_prediction')
    if schema_repair_used:
        save(folder / "independent_schema_repair_acceptance.json", {
            "schema_validated": True, "content_lock_validated": True,
            "evidence_validated": True, "market_boundary_validated": True,
            "pregame_plan_validated": True, "status": "accepted",
        })
    if schema_repair_provenance is None and require_analysis_core:
        schema_repair_provenance = {
            "schema_repair_used": False, "attempt_count": 0, "status": "not_needed",
            "original_response_path": "independent_response.json",
        }
    elif schema_repair_used:
        schema_repair_provenance["status"] = "accepted"
        schema_repair_provenance["independent_validation_status"] = "passed"
    if not require_analysis_core or schema_repair_provenance is not None:
        if require_analysis_core:
            save(folder / "schema_repair_provenance.json", schema_repair_provenance)
        independent_value = independent.model_dump()
        independent_hash = content_hash(independent_value)
        save(folder / "independent.json", {"prediction_and_basis": independent_value,
            "sha256": independent_hash, "saved_at": now()})
        saved = json.loads((folder / "independent.json").read_text(encoding="utf-8"))
        if saved["sha256"] != independent_hash or content_hash(saved["prediction_and_basis"]) != independent_hash:
            raise ValueError("independent_receipt_integrity_failure")
        independent_value = saved["prediction_and_basis"]
        collection=None
        if market_collector is not None:
            try:
                collection=market_collector(copy.deepcopy(basketball['game']))
            except Exception as exc:
                collection={'status':'unavailable','reason':type(exc).__name__,'evidence':[],'sources':[],'finished_at':now()}
            save(folder/'market_collection.json',collection)
            if collection.get('observed_game_already_started'):
                from .prediction_versions import VersionStoreError
                raise VersionStoreError('pregame_generation_closed')
            if market_guard is not None:market_guard()
            if collection.get('evidence'):
                market={'evidence':collection['evidence']}
        spread_source = collection if collection is not None else market
        independent_spread_comparison = build_spread_comparison(
            basketball.get("game"), spread_source,
            independent_value["prediction"], independent_value["prediction"],
        )
        invalid_quote_reasons = {
            "spread_market_game_identity_mismatch",
            "spread_market_team_identity_mismatch",
            "spread_market_sign_conflict",
            "spread_market_paired_lines_conflict",
        }
        if (market.get("evidence") and
                independent_spread_comparison["market"].get("reason") in invalid_quote_reasons):
            save(folder / "market_review_failure.json", {
                "reason": independent_spread_comparison["market"]["reason"],
                "action": "skip_invalid_market_quote_and_retain_independent_forecast",
                "invalid_quote_used_for_review": False,
            })
            market = {"evidence": []}
        if market["evidence"]:
            try:
                timing_status = market_timing_status(collection)
                market_context = market
                if require_analysis_core:
                    market_context = {**market, "timing_status": timing_status,
                        "information_overlap": "unknown unless a separately verified source proves otherwise"}
                review = call("market_review", {"basketball": basketball,
                    "independent": independent_value, "independent_sha256": independent_hash,
                    "market_context": market_context,
                    "deterministic_spread_comparison": {
                        "market": independent_spread_comparison["market"],
                        "independent": independent_spread_comparison["independent"],
                        "tie_break_required": (
                            independent_spread_comparison["market"].get("spread_status") == "available"
                            and independent_spread_comparison["independent"].get("ats_lean") == "no_edge"
                        ),
                    }}, market_review_schema)
                _validate_evidence(review, _ids(basketball) | _ids(market))
                if review.pregame_plan is None:
                    review.pregame_plan = independent.pregame_plan
                validate_pregame_plan(review.pregame_plan, basketball)
                changed = review.prediction != independent.prediction
                if review.decision == "unavailable" or changed != (review.decision == "revise"):
                    raise ValueError("market_review_decision_conflicts_with_numbers")
                used = {ref for f in review.factors for ref in f.evidence_ids}
                if not used.intersection(_ids(basketball)) or not used.intersection(_ids(market)):
                    raise ValueError("review_requires_basketball_and_market_evidence")
                if require_analysis_core:
                    validate_market_revision_reason(review, _ids(basketball), _ids(market))
                    validate_market_timing_claims(review, timing_status)
            except Exception as exc:
                if market_collector is None:raise
                save(folder/'market_review_failure.json',{'reason':type(exc).__name__,'error':str(exc)[:200],
                    'action':'retain_validated_independent_forecast','invalid_candidate_published':False})
                fallback_schema = market_review_schema
                fallback = {**independent_value, "decision": "unavailable", "market_assessment": "unavailable",
                    "rationale": "市场复核未成功，保留已验证的独立预测。"}
                if require_analysis_core:
                    fallback["revision_reason"] = None
                review=fallback_schema.model_validate(fallback)
        else:
            fallback = {**independent_value, "decision": "unavailable", "market_assessment": "unavailable",
                "rationale": "未获得可用市场资料，保留独立预测。"}
            if require_analysis_core:
                fallback["revision_reason"] = None
            review = market_review_schema.model_validate(fallback)

        initial_tie_break_required = (
            independent_spread_comparison["market"].get("spread_status") == "available"
            and independent_spread_comparison["independent"].get("ats_lean") == "no_edge"
        )
        before_prediction = independent.prediction.model_dump()
        candidate_spread_comparison = build_spread_comparison(
            basketball.get("game"), spread_source, before_prediction, review.prediction.model_dump())
        final_tied_to_market = (
            candidate_spread_comparison["market"].get("spread_status") == "available"
            and candidate_spread_comparison["final"].get("ats_lean") == "no_edge"
        )
        tie_break_attempted = False
        tie_break_required = initial_tie_break_required or final_tied_to_market
        if final_tied_to_market:
            if review.decision == "unavailable":
                raise ValueError("market_review_tie_break_unavailable")
            previous_review = review
            tie_break_payload = {
                "basketball": basketball,
                "independent": independent_value,
                "independent_sha256": independent_hash,
                "market_context": market_context if market.get("evidence") else market,
                "deterministic_spread_comparison": {
                    "market": candidate_spread_comparison["market"],
                    "independent": candidate_spread_comparison["independent"],
                    "final_candidate": candidate_spread_comparison["final"],
                    "tie_break_required": True,
                },
                "tie_break_context": {
                    "reason": "final_model_home_margin_equals_market_implied_home_margin",
                    "previous_market_review": previous_review.model_dump(),
                    "market_implied_home_margin": candidate_spread_comparison["market"]["market_implied_home_margin"],
                    "final_candidate_model_home_margin": candidate_spread_comparison["final"]["model_home_margin"],
                    "allowed_changes": ["expected_home_margin", "winner_side_if_sign_changes",
                                        "win_probability_only_to_preserve_home_probability",
                                        "decision", "rationale", "revision_reason"],
                },
            }
            review = call("market_review_tiebreak", tie_break_payload, market_review_schema,
                          ats_tiebreak=True)
            _validate_evidence(review, _ids(basketball) | _ids(market))
            if review.pregame_plan is None:
                review.pregame_plan = independent.pregame_plan
            validate_pregame_plan(review.pregame_plan, basketball)
            validate_market_tiebreak_repair(previous_review, review, independent,
                candidate_spread_comparison["market"]["market_implied_home_margin"])
            if require_analysis_core:
                validate_market_revision_reason(review, _ids(basketball), _ids(market))
                validate_market_timing_claims(review, market_timing_status(collection))
            tie_break_attempted = True
            candidate_spread_comparison = build_spread_comparison(
                basketball.get("game"), spread_source, before_prediction, review.prediction.model_dump())
            if candidate_spread_comparison["final"].get("ats_lean") not in {"home", "away"}:
                raise ValueError("market_review_tiebreak_final_spread_edge_zero")

        final_value = review.model_dump()
        before, after = independent.prediction, review.prediction
        delta = build_revision_delta(before, after)
        spread_comparison = candidate_spread_comparison
        timing_status = market_timing_status(collection)
        market_provenance = {
            "decision": review.decision,
            "assessment": review.market_assessment,
            "overall_market_assessment": review.market_assessment,
            "assessment_scope": "overall_market_review_only_not_a_winner_or_spread_contract",
            "market_timing_status": timing_status,
            "information_overlap": "unknown",
            "revision_delta": delta,
            "winner_relationship": {
                "market_reference_basis": "spread_implied_favorite",
                "independent": spread_comparison["independent"]["winner_relationship"],
                "final": spread_comparison["final"]["winner_relationship"],
            },
            "final_spread_tiebreak": {
                "initially_required": initial_tie_break_required,
                "required": tie_break_required,
                "attempted": tie_break_attempted,
                "resolved": (spread_comparison["final"].get("ats_lean") in {"home", "away"}
                             if spread_comparison["market"].get("spread_status") == "available" else None),
                "attempt_count": 1 if tie_break_attempted else 0,
            },
        }
        if require_analysis_core and review.revision_reason is not None:
            market_provenance["market_signal_factors"] = [
                review.factors[index].model_dump()
                for index in review.revision_reason.market_signal_factor_indices
            ]
            market_provenance["basketball_reweighting_factors"] = [
                review.factors[index].model_dump()
                for index in review.revision_reason.basketball_reweighting_factor_indices
            ]
        result = {"schema_version": "two-stage-forecast-v1", "independent": independent_value,
            "independent_sha256": independent_hash, "review": final_value,
            "adjustment": {"home_margin": after.expected_home_margin - before.expected_home_margin,
                "total": after.expected_total - before.expected_total,
                "home_win_probability": (after.win_probability if after.winner_side == "home" else 1-after.win_probability)
                    - (before.win_probability if before.winner_side == "home" else 1-before.win_probability)},
            "calls": calls, "final_prediction": after.model_dump(), "audit_path": str(folder),
            "skill_hashes": prompt_skill_hashes(skill), "spread_comparison": spread_comparison}
        if require_analysis_core:
            result["schema_version"] = "two-stage-forecast-v2"
            result["final_analysis_core"] = review.analysis_core.model_dump()
            result["revision_delta"] = delta
            result["market_provenance"] = market_provenance
            result["schema_repair_provenance"] = schema_repair_provenance
        if collection is not None:result['market_collection']=collection
        save(folder / "final.json", result)
        return result
def run_two_stage(basketball: dict, market: dict, folder: Path, *, client=None,
                  skill_loader=None, removed_paths=(), transport_label=None, market_collector=None,
                  market_guard=None, require_analysis_core=False) -> dict:
    """Public wrapper preserves the existing failure receipt for every stage."""
    folder = Path(folder)
    try:
        return _run_two_stage_impl(
            basketball, market, folder, client=client, skill_loader=skill_loader,
            removed_paths=removed_paths, transport_label=transport_label,
            market_collector=market_collector, market_guard=market_guard,
            require_analysis_core=require_analysis_core,
        )
    except Exception as exc:
        if folder.is_dir() and not (folder / "failure.json").exists():
            save(folder / "failure.json", {
                "failed_at": now(), "error_type": type(exc).__name__,
                "calls": len(list(folder.glob("*_request.json"))),
                "final_prediction_published": False,
            })
        raise
