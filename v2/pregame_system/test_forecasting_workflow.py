from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from .forecasting_workflow import (
    ForecastNumbers, IndependentForecast, MarketReviewForecast,
    VersionedIndependentForecast, VersionedMarketReviewForecast,
    build_revision_delta, market_timing_status, validate_market_timing_claims,
    validate_market_tiebreak_repair,
    prepare_pilot, prepare_analyst, run_two_stage,
)
from .forecasting_pilot import packet_content_sha256, forecast_one, run_forecast_batch


def packet(number=1):
    p = {"packet_id": f"fixture-{number}", "game": {"game_id": f"fixture-{number}",
         "game_number": number, "date": "2026-01-01", "home": {"abbr": "okc", "name": "Oklahoma City Thunder"},
         "away": {"abbr": "hou", "name": "Houston Rockets"}},
         "team_state": {"wins": 12, "note": "OKC is the favorite at -7.5"},
         "richer_team_data": {"off_rating": 119.7},
         "injuries": {"entries": [{"player": "Test Player", "status": "Out"}]},
         "starters": {"status": "unavailable"},
         "roster_changes": {"official_team_roster": ["Test Player"]},
         "quantitative_baselines": {"pure": {"expected_margin": 4.2, "expected_total": 224.2},
             "residual": {"value": 99991}, "official_numeric": {"value": 99992}},
         "market_reference": {"market_expected_home_margin": 7.5, "home_handicap_points": -7.5,
             "total_line_points": 228.5, "spread_points": 7.5, "quote_timestamp": None},
         "important_context": ["The market favors home by 7.5", "Visitor has one rest day"],
         "sources": [{"summary": "盘口上涨到7.5", "usable_for_prediction": True},
                     {"summary": "Player Out", "published_before_tipoff": False}]}
    p["packet_sha256"] = packet_content_sha256(p)
    return p


def output(home_margin=4.5, total=224.5, refs=None, review=False, decision="keep"):
    value = {"prediction": {"winner_side": "home" if home_margin > 0 else "away",
        "win_probability": 0.62, "expected_home_margin": home_margin, "expected_total": total},
        "factors": [{"classification": "inference", "evidence_ids": refs or (["model.pure", "market.reference"] if review else ["model.pure"]),
                     "effect": "home", "summary": "Prior team data supports this direction; player usage is uncertain."}],
        "uncertainties": ["Confirmed lineup unavailable"]}
    if review:
        value.update(decision=decision, market_assessment="unclear", rationale="The supplied team evidence still supports the saved forecast.")
    return value


def analysis_core(refs=None):
    refs = refs or ["model.pure"]
    return {
        "central_question": "Can the Lakers' ball handlers stress Golden State's switching before the Warriors' veteran load limits their offense?",
        "central_thesis": {
            "statement": "The matchup is most sensitive to which side can keep its primary creators effective in a shortened preseason rotation.",
            "classification": "inference", "evidence_ids": refs[:1], "confidence": "medium",
        },
        "matchup_status": "identified",
        "key_matchups": [{
            "subject": "Luka Doncic's pick-and-roll creation",
            "opponent_or_scheme": "Golden State's switching and help coverage",
            "why_it_matters": "This tests whether the Lakers can create a second-side advantage without relying on a full-game workload.",
            "expected_effect": "If the switch holds, the game stays closer; if the Lakers force help, their interior finishers gain cleaner opportunities.",
            "classification": "inference", "evidence_ids": refs[:1], "confidence": "medium",
            "uncertainty": "Preseason minutes and the actual coverage are not confirmed.",
        }],
        "expected_game_script": "The Lakers try to use Doncic to bend the first coverage while the Warriors rely on Curry-led movement. If a side cannot sustain its planned half-court creation, the other can control the late possessions; preseason workload remains unknown.",
        "flip_conditions": [{
            "condition": "Curry and Green play materially less than the estimated ranges.",
            "judgment_effect": "The home-side creation and defensive communication assumptions weaken, increasing the Lakers' chance to control the game.",
            "evidence_ids": refs[:1],
        }],
    }


def versioned_output(home_margin=4.5, total=224.5, refs=None, *, review=False, decision="keep"):
    refs = refs or ["basketball.fact"]
    value = output(home_margin, total, refs=refs, review=review, decision=decision)
    value["analysis_core"] = analysis_core(refs)
    if review:
        value["factors"] = [
            {"classification": "source_fact", "evidence_ids": ["market.quote"],
             "effect": "home", "summary": "Observed quote prices the home side as a favorite."},
            {"classification": "inference", "evidence_ids": ["basketball.fact"],
             "effect": "home", "summary": "Reweighting the home-court matchup raises the home-side estimate."},
        ]
        value["revision_reason"] = {
            "market_signal_factor_indices": [0],
            "basketball_reweighting_factor_indices": [1] if decision == "revise" else [],
        }
    return value


def test_versioned_forecast_requires_a_structured_analysis_core():
    base = output()
    with pytest.raises(ValidationError):
        VersionedIndependentForecast.model_validate(base)
    base["analysis_core"] = analysis_core()
    parsed = VersionedIndependentForecast.model_validate(base)
    assert parsed.analysis_core.central_question.startswith("Can the Lakers")
    assert parsed.analysis_core.key_matchups[0].subject.startswith("Luka Doncic")
    assert parsed.analysis_core.flip_conditions


def invalid_v2_thesis_output():
    value = versioned_output(4.5, 224.5, refs=["basketball.fact", "model.pure"])
    value["analysis_core"]["central_thesis"] = value["analysis_core"]["central_thesis"]["statement"]
    return value


def v2_schema_repair_fixture_basketball():
    return {"game": {"game_id": "espn:schema-repair", "game_type": "regular_season",
                      "home": {"id": 1, "name": "Home"}, "away": {"id": 2, "name": "Away"}},
        "evidence": [
            {"id": "basketball.fact", "section": "team_form", "value": {"note": "verified matchup fact"}},
            {"id": "model.pure", "section": "pure_prediction", "value": {"note": "independent baseline"}},
        ]}


def test_v2_schema_repair_converts_thesis_shape_once_and_preserves_prediction(tmp_path):
    original = invalid_v2_thesis_output()
    original_numbers = copy.deepcopy(original["prediction"])
    calls = []

    def client(messages, schema):
        calls.append(messages)
        if len(calls) == 1:
            assert schema is VersionedIndependentForecast
            return json.dumps(original, ensure_ascii=False), {"finish_reason": "stop"}
        assert schema is VersionedIndependentForecast
        assert "schema-only repairer" in messages[0]["content"]
        request = json.loads(messages[1]["content"])
        assert request["repair_mode"] == "schema_only"
        assert request["original_response"]["analysis_core"]["central_thesis"] == original["analysis_core"]["central_thesis"]
        assert request["locked_prediction"] == original_numbers
        assert "basketball.fact" in request["allowed_evidence_ids"]
        repaired = copy.deepcopy(original)
        statement = repaired["analysis_core"]["central_thesis"]
        repaired["analysis_core"]["central_thesis"] = {
            "statement": statement, "classification": "inference",
            "evidence_ids": ["basketball.fact"], "confidence": "medium",
        }
        return json.dumps(repaired, ensure_ascii=False), {"finish_reason": "stop"}

    result = run_two_stage(v2_schema_repair_fixture_basketball(), {"evidence": []}, tmp_path / "run",
        client=client, skill_loader=lambda: "FORECAST", require_analysis_core=True)
    root = tmp_path / "run"
    assert len(calls) == 2
    assert result["independent"]["prediction"] == original_numbers
    assert result["independent"]["analysis_core"]["central_thesis"]["statement"] == original["analysis_core"]["central_thesis"]
    provenance = result["schema_repair_provenance"]
    assert provenance["schema_repair_used"] is True and provenance["attempt_count"] == 1
    assert provenance["status"] == "accepted"
    assert json.loads((root / "independent_response.json").read_text(encoding="utf-8"))["raw"] == json.dumps(original, ensure_ascii=False)
    assert (root / "independent_validation_error.json").is_file()
    assert (root / "independent_schema_repair_request.json").is_file()
    assert (root / "independent_schema_repair_response.json").is_file()
    assert (root / "independent_schema_repair_validation.json").is_file()
    assert json.loads((root / "schema_repair_provenance.json").read_text(encoding="utf-8"))["schema_repair_used"] is True


