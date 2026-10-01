from __future__ import annotations

import copy
import json
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from .roster_context import (calculate_continuity, cold_start_context, eligible_snapshot,
                             local_roster_context, normalize_nba_minutes, normalize_nba_roster)
from .refresh_roster import refresh
from .storage import digest

HOME, AWAY = 1610612765, 1610612738
GAME = {'game_id': 'espn:fixture', 'home_team_id': HOME, 'away_team_id': AWAY,
        'season_start': 2026, 'scheduled_tipoff': '2026-10-20T19:00:00Z'}
CUTOFF = '2026-10-20T17:00:00Z'


def history(current=0):
    rows = [{'game_id': str(i), 'game_date': pd.Timestamp('2026-04-01') + pd.Timedelta(days=i),
             'season_start': 2025, 'home_team_id': HOME, 'away_team_id': AWAY} for i in range(20)]
    rows += [{'game_id': f'new{i}', 'game_date': pd.Timestamp('2026-10-10') + pd.Timedelta(days=i),
              'season_start': 2026, 'home_team_id': HOME, 'away_team_id': AWAY} for i in range(current)]
    return pd.DataFrame(rows)


def models():
    roster = {'status': 'available', 'team_id': HOME, 'season_start': 2026, 'id_namespace': 'nba',
              'players': [{'player_id': str(i), 'name': f'Player {i}'} for i in (10, 8, 1, 99)],
              'coverage': {'roster_identity_coverage': 1.0}}
    minutes = {'status': 'available', 'team_id': HOME, 'season_start': 2025, 'id_namespace': 'nba',
               'per_mode': 'Totals', 'players': [{'player_id': str(i), 'name': f'Player {i}',
                                                'minutes': float(i)} for i in range(1, 11)],
               'coverage': {'valid_minute_rows_ratio': 1.0}}
    return roster, minutes


def test_opening_cold_start_keeps_both_window_compositions():
    cold = cold_start_context(GAME, history(), CUTOFF)['home']
    assert cold['cold_start'] is True
    assert cold['current_season_games_observed'] == 0
    assert cold['current_season_games_confirmed'] is None
    assert cold['rolling_5_previous_season_games'] == 5
    assert cold['rolling_15_previous_season_games'] == 15
    assert cold['rolling_5_current_season_games'] == cold['rolling_15_current_season_games'] == 0


def test_mixed_seasons_and_cutoff_exclude_target_or_later_results():
    games = history(2)
    target = dict(games.iloc[-1], game_id=GAME['game_id'], game_date=pd.Timestamp('2026-10-20'))
    games = pd.concat([games, pd.DataFrame([target])], ignore_index=True)
    cold = cold_start_context(GAME, games, CUTOFF)['away']
    assert cold['rolling_5_current_season_games'] == 2
    assert cold['rolling_5_previous_season_games'] == 3
    assert cold['rolling_15_current_season_games'] == 2
    assert cold['rolling_15_previous_season_games'] == 13
    assert GAME['game_id'] not in cold['rolling_15_game_ids']


def test_rolling_five_can_be_current_while_fifteen_is_still_cold():
    cold = cold_start_context(GAME, history(6), CUTOFF)['home']
    assert cold['rolling_5_previous_season_games'] == 0
    assert cold['rolling_15_previous_season_games'] == 9
    assert cold['cold_start'] is True


def test_rolling_provenance_uses_available_team_stat_rows():
    games = history(2)
    stats = pd.DataFrame({'game_id': games.game_id.iloc[:-1], 'team_id': HOME})
    cold = cold_start_context(GAME, games, CUTOFF, stats)['home']
    assert cold['current_season_games_observed'] == 2
    assert cold['rolling_5_current_season_games'] == 1
    assert cold['rolling_5_previous_season_games'] == 4


def test_missing_history_is_unknown_not_zero():
    assert cold_start_context(GAME, None, CUTOFF)['home']['cold_start'] is None
    assert cold_start_context(GAME, pd.DataFrame(), CUTOFF)['away']['current_season_games_observed'] is None


