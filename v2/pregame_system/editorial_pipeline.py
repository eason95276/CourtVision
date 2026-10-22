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
    _writer_market_interpretation,
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


class PlayerReferencePatch(KeyPlayerDraft):
    index: int = Field(ge=0, le=9)


class PlayerReferenceRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patches: list[PlayerReferencePatch] = Field(min_length=1, max_length=10)


class EditorialDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=80,
        description='Two seven-character Chinese lines separated by a newline, without punctuation; a game-specific story, not a predicted result. Existing saved drafts remain readable.')
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
    response = request_deepseek(messages, key, model, max_tokens, timeout_seconds=90, opener=opener,
                               response_format={"type": "json_object"})
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
        "response_format": None if client else {"type": "json_object"},
        "thinking": "enabled" if model == "deepseek-v4-pro" else "disabled",
        "reasoning_effort": "low" if model == "deepseek-v4-pro" else None,
        "max_tokens": max_tokens, "skill_hashes": prompt_skill_hashes(messages[0]["content"])})
    start = time.perf_counter()
    try:
        from .cost_ledger import component
        with component(stage if stage in {'Change Assessment','Market Review'} else 'Editorial' if stage in {'writer0','title_revision'} else 'Repair' if 'repair' in stage else 'Editorial Quality Review',repair='repair' in stage):
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
                "evidence_ids": refs,
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


def writer_view(projection, locked_forecast, game):
    """Select prose material without altering the complete validation projection."""
    view = model_view(projection)
    facts = []
    omitted = []
    for fact in view['facts']:
        value = fact['value']
        if (fact['kind'] in {'MODEL_IDENTITY', 'MARKET'} or
                fact['kind'] == 'MODEL_OUTPUT' and value.get('owner') != 'locked_final'):
            omitted.append(fact['id'])
            continue
        if fact['kind'] == 'HISTORICAL':
            # Keep the series result/story; the match table belongs to the page.
            if 'meetings' in value:
                series = value.get('series_summary') or {}
                fact['value'] = {k: v for k, v in value.items()
                                 if k not in {'meetings', 'series_summary'}}
                fact['value']['series_summary'] = {k: series[k] for k in (
                    'series_key', 'season_start', 'games', 'wins', 'winner_team_id') if k in series}
            elif 'players' in value:
                for player in value['players']:
                    for key in ('average_statistics', 'stat_sample_counts', 'previous_team_stints'):
                        player.pop(key, None)
            elif 'metrics' in value:
                omitted.append(fact['id'])
                continue
        if fact['kind'] == 'OBSERVATION' and isinstance(value, dict):
            value.pop('games', None)
            value.pop('metrics', None)
        facts.append(fact)
    view['facts'] = facts
    from .ats import build_spread_comparison
    comparison = locked_forecast.get('spread_comparison')
    if not comparison:
        comparison = build_spread_comparison(game, locked_forecast.get('market_collection'),
                                             None, locked_forecast['final_prediction'])
    view['prediction_interpretation'] = _writer_market_interpretation(
        {'spread_comparison': comparison}, projection['locked_prediction'])
    view['writing_material_policy'] = {
        'omitted_fact_ids': omitted,
        'complete_projection_preserved': True,
        'instruction': '这是选过的写作资料，不是全量资料库。开头用故事和人物进入，不报效率、命中率或场均分差清单；'
            '正文只保留推动观点的主角，写双方怎样出招、怎样回应。关键球员卡解释作用，不补回被省略的统计表。'
            '让分和大小分只按prediction_interpretation给定的相对方向解释；数字差异不是篮球原因。'
            '不要从记忆补统计，不把未确认的伤病当事实。参考文风的节奏，不能复制其球员、事件或方向。',
    }
    return view


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