@pytest.mark.parametrize("mutation", ["winner", "probability", "margin", "total", "thesis_text"])
def test_v2_schema_repair_rejects_changed_prediction_or_judgment(tmp_path, mutation):
    original = invalid_v2_thesis_output()
    def client(messages, schema):
        if len([p for p in (tmp_path / "run").glob("*_request.json")]) == 1:
            return json.dumps(original, ensure_ascii=False), {}
        repaired = copy.deepcopy(original)
        thesis = repaired["analysis_core"]["central_thesis"]
        repaired["analysis_core"]["central_thesis"] = {
            "statement": thesis, "classification": "inference",
            "evidence_ids": ["basketball.fact"], "confidence": "medium",
        }
        if mutation == "winner":
            repaired["prediction"].update(winner_side="away", expected_home_margin=-4.5)
        elif mutation == "probability":
            repaired["prediction"]["win_probability"] = .63
        elif mutation == "margin":
            repaired["prediction"]["expected_home_margin"] = 5.0
        elif mutation == "total":
            repaired["prediction"]["expected_total"] = 225.0
        else:
            repaired["analysis_core"]["central_thesis"]["statement"] = "A different basketball thesis."
        return json.dumps(repaired, ensure_ascii=False), {}

    with pytest.raises(ValueError, match="forecast_schema_repair_failed"):
        run_two_stage(v2_schema_repair_fixture_basketball(), {"evidence": []}, tmp_path / "run",
            client=client, skill_loader=lambda: "FORECAST", require_analysis_core=True)
    provenance = json.loads((tmp_path / "run" / "schema_repair_provenance.json").read_text(encoding="utf-8"))
    assert provenance["schema_repair_used"] is True
    assert provenance["status"] == "failed"


def test_v2_schema_repair_rejects_unknown_evidence_id(tmp_path):
    original = invalid_v2_thesis_output()
    calls = 0
    def client(_messages, _schema):
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps(original, ensure_ascii=False), {}
        repaired = copy.deepcopy(original)
        thesis = repaired["analysis_core"]["central_thesis"]
        repaired["analysis_core"]["central_thesis"] = {
            "statement": thesis if isinstance(thesis, str) else thesis["statement"],
            "classification": "inference", "evidence_ids": ["fabricated-evidence-id"],
            "confidence": "medium",
        }
        return json.dumps(repaired, ensure_ascii=False), {}
    with pytest.raises(ValueError, match="forecast_schema_repair_output_invalid"):
        run_two_stage(v2_schema_repair_fixture_basketball(), {"evidence": []}, tmp_path / "run",
            client=client, skill_loader=lambda: "FORECAST", require_analysis_core=True)
    assert calls == 2
    assert not (tmp_path / "run" / "independent.json").exists()
    provenance = json.loads((tmp_path / "run" / "schema_repair_provenance.json").read_text(encoding="utf-8"))
    assert provenance["status"] == "failed"
    assert "unknown_forecast_evidence" in provenance["independent_validation_error"]


def test_v2_schema_repair_cannot_change_existing_lineup_or_workload_estimates(tmp_path):
    original = invalid_v2_thesis_output()
    original["pregame_plan"] = {
        "lineups": [
            {"side": "home", "basis": "unavailable", "players": [], "evidence_ids": [], "rationale": "尚未确认"},
            {"side": "away", "basis": "unavailable", "players": [], "evidence_ids": [], "rationale": "尚未确认"},
        ],
        "player_usage": [{"side": "away", "player_name": "Player A", "basis": "agent_estimate",
            "minutes_min": 28, "minutes_max": 34, "evidence_ids": ["basketball.fact"],
            "rationale": "原始预计负荷范围。"}],
        "key_matchup": "后场持球压力", "uncertainty": ["首发未知"],
    }
    calls = 0
    def client(_messages, _schema):
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps(original, ensure_ascii=False), {}
        repaired = copy.deepcopy(original)
        thesis = repaired["analysis_core"]["central_thesis"]
        repaired["analysis_core"]["central_thesis"] = {
            "statement": thesis, "classification": "inference",
            "evidence_ids": ["basketball.fact"], "confidence": "medium",
        }
        repaired["pregame_plan"]["player_usage"][0]["minutes_max"] = 30
        return json.dumps(repaired, ensure_ascii=False), {}
    with pytest.raises(ValueError, match="forecast_schema_repair_failed"):
        run_two_stage(v2_schema_repair_fixture_basketball(), {"evidence": []}, tmp_path / "run",
            client=client, skill_loader=lambda: "FORECAST", require_analysis_core=True)
    assert calls == 2
    provenance = json.loads((tmp_path / "run" / "schema_repair_provenance.json").read_text(encoding="utf-8"))
    assert provenance["status"] == "failed"
    assert "schema_repair_changed_existing_value:pregame_plan.player_usage[0].minutes_max" in provenance["repair_error"]


def test_v2_schema_repair_that_remains_invalid_fails_closed(tmp_path):
    original = invalid_v2_thesis_output()
    calls = []
    def client(_messages, _schema):
        calls.append(1)
        return json.dumps(original, ensure_ascii=False), {}
    with pytest.raises(ValueError, match="forecast_schema_repair_failed"):
        run_two_stage(v2_schema_repair_fixture_basketball(), {"evidence": []}, tmp_path / "run",
            client=client, skill_loader=lambda: "FORECAST", require_analysis_core=True)
    assert len(calls) == 2
    assert (tmp_path / "run" / "independent_schema_repair_validation_error.json").is_file()
    assert not (tmp_path / "run" / "independent.json").exists()


def test_v2_default_client_applies_larger_budgets_without_changing_v1(monkeypatch):
    import importlib
    from types import SimpleNamespace
    from .forecasting_workflow import default_client

    transport = importlib.import_module("v2.agent.generate_historical_pregame_analysis")
    seen = []

    def fake_request(messages, key, model, max_tokens, *, timeout_seconds, response_format=None):
        seen.append((model, max_tokens, timeout_seconds, response_format))
        return SimpleNamespace(content="{}", usage={"completion_tokens": 1},
                               finish_reason="stop", response_metadata={})

    monkeypatch.setattr(transport, "request_deepseek", fake_request)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    messages = [{"role": "user", "content": json.dumps({"basketball": {"game": {
        "game_type": "regular_season", "registered_game": False}}})}]
    default_client(messages, VersionedIndependentForecast)
    default_client(messages, VersionedMarketReviewForecast)
    default_client(messages, IndependentForecast)
    preseason = [{"role": "user", "content": json.dumps({"basketball": {"game": {
        "game_type": "preseason", "registered_game": False}}})}]
    default_client(preseason, MarketReviewForecast)
    assert [row[1] for row in seen] == [4800, 3600, 2400, 4200]
    assert [row[3] for row in seen] == [{"type": "json_object"}, {"type": "json_object"}, None, None]


def test_revision_delta_is_computed_from_independent_and_final_numbers():
    before = {"winner_side": "away", "win_probability": .56,
              "expected_home_margin": -2.5, "expected_total": 224.5}
    after = {"winner_side": "home", "win_probability": .55,
             "expected_home_margin": 3.0, "expected_total": 223.5}
    delta = build_revision_delta(before, after)
    assert delta == {
        "winner_changed": True,
        "home_win_probability_delta": pytest.approx(.11),
        "expected_home_margin_delta": pytest.approx(5.5),
        "expected_total_delta": pytest.approx(-1.0),
        "changed_axes": ["winner", "win_probability", "expected_home_margin", "expected_total"],
    }


def test_market_timing_status_never_treats_capture_as_publication_time():
    assert market_timing_status({"published_at": "2026-10-06T18:00:00+00:00"}) == "published_at_verified"
    assert market_timing_status({"published_at": "2026-10-06"}) == "published_date_only"
    assert market_timing_status({"published_at": None, "observed_at": "2026-10-06T18:28:50+00:00"}) == "observed_only"
    assert market_timing_status({"published_at": None}) == "unknown"


