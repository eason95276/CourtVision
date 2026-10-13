from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from .web_research_agent import AutonomousWebResearcher, WebResearchAgentError


GAME = {
    "game_id": "espn:fixture",
    "away_team": "Philadelphia 76ers",
    "home_team": "New York Knicks",
    "away_team_id": 1610612755,
    "home_team_id": 1610612752,
    "scheduled_tipoff": "2026-10-20T23:00:00Z",
}


def test_optional_oversized_focus_falls_back_to_existing_category_queries(tmp_path):
    seen = []
    def search(*args, **kwargs):
        seen.append(kwargs)
        return {"status":"unavailable", "records":[]}
    def client(*args, **kwargs):
        return [{"name":"search_pregame_web", "args":{"categories":["availability"],
            "reason":"current status", "focus_category":"availability", "focus":"x"*101}}]
    _, trace = AutonomousWebResearcher(searcher=SimpleNamespace(search=search),root=tmp_path,
        client=client).investigate(GAME,"2026-10-20T18:55:00Z",categories={"availability"})
    assert len(seen)==1 and seen[0]["focus"] is None and seen[0]["categories"]=={"availability"}
    assert trace["calls"][0]["ignored_oversized_optional_focus"] is True


def test_deepseek_selects_search_categories_and_fetches_returned_source(tmp_path):
    calls = []

    def client(stage, messages, tools, required):
        calls.append((stage, required))
        if stage == "search":
            payload = json.loads(messages[-1]["content"])
            assert payload["local_context"]["matchup_history"]["matchup_relevance"] == "high"
            return [{
                "name": "search_pregame_web",
                "args": {
                    "categories": ["availability", "market"],
                    "reason": "需要伤病和盘口",
                    "focus_category": "availability",
                    "focus": "Joel Embiid injury update",
                },
            }]
        return [{
            "name": "fetch_sources",
            "args": {"urls": ["https://www.nba.com/playoffs/2026/nba-finals"], "reason": "确认冠军事实"},
        }]

    class Searcher:
        local_transport = SimpleNamespace(
            fetch_many=lambda urls: [{
                "url": urls[0],
                "title": "2026 NBA Finals",
                "content": "New York Knicks won the 2026 NBA championship.",
                "published_date": "2026-06-14T00:00:00Z",
            }]
        )

        def search(self, game, cutoff, *, replay, categories, focus):
            assert categories == {"availability", "market"}
            assert focus == ("availability", "Joel Embiid injury update")
            return {
                "status": "available",
                "records": [{
                    "id": "story",
                    "category": "availability",
                    "title": "2026 NBA Finals",
                    "url": "https://www.nba.com/playoffs/2026/nba-finals",
                    "snippet": "Knicks celebrate title.",
                    "published_at": None,
                    "eligible_for_fact": True,
                }],
            }

    context, trace = AutonomousWebResearcher(
        searcher=Searcher(),
        root=tmp_path,
        client=client,
    ).investigate(
        GAME,
        "2026-10-20T18:55:00Z",
        categories={"game_story", "market", "availability"},
        local_context={
            "matchup_history": {"matchup_relevance": "high", "series_summary": {"games": 4}},
            "season_phase": {"phase": "opening_night"},
        },
    )
    assert calls == [("search", True), ("fetch", False)]
    assert context["records"][0]["snippet"].startswith("New York Knicks won")
    assert context["records"][0]["fetch_provider"] == "free-search-mcp"
    assert [item["tool"] for item in trace["calls"]] == [
        "search_pregame_web",
        "fetch_sources",
    ]


def test_researcher_rejects_categories_outside_current_game_scope(tmp_path):
    def client(stage, messages, tools, required):
        return [{
            "name": "search_pregame_web",
            "args": {"categories": ["market"], "reason": "查询盘口"},
        }]

    researcher = AutonomousWebResearcher(
        searcher=SimpleNamespace(),
        root=tmp_path,
        client=client,
    )
    with pytest.raises(WebResearchAgentError, match="research_agent_empty_categories"):
        researcher.investigate(
            GAME,
            "2026-10-20T18:55:00Z",
            categories={"availability"},
        )
    assert list(tmp_path.glob("*/failure.json"))


