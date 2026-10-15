"""Read-only input contracts for the isolated historical experiments."""
from hashlib import sha256
from pathlib import Path

import numpy as np
import pandas as pd

IDENTITY = ['game_id', 'game_date', 'season_start', 'game_type', 'home_team_id',
            'away_team_id', 'home_team', 'away_team']
TARGETS = ['home_win', 'point_margin', 'total_points']


def file_hash(path):
    digest = sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def new_run_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False)
    return path


def validate_frame(frame, features):
    if not features or len(features) != len(set(features)):
        raise ValueError('empty or duplicate feature list')
    # Only the frozen allowlist is passed by the CLI. Targets and identity are never features.
    if set(features) & set(IDENTITY + TARGETS):
        raise ValueError('target/identity feature is forbidden')
    missing = set(IDENTITY + TARGETS + features) - set(frame.columns)
    if missing:
        raise ValueError(f'missing columns: {sorted(missing)}')
    if frame.empty or frame.game_id.isna().any() or frame.game_id.duplicated().any():
        raise ValueError('empty input or duplicate/missing game identity')
    if frame[IDENTITY + TARGETS].isna().any().any():
        raise ValueError('missing identity or target')
    if (frame.home_team_id == frame.away_team_id).any():
        raise ValueError('same home and away identity')
    dates = pd.to_datetime(frame.game_date, errors='raise')
    if dates.isna().any():
        raise ValueError('missing game date')
    for column in features + TARGETS:
        values = pd.to_numeric(frame[column], errors='raise').to_numpy(dtype=float)
        if np.isinf(values).any():
            raise ValueError(f'infinite numeric column: {column}')
    home = (frame.total_points + frame.point_margin) / 2
    away = (frame.total_points - frame.point_margin) / 2
    if ((home <= 0) | (away <= 0) | (home % 1 != 0) | (away % 1 != 0)).any():
        raise ValueError('invalid score reconstruction')
    if not frame.home_win.isin([0, 1]).all() or not np.array_equal(
            (frame.point_margin > 0).astype(int), frame.home_win):
        raise ValueError('winner/score inconsistency')
    if (frame.point_margin == 0).any():
        raise ValueError('tied final score')


def load_dataset(path, features, expected_hash):
    if file_hash(path) != expected_hash:
        raise ValueError('processed input checksum mismatch')
    frame = pd.read_csv(path, dtype={'game_id': str})
    frame['game_date'] = pd.to_datetime(frame.game_date, errors='raise')
    validate_frame(frame, features)
    return frame.sort_values(['game_date', 'game_id']).reset_index(drop=True)


def chronological_split(frame, evaluation_season, window=5):
    train = frame[frame.season_start.between(evaluation_season-window, evaluation_season-1)].copy()
    evaluation = frame[frame.season_start == evaluation_season].copy()
    expected = list(range(evaluation_season-window, evaluation_season))
    if sorted(train.season_start.unique()) != expected or evaluation.empty:
        raise ValueError(f'incomplete training/evaluation seasons: expected {expected}')
    if train.game_date.max() >= evaluation.game_date.min():
        raise ValueError('chronological overlap')
    return train, evaluation