def test_production_two_stage_contract_records_winner_flip_and_observed_only_timing(tmp_path):
    basketball = {"game": {"game_type": "regular_season", "registered_game": True},
        "evidence": [{"id": "basketball.fact", "section": "team_form", "value": {"note": "home court context"}},
                     {"id": "model.pure", "section": "pure_prediction", "value": {"note": "baseline"}}]}
    market = {"evidence": [{"id": "market.quote", "section": "market", "value": {"home_spread": -4.5}}]}
    calls = []

    def client(messages, schema):
        calls.append(schema)
        if schema is VersionedIndependentForecast:
            return versioned_output(-2.5, 224.5, ["basketball.fact", "model.pure"]), {}
        assert schema is VersionedMarketReviewForecast
        return versioned_output(3.0, 223.5, ["basketball.fact", "market.quote"],
                                review=True, decision="revise"), {}

    def collector(_game):
        return {"status": "available", "evidence": market["evidence"],
                "public_market_reference": {"status": "available", "home_spread": -4.5,
                    "away_spread": 4.5, "expected_home_margin": 4.5, "total": 220.5,
                    "source_url": "https://example.test/quote", "sportsbook": "Draft Kings",
                    "published_at": None, "observed_at": "2026-10-06T18:28:50+00:00",
                    "captured_at": "2026-10-06T18:28:50+00:00"}}

    result = run_two_stage(basketball, market, tmp_path / "run", client=client,
        market_collector=collector, require_analysis_core=True)
    assert calls == [VersionedIndependentForecast, VersionedMarketReviewForecast]
    independent_request = json.loads((tmp_path / "run" / "independent_request.json").read_text(encoding="utf-8"))
    assert independent_request["default_request_settings"]["max_tokens"] == 4800
    assert {"prediction", "factors", "uncertainties", "pregame_plan", "analysis_core"} <= set(
        independent_request["schema"]["properties"])
    core_schema = independent_request["schema"]["$defs"]["AnalysisCore"]
    assert {"central_question", "central_thesis", "key_matchups", "expected_game_script", "flip_conditions"} <= set(
        core_schema["properties"])
    assert independent_request["schema"]["$defs"]["ForecastClaim"]["properties"]["statement"]["maxLength"] == 300
    assert independent_request["schema"]["$defs"]["PregamePlan"]["properties"]["key_matchup"]["maxLength"] == 120
    assert "不得把 pregame_plan 写成第二份" in independent_request["messages"][0]["content"]
    assert core_schema["properties"]["central_thesis"]["$ref"] == "#/$defs/ForecastClaim"
    assert set(independent_request["schema"]["$defs"]["ForecastClaim"]["required"]) == {
        "statement", "classification", "evidence_ids", "confidence"}
    assert "central_thesis 必须是 ForecastClaim 对象，不能是字符串" in independent_request["messages"][0]["content"]
    assert result["schema_repair_provenance"]["schema_repair_used"] is False
    review_request = json.loads((tmp_path / "run" / "market_review_request.json").read_text(encoding="utf-8"))
    assert review_request["default_request_settings"]["max_tokens"] == 3600
    assert "pregame_plan" not in review_request["schema"]["properties"]
    assert not {"PregamePlan", "LineupPlan", "PlayerUsagePlan"} & set(review_request["schema"].get("$defs", {}))
    review_core = review_request["schema"]["$defs"]["AnalysisCore"]
    assert {"central_thesis", "key_matchups", "expected_game_script", "flip_conditions"} <= set(
        review_core["properties"])
    assert "不要扩写既有 analysis_core" in review_request["messages"][0]["content"]
    review_input = json.loads(review_request["messages"][1]["content"])
    deterministic = review_input["deterministic_spread_comparison"]
    assert deterministic["independent"]["model_home_margin"] == -2.5
    assert deterministic["market"]["market_implied_home_margin"] == 4.5
    assert deterministic["independent"]["spread_edge"] == -7.0
    assert deterministic["independent"]["ats_lean_reference"]["display"] == "away +4.5"
    assert result["schema_version"] == "two-stage-forecast-v2"
    assert result["revision_delta"]["winner_changed"] is True
    assert result["revision_delta"]["changed_axes"] == ["winner", "win_probability", "expected_home_margin", "expected_total"]
    assert result["market_provenance"]["market_timing_status"] == "observed_only"
    assert result["market_provenance"]["information_overlap"] == "unknown"
    assert result["market_provenance"]["market_signal_factors"][0]["evidence_ids"] == ["market.quote"]
    assert result["market_provenance"]["basketball_reweighting_factors"][0]["evidence_ids"] == ["basketball.fact"]
    assert result["independent"]["prediction"]["winner_side"] == "away"
    assert result["final_prediction"]["winner_side"] == "home"
    assert result["spread_comparison"]["independent"]["spread_edge"] == -7.0
    assert result["spread_comparison"]["final"]["spread_edge"] == -1.5
    assert result["spread_comparison"]["independent"]["ats_lean"] == "away"
    assert result["spread_comparison"]["final"]["ats_lean"] == "away"
    assert result["market_provenance"]["winner_relationship"]["independent"] == "winner_opposed"
    assert result["market_provenance"]["winner_relationship"]["final"] == "winner_aligned"


def test_market_review_gets_required_tie_break_and_repairs_once_without_rewriting_analysis(tmp_path):
    basketball={"game":{"game_id":"espn:tiebreak","home":{"id":1,"name":"Warriors"},
        "away":{"id":2,"name":"Lakers"},"game_type":"regular_season","registered_game":True},
        "evidence":[{"id":"basketball.fact","section":"team_form","value":{"note":"verified matchup fact"}},
                    {"id":"model.pure","section":"pure_prediction","value":{"note":"baseline"}}]}
    market_evidence={"id":"market.quote","section":"market","value":{"market_reference":{"home_spread":-4.5}}}
    quote={"status":"available","game_identity":{"game_id":"espn:tiebreak","home_team_id":1,"away_team_id":2,
        "home_team":"Warriors","away_team":"Lakers"},"home_spread":-4.5,"away_spread":4.5,
        "expected_home_margin":4.5,"sportsbook":"Draft Kings","source_url":"https://example.test/odds",
        "captured_at":"2026-10-01T11:59:00Z","observed_at":"2026-10-01T11:59:00Z"}
    collection={"status":"available","public_market_reference":quote,"evidence":[market_evidence]}
    stages=[]
    def client(messages,schema):
        payload=json.loads(messages[1]["content"])
        stages.append(payload.get("tie_break_context"))
        if schema is VersionedIndependentForecast:
            return versioned_output(4.5,224.5,["basketball.fact","model.pure"]),{}
        if payload.get("tie_break_context"):
            assert "ATS tie-break" in messages[0]["content"]
            return versioned_output(5.0,224.5,["basketball.fact","market.quote"],
                                    review=True,decision="revise"),{}
        assert payload["deterministic_spread_comparison"]["tie_break_required"] is True
        assert payload["deterministic_spread_comparison"]["independent"]["spread_edge"]==0.0
        return versioned_output(4.5,224.5,["basketball.fact","market.quote"],
                                review=True,decision="keep"),{}
    result=run_two_stage(basketball,{"evidence":[market_evidence]},tmp_path/"tiebreak",
        client=client,market_collector=lambda _game:collection,require_analysis_core=True)
    assert len(stages)==3 and stages[0] is None and stages[1] is None
    assert stages[2]["reason"]=="final_model_home_margin_equals_market_implied_home_margin"
    assert result["independent"]["prediction"]["expected_home_margin"]==4.5
    assert result["independent"]["prediction"]["winner_side"]=="home"
    assert result["final_prediction"]["expected_home_margin"]==5.0
    assert result["spread_comparison"]["independent"]["spread_edge"]==0.0
    assert result["spread_comparison"]["independent"]["ats_lean"]=="no_edge"
    assert result["spread_comparison"]["final"]["spread_edge"]==0.5
    assert result["spread_comparison"]["final"]["ats_lean"]=="home"
    assert result["market_provenance"]["final_spread_tiebreak"]=={
        "initially_required":True,"required":True,"attempted":True,"resolved":True,"attempt_count":1}