def test_continuity_denominators_top_eight_and_membership_changes():
    roster, minutes = models()
    result = calculate_continuity(roster, minutes)
    assert result['retained_minutes_ratio'] == pytest.approx(19 / 55)
    assert result['top8_retained_count'] == 2
    assert result['top8_retained_minutes_ratio'] == pytest.approx(18 / 52)
    assert [p['player_id'] for p in result['newly_listed_players']] == ['99']
    assert {p['player_id'] for p in result['previous_participants_not_listed']} == {'2', '3', '4', '5', '6', '7', '9'}
    assert result['confirmed_transactions'] is None
    assert result['current_rotation'] is None
    assert result['projected_starters'] is None


def test_missing_minutes_does_not_fabricate_continuity_or_departures():
    roster, _ = models()
    result = calculate_continuity(roster, None)
    assert result['roster_identity_coverage'] == 1
    assert result['retained_minutes_ratio'] is None
    assert result['previous_participants_not_listed'] is None


@pytest.mark.parametrize('field,value', [('team_id', AWAY), ('season_start', 2024), ('id_namespace', 'espn')])
def test_continuity_rejects_identity_or_season_mismatch(field, value):
    roster, minutes = models()
    minutes[field] = value
    assert calculate_continuity(roster, minutes)['status'] == 'unavailable'


def test_zero_denominator_and_duplicate_id_are_rejected():
    roster, minutes = models()
    for p in minutes['players']:
        p['minutes'] = 0
    assert calculate_continuity(roster, minutes)['status'] == 'unavailable'
    minutes['players'].append(minutes['players'][0])
    assert calculate_continuity(roster, minutes)['reason'] == 'duplicate_player_identity'


def roster_payload(tid=HOME, season=2026):
    return {'resultSets': [{'name': 'CommonTeamRoster',
                           'headers': ['TeamID', 'SEASON', 'PLAYER_ID', 'PLAYER', 'POSITION'],
                           'rowSet': [[tid, str(season), 1, 'Known player', 'G']]}]}


def minute_payload(tid=HOME):
    return {'parameters': {'PerMode': 'Totals', 'Season': '2025-26', 'TeamID': tid, 'SeasonType': 'Regular Season'},
            'resultSets': [{'name': 'LeagueDashPlayerStats', 'headers': ['TEAM_ID', 'PLAYER_ID', 'PLAYER_NAME', 'MIN'],
                            'rowSet': [[tid, 1, 'Known player', 100]]}]}


def test_normalization_preserves_ids_and_rejects_combined_league_minutes():
    roster = normalize_nba_roster(roster_payload(), HOME, 2026, {})
    assert roster['players'][0]['raw_identity']['PLAYER_ID'] == 1
    minutes = normalize_nba_minutes(minute_payload(), HOME, 2025, {})
    assert minutes['players'][0]['minutes'] == 100
    bad = minute_payload()
    bad['parameters']['TeamID'] = 0
    with pytest.raises(ValueError, match='denominator'):
        normalize_nba_minutes(bad, HOME, 2025, {})
    with pytest.raises(ValueError, match='season_mismatch'):
        normalize_nba_roster(roster_payload(season=2025), HOME, 2026, {})


def test_capture_after_cutoff_and_corrupt_raw_are_not_eligible(tmp_path):
    raw = tmp_path / 'raw.json'
    raw.write_text('{}', encoding='utf-8')
    roster, minutes = models()
    source = {'captured_at': '2026-09-15T12:00:00Z', 'raw_path': str(raw), 'sha256': digest(raw)}
    roster['source'] = source
    path = tmp_path / 'run/teams/team.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'team_id': HOME, 'season_start': 2026,
                               'captured_at': source['captured_at'], 'roster': roster, 'minutes': minutes}), encoding='utf-8')
    assert eligible_snapshot(HOME, 2026, '2026-09-14T12:00:00Z', tmp_path) is None
    assert eligible_snapshot(HOME, 2026, CUTOFF, tmp_path) is not None
    raw.write_text('changed', encoding='utf-8')
    assert eligible_snapshot(HOME, 2026, CUTOFF, tmp_path) is None


