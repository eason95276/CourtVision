import copy
import json
from pathlib import Path

import pytest

from . import ROOT
from .official_web_roster import (LEAGUE_URL, PARSER_VERSION, embedded_props,
                                  parse_league_rosters, restore_page)
from .roster_context import eligible_snapshot
from .storage import now

PHI, BOS = 1610612755, 1610612738
LIVE_RECEIPT = ROOT / 'v2/pregame_system/outputs/network/20260915T151535_a32871603a/receipt.json'


def row(pid=2544, tid=PHI):
    return {'PERSON_ID': pid, 'TEAM_ID': tid, 'PLAYER_FIRST_NAME': 'Example',
            'PLAYER_LAST_NAME': 'Player', 'TEAM_CITY': 'Philadelphia', 'TEAM_NAME': '76ers',
            'ROSTER_STATUS': 1, 'HISTORIC': False, 'IS_DEFUNCT': 0}


def html(rows):
    payload = json.dumps({'props': {'pageProps': {'players': rows}}})
    return '<script type="application/json" id="__NEXT_DATA__">' + payload + '</script>'


def source():
    return {'status': 'success', 'url': LEAGUE_URL, 'captured_at': '2026-09-15T15:15:37Z'}


def test_real_official_page_covers_sixteen_teams_and_philadelphia_probe():
    if not LIVE_RECEIPT.is_file():
        pytest.skip('live capture is a local immutable artifact, not a fabricated fixture')
    raw, receipt = restore_page(LIVE_RECEIPT)
    from v2.frontend.export_data import build_app_data
    data = build_app_data()
    tids = {game[side]['id'] for game in data['games'] for side in ('home', 'away')}
    rosters = parse_league_rosters(raw, receipt, tids, 2026)
    assert len(rosters) == 16
    assert all(roster['status'] == 'available' for roster in rosters.values())
    phi = {p['player_id']: p['name'] for p in rosters[PHI]['players']}
    assert phi['2544'] == 'LeBron James'
    assert phi['1630178'] == 'Tyrese Maxey'
    assert phi['203954'] == 'Joel Embiid'
    assert rosters[PHI]['source']['parser_version'] == PARSER_VERSION
    assert rosters[PHI]['coverage']['opening_night_roster_confirmed'] is False


def test_active_assignment_filters_free_agents_historic_and_inactive():
    active = row()
    inactive = dict(row(2), ROSTER_STATUS=0)
    historic = dict(row(3), HISTORIC=True)
    free = row(4, 0)
    result = parse_league_rosters(html([active, inactive, historic, free]), source(), [PHI, BOS], 2026)
    assert [p['player_id'] for p in result[PHI]['players']] == ['2544']
    assert result[BOS]['status'] == 'unavailable'
    assert result[BOS]['players'] == []


def test_conflicting_player_assignment_is_rejected_without_name_matching():
    with pytest.raises(ValueError, match='conflicting'):
        parse_league_rosters(html([row(1, PHI), row(1, BOS)]), source(), [PHI, BOS], 2026)


def test_missing_embedded_data_or_unverified_source_fails_explicitly():
    with pytest.raises(ValueError, match='next_data'):
        embedded_props('<html><script src="app.js"></script></html>')
    with pytest.raises(ValueError, match='unverified'):
        parse_league_rosters(html([row()]), dict(source(), url='https://example.com/players'), [PHI], 2026)


def test_commented_out_embedded_data_is_not_current_roster():
    with pytest.raises(ValueError, match='next_data'):
        embedded_props('<!--' + html([row()]) + '-->')
    result = parse_league_rosters('<!--' + html([row(2)]) + '-->' + html([row()]), source(), [PHI], 2026)
    assert [p['player_id'] for p in result[PHI]['players']] == ['2544']


def test_raw_hash_validation_and_current_capture_not_backfilled(tmp_path):
    from .storage import digest
    raw = tmp_path / 'page.html'
    raw.write_text(html([row()]), encoding='utf-8')
    receipt = dict(source(), raw_path=str(raw), sha256=digest(raw))
    receipt_file = tmp_path / 'receipt.json'
    receipt_file.write_text(json.dumps(receipt), encoding='utf-8')
    roster = parse_league_rosters(restore_page(receipt_file)[0], receipt, [PHI], 2026)[PHI]
    record = tmp_path / 'snapshot/teams/phi.json'
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({'team_id': PHI, 'season_start': 2026,
                                 'captured_at': receipt['captured_at'], 'roster': roster, 'minutes': None}), encoding='utf-8')
    assert eligible_snapshot(PHI, 2026, '2026-09-14T15:00:00Z', tmp_path) is None
    assert eligible_snapshot(PHI, 2026, now(), tmp_path)['roster']['status'] == 'available'
    raw.write_text('tampered', encoding='utf-8')
    with pytest.raises(ValueError, match='hash_mismatch'):
        restore_page(receipt_file)
    assert eligible_snapshot(PHI, 2026, now(), tmp_path) is None