def test_tiebreak_still_equal_to_market_fails_without_programmatic_nudge(tmp_path):
    basketball={"game":{"game_id":"espn:tiebreak-fail","home":{"id":1,"name":"Warriors"},
        "away":{"id":2,"name":"Lakers"},"game_type":"regular_season","registered_game":True},
        "evidence":[{"id":"basketball.fact","section":"team_form","value":{"note":"verified matchup fact"}}]}
    market_evidence={"id":"market.quote","section":"market","value":{"market_reference":{"home_spread":-4.5}}}
    quote={"status":"available","game_identity":{"game_id":"espn:tiebreak-fail","home_team_id":1,"away_team_id":2},
        "home_spread":-4.5,"away_spread":4.5,"expected_home_margin":4.5}
    calls=[]
    def client(messages,schema):
        payload=json.loads(messages[1]["content"]);calls.append(bool(payload.get("tie_break_context")))
        if schema is VersionedIndependentForecast:
            return versioned_output(4.5,224.5,["basketball.fact"]),{}
        return versioned_output(4.5,224.5,["basketball.fact","market.quote"],
                                review=True,decision="keep"),{}
    with pytest.raises(ValueError,match="market_review_tiebreak_final_spread_edge_zero"):
        run_two_stage(basketball,{"evidence":[market_evidence]},tmp_path/"tiebreak-fail",client=client,
            market_collector=lambda _game:{"status":"available","public_market_reference":quote,
                                           "evidence":[market_evidence]},require_analysis_core=True)
    assert calls==[False,False,True]
    assert not (tmp_path/"tiebreak-fail"/"final.json").exists()


def test_tiebreak_repair_cannot_rewrite_factors_or_game_script():
    previous=VersionedMarketReviewForecast.model_validate(versioned_output(
        4.5,224.5,["basketball.fact","market.quote"],review=True,decision="keep"))
    candidate=VersionedMarketReviewForecast.model_validate(versioned_output(
        5.0,224.5,["basketball.fact","market.quote"],review=True,decision="revise"))
    candidate.factors[0].summary="rewritten basketball case"
    independent=VersionedIndependentForecast.model_validate(
        {**versioned_output(4.5,224.5,["basketball.fact","model.pure"]),"analysis_core":analysis_core()})
    with pytest.raises(ValueError,match="market_tie_break_changed_locked_field:factors"):
        validate_market_tiebreak_repair(previous,candidate,independent,4.5)


def test_market_review_cannot_create_a_new_exact_tie_from_nonzero_independent_edge(tmp_path):
    basketball={"game":{"game_id":"espn:tiebreak-final","home":{"id":1,"name":"Warriors"},
        "away":{"id":2,"name":"Lakers"},"game_type":"regular_season","registered_game":True},
        "evidence":[{"id":"basketball.fact","section":"team_form","value":{"note":"verified matchup fact"}},
                    {"id":"model.pure","section":"pure_prediction","value":{"note":"baseline"}}]}
    market_evidence={"id":"market.quote","section":"market","value":{"market_reference":{"home_spread":-4.5}}}
    quote={"status":"available","game_identity":{"game_id":"espn:tiebreak-final","home_team_id":1,"away_team_id":2},
        "home_spread":-4.5,"away_spread":4.5,"expected_home_margin":4.5}
    requested_tiebreak=[]
    def client(messages,schema):
        payload=json.loads(messages[1]["content"])
        if schema is VersionedIndependentForecast:
            return versioned_output(3.5,224.5,["basketball.fact","model.pure"]),{}
        if payload.get("tie_break_context"):
            requested_tiebreak.append(payload["tie_break_context"])
            return versioned_output(4.0,224.5,["basketball.fact","market.quote"],
                                    review=True,decision="revise"),{}
        assert payload["deterministic_spread_comparison"]["tie_break_required"] is False
        return versioned_output(4.5,224.5,["basketball.fact","market.quote"],
                                review=True,decision="revise"),{}
    result=run_two_stage(basketball,{"evidence":[market_evidence]},tmp_path/"tiebreak-final",client=client,
        market_collector=lambda _game:{"status":"available","public_market_reference":quote,
                                       "evidence":[market_evidence]},require_analysis_core=True)
    assert len(requested_tiebreak)==1
    assert requested_tiebreak[0]["reason"]=="final_model_home_margin_equals_market_implied_home_margin"
    assert result["market_provenance"]["final_spread_tiebreak"]["initially_required"] is False
    assert result["market_provenance"]["final_spread_tiebreak"]["attempted"] is True
    assert result["spread_comparison"]["independent"]["spread_edge"]==-1.0
    assert result["spread_comparison"]["final"]["spread_edge"]==-0.5
    assert result["spread_comparison"]["final"]["ats_lean"]=="away"


def test_unknown_market_publication_time_rejects_same_information_claim(tmp_path):
    basketball = {"game": {"game_type": "regular_season", "registered_game": True},
        "evidence": [{"id": "basketball.fact", "section": "team_form", "value": {"note": "home court context"}},
                     {"id": "model.pure", "section": "pure_prediction", "value": {"note": "baseline"}}]}
    market = {"evidence": [{"id": "market.quote", "section": "market", "value": {"home_spread": -4.5}}]}

    def client(_messages, schema):
        if schema is VersionedIndependentForecast:
            return versioned_output(-2.5, 224.5, ["basketball.fact", "model.pure"]), {}
        review = versioned_output(3.0, 223.5, ["basketball.fact", "market.quote"],
                                  review=True, decision="revise")
        review["rationale"] = "The market had the same public information about the latest injury before pricing the home favorite."
        return review, {}

    result = run_two_stage(basketball, market, tmp_path / "run", client=client,
        market_collector=lambda _game: {"status": "available", "evidence": market["evidence"],
            "public_market_reference": {"published_at": None,
                "observed_at": "2026-10-06T18:28:50+00:00"}}, require_analysis_core=True)
    assert result["market_provenance"]["market_timing_status"] == "observed_only"
    assert result["market_provenance"]["information_overlap"] == "unknown"
    assert result["review"]["decision"] == "unavailable"
    assert result["final_prediction"]["winner_side"] == "away"
    failure = json.loads((tmp_path / "run" / "market_review_failure.json").read_text())
    assert failure["error"] == "market_information_overlap_unverified"


def test_invalid_market_identity_is_skipped_without_blocking_independent_forecast(tmp_path):
    basketball = {"game": {"game_id": "espn:fixture", "home": {"id": 1, "name": "Warriors"},
        "away": {"id": 2, "name": "Lakers"}, "game_type": "regular_season", "registered_game": True},
        "evidence": [{"id": "basketball.fact", "section": "team_form", "value": {"note": "verified context"}},
                     {"id": "model.pure", "section": "pure_prediction", "value": {"note": "baseline"}}]}
    market_evidence = {"id": "bad-market", "section": "market", "value": {"market_reference": {"home_spread": -4.5}}}
    calls = []

    def client(_messages, schema):
        calls.append(schema)
        assert schema is VersionedIndependentForecast
        return versioned_output(-2.5, 224.5, ["basketball.fact", "model.pure"]), {}

    def collector(_game):
        return {"status": "available", "game_identity": {"game_id": "espn:other"},
            "public_market_reference": {"status": "available",
                "game_identity": {"game_id": "espn:other", "home_team_id": 1, "away_team_id": 2},
                "home_spread": -4.5, "away_spread": 4.5, "expected_home_margin": 4.5},
            "evidence": [market_evidence]}

    result = run_two_stage(basketball, {"evidence": [market_evidence]}, tmp_path / "invalid-market",
        client=client, market_collector=collector, require_analysis_core=True)
    assert calls == [VersionedIndependentForecast]
    assert result["final_prediction"] == result["independent"]["prediction"]
    assert result["spread_comparison"]["market"]["reason"] == "spread_market_game_identity_mismatch"
    assert result["spread_comparison"]["final"]["ats_lean"] == "unavailable"
    failure = json.loads((tmp_path / "invalid-market" / "market_review_failure.json").read_text())
    assert failure["invalid_quote_used_for_review"] is False


