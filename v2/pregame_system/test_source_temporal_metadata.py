"""Publication/update/observation contracts; all fixtures are offline."""
import json
import pytest
from .source_temporal_metadata import extract_temporal_metadata,normalize_time
from .research_evidence import ResearchEvidencePipeline
from .test_research_evidence import GAME,CUTOFF,source,claim,BACKGROUND,LIVE,CURRENT,URL


def test_jsonld_newsarticle_times_do_not_require_article_body():
    html='<script type="application/ld+json">'+json.dumps({"@type":"NewsArticle","url":URL,
        "datePublished":"2026-09-22T10:45:00Z","dateModified":"2026-10-02T15:00:00Z"})+'</script>'
    temporal=extract_temporal_metadata(URL,html=html,fetched_at="2026-10-02T16:00:01Z")
    assert temporal["published_at"]=="2026-09-22T10:45:00+00:00"
    assert temporal["updated_at"]=="2026-10-02T15:00:00+00:00"
    assert temporal["observed_at"] is None
    assert temporal["published_at_origin"]=="jsonld.article.datePublished"


def test_priority_and_related_article_identity():
    html='<meta property="article:published_time" content="2026-09-22T09:00:00Z"><script type="application/ld+json">'+json.dumps([
        {"@type":"NewsArticle","url":URL,"datePublished":"2026-09-22T10:00:00Z"},
        {"@type":"NewsArticle","url":URL+"/related","datePublished":"2026-10-03T12:00:00Z"}])+'</script>'
    value=extract_temporal_metadata(URL,provider={"published_date":"2026-09-22T08:00:00Z"},html=html)
    assert value["published_at"].startswith("2026-09-22T08")
    assert not any(c["value"].startswith("2026-10-03")for c in value["temporal_candidates"])


def test_date_only_is_preserved_and_untyped_timestamp_is_not_publication():
    t=extract_temporal_metadata(URL,provider={"published_date":"2026-09-22","timestamp":"2026-10-03T12:00:00Z"})
    assert t["published_at"]=="2026-09-22" and t["published_at_precision"]=="date"
    assert normalize_time("2026-09-22")[0]=="2026-09-22"


def test_html_modified_and_time_datetime_fields():
    t=extract_temporal_metadata(URL,html='<meta property="article:modified_time" content="2026-10-02T15:00:00Z"><time datetime="2026-09-22">Published</time>')
    assert t["published_at"]=="2026-09-22"
    assert t["updated_at"].endswith("+00:00")


def test_yahoo_and_espn_byline_times_are_real_source_times():
    yahoo=extract_temporal_metadata("https://sports.yahoo.com/articles/fixture.html",
        body="Story by Reporter\nSat, October 3, 2026 at 11:00 AM UTC\nArticle text")
    assert yahoo["published_at"]=="2026-10-03T11:00:00+00:00"
    espn=extract_temporal_metadata("https://www.espn.com/nba/story/_/id/1",
        body="Headline\nSep 17, 2026, 06:45 AM ET\nArticle text")
    assert espn["published_at"]=="2026-09-17T10:45:00+00:00"
    date=extract_temporal_metadata("https://sports.yahoo.com/articles/fixture.html",
        body="Wed, Sep 9, 2026·\nArticle")
    assert date["published_at"]=="2026-09-09"


def test_game_date_is_not_news_publication_and_snapshot_observation_is_explicit():
    t=extract_temporal_metadata("https://www.nba.com/game/mia-vs-tor-0012600009",
        provider={"published_date":"2026-10-03"},title="Miami Heat vs Toronto Raptors",
        fetched_at="2026-10-03T17:09:00Z")
    assert t["published_at"] is None and t["observed_at"]=="2026-10-03T17:09:00Z"


def test_the_athletic_nyt_date_path_is_recognized_as_an_article_date():
    url="https://www.nytimes.com/athletic/7655983/2026/10/05/lebron-james-philadelphia-76ers-roster-talent-embiid-experiment"
    value=extract_temporal_metadata(url,title="LeBron James and the 76ers have talent")
    assert value["page_kind"]=="article"
    assert value["published_at"]=="2026-10-05"
    assert value["published_at_precision"]=="date"
    assert value["published_at_origin"]=="source_url.date"


def test_yahoo_matchup_date_in_title_is_not_misrepresented_as_publication_date():
    title="Preseason Game Preview: Knicks at 76ers, October 5, 2026"
    value=extract_temporal_metadata("https://sports.yahoo.com/articles/preseason-game-preview-knicks-76ers-064925576.html",
        title=title,fetched_at="2026-10-05T22:42:00Z")
    assert value["page_kind"]=="article"
    assert value["published_at"] is None
    assert value["observed_at"] is None


