from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from .metrics import calibration_table, classification_metrics, regression_metrics
from .models import build_model, predict_for_task
from .config import TEST_SEASON, TRAINING_WINDOWS, VALIDATION_SEASON


TASKS = {
    "win_probability": {
        "target": "home_win",
        "models": ["simple_baseline", "logistic_regression", "hist_gradient_boosting"],
        "primary_metric": "log_loss",
    },
    "expected_margin": {
        "target": "point_margin",
        "models": ["simple_baseline", "linear_regression", "hist_gradient_boosting"],
        "primary_metric": "mae",
    },
    "expected_total": {
        "target": "total_points",
        "models": ["simple_baseline", "linear_regression", "hist_gradient_boosting"],
        "primary_metric": "mae",
    },
}


def _evaluate(task: str, y_true: pd.Series, predictions: np.ndarray) -> dict[str, float]:
    if task == "win_probability":
        return classification_metrics(y_true, predictions)
    return regression_metrics(y_true, predictions)


def _constant_prediction(task: str, y_train: pd.Series, size: int) -> np.ndarray:
    value = float(y_train.mean())
    return np.full(size, value, dtype=float)


def temporal_training_frame(
    dataset: pd.DataFrame, *, evaluation_season: int, window: int
) -> pd.DataFrame:
    """Return complete seasons strictly before the evaluation season."""
    frame = dataset[
        (dataset["season_start"] >= evaluation_season - window)
        & (dataset["season_start"] < evaluation_season)
    ].copy()
    observed = sorted(frame["season_start"].unique().tolist())
    expected = list(range(evaluation_season - window, evaluation_season))
    if observed != expected:
        raise ValueError(
            f"incomplete {window}-season training window: expected={expected}, observed={observed}"
        )
    if frame["game_date"].max() >= dataset.loc[
        dataset["season_start"] == evaluation_season, "game_date"
    ].min():
        raise ValueError("training rows overlap the evaluation period")
    return frame


def _calibration_svg(table: pd.DataFrame, path: Path) -> None:
    width, height, margin = 640, 480, 60
    plot_width, plot_height = width - 2 * margin, height - 2 * margin

    def point(x: float, y: float) -> tuple[float, float]:
        return margin + x * plot_width, height - margin - y * plot_height

    diagonal_start = point(0.0, 0.0)
    diagonal_end = point(1.0, 1.0)
    circles = []
    polyline = []
    for row in table.itertuples(index=False):
        x, y = point(row.mean_predicted_probability, row.actual_home_win_rate)
        polyline.append(f"{x:.1f},{y:.1f}")
        circles.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#ff7a59">'
            f'<title>n={row.count}, predicted={row.mean_predicted_probability:.3f}, actual={row.actual_home_win_rate:.3f}</title>'
            "</circle>"
        )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="#fbfaf7"/>