def test_market_keep_records_no_fabricated_prediction_revision(tmp_path):
    basketball = {"game": {"game_type": "regular_season", "registered_game": True},
        "evidence": [{"id": "basketball.fact", "section": "team_form", "value": {"note": "basketball view"}}]}
    market = {"evidence": [{"id": "market.quote", "section": "market", "value": {"home_spread": -2.5}}]}

    def client(_messages, schema):
        if schema is VersionedIndependentForecast:
            return versioned_output(-2.5, 224.5, ["basketball.fact"]), {}
        review = versioned_output(-2.5, 224.5, ["basketball.fact", "market.quote"], review=True, decision="keep")
        review["factors"] = [
            {"classification": "source_fact", "evidence_ids": ["market.quote"], "effect": "away",
             "summary": "The observed price leans to the away team."},
            {"classification": "inference", "evidence_ids": ["basketball.fact"], "effect": "away",
             "summary": "The independent basketball view remains coherent."},
        ]
        review["revision_reason"] = {"market_signal_factor_indices": [0], "basketball_reweighting_factor_indices": []}
        return review, {}

    result = run_two_stage(basketball, market, tmp_path / "keep", client=client,
        market_collector=lambda _game: {"status": "available", "evidence": market["evidence"],
            "public_market_reference": {"published_at": None, "captured_at": "2026-10-06T18:30:00Z"}},
        require_analysis_core=True)
    assert result["independent"]["prediction"] == result["final_prediction"]
    assert result["market_provenance"]["revision_delta"]["winner_changed"] is False
    assert result["market_provenance"]["revision_delta"]["changed_axes"] == []


def test_unknown_market_timing_guard_scans_structured_analysis_core():
    review = VersionedMarketReviewForecast.model_validate(versioned_output(
        3.0, 223.5, ["basketball.fact", "market.quote"], review=True, decision="revise"))
    review.analysis_core.expected_game_script = (
        "The market had the same public information about the latest lineup before pricing this matchup."
    )
    with pytest.raises(ValueError, match="market_information_overlap_unverified"):
        validate_market_timing_claims(review, "observed_only")


def test_projection_removes_nested_and_text_prices_but_keeps_pure_and_official_roster():
    original = packet(); before = copy.deepcopy(original)
    view, market, removed = prepare_pilot(original)
    text = json.dumps(view, ensure_ascii=False)
    assert "7.5" not in text and "228.5" not in text and "99991" not in text and "99992" not in text
    assert "favorite" not in text and "盘口" not in text and "Out" in text
    assert "4.2" in text and "224.2" in text and "official_team_roster" in text
    assert "7.5" in json.dumps(market) and "99992" in json.dumps(market)
    assert original == before and removed


def test_production_nested_official_prediction_is_not_a_blind_baseline():
    value = {"pure_prediction": {"expected_margin": 4.2}, "official_numeric_prediction": {"expected_margin": 77777},
        "residual_prediction": {"expected_margin": 88888}, "market_benchmark": {"market_total": 228.5},
        "analysis_request": "The line is 7.5", "objective_evidence": [
        {"id": "pure-id", "section": "prediction", "value": {"prediction": {"expected_margin": 77777}, "market_usage": "hidden"}},
        {"id": "injury-id", "section": "availability", "value": {"status": "Out"}},
        {"id": "odds-id", "section": "web_context", "value": {"category": "market", "total": 228.5}},
        {"id": "news-id", "section": "web_context", "value": {"category": "game_story", "snippet": "bookmaker lists 7.5", "headline": "Upcoming game"}},
    ]}
    view, market, _ = prepare_analyst(value)
    assert all(x not in json.dumps(view) for x in ["77777", "88888", "228.5", "7.5", "odds-id"])
    assert "4.2" in json.dumps(view) and "Out" in json.dumps(view)
    assert "77777" in json.dumps(market)


def test_fact_context_is_in_the_final_independent_request_not_market_context(tmp_path):
    game_id = "espn:fact-context-fixture"
    input_cutoff = "2026-10-10T12:00:00+00:00"
    feature_cutoff = "2026-10-10T11:00:00+00:00"
    fact_context = {
        "schema_version": "pregame-fact-context-v1", "game_id": game_id,
        "feature_cutoff": feature_cutoff,
        "team_form": {"home": {"requested_games": 15, "observed_games": 8,
            "season_counts": {"2025": 8}, "competition_counts": {"regular_season": 8},
            "metrics": {"net_rating_15": {"value": 2.5, "valid_games": 8, "missing_games": 0,
                "unit": "points_per_100_possessions", "aggregation": "mean_of_per_game_values",
                "method": "v1_per_game_mean_shift_1_rolling_15"}}}},
        "schedule": {"home": {"status": "unavailable", "reason": "calendar_unknown",
            "basis": None, "back_to_back": None, "source_refs": []}},
    }
    context = {
        "game": {"game_id": game_id, "scheduled_tipoff": "2026-10-10T20:00:00+00:00",
                 "game_type": "regular_season", "home": {"id": 1, "name": "Home"},
                 "away": {"id": 2, "name": "Away"}},
        "pure_prediction": {"winner_side": "home", "win_probability": .62,
                             "expected_margin": 3.5, "expected_total": 224.5},
        "official_numeric_prediction": {"expected_margin": 3.5},
        "market_benchmark": {"home_spread": -9.5, "total": 228.5},
        "objective_evidence": [
            {"id": "model.pure", "section": "prediction", "game_id": game_id,
             "cutoff": input_cutoff, "value": {"expected_margin": 3.5}},
            {"id": "fact-context", "section": "fact_context", "game_id": game_id,
             "cutoff": input_cutoff, "value": fact_context},
            {"id": "market.quote", "section": "market", "game_id": game_id,
             "cutoff": input_cutoff, "value": {"home_spread": -9.5, "total": 228.5}},
        ],
        "missing_data": [],
    }
    basketball, market, _ = prepare_analyst(context)
    assert any(item["id"] == "fact-context" for item in basketball["evidence"])
    assert "-9.5" not in json.dumps(basketball)
    calls = []

    def client(messages, schema):
        payload = json.loads(messages[1]["content"])
        if schema is IndependentForecast:
            calls.append(payload)
            projected = next(item for item in payload["evidence"] if item["id"] == "fact-context")
            assert projected["game_id"] == game_id and projected["cutoff"] == input_cutoff
            assert projected["value"]["feature_cutoff"] == feature_cutoff
            assert projected["value"]["team_form"]["home"]["observed_games"] == 8
            assert "market_context" not in payload and "-9.5" not in json.dumps(payload)
            return output(3.5, 224.5, refs=["model.pure", "fact-context"]), {}
        assert schema is MarketReviewForecast
        assert payload.get("market_context")
        return output(3.5, 224.5, refs=["model.pure", "market.quote"], review=True), {}

    run_two_stage(basketball, market, tmp_path / "forecast", client=client,
                  skill_loader=lambda: "FORECAST")
    receipt = json.loads((tmp_path / "forecast" / "independent_request.json").read_text(encoding="utf-8"))
    final_input = json.loads(receipt["messages"][1]["content"])
    assert calls and final_input == calls[0]
    assert any(item["id"] == "fact-context" for item in final_input["evidence"])


