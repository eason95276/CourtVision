"""Provider-only roster identities must survive the real history/context path."""
import copy

import pytest

from .espn_boxscore_normalizer import normalize_espn_summary_payload
from .player_knowledge import PlayerKnowledgeStore
from .test_espn_boxscore_normalizer import game, payload

CUTOFF = '2026-10-19T00:00:00Z'
HOME, AWAY = 1610612760, 1610612745
FUTURE = {'id': 'espn:future', 'season_start': 2026, 'tipoff': '2026-10-20T19:00:00Z',
          'home': {'id': HOME}, 'away': {'id': AWAY}}


@pytest.fixture
def store(tmp_path):
    result = PlayerKnowledgeStore(tmp_path / 'history.sqlite')
    target = game()
    result.initialize([target])
    rows = normalize_espn_summary_payload(target, payload(), {'10': AWAY, '25': HOME},
                                          expected_event_id='401809243')
    result.ingest_boxscore(target, payload(), normalized_rows=rows, provider='ESPN',
        collection_time_utc='2026-08-31T16:40:00Z', response_sha256='a' * 64,
        source_url='https://example.test/history')
    result.set_active_provider('ESPN')
    return result


def member(name, source_id):
    return {'name': name, 'player_id': None, 'source_player_id': source_id,
            'source_id_namespace': 'ESPN'}


def roster(away=None):
    def team(tid, members):
        return {'team_id': tid, 'current_team_roster': {
            'team_id': tid, 'status': 'available', 'roster_basis': 'espn_current_team_roster',
            'id_namespace': 'espn', 'players': members,
            'source': {'captured_at': '2026-09-15T12:00:00Z', 'source_type': 'espn_current_team_roster'},
            'coverage': {'source_roster_completeness': 'verified_complete'}}}
    return {'teams': {'home': team(HOME, [member('Home Player', '9999')]),
        'away': team(AWAY, away if away is not None else [member('Player One', '3202'),
                  member('Bench Player', '1111'), member('New Rookie', '55555')])}}


def packet(store, context=None, **kwargs):
    return store.matchup_context(FUTURE, CUTOFF, context or roster(), **kwargs)


def profiles(context):
    return {p['player_name']: p for p in context['teams']['away']['previous_season_players']}


def test_espn_only_ids_restore_history_without_inventing_nba_ids(store):
    current = roster()
    before = copy.deepcopy(current)
    result = packet(store, current)
    team = result['teams']['away']
    assert team['current_roster_player_count'] == 3
    assert team['current_roster_match_count'] == 2
    player = profiles(result)['Player One']
    assert player['player_id'] == '3202'
    assert player['player_id_namespace'] == 'ESPN'
    assert player['source_player_id'] == '3202'
    assert player['identity_match_method'] == 'same_provider_player_id'
    assert player['previous_season_total']['statistics']['points']['total'] == 23
    assert current == before
    assert current['teams']['away']['current_team_roster']['players'][0]['player_id'] is None


def test_absent_prior_record_is_not_a_zero_stat_line(store):
    rookie = profiles(packet(store))['New Rookie']
    assert rookie['previous_season_status'] == 'no_previous_season_record'
    assert rookie['previous_season_total'] is None
    assert rookie['last_season_minutes_total'] is None
    assert rookie['current_availability'] is None


def test_cross_provider_numeric_collision_never_matches_or_counts_as_retained(store):
    current = roster([{'player_id': '3202', 'player_id_namespace': 'NBA', 'name': 'Different Person'}])
    player = profiles(packet(store, current))['Different Person']
    assert player['source_player_id'] is None
    assert player['previous_season_total'] is None
    assert packet(store, current)['teams']['away']['continuity']['retained_minutes_ratio'] == 0


def test_same_digits_in_two_namespaces_do_not_collapse_roster_rows(store):
    current = roster([member('Player One', '3202'),
        {'player_id': '3202', 'player_id_namespace': 'NBA', 'name': 'Different Person'}])
    team = packet(store, current)['teams']['away']
    assert team['current_roster_player_count'] == 2
    assert team['current_roster_match_count'] == 1
    assert {p['player_id_namespace'] for p in team['previous_season_players']} == {'NBA', 'ESPN'}


def test_explicit_unknown_espn_id_does_not_fall_back_to_someone_elses_name(store):
    player = profiles(packet(store, roster([member('Player One', '55555')])))['Player One']
    assert player['source_player_id'] is None
    assert player['previous_season_status'] == 'no_previous_season_record'


def test_rows_without_ids_still_count_and_keep_unknown_identity(store):
    team = packet(store, roster([{'player_id': None, 'name': 'Unknown New Member'}]))['teams']['away']
    assert team['current_roster_player_count'] == 1
    player = team['previous_season_players'][0]
    assert player['player_id'] is None
    assert player['player_id_namespace'] is None
    assert player['previous_season_total'] is None
    assert team['continuity']['status'] == 'unavailable'


