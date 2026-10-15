"""Independently check a completed run's hashes and chronological/selection contracts."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from data_contract import file_hash


def verify(output):
    output = Path(output)
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['status'] != 'COMPLETED':
        raise ValueError('run is not completed')
    for name, expected in manifest['output_hashes'].items():
        if file_hash(output / name) != expected:
            raise ValueError(f'artifact hash mismatch: {name}')
    if not manifest['protected_files_unchanged'] or not manifest['input_unchanged']:
        raise ValueError('protection failed')
    metrics = pd.read_csv(output / 'tabular_metrics.csv')
    predictions = pd.read_csv(output / 'tabular_predictions.csv', dtype={'game_id': str})
    selections = pd.read_csv(output / 'validation_selections.csv')
    fits = pd.read_csv(output / 'tabular_fits.csv')
    for selection in selections.itertuples(index=False):
        rows = metrics[(metrics.task == selection.task) & (metrics.model == selection.model)]
        validation = rows[rows.split == 'validation']
        winner = validation.sort_values([selection.metric, 'config']).iloc[0]
        test = rows[rows.split == 'historical_test']
        if len(test) != 1 or int(winner.config) != selection.config or int(test.iloc[0].config) != selection.config:
            raise ValueError('test/validation-selected configuration mismatch')
    for row in metrics.itertuples(index=False):
        matched = predictions[(predictions.task == row.task) & (predictions.model == row.model)
                              & (predictions.config == row.config) & (predictions.split == row.split)]
        if len(matched) != row.n_eval or matched.game_id.duplicated().any():
            raise ValueError('prediction denominator/identity mismatch')
        if not np.isfinite(matched.prediction).all():
            raise ValueError('nonfinite tabular result')
    inner = fits[fits.early_stop_season.notna()]
    if not (pd.to_datetime(inner.core_train_end) < pd.to_datetime(inner.early_stop_start)).all():
        raise ValueError('early stop temporal overlap')
    if not (inner.early_stop_season == 2023).all():
        raise ValueError('early stop leaked out of training')
    for row in fits[(fits.split == 'historical_test') & fits.model.isin(['lightgbm', 'xgboost'])].itertuples(index=False):
        selection = selections[(selections.task == row.task) & (selections.model == row.model)].iloc[0]
        if row.n_trees != selection.n_trees or not pd.isna(row.early_stop_season):
            raise ValueError('test refit tree count or early stop violation')
    time_rows = pd.read_csv(output / 'timeseries_predictions.csv', dtype={'game_id': str})
    if not (pd.to_datetime(time_rows.history_end) < pd.to_datetime(time_rows.game_date)).all():
        raise ValueError('current/future game in time-series history')
    if time_rows.duplicated(['team_id', 'game_id', 'method']).any():
        raise ValueError('duplicate time-series prediction')
    failures = time_rows.status != 'success'
    if time_rows.loc[failures, 'prediction'].notna().any():
        raise ValueError('failed time-series fit substituted a prediction')
    summary = {'verified': True, 'output_hashes_checked': len(manifest['output_hashes']),
               'tabular_metric_rows': len(metrics), 'tabular_prediction_rows': len(predictions),
               'time_series_prediction_rows': len(time_rows),
               'time_series_unique_team_game_targets': len(time_rows[['team_id', 'game_id']].drop_duplicates()),
               'time_series_failures': int(failures.sum()),
               'protected_file_count': json.loads((output / 'protection.json').read_text(encoding='utf-8'))['protected_file_count']}
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    print(json.dumps(verify(parser.parse_args().output), indent=2))
