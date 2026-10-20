"""Offline preseason focus and typed analyst-plan integration."""
import copy
import json
import pytest

from .analyst import build_analyst_context
from .editorial_pipeline import run_editorial, EditorialDraft
from .forecasting_workflow import prepare_analyst, run_two_stage, IndependentForecast
from .pregame_estimates import PregamePlan, validate_pregame_plan
from .research_evidence import ResearchEvidencePipeline
from .test_analyst import LAYERS
from .test_research_evidence import GAME, CUTOFF, BACKGROUND, source, claim
from .web_search import PregameWebSearch


def inputs():
    game = {"game_id": "espn:fixture-plan", "home": {"id": 1, "name": "Toronto Raptors", "name_zh": "猛龙"},
        "away": {"id": 2, "name": "Miami Heat", "name_zh": "热火"}, "game_type": "preseason",
        "scheduled_tipoff": "2026-10-05T23:00:00Z", "cutoff": "2026-10-03T16:00:00Z"}
    roster = {"teams": {side: {"team_id": team_id, "official_team_roster": {"status": "available",
        "players": [{"name": f"{prefix} Player {n}"} for n in range(1, 7)]}}
        for side, team_id, prefix in (("home", 1, "Home"), ("away", 2, "Away"))}}
    evidence = [{"id": "prediction", "section": "prediction", "value": {"pure_prediction": LAYERS["pure_prediction"]}},
                {"id": "roster", "section": "roster_context", "value": roster}]
    layers = {**copy.deepcopy(LAYERS), "market_benchmark": None, "residual_prediction": None,
              "divergence": None, "official_numeric_prediction": {}}
    return build_analyst_context(layers, evidence, missing_data=[], game=game)


def plan():
    return {"lineups": [{"side": side, "basis": "agent_expected",
        "players": [f"{prefix} Player {n}" for n in range(1, 6)], "evidence_ids": ["roster"],
        "rationale": "Based on the current roster and frontcourt roles."}
        for side, prefix in (("home", "Home"), ("away", "Away"))],
        "player_usage": [{"side": "away", "player_name": "Away Player 1", "basis": "agent_estimate",
            "minutes_min": 15, "minutes_max": 20, "evidence_ids": ["roster"],
            "rationale": "My preseason load estimate, not an announced restriction."}],
        "key_matchup": "Frontcourt roles and bench units may decide this preseason game.",
        "uncertainty": ["Coach has not announced minutes."]}


def forecast(p=None):
    return {"prediction": {"winner_side": "away", "win_probability": .56,
        "expected_home_margin": -3.5, "expected_total": 228.5},
        "factors": [{"classification": "inference", "evidence_ids": ["roster", "prediction"],
            "effect": "away", "summary": "I prefer the new frontcourt despite the model's home lean."}],
        "uncertainties": [], "pregame_plan": p or plan()}


def test_preseason_queries_focus_usage_and_camp_instead_of_regular_history(tmp_path):
    search = PregameWebSearch(root=tmp_path)
    pre = search.queries(GAME)
    regular = search.queries({**GAME, "game_type": "regular_season"})
    assert pre != regular
    text = " ".join(query for _, query in pre)
    assert "expected minutes" in text and "bench competition" in text and "offseason" in text
    assert "previous meeting" not in text and "last 15" not in text


def test_june_personnel_report_accepted_but_other_time_classes_unchanged():
    p = ResearchEvidencePipeline(GAME, CUTOFF)
    source(p, date="2026-06-23", body=BACKGROUND)
    assert p.validate_claim(claim())["accepted"]
    assert p.validate_claim(claim(kind="coach_change"))["reason"] == "previous_season_source_not_current_background"
    assert p.validate_claim(claim(kind="current_roster"))["reason"] == "stale_current_claim"
    prior = ResearchEvidencePipeline(GAME, CUTOFF)
    source(prior, date="2025-06-23", body=BACKGROUND)
    assert prior.validate_claim(claim())["reason"] == "previous_season_source_not_current_background"


