"""Bridge registered future games into the existing saved-game analysis input."""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from v2.pregame_system.generation_tasks import GenerationTaskStore, normalize_registration
from v2.pregame_system.prediction_versions import VersionStoreError
from v2.pregame_system.storage import instant


def registered_fact_game(game):
    """Display a registered identity without manufacturing a baseline or current roster."""
    from .export_data import team
    result = copy.deepcopy(game)
    result.update(registered_game=True, report_status="unavailable", prediction=None,
        relationship={"basketball": None, "market": None, "residual": None, "final": None},
        key_factors=[], missing=[{"section": key, "reason": "not_collected"}
                               for key in ("roster", "availability", "market")],
        roster_context=None, season_phase={"phase": game["game_type"], "game_type": game["game_type"]},
        background={"market": None, "availability": {"status": "unavailable", "records": [],
                    "message": "暂无可靠伤病信息"}, "schedule": {}, "recent_form": {"home": {}, "away": {}}},
        technical={"source_label": "真实赛程 · 尚无正式赛前分析", "cutoff": None})
    for side in ("home", "away"):
        raw = game[side]
        english_name = game.get(f"{side}_team") or raw.get("name")
        result[side] = {**team(int(raw["id"]), english_name), **raw}
        if english_name:
            result[side]["name"] = english_name
    source = game.get("source") or {}
    result["source"] = {**source, "schedule_source": source.get("url"),
                        "schedule_captured_at": source.get("captured_at")}
    return result


def merge_registered_games(data: dict[str, Any], tasks: GenerationTaskStore, *, ready_only=False):
    result = copy.deepcopy(data)
    games = {game["id"]: game for game in result.get("games", [])}
    for registered in tasks.registered_games():
        game_id = registered["id"]
        if game_id in games:
            from v2.pregame_system.prediction_versions import game_identity
            if (game_identity(games[game_id]) == game_identity(registered)
                    and games[game_id].get("schedule_revision", 1) == registered["schedule_revision"]):
                games[game_id].update(game_type=registered["game_type"], schedule_revision=registered["schedule_revision"],
                    registered_game=True, policy_config=registered["policy_config"])
                if registered.get("generation_hold_reason"):
                    games[game_id]["generation_hold_reason"] = registered["generation_hold_reason"]
                continue
        candidate = registered
        try:
            selected = tasks.versions.select_current(registered)
        except VersionStoreError:
            selected = {}  # A damaged report must not break unrelated games or become input.
        if selected.get("version"):
            candidate = copy.deepcopy(selected["version"]["input_snapshot"])
            candidate.update(schedule_revision=registered["schedule_revision"], policy_config=registered["policy_config"], registered_game=True)
        else:
            candidate = copy.deepcopy(candidate)
            candidate["report_status"] = "unavailable"
        if not (candidate.get("relationship") or {}).get("final"):
            candidate = registered_fact_game(candidate)
        games[game_id] = candidate
    result["games"] = sorted(games.values(), key=lambda game: (game["tipoff"], game["id"]))
    from v2.pregame_system.scheduler_policy import product_category
    from v2.pregame_system.prediction_versions import _clock_instant
    at = _clock_instant(tasks.clock)
    for game in result["games"]:
        from v2.postgame_system.version_service import result_for_game, read_postgame
        outcome=result_for_game(tasks.versions,game['id'])
        if outcome:
            game.update(status='final',result=outcome['game'])
            post=read_postgame(tasks.versions,game['id'])
            game['postgame']={'status':'available' if post else 'unavailable','version_id':post['version_id'] if post else None}
        from .player_display_names import catalog
        roster_players=[p for t in (game.get('roster_context')or{}).get('teams',{}).values()
            for p in (t.get('official_team_roster')or t.get('current_team_roster')or t.get('roster')or{}).get('players',[])]
        game['player_display_names']=catalog(tasks.versions,roster_players) if roster_players else []
        display={r['name_key']:r['display_name_zh'] for r in game['player_display_names']}
        from v2.pregame_system.espn_boxscore_normalizer import normalize_identity_name
        for p in roster_players:
            if normalize_identity_name(p.get('name'))in display:p['display_name']=display[normalize_identity_name(p['name'])]
        game.update(product_category(game, at))
    if len(result["games"]) != len(data.get("games", [])):
        result.setdefault("coverage", {})["registered_games_added"] = len(result["games"]) - len(data.get("games", []))
    return result


