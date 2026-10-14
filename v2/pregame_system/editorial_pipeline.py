"""Compact natural writing with deterministic hard-fact guards and optional quality feedback."""
from __future__ import annotations

import copy
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from .editorial_contract import (
    build_editorial_projection, model_view, visible_units, validate_publication_hard_facts,
    make_publication_receipt, verify_hard_fact_candidates, public_visibility_issues,
)
from .editorial_skill import load_editorial_skill
from .skill_provenance import prompt_skill_hashes
from .storage import save, now, clean, content_hash


# Retry 3 needed 13 unique visible units. 32 leaves finite headroom for the
# normal sentence-level article plus populated watch/player/structured fields;
# it is a capacity bound, not permission to add targets. Larger repairs fail closed.
MAX_EDITORIAL_REPAIR_PATCHES = 32


class Theme(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=80)
    statement: str = Field(min_length=1, max_length=260)


class KeyPlayerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    player_name: str = Field(min_length=1, max_length=100)
    statement: str = Field(min_length=1, max_length=260)
    fact_ids: list[str] = Field(min_length=1, max_length=6)


class EditorialDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=80)
    lead: str = Field(min_length=1, max_length=300)
    article: str = Field(min_length=1, max_length=2200)
    watch_themes: list[Theme] = Field(min_length=0, max_length=4)
    confidence: Literal["low", "medium", "high"]
    player_analysis: list[KeyPlayerDraft] = Field(default_factory=list, max_length=10,
        description='Ordered candidates for both teams; 4–5 per team when facts support them. Public display selects at most three eligible players per team.')


class HardFactCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote: str = Field(min_length=1, max_length=500)
    code: Literal["exact_stat_value_mismatch", "exact_stat_sample_mismatch", "exact_stat_season_mismatch",
                  "specific_venue_mismatch", "specific_city_mismatch", "confirmed_status_starter_conflict",
                  "confirmed_starter_source_conflict", "transaction_claim_unverified", "locked_forecast_text_mismatch",
                  "unsupported_ml_attribution", "historical_score_mismatch", "exact_record_mismatch",
                  "current_roster_claim_unverified", "current_roster_team_mismatch",
                  "current_availability_unverified", "confirmed_availability_source_conflict",
                  "current_minutes_restriction_unverified", "confirmed_minutes_restriction_conflict",
                  "unverified_coach_quote",
                  "confirmed_starter_source_unverified", "unsupported_debut_claim",
                  "unsupported_season_opener_claim", "unverified_global_season_count",
                  "h2h_detail_count_mismatch", "game_date_timezone_mismatch",
                  "unsupported_model_training_sample", "roster_continuity_unverified",
                  "exact_rest_days_unverified", "exact_stat_value_unverified"]
    fact_ids: list[str] = Field(default_factory=list, max_length=8)


class QualityReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["passed", "warning", "hold"]
    warnings: list[str] = Field(default_factory=list, max_length=8)
    possible_hard_fact_conflicts: list[HardFactCandidate] = Field(default_factory=list, max_length=8)


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_id: str
    replacement: str = Field(min_length=1, max_length=1600)


class EditorialRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patches: list[Patch] = Field(min_length=1, max_length=MAX_EDITORIAL_REPAIR_PATCHES)


REVIEW_PROTOCOL = """你是第25秒的可选文章质量读者，只给内容反馈，不认证全文事实。
指出主线、鲜明判断、最值得解释的对位、统计是否服务观点，以及堆料或重复套话。
允许作者作篮球判断、主观排名、磨合推演和有条件的分钟预测；不因缺少逐句引用而拒绝文章。
若遇到清楚可核验的硬事实冲突，提交原文短句、冲突类型及相关fact_id作为候选；程序会另行验证。候选本身不能阻止发布。
不要用自身NBA知识补事实，不搜索，不改预测数字。只返回少量warning和possible_hard_fact_conflicts。"""


def configured_client(messages, schema, *, max_tokens=4000, model="deepseek-v4-flash"):
    from v2.agent.generate_historical_pregame_analysis import request_deepseek
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise ValueError("editorial_api_key_missing")
    opener = None
    if model == "deepseek-v4-pro":
        # Local reviewer-only adapter. The shared Forecasting transport and its
        # non-thinking settings remain byte-for-byte unchanged.
        import urllib.request
        def opener(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = "low"
            revised = urllib.request.Request(request.full_url, data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),
                headers=dict(request.header_items()), method=request.get_method())
            return urllib.request.urlopen(revised, timeout=timeout)
    response = request_deepseek(messages, key, model, max_tokens, timeout_seconds=90, opener=opener)
    return response.content, {"usage": response.usage, "finish_reason": response.finish_reason,
                              "provider": getattr(response, "response_metadata", {})}


