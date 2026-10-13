"""Read-only regression anchors for the published LAL @ GSW EARLY version."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from .prediction_versions import DEFAULT_DATABASE
from .ats import spread_comparison_for_version
from .editorial_contract import build_editorial_projection, model_view


VERSION_ID = "ffc2947f-ae38-424c-894f-c2f05d71c3d6"


@pytest.fixture
def frozen_lal_gsw():
    if not DEFAULT_DATABASE.is_file():
        pytest.skip("formal local Prediction Version database is not present")
    uri = DEFAULT_DATABASE.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        row = db.execute("SELECT payload_json,payload_sha256 FROM prediction_versions WHERE version_id=?",
                         (VERSION_ID,)).fetchone()
    if row is None:
        pytest.skip("frozen LAL @ GSW version is not in this checkout")
    payload_json, stored_hash = row
    assert hashlib.sha256(payload_json.encode("utf-8")).hexdigest() == stored_hash
    return json.loads(payload_json), stored_hash


def test_frozen_lal_gsw_anchor_keeps_actual_flip_market_timing_and_legacy_protocol(frozen_lal_gsw):
    payload, stored_hash = frozen_lal_gsw
    assert payload["version_id"] == VERSION_ID
    assert payload["game_id"] == "espn:401898390"
    assert payload["stage"] == "EARLY_PREVIEW"
    assert payload["editorial_projection"]["protocol"] == "editorial-publication-v1"
    assert payload["prediction"] == {
        "winner_side": "home", "win_probability": .55,
        "expected_margin": 3.0, "expected_total": 223.5,
        "evidence_ids": payload["judgment"]["final_prediction"]["evidence_ids"],
    }
    market = payload["market_collection"]["public_market_reference"]
    assert market["home_spread"] == -4.5 and market["total"] == 220.5
    assert market["published_at"] is None and market["observed_at"]

    root = Path(__file__).resolve().parents[2]
    artifact = Path(payload["provenance"]["forecast_artifact"]["path"])
    if not artifact.is_absolute():
        artifact = root / artifact
    independent = json.loads((artifact / "independent.json").read_text(encoding="utf-8"))["prediction_and_basis"]
    final = json.loads((artifact / "final.json").read_text(encoding="utf-8"))
    assert independent["prediction"] == {
        "winner_side": "away", "win_probability": .56,
        "expected_home_margin": -2.5, "expected_total": 224.5,
    }
    assert final["final_prediction"] == {
        "winner_side": "home", "win_probability": .55,
        "expected_home_margin": 3.0, "expected_total": 223.5,
    }
    assert final["schema_version"] == "two-stage-forecast-v1"
    assert any("同样公开信息" in factor["summary"] for factor in final["review"]["factors"])
    assert "Pure给222.8" in payload["judgment"]["analysis_article"]

    # Read-only legacy adapter derives ATS from the frozen quote and the
    # existing forecast artifact; it does not rewrite the historical payload.
    ats = spread_comparison_for_version(payload)
    assert ats["market"]["home_spread"] == -4.5
    assert ats["market"]["market_implied_home_margin"] == 4.5
    assert ats["independent"]["model_home_margin"] == -2.5
    assert ats["independent"]["spread_edge"] == -7.0
    assert ats["independent"]["ats_lean_reference"]["side"] == "away"
    assert ats["independent"]["ats_lean_reference"]["line"] == 4.5
    assert ats["final"]["model_home_margin"] == 3.0
    assert ats["final"]["spread_edge"] == -1.5
    assert ats["final"]["ats_lean_reference"]["side"] == "away"
    assert ats["final"]["ats_lean_reference"]["line"] == 4.5

    # This is a read-only regression check: the current row remains byte-for-byte
    # hash-bound to the historical version, not migrated or rewritten in place.
    with sqlite3.connect(DEFAULT_DATABASE.resolve().as_uri() + "?mode=ro", uri=True) as db:
        after = db.execute("SELECT payload_sha256 FROM prediction_versions WHERE version_id=?",
                           (VERSION_ID,)).fetchone()[0]
    assert after == stored_hash


def test_frozen_lal_gsw_values_are_explicit_in_current_writer_market_view(frozen_lal_gsw):
    payload, stored_hash = frozen_lal_gsw
    root = Path(__file__).resolve().parents[2]
    artifact = Path(payload["provenance"]["forecast_artifact"]["path"])
    if not artifact.is_absolute():
        artifact = root / artifact
    forecast = json.loads((artifact / "final.json").read_text(encoding="utf-8"))
    comparison = spread_comparison_for_version(payload)
    locked_prediction = payload["judgment"]["final_prediction"]
    forecast_provenance = {
        "analysis_core": forecast.get("final_analysis_core") or {},
        "independent_prediction": forecast["independent"]["prediction"],
        "final_prediction": forecast["final_prediction"],
        "revision_delta": forecast.get("revision_delta") or {},
        "market_provenance": forecast.get("market_provenance") or {},
        # The immutable V1 forecast artifact predates the persisted comparison
        # field; use the existing read-only adapter rather than rewriting it.
        "spread_comparison": comparison,
    }
    projection = build_editorial_projection(
        payload["analyst_context"], locked_prediction, forecast_provenance=forecast_provenance,
    )
    writer_view = model_view(projection)
    revision = next(fact["value"] for fact in writer_view["facts"]
                    if fact["kind"] == "REVISION_PROVENANCE")
    interpretation = revision["market_interpretation"]
    assert writer_view["locked_prediction"] == {
        "winner_side": "home", "win_probability": .55,
        "expected_margin": 3.0, "expected_total": 223.5,
    }
    assert interpretation["spread"] == {
        "status": "available", "margin_perspective": "home_minus_away",
        "model_home_margin": 3.0, "market_implied_home_margin": 4.5,
        "spread_edge": -1.5, "ats_lean": "away",
        "ats_lean_reference": {"side": "away", "team_id": 1610612747,
                                "team_name": "湖人", "line": 4.5,
                                "display": "湖人 +4.5"},
        "market_quote_id": comparison["market"]["market_quote_id"],
    }
    assert interpretation["total"] == {
        "status": "available", "model_total": 223.5, "market_total": 220.5,
        "total_edge": 3.0, "total_lean": "over",
        "market_quote_id": comparison["market"]["market_quote_id"],
    }
    assert "Pure" not in json.dumps(writer_view, ensure_ascii=False)

    # Projection is an in-memory Writer view only; the released historical payload remains untouched.
    with sqlite3.connect(DEFAULT_DATABASE.resolve().as_uri() + "?mode=ro", uri=True) as db:
        after = db.execute("SELECT payload_sha256 FROM prediction_versions WHERE version_id=?",
                           (VERSION_ID,)).fetchone()[0]
    assert after == stored_hash
