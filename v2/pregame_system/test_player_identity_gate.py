"""Names are not current-team evidence, including in conditional/unknown wording."""
import pytest
from .player_identity import identity_issues


@pytest.fixture
def projection():
    return {'game':{'home':{'name':'Los Angeles Lakers'},'away':{'name':'Golden State Warriors'}},
        'player_display_names':[{'canonical_name_en':'Bronny James','display_name_zh':'布朗尼·詹姆斯'},
            {'canonical_name_en':'Stephen Curry','display_name_zh':'斯蒂芬·库里'}],
        'player_identity_lexicon':[{'canonical_name_en':'LeBron James','display_name_zh':'勒布朗·詹姆斯'},
            {'canonical_name_en':'Seth Curry','display_name_zh':'塞思·库里'}],
        'facts':[{'kind':'CURRENT','value':{'side':'home','roster_snapshot':[{'name':'Bronny James'},{'name':'Luka Doncic'}]}},
            {'kind':'CURRENT','value':{'side':'away','roster_snapshot':[{'name':'Stephen Curry'}]}},
            {'kind':'BACKGROUND','value':{'quote':'The new-look Lakers without LeBron James.'}}]}


@pytest.mark.parametrize('text',[
    '詹姆斯能否出战仍待确认。','如果詹姆斯出战，进攻会不同。','詹姆斯预计首发。',
    '勒布朗·詹姆斯出战待定。','LeBron James availability is unconfirmed.',
    'Unknown Guard availability remains unknown.','约翰·史密斯能否出战待确认。',
])
def test_uncertainty_cannot_bypass_membership_or_identity(projection,text):
    assert identity_issues(text,projection,'lead')


@pytest.mark.parametrize('text',[
    '布朗尼·詹姆斯能否出战待确认。','Stephen Curry availability remains unknown.',
    '库里预计出战。','去年詹姆斯出战时，湖人的进攻有所不同。',
    '勒布朗·詹姆斯离队后，湖人围绕东契奇重建。',
    '如果勒布朗·詹姆斯还在湖人，轮换会有所不同。',
])
def test_current_players_and_explicit_history_are_distinct(projection,text):
    assert identity_issues(text,projection,'lead')==[]


def test_global_lexicon_does_not_turn_all_surnames_ambiguous(projection):
    assert identity_issues('库里能否出战待确认。',projection,'lead')==[]
    issues=identity_issues('詹姆斯能否出战待确认。',projection,'lead')
    assert issues[0]['code']=='ambiguous_current_player_identity'
    assert set(issues[0]['candidates'])=={'Bronny James','LeBron James'}


def test_changing_membership_uses_frozen_roster_not_a_hardcoded_team(projection):
    projection['facts'][0]['value']['roster_snapshot'].append({'name':'LeBron James'})
    assert identity_issues('勒布朗·詹姆斯能否出战待确认。',projection,'lead')==[]
