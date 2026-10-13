"""Evidence-bound optional AI analyst judgment for pregame reports."""
from __future__ import annotations

import json
import os
import re
import uuid
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class CentralThesisClaim(AnalysisClaim):
    statement: str = Field(min_length=1, max_length=600)


class KeyMatchupClaim(AnalysisClaim):
    statement: str = Field(min_length=1, max_length=900)


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


class EditorialClaimBinding(BaseModel):
    """Internal major-claim trace; no user-visible sentence-by-sentence citations."""
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=500)
    classification: Literal["FACT", "INFERENCE", "UNKNOWN", "CONDITIONAL"]
    evidence_ids: list[str] = Field(min_length=1, max_length=8)


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
    central_thesis: CentralThesisClaim | None = None
    core_question: str | None = Field(default=None, max_length=220)
    game_script: str | None = Field(default=None, max_length=800)
    model_risk: str | None = Field(default=None, max_length=500)
    pregame_story: EditorialStory | None = None
    watch_themes: list[WatchTheme] = Field(default_factory=list, max_length=4)
    prediction_perspective: PredictionPerspective | None = None
    final_prediction: FinalPrediction | None = None
    analysis_article: str | None = Field(default=None, max_length=2200)
    analysis_article_evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    editorial_claim_bindings: list[EditorialClaimBinding] = Field(default_factory=list, max_length=16)
    roster_analysis: list[AnalysisClaim] = Field(default_factory=list, max_length=2)
    player_analysis: list[PlayerAnalysis] = Field(default_factory=list, max_length=10)
    cold_start_analysis: list[AnalysisClaim] = Field(default_factory=list, max_length=1)
    historical_data_relevance: list[AnalysisClaim] = Field(default_factory=list, max_length=1)
    key_matchup_variables: list[KeyMatchupClaim] = Field(default_factory=list, max_length=3)
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
    watch_themes: list[WatchTheme] = Field(min_length=0, max_length=4)
    final_prediction: FinalPrediction
    analysis_article: str = Field(min_length=1, max_length=2200)
    analysis_article_evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    editorial_claim_bindings: list[EditorialClaimBinding] = Field(default_factory=list, max_length=16)
    player_analysis: list[PlayerAnalysis] = Field(default_factory=list, max_length=10)
    roster_analysis: list[AnalysisClaim] = Field(default_factory=list, max_length=1)
    cold_start_analysis: list[AnalysisClaim] = Field(default_factory=list, max_length=1)
    historical_data_relevance: list[AnalysisClaim] = Field(default_factory=list, max_length=1)
    key_matchup_variables: list[KeyMatchupClaim] = Field(default_factory=list, max_length=3)
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
        for key in ("url", "captured_at", "observed_at", "sha256", "source_type", "parser_version", "status")
        if source.get(key) is not None
    })