def test_current_identity_required_and_confirmed_out_excluded():
    basketball, _, _ = prepare_analyst(inputs().model_dump())
    validate_pregame_plan(PregamePlan.model_validate(plan()), basketball)
    historical = copy.deepcopy(basketball)
    roster = next(e["value"] for e in historical["evidence"] if e["section"] == "roster_context")
    roster["teams"]["away"]["official_team_roster"] = {"status": "unavailable"}
    with pytest.raises(ValueError, match="current_identity_unverified"):
        validate_pregame_plan(PregamePlan.model_validate(plan()), historical)
    basketball["evidence"].append({"id": "out", "section": "availability", "value": {"records": [{
        "player_name": "Away Player 1", "team_id": 2, "game_id": "espn:fixture-plan", "status": "Out"}]}})
    with pytest.raises(ValueError, match="confirmed_out_in_projected_lineup"):
        validate_pregame_plan(PregamePlan.model_validate(plan()), basketball)


def test_current_espn_fallback_roster_can_prove_membership_without_becoming_official_starter():
    context = inputs()
    roster_context = next(item["value"] for item in context.objective_evidence if item["section"] == "roster_context")
    for side in ("home", "away"):
        team = roster_context["teams"][side]
        team["roster_basis"] = "espn_current_team_roster"
        team["current_team_roster"] = team.pop("official_team_roster")
        team["current_team_roster"]["roster_basis"] = "espn_current_team_roster"
        team["official_team_roster"] = None
    from .analyst import build_analyst_context
    context = build_analyst_context({**LAYERS, "official_numeric_prediction": {}},
        context.objective_evidence, missing_data=[], game=context.game)
    basketball, _, _ = prepare_analyst(context.model_dump())
    validate_pregame_plan(PregamePlan.model_validate(plan()), basketball)


def test_qualified_current_offseason_affiliation_can_support_agent_estimate():
    basketball, _, _ = prepare_analyst(inputs().model_dump())
    roster = next(e["value"] for e in basketball["evidence"] if e["section"] == "roster_context")
    roster["teams"]["away"]["official_team_roster"] = {"status": "unavailable"}
    p = plan()
    basketball["evidence"].append({"id": "acquisitions", "section": "web_context", "value": {
        "team_id": 2, "eligible_for_fact": True, "claim_type": "personnel_change",
        "assertion_kind": "confirmed_fact", "temporal_scope": "current_season_background",
        "source_role": "primary", "quote": "Miami Heat acquired " + ", ".join(p["lineups"][1]["players"])}})
    p["lineups"][1]["evidence_ids"].append("acquisitions")
    p["player_usage"][0]["evidence_ids"].append("acquisitions")
    validate_pregame_plan(PregamePlan.model_validate(p), basketball)
    basketball["evidence"][-1]["value"]["quote"] += " in exchange for Away Player 6."
    outgoing = copy.deepcopy(p)
    outgoing["lineups"][1]["players"][-1] = "Away Player 6"
    with pytest.raises(ValueError, match="current_identity_unverified"):
        validate_pregame_plan(PregamePlan.model_validate(outgoing), basketball)
    basketball["evidence"][-1]["value"]["eligible_for_fact"] = False
    with pytest.raises(ValueError, match="current_identity_unverified"):
        validate_pregame_plan(PregamePlan.model_validate(p), basketball)


@pytest.mark.parametrize("basis,kind", [("source_expected", "projected_starters"), ("confirmed", "confirmed_starter")])
def test_lineup_basis_requires_its_corresponding_source(basis, kind):
    basketball, _, _ = prepare_analyst(inputs().model_dump())
    p = plan()
    p["lineups"][1]["basis"] = basis
    p["lineups"][1]["evidence_ids"] = ["lineup"]
    basketball["evidence"].append({"id": "lineup", "section": "web_context", "value": {
        "team_id": 2, "eligible_for_fact": True, "claim_type": kind,
        "quote": "Miami Heat starters: " + ", ".join(p["lineups"][1]["players"])}})
    validate_pregame_plan(PregamePlan.model_validate(p), basketball)
    basketball["evidence"][-1]["value"]["claim_type"] = "role_background"
    with pytest.raises(ValueError, match="lineup_basis_source_unverified"):
        validate_pregame_plan(PregamePlan.model_validate(p), basketball)