def call_recorded(folder, stage, messages, schema, client=None, *, max_tokens=4000, model="deepseek-v4-flash"):
    """Receipt before parse, no transport retry; schema is included in prompt."""
    messages = copy.deepcopy(messages)
    request_schema = schema.model_json_schema()
    if schema is EditorialDraft:
        request_schema["required"] = list(dict.fromkeys(request_schema.get("required", []) + ["player_analysis"]))
    messages[0]["content"] += "\n只返回以下schema的JSON，不输出代码围栏：\n" + json.dumps(request_schema, ensure_ascii=False)
    input_hash = content_hash(messages)
    save(folder / f"{stage}_request.json", {"created_at": now(), "messages": messages,
        "schema": request_schema, "validation_schema": schema.model_json_schema(), "input_hash": input_hash,
        "input_characters": sum(len(m["content"]) for m in messages), "model": model,
        "mode": "injected" if client else "configured_deepseek", "automatic_retries": 0,
        "thinking": "enabled" if model == "deepseek-v4-pro" else "disabled",
        "reasoning_effort": "low" if model == "deepseek-v4-pro" else None,
        "max_tokens": max_tokens, "skill_hashes": prompt_skill_hashes(messages[0]["content"])})
    start = time.perf_counter()
    try:
        from .cost_ledger import component
        with component(stage if stage in {'Change Assessment','Market Review'} else 'Editorial' if stage=='writer0' else 'Repair' if 'repair' in stage else 'Editorial Quality Review',repair='repair' in stage):
            raw, usage = client(messages, schema) if client else configured_client(messages, schema, max_tokens=max_tokens, model=model)
    except Exception as exc:
        save(folder / f"{stage}_transport_failure.json", {"at": now(), "error_type": type(exc).__name__,
             "latency_seconds": time.perf_counter()-start, "automatic_retries": 0,
             "model":model, "thinking":"enabled" if model == "deepseek-v4-pro" else "disabled",
             "reasoning_effort":"low" if model == "deepseek-v4-pro" else None,
             "usage":None, "usage_status":"not_returned_to_editorial_transport"})
        raise
    metadata = {"received_at": now(), "raw": clean(raw), "usage": clean(usage),
                "input_sha256": input_hash,
                "latency_seconds": time.perf_counter()-start, "model": model,
                "mode": "injected" if client else "configured_deepseek"}
    save(folder / f"{stage}_response.json", metadata)
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return schema.model_validate(parsed).model_dump(), metadata


def review_quality(projection, text_payload, folder, stage="quality_review", client=None):
    return call_recorded(folder, stage, [{"role": "system", "content": REVIEW_PROTOCOL},
        {"role": "user", "content": json.dumps({"projection": model_view(projection), "text": text_payload}, ensure_ascii=False)}],
        QualityReview, client, max_tokens=3000, model="deepseek-v4-flash")


