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
