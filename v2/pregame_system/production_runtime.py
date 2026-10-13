"""Local production assets: configuration, preflight and frozen identity. No training."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path, PureWindowsPath

import joblib
import pandas as pd

from . import ROOT
from .runtime_assets import RuntimeAssetError
from .storage import content_hash, digest, instant

ASSET_ROOT_ENV = "COURTVISION_RUNTIME_ASSET_ROOT"
MODEL_ENV = "COURTVISION_PRODUCTION_MODEL"
CHECK_COMMAND = "python -m v2.pregame_system.runtime_check"


def configuration_value(name):
    # Only read asset settings here; never load or report API secrets.
    # An explicitly empty environment setting disables the local setting in tests/CI.
    if name in os.environ:
        return os.environ[name].strip()
    local = ROOT / ".env"
    if local.is_file():
        for line in local.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == name:
                return value.strip().strip("\"'")
    return ""


def configured_root():
    value = configuration_value(ASSET_ROOT_ENV)
    if not value:
        return None
    path = Path(value).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def production_error(detail):
    return RuntimeAssetError(
        f"registered_game_requires_production_runtime_assets: {detail}. "
        f"Configure {ASSET_ROOT_ENV} (environment or local .env), then run: {CHECK_COMMAND}. "
        "Demo bootstrap cannot supply production history."
    )


def resolve_production(production=None):
    root = configured_root() or ROOT
    if production is not None:
        path = Path(production).resolve()
    else:
        pinned = configuration_value(MODEL_ENV)
        if pinned:
            path = (root / pinned).resolve()
        else:
            candidates = sorted((root / "v2/pregame_system/outputs/production").glob("*/model.json"))
            if not candidates:
                raise production_error("missing production model.json")
            path = candidates[-1]
    return root, path, [root / "data/raw/Games.csv", root / "data/raw/TeamStatistics.csv"]


def load_production(production=None):
    """Validate actual loaded models against current feature construction and history."""
    from courtvision.data import TEAM_ID_TO_ABBR
    from .features import history
    from .prediction import feature_row

    root, metadata_path, sources = resolve_production(production)
    try:
        for path in [metadata_path, *sources]:
            if not path.is_file():
                raise ValueError(f"missing {path}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        instant(metadata["training_cutoff"])
        if not metadata.get("version") or set(metadata["models"]) != {"win_probability", "expected_margin", "expected_total"}:
            raise ValueError("invalid model metadata/tasks")
        loaded = {}
        for task, info in metadata["models"].items():
            # A relocated bundle may have historical absolute paths in metadata.
            # Resolve the artifact beside its metadata, never silently use another checkout's model.
            original = Path(info["path"])
            windows = PureWindowsPath(info["path"])
            relative = windows.name if windows.is_absolute() else original.name if original.is_absolute() else original
            path = (metadata_path.parent / relative).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"model path escapes asset root: {task}")
            if not path.is_file() or digest(path) != info["sha256"]:
                raise ValueError(f"missing or hash-mismatched model: {task} ({path})")
            try:
                model = joblib.load(path)
            except Exception as exc:
                raise ValueError(f"cannot load model: {task}: {type(exc).__name__}: {exc}") from exc
            if not callable(getattr(model, "predict", None)):
                raise ValueError(f"unloadable prediction interface: {task}")
            columns = info["feature_columns"]
            if not columns or len(columns) != len(set(columns)):
                raise ValueError(f"invalid feature_columns: {task}")
            names = getattr(model, "feature_names_in_", None)
            if names is not None and list(names) != columns:
                raise ValueError(f"model feature schema/order mismatch: {task}")
            count = getattr(model, "n_features_in_", len(columns))
            if count != len(columns):
                raise ValueError(f"model feature count mismatch: {task}")
            info["path"] = str(path)
            loaded[task] = model
        source_hashes = {path.name: digest(path) for path in sources}
        games, stats = history(*sources)
        if any(digest(path) != source_hashes[path.name] for path in sources):
            raise ValueError("history asset changed during loading")
        if games.empty or stats.empty:
            raise ValueError("empty competitive history")
        game_teams = set(games.home_team_id) | set(games.away_team_id)
        stat_teams = set(stats.team_id)
        missing = set(TEAM_ID_TO_ABBR) - (game_teams & stat_teams)
        if missing:
            raise ValueError(f"missing modern team coverage: {sorted(missing)}")
        # Build one result-free future anchor with the unchanged real feature builder.
        latest = games.iloc[-1]
        tip = (games.game_date.max() + pd.Timedelta(days=3)).tz_localize("America/New_York")
        cutoff = (tip - pd.Timedelta(days=1)).isoformat()
        probe = {"game_id": "runtime-preflight-anchor", "home_team_id": int(latest.home_team_id),
                 "away_team_id": int(latest.away_team_id), "season_start": int(latest.season_start),
                 "is_playoffs": 0, "scheduled_tipoff": tip.isoformat()}
        row, _, _, _ = feature_row(probe, cutoff, games, stats)
        for task, info in metadata["models"].items():
            missing = set(info["feature_columns"]) - set(row.columns)
            if missing:
                raise ValueError(f"feature schema incompatible: {task}: {sorted(missing)}")
            from courtvision.models import predict_for_task
            try:
                value = float(predict_for_task(loaded[task], task, row[info["feature_columns"]])[0])
            except Exception as exc:
                raise ValueError(f"model inference incompatible: {task}: {type(exc).__name__}: {exc}") from exc
            if not math.isfinite(value) or (task == "win_probability" and not 0 <= value <= 1) or (task == "expected_total" and value <= 0):
                raise ValueError(f"invalid preflight output: {task}")
        schema = {task: info["feature_columns"] for task, info in metadata["models"].items()}
        identity = {"runtime_mode": "PRODUCTION", "model_version": metadata["version"],
            "training_cutoff": metadata["training_cutoff"], "metadata_sha256": digest(metadata_path),
            "model_hashes": {task: info["sha256"] for task, info in metadata["models"].items()},
            "data_sources": {path.name: {"sha256": source_hashes[path.name], "bytes": path.stat().st_size} for path in sources},
            "history_rows": len(games), "history_latest_game": str(games.game_date.max()),
            "teams_covered": sorted(int(team) for team in game_teams & stat_teams),
            "feature_schema_hash": content_hash(schema),
            "feature_version": {"v1_sha256": digest(ROOT / "src/courtvision/features.py"),
                                "v2_sha256": digest(ROOT / "v2/pregame_system/features.py")},
            "data_semantics": "local competitive history; per-row original ingestion times unavailable"}
        identity["asset_identity_hash"] = content_hash(identity)
        return metadata_path, sources, metadata, games, stats, identity
    except (OSError, ValueError, KeyError, TypeError, EOFError, ImportError, AttributeError) as exc:
        raise production_error(str(exc)) from exc
