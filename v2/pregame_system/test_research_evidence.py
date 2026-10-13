"""Offline contracts: no live tools or models are called here."""
import copy
import json
from types import SimpleNamespace

import pytest

from .research_evidence import ResearchEvidencePipeline, publication_bounds, rank_candidates
from .web_research_agent import AutonomousWebResearcher

GAME = {"game_id": "espn:401902644", "scheduled_tipoff": "2026-10-03T23:00:00Z",
        "away_team": "Miami Heat", "home_team": "Toronto Raptors", "away_team_id": 1610612748,
        "home_team_id": 1610612761, "status": "scheduled", "registered_game": True,
        "game_type": "preseason", "season_start": 2026}
CUTOFF = "2026-10-02T16:00:00Z"
URL = "https://www.nba.com/news/2026-27-season-preview-mia"
BACKGROUND = "Miami Heat acquired Giannis Antetokounmpo in a blockbuster trade this offseason."
LIVE = "Miami Heat lists Giannis Antetokounmpo as questionable for the game on October 3, 2026."
CURRENT = "Miami Heat coach discussed the current training camp rotation at practice."


def source(pipeline, *, url=URL, date="2026-09-22", body=BACKGROUND, category="lineup", captured="2026-10-02T16:00:01Z"):
    return pipeline.add_source({"url": url, "title": "Miami Heat 2026-27 preseason preview", "category": category,
        "discovered_via": "中文线索 → NBA 原文"}, {"url": url, "content": body + "\nAdditional source context about Miami Heat this season.",
        "published_date": date, "captured_at": captured})


def claim(*, url=URL, kind="personnel_change", quote=BACKGROUND, game_reference="", analysis_question_ids=None):
    return {"url": url, "quote": quote, "claim_type": kind, "assertion_kind": "confirmed_fact",
            "team_id": GAME["away_team_id"], "game_reference": game_reference,
            "analysis_question_ids": list(analysis_question_ids or [])}


def test_date_only_background_is_accepted_independent_of_discovery_category():
    pipeline = ResearchEvidencePipeline(GAME, CUTOFF)
    source(pipeline)
    assert pipeline.validate_claim(claim())["accepted"]
    fact = pipeline.claims[0]
    assert fact["publication_precision"] == "date"
    assert fact["temporal_confidence"] == "sufficient_for_background"
    assert fact["category"] == "roster_news"  # Was discovered under lineup.
    assert fact["snippet"] == BACKGROUND and "Additional" not in fact["snippet"]


def test_same_article_cannot_confirm_tonights_status_or_disguised_live_claim():
    pipeline = ResearchEvidencePipeline(GAME, CUTOFF)
    source(pipeline, body=BACKGROUND + "\n" + LIVE)
    assert pipeline.validate_claim(claim())["accepted"]
    assert not pipeline.validate_claim(claim(kind="availability", quote=LIVE, game_reference="October 3, 2026"))["accepted"]
    assert pipeline.validate_claim(claim(quote=LIVE))["reason"] == "high_volatility_claim_cannot_use_weaker_time_class"
    assert pipeline.summary()["coverage"]["availability"]["status"] == "UNKNOWN"


def test_precise_game_specific_injury_report_is_accepted():
    pipeline = ResearchEvidencePipeline(GAME, CUTOFF)
    source(pipeline, date="2026-10-02T15:00:00Z", body=LIVE)
    assert pipeline.validate_claim(claim(kind="availability", quote=LIVE, game_reference="October 3, 2026"))["accepted"]


@pytest.mark.parametrize("stage", ["EARLY_PREVIEW", "FINAL_PREGAME"])
def test_date_only_same_day_injury_is_unknown_not_invented(stage):
    pipeline = ResearchEvidencePipeline(GAME, CUTOFF, stage=stage)
    source(pipeline, date="2026-10-02", body=LIVE)
    assert not pipeline.validate_claim(claim(kind="availability", quote=LIVE, game_reference="October 3, 2026"))["accepted"]
    status = pipeline.summary()["coverage"]["availability"]
    assert status["status"] == "UNKNOWN" and status["unknown_reason"] and status["sources_checked"]


def test_current_date_only_is_usable_when_whole_possible_day_precedes_cutoff():
    pipeline = ResearchEvidencePipeline(GAME, CUTOFF)
    source(pipeline, date="2026-10-01", body=CURRENT)
    assert pipeline.validate_claim(claim(kind="coach_comments", quote=CURRENT))["accepted"]
    assert pipeline.claims[0]["temporal_confidence"] == "sufficient_for_current_date"


def test_final_has_stricter_live_freshness():
    results = []
    for stage in ("EARLY_PREVIEW", "FINAL_PREGAME"):
        p = ResearchEvidencePipeline(GAME, CUTOFF, stage=stage)
        source(p, date="2026-10-01T20:00:00Z", body=LIVE)
        results.append(p.validate_claim(claim(kind="availability", quote=LIVE, game_reference="October 3, 2026")))
    assert results[0]["accepted"] and results[1]["reason"] == "stale_live_claim"