def draft_judgment(draft, context, numbers, projection=None):
    c = context.model_dump() if hasattr(context, "model_dump") else context
    allowed = {e["id"] for e in c.get("objective_evidence", []) if e.get("id")}
    prediction_refs = [e["id"] for e in c.get("objective_evidence", [])
                       if e.get("id") and e.get("section") == "prediction"]
    def judgment_refs(refs):
        result = []
        for ref in refs or []:
            mapped = prediction_refs[0] if ref == "model.pure" and prediction_refs else ref
            if mapped in allowed and mapped not in result:
                result.append(mapped)
        return result
    evidence = [i for i in numbers.get("evidence_ids", []) if i in allowed]
    if not evidence:
        evidence = [e["id"] for e in c.get("objective_evidence", []) if e.get("section") == "prediction"]
    final = {**numbers, "evidence_ids": evidence[:12]}
    players = []
    facts = {fact["id"]: fact for fact in (projection or {}).get("facts", [])}
    def current_name(fact, lookup):
        value = fact.get("value") or {}
        side = value.get("side")
        if (fact.get("kind") != "CURRENT" or side not in ("home", "away") or
                value.get("team_id") != ((c.get("game") or {}).get(side) or {}).get("id")):
            return None
        return next((str(p.get("name") or p.get("player_name") or "").strip()
                     for p in value.get("roster_snapshot") or []
                     if str(p.get("name") or p.get("player_name") or "").strip().casefold() == lookup.casefold()), None)
    used_names = set()
    for player in draft.get("player_analysis") or []:
        name = player["player_name"].strip()
        if name.casefold() in used_names or not set(player["fact_ids"]) <= set(facts):
            raise ValueError("key_player_duplicate_or_unknown_fact")
        used_names.add(name.casefold())
        referenced = [facts[ref] for ref in player["fact_ids"]]
        canonical = next((matched for fact in referenced if (matched := current_name(fact, name))), None)
        if canonical is None:
            raise ValueError("key_player_current_identity_not_supported")
        name = canonical
        refs = sorted({ref for fact in referenced for ref in fact.get("source_evidence_ids") or [] if ref in allowed})
        players.append({"player_name": name, "statement": player["statement"],
                        "classification": "subjective_analysis", "evidence_ids": refs[:6]})
    core_fact = next((item for item in (projection or {}).get("facts", [])
                      if item.get("kind") == "DECISION_CORE"), None)
    core = (core_fact or {}).get("value") or {}
    structured_core = {}
    if core:
        thesis = core.get("central_thesis") or {}
        thesis_refs = judgment_refs(thesis.get("evidence_ids"))
        if thesis and not thesis_refs:
            raise ValueError("forecast_thesis_evidence_not_in_analyst_context")
        structured_core["central_thesis"] = ({
            "statement": thesis.get("statement"),
            "classification": {"source_fact": "source_fact", "model_output": "data_inference",
                                "inference": "subjective_analysis"}.get(thesis.get("classification"), "subjective_analysis"),
            "evidence_ids": thesis_refs,
        } if thesis else None)
        structured_core["core_question"] = core.get("central_question")
        structured_core["game_script"] = core.get("expected_game_script")
        risks = core.get("flip_conditions") or []
        if risks:
            structured_core["model_risk"] = "；".join(
                f"如果{item.get('condition')}，则{item.get('judgment_effect')}" for item in risks[:2]
            )[:500]
        matchups = []
        for item in (core.get("key_matchups") or [])[:3]:
            refs = judgment_refs(item.get("evidence_ids"))
            if not refs:
                raise ValueError("forecast_matchup_evidence_not_in_analyst_context")
            statement = (f"{item.get('subject')} vs {item.get('opponent_or_scheme')}："
                         f"{item.get('why_it_matters')} {item.get('expected_effect')}")
            matchups.append({
                "statement": statement[:900],
                "classification": {"source_fact": "source_fact", "model_output": "data_inference",
                                    "inference": "subjective_analysis"}.get(item.get("classification"), "subjective_analysis"),
                "evidence_ids": refs[:6],
            })
        structured_core["key_matchup_variables"] = matchups
    pure = c.get("pure_prediction") or {}
    probability = pure.get("win_probability")
    pure_side = "home" if isinstance(probability, (int, float)) and probability >= .5 else "away"
    result = {"winner_lean": numbers["winner_side"], "side_lean": "no_lean", "total_lean": "no_lean",
        "confidence": draft["confidence"], "supporting_evidence_ids": evidence[:12], "conflicting_evidence_ids": [],
        "baseline_agreement": {"pure": "agree" if pure_side == numbers["winner_side"] else "disagree",
                               "market": "mixed" if c.get("market_benchmark") else "not_applicable",
                               "residual": "mixed" if c.get("residual_prediction") else "not_applicable"},
        "subjective_rationale": draft["lead"],
        "pregame_story": {"title": draft["title"], "statement": draft["lead"], "classification": "subjective_analysis", "evidence_ids": evidence[:6]},
        "watch_themes": [{**t, "classification": "subjective_analysis", "evidence_ids": evidence[:6]} for t in draft["watch_themes"]],
        "analysis_article": draft["article"], "analysis_article_evidence_ids": evidence[:12],
        "player_analysis": players,
        "final_prediction": final, "classification": "subjective_analyst_judgment"}
    result.update({key: value for key, value in structured_core.items() if value is not None})
    return result


def normalize(draft):
    d = copy.deepcopy(draft)
    for key in ("title", "lead", "article"):
        d[key] = d[key].replace("\\n", "\n").strip()
    return EditorialDraft.model_validate(d).model_dump()


_JUDGMENT_TEXT_SCALARS = {"core_question", "game_script", "model_risk"}
_JUDGMENT_TEXT_CLAIMS = {
    "roster_analysis", "cold_start_analysis", "historical_data_relevance",
    "key_matchup_variables", "supporting_evidence", "conflicting_evidence", "uncertainty",
}


def _is_repairable_judgment_text(location):
    parts = location.split(".")
    return ((len(parts) == 1 and parts[0] in _JUDGMENT_TEXT_SCALARS)
            or parts in (["central_thesis", "statement"], ["prediction_perspective", "statement"])
            or (len(parts) == 3 and parts[0] in _JUDGMENT_TEXT_CLAIMS
                and parts[1].isdigit() and parts[2] in {"title", "statement"}))


def _set_judgment_text(judgment, location, replacement):
    """Set one allowlisted public narrative field, never numeric prediction data."""
    parts = location.split(".")
    if len(parts) == 1 and parts[0] in _JUDGMENT_TEXT_SCALARS:
        judgment[parts[0]] = replacement
        return
    if parts == ["central_thesis", "statement"]:
        judgment["central_thesis"]["statement"] = replacement
        return
    if parts == ["prediction_perspective", "statement"]:
        judgment["prediction_perspective"]["statement"] = replacement
        return
    if (len(parts) == 3 and parts[0] in _JUDGMENT_TEXT_CLAIMS
            and parts[1].isdigit() and parts[2] in {"title", "statement"}):
        judgment[parts[0]][int(parts[1])][parts[2]] = replacement
        return
    raise ValueError("editorial_repair_target_not_publicly_editable")


def _normalized_repair_text(text):
    normalized = unicodedata.normalize("NFKC", str(text).replace("\\n", "\n"))
    return re.sub(r"\s+", " ", normalized).strip()


