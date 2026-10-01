from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    log_loss,
    mean_absolute_error,
    root_mean_squared_error,
)


def classification_metrics(y_true: pd.Series, probabilities: np.ndarray) -> dict[str, float]:
    clipped = np.clip(np.asarray(probabilities, dtype=float), 1e-6, 1.0 - 1e-6)
    labels = (clipped >= 0.5).astype(int)
    calibration = calibration_table(y_true, clipped)
    return {
        "log_loss": float(log_loss(y_true, clipped)),
        "brier_score": float(brier_score_loss(y_true, clipped)),
        "accuracy": float(accuracy_score(y_true, labels)),
        "ece": float(
            np.average(
                calibration["absolute_gap"], weights=calibration["count"]
            )
        ),
    }


def regression_metrics(y_true: pd.Series, predictions: np.ndarray) -> dict[str, float]:
    values = np.asarray(predictions, dtype=float)
    return {
        "mae": float(mean_absolute_error(y_true, values)),
        "rmse": float(root_mean_squared_error(y_true, values)),
    }


def calibration_table(
    y_true: pd.Series, probabilities: np.ndarray, bins: int = 10
) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "actual": np.asarray(y_true, dtype=int),
            "probability": np.clip(np.asarray(probabilities, dtype=float), 0.0, 1.0),
        }
    )
    edges = np.linspace(0.0, 1.0, bins + 1)
    frame["bin"] = pd.cut(
        frame["probability"], bins=edges, include_lowest=True, right=True
    )
    grouped = frame.groupby("bin", observed=True)
    result = grouped.agg(
        count=("actual", "size"),
        mean_predicted_probability=("probability", "mean"),
        actual_home_win_rate=("actual", "mean"),
    ).reset_index()
    result["bin"] = result["bin"].astype(str)
    result["absolute_gap"] = (
        result["mean_predicted_probability"] - result["actual_home_win_rate"]
    ).abs()
    return result


def individual_log_loss(y_true: pd.Series, probabilities: pd.Series) -> pd.Series:
    p = probabilities.clip(1e-6, 1.0 - 1e-6)
    y = y_true.astype(float)
    return -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))
