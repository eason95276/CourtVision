from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from .prediction_versions import PredictionVersionStore, VersionStoreError
from .ats import build_spread_comparison
from .editorial_test_helpers import seal_fixture_payload


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def game(*, tipoff="2026-10-20T23:00:00Z", game_id="espn:401909089"):
    return {
        "id": game_id,
        "status": "scheduled",
        "tipoff": tipoff,
        "home": {"id": 1610612752},
        "away": {"id": 1610612755},
    }


def publish(store: PredictionVersionStore, clock: Clock, *, request_id: str, margin=6.5,
            market_collection=None, independent_margin=None):
    selected_game = game()
    request = store.begin_request(selected_game, request_id=request_id)
    clock.value += timedelta(seconds=3)
    payload = {
        "game_id": selected_game["id"],
        "game_identity": request["game_identity"],
        "request_id": request_id,
        "requested_at": request["requested_at"],
        "input_cutoff_at": request["requested_at"],
        "input_frozen_at": (datetime.fromisoformat(request["requested_at"]) + timedelta(seconds=1)).isoformat(),
        "generated_at": (datetime.fromisoformat(request["requested_at"]) + timedelta(seconds=2)).isoformat(),
        "trigger": {"type": "manual_refresh"},
        "input_snapshot": selected_game,
        "model_layers": {"official_numeric_prediction": {"expected_margin": 4.0}},
        "market_collection": market_collection,
        "research": {"records": [{"id": "source-1", "snippet": "frozen input"}]},
        "analyst_context": {"context_hash": "context-a", "objective_evidence": [{"id": "source-1"}]},
        "judgment": {
            "analysis_article": "Versioned article",
            "final_prediction": {
                "winner_side": "home",
                "win_probability": 0.63,
                "expected_margin": margin,
                "expected_total": 224.5,
                "evidence_ids": ["source-1"],
            },
        },
    }
    payload["prediction"] = payload["judgment"]["final_prediction"]
    if market_collection is not None:
        independent = ({"winner_side": "home" if independent_margin > 0 else "away",
                        "win_probability": .6, "expected_home_margin": independent_margin,
                        "expected_total": 220.0}
                       if independent_margin is not None else None)
        payload["spread_comparison"] = build_spread_comparison(
            selected_game, market_collection, independent, payload["prediction"]
        )
    seal_fixture_payload(payload)
    return store.publish(selected_game, request, payload)


def test_two_successful_versions_are_immutable_and_current_points_to_second(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    first = publish(store, clock, request_id="00000000-0000-0000-0000-000000000001", margin=4.5)
    frozen = json.loads(json.dumps(first))
    second = publish(store, clock, request_id="00000000-0000-0000-0000-000000000002", margin=7.5)

    assert first["version_id"] != second["version_id"]
    assert first["judgment"]["final_prediction"]["expected_margin"] == 4.5
    assert first == frozen
    assert second["judgment"]["final_prediction"]["expected_margin"] == 7.5
    assert second["spread_comparison"]["status"] == "unavailable"
    assert second["spread_comparison"]["prediction_version_id"] == second["version_id"]
    assert store.select_current(game())["version_id"] == second["version_id"]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM prediction_versions").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM game_prediction_state").fetchone()[0] == 1
        tables = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == {"prediction_versions", "game_prediction_state"}