def test_fact_context_reaches_final_independent_request_with_identity_and_cutoff_but_no_market(tmp_path):
    game_id = "espn:fact-context-fixture"
    cutoff = "2026-10-10T12:00:00+00:00"
    fact_context = {
        "schema_version": "pregame-fact-context-v1", "game_id": game_id,
        "feature_cutoff": "2026-10-10T11:00:00+00:00",
        "team_form": {"home": {"requested_games": 15, "observed_games": 8,
            "season_counts": {"2025": 8}, "competition_counts": {"regular_season": 8},
            "metrics": {"net_rating_15": {"value": 2.5, "valid_games": 8, "missing_games": 0,
                "unit": "points_per_100_possessions", "aggregation": "mean_of_per_game_values",
                "method": "v1_per_game_mean_shift_1_rolling_15"}}}},
        "schedule": {"home": {"status": "unavailable", "reason": "calendar_unknown",
            "basis": None, "back_to_back": None, "source_refs": []}},
    }
    context = {
        "game": {"game_id": game_id, "scheduled_tipoff": "2026-10-10T20:00:00+00:00",
                 "game_type": "regular_season", "home": {"id": 1, "name": "Home"},
                 "away": {"id": 2, "name": "Away"}},
        "game_identity": {"game_id": game_id}, "cutoff": cutoff,
        "pure_prediction": {"winner_side": "home", "win_probability": .62,
                             "expected_margin": 3.5, "expected_total": 224.5},
        "official_numeric_prediction": {"expected_margin": 3.5},
        "market_benchmark": {"home_spread": -9.5, "total": 228.5},
        "objective_evidence": [
            {"id": "model.pure", "section": "prediction", "game_id": game_id, "cutoff": cutoff,
             "value": {"expected_margin": 3.5}},
            {"id": "fact-context", "section": "fact_context", "game_id": game_id, "cutoff": cutoff,
             "value": fact_context},
            {"id": "market.quote", "section": "market", "game_id": game_id, "cutoff": cutoff,
             "value": {"home_spread": -9.5, "total": 228.5}},
        ],
        "missing_data": [],
    }
    basketball, market, _ = prepare_analyst(context)
    assert any(item["id"] == "fact-context" for item in basketball["evidence"])
    assert "-9.5" not in json.dumps(basketball)
    calls = []

    def client(messages, schema):
        payload = json.loads(messages[1]["content"])
        if schema is IndependentForecast:
            calls.append(payload)
            context_fact = next(item for item in payload["evidence"] if item["id"] == "fact-context")
            assert context_fact["game_id"] == game_id and context_fact["cutoff"] == cutoff
            assert context_fact["value"]["feature_cutoff"] == fact_context["feature_cutoff"]
            assert context_fact["value"]["team_form"]["home"]["observed_games"] == 8
            assert "market_context" not in payload
            assert "-9.5" not in json.dumps(payload)
            return output(3.5, 224.5, refs=["model.pure", "fact-context"]), {}
        assert schema is MarketReviewForecast
        assert payload.get("market_context")
        return output(3.5, 224.5, refs=["model.pure", "market.quote"], review=True), {}

    run_two_stage(basketball, market, tmp_path / "forecast", client=client,
                  skill_loader=lambda: "FORECAST")
    independent = json.loads((tmp_path / "forecast" / "independent_request.json").read_text(encoding="utf-8"))
    final_input = json.loads(independent["messages"][1]["content"])
    assert calls and final_input == calls[0]
    assert any(item["id"] == "fact-context" for item in final_input["evidence"])


@pytest.mark.parametrize("adjust", [False, True])
def test_stage_one_saved_before_market_reveal_and_no_forced_distance(tmp_path, adjust):
    view, market, removed = prepare_pilot(packet())
    root = tmp_path / "run"; seen = []
    def client(messages, schema):
        seen.append(json.loads(messages[1]["content"]))
        assert "editorial_skill" not in messages[0]["content"]
        if schema is IndependentForecast:
            assert not (root / "independent.json").exists()
            assert "market_context" not in seen[-1]
            return output(), {}
        assert (root / "independent.json").is_file()
        assert seen[-1]["independent"]["prediction"]["expected_home_margin"] == 4.5
        # Exactly matching the quote is legitimate when the review has evidence.
        return output(7.5 if adjust else 4.5, refs=["model.pure", "market.reference"],
                      review=True, decision="revise" if adjust else "keep"), {}
    result = run_two_stage(view, market, root, client=client, skill_loader=lambda: "FORECAST", removed_paths=removed)
    assert result['calls'] == 2 and result['final_prediction']['expected_home_margin'] == (7.5 if adjust else 4.5)
    assert len(seen) == 2 and 'independent' in seen[1] and 'market_context' in seen[1]
    assert json.loads((root/'independent.json').read_text())['prediction_and_basis']['prediction']['expected_home_margin'] == 4.5


def test_missing_market_preserves_first_forecast_with_one_call(tmp_path):
    p=packet(); p['market_reference']={'status':'unavailable'}
    view, market, _=prepare_pilot(p); calls=[]
    def client(messages, schema):
        calls.append(schema); return output(), {}
    result=run_two_stage(view,market,tmp_path/'run',client=client,skill_loader=lambda:'FORECAST')
    assert calls==[IndependentForecast] and result['review']['decision']=='unavailable'
    assert result['final_prediction']==result['independent']['prediction']


@pytest.mark.parametrize("mode", ["correct", "still_invalid", "changes_numbers"])
def test_one_protocol_revision_preserves_numbers_and_all_citation_gates(tmp_path, mode):
    view, _, _ = prepare_pilot(packet())
    calls = []
    def client(messages, schema):
        calls.append(messages)
        if len(calls) == 1:
            return output(refs=["mistyped-id"]), {}
        assert "market_context" not in messages[1]["content"]
        assert json.loads(messages[-1]["content"])["protocol_correction"]["error"] == "unknown_forecast_evidence"
        return output(home_margin=5.5 if mode=="changes_numbers" else 4.5,
            refs=["mistyped-id"] if mode=="still_invalid" else ["model.pure"]), {}
    if mode == "correct":
        result = run_two_stage(view, {"evidence":[]}, tmp_path/"run", client=client, skill_loader=lambda:"FORECAST")
        assert result["final_prediction"]["expected_home_margin"] == 4.5
        assert (tmp_path/"run"/"independent_protocol_revision_response.json").is_file()
    else:
        with pytest.raises(ValueError, match="unknown_forecast_evidence|protocol_revision_changed_prediction"):
            run_two_stage(view, {"evidence":[]}, tmp_path/"run", client=client, skill_loader=lambda:"FORECAST")
        assert not (tmp_path/"run"/"independent.json").exists()
    assert len(calls) == 2


@pytest.mark.parametrize('failure', ['invalid_independent', 'review_failure', 'false_keep', 'unknown_evidence', 'no_basketball_basis'])
def test_failed_stages_never_publish_or_fall_back_to_odds(tmp_path, failure):
    view, market, _ = prepare_pilot(packet()); seen=[]; root=tmp_path/'run'
    def client(messages,schema):
        seen.append(schema)
        if schema is IndependentForecast:
            v=output()
            if failure=='invalid_independent': v['prediction']['winner_side']='away'
            return v,{}
        if failure=='review_failure': raise RuntimeError('fixture transport failure')
        v=output(7.5,refs=['model.pure','market.reference'],review=True,decision='revise')
        if failure=='false_keep': v['decision']='keep'
        if failure=='unknown_evidence': v['factors'][0]['evidence_ids']=['unknown']
        if failure=='no_basketball_basis': v['factors'][0]['evidence_ids']=['market.reference']
        return v,{}
    with pytest.raises((ValueError,RuntimeError)):
        run_two_stage(view,market,root,client=client,skill_loader=lambda:'FORECAST')
    assert not (root/'final.json').exists() and (root/'failure.json').exists()
    assert len(seen)==(1 if failure=='invalid_independent' else 2)
    assert (root/'independent.json').exists()==(failure!='invalid_independent')


def test_independent_output_cannot_claim_unseen_market(tmp_path):
    view, market, _=prepare_pilot(packet())
    v=output();v['factors'][0]['summary']='The market favors home.'
    with pytest.raises(ValueError,match='invents_market'):
        run_two_stage(view,market,tmp_path/'run',client=lambda *a:(v,{}),skill_loader=lambda:'FORECAST')


@pytest.mark.parametrize('statement',[
    'No market information was available at this stage, so no line-based cross-check was possible.',
    'Market data is unavailable at this stage.',
    '本阶段未提供盘口信息，无法进行价格校验。',
    '无盘口信息，无法用市场价格校验本预测。',
])
def test_independent_can_disclose_missing_market_information(statement):
    from .forecasting_workflow import validate_independent_market_boundary
    v=output();v['uncertainties'].append(statement)
    validate_independent_market_boundary(IndependentForecast.model_validate(v))


@pytest.mark.parametrize('statement',[
    'No market information was available, but the home team is -7.5.',
    'No market data; home should cover.',
    'No market data. I follow the market.',
    '本阶段没有盘口信息，但主队应让7.5分。',
])
def test_absence_prefix_does_not_allow_unseen_price_or_direction(statement):
    from .forecasting_workflow import validate_independent_market_boundary
    v=output();v['uncertainties'].append(statement)
    with pytest.raises(ValueError,match='invents_market'):
        validate_independent_market_boundary(IndependentForecast.model_validate(v))


