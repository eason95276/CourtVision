from __future__ import annotations

import pytest

from .free_search_mcp_client import FreeSearchMCPError
from .web_search import PregameWebSearch, parse_public_market_reference


GAME = {
    "game_id": "espn:fixture",
    "away_team": "Philadelphia 76ers",
    "home_team": "New York Knicks",
    "away_team_id": 1610612755,
    "home_team_id": 1610612752,
    "scheduled_tipoff": "2026-10-20T23:00:00Z",
}


def test_replay_rejects_untrusted_missing_date_and_after_cutoff(tmp_path):
    def transport(payload):
        assert payload["end_date"] == "2026-10-20"
        return {
            "results": [
                {
                    "title": "Official pregame report",
                    "url": "https://www.nba.com/news/report",
                    "published_date": "2026-10-20T12:00:00Z",
                    "captured_at": "2026-10-20T18:40:00Z",
                    "content": "Philadelphia 76ers vs New York Knicks pregame only.",
                },
                {
                    "title": "Future result",
                    "url": "https://www.espn.com/nba/story/future",
                    "published_date": "2026-10-21T12:00:00Z",
                    "captured_at": "2026-10-21T12:00:00Z",
                },
                {
                    "title": "Undated",
                    "url": "https://www.nba.com/news/undated",
                    "captured_at": "2026-10-20T18:40:00Z",
                },
                {
                    "title": "Unknown blog",
                    "url": "https://example.com/post",
                    "published_date": "2026-10-20T10:00:00Z",
                    "captured_at": "2026-10-20T18:40:00Z",
                },
            ]
        }

    result = PregameWebSearch(tmp_path, replay_transport=transport).search(
        GAME,
        "2026-10-20T18:55:00Z",
        replay=True,
        categories={"availability"},
    )
    assert result["status"] == "available"
    assert len(result["records"]) == 1
    assert result["records"][0]["domain"] == "nba.com"
    assert result["records"][0]["eligible_for_fact"] is True
    assert result["records"][0]["captured_at"] == "2026-10-20T18:40:00+00:00"
    assert result["captured_at"] is None
    assert result["search_run_at"]
    assert "historical archive transport only" in result["policy"]


def test_replay_never_uses_current_search_results(tmp_path):
    def current_search(_payload):
        pytest.fail("historical replay called a current search transport")

    result = PregameWebSearch(tmp_path, transport=current_search).search(
        GAME,
        "2026-10-20T18:55:00Z",
        replay=True,
        categories={"availability"},
    )
    assert result["status"] == "unavailable"
    assert result["records"] == []
    assert result["trace"][0]["attempts"][0]["error"] == "historical_source_archive_unavailable"


def test_replay_requires_precise_source_time_and_capture_before_cutoff(tmp_path):
    def archive(_payload):
        return {
            "results": [
                {
                    "title": "Date only",
                    "url": "https://www.nba.com/news/date-only",
                    "published_date": "2026-10-20",
                    "captured_at": "2026-10-20T18:40:00Z",
                    "content": "Philadelphia 76ers vs New York Knicks pregame update.",
                },
                {
                    "title": "Captured too late",
                    "url": "https://www.nba.com/news/captured-too-late",
                    "published_date": "2026-10-20T17:00:00Z",
                    "captured_at": "2026-10-20T19:01:00Z",
                    "content": "Philadelphia 76ers vs New York Knicks pregame update.",
                },
            ]
        }

    result = PregameWebSearch(tmp_path, replay_transport=archive).search(
        GAME,
        "2026-10-20T18:55:00Z",
        replay=True,
        categories={"availability"},
    )
    assert result["status"] == "unavailable"
    assert result["records"] == []


def test_queries_are_limited_to_pregame_context(tmp_path):
    seen = []

    def transport(payload):
        seen.append(payload["query"])
        return {"results": []}

    result = PregameWebSearch(tmp_path, transport=transport).search(
        GAME,
        "2026-10-20T18:55:00Z",
    )
    assert result["status"] == "unavailable"
    assert len(seen) == 9
    combined = " ".join(seen).casefold()
    assert "injury" in combined
    assert "starting lineup" in combined
    assert "roster" in combined
    assert "champion" in combined
    assert "odds spread total" in combined
    assert "result" not in combined


