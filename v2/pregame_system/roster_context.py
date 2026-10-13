"""Source-bound roster intelligence; never a numeric prediction input."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from .storage import BASE, content_hash, digest, instant
from .prediction import local_day

CACHE = BASE / 'outputs/roster_context'


def nba_rows(payload, name):
    sets = payload.get('resultSets') or []
    for item in sets:
        if item.get('name') == name:
            headers = item.get('headers', [])
            return [dict(zip(headers, row)) for row in item.get('rowSet', [])]
    return []


def normalize_nba_roster(payload, team_id, season_start, source):
    rows = nba_rows(payload, 'CommonTeamRoster')
    players = []
    for row in rows:
        if int(row['TeamID']) != team_id or str(row['SEASON']) not in (str(season_start), f'{season_start}-{str(season_start+1)[-2:]}'):
            raise ValueError('roster_identity_or_season_mismatch')
        players.append({'player_id': str(row['PLAYER_ID']), 'name': row['PLAYER'],
                        'position': row.get('POSITION'), 'number': row.get('NUM'),
                        'raw_identity': row})
    if len({p['player_id'] for p in players}) != len(players):
        raise ValueError('duplicate_roster_player')
    return {'status': 'available' if players else 'unavailable', 'team_id': team_id,
            'season_start': season_start, 'id_namespace': 'nba', 'players': players,
            'source': source, 'coverage': {'roster_identity_coverage': 1.0 if players else None,
            'opening_night_roster_confirmed': False, 'source_roster_completeness': 'not_independently_verified'}}


def normalize_nba_minutes(payload, team_id, season_start, source):
    # Caller must request PerMode=Totals and the explicit previous season/team.
    parameters = payload.get('parameters') or {}
    if (str(parameters.get('PerMode')) != 'Totals'
            or str(parameters.get('Season')) != f'{season_start}-{str(season_start+1)[-2:]}'
            or parameters.get('SeasonType') != 'Regular Season'
            or str(parameters.get('TeamID')) != str(team_id)):
        raise ValueError('minutes_scope_or_denominator_unverified')
    rows = nba_rows(payload, 'LeagueDashPlayerStats')
    players = []
    for row in rows:
        if int(row['TEAM_ID']) != team_id:
            raise ValueError('minutes_team_mismatch')
        minutes = float(row['MIN'])
        if not math.isfinite(minutes) or minutes < 0:
            raise ValueError('invalid_minutes')
        players.append({'player_id': str(row['PLAYER_ID']), 'name': row['PLAYER_NAME'],
                        'minutes': minutes, 'raw_identity': row})
    if len({p['player_id'] for p in players}) != len(players):
        raise ValueError('duplicate_minutes_player')
    return {'status': 'available' if players else 'unavailable', 'team_id': team_id,
            'season_start': season_start, 'id_namespace': 'nba', 'players': players,
            'source': source, 'season_type': 'Regular Season', 'per_mode': 'Totals',
            'coverage': {'valid_minute_rows_ratio': 1.0 if players else None,
                         'season_totals_completeness': 'source_reported_not_independently_verified'}}


def calculate_continuity(roster, minutes):
    from .roster_continuity import calculate_continuity as _calculate_continuity

    return _calculate_continuity(roster, minutes)


def cold_start_context(game, games, cutoff, stats=None):
    season = int(game['season_start'])
    if games is None or games.empty:
        return {side: {'status': 'unavailable', 'cold_start': None,
                       'current_season_games_observed': None, 'current_season_games_confirmed': None,
                       'reason': 'team_history_unavailable'} for side in ('home', 'away')}
    prior = games[(games.game_date.dt.normalize() < local_day(cutoff)) & (games.game_id != game['game_id'])]
    result = {}
    for side in ('home', 'away'):
        tid = game[f'{side}_team_id']
        rows = prior[(prior.home_team_id == tid) | (prior.away_team_id == tid)].sort_values(['game_date', 'game_id'])
        row = {'status': 'available', 'season_start': season,
               'current_season_games_observed': int((rows.season_start == season).sum()),
               'current_season_games_confirmed': None, 'current_season_coverage': 'not_verified',
               'history_latest_game': str(rows.game_date.max()) if len(rows) else None,
               'historical_capture_semantics': 'retrospectively acquired local results; conservative date cutoff',
               'cold_start': None}
        if stats is not None:
            eligible_ids = set(stats.loc[stats.team_id == tid, 'game_id'])
            window_rows = rows[rows.game_id.isin(eligible_ids)]
            row['window_provenance'] = 'eligible team-stat rows used by basketball rolling inputs'
        else:
            window_rows = rows
            row['window_provenance'] = 'completed-game history proxy; team-stat availability not independently checked'
        for window in (5, 15):
            recent = window_rows.tail(window)
            prefix = f'rolling_{window}'
            row.update({f'{prefix}_current_season_games': int((recent.season_start == season).sum()),
                        f'{prefix}_previous_season_games': int((recent.season_start == season - 1).sum()),
                        f'{prefix}_older_season_games': int((recent.season_start < season - 1).sum()),
                        f'{prefix}_observed_games': len(recent),
                        f'{prefix}_game_ids': recent.game_id.tolist()})
        if len(window_rows):
            row['cold_start'] = row['rolling_15_current_season_games'] < min(15, len(window_rows))
        row['previous_season_is_majority'] = row['rolling_15_previous_season_games'] > row['rolling_15_observed_games'] / 2
        result[side] = row
    return result


def eligible_snapshot(team_id, season_start, cutoff, root=CACHE):
    selected = []
    for path in Path(root).glob('*/teams/*.json'):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            if record['team_id'] != team_id or record['season_start'] != season_start:
                continue
            if instant(record['captured_at']) > instant(cutoff):
                continue
            for key in ('roster', 'minutes', 'league_team_assignment', 'official_team_roster', 'current_team_roster'):
                source = (record.get(key) or {}).get('source')
                if source:
                    if instant(source['captured_at']) > instant(cutoff) or digest(source['raw_path']) != source['sha256']:
                        raise ValueError('ineligible_roster_source')
                for supporting in (record.get(key) or {}).get('supporting_sources', []):
                    if instant(supporting['captured_at']) > instant(cutoff) or digest(supporting['raw_path']) != supporting['sha256']:
                        raise ValueError('ineligible_official_page_supporting_source')
            selected.append((instant(record['captured_at']), path, record))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not selected:
        return None
    _, path, record = max(selected, key=lambda row: (row[0], str(row[1])))
    return dict(record, cache_path=str(path), cache_sha256=digest(path))


def local_roster_context(game, cutoff, games, root=CACHE, stats=None):
    if instant(cutoff) >= instant(game['scheduled_tipoff']):
        raise ValueError('roster_context_cutoff_not_pregame')
    cold = cold_start_context(game, games, cutoff, stats)
    teams = {}
    for side in ('home', 'away'):
        record = eligible_snapshot(int(game[f'{side}_team_id']), int(game['season_start']), cutoff, root)
        assignment = (record or {}).get('league_team_assignment')
        official = (record or {}).get('official_team_roster') or (record or {}).get('current_team_roster')
        legacy = (record or {}).get('roster')
        if legacy and legacy.get('source', {}).get('source_type') == 'official_nba_web_next_data':
            assignment = assignment or legacy
        elif legacy and legacy.get('roster_basis') == 'official_team_roster':
            official = official or legacy
        teams[side] = {'team_id': game[f'{side}_team_id'],
                       'roster': official,
                       'official_team_roster': official,
                       'league_team_assignment': assignment,
                       'roster_basis': official.get('roster_basis','official_team_roster') if official else 'unavailable',
                       'continuity': calculate_continuity(official, record.get('minutes')) if record else calculate_continuity(None, None),
                       'cold_start': cold[side], 'source_record': record,
                       'key_player_status': None, 'confirmed_starters': None,
                       'current_rotation': None,
                       'missing': ['no_confirmed_starters', 'current_rotation_unverified', 'current_player_status_unavailable']}
    result = {'game_id': game['game_id'], 'context_cutoff': cutoff, 'teams': teams,
              'numeric_model_usage': False,
              'classification': {'roster': 'source_listed_fact', 'continuity_and_window_counts': 'deterministic_calculation',
                                 'potential_player_impact': 'subjective_analysis_only'},
              'boundaries': ['current roster is not confirmed opening-night roster',
                             'missing previous participants are not confirmed departures',
                             'historical minutes do not predict starters',
                             'no manual player point adjustment']}
    result['context_hash'] = content_hash(result)
    return result
