"""Immutable postgame learning rows and candidate-data export."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from v2.pregame_system.storage import clean, content_hash, now, save


def build_learning_record(
    postgame_record: dict[str, Any], *, output_root: str | Path
) -> dict[str, Any]:
    review = postgame_record["prediction_review"]
    game = postgame_record["game"]
    predictions = {}
    errors = {}
    for source, key in [
        ("basketball_only", "basketball_only"),
        ("market_informed", "market_informed"),
        ("public", "public_prediction"),
    ]:
        value = review.get(key)
        if value:
            predictions[source] = value["prediction"]
            errors[source] = value["errors"]
    row_core = {
        "schema_version": "1.0",
        "canonical_game_id": postgame_record["identity"]["canonical_game_id"],
        "nba_game_id": postgame_record["identity"].get("nba_game_id"),
        "espn_game_id": postgame_record["identity"].get("espn_game_id"),
        "identity_hash": postgame_record["identity"]["identity_hash"],
        "official_prediction_hash": review.get("prediction_reference_hash"),
        "prediction_source_type": review["source_type"],
        "model_version": (
            (review.get("prediction_snapshot") or {}).get("model_version")
            or "historical_baseline"
        ),
        "market_method_version": (review.get("market_usage") or {}).get("method_version"),
        "feature_input_hash": (review.get("prediction_snapshot") or {}).get("input_hash"),
        "feature_input_path": review.get("feature_input_path"),
        "pregame_features": clean(review.get("pregame_features")),
        "market_usage": clean(review.get("market_usage")),
        "predictions": clean(predictions),
        "actual": {
            "home_win": int(game["actual_home_margin"] > 0),
            "home_margin": game["actual_home_margin"],
            "total": game["actual_total"],
        },
        "errors": clean(errors),
        "data_completeness": clean(postgame_record["data_completeness"]),
        "postgame_record_hash": postgame_record["record_hash"],
        "eligible_for_error_evaluation": bool(
            predictions.get("basketball_only") and game.get("actual_total") is not None
        ),
        "eligible_for_candidate_training": bool(
            predictions.get("basketball_only")
            and review.get("pregame_features")
            and game.get("actual_total") is not None
        ),
    }
    row = {
        **row_core,
        "learning_record_hash": content_hash(row_core),
        "created_at": now(),
    }
    root = Path(output_root)
    path = root / f"{row['learning_record_hash']}.json"
    if not path.exists():
        save(path, row)
    row["path"] = str(path)
    return row


def export_learning_records(source_root: str | Path, target: str | Path) -> int:
    candidates = []
    for path in sorted(Path(source_root).glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        created_at = value.get("created_at") or ""
        expected = value.pop("learning_record_hash", None)
        value.pop("created_at", None)
        if expected != content_hash(value):
            continue
        value["learning_record_hash"] = expected
        if value.get("eligible_for_candidate_training"):
            candidates.append((created_at, value))
    selected = {}
    for created_at, value in candidates:
        key = value["canonical_game_id"]
        rank = (created_at, value["learning_record_hash"])
        if key not in selected or rank > selected[key][0]:
            selected[key] = (rank, value)
    rows = [selected[key][1] for key in sorted(selected)]
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".candidate_training_", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(clean(row), ensure_ascii=False, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return len(rows)