def test_focus_adds_only_one_game_bound_query(tmp_path):
    queries = PregameWebSearch(tmp_path, transport=lambda _: {"results": []}).queries(
        GAME,
        {"roster_news"},
        ("roster_news", "LeBron James signing official"),
    )
    assert len(queries) == 2
    assert queries[-1][0] == "roster_news"
    assert "Philadelphia 76ers" in queries[-1][1]
    assert "New York Knicks" in queries[-1][1]
    assert "LeBron James signing official" in queries[-1][1]


def test_naive_published_time_is_treated_as_utc(tmp_path):
    def transport(payload):
        return {
            "results": [
                {
                    "title": "Report",
                    "url": "https://nba.com/news/report",
                    "published_date": "2026-10-20T18:00:00",
                    "content": "Philadelphia 76ers vs New York Knicks report.",
                }
            ]
        }

    result = PregameWebSearch(tmp_path, transport=transport).search(
        GAME,
        "2026-10-20T18:55:00Z",
        categories={"availability"},
    )
    assert result["records"][0]["published_at"].endswith("+00:00")


def test_missing_local_search_and_tavily_degrades_without_network(monkeypatch, tmp_path):
    class Unavailable:
        def search_many(self, payloads):
            raise FreeSearchMCPError("not installed")

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = PregameWebSearch(tmp_path, local_transport=Unavailable()).search(
        GAME,
        "2026-10-20T18:55:00Z",
        categories={"availability"},
    )
    assert result["status"] == "unavailable"
    assert result["trace"][0]["attempts"][0]["provider"] == "free-search-mcp"
    assert result["trace"][0]["attempts"][0]["error"] == "FreeSearchMCPError"


def test_local_free_search_supplies_official_game_story_without_key(monkeypatch, tmp_path):
    class LocalSearch:
        def search_many(self, payloads):
            output = []
            for payload in payloads:
                if "champion" in payload["query"].casefold():
                    output.append({
                        "results": [{
                            "title": "2026 NBA Finals",
                            "url": "https://www.nba.com/playoffs/2026/nba-finals",
                            "content": "New York Knicks win the 2026 NBA championship.",
                            "score": 0.9,
                        }]
                    })
                else:
                    output.append({"results": []})
            return output

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = PregameWebSearch(tmp_path, local_transport=LocalSearch()).search(
        GAME,
        "2026-10-20T18:55:00Z",
        categories={"game_story"},
    )
    assert result["status"] == "available"
    assert result["records"][0]["category"] == "game_story"
    assert result["records"][0]["search_provider"] == "free-search-mcp"
    assert result["records"][0]["domain"] == "nba.com"


def test_empty_local_result_falls_back_to_tavily_when_configured(monkeypatch, tmp_path):
    class EmptyLocal:
        def search_many(self, payloads):
            return [{"results": []} for _ in payloads]

    monkeypatch.setenv("TAVILY_API_KEY", "fixture")
    search = PregameWebSearch(tmp_path, local_transport=EmptyLocal())
    monkeypatch.setattr(
        search,
        "_tavily_transport",
        lambda payload: {
            "results": [{
                "title": "Knicks championship recap",
                "url": "https://www.nba.com/news/knicks-championship-recap",
                "published_date": "2026-06-15T12:00:00Z",
                "content": "New York Knicks won the 2026 championship.",
            }]
        },
    )
    result = search.search(
        GAME,
        "2026-10-20T18:55:00Z",
        categories={"game_story"},
    )
    assert result["status"] == "available"
    assert all(record["search_provider"] == "tavily" for record in result["records"])
    assert result["trace"][0]["attempts"] == [
        {"provider": "free-search-mcp", "accepted": 0, "rejected": 0},
        {"provider": "tavily", "accepted": 1, "rejected": 0},
    ]