def registered_schedule_game(game):
    from v2.pregame_system.agent import TEAM_NAMES
    normalized = normalize_registration(game)
    source = normalized.get("source") or {}
    captured = source.get("captured_at") or source.get("schedule_captured_at")
    if not captured and (source.get("url") or source.get("schedule_source")):
        raise VersionStoreError("registered_schedule_missing_capture_time")
    tipoff = datetime.fromisoformat(normalized["tipoff"]).astimezone(ZoneInfo("America/New_York"))
    return {
        "game_id": normalized["id"],
        "home_team_id": normalized["home"]["id"], "away_team_id": normalized["away"]["id"],
        "home_team": normalized.get("home_team") or normalized["home"].get("name") or TEAM_NAMES[normalized["home"]["id"]],
        "away_team": normalized.get("away_team") or normalized["away"].get("name") or TEAM_NAMES[normalized["away"]["id"]],
        "scheduled_tipoff": normalized["tipoff"], "status": "scheduled",
        "season_start": normalized.get("season_start", tipoff.year if tipoff.month >= 7 else tipoff.year - 1),
        "is_playoffs": int(normalized["game_type"] == "playoffs"),
        "game_type": normalized["game_type"],
        **{key: normalized[key] for key in ("venue", "neutral_site", "schedule_revision", "schedule_revision_source") if key in normalized},
        **({"source": {**source, "captured_at": captured}} if captured else {}),
    }


def build_registered_baseline(game, cutoff):
    """Explicit deterministic plan, no planner, writer, search or model training."""
    from v2.pregame_system.agent import PregameAgent
    from v2.pregame_system.prediction import Predictor
    from v2.pregame_system.runtime_assets import RuntimeAssetError
    from v2.pregame_system.storage import BASE, new_run, save
    from .export_data import normalize_game

    schedule_game = registered_schedule_game(game)
    try:
        predictor = Predictor(require_production=True)
    except RuntimeAssetError as exc:
        raise VersionStoreError(str(exc)) from exc
    if predictor.demo_context:
        raise VersionStoreError("registered_game_requires_production_runtime_assets")
    agent = PregameAgent(predictor=predictor)
    agent.schedule = [item for item in agent.schedule if item["game_id"] != schedule_game["game_id"]] + [schedule_game]
    predictor.schedule = agent.schedule
    report, _ = agent.ask(
        "registered pregame baseline", cutoff=cutoff,
        plan={"intent": "report", "game_id": schedule_game["game_id"]},
        allow_lookup=False, generate_judgment=False,
    )
    if not report.get("prediction"):
        raise VersionStoreError("registered_game_baseline_unavailable")
    path = new_run("registered_game_inputs") / "report.json"
    save(path, report)
    prepared = normalize_game(schedule_game, path, report, outputs=BASE / "outputs")
    prepared.update({key: game[key] for key in ("venue", "neutral_site", "schedule_revision", "schedule_revision_source", "season_event") if key in game})
    prepared.update(registered_game=True, game_type=game.get("game_type"), season_start=schedule_game["season_start"])
    if game.get('season') is not None:prepared['season']=game['season']
    for side in ('home','away'):
        if game[side].get('espn_team_id') is not None:
            prepared[side]['espn_team_id']=game[side]['espn_team_id']
    prepared["roster_context"] = report.get("roster_context")
    from v2.pregame_system.matchup_history import (
        build_matchup_history, build_season_form_context, season_phase_context,
    )
    prepared["matchup_history"] = build_matchup_history(schedule_game, cutoff, predictor.games, predictor.stats)
    prepared["season_form"] = build_season_form_context(schedule_game, cutoff, predictor.stats)
    rosters = ((prepared.get("roster_context") or {}).get("teams") or {})
    prepared["season_phase"] = season_phase_context(rosters.get("home"), rosters.get("away"), prepared["season_form"])
    return prepared


