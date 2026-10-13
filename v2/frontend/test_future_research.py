"""Current evidence checks use offline doubles, never paid calls."""
from types import SimpleNamespace
from datetime import datetime, timezone
import pytest
from v2.pregame_system.web_search import PregameWebSearch
from v2.pregame_system.web_research_agent import AutonomousWebResearcher
from .analysis_service import search_categories

GAME={"game_id":"espn:401902644","scheduled_tipoff":"2026-10-03T23:00:00Z","away_team":"Miami Heat",
      "home_team":"Toronto Raptors","away_team_id":1610612748,"home_team_id":1610612761,
      "status":"scheduled","registered_game":True,"game_type":"preseason","season_start":2026}


def test_existing_status_and_starters_do_not_skip_freshness():
    game={"registered_game":True,"status":"scheduled","background":{"availability":{"status":"available"}},
          "roster_context":{"teams":{"home":{"confirmed_starters":["cached"]},"away":{"projected_starters":["cached"]}}}}
    assert {"availability","lineup","roster_news"} <= search_categories(game)


def test_fact_and_baseline_adapters_preserve_provider_english_names():
    from .generation_input import registered_fact_game, registered_schedule_game
    row={"id":GAME["game_id"],"tipoff":GAME["scheduled_tipoff"],"status":"scheduled","game_type":"preseason",
         "home":{"id":GAME["home_team_id"]},"away":{"id":GAME["away_team_id"]},
         "home_team":"Toronto Raptors","away_team":"Miami Heat"}
    fact=registered_fact_game(row)
    assert fact["home"]["name"]=="Toronto Raptors" and fact["away"]["name"]=="Miami Heat"
    assert fact["home"]["name_zh"]=="猛龙" and fact["away"]["name_zh"]=="热火"
    schedule=registered_schedule_game(fact)
    assert schedule["home_team"]=="Toronto Raptors" and schedule["away_team"]=="Miami Heat"


def test_bilingual_discovery_is_not_a_domain_whitelist_or_fact(tmp_path):
    seen=[]
    def transport(payload):
        seen.append(payload)
        return {"results":[{"url":"https://bbs.hupu.com/123.html","title":"热火 猛龙 赛前伤病讨论",
                            "content":"球迷认为某球员缺阵","published_date":"2026-10-02T10:00:00Z"}]}
    search=PregameWebSearch(root=tmp_path,transport=transport)
    result=search.search(GAME,"2026-10-02T12:00:00Z",categories={"availability","roster_news"})
    assert any("Miami Heat" in p["query"] for p in seen) and any("热火" in p["query"] for p in seen)
    assert any("site:hupu.com" in p["query"] for p in seen) and any("site:zhibo8.com" in p["query"] for p in seen)
    assert all("include_domains" not in p for p in seen)
    assert all(not r["eligible_for_fact"] for r in result["records"])
    assert result["discovery_receipt_path"]


@pytest.mark.parametrize("case",["forum","old","no_time","after_cutoff","valid"])
def test_source_fetch_not_search_snippet_controls_fact_eligibility(tmp_path,case):
    url="https://www.espn.com/nba/story/heat-training" if case!="forum" else "https://bbs.hupu.com/123.html"
    date={"old":"2025-10-02T10:00:00Z","no_time":None,"after_cutoff":"2026-10-03T10:00:00Z"}.get(case,"2026-10-02T10:00:00Z")
    quote="Miami Heat coach discussed the current training camp rotation at practice."
    content=quote+" Miami Heat current preseason training update. "*10
    researcher=AutonomousWebResearcher(searcher=object(),root=tmp_path)
    context={"live_registered":True,"game":GAME,"records":[{"id":"source","category":"availability","url":url,
        "title":"Heat training update","published_at":date,"snippet":"unverified","eligible_for_fact":False}]}
    researcher._verify_future_fetches(context,[{"url":url,"content":content,"published_date":date}],"2026-10-02T12:00:00Z",
        claims=[{"url":url,"quote":quote,"claim_type":"coach_comments","assertion_kind":"confirmed_fact",
                 "team_id":GAME["away_team_id"],"game_reference":""}])
    assert bool(context["records"]) is (case=="valid")
    assert context["source_records"][0]["captured_at"]


def test_verified_claims_and_unknowns_reach_actual_analyst_context(tmp_path, monkeypatch):
    import copy
    from v2.pregame_system.analyst import AnalystJudgment
    from v2.pregame_system.forecasting_workflow import prepare_analyst
    from .analysis_service import SavedGameAnalysisService, game_identity, prediction_layers
    from .export_data import build_app_data
    from v2.pregame_system.research_evidence import ResearchEvidencePipeline
    data = build_app_data()
    game = data["games"][0]
    game["registered_game"] = True
    game["generation_context"] = {"stage": "EARLY_PREVIEW"}
    identity = game_identity(game)
    url = "https://www.nba.com/news/current-season-team-preview"
    team = game["home"]["name"]
    quote = team + " added a new member to the coaching staff this offseason."
    p = ResearchEvidencePipeline(identity, "2026-10-02T16:00:00Z")
    p.add_source({"url": url, "title": team + " 2026-27 preview", "category": "lineup"},
        {"url": url, "content": quote + " More context about this season and coaching changes." * 3,
         "published_date": "2026-09-22", "captured_at": "2026-10-02T16:00:01Z"})
    p.validate_claim({"url": url, "quote": quote, "claim_type": "coach_change", "assertion_kind": "confirmed_fact",
                      "team_id": game["home"]["id"], "game_reference": ""})
    before = copy.deepcopy(prediction_layers(game))
    seen = {}
    class Research:
        def investigate(self, supplied_game, cutoff, **kwargs):
            assert supplied_game["research_stage"] == "EARLY_PREVIEW"
            return {"status": "available", "records": p.claims, "minimum_research": p.summary()}, {"status": "success"}
    class Analyst:
        def judge(self, context):
            seen["context"] = context
            return AnalystJudgment(winner_lean="home", side_lean="no_lean", total_lean="no_lean", confidence="low",
                supporting_evidence_ids=[p.claims[0]["id"]], conflicting_evidence_ids=[],
                baseline_agreement={"pure": "agree", "market": "not_applicable", "residual": "not_applicable"},
                subjective_rationale="离线测试：当前信息只支持人员背景，尚未确认本场出战。"), {"status": "success"}
    monkeypatch.setattr(SavedGameAnalysisService, "_attach_player_knowledge", lambda *args: None)
    result = SavedGameAnalysisService(data, analyst=Analyst(), researcher=Research(), searcher=object(), require_research_success=True).analyze(
        game["id"], cutoff="2026-10-02T16:00:00Z")
    context = seen["context"]
    basketball, _, _ = prepare_analyst(context.model_dump())
    assert any(item["value"].get("quote") == quote for item in basketball["evidence"])
    assert any(item.get("section") == "research.availability" and item.get("status") == "UNKNOWN" for item in basketball["missing_data"])
    assert result["model_layers"] == before
    assert result["sources"][0]["publication_precision"] == "date"
    assert not any("hypothesis" in str(item["value"]) for item in context.objective_evidence)