def test_exact_game_preview_without_publish_time_is_retained_as_captured_context():
    game={"game_id":"espn:401914101","scheduled_tipoff":"2026-10-05T23:00:00Z",
        "away_team":"New York Knicks","home_team":"Philadelphia 76ers",
        "away_team_id":1610612752,"home_team_id":1610612755,"game_type":"preseason","season_start":2026}
    title="Preseason Game Preview: Knicks at 76ers, October 5, 2026"
    url="https://sports.yahoo.com/articles/preseason-game-preview-knicks-76ers-064925576.html"
    quote="The New York Knicks open their preseason schedule tonight against the Philadelphia 76ers."
    p=ResearchEvidencePipeline(game,"2026-10-05T22:40:00Z",stage="FINAL_PREGAME")
    p.add_source({"url":url,"title":title},{"url":url,"title":title,
        "content":quote+" This preview discusses the teams' exhibition matchup and rotation context."*3,
        "captured_at":"2026-10-05T22:42:00Z"})
    value=p.validate_claim({"url":url,"quote":quote,"claim_type":"game_news",
        "assertion_kind":"confirmed_fact","team_id":game["away_team_id"]})
    assert value["accepted"]
    assert value["temporal_basis"]=="captured_game_context"
    assert value["temporal_confidence"]=="game_specific_pregame_capture"
    assert p.claims[0]["published_at"] is None
    assert p.claims[0]["fetched_at"]=="2026-10-05T22:42:00Z"


def test_exact_athletic_matchup_story_uses_context_without_inventing_exact_time():
    game={"game_id":"espn:401914101","scheduled_tipoff":"2026-10-05T23:00:00Z",
        "away_team":"New York Knicks","home_team":"Philadelphia 76ers",
        "away_team_id":1610612752,"home_team_id":1610612755,"game_type":"preseason","season_start":2026}
    url="https://www.nytimes.com/athletic/7655983/2026/10/05/lebron-james-philadelphia-76ers-roster-talent-embiid-experiment"
    title="LeBron James and the 76ers have talent, but how will this experiment work?"
    quote="As the Sixers begin their preseason schedule Monday night at home against the New York Knicks, the curtain comes up for the first time on a grand experiment."
    p=ResearchEvidencePipeline(game,"2026-10-05T22:40:00Z",stage="FINAL_PREGAME")
    p.add_source({"url":url,"title":title},{"url":url,"title":title,
        "content":quote+" The preseason preview also discusses the new Philadelphia starting group and rotation."*3,
        "captured_at":"2026-10-05T22:42:00Z"})
    value=p.validate_claim({"url":url,"quote":quote,"claim_type":"game_news",
        "assertion_kind":"confirmed_fact","team_id":game["home_team_id"]})
    assert value["accepted"]
    assert p.sources[url]["published_at"]=="2026-10-05"
    assert p.sources[url]["publication_precision"]=="date"
    assert p.claims[0]["temporal_basis"]=="captured_game_context"


def test_game_context_capture_does_not_relax_live_status_or_unmatched_articles():
    game={"game_id":"espn:401914101","scheduled_tipoff":"2026-10-05T23:00:00Z",
        "away_team":"New York Knicks","home_team":"Philadelphia 76ers",
        "away_team_id":1610612752,"home_team_id":1610612755,"game_type":"preseason","season_start":2026}
    url="https://sports.yahoo.com/articles/preseason-game-preview-knicks-76ers-064925576.html"
    title="Preseason Game Preview: Knicks at 76ers, October 5, 2026"
    game_quote="The New York Knicks open their preseason schedule tonight against the Philadelphia 76ers."
    live_quote="Joel Embiid is questionable for tonight's game against the New York Knicks."
    body=game_quote+" "+live_quote+" The teams meet in a preseason game."*4
    p=ResearchEvidencePipeline(game,"2026-10-05T22:40:00Z",stage="FINAL_PREGAME")
    p.add_source({"url":url,"title":title},{"url":url,"title":title,"content":body,
        "captured_at":"2026-10-05T22:42:00Z"})
    live=p.validate_claim({"url":url,"quote":live_quote,"claim_type":"availability",
        "assertion_kind":"confirmed_fact","team_id":game["home_team_id"],"game_reference":"October 5, 2026"})
    assert live["reason"]=="live_precise_zoned_publication_required"

    unrelated_url="https://sports.yahoo.com/articles/training-camp-story.html"
    unrelated_title="Knicks preseason rotation outlook"
    p.add_source({"url":unrelated_url,"title":unrelated_title},{"url":unrelated_url,"title":unrelated_title,
        "content":"The Philadelphia 76ers and New York Knicks discussed training camp rotation plans. "*4,
        "captured_at":"2026-10-05T22:42:00Z"})
    unrelated=p.validate_claim({"url":unrelated_url,
        "quote":"The Philadelphia 76ers and New York Knicks discussed training camp rotation plans.",
        "claim_type":"rotation_update","assertion_kind":"reported_projection","team_id":game["home_team_id"]})
    assert unrelated["reason"]=="current_publication_time_unavailable"