def test_roster_captured_after_cutoff_is_still_rejected(store):
    current = roster()
    current['teams']['away']['current_team_roster']['source']['captured_at'] = '2026-10-20T12:00:00Z'
    team = packet(store, current)['teams']['away']
    assert team['status'] == 'roster_unverified'
    assert team['reason'] == 'roster_captured_after_cutoff'


def test_duplicate_provider_identity_is_not_silently_deduplicated(store):
    team = packet(store, roster([member('Player One', '3202'), member('Alias', '3202')]))['teams']['away']
    assert team['status'] == 'roster_unverified'
    assert team['reason'] == 'duplicate_current_roster_identity'


def test_availability_id_collision_cannot_assign_another_providers_injury(store):
    injury = {'team_id': AWAY, 'player_id': '3202', 'player_id_namespace': 'NBA',
        'player_name': 'Different Person', 'status': 'Out',
        'source_published_at': '2026-10-18T12:00:00Z', 'captured_at': '2026-10-18T12:01:00Z'}
    assert profiles(packet(store, availability_records=[injury]))['Player One']['current_availability'] is None
    injury.update(player_id_namespace='ESPN')
    assert profiles(packet(store, availability_records=[injury]))['Player One']['current_availability']['status'] == 'Out'


def test_analyst_compaction_keeps_both_identity_namespaces(store):
    from .analyst import _compact_roster_context
    compact = _compact_roster_context({'teams': {}, 'player_knowledge': packet(store)})
    player = next(p for p in compact['player_knowledge']['teams']['away']['previous_season_players']
                  if p['player_name'] == 'Player One')
    assert player['player_id_namespace'] == 'ESPN'
    assert player['source_player_id_namespace'] == 'ESPN'


def test_nba_id_and_explicit_espn_alias_remain_distinct(store):
    current = roster([{'player_id': 'nba-123', 'player_id_namespace': 'NBA', 'name': 'Player One',
                       'player_id_aliases': {'ESPN': '3202'}}])
    player = profiles(packet(store, current))['Player One']
    assert player['player_id'] == 'nba-123'
    assert player['player_id_namespace'] == 'NBA'
    assert player['source_player_id'] == '3202'
    assert player['identity_match_method'] == 'explicit_provider_id_alias'


def test_player_moving_teams_keeps_previous_stint_separate_from_current_membership(store):
    current = roster()
    current['teams']['home']['current_team_roster']['players'] = [member('Player One', '3202')]
    player = packet(store, current)['teams']['home']['previous_season_players'][0]
    assert player['team_id'] == HOME
    assert player['previous_season_status'] == 'observed_for_other_team'
    assert player['current_team_last_season_stint'] is None
    assert player['previous_season_total']['statistics']['points']['total'] == 23


def test_unknown_source_namespace_is_not_assumed_to_be_espn(store):
    current = roster([{'player_id': None, 'source_player_id': '3202', 'name': 'Different Person'}])
    player = profiles(packet(store, current))['Different Person']
    assert player['player_id'] is None
    assert player['source_player_id'] is None
    assert player['previous_season_total'] is None


def test_two_roster_aliases_for_one_historical_person_are_ambiguous(store):
    current = roster([{'player_id': 'nba-1', 'name': 'Player One', 'player_id_namespace': 'NBA',
                       'player_id_aliases': {'ESPN': '3202'}},
                      {'player_id': 'nba-2', 'name': 'Different Person', 'player_id_namespace': 'NBA',
                       'player_id_aliases': {'ESPN': '3202'}}])
    team = packet(store, current)['teams']['away']
    assert team['current_roster_match_count'] == 0
    assert team['players_with_identity_crosswalk_unresolved'] == 2
    assert all(p['previous_season_total'] is None for p in team['previous_season_players'])


def test_roster_level_namespace_also_applies_to_availability_matching(store):
    current = roster([{'player_id': '3202', 'name': 'Player One'}])
    injury = {'team_id': AWAY, 'player_id': '3202', 'player_id_namespace': 'ESPN',
        'status': 'Out', 'source_published_at': '2026-10-18T12:00:00Z',
        'captured_at': '2026-10-18T12:01:00Z'}
    assert profiles(packet(store, current, availability_records=[injury]))['Player One']['current_availability']['status'] == 'Out'


def test_real_history_attachment_preserves_numeric_inputs(store):
    from v2.frontend.analysis_service import SavedGameAnalysisService
    current = {**copy.deepcopy(FUTURE), 'roster_context': roster(),
               'relationship': {'final': {'win_probability': .6, 'expected_margin': 3, 'expected_total': 220}}}
    before = copy.deepcopy(current['relationship'])
    service = SavedGameAnalysisService({'games': [current]}, analyst=object(), player_knowledge_store=store)
    service._attach_player_knowledge(current, CUTOFF)
    assert current['relationship'] == before
    assert current['roster_context']['player_knowledge']['teams']['away']['current_roster_match_count'] == 2