def test_series_recap_with_a_short_snippet_can_be_fetched_for_missing_details(tmp_path):
    url = "https://www.nba.com/game/nyk-vs-phi-0042500214"

    def client(stage, messages, tools, required):
        if stage == "search":
            return [{"name": "search_pregame_web", "args": {
                "categories": ["game_story"], "reason": "补充系列赛比赛过程",
            }}]
        payload = json.loads(messages[-1]["content"])
        assert payload["local_context"]["matchup_history"]["matchup_relevance"] == "high"
        assert payload["sources"][0]["snippet"] == "Knicks sweep the series."
        return [{"name": "fetch_sources", "args": {"urls": [url], "reason": "读取复盘细节"}}]

    searcher = SimpleNamespace(
        search=lambda *args, **kwargs: {"status": "available", "records": [{
            "id": "recap", "category": "game_story", "url": url,
            "snippet": "Knicks sweep the series.", "published_at": "2026-05-11T00:00:00Z",
        }]},
        local_transport=SimpleNamespace(fetch_many=lambda urls: [{
            "url": url, "content": "Full recap with observed possessions and player quotes.",
            "published_date": "2026-05-11T00:00:00Z",
        }]),
    )
    context, trace = AutonomousWebResearcher(searcher=searcher, client=client, root=tmp_path).investigate(
        GAME, "2026-09-27T00:00:00Z", categories={"game_story"},
        local_context={"matchup_history": {"matchup_relevance": "high"}},
    )
    assert context["records"][0]["snippet"].startswith("Full recap")
    assert len(trace["calls"]) == 2


def test_historical_research_uses_archive_without_model_or_live_fetch(tmp_path):
    events = []

    class ArchiveSearcher:
        def search(self, game, cutoff, *, replay, categories):
            events.append(("archive", replay, cutoff))
            return {
                "status": "unavailable",
                "reason": "historical_source_archive_unavailable",
                "records": [],
                "trace": [],
            }

    def client(*_args, **_kwargs):
        pytest.fail("historical replay called an LLM tool selector")

    researcher = AutonomousWebResearcher(
        searcher=ArchiveSearcher(),
        root=tmp_path,
        client=client,
    )
    context, trace = researcher.investigate(
        GAME,
        "2026-10-20T18:55:00Z",
        replay=True,
        categories={"game_story", "availability"},
    )

    assert events == [("archive", True, "2026-10-20T18:55:00Z")]
    assert context["status"] == "unavailable"
    assert trace["mode"] == "historical_archive"
    assert trace["calls"] == []
    assert list(tmp_path.iterdir()) == []