def _compact_player_knowledge(value: dict[str, Any]) -> dict[str, Any]:
    """Bound the writer DTO; the full source packet remains in input_snapshot."""
    result = deepcopy(value)
    teams = result.get("teams") or {}
    if not any("previous_season_players" in team or "historical_team_leaders" in team for team in teams.values()):
        return clean(result)
    coverage = result.get("coverage") or {}
    result["coverage"] = {key: coverage[key] for key in (
        "status", "season", "season_type", "provider", "expected_games", "collected_games",
        "player_game_rows", "coverage_ratio", "team_coverage", "cutoff",
    ) if key in coverage}
    if coverage.get("source"):
        result["coverage"]["source"] = {key: coverage["source"][key] for key in (
            "collection_time_min", "collection_time_max", "response_hash_count", "data_version",
        ) if key in coverage["source"]}
    metrics = ("points", "assists", "rebounds", "fga", "three_pa", "turnovers", "steals", "blocks")
    summary_fields = ("games_played", "confirmed_dnp_games", "minutes_total", "minutes_per_appearance",
                      "starts", "starter_observed_games", "team_minutes_share")
    replacement_fields = ("player_id", "candidate_appearances_in_dnp_games", "average_minutes_in_dnp_games",
                          "average_minutes_when_key_player_played", "minutes_difference")
    history_fields = (*summary_fields, "minutes_precision", "minutes_rank", "points_total", "per_observed_game",
                      "observed_games_by_statistic", "missing_games_by_statistic", "shooting_percentages")
    top8_fields = ("player_id", "player_id_namespace", "player_name", "minutes_total", "currently_listed")
    caveats = set()

    def history(profile, *, leader=False):
        if not profile:
            return None
        compact = {"summary": [profile.get(key) for key in summary_fields]}
        if leader:
            compact.update({key: profile[key] for key in ("player_id", "player_id_namespace", "player_name", "team_id") if key in profile})
        precision = profile.get("minutes_precision") or {}
        if precision.get("caveat"):
            caveats.add(precision["caveat"])
        if precision and not leader:
            compact["minutes_precision"] = [precision.get("status"), precision.get("observed_minute_rows")]
        if not leader and profile.get("team_ranks"):
            ranks = profile["team_ranks"]
            compact["minutes_rank"] = [ranks.get("minutes_rank"), (ranks.get("rank_denominators") or {}).get("minutes_rank")]
        averages, samples, missing = [], [], []
        for metric in metrics:
            source = (profile.get("statistics") or {}).get(metric)
            if not source:
                averages.append(None)
                samples.append(None)
                missing.append(None)
                continue
            averages.append(source.get("per_observed_game", source.get("per_appearance")))
            samples.append(source.get("observed_games"))
            missing.append(source.get("missing_games"))
            if metric == "points" and not leader and "total" in source:
                compact["points_total"] = source["total"]
        compact["per_observed_game"] = averages
        compact["observed_games_by_statistic"] = samples
        compact["missing_games_by_statistic"] = missing
        shooting = profile.get("shooting_percentages") or {}
        compact["shooting_percentages"] = [shooting.get(key) for key in ("field_goal", "three_point", "free_throw")]
        row = [compact.get(key) for key in history_fields]
        if leader:
            return {**{key: compact[key] for key in ("player_id", "player_id_namespace", "player_name", "team_id") if key in compact}, "history": row}
        return row

    for team in teams.values():
        if team.get("previous_team_top8_minutes"):
            team["previous_team_top8_minutes"] = [[entry.get(key) for key in top8_fields] for entry in team["previous_team_top8_minutes"]]
        if team.get("previous_season_players"):
            team.pop("historical_team_leaders", None)  # Previous top-eight membership is retained separately below.
        elif team.get("historical_team_leaders"):
            team["historical_team_leaders"] = [history(profile, leader=True) for profile in team["historical_team_leaders"]]
        continuity = team.get("continuity") or {}
        team["continuity"] = {key: continuity[key] for key in (
            "status", "reason", "retained_minutes_ratio", "top8_retained_count", "top8_retained_minutes_ratio",
            "roster_identity_coverage", "minute_rows_coverage", "roster_completeness_caveat",
            "retained_minutes_ratio_interpretation", "identity_crosswalk_unresolved_players",
        ) if key in continuity}
        players = []
        for profile in team.get("previous_season_players") or []:
            compact = {key: profile[key] for key in (
                "player_id", "source_player_id", "player_name",
                "previous_season_status", "previous_season_history_status", "current_availability",
            ) if key in profile}
            if profile.get("previous_season_history_reason"):
                compact["previous_season_history_reason"] = profile["previous_season_history_reason"]
            if profile.get("previous_season_history_status") == "available":
                compact.pop("previous_season_status", None)  # Current/other-team stints below already state membership.
            current = history(profile.get("current_team_last_season_stint"))
            previous = history(profile.get("previous_season_total"))
            stints = profile.get("previous_team_stints") or []
            same_team = bool(current and previous and len(stints) == 1
                             and stints[0].get("team_id") == profile.get("team_id"))
            if same_team:
                previous = {"same_as": "current_team_last_season_stint"}
            compact["current_team_last_season_stint"] = current
            compact["previous_season_total"] = previous
            if not same_team:
                compact["previous_team_stints"] = [
                    [stint.get("team_id"), stint.get("games_played")] for stint in stints
                ]
            if profile.get("replacement_observations"):
                compact["replacement_observations"] = [
                    [observation.get(key) for key in replacement_fields] for observation in profile["replacement_observations"]
                ]
            players.append(compact)
        if "previous_season_players" in team:
            team["previous_season_players"] = players
    if caveats:
        result["minutes_precision_caveats"] = sorted(caveats)
    result["replacement_observation_semantics"] = "descriptive historical association; not an injury cause or causal effect"
    result["column_definitions"] = {
        "history": list(history_fields),
        "previous_team_top8_minutes": list(top8_fields),
        "previous_team_stints": ["team_id", "games_played"],
        "summary": list(summary_fields),
        "per_observed_game": list(metrics),
        "observed_games_by_statistic": list(metrics),
        "missing_games_by_statistic": list(metrics),
        "shooting_percentages": ["field_goal", "three_point", "free_throw"],
        "minutes_precision": ["source_resolution", "observed_minute_rows"],
        "minutes_rank": ["within_team_rank", "rank_denominator"],
        "replacement_observations": list(replacement_fields),
    }
    result["id_namespaces"] = {"player_id": "NBA", "source_player_id": (result.get("coverage") or {}).get("provider")}
    result["context_projection"] = "compact descriptors; full historical packet retained in input_snapshot"
    return clean(result)


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
    if value.get("identity_preflight") is not None:
        result["identity_preflight"] = clean(value["identity_preflight"])
    if value.get("current_roster_identity_coverage") is not None:
        result["current_roster_identity_coverage"] = clean(value["current_roster_identity_coverage"])
    if value.get("player_knowledge_history_coverage") is not None:
        result["player_knowledge_history_coverage"] = clean(value["player_knowledge_history_coverage"])
    if value.get("player_knowledge") is not None:
        result["player_knowledge"] = _compact_player_knowledge(value["player_knowledge"])
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
        basis = (team or {}).get("roster_basis")
        official = (team or {}).get("official_team_roster")
        current = (team or {}).get("current_team_roster")
        legacy = (team or {}).get("roster")
        if official is not None and basis not in (None, "official_team_roster"):
            current, official = official, None
        elif official is None and current is None and legacy is not None:
            if basis in (None, "official_team_roster"):
                official = legacy
            else:
                current = legacy
        member_roster = official or current
        assignment = (team or {}).get("league_team_assignment")
        source_record = (team or {}).get("source_record") or {}
        players = [
            clean({key: player.get(key) for key in ("player_id", "name", "position", "number") if player.get(key) not in (None, "")})
            for player in ((member_roster or {}).get("players") or [])
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
        identity_coverage = (team or {}).get("current_roster_identity_coverage")
        history_coverage = (team or {}).get("player_knowledge_history_coverage")
        result["teams"][side] = clean({
            "team_id": (team or {}).get("team_id"),
            "roster_basis": (team or {}).get("roster_basis"),
            "official_team_roster": {
                "status": (official or {}).get("status"),
                "players": players,
                "player_count": len(players),
                "coverage": (official or {}).get("coverage"),
                "observed_at": (official or {}).get("observed_at"),
                "source": _source_summary((official or {}).get("source")),
            } if official else None,
            "current_team_roster": {
                "status": (current or {}).get("status"),
                "players": [clean({key: player.get(key) for key in ("player_id", "name", "position", "number")
                                    if player.get(key) not in (None, "")}) for player in (current or {}).get("players", [])],
                "player_count": len((current or {}).get("players") or []),
                "roster_basis": (team or {}).get("roster_basis"),
                "observed_at": (current or {}).get("observed_at"),
                "source": _source_summary((current or {}).get("source")),
            } if current else None,
            "current_roster_identity_coverage": identity_coverage,
            "player_knowledge_history_coverage": history_coverage,
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
        "game": clean({k: game[k] for k in ("game_id", "home", "away", "home_team_id", "away_team_id", "date", "tipoff", "scheduled_tipoff", "cutoff", "venue", "neutral_site", "game_type", "schedule_revision", "registered_game") if k in game}) if game else None,
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


def validate_judgment(judgment: AnalystJudgment | dict[str, Any], context: AnalystContext) -> AnalystJudgment:
    """Validate output shape, citation existence and numeric/policy boundaries."""
    value = AnalystJudgment.model_validate(judgment)
    if set(value.baseline_agreement) != {"pure", "market", "residual"}:
        raise ValueError("judgment_baseline_agreement_incomplete")
    allowed = {item["id"] for item in context.objective_evidence if item.get("id")}
    groups = (value.roster_analysis, value.player_analysis, value.cold_start_analysis,
              value.historical_data_relevance, value.key_matchup_variables, value.supporting_evidence,
              value.conflicting_evidence, value.uncertainty, value.watch_themes)
    claims = [claim for group in groups for claim in group]
    claims += [c for c in (value.pregame_story, value.prediction_perspective, value.central_thesis) if c is not None]
    referenced = (set(value.supporting_evidence_ids) | set(value.conflicting_evidence_ids)
        | {i for claim in claims for i in claim.evidence_ids}
        | set(value.final_prediction.evidence_ids if value.final_prediction else [])
        | set(value.analysis_article_evidence_ids))
    if not referenced <= allowed:
        raise ValueError("unknown_judgment_evidence")
    if set(value.supporting_evidence_ids) & set(value.conflicting_evidence_ids):
        raise ValueError("judgment_evidence_role_conflict")
    if len(value.domain_analysis_signals) != len(set(value.domain_analysis_signals)):
        raise ValueError("judgment_domain_signal_duplicate")
    product = "第25秒赛前" in (context.analysis_request or "")
    if product:
        if (value.pregame_story is None or value.final_prediction is None or not value.analysis_article
                or not 0 <= len(value.watch_themes) <= 4):
            raise ValueError("judgment_product_content_incomplete")
        if value.winner_lean != value.final_prediction.winner_side:
            raise ValueError("judgment_final_prediction_direction_mismatch")
        if any(abs(n * 2-round(n * 2)) > 1e-9 for n in
               (value.final_prediction.expected_margin, value.final_prediction.expected_total)):
            raise ValueError("judgment_final_prediction_not_half_point")
        if abs(value.final_prediction.win_probability*100-round(value.final_prediction.win_probability*100)) > 1e-9:
            raise ValueError("judgment_final_probability_not_whole_percent")
    # Product execution boundary, not a Chinese factuality classifier.
    text = json.dumps(value.model_dump(), ensure_ascii=False).casefold()
    if any(word in text for word in ("下注", "投注金额", "bankroll", "bet amount", "expected return", "自动投注")):
        raise ValueError("judgment_betting_execution_language")
    return value


class EvidenceBoundAnalyst:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        client=None,
        max_calls: int = 5,
        include_forecasting_skill: bool = False,
        forecast_client=None,
        reviewer_client=None,
        quality_review_enabled: bool = False,
    ):
        self.root = Path(root or BASE / "outputs/analyst_calls")
        self.client = client
        self.max_calls = min(max(int(max_calls), 1), 6)
        self.include_forecasting_skill = bool(include_forecasting_skill)
        self.forecast_client = forecast_client
        self.reviewer_client = reviewer_client
        self.quality_review_enabled = bool(quality_review_enabled or
            os.environ.get("COURTVISION_EDITORIAL_QUALITY_REVIEW", "").strip() == "1")

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
        legacy = 0
        for path in self.root.glob("*/attempt.json"):
            if json.loads(path.read_text(encoding="utf-8")).get("editorial_protocol") not in {
                    "grounded-editorial-v2", "editorial-publication-v1"}: legacy += 1
        return legacy + len(list(self.root.glob("*/forecast/*_request.json"))) + len(list(self.root.glob("*/editorial/*_request.json")))

    @staticmethod
    def _messages(
        context: AnalystContext,
    ) -> list[dict[str, str]]:
        from .editorial_skill import load_editorial_skill

        system = (
            "你是第25秒结构化分析输出器。使用本次提供的身份、cutoff和evidence，"
            "只返回调用方schema；引用ID必须属于本次输入，不伪造来源或修改Official数值。"
            "未提供模型解释不作归因。公开数字单位/方向遵守schema，不提供投注执行。"
            "objective_evidence 中 roster_context 行是对顶层 roster_context 的引用；球员身份与历史摘要以顶层冻结资料为准，并引用该行 evidence ID。"
        )
        system += "\n\n" + load_editorial_skill()
        prompt_context = context.model_dump()
        roster = prompt_context.get("roster_context") or {}
        for item in prompt_context.get("objective_evidence") or []:
            if item.get("section") == "roster_context":
                item["value"] = {"context_reference": "roster_context",
                    "context_hash": content_hash(roster), "game_id": roster.get("game_id"),
                    "cutoff": roster.get("cutoff")}
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(prompt_context, ensure_ascii=False, separators=(",", ":"))},
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
            market_review_possible = bool(market["evidence"]) or getattr(self, "market_collector", None) is not None
            # Reserve one additional V2 Independent call for the bounded
            # schema-only repair. It is used only after a valid-JSON/schema error.
            required_calls = 6 if market_review_possible else 4
            if self._used_calls() + required_calls > self.max_calls:
                raise ValueError("analyst_budget_cannot_cover_forecast_and_writer")
            locked_forecast = run_two_stage(basketball, market, folder / "forecast",
                client=self.forecast_client or self.client, removed_paths=removed,
                market_collector=getattr(self,'market_collector',None),market_guard=getattr(self,'market_guard',None),
                require_analysis_core=True)
        collection=(locked_forecast or{}).get('market_collection')
        if collection is not None and getattr(self,'market_guard',None) is not None:
            self.market_guard()
        final_frozen_at=None
        if collection is not None:
            final_frozen_at=now()
            context.game={**context.game,'cutoff':final_frozen_at}
            context.evidence_quality['public_market_reference_available']=bool(collection.get('evidence'))
            for item in collection.get('evidence',[]):
                context.objective_evidence.append({**item,'cutoff':final_frozen_at})
                context.evidence_index[item['id']]={'section':item['section'],'source_type':item.get('kind','MarketSnapshot')}
            context.context_hash=content_hash(context.model_dump(exclude={'context_hash'}))
            save(folder/'context_after_market.json',context.model_dump())
        if locked_forecast is not None:
            from .editorial_pipeline import run_editorial
            from .editorial_skill import load_editorial_skill
            save(folder / "attempt.json", {"created_at": now(), "context_hash": context.context_hash,
                "editorial_protocol": "editorial-publication-v1", "model": "deepseek-v4-flash",
                "skill_hashes": prompt_skill_hashes(load_editorial_skill()),
                "forecast_audit_path": locked_forecast["audit_path"], "forecast_skill_hashes": locked_forecast["skill_hashes"],
                "max_calls": self.max_calls, "automatic_retries": 0})
            try:
                judgment, trace = run_editorial(context, locked_forecast, folder / "editorial",
                    writer_client=self.client, reviewer_client=self.reviewer_client,
                    quality_review_enabled=self.quality_review_enabled,
                    max_calls=min(4, self.max_calls-self._used_calls()))
                saved_trace = {key: value for key, value in trace.items() if key != "spread_comparison"}
                path = save(folder / "response.json", {"received_at": now(), "judgment": judgment, **saved_trace})
                return AnalystJudgment.model_validate(judgment), {"status": "success", "path": str(path), **trace,
                    **({'market_collection':collection,'final_input_frozen_at':final_frozen_at} if collection is not None else {}),
                    "forecast_audit_path": locked_forecast["audit_path"], "forecast_skill_hashes": locked_forecast["skill_hashes"],
                    "schema_repair_provenance": locked_forecast.get("schema_repair_provenance"),
                    "spread_comparison": locked_forecast.get("spread_comparison")}
            except Exception as exc:
                save(folder / "failure.json", {"failed_at": now(), "error_type": type(exc).__name__, "message": str(exc)[:10000]})
                raise
        # Legacy non-Forecasting callers retain schema compatibility, but a new
        # version still requires the separate factual publication receipt.
        locked_numbers = None
        from .editorial_contract import build_editorial_contract
        editorial_contract = build_editorial_contract(context, locked_numbers)
        messages[0]["content"] += "\n\n本次可写事实协议（仅作来源/类型/身份约束）：\n" + json.dumps(editorial_contract, ensure_ascii=False)
        output_schema = (
            ProductAnalystOutput
            if "第25秒赛前" in (context.analysis_request or "")
            else AnalystJudgment
        )
        def parse_output(payload):
            value = output_schema.model_validate(payload).model_dump()
            return AnalystJudgment.model_validate(value)
        writer_token_budget = 6400 if (context.game or {}).get("registered_game") else 4800
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
                "max_output_tokens": writer_token_budget,
                "output_schema": output_schema.__name__,
                "correction_of": _correction.get("path") if _correction else None,
                "forecast_audit_path": locked_forecast["audit_path"] if locked_forecast else None,
                "forecast_skill_hashes": locked_forecast["skill_hashes"] if locked_forecast else {},
            },
        )
        judgment = None
        reply_start = time.perf_counter()
        save(folder / "writer_request.json", {"created_at": now(), "model": "deepseek-v4-flash",
            "messages": messages, "schema": output_schema.model_json_schema(), "context_hash": context.context_hash,
            "input_characters": sum(len(message["content"]) for message in messages), "automatic_retries": 0})
        try:
            if self.client is not None:
                payload, usage = self.client(messages, output_schema)
                save(folder / "writer_raw_response.json", {"received_at": now(), "raw": clean(payload), "usage": clean(usage),
                    "latency_seconds": time.perf_counter()-reply_start})
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
                    max_tokens=writer_token_budget,
                    temperature=0,
                    use_responses_api=False,
                    disabled_params={"parallel_tool_calls": None},
                    extra_body={"thinking": {"type": "disabled"}, "max_tokens": writer_token_budget},
                )
                structured = llm.with_structured_output(
                    output_schema, method="function_calling", include_raw=True
                )
                result = structured.invoke(messages)
                raw_message = result.get("raw")
                save(folder / "writer_raw_response.json", {"received_at": now(),
                    "raw": raw_message.model_dump(mode="json") if raw_message else None,
                    "usage": (getattr(raw_message, "usage_metadata", None) or (raw_message.response_metadata or {}).get("token_usage", {})) if raw_message else {},
                    "latency_seconds": time.perf_counter()-reply_start})
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
            from .editorial_contract import validate_editorial_boundaries
            editorial_validation = validate_editorial_boundaries(judgment, context, locked_numbers=locked_numbers)
            judgment = validate_judgment(judgment, context)
            save(folder / "editorial_validation.json", editorial_validation)
            response = {
                "received_at": now(),
                "context_hash": context.context_hash,
                "judgment": judgment.model_dump(),
                "usage": clean(usage or {}),
                "raw": raw,
                "structured_repairs": structured_repairs if self.client is None else [],
                "style_repairs": style_repairs,
                "forecast_workflow": locked_forecast,
                "editorial_validation": editorial_validation,
                "editorial_contract": editorial_contract,
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
                "editorial_validation": editorial_validation,
                "editorial_contract": editorial_contract,
            }
        except Exception as exc:
            key = os.environ.get("DEEPSEEK_API_KEY", "")
            message = str(exc).replace(key, "[REDACTED]")[:600] if key else str(exc)[:600]
            validator_errors = getattr(exc, "errors", None)
            if not isinstance(validator_errors, list):
                validator_errors = [{"code": str(exc), "statement": None}]
            save(
                folder / "failure.json",
                {
                    "failed_at": now(),
                    "context_hash": context.context_hash,
                    "error_type": type(exc).__name__,
                    "message": message,
                    "validator_errors": validator_errors,
                },
            )
            raise
