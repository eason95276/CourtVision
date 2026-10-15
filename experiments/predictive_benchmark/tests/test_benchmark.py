from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

SUITE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE))
sys.path.insert(0, str(SUITE.parents[1] / 'src'))

from data_contract import chronological_split, load_dataset, new_run_directory, validate_frame
from timeseries import team_games, history_before, forecast_one, run_timeseries, summarize_timeseries
from benchmark import make_booster, select_config, fit_booster


def fixture_frame():
    rows = []
    for season in range(2019, 2026):
        for index in range(8):
            rows.append(dict(game_id=f'{season}-{index}', game_date=pd.Timestamp(season, 10, index+1),
                             season_start=season, game_type='Regular Season', home_team_id=1,
                             away_team_id=2, home_team='One', away_team='Two',
                             home_win=1, point_margin=10, total_points=210, lag=float(index)))
    return pd.DataFrame(rows)


def test_chronological_outer_split_and_training_only_early_stop():
    train, valid = chronological_split(fixture_frame(), 2024)
    core, early = chronological_split(train, 2023, window=4)
    assert sorted(train.season_start.unique()) == list(range(2019, 2024))
    assert core.game_date.max() < early.game_date.min() < valid.game_date.min()
    assert set(core.game_id).isdisjoint(early.game_id)


def test_missing_training_season_is_rejected():
    frame = fixture_frame()
    with pytest.raises(ValueError, match='complete'):
        chronological_split(frame[frame.season_start != 2021], 2024)


@pytest.mark.parametrize('column', ['total_points', 'point_margin', 'home_win', 'game_id'])
def test_outcome_or_identity_cannot_be_a_model_feature(column):
    with pytest.raises(ValueError, match='feature'):
        validate_frame(fixture_frame(), ['lag', column])


def test_duplicate_game_is_rejected():
    frame = fixture_frame()
    with pytest.raises(ValueError, match='duplicate'):
        validate_frame(pd.concat([frame, frame.iloc[:1]]), ['lag'])


def test_checksum_mismatch_rejected_before_csv_parse(tmp_path):
    path = tmp_path / 'data.csv'
    path.write_text('not csv', encoding='utf-8')
    with pytest.raises(ValueError, match='checksum'):
        load_dataset(path, ['lag'], '0' * 64)


def test_new_run_never_overwrites(tmp_path):
    new_run_directory(tmp_path / 'run')
    with pytest.raises(FileExistsError):
        new_run_directory(tmp_path / 'run')


def test_team_points_direction_and_no_synthetic_off_days():
    rows = team_games(fixture_frame())
    assert set(rows[rows.team_id == 1].points) == {110.0}
    assert set(rows[rows.team_id == 2].points) == {100.0}
    assert len(rows) == 2 * len(fixture_frame())


def test_same_time_and_future_outcome_not_used():
    rows = team_games(fixture_frame())
    cutoff = pd.Timestamp(2025, 10, 4)
    history = history_before(rows[rows.team_id == 1], cutoff, 60)
    changed = rows.copy()
    changed.loc[changed.game_date >= cutoff, 'points'] = 999
    again = history_before(changed[changed.team_id == 1], cutoff, 60)
    pd.testing.assert_frame_equal(history, again)
    assert history.game_date.max() < cutoff


def test_arima_current_or_future_target_cannot_change_prior_prediction():
    history = np.arange(60, dtype=float) % 7 + 100
    first, _ = forecast_one(history, 'arima_100')
    second, _ = forecast_one(history.copy(), 'arima_100')
    assert first == pytest.approx(second)


def test_arima_failure_recorded_no_fallback():
    def broken(*args):
        raise RuntimeError('deliberate fit failure')
    rows = run_timeseries(fixture_frame(), test_season=2025, min_history=10,
                          forecaster=broken)
    assert len(rows) == 2 * 8 * 4
    assert set(rows.status) == {'failed'}
    assert rows.prediction.isna().all()
    metrics, common = summarize_timeseries(rows)
    assert set(metrics.n_success) == {0}
    assert set(metrics[metrics.scope == 'all'].n_failed) == {16}
    assert set(metrics[metrics.scope != 'all'].n_failed) == {8}
    assert common.empty


def test_identity_only_team_selection_and_unknown_history():
    frame = fixture_frame()
    changed = frame.copy()
    changed.total_points += 20
    first = run_timeseries(frame, test_season=2025, min_history=999)
    second = run_timeseries(changed, test_season=2025, min_history=999)
    assert set(first.team_id) == set(second.team_id) == {1, 2}
    assert set(first.status) == {'insufficient_history'}


def test_validation_selects_without_test_metrics():
    candidates = [dict(config=0, mae=10), dict(config=1, mae=9)]
    assert select_config(candidates, 'mae')['config'] == 1


@pytest.mark.parametrize('library', ['lightgbm', 'xgboost'])
def test_real_booster_training_and_chronological_fit_metadata(library):
    frame = fixture_frame()
    train, _ = chronological_split(frame, 2024)
    model, diagnostic = fit_booster(train, ['lag'], 'expected_total', library, 0)
    prediction = model.predict(frame[frame.season_start == 2024][['lag']])
    assert np.isfinite(prediction).all()
    assert diagnostic['early_stop_season'] == 2023
    assert diagnostic['core_train_end'] < diagnostic['early_stop_start']
    assert diagnostic['refit_n'] == len(train)


@pytest.mark.parametrize('library', ['lightgbm', 'xgboost'])
def test_booster_seed_and_thread_limit(library):
    model = make_booster('expected_total', library, 0, 10)
    assert model.get_params()['random_state'] == 42
    assert model.get_params()['n_jobs'] == 2


