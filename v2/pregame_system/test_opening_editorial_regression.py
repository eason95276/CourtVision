from .editorial_contract import build_editorial_projection
from .editorial_hard_facts import _stat_issues_for_text, _season_scope_issues


def projection():
    return {'game':{'home':{'name_zh':'活塞'},'away':{'name_zh':'凯尔特人'}},'facts':[
        {'id':'stats','kind':'HISTORICAL','value':{'side':'away','season':'2025-26','players':[
            {'player':'Derrick White','average_statistics':{'points':16.455,'assists':5.377}},
            {'player':'Payton Pritchard','average_statistics':{'points':17.025,'assists':5.165}}]}}]}


def test_compact_historical_points_do_not_become_assists():
    assert _stat_issues_for_text('怀特和普里查德上赛季分别有16.5分5.4助攻和17.0分5.2助攻的输出。',projection(),'article') == []
    bad=_stat_issues_for_text('怀特上赛季有19.8分5.4助攻的输出。',projection(),'article')
    assert any(r['code']=='exact_stat_value_mismatch' and r['metric']=='points_for_per_game' for r in bad)


def test_opener_remains_blocked_without_verified_event():
    p=projection()
    assert _season_scope_issues('这场揭幕战真正要看的，是双方怎样回应对位。',p,'article')
    p['facts'].append({'id':'opening','kind':'GAME','game_specific':True,
        'value':{'season_event':{'name':'opening night','verified':True,'source_url':'https://pr.nba.com/schedule/'}}})
    assert _season_scope_issues('这场揭幕战真正要看的，是双方怎样回应对位。',p,'article')==[]


def test_market_total_is_not_locked_prediction_or_historical_stat():
    p=projection()
    p['facts'] += [{'id':'prediction','kind':'PUBLIC_DECISION','value':{'numbers':{'expected_total':226.5}}},
        {'id':'quote','kind':'MARKET','game_specific':True,'source_evidence_ids':['market-source'],
         'value':{'source_url':'https://sports.core.api.espn.com/odds','total':231.5,'captured_at':'2026-10-14T12:00:00Z'}}]
    assert _stat_issues_for_text('总分226.5则低于市场231.5。',p,'article')==[]
    assert _stat_issues_for_text('总分226.5则低于市场232.5。',p,'article')


def test_lack_of_confirmed_starters_is_not_a_confirmation():
    from .editorial_hard_facts import _availability_issues_for_text
    p=projection();p['facts'].append({'id':'roster','kind':'CURRENT','value':{'side':'home','team_id':1,'roster_snapshot':[{'name':'Joel Embiid'}]}})
    assert not any(r['code']=='confirmed_starter_source_unverified'for r in
        _availability_issues_for_text('双方都缺确认首发，Joel Embiid的负荷需要等待。',p,'article'))
    assert any(r['code']=='confirmed_starter_source_unverified'for r in
        _availability_issues_for_text('双方确认首发，Joel Embiid在其中。',p,'article'))


def test_availability_word_is_not_a_transaction_verb():
    from .editorial_hard_facts import _transaction_issues_for_text
    assert _transaction_issues_for_text('拥有 Jaylen Brown 等球星，但可用性未知。',projection(),'article')==[]
    assert _transaction_issues_for_text('用 Jaylen Brown 换来 Paul George。',projection(),'article')
