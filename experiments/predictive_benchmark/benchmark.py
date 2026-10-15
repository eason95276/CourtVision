"""Offline comparisons. No import of production generation or model promotion."""
import time

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from courtvision.metrics import classification_metrics, regression_metrics
from data_contract import chronological_split

TASKS = {'win_probability': 'home_win', 'expected_margin': 'point_margin', 'expected_total': 'total_points'}
MODELS = ['constant', 'linear', 'hist_gradient_boosting', 'lightgbm', 'xgboost']
# Locked small grid; test results must not change these choices during a run.
CONFIGS = [{'depth': 3, 'leaves': 7, 'learning_rate': 0.05, 'max_trees': 250},
           {'depth': 5, 'leaves': 23, 'learning_rate': 0.03, 'max_trees': 400}]


def score(task, actual, prediction):
    if task == 'win_probability':
        return classification_metrics(actual, prediction)
    return regression_metrics(actual, prediction)


def predict(model, task, x):
    values = model.predict_proba(x)[:, 1] if task == 'win_probability' else model.predict(x)
    if not np.isfinite(values).all():
        raise ValueError('non-finite prediction')
    return values


def make_booster(task, library, config, n_trees, early_stop=False):
    spec = CONFIGS[config]
    classification = task == 'win_probability'
    common = dict(n_estimators=n_trees, learning_rate=spec['learning_rate'],
                  random_state=42, n_jobs=2)
    if library == 'lightgbm':
        from lightgbm import LGBMClassifier, LGBMRegressor
        cls = LGBMClassifier if classification else LGBMRegressor
        return cls(**common, num_leaves=spec['leaves'], max_depth=spec['depth'],
                   min_child_samples=30, reg_lambda=1.0, verbosity=-1,
                   deterministic=True, force_col_wise=True)
    if library != 'xgboost':
        raise ValueError(f'unsupported booster: {library}')
    from xgboost import XGBClassifier, XGBRegressor
    cls = XGBClassifier if classification else XGBRegressor
    return cls(**common, max_depth=spec['depth'], min_child_weight=10,
               reg_lambda=1.0, tree_method='hist',
               objective='binary:logistic' if classification else 'reg:squarederror',
               eval_metric='logloss' if classification else 'rmse',
               **({'early_stopping_rounds': 25} if early_stop else {}))


def fit_booster(train, features, task, library, config, frozen_trees=None):
    """Early-stop within training; refit on all training with a frozen tree count."""
    target = TASKS[task]
    start = time.perf_counter()
    diagnostic = {'library': library, 'refit_n': len(train)}
    if frozen_trees is None:
        stop_season = int(train.season_start.max())
        core = train[train.season_start < stop_season]
        stop = train[train.season_start == stop_season]
        if core.empty or stop.empty or core.game_date.max() >= stop.game_date.min():
            raise ValueError('invalid inner early stopping partition')
        model = make_booster(task, library, config, CONFIGS[config]['max_trees'], True)
        if library == 'lightgbm':
            from lightgbm import early_stopping
            kwargs = {'eval_X': stop[features], 'eval_y': stop[target],
                      'callbacks': [early_stopping(25, verbose=False)]}
        else:
            kwargs = {'eval_set': [(stop[features], stop[target])], 'verbose': False}
        model.fit(core[features], core[target], **kwargs)
        count = int(model.best_iteration_) if library == 'lightgbm' else int(model.best_iteration) + 1
        diagnostic.update(early_stop_season=stop_season, core_train_n=len(core),
                          early_stop_n=len(stop), core_train_end=core.game_date.max().isoformat(),
                          early_stop_start=stop.game_date.min().isoformat())
    else:
        count = int(frozen_trees)
        diagnostic['early_stop_season'] = None
    model = make_booster(task, library, config, max(1, count))
    model.fit(train[features], train[target])
    diagnostic.update(n_trees=max(1, count), fit_seconds=time.perf_counter()-start,
                      train_start=train.game_date.min().isoformat(), train_end=train.game_date.max().isoformat())
    return model, diagnostic