<line x1="{diagonal_start[0]}" y1="{diagonal_start[1]}" x2="{diagonal_end[0]}" y2="{diagonal_end[1]}" stroke="#999" stroke-dasharray="6 6"/>
<polyline points="{' '.join(polyline)}" fill="none" stroke="#123c69" stroke-width="3"/>
{''.join(circles)}
<text x="{width/2}" y="28" text-anchor="middle" font-family="Arial" font-size="20">CourtVision Test Calibration</text>
<text x="{width/2}" y="{height-15}" text-anchor="middle" font-family="Arial" font-size="14">Mean predicted home-win probability</text>
<text x="18" y="{height/2}" text-anchor="middle" transform="rotate(-90 18 {height/2})" font-family="Arial" font-size="14">Actual home-win rate</text>
</svg>"""
    path.write_text(svg, encoding="utf-8")


def run_experiments(
    dataset: pd.DataFrame,
    feature_columns: list[str],
    artifact_dir: Path,
    *,
    validation_season: int = VALIDATION_SEASON,
    test_season: int = TEST_SEASON,
    windows: tuple[int, ...] = TRAINING_WINDOWS,
) -> dict[str, object]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    metrics_rows: list[dict[str, object]] = []
    predictions: dict[tuple[str, int, str, str], np.ndarray] = {}
    fitted_models: dict[tuple[str, int, str], object] = {}

    validation = dataset[dataset["season_start"] == validation_season].copy()
    test = dataset[dataset["season_start"] == test_season].copy()
    if validation.empty or test.empty:
        raise ValueError("validation or test season is empty")

    for task, specification in TASKS.items():
        target = str(specification["target"])
        for window in windows:
            validation_train = temporal_training_frame(
                dataset, evaluation_season=validation_season, window=window
            )
            test_train = temporal_training_frame(
                dataset, evaluation_season=test_season, window=window
            )

            for model_name in specification["models"]:
                for split_name, train_frame, eval_frame in [
                    ("validation", validation_train, validation),
                    ("test", test_train, test),
                ]:
                    if model_name == "simple_baseline":
                        predicted = _constant_prediction(
                            task, train_frame[target], len(eval_frame)
                        )
                        fitted = None
                    else:
                        fitted = build_model(task, model_name)
                        fitted.fit(train_frame[feature_columns], train_frame[target])
                        predicted = predict_for_task(
                            fitted, task, eval_frame[feature_columns]
                        )
                    result = _evaluate(task, eval_frame[target], predicted)
                    metrics_rows.append(
                        {
                            "task": task,
                            "window_years": window,
                            "model": model_name,
                            "split": split_name,
                            "train_start_season": int(train_frame["season_start"].min()),
                            "train_end_season": int(train_frame["season_start"].max()),
                            "eval_season": int(eval_frame["season_start"].iloc[0]),
                            "n_train": len(train_frame),
                            "n_eval": len(eval_frame),
                            **result,
                        }
                    )
                    predictions[(task, window, model_name, split_name)] = predicted
                    if split_name == "test" and fitted is not None:
                        fitted_models[(task, window, model_name)] = fitted

    for split_name, eval_frame in [("validation", validation), ("test", test)]:
        predicted = eval_frame["elo_home_win_probability"].to_numpy(dtype=float)
        result = classification_metrics(eval_frame["home_win"], predicted)
        metrics_rows.append(
            {
                "task": "win_probability",
                "window_years": 0,
                "model": "elo_baseline",
                "split": split_name,
                "train_start_season": np.nan,
                "train_end_season": int(eval_frame["season_start"].iloc[0]) - 1,
                "eval_season": int(eval_frame["season_start"].iloc[0]),
                "n_train": np.nan,
                "n_eval": len(eval_frame),
                **result,
            }
        )
        predictions[("win_probability", 0, "elo_baseline", split_name)] = predicted

    metrics = pd.DataFrame(metrics_rows)
    metrics.to_csv(artifact_dir / "model_metrics.csv", index=False)

    selected: dict[str, dict[str, object]] = {}
    final_predictions = test[
        [
            "game_id",
            "game_date",
            "season_start",
            "home_team_id",
            "away_team_id",
            "home_team",
            "away_team",
            "home_abbr",
            "away_abbr",
            "game_type",
            "is_playoffs",
            "home_win",
            "point_margin",
            "total_points",
        ]
    ].copy()
    for task, specification in TASKS.items():
        primary_metric = str(specification["primary_metric"])
        candidates = metrics[
            (metrics["task"] == task) & (metrics["split"] == "validation")
        ].sort_values([primary_metric, "model", "window_years"])
        best = candidates.iloc[0]
        window = int(best["window_years"])
        model_name = str(best["model"])
        selected[task] = {
            "window_years": window,
            "model": model_name,
            "selection_metric": primary_metric,
            "validation_value": float(best[primary_metric]),
        }
        predicted = predictions[(task, window, model_name, "test")]
        output_name = {
            "win_probability": "win_probability",
            "expected_margin": "predicted_margin",
            "expected_total": "predicted_total",
        }[task]
        final_predictions[output_name] = predicted
        model = fitted_models.get((task, window, model_name))
        if model is not None:
            joblib.dump(model, artifact_dir / f"selected_{task}_evaluation_model.joblib")

    context_features = [
        "elo_home",
        "elo_away",
        "elo_diff",
        "home_rest_days",
        "away_rest_days",
        "home_back_to_back",
        "away_back_to_back",
        "diff_win_rate_roll5",
        "diff_net_rating_roll5",
        "diff_net_rating_roll15",
        "diff_pace_roll15",
    ]
    for column in context_features:
        final_predictions[column] = test[column].to_numpy()
    final_predictions.to_csv(artifact_dir / "test_predictions.csv", index=False)

    best_win = selected["win_probability"]
    win_probabilities = final_predictions["win_probability"].to_numpy()
    calibration = calibration_table(test["home_win"], win_probabilities)
    calibration.to_csv(artifact_dir / "win_probability_calibration.csv", index=False)
    _calibration_svg(calibration, artifact_dir / "win_probability_calibration.svg")

    metadata = {
        "validation_season": validation_season,
        "test_season": test_season,
        "training_window_interpretation": (
            "N complete training seasons immediately before the evaluation season; "
            "validation and test seasons remain fixed"
        ),
        "selected": selected,
        "feature_count": len(feature_columns),
        "selected_win_calibration_model": best_win,
    }
    (artifact_dir / "experiment_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"metrics": metrics, "predictions": final_predictions, "metadata": metadata}