def effective_repair_patches(draft, units, patches, allowed_unit_ids, projection):
    validation = inspect_repair_patches(draft, units, patches, allowed_unit_ids, projection)
    rejected = validation.get('rejected_patches') or []
    if (rejected and all(row['reasons'] == ['no_op'] for row in rejected)
            and len(rejected) < len(patches)):
        skipped = {row['patch_index'] for row in rejected}
        effective = [patch for index, patch in enumerate(patches) if index not in skipped]
        checked = inspect_repair_patches(draft, units, effective, allowed_unit_ids, projection)
        return effective, {**checked, 'response_patch_count': len(patches),
                           'deferred_no_op_patches': rejected}
    return patches, validation


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
    "unverified_coach_quote": ("引号中的文字不能作为可逐字核验的教练原话；英文采访的中文意译也不是原文引语。",
        "根据已采纳来源把整句改成明确的中文转述，不保留未经核验的引号，不只原样删标点；转述含义仍须有来源支持。"),
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
    "ambiguous_current_player_identity": ("当前简称对应不止一名球员，不能猜成其中一人。",
        "若确指本场当前名单中的球员，请写其完整中文或英文姓名；若指不在本场名单中的球员，删除本场出战/轮换假设，改为本场已核实球员的角色分析。待确认、如果、预计不能替代身份核对。"),
    "current_game_player_identity_unverified": ("该人物没有本场两队的当前名单身份支持。",
        "不得把历史球员或其他球队球员写成本场伤病、出战、首发或轮换变量。删除该本场假设，保留有来源的历史/离队背景时明确过去时间；不要把不在阵容误写成伤病待定。"),
    "current_availability_unverified": ("没有本场已确认的出战/缺阵依据；长期伤病背景不等于本场官方状态。",
        "删除确定的本场出战或长期缺阵断言。可以保留 BACKGROUND 明确支持的伤病事件本身，再作预计/条件判断；不能把受伤自动升级成已确认 OUT。"),
    "confirmed_availability_source_conflict": ("出战状态用词与所指球员的冻结状态冲突，或把其他人的缺阵错误连到了本句提及的球员。",
        "整句改写成该球员的篮球角色或对位分析，删除本句所有缺阵、出战、可打、首发确认等状态表述；不要重复原句或只追加解释，不改事实源或预测数字。"),
    "unverified_global_season_count": ("没有完整赛季日历证据支持本赛季第一场等精确场次断言。",
        "完整删除本赛季第一场、首场、揭幕战等场次表述，改写为新组合或轮换磨合的分析；不能仅改同一句中的分差数字。"),
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


def current_player_catalog(projection):
    catalog = {}
    game = projection.get("game") or {}
    for fact in projection.get("facts", []):
        value = fact.get("value") or {}
        side = value.get("side")
        if (fact.get("kind") != "CURRENT" or side not in {"home", "away"}
                or value.get("team_id") != (game.get(side) or {}).get("id")):
            continue
        for player in value.get("roster_snapshot") or []:
            name = str(player.get("name") or player.get("player_name") or "").strip()
            if name:
                catalog.setdefault(name.casefold(), []).append({"name": name, "side": side,
                    "team": (game.get(side) or {}).get("name_zh"), "fact_id": fact["id"]})
    return catalog


def invalid_player_references(draft, projection):
    catalog = current_player_catalog(projection)
    all_facts = {fact["id"] for fact in projection.get("facts", [])}
    targets = []
    seen = set()
    for index, player in enumerate(draft.get("player_analysis") or []):
        name = player["player_name"].strip().casefold()
        if name in seen:
            raise ValueError("key_player_duplicate_or_unknown_fact")
        seen.add(name)
        identities = catalog.get(name, [])
        if not identities or len({row["side"] for row in identities}) != 1:
            raise ValueError("key_player_current_identity_not_supported")
        refs = set(player["fact_ids"])
        if not refs <= all_facts or not refs.intersection(row["fact_id"] for row in identities):
            targets.append({"index": index, "original": copy.deepcopy(player), "current_identity": identities})
    return targets