def prepare_generation_data(data, game_id, *, cutoff, baseline_builder=None):
    result = copy.deepcopy(data)
    game = next(item for item in result["games"] if item["id"] == game_id)
    kind = game.get("game_type", "regular_season")
    game_index = result["games"].index(game)
    prepared = game
    if not (game.get("relationship") or {}).get("final"):
        prepared = (baseline_builder or build_registered_baseline)(game, cutoff)
        # The baseline must describe the registered matchup, never another saved sample.
        from v2.pregame_system.prediction_versions import game_identity
        if game_identity(prepared) != game_identity(game):
            raise VersionStoreError("registered_baseline_identity_mismatch")
    preflight = game.get("current_roster_preflight")
    if preflight is not None:
        prepared = copy.deepcopy(prepared)
        prepared["current_roster_preflight"] = copy.deepcopy(preflight)
        prepared["roster_context"] = _bind_roster_identity_preflight(
            prepared.get("roster_context"), preflight, prepared,
            current_snapshots=game.get("current_roster_snapshots"),
        )
        prepared.pop("current_roster_snapshots", None)
    if prepared is not game or preflight is not None:
        result["games"][game_index] = prepared
    game = prepared
    game["game_type"] = kind
    phase = dict(game.get("season_phase") or {})
    phase["game_type"] = kind
    if kind in {"preseason", "playoffs"}:
        phase["phase"] = kind
    elif not phase.get("phase"):
        phase["phase"] = "regular_season"
    game["season_phase"] = phase
    return result


def _bind_roster_identity_preflight(roster_context, preflight, game, *, current_snapshots=None):
    if preflight.get("schema_version") != "current-roster-preflight-v1" or preflight.get("status") != "ready":
        raise VersionStoreError("current_roster_preflight_not_ready")
    context = copy.deepcopy(roster_context or {})
    teams = context.get("teams") or {}
    verified = preflight.get("teams") or {}
    for side in ("away", "home"):
        receipt = verified.get(side) or {}
        team = teams.get(side) or {}
        expected_team_id = int((game.get(side) or {}).get("id") or game.get(f"{side}_team_id"))
        snapshot = (current_snapshots or {}).get(side)
        if snapshot:
            official_snapshot = snapshot.get("official_team_roster")
            current_snapshot = snapshot.get("current_team_roster")
            membership = official_snapshot or current_snapshot
            if not isinstance(membership, dict):
                raise VersionStoreError("current_roster_preflight_snapshot_missing:" + side)
            team["official_team_roster"] = official_snapshot
            team["current_team_roster"] = current_snapshot
            team["roster"] = membership
            team["roster_basis"] = snapshot.get("roster_basis") or membership.get("roster_basis") or "unavailable"
            team["league_team_assignment"] = snapshot.get("league_team_assignment")
            team["source_record"] = snapshot
            team["confirmed_starters"] = None
            team["projected_starters"] = None
            team["current_rotation"] = None
            team["key_player_status"] = None
            team["continuity"] = {"status": "unavailable",
                "reason": "current_roster_refreshed_without_matching_minute_snapshot"}
            team["missing"] = list(dict.fromkeys([*(team.get("missing") or []),
                "continuity_denominator_not_refreshed_with_current_roster"]))
        official = team.get("official_team_roster") or team.get("roster") or {}
        source = official.get("source") or {}
        players = official.get("players") or []
        try:
            matched = (
                receipt.get("status") == "verified"
                and int(receipt.get("team_id")) == expected_team_id
                and official.get("status") == "available"
                and int(official.get("team_id")) == expected_team_id
                and len(players) == int(receipt.get("player_count"))
                and source.get("sha256") == receipt.get("raw_response_sha256")
                and instant(official.get("observed_at") or source.get("captured_at"))
                    == instant(receipt.get("observed_at"))
                and source.get("source_type") == receipt.get("source_type")
            )
        except (TypeError, ValueError):
            matched = False
        if not matched:
            raise VersionStoreError("current_roster_preflight_snapshot_mismatch:" + side)
        team["current_roster_identity_coverage"] = {
            "status": "verified",
            "verified_player_count": len(players),
            "observed_at": receipt["observed_at"],
            "source_type": receipt["source_type"],
            "scope": "current team membership only; not game availability or starter status",
        }
        teams[side] = team
    context["teams"] = teams
    context["identity_preflight"] = {
        "status": "ready",
        "schema_version": preflight["schema_version"],
        "game_id": preflight.get("game_id"),
        "teams": {side: {key: (verified[side] or {}).get(key) for key in (
            "team_id", "player_count", "observed_at", "source_type", "raw_response_sha256", "parser_version"
        )} for side in ("away", "home")},
        "scope": preflight.get("scope"),
    }
    context["current_roster_identity_coverage"] = {
        "status": "verified",
        "teams": {side: (teams[side] or {}).get("current_roster_identity_coverage") for side in ("away", "home")},
    }
    return context