def test_prediction_version_freezes_independent_and_final_ats_against_its_quote(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    store = PredictionVersionStore(tmp_path / "ats.sqlite", clock=clock)
    quote = {"status": "available", "game_identity": {
        "game_id": "espn:401909089", "home_team_id": 1610612752, "away_team_id": 1610612755,
        "home_team": "New York Knicks", "away_team": "Philadelphia 76ers"},
        "home_spread": -4.5, "away_spread": 4.5, "expected_home_margin": 4.5,
        "sportsbook": "Draft Kings", "source_url": "https://example.test/odds",
        "captured_at": "2026-09-29T23:59:00Z", "observed_at": "2026-09-29T23:59:00Z",
        "published_at": None, "raw_response_sha256": "b" * 64}
    collection = {"status": "available", "public_market_reference": quote,
        "evidence": [{"id": "market-quote-id", "value": {"market_reference": quote}}]}
    saved = publish(store, clock, request_id="00000000-0000-0000-0000-000000000101",
                    margin=6.5, market_collection=collection, independent_margin=3.5)
    comparison = saved["spread_comparison"]
    assert comparison["prediction_version_id"] == saved["version_id"]
    assert comparison["market"]["market_quote_id"] == "market-quote-id"
    assert comparison["market"]["market_implied_home_margin"] == 4.5
    assert comparison["independent"]["spread_edge"] == -1.0
    assert comparison["independent"]["ats_lean"] == "away"
    assert comparison["final"]["spread_edge"] == 2.0
    assert comparison["final"]["ats_lean"] == "home"
    with store.connect() as db:
        raw, digest = db.execute("SELECT payload_json,payload_sha256 FROM prediction_versions WHERE version_id=?",
                                 (saved["version_id"],)).fetchone()
    assert json.loads(raw)["spread_comparison"] == comparison
    assert __import__("hashlib").sha256(raw.encode("utf-8")).hexdigest() == digest


def test_prediction_version_rejects_forged_spread_edge(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    store = PredictionVersionStore(tmp_path / "bad-ats.sqlite", clock=clock)
    quote = {"status": "available", "home_spread": -4.5,
             "expected_home_margin": 4.5, "captured_at": "2026-09-29T23:59:00Z"}
    collection = {"status": "available", "public_market_reference": quote}
    payload_margin = 6.5
    selected_game = game()
    request = store.begin_request(selected_game, request_id="00000000-0000-0000-0000-000000000102")
    final = {"winner_side": "home", "win_probability": .63, "expected_margin": payload_margin,
             "expected_total": 224.5, "evidence_ids": ["source-1"]}
    independent = {"winner_side": "home", "win_probability": .6, "expected_home_margin": 3.5,
                   "expected_total": 220.0}
    comparison = build_spread_comparison(selected_game, collection, independent, final)
    comparison["independent"]["spread_edge"] = 999
    clock.value += timedelta(seconds=3)
    payload = {"game_id": selected_game["id"], "game_identity": request["game_identity"],
        "request_id": request["request_id"], "requested_at": request["requested_at"],
        "input_cutoff_at": request["requested_at"],
        "input_frozen_at": (datetime.fromisoformat(request["requested_at"]) + timedelta(seconds=1)).isoformat(),
        "generated_at": (datetime.fromisoformat(request["requested_at"]) + timedelta(seconds=2)).isoformat(),
        "input_snapshot": selected_game, "market_collection": collection,
        "objective_evidence": [{"id": "source-1", "section": "fixture", "value": {"verified": True}}],
        "evidence_index": {"source-1": {"section": "fixture", "source_type": "fixture"}},
        "spread_comparison": comparison, "prediction": final, "judgment": {"final_prediction": final}}
    with pytest.raises(VersionStoreError, match="spread_comparison_invalid:spread_comparison_independent_mismatch"):
        store.publish(selected_game, request, seal_fixture_payload(payload))


def test_prediction_version_rejects_final_exact_market_tie(tmp_path):
    clock=Clock(datetime(2026,9,30,tzinfo=timezone.utc))
    store=PredictionVersionStore(tmp_path/"final-tie.sqlite",clock=clock)
    quote={"status":"available","home_spread":-4.5,"away_spread":4.5,
        "expected_home_margin":4.5,"captured_at":"2026-09-29T23:59:00Z"}
    collection={"status":"available","public_market_reference":quote}
    with pytest.raises(VersionStoreError,match="spread_comparison_invalid:final_spread_edge_zero"):
        publish(store,clock,request_id="00000000-0000-0000-0000-000000000103",
                margin=4.5,market_collection=collection,independent_margin=4.5)


def test_prediction_version_publisher_rejects_final_exact_market_tie(tmp_path):
    clock=Clock(datetime(2026,9,30,tzinfo=timezone.utc))
    store=PredictionVersionStore(tmp_path/"final-tie.sqlite",clock=clock)
    quote={"status":"available","home_spread":-4.5,"away_spread":4.5,
        "expected_home_margin":4.5,"captured_at":"2026-09-29T23:59:00Z"}
    collection={"status":"available","public_market_reference":quote}
    with pytest.raises(VersionStoreError,match="spread_comparison_invalid:final_spread_edge_zero"):
        publish(store,clock,request_id="00000000-0000-0000-0000-000000000103",
                margin=4.5,market_collection=collection,independent_margin=4.5)


def test_duplicate_published_request_is_idempotent(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    first = publish(store, clock, request_id="00000000-0000-0000-0000-000000000011")
    duplicate = store.begin_request(game(), request_id="00000000-0000-0000-0000-000000000011")
    assert duplicate["request_seq"] == first["request_seq"]
    assert duplicate["already_published"]["version_id"] == first["version_id"]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM prediction_versions").fetchone()[0] == 1


def test_old_slow_request_cannot_replace_newer_success(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    selected_game = game()
    old = store.begin_request(selected_game, request_id="00000000-0000-0000-0000-000000000021")
    new = store.begin_request(selected_game, request_id="00000000-0000-0000-0000-000000000022")

    def candidate(request, margin):
        final = {
            "winner_side": "home", "win_probability": 0.6,
            "expected_margin": margin, "expected_total": 220.0,
        }
        return {
            "game_id": selected_game["id"], "game_identity": request["game_identity"],
            "request_id": request["request_id"], "requested_at": request["requested_at"],
            "input_cutoff_at": request["requested_at"],
            "input_frozen_at": (datetime.fromisoformat(request["requested_at"]) + timedelta(seconds=1)).isoformat(),
            "generated_at": (datetime.fromisoformat(request["requested_at"]) + timedelta(seconds=2)).isoformat(),
            "input_snapshot": selected_game,
            "prediction": final,
            "judgment": {"final_prediction": final},
        }

    clock.value += timedelta(seconds=3)
    newer = store.publish(selected_game, new, seal_fixture_payload(candidate(new, 8.5)))
    with pytest.raises(VersionStoreError, match="stale_request_seq"):
        store.publish(selected_game, old, seal_fixture_payload(candidate(old, 3.5)))
    assert store.select_current(selected_game)["version_id"] == newer["version_id"]


def test_failed_new_request_does_not_prevent_older_valid_request_from_publishing(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    selected_game = game()
    old = store.begin_request(selected_game, request_id="00000000-0000-0000-0000-000000000031")
    store.begin_request(selected_game, request_id="00000000-0000-0000-0000-000000000032")
    clock.value += timedelta(seconds=3)
    payload = {
        "game_id": selected_game["id"], "game_identity": old["game_identity"],
        "request_id": old["request_id"], "requested_at": old["requested_at"],
        "input_cutoff_at": old["requested_at"],
        "input_frozen_at": (datetime.fromisoformat(old["requested_at"]) + timedelta(seconds=1)).isoformat(),
        "generated_at": (datetime.fromisoformat(old["requested_at"]) + timedelta(seconds=2)).isoformat(),
        "input_snapshot": selected_game,
        "judgment": {"final_prediction": {
            "winner_side": "away", "win_probability": 0.57, "expected_margin": 2.5,
            "expected_total": 218.5,
        }},
    }
    payload["prediction"] = payload["judgment"]["final_prediction"]
    saved = store.publish(selected_game, old, seal_fixture_payload(payload))
    assert store.select_current(selected_game)["version_id"] == saved["version_id"]


def test_independent_connections_serialize_publish_and_preserve_newer_request(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    path = tmp_path / "versions.sqlite"
    store_a = PredictionVersionStore(path, clock=clock)
    store_b = PredictionVersionStore(path, clock=clock)
    selected_game = game()
    old = store_a.begin_request(selected_game, request_id="00000000-0000-0000-0000-000000000041")
    new = store_b.begin_request(selected_game, request_id="00000000-0000-0000-0000-000000000042")
    clock.value += timedelta(seconds=3)

    def make(request, margin):
        final = {
            "winner_side": "home", "win_probability": 0.61,
            "expected_margin": margin, "expected_total": 225.0,
        }
        return {
            "game_id": selected_game["id"], "game_identity": request["game_identity"],
            "request_id": request["request_id"], "requested_at": request["requested_at"],
            "input_cutoff_at": request["requested_at"],
            "input_frozen_at": (datetime.fromisoformat(request["requested_at"]) + timedelta(seconds=1)).isoformat(),
            "generated_at": (datetime.fromisoformat(request["requested_at"]) + timedelta(seconds=2)).isoformat(),
            "input_snapshot": selected_game,
            "prediction": final,
            "judgment": {"final_prediction": final},
        }

    newer_published = threading.Event()
    release_old = threading.Event()
    errors = []

    def publish_old():
        release_old.wait(timeout=5)
        try:
            store_a.publish(selected_game, old, seal_fixture_payload(make(old, 2.5)))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=publish_old)
    thread.start()
    newer = store_b.publish(selected_game, new, seal_fixture_payload(make(new, 6.5)))
    newer_published.set()
    release_old.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], VersionStoreError)
    assert store_a.select_current(selected_game)["version_id"] == newer["version_id"]


def test_payload_hash_tampering_is_detected(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    saved = publish(store, clock, request_id="00000000-0000-0000-0000-000000000051")
    with store.connect() as db:
        db.execute(
            "UPDATE prediction_versions SET payload_json=? WHERE version_id=?",
            ('{"damaged":true}', saved["version_id"]),
        )
    with pytest.raises(VersionStoreError, match="version_payload_hash_mismatch"):
        store.select_current(game())


def test_state_foreign_key_rejects_cross_game_version_pointer(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    saved = publish(store, clock, request_id="00000000-0000-0000-0000-000000000061")
    other = game(game_id="espn:401909088")
    with store.connect() as db:
        db.execute("BEGIN")
        db.execute(
            "INSERT INTO game_prediction_state(game_id, game_identity_json) VALUES(?, ?)",
            (other["id"], json.dumps({
                "game_id": other["id"], "home_team_id": other["home"]["id"],
                "away_team_id": other["away"]["id"], "scheduled_tipoff": "2026-10-20T23:00:00+00:00",
            }, sort_keys=True)),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE game_prediction_state SET current_version_id=? WHERE game_id=?",
                (saved["version_id"], other["id"]),
            )
        db.execute("ROLLBACK")


def test_tipoff_boundary_locks_latest_version_and_blocks_later_publish(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    selected_game = game()
    saved = publish(store, clock, request_id="00000000-0000-0000-0000-000000000071")
    clock.value = datetime.fromisoformat(selected_game["tipoff"])
    locked = store.select_current(selected_game)
    assert locked["status"] == "locked"
    assert locked["locked_at"] == selected_game["tipoff"].replace("Z", "+00:00")
    assert locked["version_id"] == saved["version_id"]
    with pytest.raises(VersionStoreError, match="pregame_generation_closed"):
        store.begin_request(selected_game)


def test_start_without_successful_version_locks_unavailable(tmp_path):
    clock = Clock(datetime.fromisoformat("2026-10-20T23:00:00+00:00"))
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    locked = store.select_current(game())
    assert locked["status"] == "locked_unavailable"
    assert locked["version"] is None
    assert locked["locked_at"] is not None
    with pytest.raises(VersionStoreError):
        store.begin_request(game())


def test_tipoff_or_team_identity_change_stops_publication(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    store.select_current(game())
    changed_time = game(tipoff="2026-10-21T23:00:00Z")
    with pytest.raises(VersionStoreError, match="schedule_changed"):
        store.begin_request(changed_time)
    changed_team = game()
    changed_team["away"]["id"] = 1610612748
    with pytest.raises(VersionStoreError, match="schedule_changed"):
        store.begin_request(changed_team)


@pytest.mark.parametrize("status", ["in", "post", "in_progress", "cancelled"])
def test_non_scheduled_status_rejects_request_before_generation(tmp_path, status):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    selected_game = game()
    selected_game["status"] = status
    with pytest.raises(VersionStoreError, match="pregame_generation_closed"):
        store.begin_request(selected_game)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM prediction_versions").fetchone()[0] == 0


def test_schedule_change_does_not_repoint_or_mutate_existing_version(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    store = PredictionVersionStore(tmp_path / "versions.sqlite", clock=clock)
    saved = publish(store, clock, request_id="00000000-0000-0000-0000-000000000091")
    changed = game(tipoff="2026-10-21T23:00:00Z")
    selection = store.select_current(changed)
    assert selection["status"] == "schedule_changed"
    assert selection["version"] is None
    assert selection["state_revision"] == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM prediction_versions").fetchone()[0] == 1
        assert db.execute(
            "SELECT current_version_id FROM game_prediction_state WHERE game_id=?",
            (saved["game_id"],),
        ).fetchone()[0] == saved["version_id"]


def test_reopening_store_preserves_locked_pointer(tmp_path):
    clock = Clock(datetime(2026, 9, 30, tzinfo=timezone.utc))
    path = tmp_path / "versions.sqlite"
    store = PredictionVersionStore(path, clock=clock)
    saved = publish(store, clock, request_id="00000000-0000-0000-0000-000000000081")
    clock.value = datetime.fromisoformat(game()["tipoff"])
    store.select_current(game())

    reopened = PredictionVersionStore(path, clock=clock)
    locked = reopened.select_current(game())
    assert locked["status"] == "locked"
    assert locked["version_id"] == saved["version_id"]
    assert locked["state_revision"] == 2
