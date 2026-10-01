"""Official NBA webpage roster snapshots, without blocked stats endpoints."""
from __future__ import annotations

import argparse
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from .sources import get
from .storage import digest, new_run, now, save
from .roster_context import calculate_continuity

PARSER_VERSION = 'nba-players-next-data-v1'
LEAGUE_URL = 'https://www.nba.com/players'


class NextDataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active = False
        self.parts = []
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('id') == '__NEXT_DATA__':
            self.active = True
            self.count += 1

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, value):
        if self.active:
            self.parts.append(value)


def embedded_props(html):
    parser = NextDataParser()
    parser.feed(html)
    if parser.count != 1:
        raise ValueError('official_page_missing_or_duplicate_next_data')
    return json.loads(''.join(parser.parts))['props']['pageProps']


def parse_league_rosters(html, source, team_ids, season_start):
    if (source.get('status') != 'success' or source.get('url') != LEAGUE_URL
            or urlparse(source['url']).hostname != 'www.nba.com'):
        raise ValueError('unverified_official_page_source')
    players = embedded_props(html).get('players')
    if not isinstance(players, list) or not players:
        raise ValueError('official_player_index_schema_changed')
    groups = {int(tid): [] for tid in team_ids}
    assigned = {}
    for raw in players:
        # Explicit official active-roster flag; never guess from names or G-League flags.
        if raw.get('ROSTER_STATUS') != 1 or raw.get('HISTORIC') is not False or raw.get('IS_DEFUNCT') != 0:
            continue
        tid = int(raw['TEAM_ID'])
        if tid not in groups:
            continue
        pid = str(int(raw['PERSON_ID']))
        if pid in assigned:
            raise ValueError('duplicate_or_conflicting_current_player_assignment')
        name = f"{raw['PLAYER_FIRST_NAME']} {raw['PLAYER_LAST_NAME']}".strip()
        if not name or not raw['TEAM_NAME']:
            raise ValueError('missing_official_player_identity')
        assigned[pid] = tid
        groups[tid].append({'player_id': pid, 'name': name, 'team_id': tid,
                            'team': f"{raw['TEAM_CITY']} {raw['TEAM_NAME']}",
                            'position': raw.get('POSITION'), 'number': raw.get('JERSEY_NUMBER'),
                            'raw_identity': raw})
    enriched_source = dict(source, source_type='official_nba_web_next_data', parser_version=PARSER_VERSION)
    return {tid: {'status': 'available' if rows else 'unavailable', 'team_id': tid,
                  'season_start': season_start, 'id_namespace': 'nba',
                  'season_semantics': 'requested demo season; current capture membership, not finalized season roster',
                  'players': sorted(rows, key=lambda row: int(row['player_id'])),
                  'source': enriched_source, 'parser_version': PARSER_VERSION,
                  'roster_snapshot_time': source['captured_at'],
                  'coverage': {'roster_identity_coverage': 1.0 if rows else None,
                               'opening_night_roster_confirmed': False,
                               'source_roster_completeness': 'official active assignment index; camp/final roster not independently verified'}}
            for tid, rows in groups.items()}


def restore_page(receipt_path):
    source = json.loads(Path(receipt_path).read_text(encoding='utf-8'))
    if digest(source['raw_path']) != source['sha256']:
        raise ValueError('official_page_raw_hash_mismatch')
    return Path(source['raw_path']).read_text(encoding='utf-8'), source


def refresh_official_web(games, *, league_receipt=None, discovery_receipts=(), fetcher=get):
    if league_receipt:
        html, source = restore_page(league_receipt)
    else:
        html, source = fetcher(LEAGUE_URL, parse_json=False)
    if html is None:
        raise ValueError('official_league_page_unavailable; no repeated requests')
    team_ids = sorted({int(g[f'{side}_team_id']) for g in games for side in ('home', 'away')})
    seasons = {int(g['season_start']) for g in games}
    if len(seasons) != 1:
        raise ValueError('one_demo_season_required')
    rosters = parse_league_rosters(html, source, team_ids, seasons.pop())
    if not rosters.get(1610612755) or rosters[1610612755]['status'] != 'available':
        raise ValueError('Philadelphia_probe_not_available')
    # No expected player is inserted, and names are not parser success criteria.
    folder = new_run('roster_context')
    coverage = []
    for tid, roster in rosters.items():
        continuity = calculate_continuity(None, None)
        save(folder / 'teams' / f'{tid}.json', {'team_id': tid, 'season_start': roster['season_start'],
             'captured_at': source['captured_at'], 'roster': None, 'minutes': None,
             'league_team_assignment': roster, 'official_team_roster': None,
             'roster_basis': 'unavailable',
             'acquisition_status': roster['status'], 'parser_version': PARSER_VERSION,
             'attempts': [source], 'previous_roster': None, 'confirmed_transactions': None,
             'minutes_status': 'unavailable; official stats pages did not embed franchise player minute totals'})
        coverage.append({'team_id': tid, 'roster_count': len(roster['players']),
                         'roster_available': roster['status'] == 'available', 'continuity': continuity})
    save(folder / 'manifest.json', {'captured_at': now(), 'roster_snapshot_time': source['captured_at'],
         'source': source, 'source_type': 'official_nba_web_next_data', 'parser_version': PARSER_VERSION,
         'discovery_receipts': list(discovery_receipts), 'teams': coverage,
         'roster_basis': 'ROSTER_STATUS=1, HISTORIC=false, IS_DEFUNCT=0, exact NBA TEAM_ID',
         'source_scope_note': 'league index active assignments may differ from editorial team roster page',
         'numeric_forecast_modified': False})
    return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--league-receipt', type=Path)
    parser.add_argument('--discovery-receipts', nargs='*', type=Path, default=[])
    args = parser.parse_args()
    from v2.frontend.export_data import OUTPUTS, latest_file, latest_reports, read_json
    schedule = read_json(latest_file(OUTPUTS / 'future_schedule', 'games.json'))
    report_ids = set(latest_reports())
    games = [game for game in schedule if game['game_id'] in report_ids]
    discoveries = [json.loads(path.read_text(encoding='utf-8')) for path in args.discovery_receipts]
    print(refresh_official_web(games, league_receipt=args.league_receipt, discovery_receipts=discoveries))


if __name__ == '__main__':
    main()
