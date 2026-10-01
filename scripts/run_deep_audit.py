from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from courtvision.deep_audit import (
    dump_json,
    feature_audit,
    feature_group_ablation,
    feature_matrix_diagnostics,
    paired_market_uncertainty,
    rolling_origin_stability,
    seed_stability,
    write_feature_audit_report,
    write_stability_report,
)


def main() -> int:
    artifact_dir = PROJECT_ROOT / "artifacts"
    report_dir = PROJECT_ROOT / "reports"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    dataset = pd.read_csv(
        PROJECT_ROOT / "data" / "processed" / "basketball_only_games.csv",
        parse_dates=["game_date"],
    )
    semantics = pd.read_csv(report_dir / "Feature_List.csv")
    features = semantics["feature"].tolist()
    metadata = json.loads(
        (artifact_dir / "experiment_metadata.json").read_text(encoding="utf-8")
    )
    selected = metadata["selected"]

    print("Running rolling-origin and seed stability checks...", flush=True)
    stability = rolling_origin_stability(dataset, features, selected)
    stability.to_csv(artifact_dir / "stability_by_season.csv", index=False)
    seeds = seed_stability(dataset, features, selected)
    seeds.to_csv(artifact_dir / "seed_stability.csv", index=False)

    print("Running paired model-versus-market uncertainty analysis...", flush=True)
    joined = pd.read_csv(artifact_dir / "test_predictions_with_market.csv")
    uncertainty, distribution_summary = paired_market_uncertainty(joined)
    uncertainty.to_csv(artifact_dir / "market_uncertainty.csv", index=False)
    dump_json(distribution_summary, artifact_dir / "market_uncertainty.json")

    print("Auditing all 85 features and group ablations...", flush=True)
    audit = feature_audit(dataset, features, semantics, artifact_dir)
    audit.to_csv(artifact_dir / "feature_audit.csv", index=False)
    ablation = feature_group_ablation(dataset, features, selected)
    ablation.to_csv(artifact_dir / "feature_group_ablation.csv", index=False)
    matrix_diagnostics = feature_matrix_diagnostics(dataset, features)
    dump_json(matrix_diagnostics, artifact_dir / "feature_matrix_diagnostics.json")

    write_stability_report(
        stability,
        seeds,
        uncertainty,
        report_dir / "Stability_and_Uncertainty_Report.md",
    )
    write_feature_audit_report(
        audit,
        ablation,
        matrix_diagnostics,
        report_dir / "Feature_Audit_Report.md",
    )
    print(
        json.dumps(
            {
                "stability_rows": len(stability),
                "seed_rows": len(seeds),
                "uncertainty_rows": len(uncertainty),
                "feature_rows": len(audit),
                "ablation_rows": len(ablation),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
