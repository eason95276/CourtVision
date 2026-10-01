"""Keep rendered official team rosters separate from league assignments."""
from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

from .official_web_roster import embedded_props, parse_league_rosters, restore_page
from .sources import get
from .storage import new_run, now, save

VERSION = 'official-team-rendered-roster-v1'


class RosterCells(HTMLParser):
    """Read real table cells; comments and script text never supply players."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active = False
        self.parts = []
        self.names = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'td' and 'player' in attrs.get('class', '').split():
            self.active = True
            self.parts = []
        if tag == 'button' and attrs.get('title', '').startswith('Show ') and attrs.get('title', '').endswith(' Stats'):
            self.names.append(attrs['title'][5:-6])

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self.active:
            self.names.append(' '.join(''.join(self.parts).split()))
            self.active = False


def normalized_name(name):
    return ' '.join(name.casefold().split())


def parse_rendered_table(html, tid, assignment, all_assignments, source):
    parser = RosterCells()
    parser.feed(html)
    names = list(dict.fromkeys(parser.names))
    if not names:
        return None
    identities = {}
    for roster in all_assignments.values():
        for player in roster['players']:
            identities.setdefault(normalized_name(player['name']), []).append(player)
    players = []
    conflicts = []
    for name in names:
        candidates = identities.get(normalized_name(name), [])
        ids = {p['player_id'] for p in candidates}
        if len(ids) == 1:
            reference = candidates[0]
            players.append({'player_id': reference['player_id'], 'name': name, 'team_id': tid,
                            'position': reference.get('position'), 'number': None,
                            'raw_identity': {'rendered_name': name, 'league_person_id': reference['player_id']},
                            'identity_mapping': 'unique exact normalized name within captured NBA player index'})
            if reference['team_id'] != tid:
                conflicts.append({'player_id': reference['player_id'], 'name': name,
                                  'league_team_id': reference['team_id'], 'official_page_team_id': tid})
        else:
            players.append({'player_id': None, 'name': name, 'team_id': tid,
                            'raw_identity': {'rendered_name': name}, 'identity_mapping': 'unresolved'})
    return pack_roster(tid, assignment['season_start'], players, source, [], conflicts,
                       'explicit rendered td.player cells; historical comments excluded')


def pack_roster(tid, season, players, source, supporting, conflicts, method):
    ids = [p['player_id'] for p in players if p['player_id'] is not None]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate_official_roster_identity')
    return {'status': 'available' if players else 'unavailable', 'team_id': tid,
            'season_start': season, 'id_namespace': 'nba', 'players': players,
            'roster_basis': 'official_team_roster', 'parser_version': VERSION,
            'source': dict(source, source_type='official_team_roster_page', parser_version=VERSION),
            'supporting_sources': supporting, 'assignment_conflicts': conflicts,
            'parse_method': method, 'coverage': {'roster_identity_coverage': len(ids)/len(players) if players else None,
            'opening_night_roster_confirmed': False, 'source_roster_completeness': 'source page displayed membership; not opening-night final roster'}}


def celtics_client_roster(assignment, page_source, data, data_source, overrides, override_source, script_source):
    players = copy.deepcopy(data['results']['roster'])
    for player in players:
        override = next((p for p in overrides.get('roster', []) if p['id'] == player['id']), None)
        if override:
            player.update(override)
    # Reproduce the exact official client membership operations, not league flags.
    hidden = set(overrides.get('hiddenPlayers', []))
    players = [p for p in players if p['id'] not in hidden]
    present = {p['id'] for p in players}
    players += [p for p in overrides.get('newPlayers', []) if p['id'] not in present]
    result = []
    for p in players:
        result.append({'player_id': str(p['id']), 'name': p['name'], 'team_id': 1610612738,
                       'position': p.get('position'), 'number': p.get('number'), 'raw_identity': p,
                       'identity_mapping': 'official page client NBA player id'})
    return pack_roster(1610612738, assignment['season_start'], result, page_source,
                       [data_source, override_source, script_source], [],
                       'official Celtics client content feed with hiddenPlayers/newPlayers overrides')


def parse_team_next_data(html, tid, assignment, source, client_source):
    props = embedded_props(html)
    if int(props['team']['id']) != tid or props['page']['slug'] != 'roster':
        raise ValueError('official_team_page_scope_mismatch')
    players = []
    for raw in props['rosterData']['roster']:
        if raw.get('teamId') not in (None, '') and int(raw['teamId']) != tid:
            raise ValueError('official_roster_row_team_mismatch')
        players.append({'player_id': str(raw['id']), 'name': raw['name'], 'team_id': tid,
                        'position': raw.get('position'), 'number': raw.get('number'),
                        'raw_identity': raw, 'identity_mapping': 'official team page NBA player id'})
        players[-1]['team_membership_evidence'] = ('official page team scope; row teamId absent'
                                                   if raw.get('teamId') in (None, '') else 'official row teamId and page scope agree')
    return pack_roster(tid, assignment['season_start'], players, source, [client_source], [],
                       'official team page rosterData.roster rendered by verified shared roster component')


def hawks_client_roster(assignment, page_source, local, local_source, nba, nba_source, script_source):
    local_by_id = {p['pid']: p for p in local['roster']['players']}
    players = []
    for raw in nba['results']['roster']:
        pid = str(raw['id'])
        p = local_by_id.get(pid)
        if not p or not p.get('active'):
            continue
        players.append({'player_id': pid, 'name': f"{p['fn']} {p['ln']}", 'team_id': 1610612737,
                        'position': raw.get('position'), 'number': raw.get('number'),
                        'raw_identity': {'nba': raw, 'team_config': p},
                        'identity_mapping': 'official Hawks pid joined to official NBA content player id'})
    return pack_roster(1610612737, assignment['season_start'], players, page_source,
                       [local_source, nba_source, script_source], [],
                       'official Hawks client renders team-config players with active truthy')


def refresh_team_rosters(league_receipt, receipt_map=None, fetcher=get):
    from v2.frontend.export_data import OUTPUTS, latest_file, latest_reports, read_json
    games = read_json(latest_file(OUTPUTS/'future_schedule', 'games.json'))
    report_ids = set(latest_reports())
    games = [g for g in games if g['game_id'] in report_ids]
    tids = {int(g[f'{s}_team_id']) for g in games for s in ('home', 'away')}
    raw, league_source = restore_page(league_receipt)
    # Map identity from all NBA franchises, never from a guessed name/contract.
    from courtvision.data import TEAM_ID_TO_ABBR
    all_assignments = parse_league_rosters(raw, league_source, TEAM_ID_TO_ABBR, 2026)
    receipt_map = receipt_map or {}
    def fetch(url, html=False):
        if url in receipt_map:
            value, source = restore_page(receipt_map[url])
            return value if html else json.loads(value), source
        return fetcher(url, parse_json=not html)
    client_url = 'https://www.nba.com/_teams/_next/static/chunks/pages/%5Bteamslug%5D/roster-a50f8cba31674450.js'
    client_js, client_source = fetch(client_url, True)
    if client_source['status'] != 'success' or 'S=v.roster' not in client_js:
        raise ValueError('shared_roster_client_render_contract_changed')
    def one(tid):
        assignment = all_assignments[tid]
        slug = assignment['players'][0]['raw_identity']['TEAM_SLUG']
        url = f'https://www.nba.com/{slug}/roster'
        html, page_source = fetch(url, True)
        official = None
        failure = None
        try:
            if page_source['status'] == 'success':
                official = parse_rendered_table(html, tid, assignment, all_assignments, page_source)
                if official is None and '__NEXT_DATA__' in html:
                    if '/roster-a50f8cba31674450.js' not in html:
                        raise ValueError('official_team_roster_client_version_changed')
                    official = parse_team_next_data(html, tid, assignment, page_source, client_source)
                if official is None and tid == 1610612737:
                    script = 'https://cdn.atlhawksdigital.com/js/roster.js?u=20240709'
                    if script not in html:
                        raise ValueError('hawks_roster_client_changed')
                    js, js_source = fetch(script, True)
                    local_url = 'https://www.atlhawksdigital.com/api/v1/roster'
                    nba_url = 'https://content-api-prod.nba.com/public/1/leagues/nba/teams/1610612737/roster'
                    local, local_source = fetch(local_url)
                    nba, nba_source = fetch(nba_url)
                    if local_url not in js or 'player_local.active' not in js:
                        raise ValueError('hawks_active_filter_contract_changed')
                    official = hawks_client_roster(assignment, page_source, local, local_source,
                                                  nba, nba_source, js_source)
                if official is None and tid == 1610612738:
                    api = 'https://content-api-prod.nba.com/public/1/leagues/nba/teams/1610612738/roster/'
                    override = 'https://cdn.celtics.com/dotcom/team/roster/overrides.json'
                    script = 'https://cdn.celtics.com/evergreen/dotcom/team/roster/js/roster_070225134936.min.js'
                    # Only use client-derived paths when the fetched page actually references that script.
                    if script not in html:
                        raise ValueError('celtics_page_client_version_changed')
                    js, js_source = fetch(script, True)
                    data, data_source = fetch(api)
                    overrides, override_source = fetch(override)
                    if any(s['status'] != 'success' for s in (js_source, data_source, override_source)):
                        raise ValueError('official_client_source_unavailable')
                    if 'hiddenPlayers' not in js or 'newPlayers' not in js or override not in js:
                        raise ValueError('official_client_filter_contract_changed')
                    official = celtics_client_roster(assignment, page_source, data, data_source,
                                                      overrides, override_source, js_source)
            if official is None:
                failure = 'page_unavailable_or_no_supported_explicit_rendered_membership'
        except (ValueError, KeyError, TypeError) as exc:
            failure = str(exc)
        official_ids = {p['player_id'] for p in (official or {}).get('players', []) if p['player_id'] is not None}
        assigned_ids = {p['player_id'] for p in assignment['players']}
        timestamps = [league_source['captured_at'], page_source.get('captured_at') or now()]
        timestamps += [s['captured_at'] for s in (official or {}).get('supporting_sources', [])]
        from .storage import instant
        return {'team_id': tid, 'season_start': 2026, 'captured_at': max(timestamps, key=instant),
                'official_team_roster': official, 'league_team_assignment': assignment,
                'roster': official, 'roster_basis': 'official_team_roster' if official else 'unavailable',
                'minutes': None, 'official_failure': failure, 'page_attempt': page_source,
                'assignment_only_player_ids': sorted(assigned_ids-official_ids) if official else None,
                'official_only_player_ids': sorted(official_ids-assigned_ids) if official else None,
                'contract_status': 'not_inferred', 'continuity_status': 'unavailable_missing_team_minutes'}
    with ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(one, sorted(tids)))
    folder = new_run('roster_context')
    for record in records:
        save(folder/'teams'/f"{record['team_id']}.json", record)
    save(folder/'manifest.json', {'captured_at': now(), 'parser_version': VERSION,
         'official_team_roster_count': sum(bool(r['official_team_roster']) for r in records),
         'teams': [{k:r[k] for k in ['team_id','roster_basis','official_failure','assignment_only_player_ids','official_only_player_ids']} for r in records],
         'league_source': league_source, 'numeric_forecast_modified': False})
    return folder


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--league-receipt', type=Path, required=True)
    parser.add_argument('--receipt-map', type=Path)
    args = parser.parse_args()
    receipt_map = json.loads(args.receipt_map.read_text(encoding='utf8')) if args.receipt_map else None
    print(refresh_team_rosters(args.league_receipt, receipt_map=receipt_map))