def test_game_specific_reported_workload_projection_is_not_mislabeled_confirmed_live_restriction():
    game={"game_id":"espn:401914101","scheduled_tipoff":"2026-10-05T23:00:00Z",
        "away_team":"New York Knicks","home_team":"Philadelphia 76ers",
        "away_team_id":1610612752,"home_team_id":1610612755,"game_type":"preseason","season_start":2026}
    url="https://sports.yahoo.com/articles/preseason-game-preview-knicks-76ers-064925576.html"
    title="Preseason Game Preview: Knicks at 76ers, October 5, 2026"
    quote=("The second-year coach has already stressed that he intends to ease his core into the exhibition slate. "
        "Jalen Brunson, Mikal Bridges, Josh Hart, OG Anunoby, and Karl-Anthony Towns will not shoulder heavy workloads right away.")
    p=ResearchEvidencePipeline(game,"2026-10-05T22:40:00Z",stage="FINAL_PREGAME")
    p.add_source({"url":url,"title":title},{"url":url,"title":title,
        "content":quote+" The Knicks open their preseason schedule tonight against the Philadelphia 76ers."*3,
        "captured_at":"2026-10-05T22:42:00Z"})
    projection=p.validate_claim({"url":url,"quote":quote,"claim_type":"minutes_restriction",
        "assertion_kind":"reported_projection","team_id":game["away_team_id"]})
    assert projection["accepted"]
    assert projection["temporal_class"]=="CURRENT"
    assert projection["temporal_basis"]=="captured_game_context"
    assert p.claims[0]["assertion_kind"]=="reported_projection"

    confirmed=p.validate_claim({"url":url,"quote":quote,"claim_type":"minutes_restriction",
        "assertion_kind":"confirmed_fact","team_id":game["away_team_id"],"game_reference":"October 5, 2026"})
    assert confirmed["reason"]=="live_precise_zoned_publication_required"


def test_game_context_capture_after_tipoff_is_not_pregame_evidence():
    game={"game_id":"espn:401914101","scheduled_tipoff":"2026-10-05T23:00:00Z",
        "away_team":"New York Knicks","home_team":"Philadelphia 76ers",
        "away_team_id":1610612752,"home_team_id":1610612755,"game_type":"preseason","season_start":2026}
    url="https://sports.yahoo.com/articles/preseason-game-preview-knicks-76ers-064925576.html"
    title="Preseason Game Preview: Knicks at 76ers, October 5, 2026"
    quote="The New York Knicks open their preseason schedule tonight against the Philadelphia 76ers."
    p=ResearchEvidencePipeline(game,"2026-10-06T00:10:00Z",stage="FINAL_PREGAME")
    p.add_source({"url":url,"title":title},{"url":url,"title":title,"content":quote+" Preview details."*10,
        "captured_at":"2026-10-05T23:05:00Z"})
    value=p.validate_claim({"url":url,"quote":quote,"claim_type":"game_news",
        "assertion_kind":"confirmed_fact","team_id":game["away_team_id"]})
    assert value["reason"]=="current_publication_time_unavailable"


def test_updated_source_can_support_current_and_live_without_changing_freshness():
    p=ResearchEvidencePipeline(GAME,CUTOFF)
    fetched={"url":URL,"title":"Miami Heat","content":LIVE+"\n"+CURRENT+" Miami Heat source context."*5,
        "published_date":"2026-09-22","updated_at":"2026-10-02T15:00:00Z","captured_at":"2026-10-02T16:00:01Z"}
    p.add_source({"url":URL,"title":"Miami Heat"},fetched)
    decision=p.validate_claim(claim(kind="availability",quote=LIVE,game_reference="October 3, 2026"))
    assert decision["accepted"] and decision["temporal_basis"]=="updated_at"
    assert p.validate_claim(claim(kind="coach_comments",quote=CURRENT))["accepted"]
    stale=ResearchEvidencePipeline(GAME,CUTOFF,stage="FINAL_PREGAME")
    fetched["updated_at"]="2026-10-01T20:00:00Z"
    stale.add_source({"url":URL},fetched)
    assert stale.validate_claim(claim(kind="availability",quote=LIVE,game_reference="October 3, 2026"))["reason"]=="stale_live_claim"


