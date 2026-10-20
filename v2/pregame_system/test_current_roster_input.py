"""Current roster parsing and identity boundary; no external calls."""
import pytest
from .current_roster_input import verify_roster_component,parse_espn_roster
from .pregame_estimates import PregamePlan,validate_pregame_plan
from .test_pregame_estimates import inputs,plan
from .forecasting_workflow import prepare_analyst


def test_new_official_component_explicit_roster_pass_through():
    assert verify_roster_component("let {rosterData:y}=props;_=y.roster;render({players:_,coaches:[]})")
    with pytest.raises(ValueError,match="membership_unverified|filters"):
        verify_roster_component("let {rosterData:y}=props;_=y.roster.filter(x=>x.active);render({players:_})")


def test_espn_current_membership_is_source_namespaced_not_a_starter_list():
    html='<title>Toronto Raptors NBA Roster - ESPN</title><a href="/nba/player/_/id/99/nav">Navigation</a><table><tr><td><a href="/nba/player/_/id/3917174/immanuel-quickley">Immanuel Quickley</a></td></tr></table>'
    roster=parse_espn_roster(html,1610612761,2026,{"url":"https://www.espn.com/nba/team/roster/_/name/tor/toronto-raptors"})
    assert len(roster["players"])==1
    assert roster["players"][0]["source_id_namespace"]=="ESPN"
    assert roster["id_namespace"]=="espn"
    assert roster["players"][0]["player_id"] is None
    assert "confirmed_starters" not in roster
    with pytest.raises(ValueError,match="team_scope"):
        parse_espn_roster(html,1610612748,2026,{})


def test_current_roster_identity_allows_estimate_and_rejects_old_member():
    basketball,_,_=prepare_analyst(inputs().model_dump())
    estimate=plan()
    validate_pregame_plan(PregamePlan.model_validate(estimate),basketball)
    estimate["lineups"][0]["players"][0]="Old Historical Player"
    with pytest.raises(ValueError,match="current_identity_unverified"):
        validate_pregame_plan(PregamePlan.model_validate(estimate),basketball)


def test_fallback_parser_accepts_explicit_other_team_scope_without_hardcoded_target():
    html='<title>Boston Celtics Roster - ESPN</title><table><a href="/nba/player/_/id/1/test">Test Player</a></table>'
    roster=parse_espn_roster(html,1610612738,2026,{},expected_team_name='Boston Celtics')
    assert roster['team_id']==1610612738 and roster['players'][0]['name']=='Test Player'
