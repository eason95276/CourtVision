"""Synthetic subject/scope regressions; no live requests or production data."""
import copy

import pytest

from .editorial_hard_facts import _availability_issues_for_text, validate_publication_hard_facts


@pytest.fixture
def projection():
    away = ['Stephen Curry', 'Jimmy Butler', 'Brandin Podziemski', 'Moses Moody', 'Draymond Green']
    home = ['Walker Kessler', 'Luka Doncic']
    p = {'game': {'away': {'name_zh': '勇士', 'name': 'Golden State Warriors', 'id': 1},
                  'home': {'name_zh': '湖人', 'name': 'Los Angeles Lakers', 'id': 2}},
         'limits': {'unconfirmed_player_availability': True},
         'player_display_names': [
             {'canonical_name_en': 'Stephen Curry', 'display_name_zh': '斯蒂芬·库里'},
             {'canonical_name_en': 'Jimmy Butler', 'display_name_zh': '吉米·巴特勒'},
             {'canonical_name_en': 'Brandin Podziemski', 'display_name_zh': '布兰丁·波杰姆斯基'},
             {'canonical_name_en': 'Draymond Green', 'display_name_zh': '德雷蒙德·格林'}],
         'facts': [{'id': side, 'kind': 'CURRENT', 'value': {'side': side,
                    'roster_snapshot': [{'name': n} for n in names]}}
                   for side, names in [('away', away), ('home', home)]]}
    for name, status in [('Stephen Curry', 'available'), ('Jimmy Butler', 'out'),
                         ('Brandin Podziemski', 'out'), ('Moses Moody', 'out'), ('Walker Kessler', 'available')]:
        p['facts'].append({'id': name, 'kind': 'LIVE', 'game_specific': True,
            'value': {'player_name': name, 'claim_type': 'availability', 'status': status}})
    return p


@pytest.mark.parametrize('text', [
    '巴特勒与波杰姆斯基缺阵，他需要替库里分担持球压力。',
    '若进入预计首发后场，在巴特勒与波杰姆斯基缺阵的情况下，他需要替库里分担持球压力。',
    '勇士多名主力缺阵、进攻组织更多压在库里与格林身上。',
    '湖人换了个样子开场，勇士却带着一长串缺阵名单上门，库里身边还剩多少帮手，是这场球的看点。',
    '勇士是带着一份很长的缺阵名单来的。',
    '库里的队友巴特勒缺阵。',
    'Stephen Curry is available while Jimmy Butler is out.',
    '库里确认可以出战而巴特勒缺阵。',
    '巴特勒缺阵且库里确认可以出战。',
    'Jimmy Butler, Brandin Podziemski are out.',
    '如果库里缺阵，格林就需要组织更多进攻。',
    '尚未确认格林出战，不能说球队全员健康。',
    '库里不是缺阵，巴特勒缺阵。',
    '库里不会缺阵。',
    'Stephen Curry is not out.',
    'Jimmy Butler is not available.',
    '库里缺阵的消息尚未确认。',
    '格林控制节奏，库里绕掩护寻找接球机会。',
    'Curry is out of rhythm.',
])
def test_status_is_not_transferred_to_an_impact_player(projection, text):
    assert _availability_issues_for_text(text, projection, 'article') == []


@pytest.mark.parametrize('text', [
    '库里确认缺阵。',
    '巴特勒不是缺阵。',
    'Stephen Curry is not available.',
    '库里不可以出战。',
    '巴特勒确认可以出战。',
    '格林确认可以出战。',
    '预计首发后场，库里确认缺阵。',
    'Jimmy Butler, Stephen Curry are out.',
    '库里确认缺阵而巴特勒确认可以出战。',
    '湖人多名球员缺阵，东契奇需要组织进攻。',
    '如果巴特勒缺阵，库里已经确认缺阵。',
    '库里已经确认出战，球队全员健康。',
    'Unknown Guard is confirmed available.',
])
def test_real_conflicts_and_unknown_confirmations_still_block(projection, text):
    assert _availability_issues_for_text(text, projection, 'article')


@pytest.mark.parametrize('mode', ['wrong_team', 'duplicates', 'background', 'unqualified'])
def test_aggregate_absences_require_distinct_current_team_evidence(projection, mode):
    for fact in projection['facts']:
        if fact['kind'] == 'LIVE' and fact['value']['status'] == 'out':
            if mode == 'wrong_team': fact['value']['player_name'] = 'Other Team Player'
            if mode == 'duplicates': fact['value']['player_name'] = 'Jimmy Butler'
            if mode == 'background': fact['kind'] = 'BACKGROUND'
            if mode == 'unqualified': fact['game_specific'] = False
    assert _availability_issues_for_text('勇士多名球员缺阵。', projection, 'article')


@pytest.mark.parametrize('name,blocked', [('Stephen Curry', False), ('Jimmy Butler', True), ('Draymond Green', True)])
def test_named_card_supplies_the_subject_of_its_own_statement(projection, name, blocked):
    judgment = {'player_analysis': [{'player_name': name, 'statement': '确认可以出战。'}]}
    findings = validate_publication_hard_facts(judgment, projection)
    assert bool([x for x in findings if 'availability' in x['code']]) == blocked


def test_estimated_heading_cannot_hide_unbound_confirmation(projection):
    assert _availability_issues_for_text('预计首发后场，已经确认无法出战。', projection, 'article')


def test_availability_does_not_supply_minutes_or_confirmed_starters(projection):
    assert _availability_issues_for_text('库里本场限时20分钟。', projection, 'article')
    assert _availability_issues_for_text('库里确认首发。', projection, 'article')
    p = copy.deepcopy(projection)
    p['facts'].append({'id': 'minutes', 'kind': 'LIVE', 'game_specific': True,
        'value': {'player_name': 'Stephen Curry', 'claim_type': 'minutes_restriction',
                  'quote': 'Stephen Curry is on a 20-minute restriction.'}})
    assert _availability_issues_for_text('库里可以出战，本场限时20分钟。', p, 'article') == []
    assert _availability_issues_for_text('库里可以出战，本场限时25分钟。', p, 'article')
