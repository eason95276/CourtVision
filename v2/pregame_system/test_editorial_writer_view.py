import copy

from .editorial_pipeline import writer_view


def material(margin=7.5, market=True):
    game = {'game_id': 'espn:1', 'home': {'id': 1, 'name': 'Home'},
            'away': {'id': 2, 'name': 'Away'}, 'scheduled_tipoff': '2026-10-20T23:00:00Z'}
    locked = {'winner_side': 'home', 'win_probability': .7, 'expected_margin': margin, 'expected_total': 226.5}
    def fact(fid, kind, value):
        return {'id': fid, 'kind': kind, 'value': value, 'game_specific': False}
    projection = {'protocol': 'editorial-publication-v1', 'game_id': 'espn:1',
        'locked_prediction': locked, 'limits': {}, 'facts': [
            fact('f1', 'MODEL_OUTPUT', {'owner': 'pure_baseline', 'numbers': {'expected_margin': 20}}),
            fact('f2', 'MODEL_OUTPUT', {'owner': 'locked_final', 'numbers': locked}),
            fact('f3', 'HISTORICAL', {'meetings': [{'home_score': 100}], 'series_summary': {
                'season_start': 2025, 'games': 4, 'wins': {'1': 4, '2': 0}, 'winner_team_id': 1,
                'average_margin': 22.25, 'team_summaries': {'1': {'three_pct': .44}}}}),
            fact('f4', 'HISTORICAL', {'season': '2025-26', 'players': [
                {'player': 'A', 'statistics_team_ids': [1], 'average_statistics': {'points': 25},
                 'stat_sample_counts': {'points': 70}}]}),
            fact('f5', 'OBSERVATION', {'requested_games': 15, 'observed_games': 13, 'games': [{'score': 100}]}),
            fact('f6', 'CURRENT', {'side': 'home', 'roster_snapshot': [{'name': 'A'}]}),
        ]}
    forecast = {'final_prediction': {'winner_side': 'home', 'win_probability': .7,
        'expected_home_margin': margin, 'expected_total': 226.5}, 'market_collection': None}
    if market:
        forecast['market_collection'] = {'public_market_reference': {
            'status': 'available', 'home_spread': -4.5, 'away_spread': 4.5, 'total': 232.5,
            'game_identity': {'game_id': 'espn:1', 'home_team_id': 1, 'away_team_id': 2,
                              'scheduled_tipoff': game['scheduled_tipoff']}}}
    return projection, forecast, game


def test_prose_view_does_not_modify_validation_facts_or_locked_numbers():
    projection, forecast, game = material()
    before = copy.deepcopy(projection)
    view = writer_view(projection, forecast, game)
    assert projection == before
    assert view['locked_prediction'] == projection['locked_prediction']
    assert view['writing_material_policy']['complete_projection_preserved'] is True


def test_series_story_and_current_identity_survive_without_stat_tables():
    view = writer_view(*material())
    facts = {f['id']: f['value'] for f in view['facts']}
    assert facts['f3']['series_summary']['wins'] == {'1': 4, '2': 0}
    assert 'team_summaries' not in facts['f3']['series_summary']
    assert 'meetings' not in facts['f3']
    assert facts['f4']['players'][0]['player'] == 'A'
    assert facts['f4']['players'][0]['statistics_team_ids'] == [1]
    assert 'average_statistics' not in facts['f4']['players'][0]
    assert facts['f6']['roster_snapshot'][0]['name'] == 'A'
    assert facts['f5']['observed_games'] == 13
    assert 'games' not in facts['f5']


def test_writer_gets_final_direction_not_a_competing_baseline():
    view = writer_view(*material())
    assert 'f1' not in {f['id'] for f in view['facts']}
    position = view['prediction_interpretation']
    assert position['spread']['ats_lean'] == 'home'
    assert position['spread']['spread_edge'] == 3
    assert position['total']['total_lean'] == 'under'
    assert position['total']['total_edge'] == -6


def test_winning_team_and_covering_team_remain_distinct():
    position = writer_view(*material(margin=3))['prediction_interpretation']
    assert position['spread']['ats_lean'] == 'away'
    assert position['spread']['spread_edge'] == -1.5


def test_missing_quote_does_not_produce_an_invented_market_direction():
    position = writer_view(*material(market=False))['prediction_interpretation']
    assert position['spread']['status'] == 'unavailable'
    assert position['total']['status'] == 'unavailable'
