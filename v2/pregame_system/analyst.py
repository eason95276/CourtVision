"""Evidence-bound optional AI analyst judgment for pregame reports."""
from __future__ import annotations

import json
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from .storage import BASE, clean, content_hash, now, save
from .skill_provenance import prompt_skill_hashes


class DomainAnalysisPerspective(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal[
        "news_market_reaction",
        "pure_market_divergence",
        "surface_narrative_pricing_tension",
        "motivation_proxies",
    ]
    status: Literal["reviewable", "insufficient_evidence"]
    classification: Literal["domain_analysis_perspective"] = "domain_analysis_perspective"
    evidence_ids: list[str] = Field(default_factory=list)
    investigation_questions: list[str]
    missing_requirements: list[str] = Field(default_factory=list)


class AnalystContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pure_prediction: dict[str, Any]
    game: dict[str, Any] | None = None
    analysis_request: str | None = None
    roster_context: dict[str, Any] | None = None
    matchup_history: dict[str, Any] | None = None
    season_form: dict[str, Any] | None = None
    season_phase: dict[str, Any] | None = None
    market_benchmark: dict[str, Any] | None
    divergence: dict[str, Any] | None
    residual_prediction: dict[str, Any] | None
    official_numeric_prediction: dict[str, Any]
    objective_evidence: list[dict[str, Any]]
    evidence_index: dict[str, dict[str, str]]
    missing_data: list[dict[str, Any]]
    evidence_quality: dict[str, Any]
    domain_analysis_perspectives: list[DomainAnalysisPerspective]
    classification_legend: dict[str, str]
    context_hash: str


class AnalysisClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=260)
    classification: Literal["source_fact", "data_inference", "subjective_analysis"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=6)


class PlayerAnalysis(AnalysisClaim):
    player_name: str = Field(min_length=1, max_length=100)


class EditorialStory(AnalysisClaim):
    title: str = Field(min_length=1, max_length=80)
    statement: str = Field(min_length=1, max_length=500)


class WatchTheme(AnalysisClaim):
    title: str = Field(min_length=1, max_length=80)


class PredictionPerspective(AnalysisClaim):
    direction_view: Literal["agree", "disagree", "uncertain"]
    magnitude_view: Literal["too_high", "reasonable", "too_low", "uncertain"]
    market_relation: Literal["aligned", "divergent", "unavailable", "uncertain"]
    statement: str = Field(min_length=1, max_length=400)


class FinalPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    winner_side: Literal["home", "away"]
    win_probability: float = Field(gt=0.5, le=0.99)
    expected_margin: float = Field(ge=0.5, le=40)
    expected_total: float = Field(ge=160, le=300)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)


class AnalystJudgment(BaseModel):
    # DeepSeek occasionally adds harmless top-level explanation fields even
    # under tool calling. Ignore those fields while keeping every persisted
    # nested claim and final-prediction object strict and evidence-validated.
    model_config = ConfigDict(extra="ignore")

    winner_lean: Literal["home", "away", "no_lean", "insufficient_evidence"]
    side_lean: Literal["home", "away", "no_lean", "insufficient_evidence"]
    total_lean: Literal["over", "under", "no_lean", "insufficient_evidence"]
    confidence: Literal["low", "medium", "high"]
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    conflicting_evidence_ids: list[str] = Field(default_factory=list)
    domain_analysis_signals: list[Literal[
        "news_market_reaction",
        "pure_market_divergence",
        "surface_narrative_pricing_tension",
        "motivation_proxies",
    ]] = Field(default_factory=list, max_length=4)
    unresolved_uncertainty: list[str] = Field(default_factory=list, max_length=4)
    baseline_agreement: dict[Literal["pure", "market", "residual"], Literal["agree", "disagree", "mixed", "not_applicable"]]
    subjective_rationale: str = Field(min_length=1, max_length=500)
    core_question: str | None = Field(default=None, max_length=220)
    game_script: str | None = Field(default=None, max_length=500)
    model_risk: str | None = Field(default=None, max_length=500)
    pregame_story: EditorialStory | None = None
    watch_themes: list[WatchTheme] = Field(default_factory=list, max_length=4)
    prediction_perspective: PredictionPerspective | None = None
    final_prediction: FinalPrediction | None = None
    analysis_article: str | None = Field(default=None, max_length=1600)
    analysis_article_evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    roster_analysis: list[AnalysisClaim] = Field(default_factory=list, max_length=2)
    player_analysis: list[PlayerAnalysis] = Field(default_factory=list, max_length=5)
    cold_start_analysis: list[AnalysisClaim] = Field(default_factory=list, max_length=1)
    historical_data_relevance: list[AnalysisClaim] = Field(default_factory=list, max_length=1)
    key_matchup_variables: list[AnalysisClaim] = Field(default_factory=list, max_length=2)
    supporting_evidence: list[AnalysisClaim] = Field(default_factory=list, max_length=2)
    conflicting_evidence: list[AnalysisClaim] = Field(default_factory=list, max_length=1)
    uncertainty: list[AnalysisClaim] = Field(default_factory=list, max_length=2)
    classification: Literal["subjective_analyst_judgment"] = "subjective_analyst_judgment"