def test_minutes_estimate_cannot_be_promoted_to_confirmed_restriction():
    basketball, _, _ = prepare_analyst(inputs().model_dump())
    p = plan()
    p["player_usage"][0]["basis"] = "source_confirmed"
    with pytest.raises(ValueError, match="confirmed_minutes_source_unverified"):
        validate_pregame_plan(PregamePlan.model_validate(p), basketball)
    basketball["evidence"].append({"id": "limit", "section": "web_context", "value": {
        "team_id": 2, "eligible_for_fact": True, "claim_type": "minutes_restriction",
        "quote": "Away Player 1 has a confirmed 15 to 20 minute restriction for this game."}})
    p["player_usage"][0]["evidence_ids"] = ["roster", "limit"]
    validate_pregame_plan(PregamePlan.model_validate(p), basketball)


def test_market_review_is_second_and_keeps_typed_plan_when_not_revised(tmp_path):
    basketball, _, _ = prepare_analyst(inputs().model_dump())
    market = {"evidence": [{"id": "quote", "section": "market", "value": {"home_spread": -1.5, "total": 232.5}}]}
    calls = []
    def client(messages, schema):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        value = forecast()
        if schema is not IndependentForecast:
            assert (tmp_path/"market"/"independent.json").is_file()
            assert payload["market_context"] == market
            value.pop("pregame_plan")
            value.update(decision="keep", market_assessment="challenges", rationale="Current lineup evidence supports the saved forecast.")
            value["factors"][0]["evidence_ids"].append("quote")
        return value, {}
    result = run_two_stage(basketball, market, tmp_path/"market", client=client)
    assert "232.5" not in json.dumps(calls[0]) and len(calls) == 2
    assert result["review"]["pregame_plan"] == plan()


def test_plan_reaches_forecasting_then_writer_without_market_or_numeric_mutation(tmp_path):
    context = inputs()
    original = copy.deepcopy(context.model_dump())
    basketball, market, _ = prepare_analyst(original)
    assert market == {"evidence": []}
    def forecasting(messages, schema):
        assert schema is IndependentForecast
        assert "current roster" in messages[0]["content"] or "当前名单" in messages[0]["content"]
        request = json.loads(messages[-1]["content"])
        assert "market_context" not in request
        assert any(e["section"] == "roster_context" for e in request["evidence"])
        return forecast(), {}
    locked = run_two_stage(basketball, market, tmp_path/"forecast", client=forecasting)
    seen = {}
    def writer(messages, schema):
        assert schema is EditorialDraft
        material = json.loads(messages[-1]["content"])
        seen.update(material)
        projected = next(f["value"]["pregame_plan"] for f in material["facts"]
                         if f["kind"] == "ANALYST_REASONING" and f["value"].get("pregame_plan"))
        assert projected["lineups"][1]["basis"] == "agent_expected"
        assert projected["lineups"][1]["players"] == plan()["lineups"][1]["players"]
        assert projected["player_usage"][0]["basis"] == "agent_estimate"
        assert projected["player_usage"][0]["minutes_max"] == 20
        return {"title": "前场组合", "lead": "我更看好客队新组合。",
            "article": "我预计核心球员只打15～20分钟，前场适配和替补竞争是这场的看点。",
            "watch_themes": [{"title": "新组合", "statement": "观察球权分配。"},
                             {"title": "替补", "statement": "观察第二阵容。"}], "confidence": "low"}, {}
    judgment, trace = run_editorial(context, locked, tmp_path/"writer", writer_client=writer, require_poem_title=False)
    assert seen["game_id"] == context.game["game_id"]
    assert judgment["final_prediction"]["expected_margin"] == 3.5
    assert judgment["final_prediction"]["winner_side"] == "away"
    assert judgment["final_prediction"]["expected_total"] == 228.5
    assert trace["publication_receipt"]["status"] == "eligible"
    assert context.model_dump() == original