def test_early_can_use_background_and_unknowns_but_final_needs_fresh_claim():
    for stage in ("EARLY_PREVIEW", "FINAL_PREGAME"):
        p = ResearchEvidencePipeline(GAME, CUTOFF, stage=stage)
        source(p)
        p.validate_claim(claim())
        assert p.summary()["sufficient"] is (stage == "EARLY_PREVIEW")
        assert p.summary()["coverage"]["confirmed_starter"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("date", ["2026-10-02T16:01:00Z", "2026-10-03"])
def test_cutoff_blocks_future_or_ambiguous_same_day_publication(date):
    p = ResearchEvidencePipeline(GAME, CUTOFF)
    source(p, date=date)
    assert p.validate_claim(claim())["reason"] == "publication_not_proven_before_cutoff"


def test_missing_or_previous_season_time_cannot_confirm_current_background():
    for date in (None, "2025-09-22"):
        p = ResearchEvidencePipeline(GAME, CUTOFF)
        source(p, date=date)
        assert not p.validate_claim(claim())["accepted"]


def test_quote_identity_and_forum_are_hard_boundaries():
    p = ResearchEvidencePipeline(GAME, CUTOFF)
    source(p)
    assert p.validate_claim(claim(quote="Miami Heat: Giannis will make his debut tonight."))["reason"] == "claim_quote_not_in_fetched_body"
    bad = claim(); bad["team_id"] = 1610612747
    assert p.validate_claim(bad)["reason"] == "claim_team_identity_mismatch"
    url = "https://bbs.hupu.com/fixture.html"
    source(p, url=url, body=LIVE, date="2026-10-02T15:00:00Z")
    assert p.validate_claim(claim(url=url, kind="availability", quote=LIVE, game_reference="October 3, 2026"))["reason"] == "original_or_independent_verification_unavailable"


def test_current_official_roster_snapshot_is_not_game_availability():
    p = ResearchEvidencePipeline(GAME, CUTOFF)
    url = "https://www.nba.com/heat/roster"
    quote = "Miami Heat 2026-27 roster includes Giannis Antetokounmpo and Bam Adebayo."
    source(p, url=url, body=quote, date=None)
    assert p.validate_claim(claim(url=url, kind="current_roster", quote=quote))["accepted"]
    assert not p.validate_claim(claim(url=url, kind="availability", quote=quote, game_reference="October 3, 2026"))["accepted"]


def test_publication_bounds_dont_invent_midnight_timezone():
    value = publication_bounds("2026-09-22")
    assert value["earliest"].isoformat().startswith("2026-09-21T10")
    assert value["latest"].isoformat().startswith("2026-09-23T12")


def test_attempt2_nba_markdown_excerpt_supports_background_not_debut():
    p = ResearchEvidencePipeline(GAME, CUTOFF)
    body = ("The Heat wanted Antetokounmpo, and they got him in a [blockbuster trade]"
            "(https://www.nba.com/news/reports-heat-acquire-giannis-antetokounmpo-in-blockbuster-deal) with the Milwaukee Bucks.")
    # Exact source excerpt captured in Attempt 2; date-only metadata. The test
    # does not invoke any network or overwrite the earlier real receipt.
    quote = "The Heat wanted Antetokounmpo, and they got him in a blockbuster trade with the Milwaukee Bucks."
    source(p, body=body)
    assert p.validate_claim(claim(quote=quote))["accepted"]
    assert p.validate_claim(claim(quote="Giannis will make his Miami Heat debut on October 3, 2026."))["reason"] == "claim_quote_not_in_fetched_body"


def test_fetch_failure_recovers_deduplicates_and_keeps_hypotheses_out(tmp_path):
    bad = "https://www.espn.com/nba/game/_/gameId/401902644/heat-raptors"
    sources = [{"id": str(i), "category": c, "url": u, "title": "Miami Heat preseason preview", "snippet": "unverified lead"}
        for i, (c, u) in enumerate([("availability", bad), ("lineup", bad), ("roster_news", URL)])]
    fetches = []
    def fetch_many(urls):
        fetches.extend(urls)
        return [{"url": u, "content": "JavaScript is disabled" if u == bad else BACKGROUND + " " * 2 + "Miami Heat preseason context." * 5,
                 "published_date": "2026-09-22", "captured_at": "2026-10-02T16:00:01Z"} for u in urls]
    def client(stage, messages, tools, required):
        if stage == "search":
            return [{"name": "search_pregame_web", "args": {"categories": ["availability", "roster_news"],
                "reason": "verify whether Giannis makes his Heat debut", "focus_category": "availability", "focus": "Giannis debut hypothesis",
                "analysis_questions": [{"id":"q1","priority":"core","category":"lineup",
                    "question":"Can the current Heat frontcourt function together?"}]}}]
        if stage == "fetch": return [{"name": "fetch_sources", "args": {"urls": [bad], "reason": "verify status"}}]
        if stage.startswith("claims_"):
            assert "unverified lead" not in messages[-1]["content"]
            return [{"name": "submit_research_claims", "args": {"claims": [claim()]}}]
        pytest.fail(stage)
    searcher = SimpleNamespace(search=lambda *a, **k: {"game": GAME, "live_registered": True, "records": copy.deepcopy(sources), "cutoff": CUTOFF},
                               local_transport=SimpleNamespace(fetch_many=fetch_many))
    context, trace = AutonomousWebResearcher(searcher=searcher, client=client, root=tmp_path, max_fetch_urls=1).investigate(
        GAME, CUTOFF, categories={"availability", "lineup", "roster_news"})
    # With one URL budget the first failure is explicit; no LLM hypothesis becomes fact.
    assert len(fetches) == 1 and not context["records"]
    fetches.clear()
    context, trace = AutonomousWebResearcher(searcher=searcher, client=client, root=tmp_path, max_fetch_urls=2).investigate(
        GAME, CUTOFF, categories={"availability", "lineup", "roster_news"})
    assert fetches.count(bad) == 1 and fetches.count(URL) == 1
    assert context["minimum_research"]["sufficient"]
    assert all("debut" not in r["snippet"] for r in context["records"])
    assert context["planner_hypotheses"][0]["status"] == "unresolved"
    assert len(context["source_records"]) == 2 and len(context["records"]) == 1


def test_recovery_uses_later_batch_after_first_four_urls_fail(tmp_path):
    bad = [f"https://www.espn.com/nba/story/{i}-heat-update" for i in range(4)]
    records = [{"url": u, "category": "availability", "title": "Miami Heat update", "id": str(i)} for i, u in enumerate(bad)]
    records.append({"url": URL, "category": "lineup", "title": "Miami Heat preview", "id": "good"})
    batches = []
    def fetch(urls):
        batches.append(urls)
        return [{"url": u, "content": BACKGROUND + " Miami Heat context." * 7 if u == URL else "", "published_date": "2026-09-22"} for u in urls]
    def client(stage, *args):
        if stage == "search": return [{"name": "search_pregame_web", "args": {"categories": ["availability"], "reason": "check",
            "analysis_questions": [{"id":"q1","priority":"core","category":"availability",
                "question":"Which current rotation variable may change the game?"}]}}]
        if stage == "fetch": return [{"name": "fetch_sources", "args": {"urls": bad, "reason": "check"}}]
        return [{"name": "submit_research_claims", "args": {"claims": [claim()]}}]
    searcher = SimpleNamespace(search=lambda *a, **k: {"game": GAME, "live_registered": True, "records": records},
        local_transport=SimpleNamespace(fetch_many=fetch))
    context, trace = AutonomousWebResearcher(searcher=searcher, client=client, root=tmp_path).investigate(GAME, CUTOFF, categories={"availability", "roster_news", "lineup"})
    assert len(batches) == 2 and batches[1] == [URL]
    assert trace["acquisition"]["fallback_batches"] == 1
    assert context["minimum_research"]["sufficient"]


def test_question_tags_do_not_bypass_temporal_qualification_or_change_fact_readiness():
    questions = [{"id": "q1", "priority": "core", "category": "availability",
                  "question": "Which core player is likely to play?"}]
    p = ResearchEvidencePipeline(GAME, CUTOFF, analysis_questions=questions)
    source(p, date="2026-10-02T16:01:00Z", body=LIVE)
    decision = p.validate_claim(claim(kind="availability", quote=LIVE,
        game_reference="October 3, 2026", analysis_question_ids=["q1"]))
    summary = p.summary()
    assert decision["accepted"] is False
    assert summary["fact_readiness"]["status"] == "unavailable"
    assert summary["analysis_readiness"]["status"] == "uncovered"
    assert summary["analysis_readiness"]["uncovered_core_question_ids"] == ["q1"]


def test_core_question_is_covered_only_by_an_accepted_tagged_claim():
    questions = [{"id": "q1", "priority": "core", "category": "game_story",
                  "question": "What defensive coverage will shape this matchup?"}]
    p = ResearchEvidencePipeline(GAME, CUTOFF, analysis_questions=questions)
    source(p, date="2026-10-02T15:00:00Z", body=CURRENT)
    assert p.validate_claim(claim(kind="coach_comments", quote=CURRENT,
        analysis_question_ids=["q1"]))["accepted"]
    summary = p.summary()
    assert summary["fact_readiness"]["status"] == "ready"
    assert summary["analysis_readiness"]["status"] == "covered"
    assert summary["analysis_readiness"]["questions"][0]["evidence_ids"]


def test_candidate_ranking_is_deduplicated_not_fixed_publisher_order():
    same = [{"url": URL, "title": "Miami Heat preview", "category": c} for c in ("lineup", "game_story")]
    ranked = rank_candidates(same, GAME)
    assert len(ranked) == 1 and set(ranked[0]["discovery_categories"]) == {"lineup", "game_story"}
    assert rank_candidates(same, GAME, attempted=[URL]) == []