@pytest.mark.parametrize('statement',[
    'This partially offsets Denver absences and caps how far OKC should be favored.',
    'My projected spread is 4.5 points in favor of home, based on team evidence.',
])
def test_own_basketball_forecast_language_is_not_an_observed_market_claim(statement):
    from .forecasting_workflow import validate_independent_market_boundary
    value=output();value['factors'][0]['summary']=statement
    validate_independent_market_boundary(IndependentForecast.model_validate(value))


def test_review_retains_long_explanation_without_editing_prediction_or_request(tmp_path):
    view,market,_=prepare_pilot(packet()); rationale='Evidence-based review. ' * 55
    def client(messages,schema):
        if schema is IndependentForecast:return output(),{}
        assert '"maxLength": 1000' in messages[0]['content']
        value=output(review=True);value['rationale']=rationale
        return value,{}
    result=run_two_stage(view,market,tmp_path/'run',client=client,skill_loader=lambda:'FORECAST')
    assert result['review']['rationale']==rationale
    assert result['final_prediction']==result['independent']['prediction']
    assert 'schema_repair_provenance' not in result
    receipt=json.loads((tmp_path/'run/market_review_validation.json').read_text())
    assert receipt['accepted_long_rationale'] and not receipt['response_content_edited']


def test_immutable_receipts_prevent_overwriting_a_previous_prediction(tmp_path):
    view, _, _=prepare_pilot(packet()); root=tmp_path/'run'
    run_two_stage(view,{'evidence':[]},root,client=lambda *a:(output(),{}),skill_loader=lambda:'FORECAST')
    with pytest.raises(FileExistsError):
        run_two_stage(view,{'evidence':[]},root,client=lambda *a:pytest.fail('second call'),skill_loader=lambda:'FORECAST')


def test_enriched_runner_native_schema_and_signed_away_conversion(tmp_path):
    p=packet(); calls=[]
    def request(messages,*args,**kwargs):
        view=json.loads(messages[1]['content']);calls.append(view)
        return json.dumps(output(-4.5,review='independent' in view))
    value=forecast_one(p,request,'fixture',skill_loader=lambda:'FORECAST',audit_dir=tmp_path/'run')
    assert value=={'winner':'Houston Rockets','win_probability':0.62,'expected_margin':-4.5,'expected_total':224.5}
    assert len(calls)==2


def test_five_game_runner_saves_per_game_stages_and_only_final_predictions(tmp_path):
    packets=[packet(n) for n in (1,21,41,61,82)]; calls=[]
    def request(messages,*args,**kwargs):
        view=json.loads(messages[1]['content']);calls.append(view)
        return json.dumps(output(review='independent' in view))
    path=tmp_path/'predictions.json'
    result=run_forecast_batch(packets,path,request,'fixture',skill_loader=lambda:'FORECAST',
                              checkpoint_dir=tmp_path/'checkpoints')
    assert len(calls)==10 and len(result['predictions'])==5
    assert all(set(p['prediction'])=={'winner','win_probability','expected_margin','expected_total'} for p in result['predictions'])
    assert len(list((tmp_path/'predictions.stages').glob('*/independent.json')))==5
    assert all('independent' not in calls[i] for i in range(0,10,2))


def test_target_answers_and_known_h2h_conflict_are_rejected_before_call():
    p=packet();p['actual_total']=123
    with pytest.raises(ValueError,match='target_answer'):prepare_pilot(p)
    p=packet();p['pregame_news']={'summary':'San Antonio entered 0-3'}
    p['h2h']={'recent_meetings':[{'prior_game_result_for_okc':'L'}]*3}
    with pytest.raises(ValueError,match='h2h_narrative_conflict'):prepare_pilot(p)


def test_winner_probability_and_margin_semantics():
    with pytest.raises(ValueError): ForecastNumbers(winner_side='home',win_probability=True,expected_home_margin=2,expected_total=222)
    with pytest.raises(ValueError): ForecastNumbers(winner_side='away',win_probability=.6,expected_home_margin=2,expected_total=222)


def test_invalid_json_is_saved_without_repair_or_retry(tmp_path):
    view, market, _=prepare_pilot(packet()); root=tmp_path/'run'
    with pytest.raises(ValueError,match='forecast_stage_json_invalid'):
        run_two_stage(view,market,root,client=lambda *a:('{broken',{}),skill_loader=lambda:'FORECAST')
    assert json.loads((root/'independent_response.json').read_text())['raw']=='{broken'
    assert not (root/'market_review_request.json').exists()


def test_v2_invalid_json_still_fails_closed_before_market_review(tmp_path):
    basketball = {"game": {"game_id": "espn:v2-invalid-json", "game_type": "regular_season"},
        "evidence": [{"id": "model.pure", "section": "pure_prediction", "value": {"status": "available"}}]}
    root = tmp_path / "v2-invalid"
    raw = '{"prediction":{"winner_side":"home"'
    with pytest.raises(ValueError, match="forecast_stage_json_invalid"):
        run_two_stage(basketball, {"evidence": []}, root,
            client=lambda *_: (raw, {}), skill_loader=lambda: "FORECAST", require_analysis_core=True)
    response = json.loads((root / "independent_response.json").read_text(encoding="utf-8"))
    assert response["raw"] == raw
    assert not (root / "independent.json").exists()
    assert not (root / "market_review_request.json").exists()
    assert not (root / "independent_schema_repair_request.json").exists()
    repair = json.loads((root / "schema_repair_provenance.json").read_text(encoding="utf-8"))
    assert repair["schema_repair_used"] is False
    assert repair["status"] == "not_attempted_invalid_json"


def test_failed_independent_write_prevents_market_request(tmp_path,monkeypatch):
    from . import forecasting_workflow as mod
    original_save=mod.save;calls=[]
    def save(path,value):
        if Path(path).name=='independent.json': raise OSError('fixture disk failure')
        return original_save(path,value)
    monkeypatch.setattr(mod,'save',save)
    view,market,_=prepare_pilot(packet())
    def client(messages,schema):
        calls.append(schema.__name__); return output(),{}
    with pytest.raises(OSError):
        run_two_stage(view,market,tmp_path/'run',client=client,skill_loader=lambda:'FORECAST')
    assert calls==['IndependentForecast']


def test_saved_game_product_locks_numbers_and_separates_skills(tmp_path):
    from .analyst import EvidenceBoundAnalyst,build_analyst_context
    from .editorial_test_helpers import protocol_reviewer
    from .test_analyst import LAYERS,EVIDENCE,judgment_payload
    context=build_analyst_context(LAYERS,EVIDENCE,missing_data=[],analysis_request='第25秒赛前',
        game={'game_id':'fixture','home':{'name':'Home'},'away':{'name':'Away'}})
    context_before=context.model_dump();events=[]
    def client(messages,schema):
        name=schema.__name__; events.append(name)
        data=json.loads(messages[1]['content'])
        if name in ('VersionedIndependentForecast','VersionedMarketReviewForecast'):
            assert '<forecasting_skill ' in messages[0]['content']
            assert '<editorial_skill ' not in messages[0]['content']
            if name=='VersionedIndependentForecast':
                assert 'market_context' not in data and data['game']['home']['name']=='Home'
                assert 'market_expected_margin' not in messages[1]['content']
                return versioned_output(3.5,228.5,refs=['evidence-prediction']),{}
            assert list(tmp_path.glob('*/forecast/independent.json'))
            value=versioned_output(3.5,228.5,refs=['evidence-prediction','market.market_benchmark'],review=True)
            value['factors']=[
                {'classification':'source_fact','evidence_ids':['market.market_benchmark'],'effect':'home',
                 'summary':'The observed market price favors the home side.'},
                {'classification':'inference','evidence_ids':['evidence-prediction'],'effect':'home',
                 'summary':'Existing home-court evidence supports keeping the independent direction.'},
            ]
            value['revision_reason']={'market_signal_factor_indices':[0],
                'basketball_reweighting_factor_indices':[1]}
            return value,{}
        assert name=='EditorialDraft'
        assert 'final_prediction' not in schema.model_fields
        assert '<forecasting_skill ' not in messages[0]['content'] and '<editorial_skill ' in messages[0]['content']
        assert list(tmp_path.glob('*/forecast/final.json'))
        assert 'objective_evidence' not in data and data['protocol']=='editorial-decision-v2'
        assert data['locked_prediction']['expected_margin']==3.5
        assert data['locked_prediction']['expected_total']==228.5
        value=judgment_payload()
        draft={'title':value['pregame_story']['title'],'lead':value['pregame_story']['statement'],
            'article':value['analysis_article'],'confidence':value['confidence'],
            'watch_themes':[{k:claim[k] for k in ('title','statement')} for claim in value['watch_themes']]}
        # Compact Writer output cannot supply a replacement numeric forecast.
        with pytest.raises(ValueError):
            schema.model_validate({**draft,'final_prediction':{'expected_margin':39.5,'expected_total':299.5}})
        return draft,{}
    def reviewer(messages,schema):
        events.append(schema.__name__)
        assert schema.__name__=='QualityReview'
        assert '<forecasting_skill ' not in messages[0]['content'] and '<editorial_skill ' not in messages[0]['content']
        assert list(tmp_path.glob('*/forecast/final.json'))
        assert list(tmp_path.glob('*/editorial/writer0_response.json'))
        return protocol_reviewer(messages,schema)
    result,trace=EvidenceBoundAnalyst(root=tmp_path,client=client,reviewer_client=reviewer,
                                     include_forecasting_skill=True,max_calls=6).judge(context)
    assert events==['VersionedIndependentForecast','VersionedMarketReviewForecast','EditorialDraft','QualityReview']
    assert result.final_prediction.expected_margin==3.5 and result.final_prediction.expected_total==228.5
    assert result.final_prediction.win_probability==.62 and trace['forecast_audit_path']
    assert trace['spread_comparison']['schema_version']=='courtvision-spread-comparison-v1'
    assert trace['publication_receipt']['status']=='eligible'
    assert trace['publication_receipt']['quality_review']['status']=='warning'
    assert trace['schema_repair_provenance']['schema_repair_used'] is False
    assert context.model_dump()==context_before


