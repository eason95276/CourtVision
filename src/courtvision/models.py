from __future__ import annotations

from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .config import RANDOM_SEED


def build_model(task: str, model_name: str) -> Pipeline:
    if task == "win_probability" and model_name == "logistic_regression":
        estimator = LogisticRegression(
            max_iter=1500, C=1.0, random_state=RANDOM_SEED
        )
        return Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("model", estimator),
            ]
        )
    if task == "win_probability" and model_name == "hist_gradient_boosting":
        estimator = HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_iter=180,
            max_leaf_nodes=15,
            min_samples_leaf=30,
            l2_regularization=0.5,
            random_state=RANDOM_SEED,
        )
        return Pipeline(
            [("imputer", SimpleImputer(strategy="median")), ("model", estimator)]
        )
    if task in {"expected_margin", "expected_total"} and model_name == "linear_regression":
        return Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("model", LinearRegression()),
            ]
        )
    if task in {"expected_margin", "expected_total"} and model_name == "hist_gradient_boosting":
        estimator = HistGradientBoostingRegressor(
            learning_rate=0.05,
            max_iter=180,
            max_leaf_nodes=15,
            min_samples_leaf=30,
            l2_regularization=0.5,
            random_state=RANDOM_SEED,
        )
        return Pipeline(
            [("imputer", SimpleImputer(strategy="median")), ("model", estimator)]
        )
    raise ValueError(f"unsupported task/model combination: {task}/{model_name}")


def predict_for_task(model: Pipeline, task: str, features):
    if task == "win_probability":
        return model.predict_proba(features)[:, 1]
    return model.predict(features)