def test_current_roster_observation_never_confirms_live_status():
    p=ResearchEvidencePipeline(GAME,CUTOFF)
    url="https://www.espn.com/nba/team/roster/_/name/mia/miami-heat"
    quote="Miami Heat Roster includes Bam Adebayo and Giannis Antetokounmpo."
    p.add_source({"url":url,"title":"Miami Heat Roster"},{"url":url,"content":quote+" Miami Heat roster details."*6,
        "title":"Miami Heat Roster","captured_at":"2026-10-02T16:00:01Z"})
    decision=p.validate_claim(claim(url=url,kind="current_roster",quote=quote))
    assert decision["accepted"] and decision["temporal_basis"]=="observed_at"
    assert p.sources[url]["published_at"] is None
    assert p.validate_claim(claim(url=url,kind="availability",quote=quote,game_reference="October 3, 2026"))["reason"]=="live_precise_zoned_publication_required"


def test_fetch_time_does_not_replace_news_time():
    p=ResearchEvidencePipeline(GAME,CUTOFF)
    source(p,date=None,body=CURRENT)
    assert p.validate_claim(claim(kind="coach_comments",quote=CURRENT))["reason"]=="current_publication_time_unavailable"
    assert p.sources[URL]["observed_at"] is None


def test_current_snapshot_update_preferred_over_observation_when_provided():
    p=ResearchEvidencePipeline(GAME,CUTOFF)
    url="https://www.espn.com/nba/team/roster/_/name/mia/miami-heat"
    quote="Miami Heat Roster includes Bam Adebayo and Giannis Antetokounmpo."
    temporal=extract_temporal_metadata(url,title="Miami Heat Roster",provider={"updated_at":"2026-10-02T15:00:00Z"},
                                      fetched_at="2026-10-02T16:00:01Z")
    p.add_source({"url":url,"title":"Miami Heat Roster"},{"url":url,"content":quote+" Miami Heat roster."*8,
        "captured_at":"2026-10-02T16:00:01Z","temporal_metadata":temporal})
    decision=p.validate_claim(claim(url=url,kind="current_roster",quote=quote))
    assert decision["accepted"] and decision["temporal_basis"]=="updated_at"
    assert p.sources[url]["observed_at"]=="2026-10-02T16:00:01Z"


def test_projection_date_level_is_current_but_minutes_restriction_is_live():
    p=ResearchEvidencePipeline(GAME,CUTOFF)
    projected="Miami Heat projected starters include Bam Adebayo and Giannis Antetokounmpo."
    source(p,date="2026-10-01",body=projected)
    value=claim(kind="projected_starters",quote=projected);value["assertion_kind"]="reported_projection"
    assert p.validate_claim(value)["accepted"]
    assert not p.validate_claim({**value,"claim_type":"minutes_restriction","assertion_kind":"confirmed_fact",
        "game_reference":"October 3, 2026"})["accepted"]


def test_future_update_rejected_even_with_old_publication():
    p=ResearchEvidencePipeline(GAME,CUTOFF)
    fetched={"url":URL,"title":"Miami Heat","content":CURRENT+" Miami Heat context."*8,
        "published_at":"2026-09-22T10:00:00Z","updated_at":"2026-10-02T17:00:00Z",
        "captured_at":"2026-10-02T16:00:01Z"}
    p.add_source({"url":URL},fetched)
    assert p.validate_claim(claim(kind="coach_comments",quote=CURRENT))["reason"]=="publication_not_proven_before_cutoff"


def test_background_same_calendar_day_stays_date_level_with_pregame_capture():
    p=ResearchEvidencePipeline(GAME,CUTOFF)
    source(p,date="2026-10-02",body=BACKGROUND)
    assert p.validate_claim(claim())["accepted"]
    assert p.claims[0]["published_at"]=="2026-10-02"
    assert p.claims[0]["publication_precision"]=="date"


def test_search_provider_date_is_not_discarded_by_empty_fetch_metadata():
    p=ResearchEvidencePipeline(GAME,CUTOFF)
    temporal=extract_temporal_metadata(URL,title="Miami Heat",fetched_at="2026-10-02T16:00:01Z")
    p.add_source({"url":URL,"title":"Miami Heat","provider_temporal_metadata":{"datePublished":"2026-09-22"}},
        {"url":URL,"title":"Miami Heat","content":BACKGROUND+" Miami Heat details."*6,
         "captured_at":"2026-10-02T16:00:01Z","published_at":None,"temporal_metadata":temporal})
    assert p.validate_claim(claim())["accepted"]
    assert p.sources[URL]["published_at"]=="2026-09-22"
    assert p.sources[URL]["publication_time_source"]=="search_provider.datePublished"
