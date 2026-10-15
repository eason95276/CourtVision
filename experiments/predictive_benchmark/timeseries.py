"""Event-indexed team points: one-step walk-forward, never calendar-day zero filling."""
import json
import time
import warnings

import numpy as np
import pandas as pd

from courtvision.metrics import regression_metrics

METHODS = ['last_game', 'rolling_mean_10', 'ewma_02', 'arima_100']


def team_games(frame):
    # Derived outcomes are used only as historical targets, never as same-game input features.
    regular = frame[frame.game_type == 'Regular Season']
    pieces = []
    for side, sign in [('home', 1), ('away', -1)]:
        piece = regular[['game_id', 'game_date', 'season_start']].copy()
        piece['team_id'] = regular[f'{side}_team_id'].to_numpy()
        piece['team_name'] = regular[f'{side}_team'].to_numpy()
        piece['points'] = ((regular.total_points + sign*regular.point_margin)/2).to_numpy()
        pieces.append(piece)
    rows = pd.concat(pieces, ignore_index=True).sort_values(['team_id', 'game_date', 'game_id'])
    if rows.duplicated(['team_id', 'game_id']).any() or rows.duplicated(['team_id', 'game_date']).any():
        raise ValueError('ambiguous duplicate team/game/time')
    return rows


def history_before(rows, cutoff, window=60):
    return rows[rows.game_date < cutoff].sort_values(['game_date', 'game_id']).tail(window).copy()


def forecast_one(values, method):
    if method == 'last_game':
        return float(values[-1]), {}
    if method == 'rolling_mean_10':
        return float(np.mean(values[-10:])), {}
    if method == 'ewma_02':
        return float(pd.Series(values).ewm(alpha=0.2, adjust=False).mean().iloc[-1]), {}
    if method != 'arima_100':
        raise ValueError(f'unknown method: {method}')
    from statsmodels.tsa.arima.model import ARIMA
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        fitted = ARIMA(values, order=(1, 0, 0), trend='c', enforce_stationarity=True).fit(
            method_kwargs={'maxiter': 100})
    if not fitted.mle_retvals.get('converged', False):
        raise RuntimeError('ARIMA optimization did not converge; no fallback')
    return float(np.asarray(fitted.forecast(1))[0]), {
        'warnings': [str(w.message) for w in caught], 'converged': True,
        'params': np.asarray(fitted.params).tolist()}


def run_timeseries(frame, test_season=2025, min_history=30, forecaster=forecast_one):
    rows = team_games(frame)
    teams = sorted(rows.loc[rows.season_start == test_season, 'team_id'].unique())[:6]
    results = []
    for team_id in teams:
        team = rows[rows.team_id == team_id]
        targets = team[team.season_start == test_season]
        print(f'Time series: team {team_id}, {len(targets)} target games', flush=True)
        for target in targets.itertuples(index=False):
            history = history_before(team, target.game_date, 60)
            for method in METHODS:
                start = time.perf_counter()
                record = dict(team_id=int(team_id), team_name=target.team_name,
                              game_id=target.game_id, game_date=target.game_date.isoformat(),
                              method=method, actual=float(target.points), prediction=np.nan,
                              n_history=len(history), history_last_game_id=history.game_id.iloc[-1] if len(history) else None,
                              history_end=history.game_date.max().isoformat() if len(history) else None,
                              status='insufficient_history', error=None, diagnostics='{}')
                if len(history) >= min_history:
                    try:
                        value, diagnostic = forecaster(history.points.to_numpy(dtype=float), method)
                        if not np.isfinite(value):
                            raise ValueError('non-finite forecast')
                        record.update(prediction=value, status='success', diagnostics=json.dumps(diagnostic))
                    except Exception as error:
                        record.update(status='failed', error=f'{type(error).__name__}: {error}')
                record['seconds'] = time.perf_counter()-start
                results.append(record)
    return pd.DataFrame(results)


def summarize_timeseries(predictions):
    if predictions.empty:
        return pd.DataFrame(), pd.DataFrame()
    eligible = predictions.status.eq('success')
    counts = predictions.assign(ok=eligible).groupby(['team_id', 'game_id']).ok.sum()
    common_keys = set(counts[counts == len(METHODS)].index)
    common_mask = pd.Series([(team, game) in common_keys for team, game in zip(
        predictions.team_id, predictions.game_id)], index=predictions.index)
    metrics = []
    for scope, data in [('all', predictions)] + [
            (str(team), predictions[predictions.team_id == team]) for team in sorted(predictions.team_id.unique())]:
        for method in METHODS:
            group = data[data.method == method]
            good = group[group.status == 'success']
            common = good.loc[common_mask.loc[good.index]]
            row = dict(scope=scope, method=method, n_targets=len(group), n_success=len(good),
                       n_failed=int(group.status.eq('failed').sum()),
                       n_insufficient=int(group.status.eq('insufficient_history').sum()), n_common=len(common))
            if len(good):
                row.update(regression_metrics(good.actual, good.prediction))
            if len(common):
                row.update({f'common_{k}': v for k, v in regression_metrics(common.actual, common.prediction).items()})
            metrics.append(row)
    return pd.DataFrame(metrics), predictions.loc[common_mask].copy()
