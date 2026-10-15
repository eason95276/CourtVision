"""The saved-report index must not be restricted to today's schedule window."""
import sqlite3
from types import SimpleNamespace

from . import server


def test_off_window_published_reports_are_read_without_new_generation(tmp_path, monkeypatch):
    path = tmp_path / 'index.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE game_prediction_state(game_id TEXT, current_version_id TEXT, locked_version_id TEXT)')
        db.executemany('INSERT INTO game_prediction_state VALUES(?,?,?)', [
            ('opening', 'v1', None), ('archive', None, 'v2'), ('not-published', None, None),
            ('missing-identity', 'v3', None)])
    before = path.read_bytes()
    service = SimpleNamespace(store=SimpleNamespace(connect=lambda: sqlite3.connect(path)))
    monkeypatch.setattr(server, 'build_schedule_window_response', lambda **kwargs: {'games': []})
    reads = []
    def detail(game_id, **kwargs):
        reads.append(game_id)
        assert kwargs['version_service'] is service
        if game_id == 'missing-identity':
            return 404, {'status': 'not_found'}
        return 200, {'prediction': {'game_id': game_id, 'version': {'version_id': game_id}}}
    monkeypatch.setattr(server, 'build_game_detail_response', detail)
    status, payload = server.build_prediction_versions_response(version_service=service)
    assert status == 200
    assert set(payload['predictions']) == {'opening', 'archive'}
    assert set(reads) == {'opening', 'archive', 'missing-identity'}
    assert path.read_bytes() == before


def test_explicit_fixture_scope_does_not_expand_to_other_versions(monkeypatch):
    service = SimpleNamespace(store=SimpleNamespace(connect=lambda: (_ for _ in ()).throw(AssertionError('unexpected DB enumeration'))))
    status, payload = server.build_prediction_versions_response(data={'games': []}, version_service=service)
    assert status == 200 and payload['predictions'] == {}

