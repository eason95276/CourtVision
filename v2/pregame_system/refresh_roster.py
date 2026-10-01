"""Manual, bounded roster refresh for the current eight-game demo scope."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode, urlparse

from . import ROOT
from .sources import get
from .storage import new_run, now, save
from .roster_context import normalize_nba_minutes, normalize_nba_roster


def refresh(games, *, fetcher=get, probe_receipts=()):
    folder = new_run('roster_context')
    attempts = list(probe_receipts)
    # A denied/non-JSON endpoint family is not retried for every team.
    blocked = {urlparse(r['url']).hostname for r in attempts if r.get('status') != 'success'}
    team_ids = sorted({int(g[f'{s}_team_id']) for g in games for s in ('home', 'away')})
    seasons = {int(g['season_start']) for g in games}
    if len(seasons) != 1:
        raise ValueError('refresh_requires_one_season')
    season = seasons.pop()
    rosters = {}
    minute_records = {}
    for tid in team_ids:
        if 'stats.nba.com' in blocked:
            rosters[tid] = None
            continue
        query = urlencode({'LeagueID': '00', 'Season': f'{season}-{str(season+1)[-2:]}', 'TeamID': tid})
        payload, receipt = fetcher('https://stats.nba.com/stats/commonteamroster?' + query)
        attempts.append(receipt)
        try:
            if payload is None or receipt.get('status') != 'success':
                raise ValueError('roster_source_unavailable')
            rosters[tid] = normalize_nba_roster(payload, tid, season, receipt)
            if rosters[tid]['status'] != 'available':
                raise ValueError('empty_roster')
        except (ValueError, KeyError, TypeError):
            rosters[tid] = None
            blocked.add('stats.nba.com')
        if rosters[tid] and 'stats.nba.com' not in blocked:
            query = urlencode({'LeagueID': '00', 'Season': f'{season-1}-{str(season)[-2:]}',
                           'SeasonType': 'Regular Season', 'PerMode': 'Totals', 'MeasureType': 'Base',
                           'TeamID': tid, 'LastNGames': 0, 'Month': 0, 'OpponentTeamID': 0,
                           'PaceAdjust': 'N', 'PlusMinus': 'N', 'Rank': 'N'})
            payload, receipt = fetcher('https://stats.nba.com/stats/leaguedashplayerstats?' + query)
            attempts.append(receipt)
            try:
                if payload is None or receipt.get('status') != 'success':
                    raise ValueError('minutes_source_unavailable')
                minute_records[tid] = normalize_nba_minutes(payload, tid, season-1, receipt)
            except (ValueError, KeyError, TypeError):
                blocked.add('stats.nba.com')
    coverage = []
    for tid in team_ids:
        minutes = minute_records.get(tid)
        record = {'team_id': tid, 'season_start': season, 'captured_at': now(),
                  'roster': rosters.get(tid), 'minutes': minutes,
                  'acquisition_status': 'available' if rosters.get(tid) else 'unavailable',
                  'attempts': attempts, 'blocked_families': sorted(blocked),
                  'failure_policy': 'one attempt per failing family; remaining teams explicitly skipped',
                  'previous_roster': None, 'confirmed_transactions': None}
        save(folder / 'teams' / f'{tid}.json', record)
        coverage.append({'team_id': tid, 'roster_available': bool(rosters.get(tid)),
                         'minutes_available': bool(minutes and minutes['status'] == 'available'),
                         'reason': None if rosters.get(tid) else 'source_unavailable_or_family_blocked'})
    save(folder / 'manifest.json', {'captured_at': now(), 'game_ids': [g['game_id'] for g in games],
                                  'teams': coverage, 'attempts': attempts,
                                  'raw_response_policy': 'sources.get immutable network receipt plus response.bin',
                                  'stats_scope': 'previous regular season, franchise-specific totals',
                                  'confirmed_starters': False, 'numeric_forecast_modified': False})
    return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe-receipts', nargs='*', type=Path, default=[])
    args = parser.parse_args()
    # Fixed frontend scope comes from saved real reports, not synthetic demo fixtures.
    from v2.frontend.export_data import latest_reports, latest_file, OUTPUTS, read_json
    schedule = read_json(latest_file(OUTPUTS / 'future_schedule', 'games.json'))
    ids = set(latest_reports())
    games = [game for game in schedule if game['game_id'] in ids]
    receipts = [json.loads(path.read_text(encoding='utf-8')) for path in args.probe_receipts]
    print(refresh(games, probe_receipts=receipts))


if __name__ == '__main__':
    main()