@pytest.mark.parametrize('library', ['lightgbm', 'xgboost'])
def test_historical_test_refit_uses_frozen_tree_count_without_early_stop(library):
    train, _ = chronological_split(fixture_frame(), 2025)
    model, diagnostic = fit_booster(train, ['lag'], 'expected_total', library, 0, frozen_trees=7)
    assert model.get_params()['n_estimators'] == 7
    assert diagnostic['early_stop_season'] is None
    assert 'early_stop_start' not in diagnostic


def test_invalid_scores_and_team_identity_rejected():
    frame = fixture_frame()
    frame.loc[0, 'total_points'] = 211
    with pytest.raises(ValueError, match='score'):
        validate_frame(frame, ['lag'])
    frame = fixture_frame()
    frame.loc[0, 'away_team_id'] = 1
    with pytest.raises(ValueError, match='identity'):
        validate_frame(frame, ['lag'])


def test_production_market_not_implicitly_selected_as_feature():
    from benchmark import TASKS
    assert set(TASKS.values()) == {'home_win', 'point_margin', 'total_points'}
    # Only an explicit feature list is consumed, never every numeric column.
    frame = fixture_frame()
    frame['market_total'] = 999
    validate_frame(frame, ['lag'])


def test_partial_arima_failure_uses_shared_rows_and_preserves_denominator():
    rows = run_timeseries(fixture_frame(), test_season=2025, min_history=10,
                          forecaster=lambda values, method: (100.0, {}))
    failed = rows[(rows.method == 'arima_100')].index[0]
    rows.loc[failed, ['status', 'prediction']] = ['failed', np.nan]
    metrics, common = summarize_timeseries(rows)
    all_metrics = metrics[metrics.scope == 'all'].set_index('method')
    assert all_metrics.loc['arima_100', 'n_success'] == 15
    assert all_metrics.loc['last_game', 'n_success'] == 16
    assert set(all_metrics.n_common) == {15}
    assert len(common) == 15 * 4


def test_all_three_tasks_produce_validation_selected_test_outputs(tmp_path):
    from benchmark import run_benchmark, TASKS
    frame = fixture_frame()
    frame.loc[frame.index % 2 == 0, ['home_win', 'point_margin']] = [0, -10]
    tables = run_benchmark(frame, ['lag'], tmp_path)
    metrics = tables['tabular_metrics']
    tests = metrics[metrics.split == 'historical_test']
    assert len(tests) == 3 * 5
    assert set(tests.task) == set(TASKS)
    assert set(tests.n_eval) == {8}
    predictions = tables['tabular_predictions']
    assert len(predictions[predictions.split == 'historical_test']) == 3 * 5 * 8
    for row in tables['validation_selections'].itertuples(index=False):
        candidates = metrics[(metrics.task == row.task) & (metrics.model == row.model) & (metrics.split == 'validation')]
        assert row.validation_value == pytest.approx(candidates[row.metric].min())
    assert {p.name for p in tmp_path.glob('*.csv')} == {
        'tabular_metrics.csv', 'tabular_predictions.csv', 'tabular_fits.csv', 'validation_selections.csv'}


def test_walk_forward_current_outcome_changes_actual_not_own_prediction():
    frame = fixture_frame()
    cutoff = pd.Timestamp(2025, 10, 4)
    changed = frame.copy()
    changed.loc[changed.game_date >= cutoff, 'total_points'] += 20
    simple = lambda values, method: (float(np.mean(values)), {})
    before = run_timeseries(frame, min_history=10, forecaster=simple)
    after = run_timeseries(changed, min_history=10, forecaster=simple)
    prior = pd.to_datetime(before.game_date) <= cutoff
    np.testing.assert_array_equal(before.loc[prior, 'prediction'], after.loc[prior, 'prediction'])
    target = pd.to_datetime(before.game_date) == cutoff
    assert not np.array_equal(before.loc[target, 'actual'], after.loc[target, 'actual'])
    later = pd.to_datetime(before.game_date) > cutoff
    assert not np.array_equal(before.loc[later, 'prediction'], after.loc[later, 'prediction'])


def test_nonconverged_arima_is_failure_not_numeric_result(monkeypatch):
    import statsmodels.tsa.arima.model as arima_module
    from types import SimpleNamespace
    class Nonconverged:
        def __init__(self, *args, **kwargs):
            pass
        def fit(self, **kwargs):
            return SimpleNamespace(mle_retvals={'converged': False})
    monkeypatch.setattr(arima_module, 'ARIMA', Nonconverged)
    with pytest.raises(RuntimeError, match='did not converge'):
        forecast_one(np.arange(60, dtype=float), 'arima_100')


def test_artifact_verifier_detects_tampering(tmp_path):
    import json
    from data_contract import file_hash
    from verify_run import verify
    artifact = tmp_path / 'example.csv'
    artifact.write_text('original', encoding='utf-8')
    (tmp_path / 'manifest.json').write_text(json.dumps({
        'status': 'COMPLETED', 'output_hashes': {'example.csv': file_hash(artifact)}}), encoding='utf-8')
    artifact.write_text('modified', encoding='utf-8')
    with pytest.raises(ValueError, match='hash mismatch'):
        verify(tmp_path)


def test_public_input_contract_has_only_frozen_metadata_not_raw_rows():
    import json
    contract = json.loads((SUITE / 'input_contract.json').read_text(encoding='utf-8'))
    assert contract['dataset_sha256'] == 'e788e2bd8fa688a7c21aa0979c9091dbafb58b382f42a5adc6af4b0c9aaf262a'
    assert len(contract['feature_columns']) == 85
    assert not set(contract['feature_columns']) & {'total_points', 'point_margin', 'home_win', 'market_total'}
    assert set(contract) == {'dataset_sha256', 'feature_columns', 'feature_manifest_sha256',
                             'reproducibility_manifest_sha256', 'dataset_note'}