def test_live_forecast_failure_stops_editorial_generation(tmp_path):
    from .analyst import EvidenceBoundAnalyst,build_analyst_context
    from .test_analyst import LAYERS,EVIDENCE
    calls=[]
    def client(messages,schema):
        calls.append(schema.__name__)
        if schema.__name__=='VersionedIndependentForecast':return versioned_output(refs=['evidence-prediction']),{}
        raise RuntimeError('fixture review failure')
    with pytest.raises(RuntimeError):
        EvidenceBoundAnalyst(root=tmp_path,client=client,include_forecasting_skill=True,max_calls=6).judge(
            build_analyst_context(LAYERS,EVIDENCE,missing_data=[],analysis_request='第25秒赛前'))
    assert calls==['VersionedIndependentForecast','VersionedMarketReviewForecast']
    assert not list(tmp_path.glob('*/response.json'))


def test_live_budget_reserves_one_market_tiebreak_repair_before_any_call(tmp_path):
    from .analyst import EvidenceBoundAnalyst,build_analyst_context
    from .test_analyst import LAYERS,EVIDENCE
    with pytest.raises(ValueError,match='budget_cannot_cover'):
        EvidenceBoundAnalyst(root=tmp_path,max_calls=4,include_forecasting_skill=True,
            client=lambda *a:pytest.fail('budget exceeded')).judge(
                build_analyst_context(LAYERS,EVIDENCE,missing_data=[]))


def test_unavailable_market_with_pure_official_fallback_does_not_open_review():
    value={'pure_prediction':{'expected_margin':4.5}, 'official_numeric_prediction':{'expected_margin':4.5},
        'market_benchmark':None, 'objective_evidence':[]}
    _,market,_=prepare_analyst(value)
    assert market['evidence']==[]


def test_batch_checks_all_packets_before_any_model_request(tmp_path):
    packets=[packet(n) for n in (1,21,41,61,82)]
    packets[-1]['actual_total']=333;packets[-1]['packet_sha256']=packet_content_sha256(packets[-1])
    with pytest.raises(ValueError,match='target_answer'):
        run_forecast_batch(packets,tmp_path/'predictions.json',lambda *a,**k:pytest.fail('called before validation'),'fixture')


def test_transport_receipt_preserves_provider_metadata_without_a_database():
    import io
    from v2.agent.generate_historical_pregame_analysis import request_deepseek
    seen=[]
    def opener(request,timeout):
        seen.append(json.loads(request.data))
        return io.BytesIO(json.dumps({'id':'fixture-response','model':'fixture-model','created':123,
            'choices':[{'message':{'content':'{"x":1}'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}}).encode())
    result=request_deepseek([{'role':'user','content':'fixture'}],'fixture-key','fixture-model',2400,opener=opener)
    assert result.response_metadata=={'id':'fixture-response','model':'fixture-model','created':123}
    assert result.usage['total_tokens']==5 and result.content=='{"x":1}'
    assert seen[0]['messages']==[{'role':'user','content':'fixture'}]
    assert seen[0]['thinking']=={'type':'disabled'}


def test_model_with_declared_market_dependency_cannot_hide_under_pure_label():
    p=packet();p['quantitative_baselines']['pure']={'uses_market':True,'expected_margin':37.123}
    view,_,_=prepare_pilot(p)
    assert '37.123' not in json.dumps(view)
    assert next(x for x in view['evidence'] if x['id']=='model.pure')['value']['status']=='unavailable'


def test_moneyline_only_market_is_reviewable():
    p=packet();p['market_reference']={'moneyline':{'status':'available','home':-150,'away':130}}
    view,market,_=prepare_pilot(p)
    assert market['evidence'] and '-150' not in json.dumps(view)


def test_ineligible_quote_does_not_activate_review_via_residual_models():
    p=packet();p['market_reference']['usable_for_prediction']=False
    _,market,_=prepare_pilot(p)
    assert market['evidence']==[]


@pytest.mark.parametrize('decision',['keep','revise'])
def test_keep_and_revise_have_the_same_evidence_requirement(tmp_path,decision):
    view,market,_=prepare_pilot(packet())
    def client(messages,schema):
        if schema is IndependentForecast:return output(),{}
        assert '调整不要求出现新的伤病' in messages[0]['content']
        assert '不预设保持' in messages[0]['content']
        return output(4.5 if decision=='keep' else 7.5,refs=['model.pure'],review=True,decision=decision),{}
    with pytest.raises(ValueError,match='review_requires_basketball_and_market_evidence'):
        run_two_stage(view,market,tmp_path/'run',client=client,skill_loader=lambda:'FORECAST')


def test_legacy_export_cannot_relabel_official_as_pure(tmp_path):
    from v2.frontend.export_data import normalize_game
    from v2.frontend.analysis_service import _independent_prediction
    game={'game_id':'fixture','home_team_id':1610612760,'away_team_id':1610612745,
          'scheduled_tipoff':'2026-10-20T23:00:00Z'}
    numbers={'win_probability':.6,'expected_margin':8.5,'expected_total':230.5}
    report={'game':game,'prediction':numbers,'official_numeric_prediction':numbers,
        'market_benchmark':{'market_expected_margin':8.5},'prediction_method':'market_residual_v1'}
    path=tmp_path/'report.json';path.write_text('{}')
    normalized=normalize_game(game,path,report,outputs=tmp_path)
    assert normalized['relationship']['basketball']['status']=='unavailable'
    assert _independent_prediction(normalized)['status']=='unavailable'
    # A legacy cache with copied numbers and no provenance is also rejected.
    normalized['relationship'].pop('basketball_provenance')
    normalized['relationship']['basketball']=numbers
    assert _independent_prediction(normalized)['status']=='unavailable'
    # A separately saved Pure result remains available even with market layers.
    report['pure_prediction']={'win_probability':.57,'expected_margin':3.5,'expected_total':225.5}
    normalized=normalize_game(game,path,report,outputs=tmp_path)
    assert _independent_prediction(normalized)==report['pure_prediction']


def test_declared_pure_fallback_in_legacy_cache_remains_usable():
    from v2.frontend.analysis_service import _independent_prediction
    game={'relationship':{'basketball':{'expected_margin':3.5},'market':None,'residual':None},
          'technical':{'prediction_method':'pure_basketball_fallback'}}
    assert _independent_prediction(game)=={'expected_margin':3.5}
