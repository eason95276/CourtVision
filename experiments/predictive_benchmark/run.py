"""Usage: python run.py --dataset PATH --output NEW_DIRECTORY (offline only)."""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
import time

from threadpoolctl import threadpool_limits

SUITE = Path(__file__).resolve().parent
ROOT = SUITE.parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from benchmark import CONFIGS, run_benchmark
from data_contract import file_hash, load_dataset, new_run_directory
from timeseries import run_timeseries, summarize_timeseries


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def protection_snapshot():
    paths = set((ROOT / 'src').rglob('*.py')) | set((ROOT / 'artifacts').glob('*'))
    for folder in ('v2/pregame_system', 'v2/frontend', 'v2/postgame_system'):
        paths.update((ROOT / folder).glob('*.py'))
    for folder in ('agent/skills', 'v2/pregame_system/models', 'v2/frontend/static'):
        paths.update(p for p in (ROOT / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    return {p.relative_to(ROOT).as_posix(): file_hash(p) for p in sorted(paths) if p.is_file()}


def markdown_table(frame, columns):
    def render(value):
        return f'{value:.4f}' if isinstance(value, float) else str(value)
    lines = ['| ' + ' | '.join(columns) + ' |', '| ' + ' | '.join(['---']*len(columns)) + ' |']
    for _, row in frame.iterrows():
        lines.append('| ' + ' | '.join(render(row.get(c, '')) for c in columns) + ' |')
    return '\n'.join(lines)


def report(output, tables, time_metrics, manifest):
    lines = ['# CourtVision Offline Predictive Benchmark', '',
             '## Scope and evidence boundary', '',
             '- Real local historical training, not an LLM call or a production promotion.',
             '- 2024 outer validation; 2025 previously inspected historical evaluation. Not a new blind test.',
             '- Five prior seasons per outer split. Last training season alone controls boosted early stopping; selected tree count is frozen before full-window refit.',
             '- Two fixed boosted configurations; selection uses validation metrics only. No test-driven tuning.',
             '- HGB early_stopping=False for an explicit chronological comparison; not a byte-identical rerun of the old baseline.',
             '- No basketball accuracy uplift, market outperformance, profitability or per-game interval claim is implied.', '',
             f"Input rows: {manifest['input_rows']}; frozen features: {manifest['feature_count']}.", '',
             '## Tabular historical evaluation (all three targets)', '']
    table = tables['tabular_metrics']
    for task in ('win_probability', 'expected_margin', 'expected_total'):
        view = table[(table.task == task) & (table.split == 'historical_test')]
        columns = ['model', 'n_train', 'n_eval', 'log_loss', 'brier_score', 'ece', 'accuracy'] if task == 'win_probability' else [
            'model', 'n_train', 'n_eval', 'mae', 'rmse']
        lines.extend([f'### {task}', '', markdown_table(view, columns), ''])
    lines.extend(['## Team-points time series (separate task)', '',
                  '- First six numeric team IDs present in the historical evaluation season, selected before modeling and without results-based selection.',
                  '- Regular-season points only; scores reconstructed from total and home-positive margin, with integer/identity checks.',
                  '- Fixed ARIMA(1,0,0), last 60 completed team games, minimum 30; re-estimated before each target. No order search.',
                  '- Game-indexed sequence; no off-day zeroes, invented weekly seasonality or calendar-day equivalence.',
                  '- Previous evaluation-game results become available only to later walk-forward origins, not earlier predictions.',
                  '- Comparison on shared successful targets; failed/nonconverged fits remain missing with diagnostics, not replaced by a baseline.',
                  '- This task predicts team points, not the tabular single-game total; do not compare their MAEs as if they share a target.', '',
                  markdown_table(time_metrics[time_metrics.scope == 'all'], [
                      'method', 'n_targets', 'n_success', 'n_failed', 'n_insufficient', 'n_common', 'common_mae', 'common_rmse']), '',
                  '## Reproducibility and protection', '',
                  '- Exact dependency versions, input/config/code hashes and timing are in manifest.json.',
                  '- Source and fixed model/artifact hashes are checked before/after in protection.json; original files are never overwritten.',
                  '- Read-only local CSV, no network, no production database access or publication.',
                  '- Original raw data are not re-exported here. Derived per-game result files remain local pending license/public-release review.',
                  '- Walk-forward is retrospective: no claim of a contemporaneously captured live point-in-time feed.', ''])
    (output / 'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='must not already exist')
    args = parser.parse_args(argv)
    output = new_run_directory(args.output)
    started = time.perf_counter()
    protected = protection_snapshot()
    dump(output / 'protection-before.json', protected)
    manifest = {'created_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'RUNNING',
                'production_calls': 0, 'llm_calls': 0, 'network_calls': 0,
                'configuration': {'validation_season': 2024, 'historical_test_season': 2025,
                                  'window_years': 5, 'seed': 42, 'threads': 2, 'boosted_grid': CONFIGS,
                                  'arima_order': [1, 0, 0], 'arima_history_games': 60,
                                  'arima_min_history': 30, 'team_selection': 'first_six_sorted_numeric_ids'},
                'python': platform.python_version(),
                'packages': {name: importlib.metadata.version(name) for name in [
                    'numpy', 'pandas', 'scipy', 'scikit-learn', 'lightgbm', 'xgboost', 'statsmodels', 'pytest']},
                'code_hashes': {p.name: file_hash(p) for p in SUITE.glob('*.py')}}
    dump(output / 'manifest.json', manifest)
    try:
        # Portable metadata only: no dependency on undistributed private artifact files.
        contract = json.loads((SUITE / 'input_contract.json').read_text(encoding='utf-8'))
        expected = contract['dataset_sha256']
        features = contract['feature_columns']
        frame = load_dataset(args.dataset, features, expected)
        manifest.update(input_sha256=expected, input_rows=len(frame), feature_count=len(features),
                        feature_columns=features, dataset_name=args.dataset.name,
                        input_contract_hash=file_hash(SUITE / 'input_contract.json'),
                        parent_feature_manifest_hash=contract['feature_manifest_sha256'],
                        parent_reproducibility_manifest_hash=contract['reproducibility_manifest_sha256'])
        dump(output / 'manifest.json', manifest)
        print(f'Input verified: {len(frame)} rows; {len(features)} features', flush=True)
        with threadpool_limits(limits=2):
            tables = run_benchmark(frame, features, output)
            time_rows = run_timeseries(frame)
        time_metrics, common = summarize_timeseries(time_rows)
        time_rows.to_csv(output / 'timeseries_predictions.csv', index=False)
        time_metrics.to_csv(output / 'timeseries_metrics.csv', index=False)
        common.to_csv(output / 'timeseries_common_predictions.csv', index=False)
        manifest['time_series_teams'] = sorted(int(t) for t in time_rows.team_id.unique())
        manifest['time_series_unique_targets'] = int(len(time_rows[['team_id', 'game_id']].drop_duplicates()))
        manifest['status'] = 'COMPLETED'
        report(output, tables, time_metrics, manifest)
    except Exception as error:
        manifest.update(status='FAILED', failure=f'{type(error).__name__}: {error}')
        raise
    finally:
        after = protection_snapshot()
        changed = [key for key in sorted(set(protected) | set(after)) if protected.get(key) != after.get(key)]
        dump(output / 'protection.json', {'protected_file_count': len(protected), 'changed': changed,
                                         'unchanged': not changed, 'before': protected, 'after': after})
        manifest.update(elapsed_seconds=time.perf_counter()-started, protected_files_unchanged=not changed,
                        input_unchanged=file_hash(args.dataset) == manifest.get('input_sha256'),
                        output_hashes={p.name: file_hash(p) for p in output.iterdir() if p.is_file() and p.name != 'manifest.json'})
        if changed:
            manifest['status'] = 'PROTECTION_FAILURE'
        dump(output / 'manifest.json', manifest)
    if manifest['status'] != 'COMPLETED':
        return 1
    print(f'Completed: {output}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