def _repair_numeric_claims(text, projection):
    from .editorial_hard_facts import _NUMBER_RE, _number_value, _team_name_spans, classify_numeric_claim_type
    game = projection.get("game") or {}
    spans = [(start, end) for start, end, _side in _team_name_spans(text, game)]
    spans.extend((match.start(), match.end()) for match in re.finditer(r"第25秒|三分命中率|三分命中|三分球", text))
    rows = []
    for match in _NUMBER_RE.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in spans):
            continue
        raw = match.group().lstrip("+-")
        value = _number_value(raw)
        if value is None:
            continue
        claim_type = classify_numeric_claim_type(text, match.start(), match.end(), None, projection)
        if claim_type in {"team_identity", "structural_or_count_expression"}:
            continue
        tail = text[match.end():match.end()+12]
        prefix = text[max(0, match.start()-18):match.start()]
        numeric_unit = bool(re.match(
            r"\s*(?:[%％分场分钟篮板助攻回合胜负名次排名个次场均净效率命中率]|games?|minutes?)",
            tail, re.I))
        range_context = bool(re.match(r"\s*(?:[-–—~～到至])", tail)
                             or re.search(r"(?:[-–—~～到至])\s*$", prefix))
        if "." not in raw and not numeric_unit and not range_context:
            continue
        rows.append((float(value), raw))
    return rows


def _projection_numeric_values(projection):
    from .editorial_hard_facts import _NUMBER_RE, _number_value
    values = set()

    def visit(value):
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            numeric = float(value)
            values.add(numeric)
            if 0 <= numeric <= 1:
                values.add(numeric * 100)
            return
        if isinstance(value, str):
            for match in _NUMBER_RE.finditer(value):
                number = _number_value(match.group())
                if number is not None:
                    values.add(float(number))
                    if 0 <= number <= 1:
                        values.add(float(number) * 100)
            return
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(projection.get("locked_prediction") or {})
    for fact in projection.get("facts", []):
        if fact.get("visibility") != "audit_only":
            visit(fact.get("value") or {})
    return values


def _repairable_location(location):
    if location in {"subjective_rationale", "pregame_story.statement", "pregame_story.title"}:
        return True
    if re.fullmatch(r"article\.\d+", location):
        return True
    if re.fullmatch(r"watch_themes\.\d+\.(?:title|statement)", location):
        return True
    if re.fullmatch(r"player_analysis\.\d+\.statement", location):
        return True
    return _is_repairable_judgment_text(location)


def inspect_repair_patches(draft, units, patches, allowed_unit_ids, projection=None):
    """Validate the entire repair response before any text is changed."""
    if len(patches) > MAX_EDITORIAL_REPAIR_PATCHES:
        return {"status": "rejected", "patch_count": len(patches),
            "rejected_patches": [{"patch_index": index, "unit_id": patch.get("unit_id"),
                                  "reasons": ["capacity_exceeded"]}
                                 for index, patch in enumerate(patches)],
            "no_op_targets": [], "out_of_scope_targets": [], "duplicate_targets": []}
    by_id = {unit["id"]: unit for unit in units}
    seen = set()
    rejected = []
    allowed_numbers = _projection_numeric_values(projection or {}) if projection is not None else set()
    for index, patch in enumerate(patches):
        ident = patch["unit_id"]
        reasons = []
        if ident in seen:
            reasons.append("duplicate_target")
        seen.add(ident)
        if ident not in allowed_unit_ids or ident not in by_id:
            reasons.append("target_not_in_current_repair_allowlist")
        unit = by_id.get(ident)
        if unit is None:
            rejected.append({"patch_index": index, "unit_id": ident, "reasons": reasons})
            continue
        if not all(_repairable_location(location) for location in unit.get("aliases") or []):
            reasons.append("target_not_publicly_editable")
        if (_normalized_repair_text(patch["replacement"]) == _normalized_repair_text(unit["text"])
                or content_hash(_normalized_repair_text(patch["replacement"]))
                    == content_hash(_normalized_repair_text(unit["text"]))):
            reasons.append("no_op")
        if projection is not None:
            old_values = [value for value, _token in _repair_numeric_claims(unit["text"], projection)]
            new_values = _repair_numeric_claims(patch["replacement"], projection)
            remaining = list(old_values)
            introduced = []
            for value, token in new_values:
                match = next((i for i, old in enumerate(remaining) if abs(old-value) <= 0.000001), None)
                if match is not None:
                    remaining.pop(match)
                    continue
                places = len(token.partition(".")[2]) if "." in token else 0
                tolerance = max(0.005, 0.51 * (10 ** -places))
                if not any(abs(value-supported) <= tolerance for supported in allowed_numbers):
                    introduced.append(token)
            if introduced:
                reasons.append("unsupported_new_numeric_claim:" + ",".join(introduced))
        if reasons:
            rejected.append({"patch_index": index, "unit_id": ident, "reasons": reasons})
    return {"status": "rejected" if rejected else "accepted", "patch_count": len(patches),
            "rejected_patches": rejected,
            "no_op_targets": [row["unit_id"] for row in rejected if "no_op" in row["reasons"]],
            "out_of_scope_targets": [row["unit_id"] for row in rejected
                                     if "target_not_in_current_repair_allowlist" in row["reasons"]],
            "duplicate_targets": [row["unit_id"] for row in rejected if "duplicate_target" in row["reasons"]]}


