"""On-demand analyst orchestration over the saved product snapshot.

This layer never recomputes or mutates numeric forecasts. It turns the exact
saved display snapshot into evidence, optionally adds cutoff-safe web facts,
then delegates language synthesis to the existing EvidenceBoundAnalyst.
"""
from __future__ import annotations

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
    }


def search_categories(game: dict[str, Any]) -> set[str]:
    categories: set[str] = {"game_story"}
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
        "official_roster_names": {
            side: [
                player["name"]
                for player in (((rosters.get(side) or {}).get("official_team_roster") or {}).get("players") or [])
                if player.get("name")
            ]
            for side in ("away", "home")
        },
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


def _evidence(
    game: dict[str, Any],
    cutoff: str,
    web_context: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    opening_night = (game.get("season_phase") or {}).get("phase") == "opening_night"
    schedule = dict((game.get("background") or {}).get("schedule") or {})
    if opening_night:
        # These model features are capped at seven days. They do not describe
        # the actual offseason break and must not become editorial facts.
        schedule.pop("home_rest_days", None)
        schedule.pop("away_rest_days", None)
        schedule["season_opening"] = True
    sections = {
        "prediction": {
            "pure_prediction": _independent_prediction(game),
            "market_benchmark": game["relationship"].get("market"),
            "divergence": game["relationship"].get("divergence"),
            "residual_prediction": game["relationship"].get("residual"),
            "official_numeric_prediction": game["relationship"]["final"],
        },
        "team_form": (
            None if opening_night and (game.get("season_form") or {}).get("status") == "available"
            else (game.get("background") or {}).get("recent_form")
        ),
        "matchup": {
            "schedule": schedule,
        },
        "roster_context": game.get("roster_context"),
        "matchup_history": game.get("matchup_history"),
        "season_form": game.get("season_form"),
        "season_phase": game.get("season_phase"),
        "market": (game.get("background") or {}).get("market"),
    }
    availability = (game.get("background") or {}).get("availability") or {}
    if availability.get("status") == "available":
        sections["availability"] = availability
    evidence: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = list(game.get("missing") or [])
    for section, value in sections.items():
        if value in (None, {}, []):
            continue
        evidence_id = content_hash(
            {"game_id": game["id"], "cutoff": cutoff, "section": section, "value": value}
        )
        evidence.append(
            {
                "id": evidence_id,
                "kind": "ModelForecast" if section == "prediction" else "Fact",
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
        searcher: PregameWebSearch | None = None,
        researcher: Any | None = None,
    ):
        self.data = data
        self.analyst = analyst or EvidenceBoundAnalyst(include_forecasting_skill=True)
        self.searcher = searcher
        self.researcher = researcher

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
        game = historical_game_context(original_game, cutoff) if replay else original_game
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
        web_context = merge_cached_public_market(web_context, game, cutoff=cutoff)
        if select_public_market_reference(web_context):
            search_trace["public_market_reference"] = "available"
        evidence, sources, missing = _evidence(game, cutoff, web_context)
        public_market = select_public_market_reference(web_context)
        matchup = f"{game['away']['name_zh']}对{game['home']['name_zh']}"
        context = build_analyst_context(
            prediction_layers(game),
            evidence,
            missing_data=missing,
            game={"game_id": game_id, "home": game["home"], "away": game["away"],
                  "scheduled_tipoff": game.get("tipoff"), "cutoff": cutoff},
            analysis_request=(
                f"生成{matchup}的第25秒赛前分析。使用本次 AnalystContext 与 objective_evidence，"
                "生成 final_prediction，并严格遵守 ProductAnalystOutput schema、字段格式和 evidence ID 协议。"
            ),
        )
        input_frozen_at = now()
        if before_writer is not None:
            before_writer()
        judgment, analyst_trace = self.analyst.judge(context)
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
            "analyst_trace": analyst_trace,
        }

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
