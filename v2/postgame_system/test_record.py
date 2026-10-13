from __future__ import annotations

import json

from .identity import IdentityRegistry
from .learning import build_learning_record, export_learning_records
from .record import build_postgame_record
from .refresh import CachedRawClient, refresh_postgame


def refreshed_case(tmp_path):
    registry = IdentityRegistry(tmp_path / "identities.json")
    identity = registry.attach_nba_game(
        {
            "game_id": "0022500564",
            "game_date": "2026-01-13",
            "scheduled_tipoff": "2026-01-14T00:30:00+00:00",
            "home_team_id": 1610612748,
            "away_team_id": 1610612756,
        }
    )
    refresh = refresh_postgame(
        identity.nba_game_id,
        registry=registry,
        client=CachedRawClient(),
        output_root=tmp_path / "refresh",
    )
    return identity, refresh


def test_deterministic_record_requires_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    identity, refresh = refreshed_case(tmp_path)
    record = build_postgame_record(
        refresh.bundle_path,
        historical_audit_path=(
            "v2/outputs/game_0022500564_prediction_audit.json"
        ),
        detailed_facts_path="v2/outputs/game_0022500564_facts.json",
        output_root=tmp_path / "records",
    )

    assert record["game"]["final_score"] == {"PHX": 121, "MIA": 127}
    assert 3 <= len(record["key_factors"]) <= 5
    assert record["prediction_review"]["source_type"] == "historical_replay"
    assert record["prediction_review"]["basketball_only"]["errors"]["absolute_margin_error"] > 0
    assert record["identity"]["identity_hash"] == identity.identity_hash
    assert record["record_hash"]


def test_missing_enhancements_keep_basic_record(tmp_path):
    _, refresh = refreshed_case(tmp_path)
    bundle = json.loads(refresh.bundle_path.read_text(encoding="utf-8"))
    bundle["play_by_play"] = []
    bundle["player_boxscores"] = []
    bundle["shots"] = []
    bundle["components"].update(
        play_by_play="missing", player_boxscores="missing", shots="missing"
    )
    record = build_postgame_record(bundle, output_root=tmp_path / "records")

    assert record["status"] == "basic"
    assert {row["component"] for row in record["missing_data"]} >= {
        "play_by_play",
        "player_boxscores",
        "shots",
    }
    assert len(record["key_factors"]) >= 3
    assert record["prediction_review"]["source_type"] == "prediction_unavailable"


def test_learning_record_preserves_prediction_and_actual_targets(tmp_path):
    _, refresh = refreshed_case(tmp_path)
    record = build_postgame_record(
        refresh.bundle_path,
        historical_audit_path="v2/outputs/game_0022500564_prediction_audit.json",
        detailed_facts_path="v2/outputs/game_0022500564_facts.json",
        output_root=tmp_path / "records",
    )
    learning = build_learning_record(record, output_root=tmp_path / "learning")
    assert learning["actual"]["home_margin"] == 6
    assert learning["predictions"]["basketball_only"]["expected_total"]
    assert learning["eligible_for_error_evaluation"]
    assert not learning["eligible_for_candidate_training"]
    exported = export_learning_records(tmp_path / "learning", tmp_path / "candidate.jsonl")
    assert exported == 0
    assert (tmp_path / "candidate.jsonl").read_text(encoding="utf-8") == ""


def test_official_prediction_identity_mismatch_is_rejected(tmp_path):
    _, refresh = refreshed_case(tmp_path)
    official = {
        "status": "official_pregame_final",
        "game_id": "espn:wrong",
        "public_prediction": {
            "win_probability": 0.5,
            "expected_margin": 0.0,
            "expected_total": 220.0,
        },
    }
    import pytest

    with pytest.raises(ValueError, match="prediction_identity_mismatch"):
        build_postgame_record(
            refresh.bundle_path,
            official_prediction=official,
            output_root=tmp_path / "records",
        )


def test_official_feature_snapshot_is_copied_into_learning_record(tmp_path):
    _, refresh = refreshed_case(tmp_path)
    inputs = {
        "game": {"game_id": "0022500564"},
        "features": {
            "win_probability": {"elo_diff": 15.0},
            "expected_margin": {"elo_diff": 15.0},
            "expected_total": {"home_pace_roll5": 99.0},
        },
    }
    input_path = tmp_path / "snapshot" / "inputs.json"
    input_path.parent.mkdir()
    input_path.write_text(json.dumps(inputs), encoding="utf-8")
    from v2.pregame_system.storage import content_hash

    prediction = {
        "win_probability": 0.55,
        "expected_margin": 1.0,
        "expected_total": 230.0,
    }
    official = {
        "status": "official_pregame_final",
        "game_id": "0022500564",
        "public_prediction": prediction,
        "basketball_only_prediction": prediction,
        "market_informed_prediction": None,
        "market_usage": {"status": "unavailable"},
        "report": {
            "prediction": prediction,
            "snapshot": {"input_hash": content_hash(inputs), "model_version": "fixture-model"},
            "evidence": [
                {"section": "prediction", "value": {"path": str(input_path.parent)}}
            ],
            "sources": [],
            "market": None,
        },
    }
    record = build_postgame_record(
        refresh.bundle_path,
        official_prediction=official,
        output_root=tmp_path / "records",
    )
    learning = build_learning_record(record, output_root=tmp_path / "learning")
    assert learning["pregame_features"] == inputs["features"]
    assert learning["feature_input_hash"] == content_hash(inputs)
    assert learning["model_version"] == "fixture-model"
    assert learning["eligible_for_candidate_training"]
