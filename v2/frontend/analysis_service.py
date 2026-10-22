"""On-demand analyst orchestration over the saved product snapshot.

This layer never recomputes or mutates numeric forecasts. It turns the exact
saved display snapshot into evidence, optionally adds cutoff-safe web facts,
then delegates language synthesis to the existing EvidenceBoundAnalyst.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from v2.pregame_system.analyst import EvidenceBoundAnalyst, build_analyst_context
from v2.pregame_system.storage import clean, content_hash, now, digest
from v2.pregame_system.web_search import (
    PregameWebSearch,
    is_allowed_public_market_source,
    select_public_market_reference,
)
from v2.pregame_system.web_research_agent import AutonomousWebResearcher


DEFAULT_PUBLIC_MARKET_CACHE = Path(__file__).parent / "data" / "public_market_references.json"


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _strict_source_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None or "T" not in value:
        return None
    return parsed.astimezone(timezone.utc)


def _historical_value_in_scope(
    value: Any,
    cutoff: str,
    *declared_cutoffs: Any,
) -> bool:
    if value in (None, {}, []):
        return True
    cutoff_dt = _instant(cutoff)
    markers = [marker for marker in declared_cutoffs if marker is not None]
    if not markers:
        return False
    for marker in markers:
        timestamp = _strict_source_instant(marker)
        if timestamp is None or timestamp > cutoff_dt:
            return False

    def future_source_time(item: Any) -> bool:
        if isinstance(item, dict):
            for key in ("captured_at", "published_at", "source_published_at"):
                raw = item.get(key)
                if raw is None:
                    continue
                timestamp = _strict_source_instant(raw)
                if timestamp is None or timestamp > cutoff_dt:
                    return True
            return any(future_source_time(child) for child in item.values())
        if isinstance(item, list):
            return any(future_source_time(child) for child in item)
        return False

    return not future_source_time(value)


def historical_game_context(game: dict[str, Any], cutoff: str) -> dict[str, Any]:
    """Drop cached context without a cutoff at or before a historical replay."""
    safe_game = dict(game)
    missing = list(game.get("missing") or [])
    technical = game.get("technical") or {}

    roster = game.get("roster_context")
    roster_markers = [
        technical.get("roster_context_cutoff"),
        (roster or {}).get("context_cutoff"),
    ]
    if roster and not _historical_value_in_scope(roster, cutoff, *roster_markers):
        safe_game["roster_context"] = None
        missing.append({"section": "roster_context", "reason": "历史回放缺少赛前名单快照证明"})

    for section in ("matchup_history", "season_form"):
        value = game.get(section)
        if value and not _historical_value_in_scope(value, cutoff, value.get("cutoff")):
            safe_game[section] = None
            missing.append({"section": section, "reason": "保存的上下文截止时间晚于历史回放"})

    background = dict(game.get("background") or {})
    for section in ("recent_form", "schedule"):
        value = background.get(section)
        if value:
            background[section] = None
            missing.append({"section": section, "reason": "历史回放中没有可验证的赛前来源时间"})

    availability = background.get("availability")
    if availability and availability.get("status") == "available":
        declared = availability.get("source_published_at") or availability.get("published_at")
        if not _historical_value_in_scope(
            availability,
            cutoff,
            declared,
            availability.get("captured_at"),
        ):
            background["availability"] = {"status": "unavailable"}
            missing.append({"section": "availability", "reason": "历史回放缺少合格的发布时间和采集时间"})

    market = background.get("market")
    if market:
        quote = market.get("as_of_cutoff") or market
        if not _historical_value_in_scope(
            market,
            cutoff,
            quote.get("captured_at"),
            quote.get("source_published_at"),
        ):
            background["market"] = None
            missing.append({"section": "market", "reason": "历史回放缺少合格的盘口采集时间"})

    safe_game["background"] = background
    safe_game["missing"] = missing
    if safe_game.get("season_form") is None:
        safe_game["season_phase"] = None
    return safe_game


def analysis_cutoff(game: dict[str, Any], current: datetime | None = None) -> str:
    current = (current or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tipoff = _instant(game["tipoff"])
    if game.get("status") == "scheduled":
        return min(current, tipoff - timedelta(minutes=5)).isoformat()
    saved = (game.get("technical") or {}).get("cutoff")
    if not saved:
        raise ValueError("historical_analysis_cutoff_missing")
    return _instant(saved).isoformat()


def game_identity(game: dict[str, Any]) -> dict[str, Any]:
    return {
        "game_id": game["id"],
        "scheduled_tipoff": game["tipoff"],
        "home_team_id": game["home"]["id"],
        "away_team_id": game["away"]["id"],
        "home_team": game["home"]["name"],
        "away_team": game["away"]["name"],
        "status": game.get("status"),
        "registered_game": bool(game.get("registered_game")),
        "game_type": game.get("game_type"),
        "season_start": game.get("season_start", _instant(game["tipoff"]).year),
        "research_stage": (game.get("generation_context") or {}).get("stage") or "EARLY_PREVIEW",
        **{key: clean(game[key]) for key in ("venue", "neutral_site", "schedule_revision") if key in game},
    }


def search_categories(game: dict[str, Any]) -> set[str]:
    categories: set[str] = {"game_story"}
    if game.get("registered_game") and game.get("status") == "scheduled":
        categories.update({"availability", "lineup", "roster_news"})
    if (game.get("background") or {}).get("availability", {}).get("status") != "available":
        categories.add("availability")
    teams = ((game.get("roster_context") or {}).get("teams") or {}).values()
    teams = list(teams)
    if any(not (team.get("confirmed_starters") or team.get("projected_starters")) for team in teams):
        categories.add("lineup")
    if any((team.get("cold_start") or {}).get("cold_start") is True for team in teams):
        categories.add("roster_news")
    if not (game.get("background") or {}).get("market") and not (game.get("relationship") or {}).get("market"):
        categories.add("market")
    return categories


def research_local_context(game: dict[str, Any]) -> dict[str, Any]:
    history = game.get("matchup_history") or {}
    form = game.get("season_form") or {}
    rosters = ((game.get("roster_context") or {}).get("teams") or {})
    player_knowledge = (game.get("roster_context") or {}).get("player_knowledge") or {}
    return {
        "matchup_history": {
            "status": history.get("status"),
            "matchup_relevance": history.get("matchup_relevance"),
            "most_recent_meeting": history.get("most_recent_meeting"),
            "series_summary": history.get("series_summary"),
        },
        "season_phase": game.get("season_phase"),
        "season_form": {
            "status": form.get("status"),
            "teams": {
                side: {
                    "current_season_games": ((form.get("teams") or {}).get(side) or {}).get("current_season_games"),
                    "current_last10": ((form.get("teams") or {}).get(side) or {}).get("current_last10"),
                    "previous_last10": ((form.get("teams") or {}).get(side) or {}).get("previous_last10"),
                    "previous_playoffs": ((form.get("teams") or {}).get(side) or {}).get("previous_playoffs"),
                }
                for side in ("away", "home")
            },
        },
        "current_roster_names": {
            side: [
                player["name"]
                for player in (((rosters.get(side) or {}).get("official_team_roster")
                                or (rosters.get(side) or {}).get("current_team_roster")
                                or (rosters.get(side) or {}).get("roster") or {}).get("players") or [])
                if player.get("name")
            ]
            for side in ("away", "home")
        },
        "current_roster_identity_coverage": (game.get("roster_context") or {}).get("current_roster_identity_coverage"),
        "player_knowledge_history_coverage": {
            side: {"scope": "historical_player_record_match_only", **{
                key: ((player_knowledge.get("teams") or {}).get(side) or {}).get(key)
                for key in ("current_roster_player_count", "current_roster_match_count",
                            "current_roster_identity_unresolved_count", "players_with_previous_season_stats")
            }}
            for side in ("away", "home")
        },
        "freshness_check_required": bool(game.get("registered_game") and game.get("status") == "scheduled"),
        "game_type": game.get("game_type"),
    }


def _same_instant(left: str, right: str) -> bool:
    try:
        return _instant(left) == _instant(right)
    except (TypeError, ValueError):
        return False


def cached_public_market_context(
    game: dict[str, Any],
    path: str | Path = DEFAULT_PUBLIC_MARKET_CACHE,
    *,
    cutoff: str | None = None,
) -> dict[str, Any] | None:
    """Load one identity-checked, display-only public market reference."""
    if (game.get("background") or {}).get("market") or (game.get("relationship") or {}).get("market"):
        return None
    cache_path = Path(path)
    if not cache_path.is_file():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for record in payload.get("records") or []:
        identity = record.get("game") or {}
        if record.get("game_id") != game.get("id"):
            continue
        if identity.get("home_team_id") != (game.get("home") or {}).get("id"):
            continue
        if identity.get("away_team_id") != (game.get("away") or {}).get("id"):
            continue
        if not _same_instant(str(identity.get("scheduled_tipoff") or ""), str(game.get("tipoff") or "")):
            continue
        reference = record.get("market_reference") or {}
        if cutoff is not None:
            cutoff_dt = _instant(cutoff)
            captured_dt = _strict_source_instant(reference.get("captured_at"))
            published_raw = reference.get("published_at")
            published_dt = _strict_source_instant(published_raw) if published_raw else None
            if captured_dt is None or captured_dt > cutoff_dt:
                continue
            if published_raw and (published_dt is None or published_dt > cutoff_dt):
                continue
        if reference.get("usage") != "display_only_public_market_reference":
            continue
        if reference.get("eligible_prediction_input") is not False:
            continue
        if not is_allowed_public_market_source(str(reference.get("source_url") or "")):
            continue
        home_spread = reference.get("home_spread")
        away_spread = reference.get("away_spread")
        if home_spread is not None and away_spread is not None:
            try:
                if abs(float(home_spread) + float(away_spread)) > 0.01:
                    continue
            except (TypeError, ValueError):
                continue
        evidence_id = str(record.get("id") or content_hash(record))
        cached_record = {
            "id": evidence_id,
            "category": "market",
            "title": reference.get("source_title") or "公开市场参考",
            "url": reference["source_url"],
            "published_at": reference.get("published_at"),
            "captured_at": reference.get("captured_at"),
            "snippet": record.get("snippet") or "",
            "score": record.get("score"),
            "eligible_for_fact": True,
            "market_reference": {**reference, "evidence_id": evidence_id},
        }
        return {
            "status": "available",
            "game_id": game.get("id"),
            "captured_at": reference.get("captured_at"),
            "records": [cached_record],
            "trace": [{"category": "market", "source": "verified_cache", "accepted": 1}],
            "policy": "identity-checked display-only public market cache",
        }
    return None


def merge_cached_public_market(
    web_context: dict[str, Any] | None,
    game: dict[str, Any],
    path: str | Path = DEFAULT_PUBLIC_MARKET_CACHE,
    *,
    cutoff: str | None = None,
) -> dict[str, Any] | None:
    """Add a verified cached quote only when no web quote was accepted."""
    if select_public_market_reference(web_context):
        return web_context
    cached = cached_public_market_context(game, path, cutoff=cutoff)
    if cached is None:
        return web_context
    if web_context is None:
        return cached
    return {
        **web_context,
        "status": "available",
        "records": [*(web_context.get("records") or []), *cached["records"]],
        "trace": [*(web_context.get("trace") or []), *cached["trace"]],
    }


def _independent_prediction(game: dict[str, Any]) -> dict[str, Any]:
    relationship = game["relationship"]
    provenance = relationship.get("basketball_provenance") or {}
    legacy_pure = ((game.get("technical") or {}).get("prediction_method") == "pure_basketball_fallback"
                   and relationship.get("market") is None and relationship.get("residual") is None)
    if provenance.get("market_independent") is True or (not provenance and legacy_pure):
        return clean(relationship.get("basketball") or {"status": "unavailable"})
    return {"status": "unavailable", "reason": "Saved model output lacks independent-baseline provenance."}


def _availability_as_of(game: dict[str, Any], cutoff: str) -> dict[str, Any]:
    """Qualify shared availability before both player matching and general evidence."""
    raw = (game.get("background") or {}).get("availability") or {}
    cutoff_at = _instant(cutoff)
    team_ids = {str((game.get(side) or {}).get("id")) for side in ("home", "away")}
    source = raw.get("source") or {}
    defaults = {
        "source_url": raw.get("source_url") or raw.get("url") or source.get("source_url") or source.get("url"),
        "source_published_at": raw.get("source_published_at") or raw.get("published_at") or raw.get("source_published_at_utc")
                               or source.get("source_published_at") or source.get("published_at"),
        "captured_at": raw.get("captured_at") or raw.get("captured_at_utc") or source.get("captured_at") or source.get("captured_at_utc"),
        "source_cutoff": raw.get("source_cutoff") or raw.get("cutoff_time_utc") or raw.get("cutoff_time") or raw.get("cutoff"),
        "raw_path": raw.get("raw_path") or source.get("raw_path"),
        "raw_sha256": raw.get("raw_sha256") or source.get("raw_sha256"),
    }
    valid = []
    if raw.get("status") == "available":
        for row in raw.get("records") or []:
            if not isinstance(row, dict) or str(row.get("team_id")) not in team_ids:
                continue
            if row.get("game_id") and row["game_id"] != game.get("id"):
                continue
            record = {**defaults, **row}
            published = _strict_source_instant(record.get("source_published_at") or record.get("published_at"))
            captured = _strict_source_instant(record.get("captured_at"))
            if published is None or captured is None:
                continue
            if published > cutoff_at or captured > cutoff_at:
                continue
            valid.append(record)
    return {
        "status": "available" if valid else "unavailable", "records": valid,
        "message": f"已连接 {len(valid)} 条合格球员状态。" if valid else "暂无可靠伤病与出战信息。",
        "time_validation": {"cutoff": cutoff, "accepted_records": len(valid),
                            "rejected_records": len(raw.get("records") or []) - len(valid)},
    }


def _scope_roster_inputs(game: dict[str, Any], cutoff: str) -> None:
    """Do not expose wrong-team or future roster names to Research or the writer."""
    context = game.get("roster_context") or {}
    if context.get("teams"):
        context["teams"] = {side: team for side, team in context["teams"].items() if side in {"home", "away"}}
    for side, team in (context.get("teams") or {}).items():
        team = team or {}
        expected = (game.get(side) or {}).get("id") or game.get(f"{side}_team_id")
        official = team.get("official_team_roster") or team.get("roster")
        if not official:
            continue
        reason = None
        try:
            if official.get("status") != "available":
                reason = "official_team_roster_unavailable"
            elif expected is not None and any(int(value) != int(expected) for value in (
                team.get("team_id"), official.get("team_id"),
            ) if value is not None):
                reason = "roster_team_identity_mismatch"
            elif _strict_source_instant((official.get("source") or {}).get("captured_at")) is None:
                reason = "roster_identity_or_capture_time_unverified"
            elif _strict_source_instant((official.get("source") or {}).get("captured_at")) > _instant(cutoff):
                reason = "roster_captured_after_cutoff"
        except (TypeError, ValueError):
            reason = "roster_identity_or_capture_time_unverified"
        if reason:
            context["teams"][side] = {
                "team_id": expected, "roster_basis": "unavailable", "cold_start": team.get("cold_start"),
                "official_team_roster": {"status": "unavailable", "players": [], "reason": reason},
                "continuity": {"status": "unavailable", "reason": reason}, "missing": [reason],
            }


def _evidence(
    game: dict[str, Any],
    cutoff: str,
    web_context: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    opening_night = (game.get("season_phase") or {}).get("phase") == "opening_night"
    from v2.pregame_system.fact_context import valid_fact_context
    fact_context = valid_fact_context(
        game.get("fact_context"), game["id"], cutoff, game["tipoff"],
    )
    recent_form = deepcopy((game.get("background") or {}).get("recent_form"))
    if isinstance(recent_form, dict) and fact_context:
        for side in ("home", "away"):
            metrics = ((fact_context.get("team_form") or {}).get(side) or {}).get("metrics") or {}
            team_values = recent_form.get(side)
            if not isinstance(team_values, dict):
                continue
            for metric, details in metrics.items():
                if isinstance(details, dict) and details.get("validation") == "mismatch":
                    team_values.pop(metric, None)
    schedule = (fact_context or {}).get("schedule") or {
        side: {"status": "unavailable", "reason": "no_verified_fact_context",
               "basis": None, "back_to_back": None, "source_refs": []}
        for side in ("home", "away")
    }
    season_phase = dict(game.get("season_phase") or {})
    if opening_night:
        # Zero rows in this local history do not prove an official team or
        # league opener. Keep the sample signal without making that claim.
        season_phase.update(
            phase="very_early", display_label="当前赛季本地样本为零",
            primary_evidence="no_local_current_season_rows; league_opener_unverified",
            global_opening_night_confirmed=False,
        )
    sections = {
        "game_identity": game_identity(game),
        "model_facts": {"model_version": (game.get("technical") or {}).get("model_version"),
            "runtime_asset_identity": {key: value for key, value in ((game.get("technical") or {}).get("runtime_asset_identity") or {}).items()
                if key in {"runtime_mode", "model_version", "training_cutoff", "asset_identity_hash", "metadata_sha256", "model_hashes"}},
            "model_explanation": {"status": "unavailable"}},
        "prediction": {
            "pure_prediction": _independent_prediction(game),
            "market_benchmark": game["relationship"].get("market"),
            "divergence": game["relationship"].get("divergence"),
            "residual_prediction": game["relationship"].get("residual"),
            "official_numeric_prediction": game["relationship"]["final"],
        },
        "team_form": recent_form,
        "matchup": {
            "schedule": schedule,
        },
        "fact_context": fact_context,
        "roster_context": game.get("roster_context"),
        "matchup_history": game.get("matchup_history"),
        "season_form": game.get("season_form"),
        "season_phase": season_phase,
        "market": (game.get("background") or {}).get("market"),
    }
    availability = _availability_as_of(game, cutoff)
    if not game.get("registered_game") and not game.get("venue"):
        sections.pop("game_identity")  # Legacy contexts already carry their identity; no invented venue.
    if not ((game.get("technical") or {}).get("runtime_asset_identity")):
        sections.pop("model_facts")  # Do not manufacture training metadata for legacy saved reports.
    if availability.get("status") == "available":
        sections["availability"] = availability
    evidence: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = list(game.get("missing") or [])
    if fact_context is None:
        missing.append({"section": "fact_context", "reason": "no frozen context with matching identity and cutoff"})
    for section, value in sections.items():
        if value in (None, {}, []):
            continue
        evidence_id = content_hash(
            {"game_id": game["id"], "cutoff": cutoff, "section": section, "value": value}
        )
        evidence.append(
            {
                "id": evidence_id,
                "kind": "ModelForecast" if section == "prediction" else ("Context" if section == "fact_context" else "Fact"),
                "section": section,
                "game_id": game["id"],
                "cutoff": cutoff,
                "value": clean(value),
            }
        )
    for record in (web_context or {}).get("records") or []:
        if not record.get("eligible_for_fact"):
            continue
        evidence.append(
            {
                "id": record["id"],
                "kind": "Fact",
                "section": "web_context",
                "game_id": game["id"],
                "cutoff": cutoff,
                "value": clean(record),
            }
        )
        sources.append(
            {
                "id": record["id"],
                "title": record["title"],
                "url": record["url"],
                "published_at": record["published_at"],
                "captured_at": record.get("captured_at"),
                "category": record["category"],
                "source_id": record.get("source_id"),
                "claim_type": record.get("claim_type"),
                "publication_precision": record.get("publication_precision"),
                "temporal_confidence": record.get("temporal_confidence"),
                "verified_by": record.get("verified_by"),
                "discovered_via": record.get("discovered_via"),
            }
        )
    if not (game.get("background") or {}).get("market"):
        missing.append({
            "section": "market",
            "reason": (
                "结构化市场数据缺失；联网公开盘口仅供展示，未进入正式预测"
                if select_public_market_reference(web_context)
                else "暂无赛前市场数据"
            ),
        })
    if availability.get("status") != "available":
        missing.append({"section": "availability", "reason": "暂无可靠伤病与出战信息"})
    return evidence, sources, missing


def prediction_layers(game: dict[str, Any]) -> dict[str, Any]:
    relationship = game["relationship"]
    return {
        "pure_prediction": _independent_prediction(game),
        "market_benchmark": clean(relationship.get("market")),
        "divergence": clean(relationship.get("divergence")),
        "residual_prediction": clean(relationship.get("residual")),
        "official_numeric_prediction": clean(relationship["final"]),
    }


def analysis_base_fingerprint(game: dict[str, Any], cutoff: str) -> str:
    """Fingerprint local analysis inputs so stale saved reports are rejected."""
    evidence, _, missing = _evidence(game, cutoff, None)
    return content_hash({
        "game_identity": game_identity(game),
        "evidence": evidence,
        "missing_data": missing,
    })


def judgment_evidence_ids(judgment: dict[str, Any]) -> set[str]:
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("evidence_ids") and isinstance(item, list):
                    found.update(str(current) for current in item if current)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(judgment)
    return found


class SavedGameAnalysisService:
    def __init__(
        self,
        data: dict[str, Any],
        *,
        analyst: EvidenceBoundAnalyst | None = None,
        display_name_store=None,
        market_collector=None,
        market_reference_store=None,
        searcher: PregameWebSearch | None = None,
        researcher: Any | None = None,
        player_knowledge_store: Any | None = None,
        require_research_success: bool = False,
    ):
        self.data = data
        self.analyst = analyst or EvidenceBoundAnalyst(include_forecasting_skill=True, max_calls=6)
        self.display_name_store=display_name_store
        self.searcher = searcher
        self.researcher = researcher
        self.player_knowledge_store = player_knowledge_store
        self.require_research_success = require_research_success
        self.market_collector=market_collector
        self.market_reference_store=market_reference_store
        if self.market_collector is None and isinstance(self.analyst,EvidenceBoundAnalyst) and self.analyst.include_forecasting_skill and self.analyst.client is None and self.analyst.forecast_client is None:
            from v2.pregame_system.market_collector import BoundedMarketCollector
            self.market_collector=BoundedMarketCollector()

    def analyze(
        self,
        game_id: str,
        *,
        allow_lookup: bool = True,
        cutoff: str | None = None,
        before_writer: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        original_game = next((item for item in self.data.get("games", []) if item.get("id") == game_id), None)
        if original_game is None:
            raise KeyError("unknown_game")
        cutoff = analysis_cutoff(original_game) if cutoff is None else _instant(cutoff).isoformat()
        if _instant(cutoff) >= _instant(original_game["tipoff"]):
            raise ValueError("cutoff_not_pregame")
        replay = original_game.get("status") != "scheduled"
        game = deepcopy(original_game)
        if replay:
            game = historical_game_context(game, cutoff)
        _scope_roster_inputs(game, cutoff)
        game.setdefault("background", {})["availability"] = _availability_as_of(game, cutoff)
        self._attach_player_knowledge(game, cutoff)
        web_context = None
        research_trace = None
        search_trace = {"status": "not_requested", "categories": []}
        categories = search_categories(game)
        if allow_lookup and categories:
            try:
                searcher = self.searcher or PregameWebSearch()
                researcher = self.researcher
                if researcher is None and os.environ.get("DEEPSEEK_API_KEY"):
                    researcher = AutonomousWebResearcher(searcher=searcher)
                if researcher is not None:
                    web_context, research_trace = researcher.investigate(
                        game_identity(game),
                        cutoff,
                        replay=replay,
                        categories=categories,
                        local_context=research_local_context(game),
                    )
                else:
                    web_context = searcher.search(
                        game_identity(game),
                        cutoff,
                        replay=replay,
                        categories=categories,
                    )
                search_trace = {
                    "status": web_context["status"],
                    "categories": sorted(categories),
                    "records": len(web_context.get("records") or []),
                    "mode": "deepseek_tool_loop" if research_trace else "deterministic_fallback",
                    "tool_trace": research_trace,
                }
            except Exception as exc:
                if self.require_research_success:
                    raise
                try:
                    searcher = self.searcher or PregameWebSearch()
                    web_context = searcher.search(
                        game_identity(game),
                        cutoff,
                        replay=replay,
                        categories=categories,
                    )
                    search_trace = {
                        "status": web_context["status"],
                        "categories": sorted(categories),
                        "records": len(web_context.get("records") or []),
                        "mode": "deterministic_fallback",
                        "research_error_type": type(exc).__name__,
                    }
                except Exception as fallback_exc:
                    search_trace = {
                        "status": "unavailable",
                        "categories": sorted(categories),
                        "error_type": type(fallback_exc).__name__,
                        "research_error_type": type(exc).__name__,
                    }
        if game.get("registered_game") and not replay and allow_lookup:
            # Research started with a publication as-of bound; acquisition itself is later.
            # Freeze the actual acquired input, never backdate captured_at to requested_at.
            search_trace["research_requested_cutoff"] = cutoff
            cutoff = now()
            search_trace["input_cutoff_semantics"] = "actual post-research input freeze; publication searched as of request"
        minimum = (web_context or {}).get("minimum_research")
        if game.get("registered_game") and minimum is not None:
            if not minimum.get("sufficient"):
                raise ValueError("research_minimum_not_met:" + json.dumps(minimum.get("failure_detail"), ensure_ascii=False))
            # Real capture is not backdated. Validate it against the acquired
            # input cutoff; publication is additionally bounded by requested cutoff.
            for record in (web_context or {}).get("records", []):
                if record.get("eligible_for_fact"):
                    captured = _strict_source_instant(record.get("captured_at"))
                    if captured is None or captured > _instant(cutoff):
                        raise ValueError("research_capture_outside_frozen_cutoff")
        elif self.require_research_success and (not research_trace or research_trace.get("status") != "success"
                or not any(r.get("eligible_for_fact") and r.get("temporal_scope") == "current_pregame"
                           for r in (web_context or {}).get("records", []))):
            raise ValueError("research_minimum_not_met:missing_verified_claim_package")
        web_context = merge_cached_public_market(web_context, game, cutoff=cutoff)
        if select_public_market_reference(web_context):
            search_trace["public_market_reference"] = "available"
        evidence, sources, missing = _evidence(game, cutoff, web_context)
        if minimum:
            # UNKNOWN is metadata, not an availability/lineup fact.
            evidence.append({"id": content_hash(minimum), "kind": "Context", "section": "research_status",
                             "game_id": game_id, "cutoff": cutoff, "value": clean(minimum)})
            # Existing forecasting projection already carries missing_data.
            # Keep UNKNOWN visible without modifying its numeric methodology.
            missing.extend({"section": "research." + kind, "status": "UNKNOWN",
                "reason": status["unknown_reason"], "last_checked": status["last_checked"],
                "sources_checked": status["sources_checked"]}
                for kind, status in minimum["coverage"].items()
                if status["status"] == "UNKNOWN" and kind != "market_reference")
        public_market = select_public_market_reference(web_context)
        players=[p for t in (game.get('roster_context')or{}).get('teams',{}).values()
            for p in (t.get('official_team_roster')or t.get('current_team_roster')or t.get('roster')or{}).get('players',[])]
        if players and self.display_name_store is not None:
            game['player_display_names']=self.display_name_store.ensure(players)['names']
        matchup = f"{game['away']['name_zh']}对{game['home']['name_zh']}"
        if self.display_name_store is not None and hasattr(self.display_name_store, 'versions'):
            from .player_display_names import catalog
            naming_rows = catalog(self.display_name_store.versions)
            naming_context = json.dumps({'roster': game.get('roster_context'),
                'evidence': [e for e in evidence if e.get('section') != 'market'
                    and (e.get('value') or {}).get('category') != 'market']}, ensure_ascii=False).casefold()
            game['player_identity_lexicon'] = [{k: row[k] for k in ('canonical_name_en','display_name_zh')}
                for row in naming_rows if row.get('canonical_name_en','').casefold() in naming_context]
        context = build_analyst_context(
            prediction_layers(game),
            evidence,
            missing_data=missing,
            game={"game_id": game_id, "home": game["home"], "away": game["away"],
                  "scheduled_tipoff": game.get("tipoff"), "cutoff": cutoff,
                  **{key: clean(game[key]) for key in ("venue", "neutral_site", "schedule_revision", "game_type", "registered_game", "season_event", "player_display_names", "player_identity_lexicon") if key in game}},
            analysis_request=(
                f"生成{matchup}的第25秒赛前分析。使用本次 AnalystContext 与 objective_evidence，"
                "生成 final_prediction，并严格遵守 ProductAnalystOutput schema、字段格式和 evidence ID 协议。"
            ),
        )
        input_frozen_at = now()
        if before_writer is not None:
            before_writer()
        if self.market_collector is not None:
            def collect_market(game_context):
                result=self.market_collector.collect(game_context)
                if self.market_reference_store is not None and result.get('public_market_reference'):
                    from v2.pregame_system.market_collector import market_game
                    self.market_reference_store.save(market_game(game_context),result['public_market_reference'])
                return result
            self.analyst.market_collector=collect_market
            self.analyst.market_guard=before_writer
        judgment, analyst_trace = self.analyst.judge(context)
        collection=analyst_trace.get('market_collection')
        if collection is not None:
            search_trace['input_cutoff_semantics']='actual post-research and post-market input freeze'
            cutoff=context.game['cutoff']
            input_frozen_at=analyst_trace['final_input_frozen_at']
            evidence=context.objective_evidence
            sources.extend(s for s in collection.get('sources',[])if s['id']not in {old['id']for old in sources})
            public_market=collection.get('public_market_reference') or public_market
            game['public_market_reference']=public_market
        judgment_payload = judgment.model_dump()
        cited = judgment_evidence_ids(judgment_payload)
        return {
            "status": "success",
            "analysis_contract_version": "two-stage-forecast-v1" if analyst_trace.get("forecast_audit_path") else "matchup-history-v1",
            "game_id": game_id,
            "game_identity": game_identity(game),
            "cutoff": cutoff,
            "input_frozen_at": input_frozen_at,
            "generated_at": now(),
            "base_input_hash": analysis_base_fingerprint(game, cutoff),
            "judgment": judgment_payload,
            "context_hash": context.context_hash,
            "evidence_index": context.evidence_index,
            "input_snapshot": clean(game),
            "model_layers": prediction_layers(game),
            "analyst_context": context.model_dump(),
            "objective_evidence": clean(evidence),
            "research": clean(web_context),
            "official_numeric_prediction": clean(game["relationship"]["final"]),
            "public_market_reference": public_market,
            "market_collection": collection,
            "spread_comparison": analyst_trace.get("spread_comparison"),
            "sources": clean(sources),
            "cited_sources": [source for source in sources if source["id"] in cited],
            "provenance": {
                "analyst_model": ((self._read_attempt(analyst_trace.get("path")) or {}).get("model")),
                "researcher_model": ((self._read_attempt((research_trace or {}).get("path")) or {}).get("planner_model")),
                "analyst_artifact": self._artifact_receipt(analyst_trace.get("path")),
                "forecast_artifact": self._artifact_receipt(analyst_trace.get("forecast_audit_path")),
                "research_artifact": self._artifact_receipt((research_trace or {}).get("path")),
                "skill_hashes": {
                    **self._attempt_skill_hashes((research_trace or {}).get("path")),
                    **self._attempt_skill_hashes(analyst_trace.get("path")),
                    **(analyst_trace.get("forecast_skill_hashes") or {}),
                },
                "skill_provenance_status": {
                    "research": (
                        "captured_from_actual_prompt"
                        if "pregame_research" in self._attempt_skill_hashes((research_trace or {}).get("path"))
                        else "not_used"
                    ),
                    "forecasting": (
                        "captured_from_actual_prompt"
                        if "forecasting" in (analyst_trace.get("forecast_skill_hashes") or self._attempt_skill_hashes(analyst_trace.get("path")))
                        else "not_used"
                    ),
                    "editorial": (
                        "captured_from_actual_prompt"
                        if "editorial" in self._attempt_skill_hashes(analyst_trace.get("path"))
                        else "not_used"
                    ),
                },
            },
            "search": search_trace,
            "analyst_trace": {key: value for key, value in analyst_trace.items()
                              if key != "spread_comparison"},
            "publication_receipt": analyst_trace.get("publication_receipt"),
            "editorial_projection": analyst_trace.get("editorial_projection"),
            "editorial_material": analyst_trace.get("editorial_material"),
            "quality_review": analyst_trace.get("quality_review"),
            "quality_review_input_sha256": analyst_trace.get("quality_review_input_sha256"),
        }

    def analyze_final(self,baseline,readiness,*,cutoff,before_writer=None):
        """Reuse frozen static evidence. No Research/feature/Player Knowledge rerun."""
        from v2.pregame_system.final_delta import run_incremental
        root=self.analyst.root/'incremental_final'
        return run_incremental(baseline,readiness,self.analyst,root,
            before_writer=before_writer,client=self.analyst.forecast_client or self.analyst.client,input_cutoff=cutoff)

    def _attach_player_knowledge(self, game: dict[str, Any], cutoff: str) -> None:
        """Attach compact prior-season facts without changing numerical forecasts."""
        from v2.pregame_system.player_knowledge import PlayerKnowledgeStore

        store = self.player_knowledge_store or PlayerKnowledgeStore()
        availability = (game.get("background") or {}).get("availability") or {}
        availability_source = availability.get("source") or {}
        availability_provenance = {
            "source_url": (
                availability.get("source_url") or availability.get("url")
                or availability_source.get("source_url") or availability_source.get("url")
            ),
            "source_published_at": (
                availability.get("source_published_at") or availability.get("source_published_at_utc")
                or availability.get("published_at") or availability_source.get("source_published_at")
                or availability_source.get("published_at")
            ),
            "captured_at": (
                availability.get("captured_at") or availability.get("captured_at_utc")
                or availability_source.get("captured_at") or availability_source.get("captured_at_utc")
            ),
            "source_cutoff": (
                availability.get("source_cutoff") or availability.get("cutoff_time_utc")
                or availability.get("cutoff_time") or availability.get("cutoff")
            ),
        }
        availability_records = []
        if availability.get("status") == "available":
            for record in availability.get("records") or []:
                if not isinstance(record, dict):
                    continue
                enriched_record = dict(availability_provenance)
                enriched_record.update(record)
                availability_records.append(enriched_record)
        try:
            context = store.matchup_context(
                game,
                cutoff,
                game.get("roster_context"),
                availability_records=availability_records,
            )
        except Exception as exc:
            context = {
                "status": "unavailable",
                "reason": "player_knowledge_load_failed",
                "error_type": type(exc).__name__,
                "season": None,
                "cutoff": cutoff,
                "teams": {},
                "numeric_model_usage": False,
            }
        roster_context = deepcopy(game.get("roster_context") or {})
        roster_context["player_knowledge"] = context
        roster_context["player_knowledge_history_coverage"] = {
            side: {
                "status": ((context.get("teams") or {}).get(side) or {}).get("status", "unavailable"),
                "current_roster_player_count": ((context.get("teams") or {}).get(side) or {}).get("current_roster_player_count"),
                "matched_history_count": ((context.get("teams") or {}).get(side) or {}).get("current_roster_match_count"),
                "unresolved_identity_count": ((context.get("teams") or {}).get(side) or {}).get("current_roster_identity_unresolved_count"),
                "players_with_previous_season_stats": ((context.get("teams") or {}).get(side) or {}).get("players_with_previous_season_stats"),
                "scope": "historical_player_record_match_only",
            }
            for side in ("away", "home")
        }
        for side in ("away", "home"):
            if side in (roster_context.get("teams") or {}):
                roster_context["teams"][side]["player_knowledge_history_coverage"] = \
                    roster_context["player_knowledge_history_coverage"][side]
        game["roster_context"] = roster_context

    @staticmethod
    def _attempt_path(path_value: Any) -> Path | None:
        if not isinstance(path_value, str) or not path_value:
            return None
        path = Path(path_value)
        if path.name in {"response.json", "trace.json"}:
            return path.parent / "attempt.json"
        return path / "attempt.json"

    @classmethod
    def _attempt_skill_hashes(cls, path_value: Any) -> dict[str, str]:
        payload = cls._read_attempt(path_value) or {}
        return dict(payload.get("skill_hashes") or {})

    @classmethod
    def _read_attempt(cls, path_value: Any) -> dict[str, Any] | None:
        attempt = cls._attempt_path(path_value)
        if attempt is None or not attempt.is_file():
            return None
        try:
            return json.loads(attempt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _artifact_receipt(path_value: Any) -> dict[str, Any] | None:
        if not isinstance(path_value, str) or not path_value:
            return None
        path = Path(path_value)
        if not path.exists():
            return {"path": path.as_posix(), "status": "unavailable"}
        root = Path(__file__).resolve().parents[2]
        try:
            display_path = path.resolve().relative_to(root).as_posix()
        except ValueError:
            display_path = path.name
        if path.is_file():
            refs = [{"path": display_path, "sha256": digest(path)}]
            attempt = path.parent / "attempt.json"
            if attempt.is_file():
                try:
                    attempt_ref = attempt.resolve().relative_to(root).as_posix()
                except ValueError:
                    attempt_ref = f"{path.parent.name}/attempt.json"
                refs.append({"path": attempt_ref, "sha256": digest(attempt)})
            return {"path": display_path, "files": refs, "status": "available"}
        files = []
        for filename in ("attempt.json", "trace.json", "evidence.json", "response.json",
                         "failure.json", "parsed_unvalidated.json", "context.json", "projection.json",
                         "independent_request.json", "independent_response.json", "independent.json",
                         "independent_validation.json", "market_review_validation.json",
                         "market_review_request.json", "market_review_response.json", "final.json"):
            item = path / filename
            if item.is_file():
                try:
                    child = item.resolve().relative_to(root).as_posix()
                except ValueError:
                    child = f"{display_path}/{filename}"
                files.append({"path": child, "sha256": digest(item)})
        return {"path": display_path, "files": files, "status": "available" if files else "empty"}
