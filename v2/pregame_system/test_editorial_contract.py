"""Projection and bounded hard-fact guardrail tests; no model/network calls."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest


def test_market_spread_in_neighboring_clause_is_not_the_locked_margin():
    from .editorial_hard_facts import _locked_forecast_issues
    projection={'game':{'home':{'name_zh':'魔术'},'away':{'name_zh':'老鹰'}},'facts':[
        {'id':'locked','kind':'PUBLIC_DECISION','value':{'numbers':{'winner_side':'home','win_probability':.6,
            'expected_margin':3.5,'expected_total':220.5}}}]}
    assert _locked_forecast_issues('我选魔术赢球，让2.5分也站主队这边，预计分差能到3.5分左右。',projection,'article')==[]
    assert any(r['field']=='expected_margin'for r in
        _locked_forecast_issues('我选魔术赢球，让2.5分也站主队这边，预计分差能到5.5分左右。',projection,'article'))

from .editorial_contract import build_editorial_projection, model_view, public_visibility_issues
from .editorial_hard_facts import validate_publication_hard_facts


LOCKED = {"winner_side": "away", "win_probability": 0.56, "expected_margin": 3.5,
          "expected_total": 228.5, "evidence_ids": ["prediction", "form", "roster"]}
GAME = {"game_id": "espn:fixture", "home": {"id": 1, "name": "Toronto Raptors", "name_zh": "猛龙"},
        "away": {"id": 2, "name": "Miami Heat", "name_zh": "热火"},
        "scheduled_tipoff": "2026-10-04T07:00:00+08:00", "cutoff": "2026-10-03T02:39:04+08:00",
        "venue": {"address": {"city": "Quebec City"}, "fullName": "Videotron Centre"},
        "neutral_site": False, "game_type": "preseason", "schedule_revision": 1}


@pytest.mark.parametrize("venue", ["Xfinity Mobile Arena", {"fullName": "Xfinity Mobile Arena"}])
def test_catalog_venue_name_remains_checked_without_inventing_venue_city(venue):
    from .editorial_hard_facts import _venue_issues_for_text
    projection = {"game": {**deepcopy(GAME), "venue": venue}}
    assert _venue_issues_for_text("在 Xfinity Mobile Arena 看轮换如何展开。", projection, "article") == []
    bad = _venue_issues_for_text("在 Madison Square Garden Arena 比赛。", projection, "article")
    assert [row["code"] for row in bad] == ["specific_venue_mismatch"]
    assert bad[0]["actual_venue"] == "Xfinity Mobile Arena"
    assert _venue_issues_for_text("在费城举行。", projection, "article") == []


def test_structured_venue_keeps_explicit_city_and_malformed_address_is_unknown():
    from .editorial_hard_facts import _venue_issues_for_text
    projection = {"game": deepcopy(GAME)}
    assert any(r["code"] == "specific_city_mismatch" for r in
               _venue_issues_for_text("这场比赛在多伦多举行。", projection, "article"))
    projection["game"]["venue"] = {"fullName": "Videotron Centre", "address": "unknown"}
    assert _venue_issues_for_text("这场比赛在多伦多举行。", projection, "article") == []


@pytest.mark.parametrize('text', ['总分我反而看得轻一点，倾向大分。',
    '文班在场时，雷霆的禁区得分会被压一压。',
    '漏人和补位迟疑会制造一些轻松得分。',
    '他的活力是阵地进攻之外的另一条得分路。',
    '后场一旦失误偏多，雷霆的转换就会把分差一点点拉开。'])
def test_qualitative_one_is_not_an_exact_statistic(text):
    from .editorial_hard_facts import _stat_issues_for_text
    projection = {'game': deepcopy(GAME), 'facts': [
        {'id': 'locked', 'kind': 'LOCKED_PREDICTION', 'value': LOCKED},
        {'id': 'stat', 'kind': 'HISTORICAL', 'value': {'side': 'away',
            'sample': 'previous_last10', 'season_start': 2025,
            'metrics': {'games': 10, 'points_for_per_game': 112.5}}}]}
    assert _stat_issues_for_text(text, projection, 'article') == []


@pytest.mark.parametrize('value', ['一', '一点五', '1', '1.5'])
def test_real_one_point_statistics_still_require_evidence(value):
    from .editorial_hard_facts import _stat_issues_for_text
    projection = opponent_series_projection()
    text = f'上赛季季后赛对76人，尼克斯四场场均净胜{value}分。'
    assert any(row['code'] == 'exact_stat_value_mismatch' for row in
        _stat_issues_for_text(text, projection, 'article'))


def opponent_series_projection():
    return {"game": {"home": {"id": 1, "name_zh": "76人", "name": "Philadelphia 76ers"},
                     "away": {"id": 2, "name_zh": "尼克斯", "name": "New York Knicks"}},
            "facts": [
                {"id": "series", "kind": "HISTORICAL", "value": {"series_summary": {
                    "season_start": 2025, "games": 4, "team_summaries": {
                        "1": {"games": 4, "average_margin": -22.25, "three_pct": .3129770992, "rebounds_per_game": 32.5},
                        "2": {"games": 4, "average_margin": 22.25, "three_pct": .4477611940, "rebounds_per_game": 43.0}}}}},
                {"id": "recent", "kind": "HISTORICAL", "value": {"side": "away", "sample": "previous_last10",
                    "season_start": 2025, "metrics": {"games": 10, "point_margin_per_game": 11.9,
                        "three_pct": .3967, "rebounds_per_game": 45.7}}},
                {"id": "home-recent", "kind": "HISTORICAL", "value": {"side": "home", "sample": "previous_last10",
                    "season_start": 2025, "metrics": {"games": 10, "rebounds_per_game": 43.0}}}]}


def test_opponent_series_statistics_use_their_own_sample_and_team():
    from .editorial_hard_facts import _stat_issues_for_text
    p = opponent_series_projection()
    valid = "上赛季季后赛对76人，尼克斯四场场均净胜22.25分，三分命中率44.8%、篮板43.0。"
    assert _stat_issues_for_text(valid, p, "article") == []
    assert _stat_issues_for_text("上赛季季后赛第二轮，76人被尼克斯4比0横扫，四场场均净负22.25分，三分命中率只有31.3%，篮板也以32.5比43.0落后。", p, "article") == []
    assert _stat_issues_for_text("上赛季季后赛对76人，他们场均124.25分。", {**p, "facts": [
        {"id": "series", "kind": "HISTORICAL", "value": {"series_summary": {"season_start": 2025,
         "team_summaries": {"2": {"games": 4, "points_per_game": 124.25}, "1": {"games": 4, "points_per_game": 102.0}}}}}]}, "article") == []
    assert any(r["code"] == "exact_stat_value_mismatch" for r in
               _stat_issues_for_text(valid.replace("22.25", "30.25"), p, "article"))
    assert any(r["code"] == "exact_stat_sample_mismatch" for r in
               _stat_issues_for_text(valid.replace("四场", "五场"), p, "article"))
    assert any(r["code"] == "exact_stat_value_mismatch" for r in
               _stat_issues_for_text("76人这轮系列赛四场场均净胜22.25分。", p, "article"))
    assert any(r["code"] == "exact_stat_value_mismatch" for r in
               _stat_issues_for_text("尼克斯近十场场均净胜22.25分。", p, "article"))
    assert any(r["code"] == "exact_stat_season_mismatch" for r in
               _stat_issues_for_text(valid.replace("上赛季", "本赛季"), p, "article"))


def test_numeric_team_identity_is_not_a_statistic_but_actual_76_rebounds_is_checked():
    from .editorial_hard_facts import _stat_issues_for_text
    p = opponent_series_projection()
    assert _stat_issues_for_text("这直接决定76人的护框和篮板会不会成为问题。", p, "article") == []
    assert _stat_issues_for_text("Philadelphia 76ers 的篮板保护值得看。", p, "article") == []
    assert any(r["stated_value"] == "76" for r in
               _stat_issues_for_text("76人场均篮板76。", p, "article"))


def test_decision_contract_hides_pure_from_writer_but_keeps_revision_provenance():
    context = editorial_context()
    core = {
        "central_question": "Can Doncic bend the switch before the Warriors' veteran load changes the game?",
        "central_thesis": {"statement": "The game depends on which side's creators remain effective in short stints.",
            "classification": "inference", "evidence_ids": ["form"], "confidence": "medium"},
        "matchup_status": "identified",
        "key_matchups": [{"subject": "Doncic pick-and-roll", "opponent_or_scheme": "Warriors switching",
            "why_it_matters": "It can force help away from the rim.", "expected_effect": "A successful attack opens interior finishes.",
            "classification": "inference", "evidence_ids": ["form"], "confidence": "medium", "uncertainty": "Preseason lineup is unconfirmed."}],
        "expected_game_script": "The Lakers probe the switch while Golden State uses Curry movement; short veteran stints can change late possessions.",
        "flip_conditions": [{"condition": "Curry and Green play much less than estimated.",
            "judgment_effect": "The Warriors' half-court organization weakens.", "evidence_ids": ["form"]}],
    }
    provenance = {"analysis_core": core,
        "independent_prediction": {"winner_side": "away", "win_probability": .56,
            "expected_home_margin": -2.5, "expected_total": 224.5},
        "final_prediction": {"winner_side": "home", "win_probability": .55,
            "expected_home_margin": 3.0, "expected_total": 223.5},
        "revision_delta": {"winner_changed": True, "home_win_probability_delta": .11,
            "expected_home_margin_delta": 5.5, "expected_total_delta": -1.0,
            "changed_axes": ["winner", "win_probability", "expected_home_margin", "expected_total"]},
        "market_provenance": {"decision": "revise", "assessment": "challenges",
            "market_timing_status": "observed_only", "information_overlap": "unknown",
            "market_signal_factors": [{"summary": "Observed price favors the home team.", "evidence_ids": ["market"]}],
            "basketball_reweighting_factors": [{"summary": "Reconsidering the home-court and rotation evidence changes the close-game lean.",
                "evidence_ids": ["form"]}]}}
    material = [{"summary": "Rotation load is the largest uncertainty.", "effect": "uncertainty", "evidence_ids": ["form"]},
                {"_contract_item": "forecast_provenance", "value": provenance}]
    projection = build_editorial_projection(context, LOCKED, material)
    assert projection["protocol"] == "editorial-decision-v2"
    pure_fact = next(f for f in projection["facts"] if f.get("value", {}).get("owner") == "pure_baseline")
    assert pure_fact["visibility"] == "audit_only"
    view = model_view(projection)
    view_text = json.dumps(view, ensure_ascii=False)
    assert "pure_baseline" not in view_text and "Pure" not in view_text
    assert "0.6" not in view_text
    assert "DECISION_CORE" in view_text and "REVISION_PROVENANCE" in view_text
    assert "winner_changed" in view_text and "observed_only" in view_text


def test_public_visibility_gate_rejects_internal_model_layer_names_only():
    assert public_visibility_issues({"analysis_article": "勇士可能利用主场把比赛带进半场。"}) == []
    assert public_visibility_issues({"analysis_article": "Pure 给出的基线是222分。"}) == ["Pure"]
    assert public_visibility_issues({"analysis_article": "纯篮球预测给222分。"}) == ["纯篮球预测"]
    assert public_visibility_issues({"prediction_perspective": {"statement": "Pure 给出的胜率是60%。"}}) == ["Pure"]


def test_internal_layer_named_inside_forecast_thesis_is_audit_only_not_writer_input():
    core = {"central_question": "How does the guard attack the switch?",
        "central_thesis": {"statement": "Pure estimates the away team, but I prefer the matchup lean.",
            "classification": "inference", "evidence_ids": ["form"], "confidence": "medium"},
        "matchup_status": "identified", "key_matchups": [{"subject": "Away guard",
            "opponent_or_scheme": "Home switch", "why_it_matters": "It tests the first action.",
            "expected_effect": "Help may expose the weak side.", "classification": "inference",
            "evidence_ids": ["form"], "confidence": "medium"}],
        "expected_game_script": "Away probes the switch; home help rotates.", "flip_conditions": []}
    material = [{"_contract_item": "forecast_provenance", "value": {
        "analysis_core": core, "independent_prediction": {"winner_side": "away"},
        "final_prediction": LOCKED, "revision_delta": {"winner_changed": False, "changed_axes": []},
        "market_provenance": {"decision": "keep", "market_timing_status": "observed_only",
            "information_overlap": "unknown"}}}]
    projection = build_editorial_projection(editorial_context(), LOCKED, material)
    writer_text = json.dumps(model_view(projection), ensure_ascii=False)
    assert "Pure" not in writer_text
    assert any(fact["kind"] == "DECISION_CORE_AUDIT" and fact["visibility"] == "audit_only"
               for fact in projection["facts"])
    safe_core = next(fact["value"] for fact in model_view(projection)["facts"] if fact["kind"] == "DECISION_CORE")
    assert safe_core["central_thesis"] is None
    revision = next(fact["value"] for fact in model_view(projection)["facts"] if fact["kind"] == "REVISION_PROVENANCE")
    assert "independent_prediction" in revision and "independent_analysis_core" not in revision


def test_editorial_adapter_consumes_frozen_decision_core_instead_of_refilling_legacy_nulls():
    from .editorial_pipeline import draft_judgment
    context = editorial_context()
    core = {
        "central_question": "Can the Lakers' ball handlers stress Golden State's switching?",
        "central_thesis": {"statement": "The game turns on how the Lakers attack switches during short preseason stints.",
            "classification": "inference", "evidence_ids": ["form"], "confidence": "medium"},
        "matchup_status": "identified",
        "key_matchups": [{"subject": "Doncic pick-and-roll", "opponent_or_scheme": "Warriors switching",
            "why_it_matters": "It tests the first line of defense.", "expected_effect": "Help rotations can open interior finishes.",
            "classification": "inference", "evidence_ids": ["form"], "confidence": "medium", "uncertainty": None}],
        "expected_game_script": "Los Angeles probes the switch; Golden State tries to keep Curry-led movement alive.",
        "flip_conditions": [{"condition": "The Warriors' veterans play substantially fewer minutes.",
            "judgment_effect": "The home-side half-court edge weakens.", "evidence_ids": ["form"]}],
    }
    provenance = {"analysis_core": core, "independent_prediction": {"winner_side": "away"},
        "final_prediction": LOCKED, "revision_delta": {"winner_changed": True, "changed_axes": ["winner"]},
        "market_provenance": {"decision": "revise", "assessment": "challenges",
            "market_timing_status": "observed_only", "information_overlap": "unknown",
            "market_signal_factors": [], "basketball_reweighting_factors": []}}
    material = [{"_contract_item": "forecast_provenance", "value": provenance}]
    projection = build_editorial_projection(context, LOCKED, material)
    draft = {"title": "A specific matchup", "lead": "A bounded lead.", "article": "A short analysis.",
        "watch_themes": [], "confidence": "medium", "player_analysis": []}
    result = draft_judgment(draft, context, LOCKED, projection)
    assert result["central_thesis"]["statement"] == core["central_thesis"]["statement"]
    assert result["core_question"] == core["central_question"]
    assert result["game_script"] == core["expected_game_script"]
    assert "veterans play substantially fewer" in result["model_risk"]
    assert result["key_matchup_variables"][0]["evidence_ids"] == ["form"]


def editorial_context():
    cold = {"current_season_games_observed": 0, "current_season_games_confirmed": None,
            "current_season_coverage": "not_verified", "rolling_5_current_season_games": 0,
            "rolling_5_previous_season_games": 5, "rolling_15_current_season_games": 0,
            "rolling_15_previous_season_games": 15}
    roster = {"teams": {
        "home": {"team_id": 1, "official_team_roster": None, "continuity": {"status": "unavailable"},
                 "cold_start": deepcopy(cold)},
        "away": {"team_id": 2, "official_team_roster": None, "continuity": {"status": "unavailable"},
                 "cold_start": deepcopy(cold)},
    }, "player_knowledge": {"season": "2025-26",
        "column_definitions": {"history": [], "per_observed_game": []},
        "teams": {"home": {"team_id": 1, "historical_team_leaders": [{
            "player_name": "Scottie Barnes", "player_id": "nba:barnes", "team_id": 1}]}}}}
    form = {"status": "available", "season_start": 2026, "competition_scope": "regular season",
        "teams": {"away": {
            "previous_last10": {"status": "available", "games": 10, "points_against_per_game": 128.6,
                                "first_game_date": "2026-03-27", "last_game_date": "2026-04-14"},
            "current_last10": {"status": "no_games", "games": 0}}}}
    evidence = [
        {"id": "identity", "section": "game_identity", "value": deepcopy(GAME)},
        {"id": "prediction", "section": "prediction", "value": {"pure_prediction": {
            "win_probability": 0.60, "expected_margin": 2.8, "expected_total": 233.0}}},
        {"id": "roster", "section": "roster_context", "value": deepcopy(roster)},
        {"id": "form", "section": "season_form", "value": deepcopy(form)},
    ]
    return {"game": deepcopy(GAME), "pure_prediction": deepcopy(evidence[1]["value"]["pure_prediction"]),
            "roster_context": roster, "season_form": form, "objective_evidence": evidence,
            "evidence_index": {item["id"]: {"section": item["section"], "source_type": "fixture"} for item in evidence},
            "analysis_request": "生成第25秒赛前内容。", "context_hash": "fixture-context"}


def fact(projection, path):
    matches = [item for item in projection["facts"] if item["source_path"] == path]
    assert len(matches) == 1
    return matches[0]


def test_projection_separates_schedule_home_from_actual_venue_and_uses_beijing_time():
    p = build_editorial_projection(editorial_context(), LOCKED)
    game = fact(p, "/game")["value"]
    time = fact(p, "/game/scheduled_tipoff")["value"]
    assert game["home"]["name"] == "Toronto Raptors"
    assert game["venue"]["address"]["city"] == "Quebec City"
    assert game["venue"]["fullName"] == "Videotron Centre"
    assert time["tipoff_beijing"] == "2026-10-04 07:00"


def test_historical_form_keeps_exact_sample_season_entity_and_value():
    p = build_editorial_projection(editorial_context(), LOCKED)
    row = fact(p, "/season_form/teams/away/previous_last10")["value"]
    assert row["sample"] == "previous_last10"
    assert row["season_start"] == 2025
    assert row["side"] == "away"
    assert row["metrics"]["games"] == 10
    assert row["metrics"]["points_against_per_game"] == 128.6


def test_kawhi_current_raptors_roster_does_not_overwrite_his_historical_clippers_identity():
    context = editorial_context()
    context["roster_context"]["teams"]["home"]["team_id"] = 1610612761
    context["roster_context"]["teams"]["home"]["official_team_roster"] = {
        "status": "available", "players": [{"player_id": "202695", "name": "Kawhi Leonard"}],
    }
    context["roster_context"]["player_knowledge"] = {
        "season": "2025-26",
        "column_definitions": {
            "history": ["team_id", "games_played", "per_observed_game", "observed_games_by_statistic"],
            "previous_team_stints": ["team_id", "games_played"],
            "per_observed_game": ["points", "rebounds", "assists"],
        },
        "teams": {"home": {
            "team_id": 1610612761,
            "historical_team_leaders": [],
            "previous_season_players": [{
                "player_id": "202695", "player_name": "Kawhi Leonard", "team_id": 1610612761,
                "previous_season_history_status": "available",
                "previous_team_stints_status": "available",
                "previous_team_stints": [[1610612746, 65]],
                "current_team_last_season_stint": [1610612746, 65, [27.0, 6.0, 4.5], [65, 65, 65]],
                "previous_season_total": [None, 65, [27.0, 6.0, 4.5], [65, 65, 65]],
            }],
        }},
    }

    projection = build_editorial_projection(context, LOCKED)
    history_fact = next(f for f in projection["facts"] if f["kind"] == "HISTORICAL"
                        and any(p.get("player") == "Kawhi Leonard" for p in f["value"].get("players", [])))
    player_fact = next(p for p in history_fact["value"]["players"] if p["player"] == "Kawhi Leonard")
    current_fact = next(f for f in projection["facts"] if f["kind"] == "CURRENT"
                        and f["value"].get("roster_snapshot"))
    assert player_fact["last_season_team_ids"] == [1610612746]
    assert player_fact["team_id_last_season"] == 1610612746
    assert player_fact["statistics_scope"] == "single_team"
    assert player_fact["statistics_team_ids"] == [1610612746]
    assert current_fact["value"]["team_id"] == 1610612761
    assert "1610612761" not in player_fact["statistics_team_ids"]


def test_multiteam_previous_season_aggregate_is_not_assigned_to_current_roster_team():
    context = editorial_context()
    pk = context["roster_context"]["player_knowledge"]
    pk["column_definitions"]["history"] = ["team_id", "games_played", "per_observed_game", "observed_games_by_statistic"]
    pk["column_definitions"]["previous_team_stints"] = ["team_id", "games_played"]
    pk["teams"]["home"] = {"team_id": 10, "historical_team_leaders": [], "previous_season_players": [{
        "player_id": "p1", "player_name": "Historical Player", "team_id": 10,
        "previous_team_stints_status": "available", "previous_team_stints": [[20, 12], [30, 40]],
        "previous_season_total": {"games_played": 52},
    }]}
    projection = build_editorial_projection(context, LOCKED)
    history_fact = next(f for f in projection["facts"] if f["kind"] == "HISTORICAL"
                        and f["value"].get("players"))
    player_fact = history_fact["value"]["players"][0]
    assert player_fact["last_season_team_ids"] == [20, 30]
    assert player_fact["team_id_last_season"] is None
    assert player_fact["statistics_scope"] == "all_observed_teams"
    assert player_fact["statistics_team_ids"] == [20, 30]


def test_rolling_window_and_metric_valid_n_are_not_confused_by_hard_fact_check():
    context = editorial_context()
    fact_context = {
        "schema_version": "pregame-fact-context-v1", "game_id": GAME["game_id"],
        "feature_cutoff": "2026-10-03T02:39:04+00:00",
        "team_form": {
            "away": {"status": "partial", "requested_games": 15, "observed_games": 15,
                "season_counts": {"2025": 15}, "competition_counts": {"regular_season": 15},
                "first_game_date": "2026-03-01", "last_game_date": "2026-04-14",
                "date_timezone": "America/New_York", "metrics": {
                    "net_rating_15": {"value": 2.5, "valid_games": 13, "missing_games": 2,
                        "unit": "points_per_100_possessions", "aggregation": "mean_of_per_game_values",
                        "method": "v1_per_game_mean_shift_1_rolling_15"},
                }},
        },
        "schedule": {"home": {"status": "unavailable", "reason": "no_cutoff_eligible_schedule_evidence",
            "basis": None, "back_to_back": None, "source_refs": []}},
    }
    context["objective_evidence"].append({"id": "fc", "section": "fact_context", "value": fact_context})
    context["objective_evidence"].append({"id": "form", "section": "team_form", "value": {
        "away": {"net_rating_15": 2.5}, "home": {},
    }})
    projection = build_editorial_projection(context, LOCKED)
    valid = validate_publication_hard_facts({"analysis_article": "尼克斯近15场净效率2.5。"}, projection)
    assert not any(item["code"] == "exact_stat_sample_mismatch" for item in valid)
    mismatch = validate_publication_hard_facts({"analysis_article": "尼克斯近13场净效率2.5。"}, projection)
    assert any(item["code"] == "exact_stat_sample_mismatch" for item in mismatch)


def _sample_count_projection(*, observed, valid):
    context = editorial_context()
    fact_context = {
        "schema_version": "pregame-fact-context-v1", "game_id": GAME["game_id"],
        "feature_cutoff": "2026-10-03T02:39:04+00:00",
        "team_form": {"home": {
            "status": "partial", "requested_games": 15, "observed_games": observed,
            "season_counts": {"2025": observed}, "competition_counts": {"regular_season": observed},
            "first_game_date": "2026-03-01", "last_game_date": "2026-04-10",
            "date_timezone": "America/New_York", "metrics": {
                "def_rating_15": {"value": 111.0, "valid_games": valid,
                    "missing_games": observed - valid, "unit": "points_allowed_per_100_possessions",
                    "aggregation": "mean_of_per_game_values", "method": "v1_per_game_mean_shift_1_rolling_15",
                    "validation": "matched"},
            },
        }},
        "schedule": {"home": {"status": "unavailable", "reason": "calendar_unknown",
            "basis": None, "back_to_back": None, "source_refs": []}},
    }
    context["objective_evidence"].extend([
        {"id": "fc-sample", "section": "fact_context", "value": fact_context},
        {"id": "team-form-sample", "section": "team_form", "value": {"home": {"def_rating_15": 111.0}}},
    ])
    return build_editorial_projection(context, LOCKED)


def test_sample_validator_distinguishes_requested_observed_and_metric_valid_games():
    complete = _sample_count_projection(observed=15, valid=13)
    assert not validate_publication_hard_facts(
        {"analysis_article": "猛龙近15场窗口，防守效率有效13场。"}, complete,
    )
    wrong_valid = validate_publication_hard_facts(
        {"analysis_article": "猛龙近15场窗口，防守效率有效15场。"}, complete,
    )
    assert any(item["code"] == "exact_stat_sample_mismatch" and item.get("sample_dimension") == "valid_games"
               for item in wrong_valid)

    partial = _sample_count_projection(observed=8, valid=8)
    wrong_observed = validate_publication_hard_facts(
        {"analysis_article": "猛龙请求窗口15场，实际15场。"}, partial,
    )
    assert any(item["code"] == "exact_stat_sample_mismatch" and item.get("sample_dimension") == "observed_games"
               for item in wrong_observed)
    assert not validate_publication_hard_facts(
        {"analysis_article": "猛龙请求窗口15场，实际纳入8场，防守效率有效8场。"}, partial,
    )


def test_numeric_team_rest_days_need_a_schedule_fact_but_general_rest_language_is_allowed():
    projection = build_editorial_projection(editorial_context(), LOCKED)
    exact = validate_publication_hard_facts(
        {"analysis_article": "猛龙本场赛前已经休息7天。"}, projection,
    )
    assert any(item["code"] == "exact_rest_days_unverified" for item in exact)
    exact_chinese = validate_publication_hard_facts(
        {"analysis_article": "猛龙赛前已休息七天。"}, projection,
    )
    assert any(item["code"] == "exact_rest_days_unverified" for item in exact_chinese)
    article_context_claim = validate_publication_hard_facts(
        {"analysis_article": "双方已经休息7天。"}, projection,
    )
    assert any(item["code"] == "exact_rest_days_unverified" for item in article_context_claim)
    negated_not_this_game = validate_publication_hard_facts(
        {"analysis_article": "猛龙本场赛前并没有休息7天。"}, projection,
    )
    assert not any(item["code"] == "exact_rest_days_unverified" for item in negated_not_this_game)
    negated_not_rest = validate_publication_hard_facts(
        {"analysis_article": "猛龙本场赛前不是休息7天。"}, projection,
    )
    assert not any(item["code"] == "exact_rest_days_unverified" for item in negated_not_rest)
    negated_comparison = validate_publication_hard_facts(
        {"analysis_article": "猛龙并没有比热火多休息7天。"}, projection,
    )
    assert not any(item["code"] == "exact_rest_days_unverified" for item in negated_comparison)
    double_negative_affirmation = validate_publication_hard_facts(
        {"analysis_article": "猛龙并非没有休息7天。"}, projection,
    )
    assert any(item["code"] == "exact_rest_days_unverified" for item in double_negative_affirmation)
    conditional_suffix = validate_publication_hard_facts(
        {"analysis_article": "猛龙休息7天的话，轮换可能更有活力。"}, projection,
    )
    assert not any(item["code"] == "exact_rest_days_unverified" for item in conditional_suffix)
    general = validate_publication_hard_facts(
        {"analysis_article": "如果球员得到充分休息，轮换的活力可能更好。"}, projection,
    )
    assert not any(item["code"] == "exact_rest_days_unverified" for item in general)
    player_rest = validate_publication_hard_facts(
        {"analysis_article": "热火轮休7名球员，比赛节奏可能改变。"}, projection,
    )
    assert not any(item["code"] == "exact_rest_days_unverified" for item in player_rest)
    unknown = validate_publication_hard_facts(
        {"analysis_article": "无法确认猛龙是否已经休息7天。"}, projection,
    )
    assert not any(item["code"] == "exact_rest_days_unverified" for item in unknown)
    conditional = validate_publication_hard_facts(
        {"analysis_article": "如果猛龙休息7天，轮换可能更有活力。"}, projection,
    )
    assert not any(item["code"] == "exact_rest_days_unverified" for item in conditional)
    coach_player = validate_publication_hard_facts(
        {"analysis_article": "热火教练表示会让某球员休息7天。"}, projection,
    )
    assert not any(item["code"] == "exact_rest_days_unverified" for item in coach_player)
    coach_quote = validate_publication_hard_facts(
        {"analysis_article": "热火教练表示：“猛龙本场赛前休息7天。”"}, projection,
    )
    assert not any(item["code"] == "exact_rest_days_unverified" for item in coach_quote)
    assert any(item["code"] == "unverified_coach_quote" for item in coach_quote)


def test_mismatched_fact_context_metric_is_diagnostic_only_through_evidence_and_projection():
    from v2.frontend.analysis_service import _evidence
    from .fact_context import build_fact_context
    from .editorial_hard_facts import validate_publication_hard_facts
    from .test_fact_context import GAME as HISTORY_GAME, source_history, existing_feature_values, CUTOFF

    games, stats = source_history()
    feature_values = existing_feature_values(stats)
    feature_values["home_net_rating_roll15"] += 1.0
    fact_context = build_fact_context(HISTORY_GAME, CUTOFF, games, stats, feature_values)
    recent_form = {side: {
        "off_rating_15": feature_values[f"{side}_off_rating_roll15"],
        "def_rating_15": feature_values[f"{side}_def_rating_roll15"],
        "net_rating_15": feature_values[f"{side}_net_rating_roll15"],
        "pace_15": feature_values[f"{side}_pace_roll15"],
        "win_rate_15": feature_values[f"{side}_win_rate_roll15"],
        "three_pct_15": feature_values[f"{side}_three_pct_roll15"],
        "rebound_share_15": feature_values[f"{side}_rebound_share_roll15"],
        "turnover_rate_15": feature_values[f"{side}_turnover_rate_roll15"],
    } for side in ("home", "away")}
    game = {
        "id": HISTORY_GAME["game_id"], "status": "scheduled", "tipoff": HISTORY_GAME["scheduled_tipoff"],
        "home": {"id": HISTORY_GAME["home_team_id"], "name": "Miami Heat", "name_zh": "热火"},
        "away": {"id": HISTORY_GAME["away_team_id"], "name": "Toronto Raptors", "name_zh": "猛龙"},
        "game_type": "preseason", "season_start": 2026, "neutral_site": False,
        "relationship": {"basketball": {"win_probability": .55, "expected_margin": 1.0, "expected_total": 220.0},
            "market": None, "divergence": None, "residual": None,
            "final": {"win_probability": .55, "expected_margin": 1.0, "expected_total": 220.0}},
        "background": {"recent_form": recent_form, "schedule": {"home_rest_days": 7, "away_rest_days": 1},
            "availability": {"status": "unavailable", "records": []}, "market": None},
        "fact_context": fact_context, "roster_context": None, "matchup_history": None,
        "season_form": None, "season_phase": {"phase": "very_early"}, "technical": {},
        "source": {}, "missing": [],
    }
    evidence, _, _ = _evidence(game, "2026-10-03T20:00:00+00:00", None)
    # The raw alternate team_form channel must not keep the mismatched metric.
    team_form = next(item["value"] for item in evidence if item["section"] == "team_form")
    assert "net_rating_15" not in team_form["home"]
    analyst_context = {
        "game": {"game_id": game["id"], "home": game["home"], "away": game["away"],
            "scheduled_tipoff": game["tipoff"], "cutoff": "2026-10-03T20:00:00+00:00",
            "game_type": "preseason"},
        "pure_prediction": {"win_probability": .55, "expected_margin": 1, "expected_total": 220},
        "roster_context": None, "season_form": None, "season_phase": game["season_phase"],
        "objective_evidence": evidence,
        "evidence_index": {item["id"]: {"section": item["section"], "source_type": item["kind"]} for item in evidence},
    }
    projection = build_editorial_projection(analyst_context, LOCKED)
    rolling = next(item for item in projection["facts"] if item["value"].get("sample") == "rolling_15"
                   and item["value"].get("side") == "home")
    assert "net_rating_15" not in rolling["value"]["metrics"]
    assert "off_rating_15" in rolling["value"]["metrics"]
    diagnostic = next(item for item in projection["facts"] if item["kind"] == "UNKNOWN"
                      and (item["value"].get("diagnostic_metric") or {}).get("name") == "net_rating_15")
    assert diagnostic["value"]["diagnostic_metric"]["value"] == feature_values["home_net_rating_roll15"]
    assert diagnostic["value"]["diagnostic_metric"]["validation"] == "mismatch"

    metric_value = feature_values["home_net_rating_roll15"]
    unverified = validate_publication_hard_facts(
        {"analysis_article": f"热火近15场净效率 {metric_value:.1f}。"}, projection,
    )
    assert any(item["code"] == "exact_stat_value_unverified" for item in unverified)
    matched_value = feature_values["home_off_rating_roll15"]
    verified = validate_publication_hard_facts(
        {"analysis_article": f"热火近15场进攻效率 {matched_value:.1f}。"}, projection,
    )
    assert not any(item["code"] in {"exact_stat_value_unverified", "exact_stat_value_mismatch"}
                   for item in verified)


@pytest.mark.parametrize("article", [
    "猛龙主场迎战热火，这个赛季的第一场值得观察。",
    "我认为这套锋线属于联盟第一档。",
    "我预计核心球员只打15～20分钟。",
    "我更看好热火，主要看新阵容的匹配方式。",
    "旧样本对当前阵容的参考价值有限。",
    "这套阵容可能更快磨合。",
    "如果主力时间有限，篮板和护框对比赛的影响会更明显。",
])
def test_analytical_freedom_and_team_home_wording_are_not_publication_blockers(article):
    p = build_editorial_projection(editorial_context(), LOCKED)
    assert validate_publication_hard_facts({"analysis_article": article}, p) == []


def test_ten_game_statistic_mislabeled_as_fifteen_is_blocked():
    p = build_editorial_projection(editorial_context(), LOCKED)
    findings = validate_publication_hard_facts(
        {"analysis_article": "热火上赛季近15场场均失分128.6。"}, p)
    assert any(item["code"] == "exact_stat_sample_mismatch" for item in findings)


def test_generic_single_game_context_does_not_change_sample_for_prior_model_numbers():
    p = build_editorial_projection(editorial_context(), LOCKED)
    findings = validate_publication_hard_facts({"analysis_article":
        "纯模型给主队约60%胜率、净胜2.8分；这是一场季前赛，双方轮换都未确认。"}, p)
    assert not any(item["code"] == "exact_stat_sample_mismatch" for item in findings)


def test_exact_stat_value_mismatch_is_blocked():
    p = build_editorial_projection(editorial_context(), LOCKED)
    findings = validate_publication_hard_facts(
        {"analysis_article": "热火上赛季近10场场均失分127.6。"}, p)
    assert any(item["code"] == "exact_stat_value_mismatch" for item in findings)


@pytest.mark.parametrize("article", [
    "比赛可能在第三节拉开到8–12分，随后看篮板控制。",
    "如果尼克斯三分命中率降到30%，他们需要更多冲抢。",
    "预计 Joel Embiid 本场出战18–24分钟。",
    "76 人最薄的一环是中锋深度。",
])
def test_forecast_conditional_projection_and_structural_numbers_are_not_historical_stats(article):
    from .editorial_hard_facts import validate_publication_hard_facts
    context = editorial_context()
    context["season_form"]["teams"]["away"]["previous_last10"]["rebounds_per_game"] = 38.2
    projection = build_editorial_projection(context, LOCKED)
    assert validate_publication_hard_facts({"analysis_article": article}, projection) == []


def test_flip_condition_win_probability_is_not_a_historical_win_rate():
    from .editorial_hard_facts import validate_publication_hard_facts
    projection = build_editorial_projection(editorial_context(), LOCKED)
    text = "Embiid 健康出战并承担接近正常负荷，76人内线劣势被抵消：分差收窄至4-6分，胜率下调至65%附近。"
    assert validate_publication_hard_facts({"analysis_article": text}, projection) == []


@pytest.mark.parametrize("text,metric,expected", [
    ("尼克斯上季场均篮板45.2个。", "rebounds_per_game", "historical_current_factual_statistic"),
    ("最终预测尼克斯胜，胜率78%。", "win_rate", "locked_prediction_output"),
    ("尼克斯可能把领先扩大到8–12分。", "point_margin_per_game", "forecast_or_game_script_estimate"),
    ("如果三分命中率降到30%，比赛可能改变。", "three_pct", "conditional_or_hypothetical_number"),
    ("中锋深度最薄的一环。", "rebounds_per_game", "structural_or_count_expression"),
    ("76 人将面对尼克斯。", None, "team_identity"),
])
def test_numeric_claim_type_classification(text, metric, expected):
    from .editorial_hard_facts import _NUMBER_RE, classify_numeric_claim_type
    projection = {"game": {"home": {"id": 1, "name_zh": "尼克斯", "name": "New York Knicks"},
                           "away": {"id": 2, "name_zh": "76人", "name": "Philadelphia 76ers"}}, "facts": []}
    token = next(_NUMBER_RE.finditer(text))
    assert classify_numeric_claim_type(text, token.start(), token.end(), metric, projection) == expected


def test_previous_season_abbreviation_is_a_valid_season_scope_for_player_statistics():
    from .editorial_hard_facts import validate_publication_hard_facts
    projection = {"game": {"home": {"id": 1, "name_zh": "尼克斯"},
                           "away": {"id": 2, "name_zh": "76人"}},
        "facts": [{"id": "player-history", "kind": "HISTORICAL", "value": {"side": "home",
            "season_start": 2025, "players": [{"player": "Jalen Brunson",
                "average_statistics": {"points": 26.041, "assists": 6.797},
                "stat_sample_counts": {"points": 74, "assists": 74}}]}}]}
    assert validate_publication_hard_facts(
        {"analysis_article": "Brunson上季场均26.0分、6.8助攻。"}, projection) == []
    wrong = validate_publication_hard_facts(
        {"analysis_article": "Brunson上季场均29.0分、6.8助攻。"}, projection)
    assert any(item["code"] == "exact_stat_value_mismatch" for item in wrong)


def _h2h_numeric_projection():
    game = {"game_id": "fixture-h2h", "home": {"id": 1, "name_zh": "尼克斯", "name": "New York Knicks"},
        "away": {"id": 2, "name_zh": "76人", "name": "Philadelphia 76ers"}}
    series = {"season_start": 2025, "games": 4, "team_summaries": {
        "1": {"games": 4, "rebounds_per_game": 43.0, "three_pct": .44776119402985076},
        "2": {"games": 4, "rebounds_per_game": 32.5, "three_pct": .31297709923664124}},
        "wins": {"1": 4, "2": 0}, "game_ids": ["g1", "g2", "g3", "g4"]}
    return {"game": game, "facts": [{"id": "h2h", "kind": "HISTORICAL",
        "value": {"series_summary": series, "meetings": [{"game_id": f"g{i}"} for i in range(4)]}}]}


def test_h2h_two_team_comparison_numbers_use_the_matching_team_side():
    from .editorial_hard_facts import validate_publication_hard_facts
    projection = _h2h_numeric_projection()
    article = "上赛季季后赛尼克斯场均43.0个篮板对76人的32.5个，三分命中率44.8%对31.3%。"
    assert validate_publication_hard_facts({"analysis_article": article}, projection) == []
    compact = "上赛季季后赛尼克斯场均43.0个篮板对32.5个。"
    assert validate_publication_hard_facts({"analysis_article": compact}, projection) == []
    swapped = "上赛季季后赛尼克斯场均32.5个篮板对76人的43.0个。"
    findings = validate_publication_hard_facts({"analysis_article": swapped}, projection)
    assert any(item["code"] == "exact_stat_value_mismatch" for item in findings)


def test_player_games_played_is_not_misread_as_a_rolling_sample_and_unknown_count_stays_blocked():
    from .editorial_hard_facts import validate_publication_hard_facts
    judgment = {"player_analysis": [{"player_name": "Joel Embiid", "statement": "上季仅出战38场。"}]}
    projection = {"game": deepcopy(GAME), "facts": [{"id": "current-roster", "kind": "CURRENT",
        "value": {"side": "away", "team_id": 2,
                  "roster_snapshot": [{"name": "Joel Embiid", "team_id": 2}]}}]}
    findings = validate_publication_hard_facts(judgment, projection)
    assert any(item["code"] == "exact_stat_value_unverified" for item in findings)
    assert not any(item["code"] == "exact_stat_sample_mismatch" for item in findings)


def test_player_games_played_count_matches_only_an_explicit_historical_player_fact():
    from .editorial_hard_facts import validate_publication_hard_facts
    judgment = {"player_analysis": [{"player_name": "Joel Embiid", "statement": "上季仅出战38场。"}]}
    projection = {"game": deepcopy(GAME), "facts": [{"id": "embiid-history", "kind": "HISTORICAL",
        "value": {"side": "away", "season_start": 2025, "players": [
            {"player": "Joel Embiid", "games_played": 38, "average_statistics": {}}]}}]}
    assert validate_publication_hard_facts(judgment, projection) == []
    judgment["player_analysis"][0]["statement"] = "上季仅出战40场。"
    findings = validate_publication_hard_facts(judgment, projection)
    assert any(item["code"] == "exact_stat_value_mismatch" and item.get("metric") == "games_played"
               for item in findings)


def test_historical_minutes_need_a_player_fact_but_minutes_projection_is_not_a_fact_claim():
    from .editorial_hard_facts import validate_publication_hard_facts
    projection = {"game": deepcopy(GAME), "facts": [{"id": "embiid-history", "kind": "HISTORICAL",
        "value": {"side": "away", "season_start": 2025, "players": [{"player": "Joel Embiid",
            "average_statistics": {"minutes": 27.1}, "stat_sample_counts": {"minutes": 38}}]}}]}
    judgment = {"player_analysis": [{"player_name": "Joel Embiid", "statement": "上季场均27.1分钟。"}]}
    assert validate_publication_hard_facts(judgment, projection) == []


def test_unverified_historical_minutes_are_blocked_but_projected_minutes_are_not():
    from .editorial_hard_facts import validate_publication_hard_facts
    projection = {"game": deepcopy(GAME), "facts": [{"id": "current-roster", "kind": "CURRENT",
        "value": {"side": "away", "team_id": 2,
                  "roster_snapshot": [{"name": "Joel Embiid", "team_id": 2}]}}]}
    historical = validate_publication_hard_facts(
        {"analysis_article": "Embiid上季场均约27分钟。"}, projection)
    assert any(item["code"] == "exact_stat_value_unverified" and item.get("metric") == "minutes_per_game"
               for item in historical)
    projected = validate_publication_hard_facts(
        {"analysis_article": "预计 Embiid 本场出战18–24分钟。"}, projection)
    assert projected == []


def test_confirmed_personnel_change_background_evidence_can_verify_transaction_without_weakening_unknown_claims():
    from .editorial_hard_facts import validate_publication_hard_facts
    projection = {"game": {"home": {"id": 1, "name_zh": "尼克斯", "name": "New York Knicks"},
                           "away": {"id": 2, "name_zh": "76人", "name": "Philadelphia 76ers"}},
        "facts": [{"id": "confirmed-personnel-change", "kind": "BACKGROUND", "game_specific": False,
            "source_evidence_ids": ["source-1"], "value": {"team_id": 2, "claim_type": "personnel_change",
                "assertion_kind": "confirmed_fact", "temporal_scope": "current_season_background",
                "quote": "The Sixers traded away Paul George for Jaylen Brown and signed LeBron James."}}]}
    supported = "76人用 Paul George 换来 Jaylen Brown，并签下 LeBron James。"
    assert validate_publication_hard_facts({"analysis_article": supported}, projection) == []
    unsupported = "76人用 Paul George 换来 Jaylen Brown。"
    no_source = {"game": projection["game"], "facts": []}
    assert any(item["code"] == "transaction_claim_unverified"
               for item in validate_publication_hard_facts({"analysis_article": unsupported}, no_source))
    opinion_only = deepcopy(projection)
    opinion_only["facts"][0]["value"]["assertion_kind"] = "source_opinion"
    assert any(item["code"] == "transaction_claim_unverified"
               for item in validate_publication_hard_facts({"analysis_article": unsupported}, opinion_only))


def test_brand_number_is_not_a_total_but_actual_total_still_checked():
    from .editorial_hard_facts import _stat_issues_for_text
    p = build_editorial_projection(editorial_context(), LOCKED)
    assert _stat_issues_for_text("第25秒把总分从基线的233.0下调到228.5。", p, "brand") == []
    issues = _stat_issues_for_text("第25秒预计总分为299.5。", p, "bad-total")
    assert any(x["code"] == "locked_forecast_text_mismatch" and x["stated_value"] == "299.5"
               and x["field"] == "expected_total" for x in issues)


def test_unknown_minutes_and_starters_are_not_confirmations():
    from .editorial_hard_facts import _availability_issues_for_text
    p = build_editorial_projection(editorial_context(), LOCKED)
    assert _availability_issues_for_text("双方都没有确认首发，也没有确认的分钟限制。",p,"unknown") == []
    assert _availability_issues_for_text("双方都没有确认的分钟限制，实际出场时间会影响比赛。",p,"unknown") == []
    assert _availability_issues_for_text("没有确认首发，没有分钟限制来源。",p,"unknown") == []
    issues = _availability_issues_for_text("双方都没有确认的分钟限制。教练已确认限制在24分钟。",p,"real-claim")
    assert any(x["code"] == "current_minutes_restriction_unverified" for x in issues)


@pytest.mark.parametrize("negation", ["均无", "尚无", "暂无", "没有", "并未"])
def test_negated_confirmed_starters_do_not_become_positive_starter_facts(negation):
    from .editorial_hard_facts import _availability_issues_for_text
    p=build_editorial_projection(editorial_context(),LOCKED)
    assert _availability_issues_for_text(f"双方{negation}确认首发，Scottie Barnes的分钟是我的预计。",p,"negated") == []
    assert any(x["code"] == "confirmed_starter_source_unverified" for x in
        _availability_issues_for_text("Scottie Barnes已经确认首发。",p,"assertion"))


def test_margin_beside_win_rate_is_not_checked_as_another_percentage():
    from .editorial_hard_facts import _stat_issues_for_text
    p = build_editorial_projection(editorial_context(), LOCKED)
    issues = _stat_issues_for_text("胜率和3分左右的优势，总分228.5。",p,"units")
    assert not any(x.get("metric") in {"win_rate","win_pct"} and x.get("stated_value")=="3" for x in issues)


def test_explicit_pure_probability_supports_fraction_and_percent_not_wrong_value():
    from .editorial_hard_facts import _matches_explicit_model_output
    p = build_editorial_projection(editorial_context(), LOCKED)
    assert _matches_explicit_model_output("0.6000","win_rate","模型基线胜率",p)
    assert _matches_explicit_model_output("60.0","win_rate","模型基线胜率",p)
    assert not _matches_explicit_model_output("0.8000","win_rate","模型基线胜率",p)


def test_rolling_label_number_is_sample_not_stat_and_later_team_does_not_own_earlier_stat():
    from .editorial_hard_facts import _stat_issues_for_text
    p={"game":GAME,"facts":[{"id":"away","kind":"HISTORICAL","value":{"side":"away","games":15,
        "metrics":{"games":15,"def_rating":123.4}}}, {"id":"home","kind":"HISTORICAL","value":{"side":"home","games":15,
        "metrics":{"games":15,"def_rating":110.0,"three_pct":0.383}}}]}
    assert _stat_issues_for_text("上赛季rolling_15的防守效率是123.4；猛龙上赛季三分命中率38.3%。",p,"mixed") == []
    assert _stat_issues_for_text("热火上赛季rolling_15防守效率123.4。",p,"sample") == []
    assert any(x["code"]=="exact_stat_value_mismatch" for x in _stat_issues_for_text("猛龙上赛季防守效率123.4。",p,"wrong-team"))


def test_one_game_in_an_opinion_is_not_a_one_point_statistic():
    from .editorial_hard_facts import _stat_issues_for_text
    p={"game":GAME,"facts":[{"id":"score","kind":"HISTORICAL","value":{"metrics":{"points_for_per_game":110}}}]}
    assert _stat_issues_for_text("比赛会变成一场谁的新人更能得分的混战。",p,"opinion") == []
    assert any(x["code"]=="exact_stat_value_mismatch" for x in _stat_issues_for_text("上赛季场均得分99。",p,"wrong-stat"))


def test_historical_stat_cannot_be_labeled_current_season():
    p = build_editorial_projection(editorial_context(), LOCKED)
    findings = validate_publication_hard_facts(
        {"analysis_article": "热火本赛季近10场场均失分128.6。"}, p)
    assert any(item["code"] == "exact_stat_season_mismatch" for item in findings)


def test_historical_exact_stat_requires_season_scope():
    p = build_editorial_projection(editorial_context(), LOCKED)
    findings = validate_publication_hard_facts(
        {"analysis_article": "热火近10场场均失分128.6。"}, p)
    assert any(item["code"] == "exact_stat_season_unlabeled" for item in findings)


def test_specific_wrong_venue_and_city_are_blocked_but_home_team_is_allowed():
    p = build_editorial_projection(editorial_context(), LOCKED)
    venue = validate_publication_hard_facts({"analysis_article": "本场在 Scotiabank Arena 举行。"}, p)
    city = validate_publication_hard_facts({"analysis_article": "比赛将在 Toronto 举行。"}, p)
    assert any(item["code"] == "specific_venue_mismatch" for item in venue)
    assert any(item["code"] == "specific_city_mismatch" for item in city)
    assert validate_publication_hard_facts({"analysis_article": "猛龙主场迎战热火。"}, p) == []
    moved = validate_publication_hard_facts(
        {"analysis_article": "本场移师多伦多，猛龙仍是赛程指定主队。"}, p)
    assert any(item["code"] == "specific_city_mismatch" for item in moved)


def test_unknown_venue_city_is_not_mislabeled_as_a_proven_city_conflict():
    c = editorial_context()
    c["game"]["venue"]["address"]["city"] = ""
    p = build_editorial_projection(c, LOCKED)
    findings = validate_publication_hard_facts({"analysis_article": "热火带着新前场来到多伦多。"}, p)
    assert not any(item["code"] == "specific_city_mismatch" for item in findings)


def test_confirmed_out_player_cannot_be_called_confirmed_starter():
    c = editorial_context()
    c["objective_evidence"].append({"id": "availability", "section": "availability", "value": {"records": [{
        "player_id": "player-1", "player_name": "Test Player", "team_id": 1, "game_id": "espn:fixture",
        "status": "Out", "captured_at": "2026-10-03T01:00:00+08:00"}]}})
    p = build_editorial_projection(c, LOCKED)
    findings = validate_publication_hard_facts({"analysis_article": "Test Player 已确认首发出战。"}, p)
    assert any(item["code"] == "confirmed_status_starter_conflict" for item in findings)


def _web_player_status(player, game_id, claim_type="availability", assertion="confirmed_fact", quote=None):
    return {"id": "status", "section": "web_context", "game_id": game_id, "value": {
        "eligible_for_fact": True, "assertion_kind": assertion, "temporal_class": "LIVE" if assertion == "confirmed_fact" else "CURRENT",
        "temporal_scope": "current_pregame", "claim_type": claim_type, "game_reference": game_id,
        "player_name": player, "team_id": 2,
        "quote": quote or f"{player} is available for {game_id}.", "captured_at": "2026-10-03T01:00:00+08:00"}}


def test_availability_evidence_requires_same_player_and_same_game():
    article = {"analysis_article": "Giannis Antetokounmpo 本场可以出战。"}
    c = editorial_context()
    c["objective_evidence"].append(_web_player_status("Giannis Antetokounmpo", "espn:fixture"))
    assert validate_publication_hard_facts(article, build_editorial_projection(c, LOCKED)) == []
    other_player = editorial_context()
    other_player["objective_evidence"].append(_web_player_status("Tyler Herro", "espn:fixture"))
    assert any(item["code"] == "current_availability_unverified"
               for item in validate_publication_hard_facts(article, build_editorial_projection(other_player, LOCKED)))
    other_game = editorial_context()
    other_game["objective_evidence"].append(_web_player_status("Giannis Antetokounmpo", "espn:other"))
    assert any(item["code"] == "current_availability_unverified"
               for item in validate_publication_hard_facts(article, build_editorial_projection(other_game, LOCKED)))


def test_english_word_output_is_not_parsed_as_an_out_status():
    p = build_editorial_projection(editorial_context(), LOCKED)
    findings = validate_publication_hard_facts({"subjective_rationale": "The model output is unavailable."}, p)
    assert not any(item["code"] == "current_availability_unverified" for item in findings)


def test_explicit_minutes_restriction_requires_matching_game_player_source():
    article = {"analysis_article": "Jimmy Butler is officially on a 24-minute restriction."}
    c = editorial_context()
    p = build_editorial_projection(c, LOCKED)
    assert any(item["code"] == "current_minutes_restriction_unverified"
               for item in validate_publication_hard_facts(article, p))

    c["objective_evidence"].append(_web_player_status(
        "Jimmy Butler", "espn:fixture", claim_type="minutes_restriction",
        quote="Jimmy Butler is on a 24-minute restriction for espn:fixture."))
    p = build_editorial_projection(c, LOCKED)
    assert validate_publication_hard_facts(article, p) == []
    mismatch = {"analysis_article": "Jimmy Butler is officially on a 20-minute restriction."}
    assert any(item["code"] == "confirmed_minutes_restriction_conflict"
               for item in validate_publication_hard_facts(mismatch, p))


def test_conditional_minutes_projection_stays_nonblocking():
    p = build_editorial_projection(editorial_context(), LOCKED)
    assert validate_publication_hard_facts({"analysis_article":
        "If Jimmy Butler were limited to 15–20 minutes, Miami's frontcourt rotation could change."}, p) == []
    unknown = validate_publication_hard_facts({"analysis_article":
        "首发、轮换、分钟限制与伤病状态全部未确认。"}, p)
    assert not any(item["code"] in {"current_minutes_restriction_unverified",
        "confirmed_minutes_restriction_conflict", "current_availability_unverified"} for item in unknown)


def test_direct_coach_quote_requires_quote_bound_coach_comment():
    quote = "我们会把节奏提起来。"
    article = {"analysis_article": f"热火主帅表示：“{quote}”"}
    p = build_editorial_projection(editorial_context(), LOCKED)
    assert any(item["code"] == "unverified_coach_quote"
               for item in validate_publication_hard_facts(article, p))

    c = editorial_context()
    c["objective_evidence"].append({"id": "coach-quote", "section": "web_context",
        "game_id": "espn:fixture", "value": {"eligible_for_fact": True,
        "assertion_kind": "confirmed_fact", "temporal_class": "CURRENT",
        "temporal_scope": "current_pregame", "claim_type": "coach_comments",
        "game_reference": "espn:fixture", "quote": f"热火主帅表示：“{quote}”",
        "captured_at": "2026-10-03T01:00:00+08:00"}})
    p = build_editorial_projection(c, LOCKED)
    assert validate_publication_hard_facts(article, p) == []


def test_non_attributed_quoted_basketball_phrase_is_not_a_fake_coach_quote():
    p = build_editorial_projection(editorial_context(), LOCKED)
    assert validate_publication_hard_facts({"analysis_article": "我把这个阶段叫作“前场磨合期”。"}, p) == []


def test_projected_starter_can_be_reported_as_projected_but_not_confirmed():
    c = editorial_context()
    c["objective_evidence"].append(_web_player_status("Giannis Antetokounmpo", "espn:fixture",
        claim_type="projected_starters", assertion="reported_projection",
        quote="Giannis Antetokounmpo is a projected starter for espn:fixture."))
    p = build_editorial_projection(c, LOCKED)
    assert validate_publication_hard_facts({"analysis_article":
        "A source lists Giannis Antetokounmpo as a projected starter, not confirmed."}, p) == []


def test_confirmed_starter_requires_same_game_confirmed_source():
    c = editorial_context()
    c["objective_evidence"].append(_web_player_status("Giannis Antetokounmpo", "espn:fixture",
        claim_type="confirmed_starter", quote="Giannis Antetokounmpo is a confirmed starter for espn:fixture."))
    p = build_editorial_projection(c, LOCKED)
    assert validate_publication_hard_facts({"analysis_article":
        "Giannis Antetokounmpo is a confirmed starter."}, p) == []


def test_confirmed_starter_source_conflict_is_blocked():
    c = editorial_context()
    c["roster_context"]["teams"]["home"]["official_team_roster"] = {
        "status": "available", "players": [{"name": "Scottie Barnes"}, {"name": "Test Player"}],
        "source": {"captured_at": "2026-10-02T00:00:00+08:00"}}
    c["roster_context"]["teams"]["home"]["confirmed_starters"] = [{"name": "Scottie Barnes"}]
    p = build_editorial_projection(c, LOCKED)
    findings = validate_publication_hard_facts({"analysis_article": "Test Player 是猛龙确认首发。"}, p)
    assert any(item["code"] == "confirmed_starter_source_conflict" for item in findings)


def test_unverified_transaction_claim_is_blocked_but_confirmed_source_allows_it():
    c = editorial_context()
    article = "热火休赛期通过交易得到 Scottie Barnes。"
    p = build_editorial_projection(c, LOCKED)
    assert any(item["code"] == "transaction_claim_unverified"
               for item in validate_publication_hard_facts({"analysis_article": article}, p))
    c["objective_evidence"].append({"id": "trade", "section": "web_context", "value": {
        "eligible_for_fact": True, "assertion_kind": "confirmed_fact", "temporal_class": "CURRENT",
        "claim_type": "roster_transaction", "game_reference": "espn:fixture",
        "quote": "Scottie Barnes was traded to Miami Heat in a confirmed transaction."}})
    p = build_editorial_projection(c, LOCKED)
    assert validate_publication_hard_facts({"analysis_article": article}, p) == []


def test_debut_premise_is_checked_even_when_inside_a_conditional():
    c = editorial_context()
    c["objective_evidence"].append({"id": "giannis-news", "section": "web_context", "value": {
        "eligible_for_fact": True, "assertion_kind": "confirmed_fact", "temporal_class": "BACKGROUND",
        "claim_type": "role_background", "game_reference": "espn:fixture",
        "quote": "Giannis Antetokounmpo joined the Miami Heat."}})
    p = build_editorial_projection(c, LOCKED)
    article = "作为Giannis加盟热火后的首秀，如果主力时间受限，磨合仍难判断。"
    assert any(item["code"] == "unsupported_debut_claim"
               for item in validate_publication_hard_facts({"analysis_article": article}, p))


def test_double_negation_cannot_turn_missing_availability_into_confirmed_playing():
    c = editorial_context()
    c["objective_evidence"].append({"id": "giannis-news", "section": "web_context", "value": {
        "eligible_for_fact": True, "assertion_kind": "confirmed_fact", "temporal_class": "BACKGROUND",
        "claim_type": "role_background", "game_reference": "espn:fixture",
        "quote": "Giannis Antetokounmpo joined the Miami Heat."}})
    p = build_editorial_projection(c, LOCKED)
    article = "并非无法确认 Giannis Antetokounmpo 本场出战，他已经获准上场。"
    assert any(item["code"] == "current_availability_unverified"
               for item in validate_publication_hard_facts({"analysis_article": article}, p))


def test_roster_continuity_and_fit_are_analytical_freedom_without_evidence():
    p = build_editorial_projection(editorial_context(), LOCKED)
    opinions = [
        "我认为猛龙这套核心框架的连续性更好。",
        "热火休赛期变化更大，磨合可能是风险。",
        "这套锋线的适配性很强。",
        "我更看好这套新阵容快速形成化学反应。",
        "这支球队更年轻、更有冲劲，锋线资源也更强。",
        "历史交手的参考价值有限，因为两队阵容已经变化。",
    ]
    for article in opinions:
        assert validate_publication_hard_facts({"analysis_article": article}, p) == []


def test_continuity_opinion_does_not_hide_unverified_confirmed_starter_fact():
    p = build_editorial_projection(editorial_context(), LOCKED)
    article = "Barnes、Kawhi、Barrett 已经确定是猛龙新赛季首发，因此阵容连续性更好。"
    findings = validate_publication_hard_facts({"analysis_article": article}, p)
    assert any(item["code"] == "confirmed_starter_source_unverified" for item in findings)


def test_model_training_size_cannot_be_inferred_from_a_rolling_window():
    p = build_editorial_projection(editorial_context(), LOCKED)
    article = "Rolling近15场都来自上赛季，说明模型完全只用这15场训练出来。"
    assert any(item["code"] == "unsupported_model_training_sample"
               for item in validate_publication_hard_facts({"analysis_article": article}, p))


def test_local_zero_sample_does_not_prove_the_whole_season_opener():
    p = build_editorial_projection(editorial_context(), LOCKED)
    article = "双方本赛季一场球都没打过，所以本场一定是季前赛首战。"
    issues = validate_publication_hard_facts({"analysis_article": article}, p)
    assert any(item["code"] == "unverified_global_season_count" for item in issues)
    assert any(item["code"] == "unsupported_season_opener_claim" for item in issues)


def test_recent_h2h_detail_count_cannot_exceed_frozen_rows():
    c = editorial_context()
    c["matchup_history"] = {"meeting_count": 65, "meetings":[
        {"home_score":128,"away_score":114,"home_team_id":1,"away_team_id":2}]}
    p = build_editorial_projection(c, LOCKED)
    article = "这里已经提供了最近15次交锋的逐场比分明细，可以逐场核验全部15场。"
    assert any(item["code"] == "h2h_detail_count_mismatch"
               for item in validate_publication_hard_facts({"analysis_article": article}, p))


def test_displayed_game_day_uses_beijing_tipoff_date():
    p = build_editorial_projection(editorial_context(), LOCKED)
    issues = validate_publication_hard_facts({"analysis_article": "比赛10月3日开打。"}, p)
    assert any(item["code"] == "game_date_timezone_mismatch" for item in issues)


def test_explicit_current_roster_claim_needs_roster_or_source_support():
    c = editorial_context()
    p = build_editorial_projection(c, LOCKED)
    claim = {"analysis_article": "Scottie Barnes 目前效力于热火。"}
    assert any(item["code"] == "current_roster_claim_unverified"
               for item in validate_publication_hard_facts(claim, p))
    c["roster_context"]["teams"]["home"]["official_team_roster"] = {
        "status": "available", "players": [{"name": "Scottie Barnes"}],
        "source": {"captured_at": "2026-10-02T00:00:00+08:00"}}
    p = build_editorial_projection(c, LOCKED)
    assert any(item["code"] == "current_roster_team_mismatch"
               for item in validate_publication_hard_facts(claim, p))


@pytest.mark.parametrize("field,article", [
    ("winner_side", "第25秒最终预测猛龙胜。"),
    ("winner_side", "我的最终预测是热火净负3.5分，总分228.5。"),
    ("win_probability", "第25秒最终预测热火胜，胜率57%。"),
    ("expected_margin", "第25秒最终预测热火预计赢4.5分。"),
    ("expected_total", "第25秒最终预测总分229.5。"),
    ("expected_total", "我最终给总分二百三十点五。"),
])
def test_final_forecast_text_must_match_locked_numbers(field, article):
    locked = {**LOCKED, "win_probability": 0.56, "expected_margin": 3.5, "expected_total": 228.5}
    p = build_editorial_projection(editorial_context(), locked)
    findings = validate_publication_hard_facts({"analysis_article": article}, p)
    assert any(item["code"] == "locked_forecast_text_mismatch" and item["field"] == field for item in findings)


def test_correct_locked_prediction_numbers_are_allowed_in_chinese_number_words():
    locked = {**LOCKED, "win_probability": 0.56, "expected_margin": 3.5, "expected_total": 228.5}
    p = build_editorial_projection(editorial_context(), locked)
    article = "我选热火，胜率百分之五十六，预计净胜三点五分，总分二百二十八点五。"
    assert validate_publication_hard_facts({"analysis_article": article}, p) == []


def _decision_projection_for_locked_numbers(final, independent):
    home_margin = final.get("expected_home_margin")
    winner_positive = (float(final.get("expected_margin")) if final.get("expected_margin") is not None
                       else float(home_margin) if final["winner_side"] == "home" else -float(home_margin))
    locked = {"winner_side": final["winner_side"], "win_probability": final["win_probability"],
              "expected_margin": winner_positive, "expected_total": final["expected_total"]}
    provenance = {"analysis_core": {}, "independent_prediction": independent,
        "final_prediction": final, "revision_delta": {}, "market_provenance": {}}
    material = [{"_contract_item": "forecast_provenance", "value": provenance}]
    return build_editorial_projection(editorial_context(), locked, material)


def test_v2_public_decision_numbers_are_the_locked_authority_for_hard_fact_gate():
    final = {"winner_side": "home", "win_probability": .72,
             "expected_home_margin": 7.5, "expected_total": 226.5}
    independent = {"winner_side": "home", "win_probability": .78,
                   "expected_home_margin": 9.5, "expected_total": 224.5}
    projection = _decision_projection_for_locked_numbers(final, independent)
    assert projection["protocol"] == "editorial-decision-v2"
    text = "第25秒最终预测尼克斯胜，胜率72%，预计尼克斯赢7.5分，总分226.5。"
    assert validate_publication_hard_facts({"analysis_article": text}, projection) == []


def test_v2_repeating_final_numbers_passes_when_independent_prediction_differs():
    final = {"winner_side": "home", "win_probability": .72,
             "expected_home_margin": 7.5, "expected_total": 226.5}
    independent = {"winner_side": "home", "win_probability": .78,
                   "expected_home_margin": 9.5, "expected_total": 224.5}
    projection = _decision_projection_for_locked_numbers(final, independent)
    text = "最终我站尼克斯，胜率72%，预计尼克斯赢7.5分，总分226.5。"
    assert validate_publication_hard_facts({"analysis_article": text}, projection) == []


def test_v2_repeating_independent_numbers_as_final_is_blocked():
    final = {"winner_side": "home", "win_probability": .72,
             "expected_home_margin": 7.5, "expected_total": 226.5}
    independent = {"winner_side": "home", "win_probability": .78,
                   "expected_home_margin": 9.5, "expected_total": 224.5}
    projection = _decision_projection_for_locked_numbers(final, independent)
    text = "第25秒最终预测尼克斯胜，胜率78%，预计尼克斯赢9.5分，总分224.5。"
    findings = validate_publication_hard_facts({"analysis_article": text}, projection)
    fields = {item.get("field") for item in findings if item["code"] == "locked_forecast_text_mismatch"}
    assert {"win_probability", "expected_margin", "expected_total"} <= fields


def test_v2_away_winner_uses_signed_home_margin_but_public_margin_is_winner_positive():
    final = {"winner_side": "away", "win_probability": .56,
             "expected_home_margin": -2.5, "expected_total": 224.5,
             # ATS margin is deliberately different and must not substitute for Final margin.
             "model_home_margin": 5.5}
    independent = {"winner_side": "away", "win_probability": .54,
                   "expected_home_margin": -1.5, "expected_total": 225.0}
    projection = _decision_projection_for_locked_numbers(final, independent)
    text = "第25秒最终预测热火胜，胜率56%，预计热火赢2.5分，总分224.5。"
    assert validate_publication_hard_facts({"analysis_article": text}, projection) == []


def test_repair_schema_does_not_expose_locked_prediction_numbers_as_text_units():
    from .editorial_contract import visible_units
    from .editorial_pipeline import apply_patches
    judgment = {"analysis_article": "短文。", "final_prediction": {
        "winner_side": "home", "win_probability": .72, "expected_margin": 7.5, "expected_total": 226.5}}
    units = visible_units(judgment)
    assert not any("final_prediction" in unit["id"] or "final_prediction" in unit["aliases"] for unit in units)
    with pytest.raises(ValueError, match="editorial_repair_target_not_publicly_editable"):
        apply_patches({"title": "x", "lead": "y", "article": "短文。", "watch_themes": [],
            "confidence": "low", "player_analysis": []},
            [{"id": "final_prediction.expected_total", "aliases": ["final_prediction.expected_total"], "text": "226.5"}],
            [{"unit_id": "final_prediction.expected_total", "replacement": "229"}],
            {"final_prediction.expected_total"})


def test_forecast_reasoning_is_available_as_opinion_material():
    material = [{"summary": "如果新前场能同时出场，护框与篮板可能改善。", "effect": "away",
                 "evidence_ids": ["form"]}]
    p = build_editorial_projection(editorial_context(), LOCKED, material)
    item = next(x for x in p["facts"] if x["kind"] == "ANALYST_REASONING")
    assert item["value"]["statement"] == material[0]["summary"]
    assert "not objective fact or ML feature attribution" in item["value"]["scope"]
    assert "ANALYST_REASONING" in model_view(p)["scope_legend"]


def test_ai_can_disagree_with_baseline_but_cannot_invent_ml_feature_causality():
    p = build_editorial_projection(editorial_context(), LOCKED)
    allowed = {"analysis_article": "数据模型更偏猛龙，但我更看重新阵容的篮板和护框。"}
    baseline = {"analysis_article": "Pure模型的胜率是60%，第25秒最终预测仍倾向热火。"}
    blocked = {"analysis_article": "Pure模型因为猛龙净效率更高，所以给了60%胜率。"}
    assert validate_publication_hard_facts(allowed, p) == []
    assert validate_publication_hard_facts(baseline, p) == []
    assert any(item["code"] == "unsupported_ml_attribution"
               for item in validate_publication_hard_facts(blocked, p))


def test_historical_score_claim_must_match_a_provided_matchup_record():
    c = editorial_context()
    c["matchup_history"] = {"meeting_count": 1, "meetings": [{
        "home_score": 128, "away_score": 114, "home_team_id": 1, "away_team_id": 2,
        "season_start": 2025}]}
    p = build_editorial_projection(c, LOCKED)
    valid = validate_publication_hard_facts({"analysis_article": "上次交手猛龙以128比114获胜。"}, p)
    invalid = validate_publication_hard_facts({"analysis_article": "上次交手猛龙以120比114获胜。"}, p)
    assert valid == []
    assert any(item["code"] == "historical_score_mismatch" for item in invalid)


def test_exact_win_loss_record_must_match_structured_season_form():
    c = editorial_context()
    c["season_form"]["teams"]["away"]["previous_last10"].update({"wins": 4, "losses": 6})
    p = build_editorial_projection(c, LOCKED)
    assert validate_publication_hard_facts(
        {"analysis_article": "热火上赛季近10场战绩4胜6负。"}, p) == []
    issues = validate_publication_hard_facts(
        {"analysis_article": "热火上赛季近10场战绩7胜3负。"}, p)
    assert any(item["code"] == "exact_record_mismatch" for item in issues)


@pytest.mark.parametrize("run", ["20261002T170352_76ca0bb0c7", "20261002T183747_2b8de7d8cb"])
def test_frozen_attempt_files_remain_unchanged(run):
    from .storage import digest
    root = Path(__file__).parent / "outputs/heat_raptors_real_acceptance" / run
    before = {path: digest(path) for path in root.rglob("*") if path.is_file()}
    forecast = json.loads(next(root.glob("analyst/*/forecast/final.json")).read_text(encoding="utf-8"))
    for path in root.glob("analyst/*/context.json"):
        context = json.loads(path.read_text(encoding="utf-8"))
        numbers = forecast["final_prediction"]
        locked = {"winner_side": numbers["winner_side"], "win_probability": numbers["win_probability"],
                  "expected_margin": abs(numbers["expected_home_margin"]), "expected_total": numbers["expected_total"],
                  "evidence_ids": []}
        assert build_editorial_projection(context, locked)["game_id"] == context["game"]["game_id"]
    assert all(digest(path) == value for path, value in before.items())


@pytest.mark.parametrize("text", [
    "公牛五号位的护框和篮板都打了折扣。",
    "独行侠护框和防守篮板的第一责任人。",
    "公牛5号位的护框和篮板都打了折扣。",
    "猛龙进攻端多一个稳定得分点还是少一个。",
])
def test_position_and_scoring_option_counts_are_not_boxscore_values(text):
    from .editorial_hard_facts import _stat_issues_for_text
    projection = {"game": deepcopy(GAME), "facts": []}
    assert _stat_issues_for_text(text, projection, "article") == []


@pytest.mark.parametrize("text,metric", [
    ("公牛上季场均篮板5个。", "rebounds_per_game"),
    ("猛龙上季场均得分1分。", "points_for_per_game"),
])
def test_real_stat_values_remain_factual_beside_position_fix(text, metric):
    from .editorial_hard_facts import _NUMBER_RE, classify_numeric_claim_type
    token = next(_NUMBER_RE.finditer(text))
    assert classify_numeric_claim_type(text, token.start(), token.end(), metric, {"game": GAME}) == "historical_current_factual_statistic"
