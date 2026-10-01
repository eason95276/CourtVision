from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from courtvision.bad_cases import build_bad_cases, build_error_slices
from courtvision.config import (
    HISTORY_START_SEASON,
    MODELING_START_SEASON,
    RANDOM_SEED,
    RAW_SOURCES,
    ROLLING_WINDOWS,
    TEST_SEASON,
    TRAINING_WINDOWS,
    VALIDATION_SEASON,
)
from courtvision.data import load_games, load_odds, load_team_statistics
from courtvision.experiment import run_experiments
from courtvision.features import build_game_dataset, feature_semantics
from courtvision.market import audit_and_benchmark_market
from courtvision.quality import run_data_quality_checks, write_data_quality_report
from courtvision.reporting import write_report


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_feature_report(semantics: pd.DataFrame, path: Path) -> None:
    categories = {
        "Elo": semantics[semantics["feature"].str.startswith("elo_")],
        "Rolling team form": semantics[semantics["feature"].str.contains("roll")],
        "Schedule context": semantics[
            semantics["feature"].str.contains("rest_days|back_to_back|games_played")
        ],
        "Game context": semantics[semantics["feature"].eq("is_playoffs")],
    }
    lines = [
        "# CourtVision Feature List and Time Semantics",
        "",
        f"Feature count: {len(semantics)}",
        "",
        "Every rolling feature is computed per team with `shift(1)` before the rolling mean.",
        "Market odds, target-game scores and target-game box scores are excluded.",
        "",
    ]
    for category, frame in categories.items():
        lines.extend([f"## {category}", ""])
        for row in frame.itertuples(index=False):
            lines.append(
                f"1. `{row.feature}`: {row.source_history}; {row.time_semantics}."
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    raw_dir = PROJECT_ROOT / "data" / "raw"
    processed_dir = PROJECT_ROOT / "data" / "processed"
    artifact_dir = PROJECT_ROOT / "artifacts"
    report_dir = PROJECT_ROOT / "reports"
    processed_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    raw_files = {
        "games": raw_dir / "Games.csv",
        "team_statistics": raw_dir / "TeamStatistics.csv",
        "odds": raw_dir / "nba_2008-2026.csv",
    }
    missing = [str(path) for path in raw_files.values() if not path.exists()]
    if missing:
        raise FileNotFoundError(f"missing raw files: {missing}")
    for filename, source in RAW_SOURCES.items():
        path = raw_dir / filename
        actual_hash = sha256(path)
        if path.stat().st_size != source["bytes"] or actual_hash != source["sha256"]:
            raise ValueError(
                f"raw source integrity mismatch for {filename}; "
                "run scripts/download_data.py and inspect the fixed source version"
            )

    print("Loading fixed raw snapshots...", flush=True)
    games = load_games(
        raw_files["games"], min_season_start=HISTORY_START_SEASON
    )
    excluded_invalid_results = games.attrs.get("excluded_invalid_results", [])
    eligible_team_keys = {
        (row.game_id, int(team_id))
        for row in games.itertuples(index=False)
        for team_id in (row.home_team_id, row.away_team_id)
    }
    team_stats = load_team_statistics(
        raw_files["team_statistics"],
        min_season_start=HISTORY_START_SEASON,
        eligible_game_ids=set(games["game_id"]),
        eligible_team_keys=eligible_team_keys,
    )
    odds = load_odds(raw_files["odds"])

    print("Building leakage-safe pregame features...", flush=True)
    dataset, feature_columns = build_game_dataset(games, team_stats)
    dataset = dataset[
        dataset["season_start"] >= MODELING_START_SEASON
    ].reset_index(drop=True)
    dataset.to_csv(processed_dir / "basketball_only_games.csv", index=False)
    semantics = feature_semantics(feature_columns)
    semantics.to_csv(report_dir / "Feature_List.csv", index=False)
    write_feature_report(semantics, report_dir / "Feature_List.md")

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_datasets": RAW_SOURCES,
        "raw_files": {
            name: {
                "path": str(path.relative_to(PROJECT_ROOT)),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for name, path in raw_files.items()
        },
        "processed_rows": len(dataset),
        "processed_date_min": dataset["game_date"].min().isoformat(),
        "processed_date_max": dataset["game_date"].max().isoformat(),
        "feature_count": len(feature_columns),
        "feature_columns": feature_columns,
    }
    (artifact_dir / "data_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("Running blocking data-quality checks...", flush=True)
    quality = run_data_quality_checks(
        games,
        team_stats,
        dataset,
        feature_columns,
        odds,
        excluded_invalid_results=excluded_invalid_results,
    )
    write_data_quality_report(
        quality,
        artifact_dir / "data_quality_report.json",
        report_dir / "Data_Quality_Report.md",
    )
    if quality["status"] != "passed":
        raise ValueError(
            "blocking data-quality checks failed; inspect reports/Data_Quality_Report.md"
        )

    print("Running 5, 8 and 10 season experiments...", flush=True)
    experiment = run_experiments(dataset, feature_columns, artifact_dir)
    print("Auditing market field direction and running benchmark...", flush=True)
    market = audit_and_benchmark_market(
        odds, experiment["predictions"], artifact_dir
    )
    print("Creating structured bad cases...", flush=True)
    build_bad_cases(market["joined"], artifact_dir)
    build_error_slices(market["joined"], artifact_dir)
    write_report(
        artifact_dir,
        report_dir / "Baseline_Modeling_Report.md",
        feature_count=len(feature_columns),
    )

    reproducibility_files = [
        processed_dir / "basketball_only_games.csv",
        artifact_dir / "model_metrics.csv",
        artifact_dir / "test_predictions.csv",
        artifact_dir / "market_benchmark_summary.json",
        artifact_dir / "data_quality_report.json",
        report_dir / "Baseline_Modeling_Report.md",
        report_dir / "Feature_List.csv",
    ]
    reproducibility_manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {
            package: importlib.metadata.version(package)
            for package in ["pandas", "numpy", "scikit-learn", "scipy", "joblib"]
        },
        "config": {
            "random_seed": RANDOM_SEED,
            "history_start_season": HISTORY_START_SEASON,
            "modeling_start_season": MODELING_START_SEASON,
            "validation_season": VALIDATION_SEASON,
            "test_season": TEST_SEASON,
            "training_windows": TRAINING_WINDOWS,
            "rolling_windows": ROLLING_WINDOWS,
        },
        "raw_files": manifest["raw_files"],
        "rebuildable_artifacts": {
            str(path.relative_to(PROJECT_ROOT)): {
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in reproducibility_files
        },
    }
    (artifact_dir / "reproducibility_manifest.json").write_text(
        json.dumps(reproducibility_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "processed_games": len(dataset),
                "feature_count": len(feature_columns),
                "selected": experiment["metadata"]["selected"],
                "report": str(report_dir / "Baseline_Modeling_Report.md"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