def apply_player_reference_repair(draft, patches, targets, projection):
    allowed = {target["index"]: target for target in targets}
    indices = [patch["index"] for patch in patches]
    if len(indices) != len(set(indices)) or set(indices) != set(allowed):
        raise ValueError("key_player_reference_repair_scope_invalid")
    result = copy.deepcopy(draft)
    for patch in patches:
        index = patch["index"]
        if patch["player_name"] != draft["player_analysis"][index]["player_name"]:
            raise ValueError("key_player_reference_repair_changed_player")
        result["player_analysis"][index] = {key: patch[key] for key in ("player_name", "statement", "fact_ids")}
    if invalid_player_references(result, projection):
        raise ValueError("key_player_reference_repair_unverified")
    return result


def run_editorial(context, locked_forecast, folder, *, writer_client=None, reviewer_client=None,
                  quality_review_enabled=False, max_calls=4, writer_model="deepseek-v4-flash", writing_brief=None,
                  require_poem_title=True):
    if writer_model not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
        raise ValueError('unsupported_editorial_model')
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
    writing_input = writer_view(projection, locked_forecast, c.get('game') or {})
    if writing_brief is not None:
        writing_input['editor_assignment'] = {
            'origin': 'operator_editorial_direction',
            'scope': 'writing focus and voice, not new facts or a Forecasting result',
            'brief': copy.deepcopy(writing_brief),
        }
    writing_input['current_player_identity_index'] = list(current_player_catalog(projection).values())
    save(folder / 'writer_view.json', writing_input)
    draft, meta = call_recorded(folder, "writer0", [{"role": "system", "content": skill +
        "\n本次输入是冻结写作资料与已完成的第25秒篮球分析素材。用它形成作者判断；素材不是客观事实，也不提供ML特征归因。"
        "完整参考稿只提供文风，不提供本场事实、方向或现成句子；其中注明被用户否定的开场不得模仿。"
        "写成自然、顺口的体育评论，不为制造激情硬切短句；人物互动和具体比赛过程承担分析，不要列名单、复述资料或写成战术说明书。"
        "visibility=public 可按来源准确表达；visibility=writer_internal 只用于理解判断路径，不照抄内部层名称；audit_only 不会进入本次Writer视图。"
        "用户正文不得出现 Pure、Residual、ML 或其他内部模型层名称。不得搜索或改预测。"},
        {"role": "user", "content": json.dumps(writing_input, ensure_ascii=False)}],
        EditorialDraft, writer_client, max_tokens=5500 if writer_model == 'deepseek-v4-pro' else 3500,
        model=writer_model)
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

    player_targets = invalid_player_references(draft, projection)
    if player_targets:
        save(folder / "player_reference_findings.json", {"targets": player_targets})
        if calls >= max_calls:
            raise ValueError("key_player_reference_repair_budget_unavailable")
        calls += 1
        repaired, meta = call_recorded(folder, "player_reference_revision", [{"role": "system", "content":
            "只修正列出的球员卡身份引用。球员姓名与卡片序号不可变，每个target恰好一个patch；不改其他卡片、标题、正文、预测或事实。"
            "fact_ids必须包含current_identity提供的CURRENT名单fact_id，可保留实际支持说明的其他投影引用。"
            "statement若把球员说成另一队的角色，只按current_identity中真实球队重写这一张卡，不添加新事实；球员具体出战仍未知时保留预计或条件。"},
            {"role": "user", "content": json.dumps({"targets": player_targets,
                "projection": model_view(projection)}, ensure_ascii=False)}],
            PlayerReferenceRepair, writer_client, max_tokens=1800, model=writer_model)
        metadata.append({"stage": "player_reference_revision", **{k: v for k, v in meta.items() if k != "raw"}})
        draft = apply_player_reference_repair(draft, repaired["patches"], player_targets, projection)
        repair_rounds += 1
        save(folder / "player_reference_validation.json", {"status": "accepted", "targets": player_targets,
            "patches": repaired["patches"], "other_draft_fields_changed": False})

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
        calls += 1; repair_rounds += 1
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
            save(folder / "summary.json", {"status": "rejected", "calls": calls, "repair_rounds": repair_rounds,
                "hard_fact_findings": hard_findings, "post_repair_findings": unchanged_findings,
                "repair_provenance": [], "repair_validation": response_validation, "stages": metadata})
            raise ValueError("editorial_repair_response_invalid:" + type(exc).__name__) from exc
        metadata.append({"stage": "repair1", **{k: v for k, v in meta.items() if k != "raw"}})
        patches["patches"], response_validation = effective_repair_patches(
            draft, units, patches["patches"], affected, projection)
        if response_validation.get("deferred_no_op_patches"):
            metadata[-1]["deferred_no_op_patches"] = response_validation["deferred_no_op_patches"]
        if response_validation["status"] != "accepted":
            unchanged_findings = validate_publication_hard_facts(judgment, projection)
            metadata[-1]["status"] = "ineffective_or_out_of_scope"
            metadata[-1]["rejected_patches"] = response_validation["rejected_patches"]
            response_validation.update(reason="repair_response_ineffective_or_out_of_scope",
                original_findings=hard_findings, applied_patches=[],
                post_repair_findings=unchanged_findings)
            save(folder / "repair1_validation.json", response_validation)
            save(folder / "summary.json", {"status": "rejected", "calls": calls, "repair_rounds": repair_rounds,
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
        if hard_findings and calls < max_calls:
            # One final wording correction for source scope; never another forecast.
            residual = copy.deepcopy(hard_findings)
            second_affected = {row["unit_id"] for row in residual}
            second_targets = [unit for unit in units if unit["id"] in second_affected]
            calls += 1
            repair_rounds += 1
            second, meta = call_recorded(folder, "repair2", [{"role": "system", "content":
                "这是最后一次限定文本修正。只修 affected_text 中仍然触发的确定性冲突，按finding_explanations改正。"
                "没有本场来源时删去确定缺阵判断，改成明确的如果/若条件句或仅描述伤病恢复背景；不能只换姓名语言或加背景二字。"
                "数字和地点等具体断言只能使用冻结投影中同口径事实；无安全依据时改成不含该精确断言的分析句。"
                "逐个target返回一个实质不同的replacement，不改其他句子、球员身份、预测数字或原始事实，不添加新断言。"},
                {"role": "user", "content": json.dumps({"projection": model_view(projection),
                    "hard_fact_blockers": residual, "affected_text": second_targets,
                    "finding_explanations": _repair_guidance(residual)}, ensure_ascii=False)}],
                EditorialRepair, writer_client, max_tokens=1600)
            second["patches"], second_validation = effective_repair_patches(
                draft, units, second["patches"], second_affected, projection)
            if second_validation["status"] != "accepted":
                save(folder / "repair2_validation.json", second_validation)
                raise ValueError("editorial_second_repair_invalid")
            draft, extra_overrides, extra_provenance = apply_patches(draft, units, second["patches"],
                second_affected, include_judgment_overrides=True, projection=projection)
            judgment_overrides = list({**dict(judgment_overrides), **dict(extra_overrides)}.items())
            judgment = make_judgment(draft, judgment_overrides)
            hard_findings = validate_publication_hard_facts(judgment, projection)
            repair_provenance.extend(extra_provenance)
            metadata.append({"stage": "repair2", **{k: v for k, v in meta.items() if k != "raw"},
                "applied_patches": extra_provenance})
            save(folder / "draft2.json", {"draft": draft, "judgment": judgment, "hard_fact_findings": hard_findings})
            save(folder / "repair2_validation.json", {**second_validation, "original_findings": residual,
                "applied_patches": extra_provenance, "post_repair_findings": hard_findings})
        if hard_findings:
            save(folder / "summary.json", {"status": "rejected", "calls": calls, "repair_rounds": repair_rounds,
                "hard_fact_findings": hard_findings, "post_repair_findings": hard_findings,
                "repair_provenance": repair_provenance, "repair_validation": validation, "stages": metadata})
            raise ValueError("publication_hard_fact_blocker:" + json.dumps(hard_findings, ensure_ascii=False))

    title_policy = {"required": bool(require_poem_title), "revision_calls": 0}
    if require_poem_title:
        from .editorial_titles import TitleRevision, normalize_poem_title
        original_title = judgment["pregame_story"]["title"]
        title = normalize_poem_title(original_title)
        baseline_title=locked_forecast.get('same_game_baseline_title')or{}
        fallback_title=(normalize_poem_title(baseline_title.get('title'))
            if baseline_title.get('game_id')==c.get('game',{}).get('game_id')and baseline_title.get('version_id')else None)
        if title is None and fallback_title and calls >= max_calls:
            title = fallback_title
            title_policy['retained_same_game_baseline_title'] = baseline_title['version_id']
            title_policy['retained_without_extra_call'] = True
        if title is None:
            if calls >= max_calls:
                save(folder / "title_validation.json", {**title_policy, "status": "budget_unavailable"})
                raise ValueError("editorial_title_correction_budget_unavailable")
            calls += 1
            title_policy["revision_calls"] = 1
            revised, meta = call_recorded(folder, "title_revision", [
                {"role": "system", "content": skill +
                    "\n这次只改标题。严格返回upper和lower两个数组，每个恰好七项，每项一个汉字。不要返回title字段。"
                    "两句各七个汉字，不加标点，不把两个字塞进同一项。程序会逐项检查并拼成两句。"
                    "只用所给本场事实，不写确定赛果，不生造恩怨或缩写，不改正文、人物卡与预测。"},
                {"role": "user", "content": json.dumps({"current_title": original_title,
                    "current_line_character_counts":[len(line)for line in original_title.splitlines()],
                    "current_line_characters":[list(line)for line in original_title.splitlines()],
                    "required_characters_per_line":7,
                    "same_game_valid_title_if_still_applicable":fallback_title,
                    "writing_material": writing_input}, ensure_ascii=False)}],
                TitleRevision, writer_client, max_tokens=300, model=writer_model)
            metadata.append({"stage": "title_revision", **{k:v for k,v in meta.items() if k != "raw"}})
            title = normalize_poem_title(TitleRevision.model_validate(revised).rendered_title())
            if title is None and fallback_title:
                title=fallback_title
                title_policy['retained_same_game_baseline_title']=baseline_title['version_id']
            if title is None:
                save(folder / "title_validation.json", {**title_policy, "status": "invalid_after_one_revision"})
                raise ValueError("editorial_title_format_invalid_after_one_revision")
        before_title_change = copy.deepcopy(judgment)
        judgment["pregame_story"]["title"] = title
        from .analyst import AnalystContext, AnalystJudgment, validate_judgment
        judgment = validate_judgment(AnalystJudgment.model_validate(judgment),
            AnalystContext.model_validate(c)).model_dump()
        unchanged = copy.deepcopy(judgment)
        unchanged["pregame_story"]["title"] = original_title
        if unchanged != before_title_change:
            raise ValueError("editorial_title_revision_changed_other_fields")
        hard_findings = validate_publication_hard_facts(judgment, projection)
        if hard_findings:
            save(folder / "title_validation.json", {**title_policy, "status": "factual_blocker", "findings": hard_findings})
            raise ValueError("editorial_title_factual_blocker:" + json.dumps(hard_findings, ensure_ascii=False))
        draft["title"] = title
        title_policy.update(status="passed", title=title,
            separator_normalized=title_policy["revision_calls"] == 0 and title != original_title)
        save(folder / "title_validation.json", title_policy)

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
               "repair_provenance": repair_provenance, "title_policy": title_policy, "stages": metadata}
    save(folder / "summary.json", summary)
    return judgment, {"publication_receipt": receipt, "editorial_projection": projection,
        "editorial_material": material, "quality_review": quality, "editorial_calls": calls,
        "quality_review_input_sha256": quality_review_input_sha256,
        "editorial_stages": metadata, "repair_rounds": repair_rounds,
        "repair_provenance": repair_provenance, "title_policy": title_policy,
        "editorial_artifact": str(folder)}