def apply_patches(draft, units, patches, allowed_unit_ids, *, include_judgment_overrides=False, projection=None):
    """Apply scoped public-text repairs while preserving the frozen forecast.

    Forecasting-owned narrative such as game_script is copied into the public
    judgment, so an identified factual wording issue can be corrected there.
    No prediction-number path is allowed by this applicator.
    """
    if len(patches) > MAX_EDITORIAL_REPAIR_PATCHES:
        raise ValueError("editorial_repair_capacity_exceeded")
    validation = inspect_repair_patches(draft, units, patches, allowed_unit_ids, projection)
    if validation["rejected_patches"]:
        reasons = {reason for row in validation["rejected_patches"] for reason in row["reasons"]}
        if "no_op" in reasons:
            raise ValueError("editorial_repair_no_op")
        if any(reason.startswith("unsupported_new_numeric_claim") for reason in reasons):
            raise ValueError("editorial_repair_new_numeric_claim_unverified")
        if "duplicate_target" in reasons:
            raise ValueError("editorial_repair_duplicate_target")
        if "target_not_publicly_editable" in reasons:
            raise ValueError("editorial_repair_target_not_publicly_editable")
        raise ValueError("editorial_repair_scope_invalid")
    result = copy.deepcopy(draft)
    by_id = {u["id"]: u for u in units}
    used = set()
    article_edits = []
    judgment_overrides = []
    provenance = []
    for patch in patches:
        ident = patch["unit_id"]
        if ident in used:
            raise ValueError("editorial_repair_duplicate_target")
        if ident not in allowed_unit_ids or ident not in by_id:
            raise ValueError("editorial_repair_scope_invalid")
        used.add(ident)
        aliases = by_id[ident]["aliases"]
        patched_locations = []
        # Aliases share the same original string; update the canonical draft once.
        for location in aliases:
            if location == "subjective_rationale" or location == "pregame_story.statement":
                result["lead"] = patch["replacement"]
            elif location == "pregame_story.title":
                result["title"] = patch["replacement"]
            elif location.startswith("article."):
                article_edits.append({**by_id[ident]["article_span"], "replacement": patch["replacement"]})
            elif location.startswith("watch_themes."):
                _, index, key = location.split(".")
                if not index.isdigit() or key not in {"title", "statement"}:
                    raise ValueError("editorial_repair_target_not_publicly_editable")
                result["watch_themes"][int(index)][key] = patch["replacement"]
            elif location.startswith("player_analysis."):
                _, index, key = location.split(".")
                if not index.isdigit() or key != "statement":
                    raise ValueError("editorial_repair_target_not_publicly_editable")
                result["player_analysis"][int(index)][key] = patch["replacement"]
            else:
                # Validate the exact target path before retaining the override.
                if not _is_repairable_judgment_text(location):
                    raise ValueError("editorial_repair_target_not_publicly_editable")
                judgment_overrides.append((location, patch["replacement"]))
            patched_locations.append(location)
        provenance.append({"unit_id": ident, "locations": patched_locations,
                           "original_sha256": content_hash(by_id[ident]["text"]),
                           "replacement_sha256": content_hash(patch["replacement"])})
    for edit in sorted(article_edits,key=lambda e:e["start"],reverse=True):
        result["article"] = result["article"][:edit["start"]]+edit["replacement"]+result["article"][edit["end"]:]
    normalized = normalize(result)
    if include_judgment_overrides:
        return normalized, judgment_overrides, provenance
    if judgment_overrides:
        raise ValueError("editorial_repair_target_requires_judgment_projection")
    return normalized


def _analysis_material(locked_forecast, context):
    allowed = set(context.get("evidence_index") or {})
    result = []
    for factor in (locked_forecast.get("review") or {}).get("factors") or []:
        if factor.get("classification") != "inference" or not factor.get("summary"):
            continue
        result.append({"summary": factor["summary"], "effect": factor.get("effect"),
                       "evidence_ids": [ref for ref in factor.get("evidence_ids") or [] if ref in allowed]})
    plan = (locked_forecast.get("review") or {}).get("pregame_plan")
    if plan:
        refs = {ref for row in plan.get("lineups", []) + plan.get("player_usage", [])
                for ref in row.get("evidence_ids", []) if ref in allowed}
        result.append({"summary": "本场预计首发与核心球员负荷", "effect": "uncertainty",
                       "evidence_ids": sorted(refs), "pregame_plan": plan})
    if locked_forecast.get("schema_version") == "two-stage-forecast-v2":
        result.append({"_contract_item": "forecast_provenance", "value": {
            "analysis_core": locked_forecast.get("final_analysis_core"),
            "independent_prediction": (locked_forecast.get("independent") or {}).get("prediction"),
            "final_prediction": locked_forecast.get("final_prediction"),
            "revision_delta": locked_forecast.get("revision_delta"),
            "market_provenance": locked_forecast.get("market_provenance"),
            # This is the already-computed, version-bound ATS contract. The
            # projection reuses its final comparison; it must not recalculate spread direction.
            "spread_comparison": locked_forecast.get("spread_comparison"),
        }})
    return result


