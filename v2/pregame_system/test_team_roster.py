import json

import pytest

from .team_roster import RosterCells, parse_rendered_table, parse_team_next_data
from .roster_context import calculate_continuity, local_roster_context
from .storage import digest

PHI, BOS = 1610612755, 1610612738


def assignment(tid=PHI):
    return {'team_id': tid, 'season_start': 2026, 'status': 'available',
            'source': {'source_type': 'official_nba_web_next_data'}, 'id_namespace': 'nba',
            'players': [{'player_id': '1', 'name': 'Known Player', 'team_id': tid}]}


def test_rendered_roster_excludes_comments_and_preserves_unique_nba_mapping():
    html = '<!--<td class="player">Old Player</td>--><td class="player">Known Player</td>'
    a = assignment()
    result = parse_rendered_table(html, PHI, a, {PHI: a}, {})
    assert [p['name'] for p in result['players']] == ['Known Player']
    assert result['players'][0]['player_id'] == '1'
    assert result['roster_basis'] == 'official_team_roster'


def test_heat_buttons_are_explicit_displayed_player_names():
    parser = RosterCells()
    parser.feed('<button title="Show Known Player Stats"></button><!--<button title="Show Old Player Stats"></button>-->')
    assert parser.names == ['Known Player']


def test_ambiguous_name_does_not_guess_nba_identity():
    a, b = assignment(), assignment(BOS)
    b['players'][0]['player_id'] = '2'
    result = parse_rendered_table('<td class="player">Known Player</td>', PHI, a, {PHI:a, BOS:b}, {})
    assert result['players'][0]['player_id'] is None
    assert result['coverage']['roster_identity_coverage'] == 0
    minutes = {'status':'available', 'players':[{'player_id':'1','minutes':100}],
               'team_id':PHI,'season_start':2025,'id_namespace':'nba','per_mode':'Totals'}
    assert calculate_continuity(result, minutes)['reason'] == 'unresolved_current_roster_identity'


def test_page_scope_allows_empty_row_team_id_without_filling_raw_identity():
    props = {'team': {'id':PHI}, 'page':{'slug':'roster'},
             'rosterData':{'roster':[{'id':1,'name':'Known Player','teamId':''}]}}
    html = '<script id="__NEXT_DATA__">' + json.dumps({'props':{'pageProps':props}}) + '</script>'
    result = parse_team_next_data(html, PHI, assignment(), {}, {})
    assert result['players'][0]['team_id'] == PHI
    assert result['players'][0]['raw_identity']['teamId'] == ''
    props['rosterData']['roster'][0]['teamId'] = BOS
    html = '<script id="__NEXT_DATA__">' + json.dumps({'props':{'pageProps':props}}) + '</script>'
    with pytest.raises(ValueError, match='team_mismatch'):
        parse_team_next_data(html, PHI, assignment(), {}, {})


def test_league_assignment_never_silently_supplies_continuity(tmp_path):
    record = {'team_id':PHI, 'season_start':2026,'captured_at':'2026-09-15T15:00:00Z',
              'league_team_assignment':assignment(), 'official_team_roster':None, 'roster':None,
              'minutes':{'status':'available','team_id':PHI,'season_start':2025,'id_namespace':'nba',
                         'per_mode':'Totals','players':[{'player_id':'1','name':'Known Player','minutes':100}]}}
    record['league_team_assignment'].pop('source')  # In-memory calculation fixture, no claimed external receipt.
    path = tmp_path/'run/teams/phi.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding='utf8')
    game = {'game_id':'fixture','season_start':2026,'home_team_id':PHI,'away_team_id':BOS,
            'scheduled_tipoff':'2026-10-20T19:00:00Z'}
    ctx = local_roster_context(game, '2026-09-16T12:00:00Z', None, root=tmp_path)
    assert ctx['teams']['home']['league_team_assignment']
    assert ctx['teams']['home']['roster'] is None
    assert ctx['teams']['home']['continuity']['retained_minutes_ratio'] is None


def test_actual_sixteen_team_cache_and_prediction_overlay():
    from v2.frontend.export_data import build_app_data
    payload = build_app_data()
    teams = {t['team_id']: t for g in payload['games'] for t in g['roster_context']['teams'].values()}
    assert len(teams) == 16
    assert all(t['official_team_roster'] and t['roster_basis'] == 'official_team_roster' for t in teams.values())
    assert len(teams[PHI]['official_team_roster']['players']) == 16
    assert teams[PHI]['league_team_assignment']['player_count'] == 24
    assert all(t['continuity']['retained_minutes_ratio'] is None for t in teams.values())