def ordinary_model(task, name):
    classification = task == 'win_probability'
    if name == 'linear':
        estimator = LogisticRegression(max_iter=1500, random_state=42) if classification else LinearRegression()
        return make_pipeline(SimpleImputer(strategy='median'), StandardScaler(), estimator)
    cls = HistGradientBoostingClassifier if classification else HistGradientBoostingRegressor
    return make_pipeline(SimpleImputer(strategy='median'), cls(
        max_iter=180, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=30,
        l2_regularization=0.5, random_state=42, early_stopping=False))


def select_config(rows, metric):
    return min(rows, key=lambda row: (row[metric], row['config']))


def run_benchmark(frame, features, output, validation_season=2024, test_season=2025):
    validation_train, validation = chronological_split(frame, validation_season)
    test_train, test = chronological_split(frame, test_season)
    metrics, forecasts, fits, selections = [], [], [], []
    for task, target in TASKS.items():
        metric = 'log_loss' if task == 'win_probability' else 'mae'
        for name in MODELS:
            print(f'Tabular: {task} / {name}', flush=True)
            candidates, fitted = [], {}
            for config in range(2) if name in ('lightgbm', 'xgboost') else range(1):
                started = time.perf_counter()
                if name == 'constant':
                    model, diagnostic = None, {}
                    values = np.full(len(validation), validation_train[target].mean())
                elif name in ('lightgbm', 'xgboost'):
                    model, diagnostic = fit_booster(validation_train, features, task, name, config)
                    values = predict(model, task, validation[features])
                else:
                    model = ordinary_model(task, name)
                    model.fit(validation_train[features], validation_train[target])
                    values, diagnostic = predict(model, task, validation[features]), {}
                elapsed = time.perf_counter()-started
                row = dict(task=task, model=name, config=config, split='validation',
                           n_train=len(validation_train), n_eval=len(validation), seconds=elapsed,
                           **score(task, validation[target], values))
                candidates.append(row)
                metrics.append(row)
                fits.append(dict(task=task, model=name, split='validation', config=config, **diagnostic))
                fitted[config] = diagnostic
                forecasts.extend(dict(task=task, model=name, config=config, split='validation',
                                      game_id=g, actual=float(y), prediction=float(p))
                                 for g, y, p in zip(validation.game_id, validation[target], values))
            choice = select_config(candidates, metric)
            config = choice['config']
            selections.append(dict(task=task, model=name, config=config, metric=metric,
                                   validation_value=choice[metric],
                                   n_trees=fitted[config].get('n_trees')))
            # Test is evaluated once for the validation-selected configuration. No test early stop.
            started = time.perf_counter()
            if name == 'constant':
                values, diagnostic = np.full(len(test), test_train[target].mean()), {}
            elif name in ('lightgbm', 'xgboost'):
                model, diagnostic = fit_booster(test_train, features, task, name, config,
                                               frozen_trees=fitted[config]['n_trees'])
                values = predict(model, task, test[features])
            else:
                model = ordinary_model(task, name)
                model.fit(test_train[features], test_train[target])
                values, diagnostic = predict(model, task, test[features]), {}
            metrics.append(dict(task=task, model=name, config=config, split='historical_test',
                                n_train=len(test_train), n_eval=len(test),
                                seconds=time.perf_counter()-started, **score(task, test[target], values)))
            fits.append(dict(task=task, model=name, split='historical_test', config=config, **diagnostic))
            forecasts.extend(dict(task=task, model=name, config=config, split='historical_test',
                                  game_id=g, actual=float(y), prediction=float(p))
                             for g, y, p in zip(test.game_id, test[target], values))
    tables = {'tabular_metrics': pd.DataFrame(metrics), 'tabular_predictions': pd.DataFrame(forecasts),
              'tabular_fits': pd.DataFrame(fits), 'validation_selections': pd.DataFrame(selections)}
    for name, table in tables.items():
        table.to_csv(output / f'{name}.csv', index=False)
    return tables