def test_dated_but_irrelevant_team_content_is_not_a_game_story(tmp_path):
    def transport(payload):
        return {
            "results": [{
                "title": "76ers unveil alternate uniform",
                "url": "https://www.nba.com/sixers/news/alternate-uniform",
                "published_date": "2026-08-01T12:00:00Z",
                "content": "Philadelphia 76ers announce a uniform design.",
            }]
        }

    result = PregameWebSearch(tmp_path, transport=transport).search(
        GAME,
        "2026-10-20T18:55:00Z",
        categories={"game_story"},
    )
    assert result["status"] == "unavailable"
    assert all(item["accepted"] == 0 for item in result["trace"])


def test_generic_playoffs_page_with_both_teams_in_navigation_is_not_matchup_evidence(tmp_path):
    result = {
        "title": "2026 NBA Playoffs",
        "url": "https://www.nba.com/playoffs/2026",
        "content": "The playoff bracket lists Philadelphia 76ers and New York Knicks among many teams.",
    }
    assert PregameWebSearch._story_is_relevant(result, GAME) is False


def test_highlight_video_without_a_written_recap_is_not_tactical_evidence():
    result = {
        "title": "Philadelphia 76ers vs New York Knicks Semifinals Highlights",
        "url": "https://www.nba.com/watch/video/phi-vs-nyk-highlights",
        "content": "76ers vs Knicks, 2026 playoff semifinals.",
    }
    assert PregameWebSearch._story_is_relevant(result, GAME) is False


def test_dated_official_signing_is_roster_news_and_not_current_without_cutoff(tmp_path):
    def transport(payload):
        return {"results": [{
            "title": "PHILADELPHIA 76ERS SIGN LEBRON JAMES",
            "url": "https://www.nba.com/sixers/news/philadelphia-76ers-sign-lebron-james",
            "content": "PHILADELPHIA — July 27, 2026 — The Philadelphia 76ers announced they signed LeBron James.",
        }]}

    before = PregameWebSearch(tmp_path / "before", transport=transport).search(
        GAME, "2026-07-27T23:00:00Z", categories={"game_story"}
    )
    after = PregameWebSearch(tmp_path / "after", transport=transport).search(
        GAME, "2026-10-20T18:55:00Z", categories={"game_story"}
    )
    assert before["status"] == "unavailable"
    assert after["records"][0]["category"] == "roster_news"
    assert after["records"][0]["published_at"] == "2026-07-27T00:00:00+00:00"
    assert after["records"][0]["publication_time_semantics"] == "date_only"


def test_real_known_public_market_sample_preserves_home_spread_direction(tmp_path):
    def transport(payload):
        assert payload["topic"] == "general"
        assert "betmgm.com" in payload["include_domains"]
        return {
            "results": [
                {
                    "title": "76ers vs. Knicks Odds Tonight - October 20, 2026",
                    "url": "https://sports.betmgm.com/en/blog/team/philadelphia-76ers/",
                    "content": (
                        "Philadelphia 76ers +5.5 (-110) O 231.5 (-110) +150 "
                        "New York Knicks -5.5 (-110) U 231.5 (-110) -185"
                    ),
                    "score": 0.94,
                }
            ]
        }

    result = PregameWebSearch(tmp_path, transport=transport).search(
        GAME,
        "2026-10-20T18:55:00Z",
        categories={"market"},
    )
    assert result["status"] == "available"
    record = result["records"][0]
    market = record["market_reference"]
    assert market["away_spread"] == 5.5
    assert market["home_spread"] == -5.5
    assert market["expected_home_margin"] == 5.5
    assert market["total"] == 231.5
    assert market["eligible_prediction_input"] is False
    assert market["usage"] == "display_only_public_market_reference"
    assert market["published_at"] is None
    assert market["captured_at"]