def _quality_text(judgment):
    fields = ("subjective_rationale", "analysis_article", "pregame_story", "watch_themes",
              "roster_analysis", "player_analysis", "cold_start_analysis", "historical_data_relevance",
              "central_thesis", "core_question", "game_script", "model_risk", "key_matchup_variables",
              "supporting_evidence", "conflicting_evidence", "uncertainty")
    return {key: judgment.get(key) for key in fields if judgment.get(key)}


def _quality_failure(exc):
    message = str(exc).replace(os.environ.get("DEEPSEEK_API_KEY", "\0"), "[REDACTED]")
    return {"status": "unavailable", "warnings": [message[:300]], "possible_hard_fact_conflicts": []}


_REPAIR_FINDING_GUIDANCE = {
    "exact_stat_value_mismatch": ("句中被识别为精确统计的数值与对应冻结事实不一致。",
        "仅可改用投影中同一指标、赛季、球队和样本支持的数值；否则删除精确数值或改为不含精确事实的判断。"),
    "exact_stat_value_unverified": ("当前冻结材料没有该精确统计的可核验值。",
        "删除该数字或将其改为不含精确数值的分析表达；不要从外部记忆补数。"),
    "exact_stat_sample_mismatch": ("句中统计所称样本窗口与冻结事实中的窗口不一致。",
        "只能使用材料明确支持的样本口径；否则删除样本数和对应精确统计。"),
    "exact_stat_season_unlabeled": ("该统计只在特定赛季/范围下有依据，但原句未标明适用赛季。",
        "补上投影明确提供的赛季标签，或删除精确统计。"),
    "transaction_claim_unverified": ("冻结的已采纳来源没有支持句中这笔具体交易关系。",
        "删除交易关系断言；不能从名单变化或球员身份推断交易细节。"),
    "current_availability_unverified": ("没有本场已确认的出战/缺阵依据；长期伤病背景不等于本场官方状态。",
        "删除确定的本场出战或长期缺阵断言。可以保留 BACKGROUND 明确支持的伤病事件本身，再作预计/条件判断；不能把受伤自动升级成已确认 OUT。"),
    "locked_forecast_text_mismatch": ("句中第25秒最终预测数字与锁定 Final 不一致。",
        "只将用户侧文字改成 projection.locked_prediction / PUBLIC_DECISION 的值；绝不修改预测对象。"),
}


def _repair_guidance(findings):
    grouped = {}
    for finding in findings:
        grouped.setdefault(finding["unit_id"], []).append(finding)
    result = []
    for unit_id, rows in grouped.items():
        codes = list(dict.fromkeys(row.get("code") for row in rows))
        result.append({"unit_id": unit_id,
            "why_flagged": list(dict.fromkeys(_REPAIR_FINDING_GUIDANCE.get(code,
                ("该 visible unit 触发确定性 hard-fact finding。", "仅按冻结投影修正，不增加新事实。"))[0]
                for code in codes)),
            "safe_action": list(dict.fromkeys(_REPAIR_FINDING_GUIDANCE.get(code,
                ("该 visible unit 触发确定性 hard-fact finding。", "仅按冻结投影修正，不增加新事实。"))[1]
                for code in codes)),
            "finding_codes": codes,
            "authoritative_fact_ids": sorted({fact_id for row in rows for fact_id in row.get("available_fact_ids", [])})})
    return result


