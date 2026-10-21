"""Offline writer, hard-fact repair, lock and non-blocking quality feedback tests."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from .analyst import AnalystContext
from .editorial_contract import (
    _writer_market_interpretation,
    validate_publication_hard_facts,
    verify_publication_receipt,
)
from .editorial_pipeline import EditorialDraft, EditorialRepair, QualityReview, normalize, run_editorial

ROOT = Path(__file__).parent / "outputs/heat_raptors_real_acceptance/20261002T183747_2b8de7d8cb"
SHORT_ARTICLE = "这场球我更看重前场搭配。新组合可能改变护框和篮板，但真正要看球员今晚承担什么角色。"


@pytest.fixture
def frozen():
    if not ROOT.is_dir():
        pytest.skip("optional frozen Attempt4 context missing")
    folder = next(ROOT.glob("analyst/*"))
    context = AnalystContext.model_validate(json.loads((folder / "context.json").read_text(encoding="utf-8")))
    forecast = json.loads(next(folder.glob("forecast/final.json")).read_text(encoding="utf-8"))
    return context, forecast


def writer(messages, schema):
    assert schema is EditorialDraft
    model_input = json.loads(messages[-1]["content"])
    assert "objective_evidence" not in model_input
    return {"title": "前场组合要回答的问题", "lead": "我更看好热火，但关键仍在新组合如何落地。",
            "article": SHORT_ARTICLE,
            "watch_themes": [{"title": "篮板与护框", "statement": "新前场如何共同保护禁区。"},
                             {"title": "出场角色", "statement": "球权如何分配仍值得观察。"}],
            "confidence": "low"}, {"fixture": True}


def test_versioned_writer_receives_decision_core_and_market_transition_without_pure(tmp_path):
    from .analyst import build_analyst_context
    from .ats import build_spread_comparison
    from .test_analyst import EVIDENCE, LAYERS
    layers = copy.deepcopy(LAYERS)
    layers["market_benchmark"] = {
        "market_implied_win_probability": .60,
        "market_expected_margin": 4.5,
        "market_total": 217.0,
    }
    context = build_analyst_context(layers, EVIDENCE, missing_data=[],
        analysis_request="生成第25秒赛前分析。", game={"game_id": "fixture-v2",
        "home": {"name": "Home", "id": 1}, "away": {"name": "Away", "id": 2}})
    evidence_id = next(iter(context.evidence_index))
    core = {"central_question": "Can the away guard attack the home switch?",
        "central_thesis": {"statement": "The away guard's pick-and-roll is the cleanest route to create a second-side edge.",
            "classification": "inference", "evidence_ids": [evidence_id], "confidence": "medium"},
        "matchup_status": "identified", "key_matchups": [{"subject": "Away guard pick-and-roll",
            "opponent_or_scheme": "Home switching", "why_it_matters": "It tests the first layer of the defense.",
            "expected_effect": "Help rotations can expose the weak side.", "classification": "inference",
            "evidence_ids": [evidence_id], "confidence": "medium", "uncertainty": None}],
        "expected_game_script": "Away probes the switch; Home responds with help and must recover to the weak side.",
        "flip_conditions": [{"condition": "The home center plays fewer minutes.",
            "judgment_effect": "The switching defense has less back-line protection.", "evidence_ids": [evidence_id]}]}
    numbers = {"winner_side": "home", "win_probability": .55, "expected_margin": 3.0,
               "expected_total": 220.0, "evidence_ids": [evidence_id]}
    forecast = {"schema_version": "two-stage-forecast-v2",
        "independent": {"prediction": {"winner_side": "away", "win_probability": .56,
            "expected_home_margin": -2.5, "expected_total": 224.5}},
        "final_prediction": {"winner_side": "home", "win_probability": .55,
            "expected_home_margin": 3.0, "expected_total": 220.0},
        "final_analysis_core": core,
        "revision_delta": {"winner_changed": True, "home_win_probability_delta": .11,
            "expected_home_margin_delta": 5.5, "expected_total_delta": -4.5,
            "changed_axes": ["winner", "win_probability", "expected_home_margin", "expected_total"]},
        "market_provenance": {"decision": "revise", "assessment": "challenges",
            "market_timing_status": "observed_only", "information_overlap": "unknown",
            "market_signal_factors": [{"summary": "Observed quote favors Home.", "evidence_ids": [evidence_id]}],
            "basketball_reweighting_factors": [{"summary": "Existing lineup evidence supports a narrower home lean.",
                "evidence_ids": [evidence_id]}]},
        "review": {"factors": [{"classification": "inference", "effect": "home",
            "summary": "Existing lineup evidence supports a narrower home lean.", "evidence_ids": [evidence_id]}],
            "pregame_plan": None}}
    market_collection = {"public_market_reference": {
        "status": "available", "home_spread": -4.5, "away_spread": 4.5, "total": 217.0,
        "expected_home_margin": 4.5, "sportsbook": "fixture book",
        "source_url": "https://example.invalid/fixture-market",
        "game_identity": {"game_id": "fixture-v2", "home_team_id": 1, "away_team_id": 2,
                          "home_team": "Home", "away_team": "Away"},
    }}
    forecast["spread_comparison"] = build_spread_comparison(
        context.game, market_collection,
        forecast["independent"]["prediction"], forecast["final_prediction"],
    )
    seen = {}
    def capture(messages, schema):
        payload = json.loads(messages[-1]["content"])
        seen["payload"] = payload
        seen["system"] = messages[0]["content"]
        text = json.dumps(payload, ensure_ascii=False)
        assert "Pure" not in text and "pure_baseline" not in text
        assert "DECISION_CORE" in text and "REVISION_PROVENANCE" in text
        revision = next(fact["value"] for fact in payload["facts"]
                        if fact["kind"] == "REVISION_PROVENANCE")
        interpretation = revision["market_interpretation"]
        assert interpretation["spread"] == {
            "status": "available", "margin_perspective": "home_minus_away",
            "model_home_margin": 3.0, "market_implied_home_margin": 4.5,
            "spread_edge": -1.5, "ats_lean": "away",
            "ats_lean_reference": {"side": "away", "team_id": 2, "team_name": "Away",
                                    "line": 4.5, "display": "Away +4.5"},
            "market_quote_id": interpretation["spread"]["market_quote_id"],
        }
        assert interpretation["total"] == {
            "status": "available", "model_total": 220.0, "market_total": 217.0,
            "total_edge": 3.0, "total_lean": "over",
            "market_quote_id": interpretation["total"]["market_quote_id"],
        }
        assert "胜负方向和让分方向是两件事" in messages[0]["content"]
        from .editorial_skill import load_editorial_skill
        assert load_editorial_skill() in messages[0]['content']
        return {"title": "A specific matchup", "lead": "The matchup turns on the switch.",
            "article": "The away guard will test the home switch. The home side's back line is the counter.",
            "watch_themes": [], "confidence": "medium", "player_analysis": []}, {"fixture": True}

    judgment, trace = run_editorial(context, forecast, tmp_path / "decision-v2", writer_client=capture, require_poem_title=False)
    fact_kinds = {fact["kind"] for fact in seen["payload"]["facts"]}
    assert "DECISION_CORE" in fact_kinds and "REVISION_PROVENANCE" in fact_kinds
    assert "supporting context beneath the Decision Core" in seen["payload"]["scope_legend"]["ANALYST_REASONING"]
    assert trace["editorial_projection"]["protocol"] == "editorial-decision-v2"
    assert judgment["central_thesis"]["statement"] == core["central_thesis"]["statement"]
    assert judgment["core_question"] == core["central_question"]
    assert judgment["key_matchup_variables"]
    payload = {"game_id": context.game["game_id"], "prediction": judgment["final_prediction"],
        "judgment": judgment, "analyst_context": context.model_dump(),
        "objective_evidence": context.objective_evidence,
        "editorial_material": trace["editorial_material"],
        "editorial_projection": trace["editorial_projection"],
        "publication_receipt": trace["publication_receipt"],
        "quality_review_input_sha256": trace["quality_review_input_sha256"]}
    verify_publication_receipt(payload)


@pytest.mark.parametrize(
    ("model_total", "market_total", "expected_edge", "expected_lean"),
    [(223.5, 220.5, 3.0, "over"), (217.5, 220.5, -3.0, "under"),
     (220.5, 220.5, 0.0, "aligned")],
)
def test_writer_market_interpretation_compares_total_without_thresholds(
    model_total, market_total, expected_edge, expected_lean,
):
    comparison = {
        "status": "available",
        "market": {"status": "available", "spread_status": "available",
                   "total": market_total, "market_quote_id": "frozen-quote"},
        "final": {"status": "available", "model_home_margin": 3.0,
                  "market_implied_home_margin": 4.5, "spread_edge": -1.5,
                  "ats_lean": "away", "ats_lean_reference": {"side": "away", "display": "Away +4.5"}},
    }
    interpretation = _writer_market_interpretation(
        {"spread_comparison": comparison}, {"expected_total": model_total},
    )
    assert interpretation["spread"]["model_home_margin"] == 3.0
    assert interpretation["spread"]["market_implied_home_margin"] == 4.5
    assert interpretation["spread"]["spread_edge"] == -1.5
    assert interpretation["spread"]["ats_lean"] == "away"
    assert interpretation["spread"]["ats_lean_reference"]["display"] == "Away +4.5"
    assert interpretation["total"]["model_total"] == model_total
    assert interpretation["total"]["market_total"] == market_total
    assert interpretation["total"]["total_edge"] == expected_edge
    assert interpretation["total"]["total_lean"] == expected_lean


def test_writer_market_interpretation_keeps_total_only_and_unavailable_quotes_distinct():
    total_only = _writer_market_interpretation({"spread_comparison": {
        "status": "unavailable",
        "market": {"status": "available", "spread_status": "unavailable",
                   "total": 220.5, "market_quote_id": "total-only-quote"},
        "final": {"status": "unavailable", "ats_lean": "unavailable"},
    }}, {"expected_total": 223.5})
    assert total_only["spread"]["status"] == "unavailable"
    assert total_only["spread"]["ats_lean"] == "unavailable"
    assert total_only["total"]["status"] == "available"
    assert total_only["total"]["total_edge"] == 3.0
    assert total_only["total"]["total_lean"] == "over"

    missing = _writer_market_interpretation({}, {"expected_total": 223.5})
    assert missing["spread"]["status"] == "unavailable"
    assert missing["total"]["status"] == "unavailable"
    assert missing["total"]["market_total"] is None
    assert missing["total"]["total_lean"] == "unavailable"


def test_v2_game_script_hard_fact_repair_updates_public_judgment_not_locked_forecast(tmp_path):
    from .analyst import build_analyst_context
    from .test_analyst import EVIDENCE, LAYERS
    game = {"game_id": "fixture-v2-game-script-repair", "home": {"name": "Home", "id": 1},
        "away": {"name": "Away", "id": 2}, "scheduled_tipoff": "2026-10-21T19:00:00-04:00",
        "cutoff": "2026-10-07T10:00:00+00:00",
        "venue": {"fullName": "Fixture Arena", "address": {"city": "Toronto"}}}
    context = build_analyst_context(LAYERS, EVIDENCE, missing_data=[],
        analysis_request="生成第25秒赛前分析。", game=game)
    evidence_id = next(item["id"] for item in context.objective_evidence if item.get("section") == "prediction")
    original_forecast = {
        "schema_version": "two-stage-forecast-v2",
        "independent": {"prediction": {"winner_side": "home", "win_probability": .56,
            "expected_home_margin": 2.5, "expected_total": 221.0}},
        "final_prediction": {"winner_side": "home", "win_probability": .55,
            "expected_home_margin": 3.0, "expected_total": 220.0},
        "final_analysis_core": {
            "central_question": "Which side can control the opening action?",
            "central_thesis": {"statement": "The first matchup should decide the early shape.",
                "classification": "inference", "evidence_ids": [evidence_id]},
            "key_matchups": [],
            "expected_game_script": "The teams open the game at Other Arena.",
            "flip_conditions": [],
        },
        "revision_delta": {"changed_axes": []}, "market_provenance": {"decision": "unavailable"},
        "review": {"factors": [], "pregame_plan": None},
    }
    frozen_forecast = copy.deepcopy(original_forecast)
    calls = []
    def client(messages, schema):
        calls.append(schema.__name__)
        if schema is EditorialDraft:
            return {"title": "A concrete matchup", "lead": "The first action matters.",
                "article": "The opening possessions will test both rotations.", "watch_themes": [],
                "confidence": "medium", "player_analysis": []}, {"fixture": True}
        assert schema is EditorialRepair
        request = json.loads(messages[-1]["content"])
        assert any(item["unit_id"] == "game_script" for item in request["hard_fact_blockers"])
        assert any(item["id"] == "game_script" for item in request["affected_text"])
        return {"patches": [{"unit_id": "game_script",
            "replacement": "主队希望先把比赛带入熟悉的半场节奏。"}]}, {"fixture": True}

    judgment, trace = run_editorial(context, original_forecast, tmp_path / "game-script-repair", writer_client=client, require_poem_title=False)
    assert calls == ["EditorialDraft", "EditorialRepair"]
    assert judgment["game_script"] == "主队希望先把比赛带入熟悉的半场节奏。"
    assert {key: judgment["final_prediction"][key] for key in
        ("winner_side", "win_probability", "expected_margin", "expected_total")} == {
            "winner_side": "home", "win_probability": .55, "expected_margin": 3.0, "expected_total": 220.0}
    assert original_forecast == frozen_forecast
    assert trace["publication_receipt"]["status"] == "eligible"
    assert trace["repair_provenance"][0]["unit_id"] == "game_script"
    assert trace["editorial_stages"][-1]["applied_patches"][0]["replacement_sha256"]


def test_decision_writer_blocks_internal_model_layer_names(tmp_path):
    from .analyst import build_analyst_context
    from .test_analyst import EVIDENCE, LAYERS
    context = build_analyst_context(LAYERS, EVIDENCE, missing_data=[],
        analysis_request="生成第25秒赛前分析。", game={"game_id": "fixture-visibility",
        "home": {"name": "Home", "id": 1}, "away": {"name": "Away", "id": 2}})
    evidence_id = next(iter(context.evidence_index))
    core = {"central_question": "Which side controls the switch matchup?",
        "central_thesis": {"statement": "The game turns on the guard matchup.", "classification": "inference",
            "evidence_ids": [evidence_id], "confidence": "medium"}, "matchup_status": "identified",
        "key_matchups": [{"subject": "Away guard", "opponent_or_scheme": "Home switch", "why_it_matters": "It tests the first action.",
            "expected_effect": "A forced help rotation opens the weak side.", "classification": "inference",
            "evidence_ids": [evidence_id], "confidence": "medium", "uncertainty": None}],
        "expected_game_script": "Away attacks the switch and Home rotates help.", "flip_conditions": []}
    locked = {"winner_side": "home", "win_probability": .55, "expected_margin": 3.0,
        "expected_total": 220.0, "evidence_ids": [evidence_id]}
    forecast = {"schema_version": "two-stage-forecast-v2", "independent": {"prediction": locked},
        "final_prediction": {"winner_side": "home", "win_probability": .55,
            "expected_home_margin": 3.0, "expected_total": 220.0}, "final_analysis_core": core,
        "revision_delta": {"winner_changed": False, "home_win_probability_delta": 0.0,
            "expected_home_margin_delta": 0.0, "expected_total_delta": 0.0, "changed_axes": []},
        "market_provenance": {"decision": "unavailable", "assessment": "unavailable",
            "market_timing_status": "unknown", "information_overlap": "unknown"},
        "review": {"factors": [], "pregame_plan": None}}
    def leaking_writer(_messages, _schema):
        return {"title": "A matchup", "lead": "A clean lead.", "article": "Pure gives the baseline.",
            "watch_themes": [], "confidence": "medium", "player_analysis": []}, {"fixture": True}
    with pytest.raises(ValueError, match="editorial_internal_model_layer_leak:Pure"):
        run_editorial(context, forecast, tmp_path / "visibility", writer_client=leaking_writer, require_poem_title=False)


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_editorial_draft_accepts_zero_through_four_watch_themes(count):
    draft = {"title": "Fixture title", "lead": "Fixture lead", "article": SHORT_ARTICLE,
             "watch_themes": [{"title": "Theme", "statement": "Fixture theme."} for _ in range(count)],
             "confidence": "low"}
    assert len(EditorialDraft.model_validate(draft).watch_themes) == count


def test_editorial_draft_rejects_more_than_four_watch_themes():
    draft = {"title": "Fixture title", "lead": "Fixture lead", "article": SHORT_ARTICLE,
             "watch_themes": [{"title": "Theme", "statement": "Fixture theme."} for _ in range(5)],
             "confidence": "low"}
    with pytest.raises(ValueError):
        EditorialDraft.model_validate(draft)


def test_editorial_draft_allows_eight_source_bound_candidates_for_status_replacement():
    draft = {"title": "Fixture title", "lead": "Fixture lead", "article": SHORT_ARTICLE,
             "watch_themes": [], "confidence": "medium",
             "player_analysis": [{"player_name": f"Fixture Player {index}",
                                   "statement": "该角色与本场对手之间的互动值得观察。",
                                   "fact_ids": ["fixture-current"]} for index in range(8)]}
    assert len(EditorialDraft.model_validate(draft).player_analysis) == 8


def current_player_fixture(context):
    context = context.model_copy(deep=True)
    team_id = context.game["home"]["id"]
    context.roster_context = {"teams": {"home": {"team_id": team_id, "official_team_roster": {
        "status": "available", "team_id": team_id,
        "players": [{"name": "Fixture Guard", "team_id": team_id, "player_id": "fixture-current"}]}}}}
    context.objective_evidence.append({"id": "fixture-current-roster", "section": "roster_context",
                                       "kind": "Context", "value": context.roster_context})
    context.evidence_index["fixture-current-roster"] = {"section": "roster_context", "label": "synthetic current roster"}
    return context


def test_key_player_current_identity_evidence_and_frozen_numeric_output(frozen, tmp_path):
    context, forecast = frozen
    context = current_player_fixture(context)
    def with_player(messages, schema):
        draft, meta = writer(messages, schema)
        facts = json.loads(messages[-1]["content"])["facts"]
        roster = next(f for f in facts if f["kind"] == "CURRENT" and f["value"].get("roster_snapshot"))
        name = roster["value"]["roster_snapshot"][0]["name"]
        draft["player_analysis"] = [{"player_name": name, "statement": "这名球员的持球选择是我最想观察的变量。", "fact_ids": [roster["id"]]}]
        return draft, meta
    judgment, trace = run_editorial(context, forecast, tmp_path / "players", writer_client=with_player, require_poem_title=False)
    assert len(judgment["player_analysis"]) == 1
    assert judgment["player_analysis"][0]["classification"] == "subjective_analysis"
    assert set(judgment["player_analysis"][0]["evidence_ids"]) <= set(context.evidence_index)
    assert judgment["final_prediction"]["expected_total"] == forecast["final_prediction"]["expected_total"]
    request = json.loads((tmp_path / "players/writer0_request.json").read_text(encoding="utf-8"))
    assert "player_analysis" in request["schema"]["required"]
    assert trace["editorial_calls"] == 1


@pytest.mark.parametrize("mode", ["historical", "unknown_fact", "duplicate"])
def test_key_player_rejects_historical_only_identity_unknown_fact_or_duplicate(frozen, tmp_path, mode):
    context, forecast = frozen
    context = current_player_fixture(context)
    def invalid(messages, schema):
        from .editorial_pipeline import PlayerReferenceRepair
        if schema is PlayerReferenceRepair:
            targets = json.loads(messages[-1]["content"])["targets"]
            return {"patches": [{"index": t["index"], **t["original"]} for t in targets]}, {"fixture": True}
        draft, meta = writer(messages, schema)
        facts = json.loads(messages[-1]["content"])["facts"]
        roster = next(f for f in facts if f["kind"] == "CURRENT" and f["value"].get("roster_snapshot"))
        name = roster["value"]["roster_snapshot"][0]["name"]
        fact_id = "missing" if mode == "unknown_fact" else (next(f["id"] for f in facts if f["kind"] == "HISTORICAL") if mode == "historical" else roster["id"])
        p = {"player_name": name, "statement": "这名球员的角色值得看。", "fact_ids": [fact_id]}
        draft["player_analysis"] = [p, copy.deepcopy(p)] if mode == "duplicate" else [p]
        return draft, meta
    with pytest.raises(ValueError, match="key_player_"):
        run_editorial(context, forecast, tmp_path / mode, writer_client=invalid, require_poem_title=False)


def test_key_player_hard_fact_repair_is_scoped_to_its_statement(frozen, tmp_path):
    context, forecast = frozen
    context = current_player_fixture(context)
    def scripted(messages, schema):
        if schema is EditorialDraft:
            draft, meta = writer(messages, schema)
            roster = next(f for f in json.loads(messages[-1]["content"])["facts"]
                          if f["kind"] == "CURRENT" and f["value"].get("roster_snapshot"))
            draft["player_analysis"] = [{"player_name": "Fixture Guard", "statement": "Fixture Guard is out for this game.",
                                         "fact_ids": [roster["id"]]}]
            return draft, meta
        assert schema is EditorialRepair
        assert any(b["unit_id"] == "player_analysis.0.statement" for b in
                   json.loads(messages[-1]["content"])["hard_fact_blockers"])
        return {"patches": [{"unit_id": "player_analysis.0.statement", "replacement": "他的出战情况未确认，若能上场，持球选择值得看。"}]}, {}
    judgment, trace = run_editorial(context, forecast, tmp_path / "player-repair", writer_client=scripted, require_poem_title=False)
    assert judgment["player_analysis"][0]["player_name"] == "Fixture Guard"
    assert "未确认" in judgment["player_analysis"][0]["statement"]
    assert trace["editorial_calls"] == 2


def test_short_opinionated_article_publishes_without_sentence_citations_or_accept_loop(frozen, tmp_path):
    context, forecast = frozen
    original = copy.deepcopy(context.model_dump())
    numbers = copy.deepcopy(forecast["final_prediction"])
    seen = {}
    def capture(messages, schema):
        seen.update(json.loads(messages[-1]["content"]))
        return writer(messages, schema)
    judgment, trace = run_editorial(context, forecast, tmp_path / "editorial", writer_client=capture, require_poem_title=False)
    assert judgment["analysis_article"] == SHORT_ARTICLE
    assert len(judgment["analysis_article"]) < 400
    assert trace["editorial_calls"] == 1
    assert trace["quality_review"]["status"] == "disabled"
    assert any(fact["kind"] == "ANALYST_REASONING" for fact in seen["facts"])
    assert any(fact["kind"] == "MODEL_OUTPUT" for fact in seen["facts"])
    assert judgment["final_prediction"]["winner_side"] == numbers["winner_side"]
    assert judgment["final_prediction"]["win_probability"] == numbers["win_probability"]
    assert judgment["final_prediction"]["expected_margin"] == abs(numbers["expected_home_margin"])
    assert judgment["final_prediction"]["expected_total"] == numbers["expected_total"]
    assert context.model_dump() == original
    verify_publication_receipt({"game_id": context.game["game_id"], "judgment": judgment,
        "prediction": judgment["final_prediction"], "analyst_context": original,
        "editorial_projection": trace["editorial_projection"],
        "editorial_material": trace["editorial_material"],
        "quality_review_input_sha256": trace["quality_review_input_sha256"],
        "publication_receipt": trace["publication_receipt"]})


def test_editorial_pipeline_can_publish_with_zero_watch_themes(frozen, tmp_path):
    context, forecast = frozen
    numbers = copy.deepcopy(forecast["final_prediction"])
    def no_themes(messages, schema):
        draft, usage = writer(messages, schema)
        draft["watch_themes"] = []
        return draft, usage
    judgment, trace = run_editorial(context, forecast, tmp_path / "no-watch-themes", writer_client=no_themes, require_poem_title=False)
    assert judgment["watch_themes"] == []
    assert trace["publication_receipt"]["status"] == "eligible"
    assert judgment["final_prediction"]["expected_total"] == numbers["expected_total"]


@pytest.mark.parametrize("status", ["warning", "hold", "passed"])
def test_quality_feedback_never_blocks_publication(frozen, tmp_path, status):
    context, forecast = frozen
    def reviewer(messages, schema):
        assert schema is QualityReview
        return {"status": status, "warnings": ["文章的对位解释还可更具体。"],
                "possible_hard_fact_conflicts": []}, {"fixture": True}
    judgment, trace = run_editorial(context, forecast, tmp_path / status,
        writer_client=writer, reviewer_client=reviewer, require_poem_title=False)
    assert judgment["analysis_article"] == SHORT_ARTICLE
    assert trace["publication_receipt"]["status"] == "eligible"
    assert trace["publication_receipt"]["quality_review"]["status"] == status
    assert trace["editorial_calls"] == 2
    request = json.loads((tmp_path / status / "quality_review_request.json").read_text(encoding="utf-8"))
    assert trace["quality_review_input_sha256"] == request["input_hash"]
    assert trace["publication_receipt"]["quality_review_input_sha256"] == request["input_hash"]


def test_quality_reviewer_timeout_is_recorded_but_nonblocking(frozen, tmp_path):
    context, forecast = frozen
    def unavailable(*args, **kwargs):
        raise TimeoutError("optional quality reviewer timed out")
    judgment, trace = run_editorial(context, forecast, tmp_path / "unavailable",
        writer_client=writer, reviewer_client=unavailable, require_poem_title=False)
    assert judgment["analysis_article"] == SHORT_ARTICLE
    assert trace["publication_receipt"]["status"] == "eligible"
    assert trace["quality_review"]["status"] == "unavailable"
    assert (tmp_path / "unavailable/quality_review_failure.json").is_file()
    request = json.loads((tmp_path / "unavailable/quality_review_request.json").read_text(encoding="utf-8"))
    assert trace["quality_review_input_sha256"] == request["input_hash"]


def test_quality_hold_with_exact_candidate_remains_a_warning_without_deterministic_match(frozen, tmp_path):
    context, forecast = frozen
    def reviewer(messages, schema):
        assert schema is QualityReview
        return {"status": "hold", "warnings": ["请人工关注来源范围。"],
                "possible_hard_fact_conflicts": [{"quote": "我更看好热火，但关键仍在新组合如何落地。",
                    "code": "exact_stat_sample_mismatch", "fact_ids": []}]}, {"fixture": True}
    judgment, trace = run_editorial(context, forecast, tmp_path / "candidate", writer_client=writer,
        reviewer_client=reviewer, require_poem_title=False)
    assert judgment["analysis_article"] == SHORT_ARTICLE
    assert trace["publication_receipt"]["status"] == "eligible"
    assert trace["publication_receipt"]["quality_review"]["status"] == "hold"


def test_one_exact_stat_blocker_gets_one_local_repair_and_recheck(frozen, tmp_path):
    context, forecast = frozen
    calls = []
    def generate(messages, schema):
        calls.append(schema.__name__)
        if schema is EditorialDraft:
            value, usage = writer(messages, schema)
            value["article"] = "热火上赛季近15场场均失分128.6。"
            return value, usage
        assert schema is EditorialRepair
        request = json.loads(messages[-1]["content"])
        assert any(item["code"] == "exact_stat_sample_mismatch" for item in request["hard_fact_blockers"])
        return {"patches": [{"unit_id": request["affected_text"][0]["id"],
            "replacement": "热火上赛季近10场场均失分128.6。"}]}, {"fixture": True}
    judgment, trace = run_editorial(context, forecast, tmp_path / "repair", writer_client=generate, require_poem_title=False)
    assert calls == ["EditorialDraft", "EditorialRepair"]
    assert trace["repair_rounds"] == 1
    assert not validate_publication_hard_facts(judgment, trace["editorial_projection"])
    assert judgment["final_prediction"]["expected_total"] == forecast["final_prediction"]["expected_total"]


def test_repair_no_op_is_rejected_and_recorded_before_application(frozen, tmp_path):
    context, forecast = frozen
    def scripted(messages, schema):
        if schema is EditorialDraft:
            value, meta = writer(messages, schema)
            value["article"] = "热火上赛季近15场场均失分128.6。"
            return value, meta
        request = json.loads(messages[-1]["content"])
        unit_id = request["affected_text"][0]["id"]
        original = request["affected_text"][0]["text"]
        return {"patches": [{"unit_id": unit_id, "replacement": original}]}, {"fixture": True}
    with pytest.raises(ValueError, match="editorial_repair_ineffective"):
        run_editorial(context, forecast, tmp_path / "no-op", writer_client=scripted, require_poem_title=False)
    validation = json.loads((tmp_path / "no-op/repair1_validation.json").read_text(encoding="utf-8"))
    assert validation["status"] == "rejected"
    assert validation["no_op_targets"]
    assert validation["applied_patches"] == []
    assert validation["post_repair_findings"]
    assert not (tmp_path / "no-op/publication_receipt.json").exists()


def test_effective_repair_deletes_unsupported_number_and_reduces_findings(frozen, tmp_path):
    context, forecast = frozen
    def scripted(messages, schema):
        if schema is EditorialDraft:
            value, meta = writer(messages, schema)
            value["article"] = "热火上赛季近15场场均失分128.6。"
            return value, meta
        request = json.loads(messages[-1]["content"])
        return {"patches": [{"unit_id": request["affected_text"][0]["id"],
            "replacement": "热火的防守表现仍值得观察。"}]}, {"fixture": True}
    judgment, trace = run_editorial(context, forecast, tmp_path / "effective-delete", writer_client=scripted, require_poem_title=False)
    validation = json.loads((tmp_path / "effective-delete/repair1_validation.json").read_text(encoding="utf-8"))
    assert validation["status"] == "applied_and_rechecked"
    assert validation["applied_patches"]
    assert validation["post_repair_findings"] == []
    assert judgment["analysis_article"] == "热火的防守表现仍值得观察。"
    assert trace["publication_receipt"]["status"] == "eligible"


def test_repair_rejects_new_numeric_claim_not_present_in_frozen_projection():
    from .editorial_pipeline import apply_patches
    draft = {"title": "Fixture", "lead": "Lead", "article": "某队曾场均得分未知。",
        "watch_themes": [], "confidence": "medium", "player_analysis": []}
    unit = {"id": "article.0", "text": "某队曾场均得分未知。", "aliases": ["article.0"],
        "article_span": {"start": 0, "end": len(draft["article"])} }
    with pytest.raises(ValueError, match="editorial_repair_new_numeric_claim_unverified"):
        apply_patches(draft, [unit], [{"unit_id": "article.0", "replacement": "某队曾场均得分240分。"}],
            {"article.0"}, projection={"game": {}, "locked_prediction": {}, "facts": []})


def test_repair_can_replace_a_wrong_number_with_the_matching_frozen_fact():
    from .editorial_pipeline import apply_patches
    original = "尼克斯上赛季场均篮板32.5个。"
    draft = {"title": "Fixture", "lead": "Lead", "article": original,
        "watch_themes": [], "confidence": "medium", "player_analysis": []}
    unit = {"id": "article.0", "text": original, "aliases": ["article.0"],
        "article_span": {"start": 0, "end": len(original)}}
    projection = {"game": {}, "locked_prediction": {}, "facts": [{"kind": "HISTORICAL",
        "value": {"metrics": {"rebounds_per_game": 43.0}}}]}
    repaired = apply_patches(draft, [unit], [{"unit_id": "article.0",
        "replacement": "尼克斯上赛季场均篮板43.0个。"}], {"article.0"}, projection=projection)
    assert repaired["article"] == "尼克斯上赛季场均篮板43.0个。"


def test_capacity_fix_applies_13_unit_repair_and_runs_post_repair_gate():
    from .editorial_contract import visible_units
    from .editorial_pipeline import MAX_EDITORIAL_REPAIR_PATCHES, apply_patches

    venues = ["Alpha", "Bravo", "Cedar", "Delta", "Echo", "Foxtrot", "Grove",
              "Harbor", "Ivory", "Juniper", "Kestrel", "Linden", "Maple"]
    article = "".join(f"比赛被安排在 {name} Arena。" for name in venues)
    draft = {"title": "Fixture", "lead": "Fixture lead", "article": article,
        "watch_themes": [], "confidence": "medium", "player_analysis": []}
    judgment = {"analysis_article": article}
    projection = {"game": {"venue": {"fullName": "Madison Square Garden"}}, "facts": []}
    findings = validate_publication_hard_facts(judgment, projection)
    units = visible_units(judgment)
    repairable = sorted({item["unit_id"] for item in findings})
    assert len(repairable) == 13
    assert MAX_EDITORIAL_REPAIR_PATCHES >= len(repairable)

    response = EditorialRepair.model_validate({"patches": [
        {"unit_id": unit_id, "replacement": "双方的开局选择会影响这场比赛的空间对位。"}
        for unit_id in repairable
    ]})
    repaired, judgment_overrides, provenance = apply_patches(
        draft, units, response.model_dump()["patches"], set(repairable),
        include_judgment_overrides=True)

    assert repaired["article"] != article
    assert judgment_overrides == []
    assert len(provenance) == 13
    assert validate_publication_hard_facts(
        {"analysis_article": repaired["article"]}, projection) == []


def test_repair_capacity_is_finite_and_overflow_fails_closed():
    from .editorial_pipeline import MAX_EDITORIAL_REPAIR_PATCHES, apply_patches
    assert MAX_EDITORIAL_REPAIR_PATCHES == 32
    patches = [{"unit_id": f"article.{index}", "replacement": "修正后的文字。"}
               for index in range(MAX_EDITORIAL_REPAIR_PATCHES + 1)]
    with pytest.raises(Exception):
        EditorialRepair.model_validate({"patches": patches})
    with pytest.raises(ValueError, match="editorial_repair_capacity_exceeded"):
        apply_patches({"title": "x", "lead": "y", "article": "z", "watch_themes": [],
            "confidence": "medium", "player_analysis": []}, [], patches, set())


def test_repair_rejects_conflicting_duplicate_unit_patches():
    from .editorial_pipeline import apply_patches
    draft = {"title": "Fixture", "lead": "Lead", "article": "原句。",
        "watch_themes": [], "confidence": "medium", "player_analysis": []}
    units = [{"id": "article.0", "text": "原句。", "aliases": ["article.0"],
        "article_span": {"start": 0, "end": 3}}]
    patches = [{"unit_id": "article.0", "replacement": "替换甲。"},
               {"unit_id": "article.0", "replacement": "替换乙。"}]
    with pytest.raises(ValueError, match="editorial_repair_duplicate_target"):
        apply_patches(draft, units, patches, {"article.0"})


def test_13th_patch_cannot_modify_locked_prediction_even_under_expanded_capacity():
    from .editorial_pipeline import MAX_EDITORIAL_REPAIR_PATCHES, apply_patches
    draft = {"title": "Fixture", "lead": "Lead", "article": "原句。",
        "watch_themes": [], "confidence": "medium", "player_analysis": []}
    units = [{"id": f"article.{index}", "text": f"原句{index}。", "aliases": [f"article.{index}"],
        "article_span": {"start": 0, "end": 3}} for index in range(13)]
    units.append({"id": "final_prediction.expected_total", "text": "226.5",
        "aliases": ["final_prediction.expected_total"]})
    allowed = {unit["id"] for unit in units}
    patches = [{"unit_id": unit["id"], "replacement": "修正文字。"} for unit in units]
    assert len(patches) <= MAX_EDITORIAL_REPAIR_PATCHES
    with pytest.raises(ValueError, match="editorial_repair_target_not_publicly_editable"):
        apply_patches(draft, units, patches, allowed, include_judgment_overrides=True)


def test_hard_fact_still_present_after_repair_fails_closed_without_another_retry(frozen, tmp_path):
    context, forecast = frozen
    calls = []
    def generate(messages, schema):
        calls.append(schema.__name__)
        if schema is EditorialDraft:
            value, usage = writer(messages, schema)
            value["article"] = "热火上赛季近15场场均失分128.6。"
            return value, usage
        request = json.loads(messages[-1]["content"])
        return {"patches": [{"unit_id": request["affected_text"][0]["id"],
            "replacement": "热火上赛季近15场场均失分128.6。"}]}, {"fixture": True}
    with pytest.raises(ValueError, match="editorial_repair_ineffective"):
        run_editorial(context, forecast, tmp_path / "repair-failed", writer_client=generate, require_poem_title=False)
    assert calls == ["EditorialDraft", "EditorialRepair"]
    validation = json.loads((tmp_path / "repair-failed/repair1_validation.json").read_text(encoding="utf-8"))
    assert validation["no_op_targets"]
    assert validation["post_repair_findings"]


def test_writer_cannot_return_a_second_forecast(frozen, tmp_path):
    context, forecast = frozen
    def illicit(messages, schema):
        value, usage = writer(messages, schema)
        value["final_prediction"] = {"winner_side": "home", "expected_total": 250}
        return value, usage
    with pytest.raises(Exception):
        run_editorial(context, forecast, tmp_path / "illicit", writer_client=illicit, require_poem_title=False)
    assert not (tmp_path / "illicit/quality_review_request.json").exists()


@pytest.mark.parametrize("field,value", [
    ("winner_side", "home"), ("win_probability", 0.58),
    ("expected_margin", 9.5), ("expected_total", 244.5),
])
def test_writer_schema_rejects_each_second_numeric_forecast_field(frozen, tmp_path, field, value):
    context, forecast = frozen
    def illicit(messages, schema):
        draft, usage = writer(messages, schema)
        draft["final_prediction"] = {field: value}
        return draft, usage
    with pytest.raises(Exception):
        run_editorial(context, forecast, tmp_path / field, writer_client=illicit, require_poem_title=False)


def test_publication_pipeline_contains_no_sentence_accept_gate():
    from pathlib import Path
    source = Path(__file__).with_name("editorial_pipeline.py").read_text(encoding="utf-8")
    assert "validate_semantic_report" not in source
    assert "SemanticReview" not in source
    assert "status == \"accept\"" not in source


def test_optional_quality_prompt_has_no_sentence_accept_protocol():
    from .editorial_pipeline import REVIEW_PROTOCOL
    assert "逐单元" not in REVIEW_PROTOCOL
    assert "必须返回每句话" not in REVIEW_PROTOCOL
    assert "accept/reject/hold" not in REVIEW_PROTOCOL


def test_article_length_is_nonempty_schema_not_minimum_word_gate():
    article = {"title": "导读", "lead": "我看好客队。",
        "article": "简短但完整。", "watch_themes": [
            {"title": "变量", "statement": "看出场角色。"},
            {"title": "对位", "statement": "看篮板与护框。"}], "confidence": "low"}
    assert normalize(article)["article"] == "简短但完整。"
    article["article"] = ""
    with pytest.raises(Exception):
        normalize(article)


def test_player_reference_revision_uses_current_witness_without_rewriting_article(frozen, tmp_path):
    from .editorial_pipeline import PlayerReferenceRepair
    context, forecast = frozen
    context = current_player_fixture(context)
    calls=[]
    def scripted(messages, schema):
        calls.append(schema)
        payload=json.loads(messages[-1]["content"])
        if schema is EditorialDraft:
            draft, meta=writer(messages,schema)
            roster=next(f for f in payload['facts']if f['kind']=='CURRENT'and f['value'].get('roster_snapshot'))
            historical=next(f for f in payload['facts']if f['kind']=='HISTORICAL')
            draft['player_analysis']=[{'player_name':roster['value']['roster_snapshot'][0]['name'],
                'statement':'这名球员的持球选择是关键。','fact_ids':[historical['id']]}]
            return draft,meta
        assert schema is PlayerReferenceRepair
        target=payload['targets'][0]
        return {'patches':[{'index':target['index'],**target['original'],
            'fact_ids':[target['current_identity'][0]['fact_id']]}]}, {'fixture':True}
    judgment,trace=run_editorial(context,forecast,tmp_path/'identity-revision',writer_client=scripted,require_poem_title=False)
    assert calls==[EditorialDraft,PlayerReferenceRepair]
    assert judgment['analysis_article']==SHORT_ARTICLE
    assert judgment['final_prediction']['expected_total']==forecast['final_prediction']['expected_total']
    assert trace['editorial_calls']==2
    receipt=json.loads((tmp_path/'identity-revision/player_reference_validation.json').read_text(encoding='utf-8'))
    assert receipt['status']=='accepted'and receipt['other_draft_fields_changed']is False