def test_planner_questions_reach_claim_extractor_and_prevent_personnel_early_stop(tmp_path):
    game = {**GAME, "game_id": "espn:fixture-lal-gsw", "away_team": "Los Angeles Lakers",
        "home_team": "Golden State Warriors", "away_team_id": 1610612747, "home_team_id": 1610612744,
        "registered_game": True, "status": "scheduled", "game_type": "regular_season",
        "season_start": 2026, "scheduled_tipoff": "2026-10-20T23:00:00Z"}
    questions = [
        {"id": "q1", "priority": "core", "category": "game_story",
         "question": "Which coverage will decide the Lakers-Warriors matchup?"},
        {"id": "q2", "priority": "supporting", "category": "lineup",
         "question": "How is the current rotation expected to change?"},
    ]
    urls = [f"https://www.espn.com/nba/story/lakers-focus-{index:02d}" for index in range(4)] + [
        "https://www.espn.com/nba/story/lakers-focus-99"]
    records = [{"id": str(index), "category": "game_story", "url": url,
        "title": "Lakers Warriors preseason rotation and matchup update", "snippet": "current report"}
        for index, url in enumerate(urls)]
    bodies = {
        url: ("Los Angeles Lakers coach discussed the current training camp rotation at practice. "
              "The Golden State Warriors are preparing for the scheduled game.") for url in urls[:4]
    }
    bodies[urls[-1]] = ("Los Angeles Lakers coach explained how the Lakers pick-and-roll will be handled by switching. "
                        "The Golden State Warriors are preparing for this specific defensive coverage question.")
    extraction_payloads = []
    fetched_batches = []

    def client(stage, messages, tools, required):
        if stage == "search":
            return [{"name": "search_pregame_web", "args": {
                "categories": ["game_story", "lineup"], "reason": "coverage question",
                "analysis_questions": questions,
            }}]
        if stage == "fetch":
            supplied = json.loads(messages[-1]["content"])["sources"]
            return [{"name": "fetch_sources", "args": {"urls": [row["url"] for row in supplied[:4]], "reason": "source bodies"}}]
        if stage.startswith("claims_"):
            payload = json.loads(messages[-1]["content"])
            extraction_payloads.append(payload)
            assert payload["analysis_questions"] == questions
            claims = []
            for source in payload["sources"]:
                url = source["url"]
                body = source["body"]
                if "switching" in body:
                    quote = "Los Angeles Lakers coach explained how the Lakers pick-and-roll will be handled by switching."
                    tags = ["q1"]
                else:
                    quote = "Los Angeles Lakers coach discussed the current training camp rotation at practice."
                    tags = ["q2"]
                claims.append({"url": url, "quote": quote, "claim_type": "coach_comments",
                    "assertion_kind": "confirmed_fact", "team_id": game["away_team_id"],
                    "game_reference": "", "analysis_question_ids": tags})
            return [{"name": "submit_research_claims", "args": {"claims": claims}}]
        pytest.fail(stage)

    class Searcher:
        local_transport = SimpleNamespace(timeout_seconds=10)

        def search(self, _game, _cutoff, *, replay, categories, focus, analysis_questions=None):
            assert replay is False
            assert categories == {"game_story", "lineup", "availability"}
            assert analysis_questions == questions
            return {"game": game, "cutoff": "2026-10-20T18:55:00Z", "live_registered": True,
                "records": records}

        def fetch_bodies(self, requested, **_kwargs):
            fetched_batches.append(list(requested))
            return [{"url": url, "title": "Los Angeles Lakers Warriors preseason rotation and matchup update",
                "content": bodies[url], "published_date": "2026-10-20T15:00:00Z",
                "captured_at": "2026-10-20T18:00:00Z"} for url in requested]

    context, trace = AutonomousWebResearcher(searcher=Searcher(), client=client, root=tmp_path,
        max_fetch_urls=8, acquisition_budget_seconds=30).investigate(
            game, "2026-10-20T18:55:00Z", categories={"game_story", "lineup", "availability"})
    assert len(extraction_payloads) == 2
    assert len(fetched_batches) == 2 and len(fetched_batches[0]) == 4 and fetched_batches[1] == [urls[-1]]
    assert context["minimum_research"]["fact_readiness"]["status"] == "ready"
    assert context["minimum_research"]["analysis_readiness"]["status"] == "covered"
    assert trace["acquisition"]["stop_reason"] == "core_analysis_questions_covered"


def test_registered_planner_must_provide_a_core_analysis_question(tmp_path):
    game = {**GAME, "registered_game": True, "status": "scheduled"}
    def client(*_args):
        return [{"name": "search_pregame_web", "args": {
            "categories": ["game_story"], "reason": "fixture",
            "analysis_questions": [{"id": "q1", "priority": "supporting", "category": "game_story",
                "question": "A supporting-only question."}],
        }}]
    with pytest.raises(WebResearchAgentError, match="core_analysis_question_missing"):
        AutonomousWebResearcher(searcher=SimpleNamespace(), client=client, root=tmp_path).investigate(
            game, "2026-10-20T18:55:00Z", categories={"game_story"})
