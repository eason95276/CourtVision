"""Serve the CourtVision V2 home and game detail frontend."""
from __future__ import annotations

import argparse
import functools
import json
import math
import os
import re
import sqlite3
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from v2.pregame_system.web_search import PregameWebSearch, select_public_market_reference
from v2.pregame_system.storage import content_hash, instant

from .analysis_service import (
    DEFAULT_PUBLIC_MARKET_CACHE,
    analysis_cutoff,
    cached_public_market_context,
    game_identity,
)
from .export_data import DEFAULT_OUTPUT, STATIC_ROOT, export_app_data, read_json
from .prediction_version_service import PredictionVersionService
from .schedule_catalog import ScheduleCatalog
from v2.pregame_system.prediction_versions import VersionStoreError

_PREDICTION_VERSION_SERVICE: PredictionVersionService | None = None
_SCHEDULE_CATALOG: ScheduleCatalog | None = None
_SCHEDULE_SYNC_RUNTIME = None
_SCHEDULE_RUNTIME_LOCK = threading.Lock()
DEFAULT_PORTFOLIO_EDITORIAL_DIR = Path(__file__).parent / "output" / "editorial-overrides"


def _prediction_display_projection(version: dict[str, Any] | None) -> dict[str, Any]:
    """Build a transient cutoff view only from the selected version's frozen inputs."""
    version = version if isinstance(version, dict) else {}
    snapshot = version.get("input_snapshot") or {}
    technical = snapshot.get("technical") or {}
    fact_context = snapshot.get("fact_context") or {}
    roster_context = snapshot.get("roster_context") or {}

    def frozen_timestamp(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
        except (TypeError, ValueError):
            return None
        return value.strip()

    feature_cutoff = fact_context.get("feature_cutoff")
    if feature_cutoff is None:
        feature_cutoff = technical.get("fact_context_cutoff")
    roster_cutoff = roster_context.get("context_cutoff")
    if roster_cutoff is None:
        roster_cutoff = technical.get("roster_context_cutoff")
    from v2.pregame_system.ats import spread_comparison_for_version
    from v2.pregame_system.editorial_contract import _writer_market_interpretation
    prediction = (version.get('judgment') or {}).get('final_prediction') or version.get('prediction') or {}
    interpretation = _writer_market_interpretation(
        {'spread_comparison':spread_comparison_for_version(version)}, prediction)
    return {
        "schema_version": "prediction-display-cutoffs-v1",
        "version_id": version.get("version_id"),
        "market_interpretation": interpretation,
        "cutoffs": {
            "input_cutoff_at": frozen_timestamp(version.get("input_cutoff_at")),
            "feature_cutoff_at": frozen_timestamp(feature_cutoff),
            "roster_context_cutoff_at": frozen_timestamp(roster_cutoff),
        },
    }


def _with_display_projection(selection: dict[str, Any]) -> dict[str, Any]:
    """Attach UI-only metadata beside the immutable payload, never inside it."""
    result = dict(selection)
    result["display_projection"] = _prediction_display_projection(result.get("version"))
    return result


def _attach_official_injury_report(game, service):
    from .official_injury_reports import OfficialInjuryReportStore
    try:
        game['official_injury_report'] = OfficialInjuryReportStore(service.store.path).read_as_of(game)
    except (OSError, sqlite3.Error, ValueError, KeyError):
        game['official_injury_report'] = {'status':'unavailable','records':[]}


def _with_portfolio_editorial(selection: dict[str, Any]) -> dict[str, Any]:
    """Expose an editorial-only artifact beside its immutable base prediction."""
    result = _with_display_projection(selection)
    version = result.get("version") or {}
    version_id = version.get("version_id")
    if not isinstance(version_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", version_id):
        return result
    path = DEFAULT_PORTFOLIO_EDITORIAL_DIR / f"{version_id}.json"
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result
    if not isinstance(artifact, dict):
        return result
    editorial = artifact.get("editorial") if isinstance(artifact, dict) else None
    if (artifact.get("schema_version") != "portfolio-editorial-v1" or
            artifact.get("base_prediction_version_id") != version_id or
            artifact.get("base_prediction_payload_sha256") != version.get("payload_sha256") or
            not isinstance(editorial, dict) or
            artifact.get("editorial_sha256") != content_hash(editorial) or
            not isinstance(editorial.get("pregame_story"), dict) or
            not isinstance(editorial.get("analysis_article"), str) or
            not isinstance(editorial.get("watch_themes"), list) or
            not isinstance(editorial.get("player_analysis"), list)):
        return result
    result["display_projection"]["portfolio_editorial"] = {
        "schema_version": "portfolio-editorial-v1",
        "artifact_id": artifact.get("artifact_id"),
        "base_prediction_version_id": version_id,
        "base_prediction_payload_sha256": version.get("payload_sha256"),
        "editorial_sha256": artifact.get("editorial_sha256"),
        "generated_at": artifact.get("generated_at"),
        "editorial": editorial,
    }
    return result


def _current_schedule_season_form(
    game: dict[str, Any], version: dict[str, Any] | None, catalog: ScheduleCatalog,
) -> dict[str, Any] | None:
    """Make a display-only recent record from final same-season games before the frozen cutoff."""
    from datetime import datetime, timezone
    from .export_data import team as team_payload

    def aware(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc) if parsed.tzinfo and parsed.utcoffset() is not None else None

    tipoff = aware(game.get("tipoff"))
    if tipoff is None:
        return None
    input_cutoff = aware((version or {}).get("input_cutoff_at"))
    if input_cutoff is None:
        input_cutoff = aware((game.get("technical") or {}).get("cutoff"))
    cutoff = min(tipoff, input_cutoff) if input_cutoff else tipoff
    season_year = game.get("season")
    try:
        season_year = int(season_year)
    except (TypeError, ValueError):
        label = str(game.get("season_label") or "")
        match = re.fullmatch(r"(\d{4})-(\d{2,4})", label)
        if not match:
            return None
        season_year = int(match.group(1)) + 1
    if season_year < 1901:
        return None
    target_game_id = str(game.get("id") or "")
    teams: dict[str, Any] = {}
    coverage = catalog.count_games(season=season_year)
    if coverage <= 0:
        return None
    objective_evidence = ((version or {}).get("analyst_context") or {}).get("objective_evidence") or []

    def confirmed_by_cutoff_research(row, team_id: int, team_score: float, opponent_score: float,
                                     cutoff_at: datetime) -> bool:
        # Research facts are already bound to this target game and team ID;
        # exact score plus a pre-cutoff publication timestamp can corroborate a
        # schedule row synced later, while names vary between provider aliases.
        score_pattern = re.compile(rf"(?<!\d){int(team_score)}\s*[-–]\s*{int(opponent_score)}(?!\d)")
        for item in objective_evidence:
            if item.get("section") != "web_context":
                continue
            fact = item.get("value") or {}
            if (fact.get("eligible_for_fact") is not True or
                    fact.get("claim_type") != "game_news" or
                    str(item.get("game_id") or fact.get("game_id")) != target_game_id):
                continue
            try:
                fact_team = int(fact.get("team_id"))
            except (TypeError, ValueError):
                continue
            published = aware(fact.get("source_published_at") or fact.get("published_at") or
                              fact.get("publication_earliest"))
            quote = str(fact.get("quote") or "").casefold()
            if fact_team == team_id and published is not None and published <= cutoff_at and score_pattern.search(quote):
                return True
        return False

    for side in ("away", "home"):
        team_id = int(game[side]["id"])
        rows = catalog.completed_team_games_before(
            team_id=team_id, season=season_year, cutoff=cutoff.isoformat(),
            exclude_game_id=target_game_id,
        )
        records = []
        for row in rows:
            home_score = row.get("home_score")
            away_score = row.get("away_score")
            try:
                home_score, away_score = float(home_score), float(away_score)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(home_score) or not math.isfinite(away_score) or home_score <= 0 or away_score <= 0:
                continue
            is_home = int(row["home_team_id"]) == team_id
            team_score, opponent_score = (home_score, away_score) if is_home else (away_score, home_score)
            if team_score == opponent_score:
                continue
            opponent_id = int(row["away_team_id"] if is_home else row["home_team_id"])
            opponent_raw_name = row.get("away_team_name") if is_home else row.get("home_team_name")
            opponent = team_payload(opponent_id, opponent_raw_name)
            captured = aware(row.get("captured_at"))
            if (captured is None or captured > cutoff) and not confirmed_by_cutoff_research(
                    row, team_id, team_score, opponent_score, cutoff):
                continue
            records.append({
                "game_id": row["game_id"], "played_at": row["scheduled_tipoff"],
                "won": team_score > opponent_score,
                "team_score": int(team_score) if team_score.is_integer() else team_score,
                "opponent_score": int(opponent_score) if opponent_score.is_integer() else opponent_score,
                "opponent_team_id": opponent_id, "opponent_name_zh": opponent.get("name_zh"),
            })
        total_wins = sum(1 for row in records if row["won"])
        total_games = len(records)
        recent = records[:5]
        recent_wins = sum(1 for row in recent if row["won"])
        streak_kind = records[0]["won"] if records else None
        streak_count = 0
        for row in records:
            if row["won"] != streak_kind:
                break
            streak_count += 1
        teams[side] = {
            "team_id": team_id,
            "current_season_games": total_games,
            "current_season_game_ids": [row["game_id"] for row in records],
            "current_season_record": {"wins": total_wins, "losses": total_games - total_wins},
            "current_last5": {
                "status": "available" if recent else "no_games", "games": len(recent),
                "wins": recent_wins, "losses": len(recent) - recent_wins,
                "win_rate": recent_wins / len(recent) if recent else None,
                "game_ids": [row["game_id"] for row in recent],
            },
            "last_result": records[0] if records else None,
            "streak": {"result": "win" if streak_kind else "loss", "games": streak_count} if records else None,
        }
    form = {
        "status": "available", "game_id": target_game_id, "cutoff": cutoff.isoformat(),
        "season_start": season_year - 1, "competition_scope": "all_game_types_in_current_season",
        "source": "schedule_catalog_final_games", "numeric_model_usage": False, "teams": teams,
    }
    form["context_hash"] = content_hash(form)
    return form


def _prediction_version_service() -> PredictionVersionService:
    global _PREDICTION_VERSION_SERVICE
    if _PREDICTION_VERSION_SERVICE is None:
        from .current_roster_preflight import prepare_current_roster_preflight
        _PREDICTION_VERSION_SERVICE = PredictionVersionService(
            pre_generation_preparer=prepare_current_roster_preflight
        )
    return _PREDICTION_VERSION_SERVICE


def build_prediction_versions_response(
    *, data: dict[str, Any] | None = None, version_service: PredictionVersionService | None = None
) -> tuple[int, dict[str, Any]]:
    service = version_service or _prediction_version_service()
    payload = (_runtime_data(data, service, ready_only=True) if data is not None
               else build_schedule_window_response(version_service=service))
    selections: dict[str, Any] = {}
    for game in payload.get("games", []):
        game_id = game.get("id")
        if not isinstance(game_id, str):
            continue
        if game.get("schedule_catalog_fact") and not game.get("registered_game") and not _has_published_version(service, game_id):
            continue
        try:
            selections[game_id] = _with_portfolio_editorial(service.selected(game))
        except VersionStoreError as exc:
            selections[game_id] = _with_portfolio_editorial({
                "status": str(exc), "game_id": game_id, "version": None,
                "version_id": None, "state_revision": 0, "locked_at": None,
                "lock_reason": None,
            })
    return 200, {"status": "success", "predictions": selections}


def _runtime_data(data, service, *, ready_only=False):
    if not hasattr(service, "tasks"):
        return data
    from .generation_input import merge_registered_games
    return merge_registered_games(data, service.tasks, ready_only=ready_only)


def _schedule_catalog(service=None) -> ScheduleCatalog:
    global _SCHEDULE_CATALOG
    service = service or _prediction_version_service()
    store = service.store
    if _SCHEDULE_CATALOG is None or _SCHEDULE_CATALOG.path != store.path:
        _SCHEDULE_CATALOG = ScheduleCatalog(store.path, clock=getattr(service, "clock", None))
    return _SCHEDULE_CATALOG


def start_schedule_sync_runtime(service=None):
    """Start schedule-facts refresh only; this never starts or expands AI Scheduler."""
    global _SCHEDULE_SYNC_RUNTIME
    with _SCHEDULE_RUNTIME_LOCK:
        if _SCHEDULE_SYNC_RUNTIME is not None:
            return _SCHEDULE_SYNC_RUNTIME
        from .schedule_sync import ScheduleSyncRuntime
        _SCHEDULE_SYNC_RUNTIME = ScheduleSyncRuntime(_schedule_catalog(service)).start()
        return _SCHEDULE_SYNC_RUNTIME


def _has_published_version(service, game_id: str) -> bool:
    try:
        db = service.store.connect()
        try:
            return bool(db.execute("SELECT 1 FROM prediction_versions WHERE game_id=? LIMIT 1", (game_id,)).fetchone())
        finally:
            db.close()
    except (AttributeError, sqlite3.Error):
        return False


def _postgame_status(service, game: dict[str, Any]) -> dict[str, Any] | None:
    if str(game.get("status") or "").casefold() not in {"final", "post", "completed", "finished"}:
        return None
    from v2.postgame_system.version_service import read_postgame, read_postgame_status
    from v2.postgame_system.editorial import build_public_postgame_entry
    try:
        status = read_postgame_status(service.store, game["id"], game_status=game.get("status"),
                                      has_published_pregame=_has_published_version(service, game["id"]))
        if not status or status.get("status") != "available":
            return status
        postgame = read_postgame(service.store, game["id"])
        if not postgame:
            return {"status": "pending"}
        return build_public_postgame_entry(service.store, postgame)
    except (ValueError, KeyError):
        return {"status": "unavailable", "message": "赛后复盘校验未通过"}


def _schedule_fact_game(record: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    from .export_data import team as team_payload

    home = {**team_payload(int(record["home_team_id"]), record.get("home_team_name")),
            "espn_team_id": record.get("home_espn_team_id"), "score": record.get("home_score")}
    away = {**team_payload(int(record["away_team_id"]), record.get("away_team_name")),
            "espn_team_id": record.get("away_espn_team_id"), "score": record.get("away_score")}
    same_teams = bool(base and int(base.get("home", {}).get("id", -1)) == home["id"]
                      and int(base.get("away", {}).get("id", -1)) == away["id"])
    if same_teams:
        result = dict(base)
        result["home"] = {**base["home"], **home}
        result["away"] = {**base["away"], **away}
        result["tipoff"] = record["scheduled_tipoff"]
        result["status"] = record["status"]
        result["game_type"] = record["game_type"]
        if record.get("season") is not None:
            result["season"] = record["season"]
        if record.get("season_label"):
            result["season_label"] = record["season_label"]
        result["schedule_revision"] = (base.get("schedule_revision") if base.get("registered_game")
                                        else record.get("schedule_revision", 1))
    else:
        result = {
            "id": record["game_id"], "tipoff": record["scheduled_tipoff"], "status": record["status"],
            "game_type": record["game_type"], "season": record["season"],
            "season_label": record.get("season_label"),
            "schedule_revision": record.get("schedule_revision", 1), "home": home, "away": away,
            "prediction": {"available": False},
            "relationship": {"basketball": None, "market": None, "residual": None, "final": None},
            "background": {"market": None, "availability": {"status": "unavailable", "records": []},
                           "recent_form": {"home": {}, "away": {}}, "schedule": {}},
            "key_factors": [], "watch_themes": [], "roster_context": None,
            "matchup_history": {"status": "unavailable"}, "season_form": {"status": "unavailable"},
            "season_phase": {"phase": record["game_type"]}, "missing": [],
            "technical": {"source_label": "尚无正式赛前分析", "cutoff": None}, "source": {}, "postgame": None,
        }
    result.update(id=record["game_id"], schedule_catalog_fact=True, homepage_visible=True,
                  beijing_date=record.get("beijing_date"),
                  nba_game_id=record.get("nba_game_id"), cup_stage=record.get("cup_stage"),
                  venue=record.get("venue"), neutral_site=record.get("neutral_site"))
    if not result.get("season_label"):
        try:
            season_year = int(result.get("season"))
            if 1900 < season_year < 2200:
                result["season_label"] = f"{season_year - 1}-{season_year % 100:02d}"
        except (TypeError, ValueError):
            pass
    result["registered_game"] = bool(result.get("registered_game"))
    if not result["registered_game"]:
        result["generation_hold_reason"] = "schedule_catalog_only_not_enabled_for_generation"
    result["source"] = {
        **(result.get("source") or {}), "schedule_source": record.get("source_url"),
        "schedule_captured_at": record.get("captured_at"),
        "schedule_response_sha256": record.get("response_sha256"),
        "schedule_event_sha256": record.get("event_sha256"), "schedule_validation": record.get("validation"),
    }
    return result


def _clear_unpublished_snapshot(game: dict[str, Any]) -> dict[str, Any]:
    """Keep identity/context shells but never expose a legacy demo number as an official prediction."""
    result = dict(game)
    result["prediction"] = {"available": False}
    result["relationship"] = {"basketball": None, "market": None, "residual": None, "final": None}
    result["background"] = {"market": None, "availability": {"status": "unavailable", "records": []},
                            "recent_form": {"home": {}, "away": {}}, "schedule": {}}
    result["key_factors"] = []
    result["watch_themes"] = []
    result["pregame_story"] = None
    result["roster_context"] = None
    result["matchup_history"] = {"status": "unavailable"}
    result["season_form"] = {"status": "unavailable"}
    result["missing"] = []
    result["technical"] = {"source_label": "尚无正式赛前分析", "cutoff": None}
    result["postgame"] = None
    result["schedule_catalog_fact"] = True
    result["generation_hold_reason"] = "schedule_catalog_only_not_enabled_for_generation"
    result["registered_game"] = False
    return result


def build_schedule_window_response(
    *, data: dict[str, Any] | None = None, version_service: PredictionVersionService | None = None,
    catalog: ScheduleCatalog | None = None, now=None,
) -> dict[str, Any]:
    service = version_service or _prediction_version_service()
    legacy = data if data is not None else read_json(DEFAULT_OUTPUT)
    enriched = _runtime_data(legacy, service, ready_only=True)
    window = (catalog or _schedule_catalog(service)).homepage_window(now)
    bases = {game.get("id"): game for game in enriched.get("games", []) if game.get("id")}
    games = []
    game_ids = set()
    for row in window["games"]:
        base = bases.get(row["game_id"])
        if base and not base.get("registered_game") and not _has_published_version(service, row["game_id"]):
            base = None
        games.append(_schedule_fact_game(row, base))
        game_ids.add(row["game_id"])
    # Keep only already-enabled games as a fallback if the independent catalog
    # source is temporarily unavailable; this never opts new catalog rows in.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    local_dates = {window["today"], window["tomorrow"]}
    china = ZoneInfo("Asia/Shanghai")
    registered_fallback_count = 0
    for game in enriched.get("games", []):
        game_id = game.get("id")
        if not game.get("registered_game") or not isinstance(game_id, str) or game_id in game_ids:
            continue
        try:
            tipoff = datetime.fromisoformat(str(game.get("tipoff", "")).replace("Z", "+00:00"))
            if tipoff.tzinfo is None or tipoff.utcoffset() is None or tipoff.astimezone(china).date().isoformat() not in local_dates:
                continue
        except (TypeError, ValueError):
            continue
        fallback = {**game, "schedule_catalog_fact": True, "homepage_visible": True,
                    "schedule_facts_fallback": "previously_registered_game",
                    "beijing_date": tipoff.astimezone(china).date().isoformat()}
        games.append(fallback)
        game_ids.add(game_id)
        registered_fallback_count += 1
    if registered_fallback_count:
        window["status"] = "stale"
        window["fallback_source"] = "previously_registered_schedules"
    games.sort(key=lambda item: (item.get("tipoff") or "", item.get("id") or ""))
    from .team_season_stats import attach_previous_season_team_stats
    for item in games:
        attach_previous_season_team_stats(item, str(service.store.path))
        _attach_official_injury_report(item, service)
        item["postgame"] = _postgame_status(service, item)
    window["today_count"] = sum(game.get("beijing_date") == window["today"] for game in games)
    window["tomorrow_count"] = sum(game.get("beijing_date") == window["tomorrow"] for game in games)
    teams = enriched.get("teams") or []
    if not teams:
        from v2.pregame_system.agent import TEAM_NAMES
        from .export_data import team as team_payload
        teams = [team_payload(team_id) for team_id in sorted(TEAM_NAMES)]
    with_prediction = sum(_has_published_version(service, game["id"]) for game in games)
    return {
        "schema_version": "3.0", "generated_at": window["as_of"],
        "schedule_window": {key: value for key, value in window.items() if key != "games"},
        "coverage": {"schedule_games": len(games), "games_with_saved_prediction": with_prediction,
                     "games_without_saved_prediction": len(games) - with_prediction, "displayed_team_count": len(teams)},
        "games": games, "teams": teams, "product": {"name": "第25秒", "slogan": "看见比分之外"},
        "source": {"schedule_catalog": "SQLite", "schedule_payload_scope": "today_and_tomorrow_only"},
    }


def build_game_detail_response(
    game_id: str, *, data: dict[str, Any] | None = None,
    version_service: PredictionVersionService | None = None, catalog: ScheduleCatalog | None = None,
) -> tuple[int, dict[str, Any]]:
    service = version_service or _prediction_version_service()
    legacy = data if data is not None else read_json(DEFAULT_OUTPUT)
    enriched = _runtime_data(legacy, service, ready_only=False)
    base = next((item for item in enriched.get("games", []) if item.get("id") == game_id), None)
    record = (catalog or _schedule_catalog(service)).get_game(game_id)
    if record:
        if base and not base.get("registered_game") and not _has_published_version(service, game_id):
            base = None
        game = _schedule_fact_game(record, base)
    elif base:
        game = {**base, "schedule_catalog_fact": True}
        if not game.get("registered_game") and not _has_published_version(service, game_id):
            game = _clear_unpublished_snapshot(game)
        if not game.get("registered_game"):
            game["generation_hold_reason"] = "legacy_snapshot_not_enabled_for_generation"
    else:
        return 404, {"status": "not_found", "message": "没有找到这场比赛。"}
    from .team_season_stats import attach_previous_season_team_stats
    attach_previous_season_team_stats(game, str(service.store.path))
    _attach_official_injury_report(game, service)
    if game.get("registered_game") or _has_published_version(service, game_id):
        try:
            selection = service.selected(game)
        except (VersionStoreError, ValueError, KeyError) as exc:
            selection = {"status": "unavailable", "game_id": game_id, "version": None,
                         "version_id": None, "state_revision": 0, "error": type(exc).__name__}
    else:
        selection = {"status": "unavailable", "game_id": game_id, "version": None,
                     "version_id": None, "state_revision": 0, "locked_at": None, "lock_reason": None}
    try:
        current_form = _current_schedule_season_form(game, selection.get("version"), catalog or _schedule_catalog(service))
    except (sqlite3.Error, OSError, TypeError, ValueError, KeyError):
        current_form = None
    if current_form is not None:
        game["season_form"] = current_form
    game["postgame"] = _postgame_status(service, game)
    selection = _with_portfolio_editorial(selection)
    return 200, {"status": "success", "game": game, "prediction": selection}


def _version_update_response(selection: dict[str, Any], *, update_status: str,
                             message: str | None = None) -> dict[str, Any]:
    selection = _with_portfolio_editorial(selection)
    return {
        **selection,
        "status": "success" if selection.get("version") else selection.get("status", "unavailable"),
        "update_status": update_status,
        "message": message,
    }


def load_local_env(path: str | Path | None = None) -> None:
    """Load an ignored project-local .env without overriding shell values."""
    env_path = Path(path or Path(__file__).parents[2] / ".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def build_ai_response(
    game_id: str,
    *,
    api_key: str | None = None,
    data: dict[str, Any] | None = None,
    request_id: str | None = None,
    version_service: PredictionVersionService | None = None,
    allow_generate: bool = False,
) -> tuple[int, dict[str, Any]]:
    service = version_service or _prediction_version_service()
    payload = _runtime_data(data or read_json(DEFAULT_OUTPUT), service)
    game = next((item for item in payload["games"] if item.get("id") == game_id), None)
    if game is None:
        return 404, {"status": "error", "message": "没有找到这场比赛的前端数据。"}
    try:
        selected = service.selected(game)
    except Exception as exc:
        return 503, {
            "status": "unavailable",
            "message": f"版本状态暂不可用：{type(exc).__name__}。",
        }
    if selected["status"] in {"locked", "locked_unavailable", "schedule_changed"}:
        return 200, _version_update_response(selected, update_status="locked")
    if not allow_generate:
        return 200, _version_update_response(selected, update_status="read_only",
            message=None if selected.get("version") else "当前没有已发布的赛前分析。")
    if game.get("generation_hold_reason"):
        return 200, _version_update_response(selected, update_status="paused",
            message="当前没有正式赛前分析，生成暂未开放。")
    key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        status = 200 if selected.get("version") else 503
        return status, _version_update_response(
            selected, update_status="failed", message="本地尚未配置分析模型凭据。当前预测保持不变。"
        )
    try:
        result = service.generate(payload, game_id, request_id=request_id)
        return 200, _with_portfolio_editorial(result)
    except Exception as exc:
        try:
            current = service.selected(game)
        except Exception:
            current = selected
        status = 200 if current.get("version") else 503
        return status, _version_update_response(
            current,
            update_status="failed",
            message=f"第25秒分析暂不可用：{type(exc).__name__}。",
        )


def build_market_response(
    game_id: str,
    *,
    data: dict[str, Any] | None = None,
    searcher: Any | None = None,
    cache_path: str | Path = DEFAULT_PUBLIC_MARKET_CACHE,
    allow_lookup: bool = False,
    version_service: PredictionVersionService | None = None,
    auto_refresh: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Resolve a display-only public quote without invoking the LLM."""
    payload = data or read_json(DEFAULT_OUTPUT)
    if data is None or version_service is not None:
        payload = _runtime_data(payload, version_service or _prediction_version_service())
    game = next((item for item in payload.get("games", []) if item.get("id") == game_id), None)
    if game is None:
        return 404, {"status": "error", "message": "没有找到这场比赛的前端数据。"}
    structured = (game.get("background") or {}).get("market")
    if structured:
        return 200, {
            "status": "structured_available",
            "game_id": game_id,
            "public_market_reference": None,
        }
    if version_service is not None or data is None:
        from .market_reference_store import MarketReferenceStore,MarketReferenceService
        display_store=MarketReferenceStore((version_service or _prediction_version_service()).store)
        display=display_store.read(game)
        if auto_refresh:MarketReferenceService(display_store).start(game)
        fetch_state=display_store.state(game_id)
        refreshing=bool(fetch_state and fetch_state['status']=='running' and instant(fetch_state['lease_until'])>instant(display_store.clock()))
        if display:
            return 200,{'status':'available','game_id':game_id,'public_market_reference':display,
                'refreshing':refreshing,'lookup':{'source':'independent_market_reference_cache'}}
        selected=(version_service or _prediction_version_service()).selected(game).get('version')or{}
        reference=selected.get('public_market_reference')
        if reference:
            return 200,{'status':'available','game_id':game_id,'public_market_reference':reference,
                'lookup':{'source':'published_prediction_version','version_id':selected['version_id']}}
    try:
        cutoff = analysis_cutoff(game)
    except (KeyError, TypeError, ValueError):
        return 200, {
            "status": "fetching" if locals().get('refreshing') else "unavailable",
            "game_id": game_id,
            "message": "缺少可核验的市场时间截止点。",
            "lookup": {"source": "cutoff_unavailable"},
        }
    cached = cached_public_market_context(game, cache_path, cutoff=cutoff)
    cached_reference = select_public_market_reference(cached)
    if cached_reference:
        return 200, {
            "status": "available",
            "game_id": game_id,
            "public_market_reference": cached_reference,
            "lookup": {"source": "verified_cache"},
        }
    if not allow_lookup:
        return 200, {"status": "fetching" if locals().get('refreshing') else "unavailable", "game_id": game_id,
                     "public_market_reference": None, "lookup": {"source": "read_only_cache"}}

    if searcher is None and not os.environ.get("TAVILY_API_KEY"):
        return 200, {
            "status": "unavailable",
            "game_id": game_id,
            "message": "暂无可核验的公开赛前市场数据。",
            "lookup": {"source": "not_configured"},
        }
    try:
        client = searcher or PregameWebSearch()
        context = client.search(
            game_identity(game),
            cutoff,
            replay=game.get("status") != "scheduled",
            categories={"market"},
        )
        reference = select_public_market_reference(context)
        if reference:
            return 200, {
                "status": "available",
                "game_id": game_id,
                "public_market_reference": reference,
                "lookup": {"source": "web_search"},
            }
    except Exception as exc:
        return 200, {
            "status": "unavailable",
            "game_id": game_id,
            "message": "公开市场参考暂时无法取得。",
            "lookup": {"source": "web_search", "error_type": type(exc).__name__},
        }
    return 200, {
        "status": "unavailable",
        "game_id": game_id,
        "message": "暂无可核验的公开赛前市场数据。",
        "lookup": {"source": "web_search", "records": 0},
    }


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"[25th-second] {self.address_string()} {format % args}")

    def json_response(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.startswith("/api/game?"):
            from urllib.parse import urlparse, parse_qs
            game_id = parse_qs(urlparse(self.path).query).get("game_id", [""])[0]
            status, payload = build_game_detail_response(game_id)
            self.json_response(status, payload)
            return
        if self.path == "/api/schedule-window":
            self.json_response(200, build_schedule_window_response())
            return
        if self.path.startswith('/api/postgame?'):
            from urllib.parse import urlparse,parse_qs
            from v2.postgame_system.version_service import read_postgame
            from v2.postgame_system.editorial import build_public_postgame_payload
            gid=parse_qs(urlparse(self.path).query).get('game_id',[''])[0]
            try:
                post=read_postgame(_prediction_version_service().store,gid)
                public_post=(
                    build_public_postgame_payload(_prediction_version_service().store,post)
                    if post else None
                )
                self.json_response(200,{'status':'available' if public_post else 'unavailable',
                                        'postgame':public_post})
            except ValueError:
                self.json_response(503,{'status':'unavailable','message':'赛后复盘校验未通过'})
            return
        if self.path.startswith("/api/market-reference?"):
            from urllib.parse import urlparse, parse_qs
            game_id = parse_qs(urlparse(self.path).query).get("game_id", [""])[0]
            # Browser reads are cache-only; they must not start provider traffic.
            status, payload = build_market_response(game_id,auto_refresh=False)
            self.json_response(status, payload)
            return
        if self.path == "/app_data.json":
            self.json_response(200, build_schedule_window_response())
            return
        if self.path == "/api/current-analyses":
            status, payload = build_prediction_versions_response()
            self.json_response(status, payload)
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path not in {"/api/ai-analysis", "/api/market-reference"}:
            self.json_response(404, {"status": "error", "message": "接口不存在。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError("invalid body length")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            game_id = payload.get("game_id")
            if not isinstance(game_id, str):
                raise ValueError("game_id required")
            request_id = payload.get("request_id")
            if request_id is not None and not isinstance(request_id, str):
                raise ValueError("request_id must be a string")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self.json_response(400, {"status": "error", "message": "请求格式无效。"})
            return
        if self.path == "/api/market-reference":
            status, response = build_market_response(game_id)
        else:
            status, response = build_ai_response(game_id, request_id=request_id)
        self.json_response(status, response)


def schedule_sync_enabled(disabled=False):
    if disabled:
        return False
    value = os.environ.get('COURTVISION_SCHEDULE_SYNC_ENABLED')
    if value is not None:
        if value.strip().casefold() not in {'true','false','1','0'}:
            raise ValueError('invalid_schedule_sync_configuration')
        return value.strip().casefold() in {'true','1'}
    return os.environ.get('COURTVISION_ENV','development').casefold() == 'production'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8625)
    parser.add_argument("--refresh-data", action="store_true")
    parser.add_argument("--disable-schedule-sync", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_local_env()
    if args.refresh_data:
        export_app_data()
    enabled = schedule_sync_enabled(args.disable_schedule_sync)
    print(f'[25th-second] schedule_sync_enabled={enabled}',flush=True)
    if enabled:
        start_schedule_sync_runtime(_prediction_version_service())
    handler = functools.partial(Handler, directory=str(STATIC_ROOT))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"第25秒 running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
