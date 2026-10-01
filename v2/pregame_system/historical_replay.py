"""Strict offline evaluation for archived pregame snapshots.

The module has no network client and never reads an outcome before prediction
finishes. Historical snapshots must carry point in time capture evidence. It
does not promote retrospectively acquired history into strict replay data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any


SCHEMA_VERSION = "pregame-replay-snapshot-v1"
PREDICTION_LAYERS = ("pure", "market", "residual", "official_numeric")
UTC = timezone.utc


class ReplayError(ValueError):
    """A replay input is absent, out of scope, or cannot be time verified."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ReplayInput:
    """Read only inputs exposed to an offline prediction adapter."""

    game_id: str
    cutoff: str
    game: Mapping[str, Any]
    pregame_inputs: Mapping[str, Any]
    snapshot_id: str


def _strict_instant(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReplayError(f"{field}_missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReplayError(f"{field}_timezone_required")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ReplayError(f"{field}_invalid")
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReplayError(f"{field}_invalid") from exc
    if str(value).strip() not in {str(converted), f"{converted}.0"}:
        raise ReplayError(f"{field}_invalid")
    return converted


def _finite_number(value: Any, field: str, *, required: bool) -> float | None:
    if value is None and not required:
        return None
    if isinstance(value, bool):
        raise ReplayError(f"{field}_invalid")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReplayError(f"{field}_invalid") from exc
    if not math.isfinite(number):
        raise ReplayError(f"{field}_invalid")
    return number


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _canonical_hash(value: Any, field: str) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReplayError(f"{field}_not_json_safe") from exc
    return hashlib.sha256(raw).hexdigest()


def pregame_inputs_sha256(pregame_inputs: Mapping[str, Any]) -> str:
    """Return the canonical hash required by the replay snapshot contract."""
    return _canonical_hash(pregame_inputs, "pregame_inputs")


def _target_outcome_in_inputs(value: Any, target_game_id: str) -> bool:
    result_keys = {
        "homescore", "awayscore", "home_score", "away_score", "final_score",
        "actual_score", "actual_margin", "actual_total", "actual_winner",
        "winner_team_id", "postgame_stats", "box_score", "boxscore",
    }
    if isinstance(value, Mapping):
        row_game_id = value.get("game_id", value.get("gameId"))
        if row_game_id is not None and str(row_game_id) == target_game_id:
            if any(str(key).casefold() in result_keys for key in value):
                return True
        return any(_target_outcome_in_inputs(item, target_game_id) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_target_outcome_in_inputs(item, target_game_id) for item in value)
    return False


def _validate_snapshot(
    snapshot: Mapping[str, Any],
    *,
    game_id: str,
    cutoff: str,
) -> tuple[dict[str, Any], str, str, str]:
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ReplayError("snapshot_schema_unsupported")
    if snapshot.get("capture_kind") != "point_in_time_pregame":
        raise ReplayError("historical_snapshot_not_point_in_time")

    game = snapshot.get("game")
    if not isinstance(game, Mapping):
        raise ReplayError("snapshot_game_identity_missing")
    stored_game_id = str(game.get("game_id") or "")
    if not stored_game_id or stored_game_id != str(game_id):
        raise ReplayError("snapshot_game_id_mismatch")

    requested_cutoff = _strict_instant(cutoff, "requested_cutoff")
    stored_cutoff = _strict_instant(snapshot.get("cutoff"), "snapshot_cutoff")
    if requested_cutoff != stored_cutoff:
        raise ReplayError("snapshot_cutoff_mismatch")

    tipoff = _strict_instant(game.get("scheduled_tipoff"), "scheduled_tipoff")
    if stored_cutoff >= tipoff:
        raise ReplayError("cutoff_not_pregame")
    captured_at = _strict_instant(snapshot.get("captured_at"), "snapshot_captured_at")
    if captured_at > stored_cutoff:
        raise ReplayError("snapshot_captured_after_cutoff")

    try:
        home_id = _as_int(game.get("home_team_id"), "home_team_id")
        away_id = _as_int(game.get("away_team_id"), "away_team_id")
    except ReplayError:
        raise
    if home_id == away_id:
        raise ReplayError("snapshot_team_identity_invalid")

    source_records = snapshot.get("source_records")
    if not isinstance(source_records, list) or not source_records:
        raise ReplayError("snapshot_source_provenance_missing")
    for index, record in enumerate(source_records):
        if not isinstance(record, Mapping):
            raise ReplayError(f"source_record_{index}_invalid")
        if not str(record.get("source_id") or "").strip():
            raise ReplayError(f"source_record_{index}_id_missing")
        _strict_instant(record.get("captured_at"), f"source_record_{index}_captured_at")
        source_capture = _strict_instant(
            record.get("captured_at"), f"source_record_{index}_captured_at"
        )
        if source_capture > stored_cutoff:
            raise ReplayError("source_captured_after_cutoff")
        published_at = record.get("published_at")
        if published_at is not None:
            if _strict_instant(published_at, f"source_record_{index}_published_at") > stored_cutoff:
                raise ReplayError("source_published_after_cutoff")

    pregame_inputs = snapshot.get("pregame_inputs")
    if not isinstance(pregame_inputs, Mapping):
        raise ReplayError("pregame_inputs_missing")
    input_hash = pregame_inputs_sha256(pregame_inputs)
    if snapshot.get("pregame_inputs_sha256") != input_hash:
        raise ReplayError("pregame_inputs_hash_mismatch")
    if _target_outcome_in_inputs(pregame_inputs, stored_game_id):
        raise ReplayError("target_outcome_in_pregame_inputs")

    normalized_game = {
        "game_id": stored_game_id,
        "scheduled_tipoff": _iso(tipoff),
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_team": str(game.get("home_team") or ""),
        "away_team": str(game.get("away_team") or ""),
    }
    if not normalized_game["home_team"] or not normalized_game["away_team"]:
        raise ReplayError("snapshot_team_names_missing")
    snapshot_id = str(snapshot.get("snapshot_id") or "")
    if not snapshot_id:
        raise ReplayError("snapshot_id_missing")
    return normalized_game, _iso(stored_cutoff), snapshot_id, input_hash


def _normalize_predictions(
    value: Any,
    *,
    home_team_id: int,
    away_team_id: int,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or "official_numeric" not in value:
        raise ReplayError("official_prediction_missing")
    if any(str(layer) not in PREDICTION_LAYERS for layer in value):
        raise ReplayError("prediction_layer_unknown")
    normalized: dict[str, dict[str, Any]] = {}
    for layer, raw in value.items():
        if not isinstance(raw, Mapping):
            raise ReplayError(f"prediction_layer_{layer}_invalid")
        margin_value = raw.get("predicted_home_margin", raw.get("expected_home_margin"))
        total_value = raw.get("predicted_total", raw.get("expected_total"))
        margin = _finite_number(margin_value, f"{layer}_predicted_home_margin", required=layer == "official_numeric")
        total = _finite_number(total_value, f"{layer}_predicted_total", required=layer == "official_numeric")
        winner_value = raw.get("winner_team_id")
        winner = _as_int(winner_value, f"{layer}_winner_team_id") if winner_value is not None else None
        if winner is not None and winner not in {home_team_id, away_team_id}:
            raise ReplayError(f"{layer}_winner_team_id_mismatch")
        if winner is None and margin is not None and margin != 0:
            winner = home_team_id if margin > 0 else away_team_id
        probability = _finite_number(
            raw.get("home_win_probability"),
            f"{layer}_home_win_probability",
            required=False,
        )
        if probability is not None and not 0 <= probability <= 1:
            raise ReplayError(f"{layer}_home_win_probability_out_of_range")
        if layer == "official_numeric" and winner is None:
            raise ReplayError("official_winner_missing")
        normalized[str(layer)] = {
            "winner_team_id": winner,
            "predicted_home_margin": margin,
            "predicted_total": total,
            "home_win_probability": probability,
        }
    return normalized


def _validate_prediction_timestamp(value: Any, cutoff: str) -> str:
    prediction_time = _strict_instant(value, "prediction_timestamp")
    if prediction_time > _strict_instant(cutoff, "snapshot_cutoff"):
        raise ReplayError("saved_prediction_created_after_cutoff")
    return _iso(prediction_time)


def _actual_result(
    raw: Any,
    *,
    game: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ReplayError("actual_result_missing")
    if str(raw.get("game_id") or "") != str(game["game_id"]):
        raise ReplayError("actual_result_game_id_mismatch")
    if _as_int(raw.get("home_team_id"), "actual_home_team_id") != game["home_team_id"]:
        raise ReplayError("actual_result_home_team_mismatch")
    if _as_int(raw.get("away_team_id"), "actual_away_team_id") != game["away_team_id"]:
        raise ReplayError("actual_result_away_team_mismatch")
    home_score = _as_int(raw.get("home_score"), "actual_home_score")
    away_score = _as_int(raw.get("away_score"), "actual_away_score")
    if home_score < 0 or away_score < 0 or home_score == away_score:
        raise ReplayError("actual_result_score_invalid")
    winner = game["home_team_id"] if home_score > away_score else game["away_team_id"]
    return {
        "game_id": game["game_id"],
        "home_team_id": game["home_team_id"],
        "away_team_id": game["away_team_id"],
        "home_score": home_score,
        "away_score": away_score,
        "winner_team_id": winner,
        "actual_home_margin": home_score - away_score,
        "actual_total": home_score + away_score,
    }


def _metrics(
    predictions: Mapping[str, Mapping[str, Any]],
    actual: Mapping[str, Any],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    actual_home_win = 1.0 if actual["winner_team_id"] == actual["home_team_id"] else 0.0
    for layer, prediction in predictions.items():
        predicted_margin = prediction.get("predicted_home_margin")
        predicted_total = prediction.get("predicted_total")
        probability = prediction.get("home_win_probability")
        output[layer] = {
            "winner": {
                "predicted_team_id": prediction.get("winner_team_id"),
                "actual_team_id": actual["winner_team_id"],
                "correct": (
                    prediction.get("winner_team_id") == actual["winner_team_id"]
                    if prediction.get("winner_team_id") is not None
                    else None
                ),
            },
            "margin": {
                "predicted_home_margin": predicted_margin,
                "actual_home_margin": actual["actual_home_margin"],
                "absolute_error": (
                    abs(predicted_margin - actual["actual_home_margin"])
                    if predicted_margin is not None else None
                ),
            },
            "total": {
                "predicted_total": predicted_total,
                "actual_total": actual["actual_total"],
                "absolute_error": (
                    abs(predicted_total - actual["actual_total"])
                    if predicted_total is not None else None
                ),
            },
            "probability": None,
        }
        if probability is not None:
            clipped = min(max(probability, 1e-15), 1 - 1e-15)
            output[layer]["probability"] = {
                "home_win_probability": probability,
                "brier_score": (probability - actual_home_win) ** 2,
                "log_loss": -(
                    actual_home_win * math.log(clipped)
                    + (1 - actual_home_win) * math.log(1 - clipped)
                ),
            }
    return output


def run_replay(
    snapshot: Mapping[str, Any],
    *,
    game_id: str,
    cutoff: str,
    actual_provider: Callable[[str], Mapping[str, Any] | None],
    predictor: Callable[[ReplayInput], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Predict from a verified pregame snapshot, then load the actual result.

    The outcome provider is never passed to the predictor and is called only
    after prediction output has been validated.
    """
    game, normalized_cutoff, snapshot_id, input_hash = _validate_snapshot(
        snapshot, game_id=game_id, cutoff=cutoff
    )
    if predictor is None:
        predictions = _normalize_predictions(
            snapshot.get("predictions"),
            home_team_id=game["home_team_id"],
            away_team_id=game["away_team_id"],
        )
        prediction_timestamp = _validate_prediction_timestamp(
            snapshot.get("prediction_created_at"), normalized_cutoff
        )
        prediction_version = str(snapshot.get("prediction_version") or "")
        prediction_source = str(snapshot.get("prediction_source") or "")
        if not prediction_version or not prediction_source:
            raise ReplayError("prediction_provenance_missing")
        evaluation_type = "saved_forecast_evaluation"
        replay_started_at = None
    else:
        request = ReplayInput(
            game_id=game["game_id"],
            cutoff=normalized_cutoff,
            game=_deep_freeze(game),
            pregame_inputs=_deep_freeze(snapshot["pregame_inputs"]),
            snapshot_id=snapshot_id,
        )
        replay_started_at = _iso(datetime.now(UTC))
        predicted = predictor(request)
        if not isinstance(predicted, Mapping):
            raise ReplayError("predictor_output_invalid")
        predictions = _normalize_predictions(
            predicted.get("predictions"),
            home_team_id=game["home_team_id"],
            away_team_id=game["away_team_id"],
        )
        prediction_version = str(predicted.get("prediction_version") or "")
        prediction_source = str(predicted.get("prediction_source") or "")
        if not prediction_version or not prediction_source:
            raise ReplayError("prediction_provenance_missing")
        prediction_timestamp = str(predicted.get("prediction_timestamp") or replay_started_at)
        _strict_instant(prediction_timestamp, "prediction_timestamp")
        evaluation_type = "deterministic_replay_adapter"

    raw_actual = actual_provider(game["game_id"])
    actual = _actual_result(raw_actual, game=game)
    return {
        "schema_version": "historical-replay-evaluation-v1",
        "status": "evaluated",
        "evaluation_type": evaluation_type,
        "snapshot_id": snapshot_id,
        "pregame_inputs_sha256": input_hash,
        "game": game,
        "cutoff": normalized_cutoff,
        "prediction_timestamp": prediction_timestamp,
        "prediction_version": prediction_version,
        "prediction_source": prediction_source,
        "replay_started_at": replay_started_at,
        "predictions": predictions,
        "actual": actual,
        "metrics": _metrics(predictions, actual),
    }


class JsonActualResultProvider:
    """Read a result file lazily, after the predictor has returned."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def __call__(self, game_id: str) -> Mapping[str, Any] | None:
        if not self.path.is_file():
            raise ReplayError("actual_results_file_missing")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReplayError("actual_results_file_invalid") from exc
        rows = payload.get("games") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise ReplayError("actual_results_games_missing")
        return next(
            (row for row in rows if isinstance(row, Mapping) and str(row.get("game_id")) == game_id),
            None,
        )


def _read_snapshot(path: str | Path) -> Mapping[str, Any]:
    snapshot_path = Path(path)
    if not snapshot_path.is_file():
        raise ReplayError("pregame_snapshot_missing")
    try:
        value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayError("pregame_snapshot_invalid") from exc
    if not isinstance(value, Mapping):
        raise ReplayError("pregame_snapshot_invalid")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-id", required=True)
    parser.add_argument("--cutoff", required=True, help="Timezone aware ISO timestamp")
    parser.add_argument("--snapshot", required=True, help="Captured pregame snapshot JSON")
    parser.add_argument("--actual-results", required=True, help="Evaluation only JSON")
    parser.add_argument("--output", help="Optional new JSON output path")
    args = parser.parse_args(argv)
    try:
        report = run_replay(
            _read_snapshot(args.snapshot),
            game_id=args.game_id,
            cutoff=args.cutoff,
            actual_provider=JsonActualResultProvider(args.actual_results),
        )
    except ReplayError as exc:
        report = {"status": "unavailable", "reason": exc.reason}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        if output.exists():
            print(json.dumps({"status": "unavailable", "reason": "output_exists"}))
            return 2
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
