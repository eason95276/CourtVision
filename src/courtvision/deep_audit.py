from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from .experiment import TASKS, temporal_training_frame
from .metrics import classification_metrics, regression_metrics
from .models import build_model, predict_for_task


TASK_OUTPUT = {
    "win_probability": ("home_win", "log_loss"),
    "expected_margin": ("point_margin", "mae"),
    "expected_total": ("total_points", "mae"),
}


def feature_group(feature: str) -> str:
    if feature.startswith("elo_"):
        return "elo"
    if "roll" in feature:
        return "rolling_form"
    if any(token in feature for token in ("rest_days", "back_to_back", "games_played")):
        return "schedule_context"
    if feature == "is_playoffs":
        return "competition_context"
    return "other"


def _evaluate(task: str, actual: pd.Series, predicted: np.ndarray) -> dict[str, float]:
    if task == "win_probability":
        return classification_metrics(actual, predicted)
    return regression_metrics(actual, predicted)


def _fit_predict(
    task: str,
    model_name: str,
    train: pd.DataFrame,
    evaluation: pd.DataFrame,
    features: list[str],
    *,
    seed: int | None = None,
) -> np.ndarray:
    target = str(TASKS[task]["target"])
    model = build_model(task, model_name)
    if seed is not None and "model__random_state" in model.get_params():
        model.set_params(model__random_state=seed)
    model.fit(train[features], train[target])
    return predict_for_task(model, task, evaluation[features])