class ProductAnalystOutput(BaseModel):
    """Compact schema used only for the fan-facing pregame generation call."""

    model_config = ConfigDict(extra="ignore")

    winner_lean: Literal["home", "away"]
    side_lean: Literal["home", "away", "no_lean", "insufficient_evidence"]
    total_lean: Literal["over", "under", "no_lean", "insufficient_evidence"]
    confidence: Literal["low", "medium", "high"]
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    conflicting_evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    baseline_agreement: dict[
        Literal["pure", "market", "residual"],
        Literal["agree", "disagree", "mixed", "not_applicable"],
    ]
    subjective_rationale: str = Field(min_length=1, max_length=500)
    pregame_story: EditorialStory
    watch_themes: list[WatchTheme] = Field(min_length=2, max_length=4)
    final_prediction: FinalPrediction
    analysis_article: str = Field(min_length=400, max_length=1600)
    analysis_article_evidence_ids: list[str] = Field(min_length=1, max_length=12)
    player_analysis: list[PlayerAnalysis] = Field(default_factory=list, max_length=4)
    roster_analysis: list[AnalysisClaim] = Field(default_factory=list, max_length=1)
    cold_start_analysis: list[AnalysisClaim] = Field(default_factory=list, max_length=1)
    historical_data_relevance: list[AnalysisClaim] = Field(default_factory=list, max_length=1)
    key_matchup_variables: list[AnalysisClaim] = Field(default_factory=list, max_length=2)
    supporting_evidence: list[AnalysisClaim] = Field(default_factory=list, max_length=2)
    conflicting_evidence: list[AnalysisClaim] = Field(default_factory=list, max_length=1)
    uncertainty: list[AnalysisClaim] = Field(default_factory=list, max_length=2)
    classification: Literal["subjective_analyst_judgment"] = "subjective_analyst_judgment"


def _ids(evidence: list[dict[str, Any]], section: str) -> list[str]:
    return [item["id"] for item in evidence if item.get("section") == section and item.get("id")]


def _source_summary(source: dict[str, Any] | None) -> dict[str, Any] | None:
    if not source:
        return None
    return clean({
        key: source.get(key)
        for key in ("url", "captured_at", "sha256", "source_type", "parser_version", "status")
        if source.get(key) is not None
    })


