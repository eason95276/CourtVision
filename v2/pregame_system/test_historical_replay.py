from __future__ import annotations

import copy
import hashlib
import json

import pytest

from .historical_replay import (
    ReplayError,
    _read_snapshot,
    main,
    run_replay,
)


GAME_ID = "fixture:game-a"
CUTOFF = "2026-01-01T12:30:00Z"


def make_snapshot(
    *,
    game_id: str = GAME_ID,
    cutoff: str = CUTOFF,
) -> dict:
    snapshot = {
        "schema_version": "pregame-replay-snapshot-v1",
        "capture_kind": "point_in_time_pregame",
        "snapshot_id": f"snapshot-{game_id}-{cutoff}",
        "game": {
            "game_id": game_id,
            "scheduled_tipoff": "2026-01-01T19:00:00Z",
            "home_team_id": 100,
            "away_team_id": 200,
            "home_team": "Home Team",
            "away_team": "Away Team",
        },
        "cutoff": cutoff,
        "captured_at": "2026-01-01T12:20:00Z",
        "source_records": [
            {
                "source_id": "form-snapshot",
                "captured_at": "2026-01-01T12:20:00Z",
                "published_at": "2026-01-01T11:45:00Z",
            }
        ],
        "pregame_inputs": {"team_form": {"home_recent_wins": 3, "away_recent_wins": 2}},
        "prediction_created_at": "2026-01-01T12:25:00Z",
        "prediction_version": "official-v1",
        "prediction_source": "saved_official_numeric",
        "predictions": {
            "pure": {
                "winner_team_id": 100,
                "predicted_home_margin": 5.0,
                "predicted_total": 218.0,
            },
            "market": {
                "winner_team_id": 200,
                "predicted_home_margin": -1.5,
                "predicted_total": 216.5,
            },
            "official_numeric": {
                "winner_team_id": 100,
                "predicted_home_margin": 4.5,
                "predicted_total": 220.0,
                "home_win_probability": 0.7,
            },
        },
    }
    snapshot["pregame_inputs_sha256"] = hashlib.sha256(
        json.dumps(
            snapshot["pregame_inputs"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return snapshot


def actual(game_id: str = GAME_ID) -> dict:
    return {
        "game_id": game_id,
        "home_team_id": 100,
        "away_team_id": 200,
        "home_score": 111,
        "away_score": 104,
    }


def test_evaluation_reads_actual_only_after_prediction_and_keeps_layers_separate():
    events: list[str] = []

    def predictor(request):
        events.append("predict")
        assert request.game_id == GAME_ID
        assert request.cutoff == "2026-01-01T12:30:00+00:00"
        with pytest.raises(TypeError):
            request.pregame_inputs["new"] = "mutation"
        return {
            "prediction_version": "offline-model-7",
            "prediction_source": "deterministic_fixture_adapter",
            "predictions": {
                "official_numeric": {
                    "winner_team_id": 100,
                    "predicted_home_margin": 4.5,
                    "predicted_total": 220,
                    "home_win_probability": 0.7,
                },
                "pure": {
                    "winner_team_id": 100,
                    "predicted_home_margin": 5,
                    "predicted_total": 218,
                },
            },
        }

    def load_actual(game_id: str):
        events.append("outcome")
        return actual(game_id)

    report = run_replay(
        make_snapshot(),
        game_id=GAME_ID,
        cutoff=CUTOFF,
        predictor=predictor,
        actual_provider=load_actual,
    )

    assert events == ["predict", "outcome"]
    assert report["evaluation_type"] == "deterministic_replay_adapter"
    assert report["prediction_version"] == "offline-model-7"
    assert report["cutoff"] == "2026-01-01T12:30:00+00:00"
    assert report["actual"]["actual_home_margin"] == 7
    assert report["actual"]["actual_total"] == 215
    assert report["metrics"]["official_numeric"]["winner"]["correct"] is True
    assert report["metrics"]["official_numeric"]["margin"]["absolute_error"] == 2.5
    assert report["metrics"]["official_numeric"]["total"]["absolute_error"] == 5
    assert report["metrics"]["official_numeric"]["probability"]["brier_score"] == pytest.approx(0.09)
    assert set(report["metrics"]) == {"official_numeric", "pure"}


def test_saved_forecast_evaluation_requires_forecast_created_before_cutoff():
    events: list[str] = []
    snapshot = make_snapshot()
    snapshot["prediction_created_at"] = "2026-01-01T12:31:00Z"

    with pytest.raises(ReplayError, match="saved_prediction_created_after_cutoff"):
        run_replay(
            snapshot,
            game_id=GAME_ID,
            cutoff=CUTOFF,
            actual_provider=lambda game_id: events.append("outcome") or actual(game_id),
        )
    assert events == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("captured_at", "2026-01-01T12:31:00Z", "snapshot_captured_after_cutoff"),
        ("source_capture", "2026-01-01T12:31:00Z", "source_captured_after_cutoff"),
        ("source_publish", "2026-01-01T12:31:00Z", "source_published_after_cutoff"),
    ],
)
def test_cutoff_rejects_later_snapshot_or_source_data(field, value, reason):
    snapshot = make_snapshot()
    if field == "captured_at":
        snapshot["captured_at"] = value
    elif field == "source_capture":
        snapshot["source_records"][0]["captured_at"] = value
    else:
        snapshot["source_records"][0]["published_at"] = value

    with pytest.raises(ReplayError, match=reason):
        run_replay(
            snapshot,
            game_id=GAME_ID,
            cutoff=CUTOFF,
            actual_provider=lambda _: pytest.fail("actual result read before cutoff validation"),
        )


def test_snapshot_requires_exact_game_and_cutoff_and_never_falls_back():
    snapshot = make_snapshot()
    with pytest.raises(ReplayError, match="snapshot_game_id_mismatch"):
        run_replay(
            snapshot,
            game_id="fixture:game-b",
            cutoff=CUTOFF,
            actual_provider=lambda _: pytest.fail("actual result must not be read"),
        )
    with pytest.raises(ReplayError, match="snapshot_cutoff_mismatch"):
        run_replay(
            snapshot,
            game_id=GAME_ID,
            cutoff="2026-01-01T12:00:00Z",
            actual_provider=lambda _: pytest.fail("actual result must not be read"),
        )
    with pytest.raises(ReplayError, match="historical_snapshot_not_point_in_time"):
        run_replay(
            {**snapshot, "capture_kind": "retrospectively_acquired"},
            game_id=GAME_ID,
            cutoff=CUTOFF,
            actual_provider=lambda _: pytest.fail("actual result must not be read"),
        )


def test_postgame_target_row_cannot_enter_pregame_inputs():
    snapshot = make_snapshot()
    snapshot["pregame_inputs"]["target_game"] = {
        "game_id": GAME_ID,
        "homeScore": 111,
        "awayScore": 104,
    }
    snapshot["pregame_inputs_sha256"] = hashlib.sha256(
        json.dumps(
            snapshot["pregame_inputs"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    with pytest.raises(ReplayError, match="target_outcome_in_pregame_inputs"):
        run_replay(
            snapshot,
            game_id=GAME_ID,
            cutoff=CUTOFF,
            actual_provider=lambda _: pytest.fail("actual result must not be read"),
        )


def test_game_b_cannot_receive_game_a_outcome():
    snapshot = make_snapshot(game_id="fixture:game-b")
    with pytest.raises(ReplayError, match="actual_result_game_id_mismatch"):
        run_replay(
            snapshot,
            game_id="fixture:game-b",
            cutoff=CUTOFF,
            actual_provider=lambda _: actual("fixture:game-a"),
        )


def test_different_cutoffs_are_independent_and_do_not_share_prediction_state():
    cutoffs = ["2026-01-01T12:00:00Z", "2026-01-01T12:30:00Z"]
    observed: list[str] = []
    reports = []

    for cutoff in cutoffs:
        snapshot = make_snapshot(cutoff=cutoff)
        capture_time = "2026-01-01T11:50:00Z" if cutoff.endswith("12:00:00Z") else "2026-01-01T12:20:00Z"
        publish_time = "2026-01-01T11:30:00Z" if cutoff.endswith("12:00:00Z") else "2026-01-01T11:45:00Z"
        prediction_time = "2026-01-01T11:55:00Z" if cutoff.endswith("12:00:00Z") else "2026-01-01T12:25:00Z"
        snapshot["captured_at"] = capture_time
        snapshot["source_records"][0]["captured_at"] = capture_time
        snapshot["source_records"][0]["published_at"] = publish_time
        snapshot["prediction_created_at"] = prediction_time

        def predictor(request):
            observed.append(request.cutoff)
            return {
                "prediction_version": "offline-v1",
                "prediction_source": "test_adapter",
                "predictions": {
                    "official_numeric": {
                        "winner_team_id": 100,
                        "predicted_home_margin": 1,
                        "predicted_total": 210,
                    }
                },
            }

        reports.append(
            run_replay(
                snapshot,
                game_id=GAME_ID,
                cutoff=cutoff,
                predictor=predictor,
                actual_provider=actual,
            )
        )

    normalized_cutoffs = [value.replace("Z", "+00:00") for value in cutoffs]
    assert observed == normalized_cutoffs
    assert [report["cutoff"] for report in reports] == normalized_cutoffs


def test_actual_result_is_not_read_when_predictor_fails():
    events: list[str] = []

    def predictor(_):
        events.append("predict")
        raise RuntimeError("model failure")

    with pytest.raises(RuntimeError, match="model failure"):
        run_replay(
            make_snapshot(),
            game_id=GAME_ID,
            cutoff=CUTOFF,
            predictor=predictor,
            actual_provider=lambda _: events.append("outcome") or actual(),
        )
    assert events == ["predict"]


def test_missing_snapshot_fails_without_reading_current_games_or_results(tmp_path, capsys):
    missing = tmp_path / "missing-snapshot.json"
    results = tmp_path / "missing-results.json"
    assert main([
        "--game-id", GAME_ID,
        "--cutoff", CUTOFF,
        "--snapshot", str(missing),
        "--actual-results", str(results),
    ]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "unavailable",
        "reason": "pregame_snapshot_missing",
    }
    with pytest.raises(ReplayError, match="pregame_snapshot_missing"):
        _read_snapshot(missing)


def test_source_timestamps_require_timezone_information():
    snapshot = make_snapshot()
    snapshot["source_records"][0]["captured_at"] = "2026-01-01T12:20:00"
    with pytest.raises(ReplayError, match="source_record_0_captured_at_timezone_required"):
        run_replay(
            snapshot,
            game_id=GAME_ID,
            cutoff=CUTOFF,
            actual_provider=lambda _: pytest.fail("actual result must not be read"),
        )


def test_ai_layer_cannot_be_mixed_into_numeric_forecast_layers():
    snapshot = make_snapshot()
    snapshot["predictions"]["ai_final"] = copy.deepcopy(
        snapshot["predictions"]["official_numeric"]
    )
    with pytest.raises(ReplayError, match="prediction_layer_unknown"):
        run_replay(
            snapshot,
            game_id=GAME_ID,
            cutoff=CUTOFF,
            actual_provider=actual,
        )