def test_fanduel_opening_day_sample_does_not_bind_title_to_wrong_team():
    result = {
        "title": "NBA Opening Day Schedule, Odds and Matchups for 2026-27",
        "url": "https://fanduel.com/research/nba-opening-day-schedule-odds-and-matchups-for-2026-27",
        "content": (
            "Philadelphia 76ers at New York Knicks, October 20, 2026. "
            "Philadelphia 76ers +5.5. New York Knicks -5.5. "
            "Total Points O 233.5 and U 233.5."
        ),
    }
    market = parse_public_market_reference(
        result,
        GAME,
        captured_at="2026-09-19T00:00:00+00:00",
    )
    assert market is not None
    assert market["away_spread"] == 5.5
    assert market["home_spread"] == -5.5
    assert market["expected_home_margin"] == 5.5
    assert market["total"] == 233.5
    assert market["eligible_prediction_input"] is False


def test_public_market_parser_rejects_wrong_date_and_unpaired_line():
    wrong_date = {
        "title": "76ers at Knicks odds October 19, 2026",
        "url": "https://www.foxsports.com/nba/game?tab=odds",
        "content": "Philadelphia 76ers +5.5 New York Knicks -5.5 Total 231.5",
    }
    assert parse_public_market_reference(
        wrong_date, GAME, captured_at="2026-09-18T00:00:00Z"
    ) is None
    unpaired = {
        "title": "76ers at Knicks odds October 20, 2026",
        "url": "https://www.foxsports.com/nba/game?tab=odds",
        "content": "Philadelphia 76ers +5.5 New York Knicks -192",
    }
    assert parse_public_market_reference(
        unpaired, GAME, captured_at="2026-09-18T00:00:00Z"
    ) is None


def test_generic_multi_game_board_cannot_bind_previous_game_total():
    result = {
        "title": "NBA Odds",
        "url": "https://sportsbook.fanduel.com/navigation/nba",
        "content": (
            "Celtics +1.5 Detroit Pistons -1.5 Total O 221.5. "
            "Philadelphia 76ers New York Knicks -5.5 Total U 233.5. "
            "October 20, 2026."
        ),
    }
    assert parse_public_market_reference(
        result,
        GAME,
        captured_at="2026-09-18T00:00:00Z",
    ) is None


def test_tier_one_bookmaker_accepts_yearless_upcoming_date_with_paired_lines():
    game = {
        "game_id": "espn:bucks-wizards",
        "away_team": "Milwaukee Bucks",
        "home_team": "Washington Wizards",
        "away_team_id": 1610612749,
        "home_team_id": 1610612764,
        "scheduled_tipoff": "2026-10-21T23:00:00Z",
    }
    result = {
        "title": "Milwaukee Bucks Odds, Game Previews, & Lines | BetMGM",
        "url": "https://sports.betmgm.com/en/blog/team/milwaukee-bucks",
        "content": (
            "Oct 21 - 6 pm EST Bucks vs. Wizards Spread Total Money "
            "Bucks +6.5 (-110) O 238.5 (-110) +180 "
            "Wizards -6.5 (-110) U 238.5 (-110) -220"
        ),
    }
    market = parse_public_market_reference(
        result,
        game,
        captured_at="2026-09-18T16:30:00+00:00",
    )
    assert market is not None
    assert market["away_spread"] == 6.5
    assert market["home_spread"] == -6.5
    assert market["total"] == 238.5


def test_yearless_date_is_rejected_for_media_and_after_tipoff():
    result = {
        "title": "76ers at Knicks odds Oct 20",
        "url": "https://www.foxsports.com/nba/game?tab=odds",
        "content": "Philadelphia 76ers +5.5 New York Knicks -5.5 Total 231.5",
    }
    assert parse_public_market_reference(
        result, GAME, captured_at="2026-09-18T00:00:00Z"
    ) is None
    result["url"] = "https://sports.betmgm.com/en/blog/team/philadelphia-76ers"
    assert parse_public_market_reference(
        result, GAME, captured_at="2026-10-21T00:00:00Z"
    ) is None