def _compact_roster_context(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    result = {
        "status": value.get("status"),
        "game_id": value.get("game_id"),
        "cutoff": value.get("cutoff"),
        "numeric_model_usage": value.get("numeric_model_usage"),
        "teams": {},
    }
    cold_fields = (
        "status", "season_start", "current_season_games_observed", "current_season_games_confirmed",
        "current_season_coverage", "cold_start", "window_provenance",
        "rolling_5_current_season_games", "rolling_5_previous_season_games", "rolling_5_older_season_games",
        "rolling_15_current_season_games", "rolling_15_previous_season_games", "rolling_15_older_season_games",
        "previous_season_is_majority", "historical_capture_semantics",
    )
    continuity_fields = (
        "status", "retained_minutes_ratio", "top8_retained_count", "top8_retained_minutes_ratio",
        "roster_identity_coverage", "minute_rows_coverage", "reason", "confirmed_starters",
        "projected_starters", "current_rotation", "confirmed_transactions",
    )
    for side, team in (value.get("teams") or {}).items():
        official = (team or {}).get("official_team_roster") or (team or {}).get("roster")
        assignment = (team or {}).get("league_team_assignment")
        source_record = (team or {}).get("source_record") or {}
        players = [
            clean({key: player.get(key) for key in ("player_id", "name", "position", "number") if player.get(key) not in (None, "")})
            for player in ((official or {}).get("players") or [])
        ]
        assignment_names = {
            str(player.get("player_id")): player.get("name")
            for player in ((assignment or {}).get("players") or [])
            if player.get("player_id") is not None
        }
        assignment_only = [
            {"player_id": str(player_id), "name": assignment_names.get(str(player_id))}
            for player_id in (source_record.get("assignment_only_player_ids") or [])
        ]
        cold = (team or {}).get("cold_start") or {}
        continuity = (team or {}).get("continuity") or {}
        result["teams"][side] = clean({
            "team_id": (team or {}).get("team_id"),
            "roster_basis": (team or {}).get("roster_basis"),
            "official_team_roster": {
                "status": (official or {}).get("status"),
                "players": players,
                "player_count": len(players),
                "coverage": (official or {}).get("coverage"),
                "source": _source_summary((official or {}).get("source")),
            } if official else None,
            "league_assignment_cross_check": {
                "status": (assignment or {}).get("status"),
                "player_count": len((assignment or {}).get("players") or []),
                "assignment_only_players": assignment_only,
                "interpretation": "source membership difference only; no transaction or contract conclusion",
            } if assignment else None,
            "continuity": {key: continuity.get(key) for key in continuity_fields if key in continuity},
            "cold_start": {key: cold.get(key) for key in cold_fields if key in cold},
            "key_player_status": (team or {}).get("key_player_status"),
            "confirmed_starters": (team or {}).get("confirmed_starters"),
            "current_rotation": (team or {}).get("current_rotation"),
            "missing": (team or {}).get("missing") or [],
        })
    return clean(result)


def _compact_evidence(evidence: list[dict[str, Any]], roster: dict[str, Any] | None) -> list[dict[str, Any]]:
    compact = []
    for item in evidence:
        current = clean(item)
        if current.get("section") == "roster_context":
            current["value"] = roster
        compact.append(current)
    return compact


def build_analyst_context(
    prediction_layers: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    missing_data: list[dict[str, Any]],
    analysis_request: str | None = None,
    game: dict[str, Any] | None = None,
) -> AnalystContext:
    sections = {item.get("section") for item in evidence}
    market_ids = _ids(evidence, "market")
    availability_ids = _ids(evidence, "availability")
    form_ids = _ids(evidence, "team_form")
    matchup_ids = _ids(evidence, "matchup")
    divergence_available = prediction_layers.get("divergence") is not None
    perspectives = [
        DomainAnalysisPerspective(
            name="news_market_reaction",
            status="reviewable" if market_ids and availability_ids else "insufficient_evidence",
            evidence_ids=[*market_ids, *availability_ids],
            investigation_questions=[],
            missing_requirements=[] if market_ids and availability_ids else ["legal market", "timestamp-qualified availability"],
        ),
        DomainAnalysisPerspective(
            name="pure_market_divergence",
            status="reviewable" if divergence_available else "insufficient_evidence",
            evidence_ids=_ids(evidence, "prediction") + market_ids,
            investigation_questions=[],
            missing_requirements=[] if divergence_available else ["legal market benchmark"],
        ),
        DomainAnalysisPerspective(
            name="surface_narrative_pricing_tension",
            status="reviewable" if market_ids and (form_ids or matchup_ids) else "insufficient_evidence",
            evidence_ids=[*market_ids, *form_ids, *matchup_ids],
            investigation_questions=[],
            missing_requirements=[] if market_ids and (form_ids or matchup_ids) else ["market and basketball narrative evidence"],
        ),
        DomainAnalysisPerspective(
            name="motivation_proxies",
            status="reviewable" if form_ids else "insufficient_evidence",
            evidence_ids=form_ids,
            investigation_questions=[],
            missing_requirements=["standings", "verified rotation signal"],
        ),
    ]
    full_roster = next((item.get("value") for item in evidence if item.get("section") == "roster_context"), None)
    roster_context = _compact_roster_context(full_roster)
    matchup_history = next(
        (clean(item.get("value")) for item in evidence if item.get("section") == "matchup_history"),
        None,
    )
    season_form = next(
        (clean(item.get("value")) for item in evidence if item.get("section") == "season_form"),
        None,
    )
    season_phase = next(
        (clean(item.get("value")) for item in evidence if item.get("section") == "season_phase"),
        None,
    )
    core = {
        "game": clean({k: game[k] for k in ("game_id", "home", "away", "home_team_id", "away_team_id", "date", "tipoff", "scheduled_tipoff", "cutoff") if k in game}) if game else None,
        "analysis_request": analysis_request[:500] if analysis_request else None,
        "roster_context": roster_context,
        "matchup_history": matchup_history,
        "season_form": season_form,
        "season_phase": season_phase,
        "pure_prediction": clean(prediction_layers["pure_prediction"]),
        "market_benchmark": clean(prediction_layers.get("market_benchmark")),
        "divergence": clean(prediction_layers.get("divergence")),
        "residual_prediction": clean(prediction_layers.get("residual_prediction")),
        "official_numeric_prediction": clean(prediction_layers["official_numeric_prediction"]),
        "objective_evidence": _compact_evidence(evidence, roster_context),
        "evidence_index": {
            str(item["id"]): {
                "section": str(item.get("section") or "unknown"),
                "source_type": str(item.get("kind") or "unknown"),
            }
            for item in evidence
            if item.get("id")
        },
        "missing_data": clean(missing_data),
        "evidence_quality": {
            "available_sections": sorted(str(section) for section in sections if section),
            "missing_sections": sorted({str(item.get("section")) for item in missing_data if item.get("section")}),
            "legal_market_available": "market" in sections and prediction_layers.get("market_benchmark") is not None,
            "timestamp_qualified_availability_available": "availability" in sections,
            "line_movement_available": False,
            "standings_available": False,
            "public_betting_data_available": False,
        },
        "domain_analysis_perspectives": [item.model_dump() for item in perspectives],
        "classification_legend": {
            "roster_source_fact": "roster listed at capture time, not confirmed opening-night roster or transaction",
            "roster_calculation": "minutes retention and window provenance; no numeric forecast adjustment",
            "observed_fact": "source-bound retrieved fact",
            "deterministic_model_output": "reproducible numeric model output",
            "statistical_inference": "data-supported inference with uncertainty",
            "domain_experience": "analysis perspective requiring evidence checks",
            "subjective_analyst_judgment": "language-model synthesis that cannot replace official numeric forecast",
        },
    }
    return AnalystContext(**core, context_hash=content_hash(core))


def validate_judgment(
    judgment: AnalystJudgment | dict[str, Any], context: AnalystContext
) -> AnalystJudgment:
    value = AnalystJudgment.model_validate(judgment)
    if set(value.baseline_agreement)!={'pure','market','residual'}:
        raise ValueError('judgment_baseline_agreement_incomplete')
    allowed = {item["id"] for item in context.objective_evidence if item.get("id")}
    claim_groups = (
        value.roster_analysis,
        value.player_analysis,
        value.cold_start_analysis,
        value.historical_data_relevance,
        value.key_matchup_variables,
        value.supporting_evidence,
        value.conflicting_evidence,
        value.uncertainty,
        value.watch_themes,
    )
    claims = [claim for group in claim_groups for claim in group]
    if value.pregame_story is not None:
        claims.append(value.pregame_story)
    if value.prediction_perspective is not None:
        claims.append(value.prediction_perspective)
    referenced = (
        set(value.supporting_evidence_ids)
        | set(value.conflicting_evidence_ids)
        | {evidence_id for claim in claims for evidence_id in claim.evidence_ids}
        | set(value.final_prediction.evidence_ids if value.final_prediction else [])
        | set(value.analysis_article_evidence_ids)
    )
    if not referenced <= allowed:
        raise ValueError("unknown_judgment_evidence")
    if set(value.supporting_evidence_ids) & set(value.conflicting_evidence_ids):
        raise ValueError("judgment_evidence_role_conflict")
    if len(value.domain_analysis_signals)!=len(set(value.domain_analysis_signals)):
        raise ValueError('judgment_domain_signal_duplicate')
    product_request = "第25秒赛前" in (context.analysis_request or "")
    if product_request and (
        value.pregame_story is None
        or value.final_prediction is None
        or value.analysis_article is None
        or len(value.analysis_article) < 400
        or not 2 <= len(value.watch_themes) <= 4
    ):
        raise ValueError("judgment_product_content_incomplete")
    if product_request and any(
        not claim.evidence_ids
        for claim in [
            value.pregame_story,
            *value.watch_themes,
        ]
        if claim is not None
    ):
        raise ValueError("judgment_product_content_requires_evidence")
    if product_request and not value.analysis_article_evidence_ids:
        raise ValueError("judgment_article_requires_evidence")
    if product_request and not value.final_prediction.evidence_ids:
        raise ValueError("judgment_final_prediction_requires_evidence")
    story_evidence_ids = {
        str(item["id"])
        for item in context.objective_evidence
        if item.get("id")
        and item.get("section") == "web_context"
        and (item.get("value") or {}).get("category") == "game_story"
    }
    champion_story_ids = {
        str(item["id"])
        for item in context.objective_evidence
        if item.get("id")
        and item.get("section") == "web_context"
        and (item.get("value") or {}).get("category") == "game_story"
        and re.search(
            r"champion|总冠军",
            f"{(item.get('value') or {}).get('title') or ''} "
            f"{(item.get('value') or {}).get('snippet') or ''}",
            flags=re.IGNORECASE,
        )
    }
    champion_story_ids.update(
        str(item["id"])
        for item in context.objective_evidence
        if item.get("id") and item.get("section") == "season_form"
        and any(
            (team.get("previous_playoffs") or {}).get("championship_clinched") is True
            for team in ((item.get("value") or {}).get("teams") or {}).values()
        )
    )
    if (
        product_request
        and champion_story_ids
        and re.search(r"冠军|卫冕|champion|defending", f"{value.pregame_story.title} {value.pregame_story.statement}", re.IGNORECASE)
        and not champion_story_ids.intersection(value.pregame_story.evidence_ids)
    ):
        raise ValueError("judgment_story_ignores_verified_championship")
    if product_request and (context.season_phase or {}).get("phase") == "opening_night":
        historical_copy = " ".join(
            [
                value.analysis_article or "",
                *(item.statement for item in value.watch_themes),
            ]
        )
        for sentence in re.split(r"[。！？!?]", historical_copy):
            if re.search(r"近\s*15\s*场", sentence) and "上赛季" not in sentence:
                raise ValueError("opening_night_rolling_window_missing_previous_season_label")
    if product_request and story_evidence_ids:
        story_copy = f"{value.pregame_story.title} {value.pregame_story.statement}"
        story_sources = " ".join(
            f"{(item.get('value') or {}).get('title') or ''} "
            f"{(item.get('value') or {}).get('snippet') or ''}"
            for item in context.objective_evidence
            if str(item.get("id")) in story_evidence_ids
        )
        if re.search(
            r"戒指|ring ceremony|颁发.{0,6}冠军|冠军旗帜升起|旗帜升起|加冕夜",
            story_copy,
            flags=re.IGNORECASE,
        ) and not re.search(
            r"戒指|ring ceremony|championship rings?|banner ceremony|raise.{0,8}banner",
            story_sources,
            flags=re.IGNORECASE,
        ):
            raise ValueError("unsupported_championship_ceremony_claim")
        if (
            value.pregame_story.classification == "source_fact"
            and re.search(r"复仇|雪耻|报仇|整个.{0,8}(?:夏天|休赛期).{0,8}研究|专门.{0,8}针对", story_copy)
        ):
            raise ValueError("subjective_motivation_labeled_as_fact")
        if re.search(r"队史首冠|队史第一个总冠军", story_copy) and not re.search(
            r"first[- ]ever championship|first championship in franchise history|队史首冠",
            story_sources,
            flags=re.IGNORECASE,
        ):
            raise ValueError("unsupported_franchise_first_championship_claim")
    if product_request and value.winner_lean != value.final_prediction.winner_side:
        raise ValueError("judgment_final_prediction_direction_mismatch")
    if product_request:
        public_product_prose = " ".join(
            [
                value.pregame_story.title,
                value.pregame_story.statement,
                *(part for item in value.watch_themes for part in (item.title, item.statement)),
                value.analysis_article,
            ]
        )
        series_totals = ((context.matchup_history or {}).get("series_summary") or {}).get("game_totals") or []
        series_margins = ((context.matchup_history or {}).get("series_summary") or {}).get("game_margins") or []
        if series_margins and min(series_margins) < 10 and re.search(
            r"(?:[两二三四五六七]场|每场|全部|所有).{0,8}(?:没能|未能|没有|未曾).{0,12}(?:个位数|10分以内|十分以内)",
            public_product_prose,
        ):
            raise ValueError("series_margin_claim_conflicts_with_scores")
        if series_totals:
            for match in re.finditer(r"总分.{0,8}(?:多在|大多|多数|通常).{0,5}(\d{3})(?:分)?上下", public_product_prose):
                center = int(match.group(1))
                if sum(abs(float(total) - center) <= 10 for total in series_totals) <= len(series_totals) / 2:
                    raise ValueError("unsupported_series_total_generalization")
        market_totals: list[float] = []
        for item in context.objective_evidence:
            source_value = item.get("value") or {}
            market_value = (
                source_value.get("market_reference") or {}
                if item.get("section") == "web_context"
                else source_value
                if item.get("section") == "market"
                else {}
            )
            try:
                market_totals.append(float(market_value["total"]))
            except (KeyError, TypeError, ValueError):
                continue
        if market_totals:
            market_total = market_totals[0]
            article = value.analysis_article
            leans_over = re.search(r"(?:我|第25秒)?.{0,8}(?:偏向|倾向|看好).{0,3}大分", article)
            leans_under = re.search(r"(?:我|第25秒)?.{0,8}(?:偏向|倾向|看好).{0,3}小分", article)
            if value.final_prediction.expected_total < market_total and leans_over:
                raise ValueError("judgment_total_lean_conflicts_with_final_prediction")
            if value.final_prediction.expected_total > market_total and leans_under:
                raise ValueError("judgment_total_lean_conflicts_with_final_prediction")
    if product_request:
        numeric_values = (
            value.final_prediction.expected_margin,
            value.final_prediction.expected_total,
        )
        if any(abs(item * 2 - round(item * 2)) > 1e-9 for item in numeric_values):
            raise ValueError("judgment_final_prediction_not_half_point")
        if abs(
            value.final_prediction.win_probability * 100
            - round(value.final_prediction.win_probability * 100)
        ) > 1e-9:
            raise ValueError("judgment_final_probability_not_whole_percent")
    if any(
        lean != "insufficient_evidence"
        for lean in (value.winner_lean, value.side_lean, value.total_lean)
    ) and not (
        value.supporting_evidence_ids
        or any(claim.evidence_ids for claim in value.supporting_evidence)
    ):
        raise ValueError("judgment_requires_supporting_evidence")
    official_names = {
        str(player.get("name"))
        for team in ((context.roster_context or {}).get("teams") or {}).values()
        for player in (((team or {}).get("official_team_roster") or {}).get("players") or [])
        if player.get("name")
    }
    if official_names and any(item.player_name not in official_names for item in value.player_analysis):
        raise ValueError("player_not_in_official_roster")
    matchup_names = re.search(r"生成(.+?)对(.+?)的", context.analysis_request or "")
    if matchup_names and context.roster_context:
        team_rosters = {
            side: {
                str(player.get("name") or "").split()[-1].casefold()
                for player in (((team or {}).get("official_team_roster") or {}).get("players") or [])
                if player.get("name") and len(str(player["name"]).split()[-1]) >= 4
            }
            for side, team in ((context.roster_context.get("teams") or {}).items())
        }
        for side, team_name in (("away", matchup_names.group(1)), ("home", matchup_names.group(2))):
            other_side = "home" if side == "away" else "away"
            other_only = team_rosters.get(other_side, set()) - team_rosters.get(side, set())
            for statement in [value.analysis_article or "", *(claim.statement for claim in claims)]:
                for clause in re.split(r"[，,；;。！？!?]", statement):
                    if not re.search(rf"{re.escape(team_name)}.{{0,8}}(?:官方|当前)?名单(?:里|中|上)", clause):
                        continue
                    if any(re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", clause, re.IGNORECASE)
                           for name in other_only):
                        raise ValueError("player_attributed_to_wrong_team_roster")
    prose = " ".join(
        [
            value.subjective_rationale,
            value.core_question or "",
            value.game_script or "",
            value.model_risk or "",
            value.pregame_story.statement if value.pregame_story else "",
            value.prediction_perspective.statement if value.prediction_perspective else "",
            *(item.statement for item in value.watch_themes),
            value.analysis_article or "",
            *value.unresolved_uncertainty,
        ]
    )
    numeric_forecast = re.search(
        r"(?:胜率|概率|分差|预计总分|预测总分|比分).{0,12}[-+]?\d+(?:\.\d+)?%?"
        r"|[-+]?\d+(?:\.\d+)?%?.{0,12}(?:胜率|概率|分差|预计总分|预测总分|比分)",
        prose,
    )
    if numeric_forecast and not product_request:
        raise ValueError("judgment_exact_numeric_forecast_not_allowed")
    blocked = ("下注", "投注金额", "bankroll", "bet amount", "expected return", "自动投注")
    if any(term.casefold() in prose.casefold() for term in blocked):
        raise ValueError("judgment_betting_execution_language")
    prose_fields = [
        value.subjective_rationale,
        value.core_question or "",
        value.game_script or "",
        value.model_risk or "",
        value.pregame_story.statement if value.pregame_story else "",
        value.prediction_perspective.statement if value.prediction_perspective else "",
        *(item.statement for item in value.watch_themes),
        value.analysis_article or "",
        *value.unresolved_uncertainty,
    ]
    all_claim_text = " ".join([*prose_fields, *(claim.statement for claim in claims)])
    detailed_sections = {item.get("section") for item in context.objective_evidence}
    if re.search(r"(?:从未|每场|全部|均未).{0,20}(?:最后[五5]分钟|决胜时刻|关键时刻)", all_claim_text) and not (
        detailed_sections & {"historical_play_by_play", "historical_clutch"}
    ):
        raise ValueError("unsupported_series_game_flow_claim")
    if re.search(r"(?:数字|数据).{0,12}(?:说明|体现).{0,24}出手质量", all_claim_text) and "shot_quality" not in detailed_sections:
        raise ValueError("unsupported_shot_quality_inference")
    negative_markers = (
        "缺少", "缺失", "暂无", "无法确认", "未确认", "不知道", "不确定", "没有", "不可验证",
        "不能", "不代表", "无确认", "无当前", "未知", "如果", "若", "假如",
    )

    def affirmative(text: str, pattern: str) -> bool:
        # A later "unknown" must not erase an affirmative claim made before
        # a comma in the same sentence.
        clauses = [part.strip() for part in re.split(r"[。；;!?！？，,]", text) if part.strip()]
        return any(
            re.search(pattern, clause, flags=re.IGNORECASE)
            and not any(marker in clause for marker in negative_markers)
            and not re.search(r"无.{0,24}(?:证据|消息|依据|信息|公布)", clause)
            for clause in clauses
        )

    evidence_category = {
        str(item.get("id")): (
            str((item.get("value") or {}).get("category"))
            if item.get("section") == "web_context"
            else "availability"
            if item.get("section") == "availability"
            else None
        )
        for item in context.objective_evidence
        if item.get("id")
    }

    def has_category(ids: list[str], category: str) -> bool:
        return any(evidence_category.get(item) == category for item in ids)

    def require_fact_support(pattern: str, category: str, error: str) -> None:
        for claim in claims:
            if affirmative(claim.statement, pattern) and not has_category(claim.evidence_ids, category):
                raise ValueError(error)
        general_ids = [*value.supporting_evidence_ids, *value.conflicting_evidence_ids]
        if any(affirmative(text, pattern) for text in prose_fields) and not has_category(general_ids, category):
            raise ValueError(error)

    attribution_pattern = (
        r"(?:这是|这些|该项|该数据|该差异).{0,18}模型.{0,18}(?:主要|核心|直接).{0,10}(?:来源|依据|驱动|原因)"
        r"|模型(?:主要|正是|直接)?(?:因为|基于|依据).{0,30}(?:给出|得出|预测)"
        r"|模型.{0,36}(?:主要|核心|直接).{0,8}(?:来源|依据|驱动|原因)"
        r"|(?:导致|驱动|决定).{0,12}模型(?:预测|判断|输出)"
    )
    if any(affirmative(text, attribution_pattern) for text in [*prose_fields, *(claim.statement for claim in claims)]):
        raise ValueError("unsupported_model_attribution_claim")

    require_fact_support(
        r"(?:已经|已|确认|被)(?:.{0,8})(?:交易|加盟|离队)|(?:交易|加盟|离队)(?:事实|已确认)",
        "roster_news",
        "unsupported_transaction_claim",
    )
    require_fact_support(
        r"(?:新援|新面孔|新名字|新组合|换血|纸面.{0,8}(?:提升|升级)|"
        r"重组.{0,8}(?:球队|阵容|名单|费城)|全新.{0,8}(?:核心|名单|阵容|组合|进攻)|"
        r"休赛期.{0,8}(?:补强|变动|引进)|补充.{0,8}(?:球员|后场|内线)|"
        r"(?:后场|内线|轮换).{0,4}补充|新加入|新引入|"
        r"补进.{0,8}(?:轮换|内线|后场|球员)|签下)",
        "roster_news",
        "unsupported_roster_change_claim",
    )
    require_fact_support(
        r"(?:确认|预计|将|担任|作为)(?:.{0,10})首发|starting lineup|will start",
        "lineup",
        "unsupported_starter_claim",
    )
    require_fact_support(
        r"(?:确认.{0,8}(?:伤停|缺阵|无法出战|出战成疑)|因伤|伤停|缺阵|出战成疑|questionable|ruled out|will miss)",
        "availability",
        "unsupported_availability_claim",
    )
    return value


class EvidenceBoundAnalyst:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        client=None,
        max_calls: int = 4,
        include_forecasting_skill: bool = False,
        forecast_client=None,
    ):
        self.root = Path(root or BASE / "outputs/analyst_calls")
        self.client = client
        self.max_calls = min(max(int(max_calls), 1), 4)
        self.include_forecasting_skill = bool(include_forecasting_skill)
        self.forecast_client = forecast_client

    def _run_folder(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        if self._used_calls() >= self.max_calls:
            raise ValueError("analyst_call_budget_exhausted")
        folder = self.root / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "_"
            + uuid.uuid4().hex[:8]
        )
        folder.mkdir(exist_ok=False)
        return folder

    def _used_calls(self) -> int:
        return len(list(self.root.glob("*/attempt.json"))) + len(list(self.root.glob("*/forecast/*_request.json")))

    @staticmethod
    def _messages(
        context: AnalystContext,
    ) -> list[dict[str, str]]:
        from .editorial_skill import load_editorial_skill

        system = (
            "你是第25秒的结构化赛前分析生成器。仅依据调用方传入的 AnalystContext 与 objective_evidence 处理本场比赛。"
            "比赛身份、cutoff、球队和球员归属、来源时间与 evidence ID 以传入数据为准，"
            "不得自行替换或补造，也不得把 cutoff 之后的信息写作赛前可得事实。"
            "伤病、出战状态、首发、交易、采访、比赛结果、统计数字和市场报价等可核验事实，必须能由传入 evidence 支持。"
            "不得把 Official Numeric Forecast 改写成新的 Official 数值；AI final_prediction 按独立字段返回。"
            "final_prediction 按所绑定 schema 提供规定字段：winner_side 为 home/away，win_probability 为胜方获胜概率，"
            "expected_margin 为胜方正分差，expected_total 为两队总分。"
            "绑定 ProductAnalystOutput 时，分差和总分使用 0.5 分刻度，概率使用整数百分比。"
            "所有 evidence IDs 必须来自本次 objective_evidence。联网来源和数据来源通过现有 sources/evidence 字段返回。"
            "不得输出下注金额、投注执行或收益承诺。严格遵守调用方绑定的结构化输出 schema。"
        )
        system += (
            "\n\n以下 Editorial Skill 仅用于赛前故事、看点、分析文章的选题和表达：\n"
            + load_editorial_skill()
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": context.model_dump_json()},
        ]

    @staticmethod
    def _repair_structured_args(raw_message: Any) -> tuple[dict[str, Any] | None, list[str]]:
        payload = None
        tool_calls = getattr(raw_message, "tool_calls", None) or []
        if tool_calls and isinstance(tool_calls[0].get("args"), dict):
            payload = clean(tool_calls[0]["args"])
        if payload is None:
            additional = getattr(raw_message, "additional_kwargs", None) or {}
            raw_calls = additional.get("tool_calls") or []
            try:
                payload = json.loads(raw_calls[0]["function"]["arguments"])
            except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                return None, []
        repairs: list[str] = []

        def walk(value: Any, path: str = "") -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    current = f"{path}.{key}" if path else key
                    if (
                        key == "classification"
                        and current != "classification"
                        and item == "subjective_analyst_judgment"
                    ):
                        value[key] = "subjective_analysis"
                        repairs.append(current)
                    else:
                        walk(item, current)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, f"{path}[{index}]")

        walk(payload)
        return payload, repairs

    @staticmethod
    def _repair_public_style(judgment: AnalystJudgment) -> tuple[AnalystJudgment, list[str]]:
        """Legacy entrypoint: normalize escaped newlines only; Skills own prose."""
        payload = judgment.model_dump()
        repairs: list[str] = []

        def repair(text: str, path: str) -> str:
            current = text.replace("\\n", "\n")
            if current != text:
                repairs.append(path)
            return current

        if payload.get("analysis_article"):
            payload["analysis_article"] = repair(payload["analysis_article"], "analysis_article")
        story = payload.get("pregame_story") or {}
        for key in ("title", "statement"):
            if story.get(key):
                story[key] = repair(story[key], f"pregame_story.{key}")
        for index, item in enumerate(payload.get("watch_themes") or []):
            for key in ("title", "statement"):
                if item.get(key):
                    item[key] = repair(item[key], f"watch_themes[{index}].{key}")
        return AnalystJudgment.model_validate(payload), repairs

    def judge(self, context: AnalystContext, *, _correction: dict[str, Any] | None = None,
              _locked_forecast: dict[str, Any] | None = None) -> tuple[AnalystJudgment, dict[str, Any]]:
        folder = self._run_folder()
        # Validate required Skill files before any network side effect. Forecasting
        # runs separately; the writer does not generate a third numeric forecast.
        if self.include_forecasting_skill:
            from .forecasting_skill import load_forecasting_skill
            from .forecasting_workflow import prepare_analyst, run_two_stage
            load_forecasting_skill()
        messages = self._messages(context)
        save(folder / "context.json", context.model_dump())
        locked_forecast = _locked_forecast
        if self.include_forecasting_skill and locked_forecast is None:
            basketball, market, removed = prepare_analyst(context.model_dump())
            if self._used_calls() + (3 if market["evidence"] else 2) > self.max_calls:
                raise ValueError("analyst_budget_cannot_cover_forecast_and_writer")
            locked_forecast = run_two_stage(basketball, market, folder / "forecast",
                client=self.forecast_client or self.client, removed_paths=removed)
        locked_numbers = None
        if locked_forecast is not None:
            numbers = locked_forecast["final_prediction"]
            allowed = {item["id"] for item in context.objective_evidence if item.get("id")}
            ids = sorted({ref for item in locked_forecast["review"]["factors"]
                          for ref in item["evidence_ids"] if ref in allowed})
            locked_numbers = FinalPrediction(
                winner_side=numbers["winner_side"], win_probability=numbers["win_probability"],
                expected_margin=abs(numbers["expected_home_margin"]),
                expected_total=numbers["expected_total"], evidence_ids=ids,
            ).model_dump()
            messages[0]["content"] += (
                "\n本次只撰写和解释已完成两阶段预测的赛前分析。locked_final_prediction 是程序固定的最终数值。"
                "输出 schema 不包含 final_prediction，程序会将已保存的预测绑定到报告。"
                "文章中的预测方向和数字必须与它一致，不得生成另一份预测或擅自修改数值。"
            )
            user_payload = json.loads(messages[1]["content"])
            user_payload["locked_final_prediction"] = locked_numbers
            user_payload["forecast_basis"] = locked_forecast["review"]
            messages[1]["content"] = json.dumps(user_payload, ensure_ascii=False)
        if _correction:
            messages.extend([
                {"role": "assistant", "content": json.dumps(_correction["draft"], ensure_ascii=False)},
                {"role": "user", "content": (
                    f"上一稿未通过事实校验：{_correction['feedback']}。"
                    "依据本次 AnalystContext 和有效 evidence ID 修正所指出的问题，返回符合原绑定 schema 的完整输出。"
                    "继续遵守 cutoff、身份、来源与 Official 数值边界。此次只允许一轮修正。"
                )},
            ])
        output_schema = (
            ProductAnalystOutput
            if "第25秒赛前" in (context.analysis_request or "")
            else AnalystJudgment
        )
        if locked_numbers is not None:
            output_schema = create_model(
                "ForecastLocked" + output_schema.__name__,
                __config__=ConfigDict(extra="ignore"),
                **{name: (field.annotation, deepcopy(field))
                   for name, field in output_schema.model_fields.items() if name != "final_prediction"},
            )

        def parse_output(payload):
            value = output_schema.model_validate(payload).model_dump()
            if locked_numbers is not None:
                value["final_prediction"] = deepcopy(locked_numbers)
                value["winner_lean"] = locked_numbers["winner_side"]
            return AnalystJudgment.model_validate(value)
        save(
            folder / "attempt.json",
            {
                "created_at": now(),
                "context_hash": context.context_hash,
                "input_hash": content_hash(messages),
                "model": "deepseek-v4-flash",
                "model_source": "injected_test_client" if self.client is not None else "configured_transport",
                "skill_hashes": prompt_skill_hashes(messages[0]["content"]),
                "max_retries": 0,
                "max_output_tokens": 4800,
                "output_schema": output_schema.__name__,
                "correction_of": _correction.get("path") if _correction else None,
                "forecast_audit_path": locked_forecast["audit_path"] if locked_forecast else None,
                "forecast_skill_hashes": locked_forecast["skill_hashes"] if locked_forecast else {},
            },
        )
        judgment = None
        try:
            if self.client is not None:
                payload, usage = self.client(messages, output_schema)
                judgment = parse_output(payload)
                raw = clean(payload)
            else:
                from langchain_openai import ChatOpenAI

                key = os.environ.get("DEEPSEEK_API_KEY", "")
                if not key:
                    raise ValueError("analyst_api_key_missing")
                llm = ChatOpenAI(
                    model="deepseek-v4-flash",
                    api_key=key,
                    base_url="https://api.deepseek.com",
                    max_retries=0,
                    timeout=40,
                    max_tokens=4800,
                    temperature=0,
                    use_responses_api=False,
                    disabled_params={"parallel_tool_calls": None},
                    extra_body={"thinking": {"type": "disabled"}, "max_tokens": 4800},
                )
                structured = llm.with_structured_output(
                    output_schema, method="function_calling", include_raw=True
                )
                result = structured.invoke(messages)
                raw_message = result.get("raw")
                structured_repairs: list[str] = []
                if not result.get("parsed"):
                    repaired, structured_repairs = self._repair_structured_args(raw_message)
                    if repaired is not None:
                        try:
                            judgment = parse_output(repaired)
                        except Exception:
                            judgment = None
                    else:
                        judgment = None
                else:
                    judgment = parse_output(result["parsed"].model_dump())
                if judgment is None:
                    save(
                        folder / "structured_output_invalid.json",
                        {
                            "parsing_error": str(result.get("parsing_error"))[:1200],
                            "raw": raw_message.model_dump(mode="json") if raw_message else None,
                        },
                    )
                    raise ValueError("analyst_structured_output_invalid")
                raw = raw_message.model_dump(mode="json") if raw_message else None
                usage = (
                    (raw_message.response_metadata or {}).get("token_usage", {})
                    if raw_message
                    else {}
                )
            save(folder / "parsed_unvalidated.json", judgment.model_dump())
            judgment, style_repairs = self._repair_public_style(judgment)
            judgment = validate_judgment(judgment, context)
            response = {
                "received_at": now(),
                "context_hash": context.context_hash,
                "judgment": judgment.model_dump(),
                "usage": clean(usage or {}),
                "raw": raw,
                "structured_repairs": structured_repairs if self.client is None else [],
                "style_repairs": style_repairs,
                "forecast_workflow": locked_forecast,
            }
            response["response_hash"] = content_hash(response)
            path = save(folder / "response.json", response)
            return judgment, {
                "status": "success",
                "path": str(path),
                "usage": response["usage"],
                "response_hash": response["response_hash"],
                "forecast_audit_path": locked_forecast["audit_path"] if locked_forecast else None,
                "forecast_skill_hashes": locked_forecast["skill_hashes"] if locked_forecast else {},
            }
        except Exception as exc:
            key = os.environ.get("DEEPSEEK_API_KEY", "")
            message = str(exc).replace(key, "[REDACTED]")[:600] if key else str(exc)[:600]
            save(
                folder / "failure.json",
                {
                    "failed_at": now(),
                    "context_hash": context.context_hash,
                    "error_type": type(exc).__name__,
                    "message": message,
                },
            )
            feedback = {
                "unsupported_roster_change_claim": "当前名单没有证明人员新加入、补强、升级或新面孔，所有字段都应删除此类未经证实的表述",
                "player_attributed_to_wrong_team_roster": "文中把对方名单中的球员写入本队名单，必须逐人核对 official_team_roster 的 home/away 归属",
                "unsupported_series_total_generalization": "系列赛总分区间概括与实际逐场总分不符，应核对 game_totals 与 average_total",
                "opening_night_rolling_window_missing_previous_season_label": "揭幕战的近15场来自上赛季，必须标明历史赛季",
                "unsupported_series_game_flow_claim": "比分和场均数据不能证明整轮系列赛是否进入最后五分钟的关键时刻，没有逐回合证据就删除该断言",
                "unsupported_shot_quality_inference": "现有命中率和球队统计未提供出手质量证据，不得将该推断表述为已核实事实",
                "series_margin_claim_conflicts_with_scores": f"逐场分差为{((context.matchup_history or {}).get('series_summary') or {}).get('game_margins')}，其中包含个位数分差，不能声称每场都未能接近个位数",
            }.get(str(exc))
            if (
                feedback and judgment is not None and _correction is None
                and "第25秒赛前" in (context.analysis_request or "")
                and self._used_calls() < self.max_calls
            ):
                corrected, trace = self.judge(context, _correction={
                    "path": str(folder), "feedback": feedback, "draft": judgment.model_dump(),
                }, _locked_forecast=locked_forecast)
                trace["correction"] = {"previous_attempt": str(folder), "reason": str(exc), "attempts": 1}
                return corrected, trace
            raise