def test_refresh_failure_stops_family_and_records_all_teams(monkeypatch, tmp_path):
    from . import refresh_roster
    monkeypatch.setattr(refresh_roster, 'new_run', lambda _: tmp_path)
    calls = []
    def denied(url):
        calls.append(url)
        return None, {'url': url, 'status': 'unavailable', 'http_status': 403}
    folder = refresh([GAME], fetcher=denied)
    assert len(calls) == 1
    assert len(list((folder / 'teams').glob('*.json'))) == 2
    assert all(json.loads(p.read_text())['roster'] is None for p in (folder / 'teams').glob('*.json'))


def test_refresh_success_uses_exact_franchise_totals(monkeypatch, tmp_path):
    from . import refresh_roster
    monkeypatch.setattr(refresh_roster, 'new_run', lambda _: tmp_path)
    def fetch(url):
        query = parse_qs(urlparse(url).query)
        tid = int(query['TeamID'][0])
        payload = roster_payload(tid) if 'commonteamroster' in url else minute_payload(tid)
        return payload, {'url': url, 'status': 'success', 'captured_at': CUTOFF}
    folder = refresh([GAME], fetcher=fetch)
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert len(manifest['attempts']) == 4
    assert all(row['roster_available'] and row['minutes_available'] for row in manifest['teams'])


def test_analyst_receives_roster_and_separate_windows_without_changing_forecast(tmp_path):
    from .analyst import build_analyst_context, EvidenceBoundAnalyst
    context = local_roster_context(GAME, CUTOFF, history(), root=tmp_path)
    prediction = {'win_probability': .55, 'expected_margin': 1, 'expected_total': 220}
    layers = {'pure_prediction': prediction, 'official_numeric_prediction': copy.deepcopy(prediction)}
    frozen = copy.deepcopy(layers)
    analyst = build_analyst_context(layers, [{'id': 'roster-evidence', 'section': 'roster_context', 'value': context}], missing_data=[])
    assert analyst.roster_context['teams']['home']['cold_start']['rolling_5_previous_season_games'] == 5
    assert analyst.roster_context['teams']['home']['official_team_roster'] is None
    assert layers == frozen
    messages = EvidenceBoundAnalyst._messages(analyst)
    assert 'roster-evidence' in messages[-1]['content']
    assert 'rolling_5_previous_season_games' in messages[-1]['content']
    assert 'official_team_roster' in messages[-1]['content']
    assert 'official_numeric_prediction' in messages[-1]['content']


def test_frontend_overlay_keeps_eight_saved_numeric_predictions():
    from v2.frontend.export_data import VERSIONED_SNAPSHOT, build_app_data, read_json
    data = build_app_data()
    source = {
        game["id"]: game
        for game in read_json(VERSIONED_SNAPSHOT)["games"]
    }
    assert len(data['games']) == 8
    for game in data['games']:
        assert game['prediction'] == source[game['id']]['prediction']
        cold = game['roster_context']['teams']['home']['cold_start']
        assert cold['rolling_5_previous_season_games'] == 5
        assert cold['rolling_15_previous_season_games'] == 15
        assert game['technical']['cutoff'] != game['technical']['roster_context_cutoff']


def test_real_agent_report_receives_context_offline_without_numeric_adjustment(monkeypatch):
    from .agent import PregameAgent
    from .storage import now
    def blocked(*args, **kwargs):
        raise AssertionError('no network or LLM permitted')
    monkeypatch.setattr('requests.Session.request', blocked)
    agent = PregameAgent()
    if agent.predictor.games.empty:
        pytest.skip('full historical prediction assets are outside the Demo runtime closure')
    report, _ = agent.ask('阵容与赛季窗口开发自测', cutoff=now(),
                          plan={'intent': 'report', 'game_id': 'espn:401909088'})
    context = report['analyst_context']['roster_context']
    assert context['game_id'] == report['roster_context']['game_id']
    assert 'source_record' not in context['teams']['home']
    assert context['teams']['home']['official_team_roster']['players']
    assert context['numeric_model_usage'] is False
    assert context['teams']['home']['cold_start']['rolling_5_previous_season_games'] == 5
    assert context['teams']['home']['cold_start']['rolling_15_previous_season_games'] == 15
    assert report['prediction'] == report['official_numeric_prediction']
    assert report['analyst_judgment'] is None
    assert report['snapshot']['official_prediction'] is False