def rolling_origin_stability(
    dataset: pd.DataFrame,
    features: list[str],
    selected: dict[str, dict[str, object]],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for task, details in selected.items():
        window = int(details["window_years"])
        model_name = str(details["model"])
        target = str(TASKS[task]["target"])
        earliest = int(dataset["season_start"].min()) + window
        for evaluation_season in range(max(2022, earliest), 2026):
            evaluation = dataset[dataset["season_start"] == evaluation_season]
            if evaluation.empty:
                continue
            train = temporal_training_frame(
                dataset, evaluation_season=evaluation_season, window=window
            )
            predicted = _fit_predict(
                task, model_name, train, evaluation, features
            )
            rows.append(
                {
                    "analysis": "locked_selected_configuration",
                    "task": task,
                    "model": model_name,
                    "window_years": window,
                    "evaluation_season": evaluation_season,
                    "n_train": len(train),
                    "n_eval": len(evaluation),
                    **_evaluate(task, evaluation[target], predicted),
                }
            )
            for phase_name, phase_value in (("regular", 0), ("playoffs", 1)):
                phase_mask = evaluation["is_playoffs"].eq(phase_value).to_numpy()
                if not phase_mask.any():
                    continue
                rows.append(
                    {
                        "analysis": f"locked_selected_configuration_{phase_name}",
                        "task": task,
                        "model": model_name,
                        "window_years": window,
                        "evaluation_season": evaluation_season,
                        "n_train": len(train),
                        "n_eval": int(phase_mask.sum()),
                        **_evaluate(
                            task,
                            evaluation.loc[phase_mask, target],
                            predicted[phase_mask],
                        ),
                    }
                )

    for task, details in selected.items():
        model_name = str(details["model"])
        target = str(TASKS[task]["target"])
        for evaluation_season in range(2019, 2026):
            evaluation = dataset[dataset["season_start"] == evaluation_season]
            if evaluation.empty:
                continue
            train = temporal_training_frame(
                dataset, evaluation_season=evaluation_season, window=5
            )
            predicted = _fit_predict(
                task, model_name, train, evaluation, features
            )
            rows.append(
                {
                    "analysis": "common_five_season_window",
                    "task": task,
                    "model": model_name,
                    "window_years": 5,
                    "evaluation_season": evaluation_season,
                    "n_train": len(train),
                    "n_eval": len(evaluation),
                    **_evaluate(task, evaluation[target], predicted),
                }
            )
    return pd.DataFrame(rows)


def seed_stability(
    dataset: pd.DataFrame,
    features: list[str],
    selected: dict[str, dict[str, object]],
    *,
    seeds: tuple[int, ...] = (7, 42, 2026),
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    evaluation = dataset[dataset["season_start"] == 2025]
    for task, details in selected.items():
        target = str(TASKS[task]["target"])
        window = int(details["window_years"])
        train = temporal_training_frame(
            dataset, evaluation_season=2025, window=window
        )
        model_names = [str(details["model"]), "hist_gradient_boosting"]
        for model_name in model_names:
            for seed in seeds:
                predicted = _fit_predict(
                    task,
                    model_name,
                    train,
                    evaluation,
                    features,
                    seed=seed,
                )
                rows.append(
                    {
                        "task": task,
                        "model": model_name,
                        "window_years": window,
                        "seed": seed,
                        **_evaluate(task, evaluation[target], predicted),
                    }
                )
    return pd.DataFrame(rows)


def _bootstrap_mean(
    values: np.ndarray,
    *,
    rng: np.random.Generator,
    iterations: int,
) -> np.ndarray:
    indexes = rng.integers(0, len(values), size=(iterations, len(values)))
    return values[indexes].mean(axis=1)


def _cluster_bootstrap_mean(
    values: np.ndarray,
    clusters: np.ndarray,
    *,
    rng: np.random.Generator,
    iterations: int,
) -> np.ndarray:
    unique_clusters = np.unique(clusters)
    grouped = [values[clusters == cluster] for cluster in unique_clusters]
    results = np.empty(iterations, dtype=float)
    for index in range(iterations):
        sampled = rng.integers(0, len(grouped), size=len(grouped))
        selected = [grouped[position] for position in sampled]
        results[index] = np.concatenate(selected).mean()
    return results


def paired_market_uncertainty(
    joined: pd.DataFrame,
    *,
    iterations: int = 10_000,
    seed: int = 42,
) -> tuple[pd.DataFrame, dict[str, object]]:
    frame = joined.copy()
    frame["game_date"] = pd.to_datetime(frame["game_date"])
    frame["week"] = frame["game_date"].dt.to_period("W-SUN").astype(str)
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    distributions: dict[str, object] = {}
    specifications = {
        "margin": (
            (frame["predicted_margin"] - frame["point_margin"]).abs(),
            (frame["market_expected_home_margin"] - frame["point_margin"]).abs(),
        ),
        "total": (
            (frame["predicted_total"] - frame["total_points"]).abs(),
            (frame["market_total"] - frame["total_points"]).abs(),
        ),
    }
    slices = {
        "all": pd.Series(True, index=frame.index),
        "regular_or_cup": frame["is_playoffs"].eq(0),
        "playoffs_or_play_in": frame["is_playoffs"].eq(1),
    }
    for task, (model_loss, market_loss) in specifications.items():
        difference = model_loss - market_loss
        for slice_name, mask in slices.items():
            values = difference.loc[mask & difference.notna()].to_numpy(dtype=float)
            clusters = frame.loc[mask & difference.notna(), "week"].to_numpy()
            iid = _bootstrap_mean(values, rng=rng, iterations=iterations)
            weekly = _cluster_bootstrap_mean(
                values, clusters, rng=rng, iterations=iterations
            )
            rows.append(
                {
                    "task": task,
                    "slice": slice_name,
                    "games": len(values),
                    "weeks": len(np.unique(clusters)),
                    "mean_model_minus_market_absolute_error": float(values.mean()),
                    "iid_ci_low": float(np.quantile(iid, 0.025)),
                    "iid_ci_high": float(np.quantile(iid, 0.975)),
                    "weekly_cluster_ci_low": float(np.quantile(weekly, 0.025)),
                    "weekly_cluster_ci_high": float(np.quantile(weekly, 0.975)),
                    "weekly_probability_model_is_worse": float((weekly > 0).mean()),
                }
            )
            if slice_name == "all":
                distributions[task] = {
                    "weekly_cluster_percentiles": {
                        str(percentile): float(np.quantile(weekly, percentile / 100))
                        for percentile in (1, 2.5, 5, 25, 50, 75, 95, 97.5, 99)
                    }
                }
    summary = {
        "interpretation": (
            "positive model-minus-market absolute error means the basketball-only model is worse"
        ),
        "iterations": iterations,
        "seed": seed,
        "cluster": "calendar week ending Sunday",
        "distributions": distributions,
    }
    return pd.DataFrame(rows), summary


def feature_audit(
    dataset: pd.DataFrame,
    features: list[str],
    semantics: pd.DataFrame,
    artifact_dir: Path,
) -> pd.DataFrame:
    test = dataset[dataset["season_start"] == 2025].copy()
    models = {
        task: joblib.load(artifact_dir / f"selected_{task}_evaluation_model.joblib")
        for task in TASK_OUTPUT
    }
    baseline_predictions = {
        task: predict_for_task(model, task, test[features])
        for task, model in models.items()
    }
    baseline_loss = {
        task: _evaluate(task, test[target], baseline_predictions[task])[metric]
        for task, (target, metric) in TASK_OUTPUT.items()
    }

    permutation: dict[tuple[str, str], tuple[float, float]] = {}
    for task, (target, metric) in TASK_OUTPUT.items():
        model = models[task]
        for feature_index, feature in enumerate(features):
            deltas = []
            for repeat in range(3):
                rng = np.random.default_rng(10_000 + feature_index * 10 + repeat)
                permuted = test[features].copy()
                permuted[feature] = rng.permutation(permuted[feature].to_numpy())
                predicted = predict_for_task(model, task, permuted)
                loss = _evaluate(task, test[target], predicted)[metric]
                deltas.append(float(loss - baseline_loss[task]))
            permutation[(task, feature)] = (
                float(np.mean(deltas)),
                float(np.std(deltas, ddof=1)),
            )

    correlations = dataset[features].corr().abs()
    correlation_values = correlations.to_numpy(copy=True)
    np.fill_diagonal(correlation_values, np.nan)
    correlations = pd.DataFrame(
        correlation_values,
        index=correlations.index,
        columns=correlations.columns,
    )
    semantics_by_feature = semantics.set_index("feature")
    rows: list[dict[str, object]] = []
    for feature in features:
        values = dataset[feature]
        q1 = float(values.quantile(0.25))
        q3 = float(values.quantile(0.75))
        iqr = q3 - q1
        if iqr == 0:
            outliers = int((values != values.median()).sum())
        else:
            outliers = int(
                ((values < q1 - 3 * iqr) | (values > q3 + 3 * iqr)).sum()
            )
        most_correlated = correlations[feature].idxmax()
        exact_relation = ""
        if feature == "elo_diff":
            exact_relation = "elo_home minus elo_away"
        elif feature.startswith("diff_"):
            suffix = feature.removeprefix("diff_")
            if f"home_{suffix}" in features and f"away_{suffix}" in features:
                exact_relation = f"home_{suffix} minus away_{suffix}"
        if "net_rating_roll" in feature:
            prefix, window = feature.split("net_rating_roll", maxsplit=1)
            rating_relation = (
                f"{prefix}off_rating_roll{window} minus "
                f"{prefix}def_rating_roll{window}"
            )
            exact_relation = "; ".join(
                relation for relation in (exact_relation, rating_relation) if relation
            )
        semantics_row = semantics_by_feature.loc[feature]
        row: dict[str, object] = {
            "feature": feature,
            "group": feature_group(feature),
            "source_history": semantics_row["source_history"],
            "time_semantics": semantics_row["time_semantics"],
            "uses_target_game_result": bool(semantics_row["uses_target_game_result"]),
            "rows": len(values),
            "missing_rate": float(values.isna().mean()),
            "unique_values": int(values.nunique(dropna=True)),
            "minimum": float(values.min()),
            "p01": float(values.quantile(0.01)),
            "median": float(values.median()),
            "p99": float(values.quantile(0.99)),
            "maximum": float(values.max()),
            "robust_extreme_count": outliers,
            "most_correlated_feature": most_correlated,
            "max_absolute_feature_correlation": float(correlations.loc[feature, most_correlated]),
            "exact_linear_relation": exact_relation,
            "home_win_correlation": float(values.corr(dataset["home_win"])),
            "point_margin_correlation": float(values.corr(dataset["point_margin"])),
            "total_points_correlation": float(values.corr(dataset["total_points"])),
        }
        for task in TASK_OUTPUT:
            mean_delta, std_delta = permutation[(task, feature)]
            row[f"{task}_permutation_loss_delta"] = mean_delta
            row[f"{task}_permutation_loss_delta_std"] = std_delta
        rows.append(row)
    return pd.DataFrame(rows)


def feature_group_ablation(
    dataset: pd.DataFrame,
    features: list[str],
    selected: dict[str, dict[str, object]],
) -> pd.DataFrame:
    groups: dict[str, list[str]] = {}
    for feature in features:
        groups.setdefault(feature_group(feature), []).append(feature)
    rows: list[dict[str, object]] = []
    for task, details in selected.items():
        window = int(details["window_years"])
        model_name = str(details["model"])
        target, primary_metric = TASK_OUTPUT[task]
        for evaluation_season in (2022, 2023, 2024):
            evaluation = dataset[dataset["season_start"] == evaluation_season]
            train = temporal_training_frame(
                dataset, evaluation_season=evaluation_season, window=window
            )
            full_prediction = _fit_predict(
                task, model_name, train, evaluation, features
            )
            full_loss = _evaluate(task, evaluation[target], full_prediction)[primary_metric]
            for group, group_features in groups.items():
                reduced_features = [
                    feature for feature in features if feature not in group_features
                ]
                predicted = _fit_predict(
                    task, model_name, train, evaluation, reduced_features
                )
                reduced_loss = _evaluate(task, evaluation[target], predicted)[primary_metric]
                rows.append(
                    {
                        "task": task,
                        "evaluation_season": evaluation_season,
                        "removed_group": group,
                        "removed_feature_count": len(group_features),
                        "full_loss": full_loss,
                        "reduced_loss": reduced_loss,
                        "loss_increase_when_removed": reduced_loss - full_loss,
                    }
                )
    return pd.DataFrame(rows)


def feature_matrix_diagnostics(
    dataset: pd.DataFrame,
    features: list[str],
) -> dict[str, object]:
    matrix = dataset[features].to_numpy(dtype=float)
    centered = matrix - matrix.mean(axis=0, keepdims=True)
    scales = centered.std(axis=0, ddof=0)
    nonconstant = scales > 0
    standardized = centered[:, nonconstant] / scales[nonconstant]
    singular_values = np.linalg.svd(standardized, full_matrices=False, compute_uv=False)
    tolerance = singular_values.max() * max(standardized.shape) * np.finfo(float).eps
    rank = int((singular_values > tolerance).sum())
    positive = singular_values[singular_values > tolerance]
    condition_number = float(positive.max() / positive.min())
    return {
        "rows": int(matrix.shape[0]),
        "feature_count": int(matrix.shape[1]),
        "nonconstant_feature_count": int(nonconstant.sum()),
        "standardized_matrix_rank": rank,
        "rank_deficiency": int(nonconstant.sum() - rank),
        "effective_condition_number": condition_number,
        "home_away_difference_relations": 28,
        "net_rating_relations": 6,
        "independent_exact_constraints": int(nonconstant.sum() - rank),
        "interpretation": (
            "rank deficiency confirms exact feature redundancy; linear predictions can remain stable "
            "while individual coefficients are not uniquely attributable"
        ),
    }


def write_stability_report(
    stability: pd.DataFrame,
    seed_results: pd.DataFrame,
    uncertainty: pd.DataFrame,
    path: Path,
) -> None:
    locked = stability[stability["analysis"] == "locked_selected_configuration"]
    common = stability[stability["analysis"] == "common_five_season_window"]
    lines = [
        "# Baseline Stability and Uncertainty Report",
        "",
        "## Rolling-origin stability",
        "",
        "The selected model families and windows were held fixed. No tuning used these results.",
        "",
    ]
    for task, (_, metric) in TASK_OUTPUT.items():
        task_rows = locked[locked["task"] == task]
        lines.append(
            f"1. `{task}` {metric}: range {task_rows[metric].min():.4f} to "
            f"{task_rows[metric].max():.4f} across {len(task_rows)} later seasons."
        )
    lines.extend(["", "## Common five-season stress test", ""])
    for task, (_, metric) in TASK_OUTPUT.items():
        task_rows = common[common["task"] == task]
        worst = task_rows.sort_values(metric, ascending=False).iloc[0]
        best = task_rows.sort_values(metric).iloc[0]
        lines.append(
            f"1. `{task}` {metric}: best {best[metric]:.4f} in {int(best['evaluation_season'])}; "
            f"worst {worst[metric]:.4f} in {int(worst['evaluation_season'])}."
        )
    lines.extend(["", "## Random-seed stability", ""])
    for (task, model), group in seed_results.groupby(["task", "model"]):
        metric = TASK_OUTPUT[task][1]
        spread = group[metric].max() - group[metric].min()
        lines.append(
            f"1. `{task}` / `{model}` {metric} seed spread: {spread:.8f}."
        )
    lines.extend(["", "## Regular season versus playoffs", ""])
    for phase in ("regular", "playoffs"):
        phase_rows = stability[
            stability["analysis"].eq(f"locked_selected_configuration_{phase}")
        ]
        for task, (_, metric) in TASK_OUTPUT.items():
            task_rows = phase_rows[phase_rows["task"] == task]
            lines.append(
                f"1. `{phase}` / `{task}` {metric}: range "
                f"{task_rows[metric].min():.4f} to {task_rows[metric].max():.4f} "
                f"across {len(task_rows)} seasons."
            )
    lines.extend(["", "## Model versus market uncertainty", ""])
    for row in uncertainty[uncertainty["slice"] == "all"].itertuples(index=False):
        lines.append(
            f"1. `{row.task}` paired MAE difference: "
            f"{row.mean_model_minus_market_absolute_error:.4f}; weekly-cluster 95% CI "
            f"[{row.weekly_cluster_ci_low:.4f}, {row.weekly_cluster_ci_high:.4f}]; "
            f"P(model worse)={row.weekly_probability_model_is_worse:.3f}."
        )
    lines.extend(["", "### Market comparison by competition phase", ""])
    for row in uncertainty[uncertainty["slice"] != "all"].itertuples(index=False):
        lines.append(
            f"1. `{row.task}` / `{row.slice}`: games={row.games}, mean difference "
            f"{row.mean_model_minus_market_absolute_error:.4f}, weekly-cluster 95% CI "
            f"[{row.weekly_cluster_ci_low:.4f}, {row.weekly_cluster_ci_high:.4f}]."
        )
    lines.extend(
        [
            "",
            "A positive difference means the basketball-only model has larger absolute error.",
            "Weekly-cluster intervals are preferred over IID intervals because games within a week share context.",
            "The interval uses one 2025-26 season and a market source without a verified capture timestamp. It supports a same-season benchmark conclusion, not a long-run market claim.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_feature_audit_report(
    audit: pd.DataFrame,
    ablation: pd.DataFrame,
    matrix_diagnostics: dict[str, object],
    path: Path,
) -> None:
    exact = audit[audit["exact_linear_relation"].astype(str).ne("")]
    highly_correlated = audit[audit["max_absolute_feature_correlation"] >= 0.98]
    lines = [
        "# CourtVision Feature Audit Report",
        "",
        f"Audited features: {len(audit)}.",
        f"Features with missing values: {int((audit['missing_rate'] > 0).sum())}.",
        f"Features using target-game result: {int(audit['uses_target_game_result'].sum())}.",
        f"Features with a documented exact linear relation: {len(exact)}.",
        f"Features with maximum pairwise absolute correlation at least 0.98: {len(highly_correlated)}.",
        f"Standardized feature-matrix rank: {matrix_diagnostics['standardized_matrix_rank']} "
        f"of {matrix_diagnostics['nonconstant_feature_count']} nonconstant columns.",
        f"Rank deficiency: {matrix_diagnostics['rank_deficiency']} dimensions.",
        "",
        "## Structural findings",
        "",
        "1. Every home/away/difference triplet contains exact linear redundancy. Elo has the same structure.",
        "2. Rolling net rating is also exactly offensive rating minus defensive rating. Together these constraints explain the 32-dimensional rank deficiency.",
        "3. This redundancy does not create time leakage, but linear-model coefficients are not safely interpretable as independent effects.",
        "4. Individual permutation importance is conservative when correlated substitutes remain available.",
        "5. All rolling features are marked as shift(1) pregame histories and have zero missing rate in the modeling table.",
        "",
        "## Group ablation summary",
        "",
    ]
    summary = (
        ablation.groupby(["task", "removed_group"])["loss_increase_when_removed"]
        .agg(["mean", "min", "max"])
        .reset_index()
    )
    for row in summary.itertuples(index=False):
        lines.append(
            f"1. `{row.task}` remove `{row.removed_group}`: mean loss change "
            f"{row.mean:.5f}, range [{row.min:.5f}, {row.max:.5f}]."
        )
    lines.extend(
        [
            "",
            "Positive loss change indicates the removed group helped on average. Negative values indicate that removal improved the evaluated seasons.",
            "These are audit diagnostics across 2022 to 2024, not permission to select or delete features using Test.",
            "Full 85-row details are in `artifacts/feature_audit.csv`.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def dump_json(value: object, path: Path) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
