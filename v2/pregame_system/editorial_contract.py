"""Editorial-safe projection and structural review binding; no prose phrase matcher."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from .ats import ATS_EPSILON
from .storage import clean, content_hash
from .editorial_hard_facts import validate_publication_hard_facts

PROTOCOL = "editorial-publication-v1"
DECISION_RECEIPT_PROTOCOL = "editorial-publication-v2"
DECISION_PROTOCOL = "editorial-decision-v2"


def _dict(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def _writer_safe_analysis_core(core):
    """Keep analysis structure for the Writer, but isolate internal model labels as audit-only."""
    value = clean(core or {})
    hidden = []

    def has_internal_label(text):
        return any(pattern.search(str(text or "")) for _label, pattern in _INTERNAL_MODEL_TERMS)

    def clean_text(parent, key, path):
        text = parent.get(key)
        if isinstance(text, str) and has_internal_label(text):
            parent[key] = None
            hidden.append(path)

    clean_text(value, "central_question", "central_question")
    clean_text(value, "expected_game_script", "expected_game_script")
    thesis = value.get("central_thesis")
    if isinstance(thesis, dict):
        clean_text(thesis, "statement", "central_thesis.statement")
        if thesis.get("statement") is None:
            value["central_thesis"] = None
    matchups = []
    for index, matchup in enumerate(value.get("key_matchups") or []):
        if any(has_internal_label(matchup.get(key)) for key in (
            "subject", "opponent_or_scheme", "why_it_matters", "expected_effect", "uncertainty"
        )):
            hidden.append(f"key_matchups[{index}]")
        else:
            matchups.append(matchup)
    value["key_matchups"] = matchups
    if not matchups and value.get("matchup_status") == "identified":
        value["matchup_status"] = "no_clear_matchup"
    conditions = []
    for index, condition in enumerate(value.get("flip_conditions") or []):
        if any(has_internal_label(condition.get(key)) for key in ("condition", "judgment_effect")):
            hidden.append(f"flip_conditions[{index}]")
        else:
            conditions.append(condition)
    value["flip_conditions"] = conditions
    return value, hidden


def _decode_compact_row(value, columns):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (list, tuple)) and columns:
        return {name: item for name, item in zip(columns, value)}
    return {}


def _finite_projection_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _writer_market_interpretation(forecast_provenance, locked_prediction):
    """Expose precomputed ATS plus a deterministic total-line comparison to the Writer."""
    comparison = forecast_provenance.get("spread_comparison") or {}
    market = comparison.get("market") or {}
    final = comparison.get("final") or {}
    quote_id = market.get("market_quote_id")

    model_home_margin = _finite_projection_number(final.get("model_home_margin"))
    market_implied_home_margin = _finite_projection_number(final.get("market_implied_home_margin"))
    spread_edge = _finite_projection_number(final.get("spread_edge"))
    ats_lean = final.get("ats_lean")
    spread_available = (
        comparison.get("status") == "available"
        and market.get("status") == "available"
        and market.get("spread_status") == "available"
        and final.get("status") == "available"
        and model_home_margin is not None
        and market_implied_home_margin is not None
        and spread_edge is not None
        and ats_lean in {"home", "away", "no_edge"}
    )
    spread = {
        "status": "available" if spread_available else "unavailable",
        "margin_perspective": "home_minus_away",
        "model_home_margin": model_home_margin if spread_available else None,
        "market_implied_home_margin": market_implied_home_margin if spread_available else None,
        "spread_edge": spread_edge if spread_available else None,
        "ats_lean": ats_lean if spread_available else "unavailable",
        "ats_lean_reference": clean(final.get("ats_lean_reference")) if spread_available else None,
        "market_quote_id": quote_id,
    }

    model_total = _finite_projection_number(locked_prediction.get("expected_total"))
    market_total = (_finite_projection_number(market.get("total"))
                    if market.get("status") == "available" else None)
    total_available = model_total is not None and market_total is not None
    total_edge = round(model_total - market_total, 10) if total_available else None
    if total_edge is None:
        total_lean = "unavailable"
    elif abs(total_edge) <= ATS_EPSILON:
        total_edge = 0.0
        total_lean = "aligned"
    else:
        total_lean = "over" if total_edge > 0 else "under"
    total = {
        "status": "available" if total_available else "unavailable",
        "model_total": model_total,
        "market_total": market_total,
        "total_edge": total_edge,
        "total_lean": total_lean,
        "market_quote_id": quote_id,
    }
    return {"schema_version": "writer-market-interpretation-v1",
            "spread": spread, "total": total}


def _historical_player_scope(profile, *, historical_team_profile, columns):
    stints_raw = profile.get("previous_team_stints") or []
    stint_columns = columns.get("previous_team_stints") or []
    stints = [_decode_compact_row(item, stint_columns) for item in stints_raw]
    stints = [row for row in stints if row.get("team_id") is not None]
    stints_status = profile.get("previous_team_stints_status")

    if historical_team_profile:
        # This list is keyed by a historical team profile, unlike
        # previous_season_players which is grouped by the current roster team.
        explicit = profile.get("team_id")
        observed_ids = [int(explicit)] if explicit is not None else []
        return {
            "last_season_team_ids": observed_ids,
            "last_season_team_ids_status": "available" if observed_ids else "unavailable",
            "team_id_last_season": observed_ids[0] if observed_ids else None,
            "statistics_scope": "single_team" if observed_ids else "unknown",
            "statistics_team_ids": observed_ids,
            "previous_team_stints": stints,
        }

    observed_ids = sorted({int(row["team_id"]) for row in stints})
    current_row = _decode_compact_row(
        profile.get("current_team_last_season_stint"), columns.get("history") or []
    )
    current_stint_team = current_row.get("team_id")
    if current_stint_team is not None:
        current_stint_team = int(current_stint_team)
        if current_stint_team not in observed_ids:
            observed_ids.append(current_stint_team)
            observed_ids.sort()

    complete = stints_status == "available"
    last_team = observed_ids[0] if complete and len(observed_ids) == 1 else None
    if current_stint_team is not None:
        scope, stats_ids = "single_team", [current_stint_team]
    elif complete and len(observed_ids) == 1:
        scope, stats_ids = "single_team", observed_ids
    elif complete and len(observed_ids) > 1:
        scope, stats_ids = "all_observed_teams", observed_ids
    else:
        scope, stats_ids = "unknown", []
    return {
        "last_season_team_ids": observed_ids,
        "last_season_team_ids_status": stints_status or ("partial" if observed_ids else "unavailable"),
        "team_id_last_season": last_team,
        "statistics_scope": scope,
        "statistics_team_ids": stats_ids,
        "previous_team_stints": stints,
    }


def build_editorial_projection(context, locked_prediction, analyst_reasoning=None, *, forecast_provenance=None):
    """Project frozen evidence, never produce new basketball facts with an LLM."""
    c = _dict(context)
    game = c.get("game") or {}
    evidence = c.get("objective_evidence") or []
    analyst_reasoning = list(analyst_reasoning or [])
    if forecast_provenance is None:
        forecast_provenance = next((item.get("value") for item in analyst_reasoning
            if item.get("_contract_item") == "forecast_provenance"), None)
    decision_mode = forecast_provenance is not None
    by_section = {}
    for e in evidence:
        by_section.setdefault(e.get("section"), []).append(e)
    facts, omitted = [], []
    def add(kind, value, refs=(), path="", game_specific=False, visibility="public"):
        if value is None or value == {} or value == []:
            return
        row = {"id": f"f{len(facts)+1:03d}", "kind": kind, "value": clean(value),
               "source_evidence_ids": list(refs), "source_path": path, "game_specific": game_specific}
        if decision_mode:
            row["visibility"] = visibility
        facts.append(row)
    def refs(section):
        return [e["id"] for e in by_section.get(section, []) if e.get("id")]
    identity = {k: game.get(k) for k in ("game_id", "home", "away", "scheduled_tipoff", "cutoff",
                "venue", "neutral_site", "game_type", "schedule_revision")}
    if identity["game_id"] is None:
        identity["game_id"] = next((e.get("game_id") for e in evidence if e.get("game_id")), None)
    # Team home/away designation and venue are distinct facts.
    add("GAME", identity, refs("game_identity"), "/game", True)
    event = game.get('season_event') or {}
    if event.get('verified') is True and event.get('source_url'):
        add('GAME', {'season_event':event}, refs('game_identity'), '/game/season_event', True)
    tipoff = game.get("scheduled_tipoff") or game.get("tipoff")
    if tipoff:
        stamp = datetime.fromisoformat(tipoff.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("editorial_tipoff_timezone_missing")
        add("GAME", {"tipoff_beijing": stamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M"),
                     "display_timezone": "Asia/Shanghai"}, refs("game_identity"), "/game/scheduled_tipoff", True)
    if decision_mode:
        add("MODEL_OUTPUT", {"owner": "pure_baseline", "numbers": c.get("pure_prediction"),
                             "probability_perspective": "home_unless_declared"},
            refs("prediction"), "/pure_prediction", visibility="audit_only")
        public_numbers = {k: v for k, v in locked_prediction.items() if k != "evidence_ids"}
        add("PUBLIC_DECISION", {"numbers": public_numbers, "perspective": "winner_positive"},
            locked_prediction.get("evidence_ids", refs("prediction")), "/locked_forecast/final_prediction")
    else:
        add("MODEL_OUTPUT", {"owner": "pure_baseline", "numbers": c.get("pure_prediction"),
                            "probability_perspective": "home_unless_declared"},
            refs("prediction"), "/pure_prediction")
        add("MODEL_OUTPUT", {"owner": "locked_final", "numbers": locked_prediction,
                            "probability_perspective": "winner", "margin_perspective": "winner_positive"},
            locked_prediction.get("evidence_ids", refs("prediction")), "/locked_forecast/final_prediction")
    phase = c.get("season_phase")
    if phase:
        add("BACKGROUND", {"season_phase": phase}, refs("season_phase"), "/season_phase")
    for index, item in enumerate(analyst_reasoning):
        if item.get("_contract_item") == "forecast_provenance":
            continue
        refs_for_item = [ref for ref in item.get("evidence_ids", []) if ref in c.get("evidence_index", {})]
        reasoning_text = item.get("summary") or ""
        internal_layer = bool(re.search(r"(?i)(?<![A-Za-z0-9])(?:Pure|Residual|Official Numeric Forecast|ML)(?![A-Za-z0-9])|纯篮球预测|纯篮球模型", reasoning_text))
        add("ANALYST_REASONING", {"statement": item.get("summary"), "effect": item.get("effect"),
                                  "evidence_ids": refs_for_item,
                                  **({"role": "supporting_context"} if decision_mode else {}),
                                  **({"pregame_plan": item["pregame_plan"]} if item.get("pregame_plan") else {}),
                                  "scope": "existing basketball analysis material; subjective/inferential, not objective fact or ML feature attribution"},
            refs_for_item, f"/locked_forecast/review/factors/{index}",
            visibility="audit_only" if decision_mode and internal_layer else "writer_internal" if decision_mode else "public")
    if decision_mode:
        core = forecast_provenance.get("analysis_core") or {}
        safe_core, hidden_core_fields = _writer_safe_analysis_core(core)
        core_refs = set((core.get("central_thesis") or {}).get("evidence_ids", []))
        for matchup in core.get("key_matchups", []):
            core_refs.update(matchup.get("evidence_ids", []))
        for condition in core.get("flip_conditions", []):
            core_refs.update(condition.get("evidence_ids", []))
        if hidden_core_fields:
            add("DECISION_CORE_AUDIT", {"analysis_core": core, "writer_hidden_fields": hidden_core_fields},
                [ref for ref in sorted(core_refs) if ref in c.get("evidence_index", {})],
                "/locked_forecast/final_analysis_core", visibility="audit_only")
        add("DECISION_CORE", safe_core, [ref for ref in sorted(core_refs) if ref in c.get("evidence_index", {})],
            "/locked_forecast/final_analysis_core", visibility="writer_internal")
        revision = {k: forecast_provenance.get(k) for k in (
            "independent_prediction", "final_prediction", "revision_delta", "market_provenance")
            if forecast_provenance.get(k) is not None}
        revision["market_interpretation"] = _writer_market_interpretation(
            forecast_provenance, locked_prediction
        )
        add("REVISION_PROVENANCE", revision, path="/locked_forecast/revision_provenance",
            visibility="writer_internal")
    add("UNKNOWN", {"topic": "model_explanation", "status": "unavailable",
                   "meaning": "Model output, training cutoff and rolling input composition do not identify feature contributions or model causality."},
        refs("model_facts") or refs("prediction"), "/model_explanation",
        visibility="audit_only" if decision_mode else "public")
    for e in by_section.get("model_facts", []):
        m = e.get("value") or {}
        asset = m.get("runtime_asset_identity") or {}
        metadata = {k: asset[k] for k in ("model_version", "training_cutoff", "asset_identity_hash",
                     "training_games", "training_samples", "training_sample_game_count") if k in asset}
        if asset.get("training_cutoff"):
            stamp = datetime.fromisoformat(asset["training_cutoff"].replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                metadata["training_cutoff_beijing"] = stamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
        add("MODEL_IDENTITY", metadata,
            [e["id"]], "/objective_evidence/model_facts", visibility="audit_only" if decision_mode else "public")
    for e in by_section.get("web_context", []):
        v = e.get("value") or {}
        if not v.get("eligible_for_fact"):
            omitted.append({"id": e.get("id"), "reason": "not_qualified_fact"})
            continue
        ref = str(v.get("game_reference") or "")
        if ((v.get("game_id") and v["game_id"] != identity["game_id"])
                or (ref.startswith("espn:") and ref != identity["game_id"])):
            omitted.append({"id": e.get("id"), "reason": "other_game_claim"})
            continue
        if v.get("market_reference"):
            add("MARKET", v["market_reference"], [e["id"]], "/objective_evidence/web_context/market_reference", True)
            continue
        assertion = v.get("assertion_kind")
        if assertion not in {"confirmed_fact", "reported_projection", "source_opinion"}:
            omitted.append({"id": e.get("id"), "reason": "legacy_source_without_claim_scope"})
            continue
        claim_type = str(v.get("claim_type") or "").casefold()
        transaction = assertion == "confirmed_fact" and any(word in claim_type for word in
            ("transaction", "trade", "signing", "roster_move", "contract_move"))
        kind = ("TRANSACTION" if transaction else "SOURCE_OPINION" if assertion == "source_opinion" else "PROJECTED" if assertion == "reported_projection"
                else {"BACKGROUND": "BACKGROUND", "CURRENT": "CURRENT", "LIVE": "LIVE"}.get(v.get("temporal_class"), "BACKGROUND"))
        add(kind, {k: v.get(k) for k in ("quote", "snippet", "claim_type", "team_id", "assertion_kind",
            "temporal_scope", "published_at", "publication_precision", "captured_at", "title", "source_role",
            "game_reference", "game_id", "player_id", "player_name", "status")},
            [e["id"]], "/objective_evidence/web_context",
            bool(v.get("game_reference") or (v.get("game_id") and v.get("game_id") == identity["game_id"])))
        # Do not duplicate identical snippet/quote in the model input.
        facts[-1]["value"].pop("snippet", None) if v.get("quote") else None
    if c.get("market_benchmark"):
        add("MARKET", c["market_benchmark"], refs("market") or refs("prediction"), "/market_benchmark", True)
    for e in by_section.get("availability", []):
        v = e.get("value") or {}
        for row in v.get("records") or []:
            add("LIVE", {k: row.get(k) for k in ("player_id", "player_name", "team_id", "status", "source_published_at", "captured_at", "game_id")},
                [e["id"]], "/objective_evidence/availability/records", row.get("game_id") == identity["game_id"])
    roster = c.get("roster_context") or {}
    for side in ("home", "away"):
        team = (roster.get("teams") or {}).get(side) or {}
        current = (team.get("official_team_roster") or team.get("current_team_roster")
                   or team.get("roster") or {})
        if current.get("status") == "available":
            add("CURRENT", {"side": side, "team_id": team.get("team_id"), "roster_snapshot": current.get("players"),
                           "captured_at": (current.get("source") or {}).get("captured_at"),
                           "roster_basis": team.get("roster_basis") or current.get("roster_basis"),
                           "source_type": (current.get("source") or {}).get("source_type"),
                           "scope": "source-listed current team membership; not final playing roster, health or starters"},
                refs("roster_context"), f"/roster_context/teams/{side}/current_roster")
        else:
            add("UNKNOWN", {"topic": "current_roster", "side": side, "status": "unavailable"},
                refs("roster_context"), f"/roster_context/teams/{side}/current_roster")
        continuity = team.get("continuity") or {}
        if continuity.get("status") in {"available", "provisional_roster_membership"}:
            add("CURRENT", {"side": side, "continuity": {k: continuity.get(k) for k in
                ("status", "retained_minutes_ratio", "top8_retained_count", "top8_retained_minutes_ratio", "roster_identity_coverage")}},
                refs("roster_context"), f"/roster_context/teams/{side}/continuity")
        else:
            add("UNKNOWN", {"topic": "roster_continuity", "side": side, "status": "unavailable"},
                refs("roster_context"), f"/roster_context/teams/{side}/continuity")
        starters = team.get("confirmed_starters")
        if starters is None:
            starters = continuity.get("confirmed_starters")
        if starters:
            add("CURRENT", {"side": side, "team_id": team.get("team_id"),
                            "confirmed_starters": clean(starters), "status": "confirmed"},
                refs("roster_context"), f"/roster_context/teams/{side}/confirmed_starters")
        cold = team.get("cold_start") or {}
        add("OBSERVATION", {"side": side, "local_window_composition": {k: cold.get(k) for k in (
            "current_season_games_observed", "current_season_games_confirmed", "current_season_coverage",
            "rolling_5_current_season_games", "rolling_5_previous_season_games", "rolling_15_current_season_games",
            "rolling_15_previous_season_games", "window_provenance")},
            "scope": "local competitive-history observations; not proof of global preseason appearances or season opener"},
            refs("roster_context"), f"/roster_context/teams/{side}/cold_start")
    history = c.get("matchup_history") or {}
    meetings = history.get("meetings") or []
    add("HISTORICAL", {"total_local_meeting_count": history.get("meeting_count"),
        "provided_meeting_rows": len(meetings), "meetings": meetings[-10:], "series_summary": history.get("series_summary"),
        "scope": "past recorded games; past opponent membership does not confirm this-game reunion"},
        refs("matchup_history"), "/matchup_history")
    form = c.get("season_form") or {}
    for side in ("home", "away"):
        team = (form.get("teams") or {}).get(side) or {}
        for key in ("previous_last10", "previous_playoffs", "current_last10"):
            row = team.get(key) or {}
            if row.get("status") == "available":
                add("HISTORICAL" if key.startswith("previous") else "OBSERVATION",
                    {"side": side, "sample": key, "season_start": (form.get("season_start")-1
                         if key.startswith("previous") and isinstance(form.get("season_start"), int) else form.get("season_start")),
                     "competition_scope": form.get("competition_scope"),
                     "metrics": {k: v for k, v in row.items() if k not in {"game_ids", "semantics"}},
                     "scope": "window/competition/sample labels must remain attached to numbers"},
                    refs("season_form"), f"/season_form/teams/{side}/{key}")
    # Rolling values and their sample definitions come from the same frozen
    # feature cutoff. Older payloads retain only fields they actually recorded.
    fact_context_evidence = next(iter(by_section.get("fact_context", [])), None)
    frozen_fact_context = (fact_context_evidence or {}).get("value") or {}
    new_team_form = frozen_fact_context.get("team_form") or {}
    if frozen_fact_context.get("schedule"):
        add("SCHEDULE", {
            "feature_cutoff": frozen_fact_context.get("feature_cutoff"),
            "teams": frozen_fact_context["schedule"],
            "scope": "cutoff-qualified schedule records only; absence is unknown when calendar coverage is incomplete",
        }, [fact_context_evidence["id"]] if fact_context_evidence and fact_context_evidence.get("id") else [],
            "/objective_evidence/fact_context/schedule")
    for e in by_section.get("team_form", []):
        raw = e.get("value") or {}
        for side in ("home", "away"):
            window = new_team_form.get(side)
            if isinstance(window, dict):
                counts = window.get("season_counts") or {}
                try:
                    target_season = int(game.get("season_start"))
                except (TypeError, ValueError):
                    target_season = None
                previous_year = str(target_season - 1) if target_season is not None else None
                current_year = str(target_season) if target_season is not None else None
                only_previous = bool(counts) and previous_year is not None and set(counts) == {previous_year}
                rolling_kind = "HISTORICAL" if only_previous else "OBSERVATION"
                all_metrics = window.get("metrics") or {}
                verified_metrics = {name: metric for name, metric in all_metrics.items()
                                    if not isinstance(metric, dict) or metric.get("validation") != "mismatch"}
                fact_refs = [e["id"]] + ([fact_context_evidence["id"]]
                    if fact_context_evidence and fact_context_evidence.get("id") else [])
                add(rolling_kind, {
                    "side": side, "sample": "rolling_15",
                    "requested_games": window.get("requested_games"),
                    "observed_games": window.get("observed_games"),
                    # `games` remains the requested window size for old parser
                    # consumers; actual observed and per-metric N are separate.
                    "games": window.get("requested_games"),
                    "season_counts": counts,
                    "competition_counts": window.get("competition_counts"),
                    "first_game_date": window.get("first_game_date"),
                    "last_game_date": window.get("last_game_date"),
                    "date_timezone": window.get("date_timezone"),
                    "feature_cutoff": frozen_fact_context.get("feature_cutoff"),
                    "metrics": verified_metrics,
                    "status": window.get("status"), "reason": window.get("reason"),
                    "scope": "rolling window metadata from the exact feature input; not an ML attribution",
                }, fact_refs,
                    f"/objective_evidence/fact_context/team_form/{side}")
                for name, metric in all_metrics.items():
                    if not isinstance(metric, dict) or metric.get("validation") != "mismatch":
                        continue
                    add("UNKNOWN", {
                        "side": side,
                        "topic": "rolling_metric_value_sample_mismatch",
                        "diagnostic_metric": {
                            "name": name, "value": metric.get("value"),
                            "requested_games": window.get("requested_games"),
                            "observed_games": window.get("observed_games"),
                            "valid_games": metric.get("valid_games"),
                            "missing_games": metric.get("missing_games"),
                            "validation": metric.get("validation"),
                            "method": metric.get("method"),
                        },
                        "scope": "original rolling value retained for diagnosis; not a verified sample statistic",
                    }, fact_refs, f"/objective_evidence/fact_context/team_form/{side}/metrics/{name}")
                continue
            # Legacy frozen fallback. Never use current season_phase to invent
            # competition composition or per-metric valid counts.
            window = (((roster.get("teams") or {}).get(side) or {}).get("cold_start") or {})
            current_games = window.get("rolling_15_current_season_games")
            previous_games = window.get("rolling_15_previous_season_games")
            season_start = form.get("season_start") if isinstance(form.get("season_start"), int) else None
            if current_games == 0 and previous_games == 15 and season_start is not None:
                rolling_kind, rolling_season = "HISTORICAL", season_start - 1
            elif previous_games == 0 and current_games == 15:
                rolling_kind, rolling_season = "OBSERVATION", season_start
            else:
                rolling_kind, rolling_season = "OBSERVATION", None
            add(rolling_kind, {"side": side, "sample": "rolling_15",
                "requested_games": 15 if current_games is not None and previous_games is not None else None,
                "observed_games": window.get("rolling_15_observed_games"),
                "games": 15 if current_games is not None and previous_games is not None
                    and current_games + previous_games == 15 else None,
                "season_start": rolling_season, "current_season_games": current_games,
                "previous_season_games": previous_games,
                "metrics": (raw.get(side) or {}),
                "scope": "legacy rolling input; competition composition and valid N were not recorded"},
                [e["id"]], f"/objective_evidence/team_form/{side}")
    pk = roster.get("player_knowledge") or {}
    columns = pk.get("column_definitions") or {}
    for side in ("home", "away"):
        team = (pk.get("teams") or {}).get(side) or {}
        historical_profiles = team.get("historical_team_leaders") or []
        profiles = historical_profiles or team.get("previous_season_players") or []
        compact = []
        for p in profiles[:8]:
            row = p.get("history") or p.get("current_team_last_season_stint") or p.get("previous_season_total")
            if isinstance(row, dict) and row.get("same_as"):
                row = p.get("current_team_last_season_stint")
            decoded = dict(zip(columns.get("history", []), row)) if isinstance(row, list) else p
            averages = decoded.get("per_observed_game") or []
            names = columns.get("per_observed_game") or []
            stats = dict(zip(names, averages)) if isinstance(averages, list) else averages
            historical_scope = _historical_player_scope(
                p, historical_team_profile=bool(historical_profiles), columns=columns,
            )
            compact.append({"player": p.get("player_name"), "player_id": p.get("player_id"),
                **historical_scope,
                "history_status": p.get("previous_season_history_status"),
                "history_reason": p.get("previous_season_history_reason"),
                "average_statistics": {k: stats.get(k) for k in ("points", "rebounds", "assists") if stats.get(k) is not None},
                "stat_sample_counts": {k: v for k,v in zip(names,decoded.get("observed_games_by_statistic") or [])
                                       if k in {"points","rebounds","assists"}}})
        add("HISTORICAL", {"side": side, "season": pk.get("season"), "scope": "historical statistics scope is explicit; not current roster, rotation, starters or health",
                          "players": compact}, refs("roster_context"), f"/roster_context/player_knowledge/teams/{side}/historical_team_leaders")
    unknown_coverage = next((e.get("value", {}).get("coverage") for e in by_section.get("research_status", [])), {}) or {}
    for key in sorted(unknown_coverage):
        status = unknown_coverage[key]
        if status.get("status") != "FOUND":
            add("UNKNOWN", {"topic": key, "status": status.get("status") or "UNKNOWN",
                           "unknown_reason": status.get("unknown_reason"), "last_checked": status.get("last_checked"),
                           "scope": "not established by the frozen research package; not proof no real-world announcement exists"},
                refs("research_status"), "/objective_evidence/research_status/coverage/" + key)
    if not by_section.get("availability"):
        add("UNKNOWN", {"topic": "availability", "status": "unavailable", "scope": "no qualified structured records"},
            refs("roster_context") or refs("prediction"), "/availability")
    # Only audit metadata is omitted; it remains intact in frozen context/Research.
    body = {"protocol": DECISION_PROTOCOL if decision_mode else PROTOCOL,
            "game_id": identity["game_id"], "game": identity, "facts": facts,
            "locked_prediction": clean(locked_prediction),
            "limits": {"model_explanation": "unavailable", "no_external_knowledge_as_fact": True,
                       "unknown_is_not_negative_world_fact": True},
            "source_context_sha256": content_hash(c),
            "omitted": omitted + [{"path": p, "reason": "audit metadata/duplicate or unqualified model input; retained in source snapshot"}
                for p in ("research.sources_checked", "raw_fetches", "forecasting.free_text_factors", "matchup.capped_rest_features",
                          "duplicate_top_level_context", "unused_player_history_statistics")]}
    if decision_mode:
        body["visibility"] = {"game": "public", "locked_prediction": "public",
            "facts": "per_fact_visibility", "limits": "writer_internal",
            "source_context_sha256": "audit_only", "omitted": "audit_only",
            "projection_sha256": "audit_only"}
        body["visibility_policy"] = {"public": "may support user-facing explanation",
            "writer_internal": "may guide reasoning but is not itself a public fact or required wording",
            "audit_only": "retained for audit and excluded from Writer input"}
    if game.get('player_display_names'):
        body['player_display_names']=[{k:r[k]for k in ('canonical_name_en','display_name_zh')if k in r}
            for r in game['player_display_names']if isinstance(r,dict)]
    if (c.get('evidence_quality')or{}).get('availability_unconfirmed'):
        body['limits']['unconfirmed_player_availability']=True
    body["projection_sha256"] = content_hash(body)
    return clean(body)


def model_view(projection):
    """Facts once; origin hashes/paths stay in the frozen audit projection."""
    def compact(value):
        if isinstance(value, dict):
            return {k: compact(v) for k, v in value.items()
                    if k not in {"scope", "logo_url", "accent", "evidence_ids"}}
        if isinstance(value, list): return [compact(v) for v in value]
        return value
    decision_mode = projection.get("protocol") == DECISION_PROTOCOL
    facts = [f for f in projection["facts"] if not (decision_mode and f.get("visibility") == "audit_only")]
    if decision_mode:
        scope_legend = {"GAME": "verified matchup and scheduled context",
            "DECISION_CORE": "the current basketball question, matchup, expected path and conditions that could change it",
            "REVISION_PROVENANCE": "how the independent view, observed market signal and final view relate; timing limits remain explicit",
            "ANALYST_REASONING": "supporting context beneath the Decision Core, not a peer-level competing conclusion",
            "OBSERVATION": "local competitive-history sample and coverage",
            "HISTORICAL": "past season/sample context, not current roster, role or health",
            "BACKGROUND": "qualified season background; use only the supplied claim",
            "CURRENT": "current roster/status snapshot, not confirmed game availability",
            "LIVE": "qualified game-specific status",
            "PROJECTED": "reported projection, not confirmed starter",
            "SOURCE_OPINION": "source viewpoint, not a proven ranking",
            "UNKNOWN": "information not established in the frozen evidence"}
    else:
        scope_legend = {"GAME": "structured matchup/venue/time, never home-city inference",
            "MODEL_OUTPUT": "output only, not model causality; probability/number owner remains explicit",
            "ANALYST_REASONING": "basketball analysis material; interpretation, not source fact or ML attribution",
            "OBSERVATION": "local competitive-history window/coverage, NOT global played-games or preseason-opener proof",
            "HISTORICAL": "named past season/sample/players, NOT current roster/role/health",
            "BACKGROUND": "qualified season background; exact quoted claim only",
            "CURRENT": "current snapshot, NOT final playing roster or health",
            "LIVE": "qualified named game-specific status only",
            "PROJECTED": "reported projection, NOT confirmed starter",
            "SOURCE_OPINION": "source viewpoint, NOT proven ranking",
            "UNKNOWN": "frozen evidence gap, NOT absence of real-world injury/announcement"}
    view = {"protocol": projection["protocol"], "game_id": projection["game_id"],
        "locked_prediction": {k: v for k, v in projection["locked_prediction"].items() if k != "evidence_ids"},
        "facts": [{**{"id": f["id"], "kind": f["kind"], "value": compact(f["value"]), "game_specific": f["game_specific"]},
                   **({"visibility": f["visibility"]} if decision_mode else {})}
                  for f in facts],
        "scope_legend": scope_legend,
        "limits": projection["limits"]}
    if decision_mode:
        view["visibility"] = {"game": "public", "locked_prediction": "public",
            "facts": "public_or_writer_internal", "limits": "writer_internal"}
        view["visibility_policy"] = projection["visibility_policy"]
    return view


_INTERNAL_MODEL_TERMS = (
    ("Pure", re.compile(r"(?i)(?<![A-Za-z0-9])Pure(?![A-Za-z0-9])")),
    ("Residual", re.compile(r"(?i)(?<![A-Za-z0-9])Residual(?![A-Za-z0-9])")),
    ("ML", re.compile(r"(?i)(?<![A-Za-z0-9])ML(?![A-Za-z0-9])")),
    ("Official Numeric Forecast", re.compile(r"(?i)Official\s+Numeric\s+Forecast")),
    ("纯篮球预测", re.compile(r"纯篮球预测")),
    ("纯篮球模型", re.compile(r"纯篮球模型")),
    ("内部模型", re.compile(r"内部模型")),
)


def public_visibility_issues(judgment):
    """Reject internal prediction-layer names in public authored text only."""
    value = _dict(judgment)
    public_fields = ("subjective_rationale", "analysis_article", "pregame_story", "watch_themes",
                     "roster_analysis", "player_analysis", "cold_start_analysis", "historical_data_relevance",
                     "key_matchup_variables", "supporting_evidence", "conflicting_evidence", "uncertainty",
                     "prediction_perspective", "core_question", "game_script", "model_risk", "central_thesis")
    texts = []
    def walk(item):
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict):
            for key, child in item.items():
                if key not in {"player_name", "evidence_ids", "fact_ids"}:
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
    for key in public_fields:
        walk(value.get(key))
    joined = "\n".join(texts)
    return [label for label, pattern in _INTERNAL_MODEL_TERMS if pattern.search(joined)]


def visible_units(judgment):
    """Complete public text coverage. Sentence boundaries, not semantic keywords."""
    import re
    j = _dict(judgment)
    units, seen = [], {}
    def add(location, text, span=None):
        if not isinstance(text, str) or not text.strip(): return
        if text in seen and span is None:
            seen[text]["aliases"].append(location)
        else:
            unit = {"id": location, "text": text, "aliases": [location]}
            if span: unit["article_span"] = span
            seen[text] = unit
            units.append(unit)
    for field in ("subjective_rationale", "core_question", "game_script", "model_risk"):
        add(field, j.get(field))
    thesis = j.get("central_thesis") or {}
    add("central_thesis.statement", thesis.get("statement"))
    for field in ("pregame_story", "prediction_perspective"):
        for key in ("title", "statement"):
            add(field + "." + key, (j.get(field) or {}).get(key))
    thesis = j.get("central_thesis") or {}
    add("central_thesis.statement", thesis.get("statement"))
    for field in ("watch_themes", "roster_analysis", "player_analysis", "cold_start_analysis",
                  "historical_data_relevance", "key_matchup_variables", "supporting_evidence",
                  "conflicting_evidence", "uncertainty"):
        for index, claim in enumerate(j.get(field) or []):
            for key in ("title", "statement"):
                add(f"{field}.{index}.{key}", claim.get(key))
    article = j.get("analysis_article") or ""
    for index, match in enumerate(re.finditer(r"[^。！？!?]+[。！？!?]?", article)):
        raw = match.group()
        left = len(raw)-len(raw.lstrip())
        right = len(raw.rstrip())
        if right > left:
            add(f"article.{index}", raw[left:right], {"start": match.start()+left, "end": match.start()+right})
    return units


def verify_hard_fact_candidates(review, deterministic_findings):
    """Quality-review candidates escalate only when a deterministic check confirms them."""
    confirmed = []
    for candidate in (review or {}).get("possible_hard_fact_conflicts", []):
        quote = candidate.get("quote") or ""
        code = candidate.get("code")
        match = next((issue for issue in deterministic_findings
                      if issue.get("code") == code and quote and quote in issue.get("quote", "")), None)
        if match:
            confirmed.append(match)
    return confirmed


def make_publication_receipt(judgment, context, projection, analysis_material=None,
                             hard_fact_findings=None, quality_review=None,
                             quality_review_input_sha256=None):
    """Bind the factual publication checks; quality feedback is informational."""
    j, c = _dict(judgment), _dict(context)
    material = clean(analysis_material or [])
    findings = clean(hard_fact_findings or [])
    if findings:
        raise ValueError("publication_hard_fact_blocker")
    quality = clean(quality_review or {"status": "disabled", "warnings": [], "possible_hard_fact_conflicts": []})
    if quality.get("status") not in {"disabled", "passed", "warning", "hold", "unavailable", "budget_unavailable", "invalid_response"}:
        raise ValueError("publication_quality_status_invalid")
    decision_mode = projection.get("protocol") == DECISION_PROTOCOL
    visibility_issues = public_visibility_issues(j) if decision_mode else []
    if visibility_issues:
        raise ValueError("publication_internal_model_layer_leak:" + ",".join(visibility_issues))
    receipt = {"protocol": DECISION_RECEIPT_PROTOCOL if decision_mode else PROTOCOL, "status": "eligible",
        "game_id": projection["game_id"], "judgment_sha256": content_hash(j),
        "article_sha256": content_hash(j.get("analysis_article") or ""),
        "prediction_sha256": content_hash(j["final_prediction"]),
        "context_sha256": content_hash(c), "evidence_sha256": content_hash(c.get("objective_evidence") or []),
        "analysis_material_sha256": content_hash(material), "projection_sha256": content_hash(projection),
        "hard_fact_checks": {"status": "passed", "findings": findings},
        "quality_review": quality, "quality_review_sha256": content_hash(quality),
        "quality_review_input_sha256": quality_review_input_sha256,
        "scope": "Required schema, identity, citations, locked prediction, structured hard-fact checks and bindings passed; quality feedback is non-blocking and does not certify every sentence."}
    if decision_mode:
        receipt["visibility_checks"] = {"status": "passed", "findings": []}
    return receipt


def verify_publication_receipt(payload):
    receipt = payload.get("publication_receipt")
    if not isinstance(receipt, dict) or receipt.get("protocol") not in {PROTOCOL, DECISION_RECEIPT_PROTOCOL}:
        raise ValueError("publication_receipt_missing")
    if receipt.get("status") != "eligible":
        raise ValueError("publication_not_eligible")
    j, c = payload.get("judgment") or {}, payload.get("analyst_context") or {}
    if payload.get("quality_review_input_sha256") != receipt.get("quality_review_input_sha256"):
        raise ValueError("publication_quality_input_binding_mismatch")
    if payload.get("prediction") != j.get("final_prediction"):
        raise ValueError("editorial_forecast_locked_violation")
    try:
        from .analyst import AnalystContext, AnalystJudgment, validate_judgment
        unknown_fields = set(j) - set(AnalystJudgment.model_fields)
        if unknown_fields:
            raise ValueError("judgment_schema_unknown_fields")
        validated = validate_judgment(AnalystJudgment.model_validate(j), AnalystContext.model_validate(c))
    except Exception as exc:
        raise ValueError("publication_schema_or_citation_invalid") from exc
    normalized = validated.model_dump()
    if any(normalized.get(key) != value for key, value in j.items() if key in normalized):
        raise ValueError("publication_output_not_normalized")
    if payload.get("objective_evidence") is not None and payload.get("objective_evidence") != (c.get("objective_evidence") or []):
        raise ValueError("publication_evidence_mismatch")
    material = payload.get("editorial_material") or []
    projection = build_editorial_projection(c, j["final_prediction"], material)
    decision_mode = projection.get("protocol") == DECISION_PROTOCOL
    if payload.get("editorial_projection") != projection:
        raise ValueError("editorial_projection_mismatch")
    if projection["game_id"] != payload.get("game_id") or receipt.get("game_id") != payload.get("game_id"):
        raise ValueError("publication_game_mismatch")
    findings = validate_publication_hard_facts(j, projection)
    if findings:
        raise ValueError("publication_hard_fact_blocker:" + json.dumps(findings, ensure_ascii=False))
    visibility_issues = public_visibility_issues(j) if decision_mode else []
    if visibility_issues or (decision_mode and receipt.get("visibility_checks") != {"status": "passed", "findings": []}):
        raise ValueError("publication_internal_model_layer_leak")
    if receipt.get("hard_fact_checks") != {"status": "passed", "findings": []}:
        raise ValueError("publication_hard_fact_receipt_invalid")
    expected = make_publication_receipt(j, c, projection, material, [], receipt.get("quality_review"),
                                       quality_review_input_sha256=receipt.get("quality_review_input_sha256"))
    check_keys = ["protocol", "status", "game_id", "judgment_sha256", "article_sha256", "prediction_sha256", "context_sha256",
                "evidence_sha256", "analysis_material_sha256", "projection_sha256", "hard_fact_checks",
                "quality_review_sha256", "quality_review_input_sha256"]
    if decision_mode:
        check_keys.append("visibility_checks")
    for key in check_keys:
        if expected[key] != receipt.get(key):
            raise ValueError("publication_receipt_binding_mismatch:" + key)


# Compatibility name for callers that used the old function. Its new meaning
# is publication eligibility, never a semantic-review pass.
def verify_publication_review(payload):
    return verify_publication_receipt(payload)


# Backward-readable helpers for old analyst callers; these never certify publication.
def forecast_editorial_view(forecast):
    return {"factors": [{"effect": f["effect"], "evidence_ids": f["evidence_ids"]} for f in forecast["review"]["factors"]],
            "semantics": "forecasting inference index; not model explanation or verified source fact"}


def build_editorial_contract(context, locked_numbers=None):
    return build_editorial_projection(context, locked_numbers or {})


class EditorialBoundaryError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("editorial_validation_rejected")


def validate_editorial_boundaries(judgment, context, *, locked_numbers=None):
    # Legacy callers may parse old JSON, but this API no longer certifies it.
    return {"status": "not_checked", "protocol": PROTOCOL, "publication_gate": False}
