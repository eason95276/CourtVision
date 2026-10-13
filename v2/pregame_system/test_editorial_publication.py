"""Factual publication receipt, optional QA and immutable historical rows."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from .prediction_versions import VERSION_SCHEMA, PredictionVersionStore, VersionStoreError, payload_sha256
from .test_analyst import EVIDENCE, LAYERS, judgment_payload
from .analyst import build_analyst_context
from .editorial_contract import build_editorial_projection, make_publication_receipt


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 1, tzinfo=timezone.utc)
    def __call__(self):
        return self.value


def game(game_id="espn:401909089"):
    return {"id": game_id, "status": "scheduled", "tipoff": "2026-10-20T23:00:00Z",
            "home": {"id": 1610612752, "name": "Knicks", "name_zh": "尼克斯"},
            "away": {"id": 1610612755, "name": "76ers", "name_zh": "76人"}}


def candidate(store, clock, selected_game=None):
    g = selected_game or game()
    request = store.begin_request(g)
    clock.value += timedelta(seconds=3)
    final = {"winner_side": "home", "win_probability": 0.63, "expected_margin": 6.5,
             "expected_total": 224.5, "evidence_ids": ["evidence-prediction"]}
    context = build_analyst_context(LAYERS, EVIDENCE, missing_data=[],
        analysis_request="生成这场比赛的第25秒赛前内容。",
        game={"game_id": g["id"], "home": g["home"], "away": g["away"],
              "scheduled_tipoff": g["tipoff"], "cutoff": request["requested_at"]})
    judgment = judgment_payload()
    judgment["winner_lean"] = "home"
    judgment["final_prediction"] = copy.deepcopy(final)
    judgment["analysis_article"] = "我更看重这场比赛的半场对位，主队的球权分配会决定比赛节奏。"
    judgment["analysis_article_evidence_ids"] = ["evidence-form"]
    projection = build_editorial_projection(context, final, [])
    payload = {"game_id": g["id"], "game_identity": request["game_identity"],
        "request_id": request["request_id"], "requested_at": request["requested_at"],
        "input_cutoff_at": request["requested_at"], "input_frozen_at": (clock.value-timedelta(seconds=2)).isoformat(),
        "generated_at": (clock.value-timedelta(seconds=1)).isoformat(), "input_snapshot": g,
        "analysis_contract_version": "two-stage-forecast-v1", "context_hash": context.context_hash,
        "analyst_context": context.model_dump(), "objective_evidence": copy.deepcopy(context.objective_evidence),
        "editorial_material": [], "editorial_projection": projection, "prediction": final, "judgment": judgment}
    payload["publication_receipt"] = make_publication_receipt(judgment, context, projection, [], [], {
        "status": "disabled", "warnings": [], "possible_hard_fact_conflicts": []})
    return g, request, payload


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    return PredictionVersionStore(tmp_path / "publication.sqlite", clock=clock), clock


def assert_unpublished(store, g):
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM prediction_versions").fetchone()[0] == 0
    selection = store.select_current(g)
    assert selection["status"] == "unpublished" and selection["version_id"] is None


def test_new_publication_requires_factual_receipt_and_full_schema(setup):
    store, clock = setup
    g, request, payload = candidate(store, clock)
    payload.pop("publication_receipt")
    with pytest.raises(VersionStoreError, match="publication_receipt_missing"):
        store.publish(g, request, payload)
    assert_unpublished(store, g)


@pytest.mark.parametrize("status", ["warning", "hold", "unavailable", "disabled"])
def test_quality_feedback_never_blocks_valid_publication(setup, status):
    store, clock = setup
    g, request, payload = candidate(store, clock)
    quality = {"status": status, "warnings": ["Optional style note."], "possible_hard_fact_conflicts": []}
    payload["publication_receipt"] = make_publication_receipt(payload["judgment"], payload["analyst_context"],
        payload["editorial_projection"], payload["editorial_material"], [], quality)
    saved = store.publish(g, request, payload)
    assert store.select_current(g)["version_id"] == saved["version_id"]
    assert saved["publication_receipt"]["status"] == "eligible"
    assert saved["publication_receipt"]["quality_review"]["status"] == status


def test_explicit_structured_hard_fact_conflict_blocks_store_publication(setup):
    store, clock = setup
    g, request, payload = candidate(store, clock)
    c = payload["analyst_context"]
    c["season_form"] = {"season_start": 2026, "teams": {"away": {
        "previous_last10": {"status": "available", "games": 10, "points_against_per_game": 128.6}}}}
    payload["objective_evidence"] = copy.deepcopy(c["objective_evidence"])
    payload["judgment"]["analysis_article"] = "热火上赛季近15场场均失分128.6。"
    payload["editorial_projection"] = build_editorial_projection(c, payload["prediction"], [])
    payload["publication_receipt"] = make_publication_receipt(payload["judgment"], c,
        payload["editorial_projection"], [], [], {"status": "warning", "warnings": [], "possible_hard_fact_conflicts": []})
    with pytest.raises(VersionStoreError, match="publication_hard_fact_blocker"):
        store.publish(g, request, payload)
    assert_unpublished(store, g)


@pytest.mark.parametrize("changed", ["article", "prediction", "context", "evidence", "projection", "quality",
                                    "quality_review_input"])
def test_receipt_hashes_bind_candidate_and_feedback(setup, changed):
    store, clock = setup
    g, request, payload = candidate(store, clock)
    if changed == "article":
        payload["judgment"]["analysis_article"] += "新增一句。"
    elif changed == "prediction":
        payload["prediction"]["expected_total"] += 1
        payload["judgment"]["final_prediction"]["expected_total"] += 1
    elif changed == "context":
        payload["analyst_context"]["missing_data"].append({"section": "new", "reason": "changed"})
    elif changed == "evidence":
        payload["objective_evidence"][0]["value"]["changed"] = True
    elif changed == "projection":
        payload["editorial_projection"]["facts"].append({"id": "extra"})
    elif changed == "quality_review_input":
        payload["quality_review_input_sha256"] = "0" * 64
    else:
        payload["publication_receipt"]["quality_review"]["warnings"] = ["tampered"]
    with pytest.raises(VersionStoreError, match="publication_receipt_binding_mismatch|publication_quality_input_binding_mismatch|editorial_projection_mismatch|publication_evidence_mismatch|editorial_forecast_locked_violation"):
        store.publish(g, request, payload)
    assert_unpublished(store, g)


def test_publication_uses_deep_copied_snapshot_under_caller_mutation(setup, monkeypatch):
    from . import editorial_contract
    store, clock = setup
    g, request, payload = candidate(store, clock)
    original = editorial_contract.verify_publication_receipt
    article = payload["judgment"]["analysis_article"]
    def verify_then_mutate(candidate_payload):
        original(candidate_payload)
        payload["judgment"]["analysis_article"] = "late caller mutation"
        payload["analyst_context"]["objective_evidence"].clear()
    monkeypatch.setattr(editorial_contract, "verify_publication_receipt", verify_then_mutate)
    saved = store.publish(g, request, payload)
    assert saved["judgment"]["analysis_article"] == article
    assert saved["analyst_context"]["objective_evidence"]


def test_receipt_from_another_game_cannot_publish(setup):
    store, clock = setup
    _, _, original = candidate(store, clock)
    other, request, payload = candidate(store, clock, game("espn:401909088"))
    payload["publication_receipt"] = copy.deepcopy(original["publication_receipt"])
    payload["editorial_projection"] = copy.deepcopy(original["editorial_projection"])
    with pytest.raises(VersionStoreError):
        store.publish(other, request, payload)
    assert_unpublished(store, other)


def test_old_saved_version_remains_readable_and_lockable_without_new_receipt(setup):
    store, clock = setup
    g, request, payload = candidate(store, clock)
    version_id = "10000000-0000-0000-0000-000000000001"
    body = {**payload, "schema_version": VERSION_SCHEMA, "version_id": version_id,
            "request_seq": request["request_seq"], "schedule_revision": request["schedule_revision"],
            "published_at": clock.value.isoformat()}
    body.pop("publication_receipt")
    body.pop("editorial_projection")
    body.pop("editorial_material")
    frozen = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = payload_sha256(body)
    with store.connect() as db:
        db.execute("""INSERT INTO prediction_versions
            (version_id,schema_version,game_id,request_id,request_seq,requested_at,
             input_cutoff_at,generated_at,published_at,payload_json,payload_sha256)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (version_id, VERSION_SCHEMA, g["id"], request["request_id"],
             request["request_seq"], request["requested_at"], body["input_cutoff_at"],
             body["generated_at"], body["published_at"], frozen, digest))
        db.execute("UPDATE game_prediction_state SET current_version_id=?,state_revision=1 WHERE game_id=?",
                   (version_id, g["id"]))
    current = store.select_current(g)
    assert current["status"] == "current" and current["version_id"] == version_id
    clock.value = datetime(2026, 10, 20, 23, 0, 1, tzinfo=timezone.utc)
    assert store.select_current(g)["status"] == "locked"
    with store.connect() as db:
        row = db.execute("SELECT payload_json,payload_sha256 FROM prediction_versions WHERE version_id=?", (version_id,)).fetchone()
    assert tuple(row) == (frozen, digest)