def run_editorial(context, locked_forecast, folder, *, writer_client=None, reviewer_client=None,
                  quality_review_enabled=False, max_calls=4):
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=False)
    c = context.model_dump() if hasattr(context, "model_dump") else context
    numbers = locked_forecast["final_prediction"]
    locked = {"winner_side": numbers["winner_side"], "win_probability": numbers["win_probability"],
              "expected_margin": abs(numbers["expected_home_margin"]), "expected_total": numbers["expected_total"],
              "evidence_ids": sorted({i for f in locked_forecast["review"]["factors"] for i in f["evidence_ids"]
                    if i in c["evidence_index"]})[:12]}
    material = _analysis_material(locked_forecast, c)
    projection = build_editorial_projection(c, locked, material)
    save(folder / "projection.json", projection)
    calls, metadata, repair_rounds = 0, [], 0
    repair_provenance = []
    if max_calls < 1:
        raise ValueError("editorial_budget_cannot_cover_writer")
    skill = load_editorial_skill()
    draft, meta = call_recorded(folder, "writer0", [{"role": "system", "content": skill +
        "\n本次输入是冻结写作资料与已完成的第25秒篮球分析素材。用它形成作者判断；素材不是客观事实，也不提供ML特征归因。"
        "visibility=public 可按来源准确表达；visibility=writer_internal 只用于理解判断路径，不照抄内部层名称；audit_only 不会进入本次Writer视图。"
        "用户正文不得出现 Pure、Residual、ML 或其他内部模型层名称。不得搜索或改预测。"},
        {"role": "user", "content": json.dumps(model_view(projection), ensure_ascii=False)}],
        EditorialDraft, writer_client, max_tokens=3500)
    calls += 1; metadata.append({"stage": "writer0", **{k: v for k, v in meta.items() if k != "raw"}})
    draft = normalize(draft)
    decision_mode = projection.get("protocol") == "editorial-decision-v2"
    if decision_mode:
        visibility_issues = public_visibility_issues(draft)
        if visibility_issues:
            save(folder / "visibility_failure.json", {"status": "blocked",
                "issues": visibility_issues, "automatic_rewrite": False})
            raise ValueError("editorial_internal_model_layer_leak:" + ",".join(visibility_issues))

    def make_judgment(current, judgment_overrides=()):
        from .analyst import AnalystJudgment, validate_judgment
        value = draft_judgment(current, c, locked, projection)
        for location, replacement in judgment_overrides:
            _set_judgment_text(value, location, replacement)
        return validate_judgment(AnalystJudgment.model_validate(value), context).model_dump()

    judgment = make_judgment(draft)
    if decision_mode:
        visibility_issues = public_visibility_issues(judgment)
        if visibility_issues:
            save(folder / "visibility_failure.json", {"status": "blocked",
                "issues": visibility_issues, "automatic_rewrite": False})
            raise ValueError("editorial_internal_model_layer_leak:" + ",".join(visibility_issues))
    units = visible_units(judgment)
    hard_findings = validate_publication_hard_facts(judgment, projection)
    save(folder / "draft0.json", {"draft": draft, "judgment": judgment, "hard_fact_findings": hard_findings})
    if hard_findings:
        if calls >= max_calls:
            raise ValueError("publication_hard_fact_repair_budget_unavailable")
        affected = {finding["unit_id"] for finding in hard_findings}
        targets = [unit for unit in units if unit["id"] in affected]
        repair_request = {"projection": model_view(projection), "hard_fact_blockers": hard_findings,
            "affected_text": targets, "finding_explanations": _repair_guidance(hard_findings),
            "repair_policy": {
                "targets": "Only the listed affected visible units are editable; one final patch per unit.",
                "facts": "Use only the supplied projection, accepted evidence and locked PUBLIC_DECISION.",
                "numbers": "Do not introduce unsupported numeric claims. If no supported exact value exists, remove the number or generalize.",
                "prohibited": ["winner", "win_probability", "expected_margin", "expected_home_margin",
                               "expected_total", "model_home_margin", "ATS", "Forecasting source artifact"],
                "effectiveness": "Every replacement must materially differ from its original text and resolve the cited issue.",
            }}
        calls += 1; repair_rounds = 1
        try:
            patches, meta = call_recorded(folder, "repair1", [{"role": "system", "content": load_editorial_skill() +
                "\n只改正下列确定的硬事实冲突。保留篮球判断，不改数字预测、未指出文本和事实输入。"
                "每个 affected visible unit 最多返回一个 patch；同一 unit 有多个问题时合并修正。"
                "不得返回未列出的 unit，也不得修改任何预测数字。replacement 必须实质改变原文；"
                "只能使用本次 projection 和已采纳证据支持的数字/事实。若没有安全替代，删除精确断言或改为不含精确事实的分析表达。"},
                {"role": "user", "content": json.dumps(repair_request, ensure_ascii=False)}],
                EditorialRepair, writer_client, max_tokens=2500)
        except Exception as exc:
            response_path = folder / "repair1_response.json"
            response_record = {}
            raw_patches = []
            if response_path.is_file():
                try:
                    response_record = json.loads(response_path.read_text(encoding="utf-8"))
                    parsed = json.loads(response_record.get("raw") or "{}")
                    raw_patches = parsed.get("patches") if isinstance(parsed, dict) else []
                    raw_patches = raw_patches if isinstance(raw_patches, list) else []
                except (OSError, ValueError, json.JSONDecodeError):
                    raw_patches = []
            response_validation = (inspect_repair_patches(draft, units, raw_patches, affected, projection)
                if all(isinstance(patch, dict) and {"unit_id", "replacement"} <= set(patch) for patch in raw_patches)
                else {"status": "rejected", "patch_count": len(raw_patches),
                      "rejected_patches": [{"patch_index": index,
                          "unit_id": patch.get("unit_id") if isinstance(patch, dict) else None,
                          "reasons": ["repair_schema_validation_failed"]} for index, patch in enumerate(raw_patches)],
                      "no_op_targets": [], "out_of_scope_targets": [], "duplicate_targets": []})
            response_validation["status"] = "rejected"
            response_validation["schema_error"] = type(exc).__name__
            response_validation["original_findings"] = hard_findings
            response_validation["applied_patches"] = []
            if not response_validation.get("rejected_patches"):
                response_validation["rejected_patches"] = [{"patch_index": index,
                    "unit_id": patch.get("unit_id") if isinstance(patch, dict) else None,
                    "reasons": ["repair_schema_validation_failed"]} for index, patch in enumerate(raw_patches)]
            unchanged_findings = validate_publication_hard_facts(judgment, projection)
            metadata.append({"stage": "repair1", "status": "invalid_response",
                "error_type": type(exc).__name__,
                "rejected_patch_count": len(response_validation["rejected_patches"])})
            response_validation["post_repair_findings"] = unchanged_findings
            save(folder / "repair1_validation.json", response_validation)
            save(folder / "summary.json", {"status": "rejected", "calls": calls, "repair_rounds": 1,
                "hard_fact_findings": hard_findings, "post_repair_findings": unchanged_findings,
                "repair_provenance": [], "repair_validation": response_validation, "stages": metadata})
            raise ValueError("editorial_repair_response_invalid:" + type(exc).__name__) from exc
        metadata.append({"stage": "repair1", **{k: v for k, v in meta.items() if k != "raw"}})
        response_validation = inspect_repair_patches(
            draft, units, patches["patches"], affected, projection)
        if response_validation["status"] != "accepted":
            unchanged_findings = validate_publication_hard_facts(judgment, projection)
            metadata[-1]["status"] = "ineffective_or_out_of_scope"
            metadata[-1]["rejected_patches"] = response_validation["rejected_patches"]
            response_validation.update(reason="repair_response_ineffective_or_out_of_scope",
                original_findings=hard_findings, applied_patches=[],
                post_repair_findings=unchanged_findings)
            save(folder / "repair1_validation.json", response_validation)
            save(folder / "summary.json", {"status": "rejected", "calls": calls, "repair_rounds": 1,
                "hard_fact_findings": hard_findings, "post_repair_findings": unchanged_findings,
                "repair_provenance": [], "repair_validation": response_validation, "stages": metadata})
            reasons = sorted({reason for row in response_validation["rejected_patches"] for reason in row["reasons"]})
            raise ValueError("editorial_repair_ineffective:" + ",".join(reasons))
        draft, judgment_overrides, repair_provenance = apply_patches(
            draft, units, patches["patches"], affected, include_judgment_overrides=True, projection=projection)
        metadata[-1]["applied_patches"] = repair_provenance
        judgment = make_judgment(draft, judgment_overrides)
        units = visible_units(judgment)
        hard_findings = validate_publication_hard_facts(judgment, projection)
        save(folder / "draft1.json", {"draft": draft, "judgment": judgment, "hard_fact_findings": hard_findings})
        validation = {**response_validation, "status": "applied_and_rechecked",
            "original_findings": json.loads((folder / "draft0.json").read_text(encoding="utf-8"))["hard_fact_findings"],
            "applied_patches": repair_provenance, "post_repair_findings": hard_findings}
        save(folder / "repair1_validation.json", validation)
        if hard_findings:
            save(folder / "summary.json", {"status": "rejected", "calls": calls, "repair_rounds": 1,
                "hard_fact_findings": hard_findings, "post_repair_findings": hard_findings,
                "repair_provenance": repair_provenance, "repair_validation": validation, "stages": metadata})
            raise ValueError("publication_hard_fact_blocker:" + json.dumps(hard_findings, ensure_ascii=False))

    quality = {"status": "disabled", "warnings": [], "possible_hard_fact_conflicts": []}
    quality_review_input_sha256 = None
    review_requested = quality_review_enabled or reviewer_client is not None
    if review_requested and calls < max_calls:
        try:
            quality, meta = review_quality(projection, _quality_text(judgment), folder,
                client=reviewer_client)
            quality_review_input_sha256 = meta.get("input_sha256")
            quality["verified_hard_fact_conflicts"] = verify_hard_fact_candidates(quality, hard_findings)
            metadata.append({"stage": "quality_review", **{k: v for k, v in meta.items() if k != "raw"}})
            calls += 1
        except Exception as exc:
            quality = _quality_failure(exc)
            request_path = folder / "quality_review_request.json"
            if request_path.is_file():
                try:
                    quality_review_input_sha256 = json.loads(request_path.read_text(encoding="utf-8")).get("input_hash")
                except Exception:
                    quality_review_input_sha256 = None
            metadata.append({"stage": "quality_review", "status": "unavailable"})
            save(folder / "quality_review_failure.json", quality)
            # QA errors and hold states are information only; hard blockers
            # have already been independently checked against structured facts.
    elif review_requested:
        quality = {"status": "budget_unavailable",
                   "warnings": ["Optional quality feedback was skipped because the bounded call budget was used by writing or hard-fact repair."],
                   "possible_hard_fact_conflicts": []}

    receipt = make_publication_receipt(judgment, c, projection, material, hard_findings, quality,
                                       quality_review_input_sha256=quality_review_input_sha256)
    save(folder / "publication_receipt.json", receipt)
    summary = {"status": "eligible", "calls": calls, "repair_rounds": repair_rounds,
               "hard_fact_checks": "passed", "quality_review_status": quality["status"],
               "repair_provenance": repair_provenance, "stages": metadata}
    save(folder / "summary.json", summary)
    return judgment, {"publication_receipt": receipt, "editorial_projection": projection,
        "editorial_material": material, "quality_review": quality, "editorial_calls": calls,
        "quality_review_input_sha256": quality_review_input_sha256,
        "editorial_stages": metadata, "repair_rounds": repair_rounds,
        "repair_provenance": repair_provenance,
        "editorial_artifact": str(folder)}
