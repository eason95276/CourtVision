"""Offline, source-bound postgame calculations; no model or network access."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = Path(__file__).with_name('manifest.json')
FIELDS = {
    'points': 'teamScore', 'opponent_points': 'opponentScore',
    'fgm': 'fieldGoalsMade', 'fga': 'fieldGoalsAttempted',
    'fg3m': 'threePointersMade', 'fg3a': 'threePointersAttempted',
    'ftm': 'freeThrowsMade', 'fta': 'freeThrowsAttempted',
    'oreb': 'reboundsOffensive', 'dreb': 'reboundsDefensive',
    'reb': 'reboundsTotal', 'ast': 'assists', 'tov': 'turnovers',
    'team_tov': 'turnoversTeam', 'team_reb': 'reboundsTeam',
}


class AnalysisError(ValueError):
    pass


def sha256(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def integer(value):
    if not re.fullmatch(r'\d+', str(value)):
        raise AnalysisError(f'Expected nonnegative integer: {value!r}')
    return int(value)


def game_id(value):
    if not re.fullmatch(r'\d{1,10}', str(value)):
        raise AnalysisError('Invalid game ID')
    return str(value).zfill(10)


def ratio(made, attempted):
    return {'numerator': made, 'denominator': attempted,
            'value': made / attempted if attempted else None,
            'unit': 'fraction', 'method': 'sum(made)/sum(attempted)'}


def validate_manifest(manifest):
    games = manifest['games']
    ids = [g['game_id'] for g in games]
    teams = {integer(t) for t in manifest['teams']}
    if (len(teams) != 2 or not 4 <= len(games) <= 7
            or len(ids) != len(set(ids))):
        raise AnalysisError('Invalid series roster or duplicate manifest IDs')
    wins = dict.fromkeys(teams, 0)
    previous = ''
    for n, g in enumerate(games, 1):
        if (game_id(g['game_id']) != g['game_id'] or g['number'] != n
                or {g['home_team_id'], g['away_team_id']} != teams
                or g['date'] < previous or g['status'] != 'Final'
                or max(wins.values()) >= 4):
            raise AnalysisError('Invalid series identity, order or post-clinch game')
        a, b = integer(g['home_score']), integer(g['away_score'])
        if a == b:
            raise AnalysisError('Final game cannot be tied')
        wins[g['home_team_id'] if a > b else g['away_team_id']] += 1
        previous = g['date']
    if max(wins.values()) != 4:
        raise AnalysisError('Manifest does not describe a completed best-of-seven series')


class SeriesAnalysis:
    def __init__(self, manifest, games, rows, provenance=None):
        validate_manifest(manifest)
        self.manifest = manifest
        self.provenance = provenance or {}
        self.expected = {g['game_id']: g for g in manifest['games']}
        self.games, self.rows, self.missing = {}, {}, {}
        game_rows = {}
        for row in games:
            gid = game_id(row['gameId'])
            if gid not in self.expected:
                continue
            if gid in game_rows:
                raise AnalysisError(f'Duplicate Games.csv game: {gid}')
            game_rows[gid] = row
        grouped = {gid: [] for gid in self.expected}
        for row in rows:
            gid = game_id(row['gameId'])
            if gid in grouped:
                grouped[gid].append(row)
        for gid, expected in self.expected.items():
            raw = game_rows.get(gid)
            if raw is not None:
                self._validate_game(raw, expected)
                self.games[gid] = expected
            team_rows = grouped[gid]
            keys = [integer(r['teamId']) for r in team_rows]
            if len(keys) != len(set(keys)):
                raise AnalysisError(f'Duplicate team row: {gid}')
            if any(k not in {expected['home_team_id'], expected['away_team_id']} for k in keys):
                raise AnalysisError(f'Unexpected team: {gid}')
            reasons = []
            if raw is None:
                reasons.append('missing_game_record')
            if set(keys) != {expected['home_team_id'], expected['away_team_id']}:
                reasons.append('missing_team_row')
            normalized = []
            for r in team_rows:
                absent = [field for field in FIELDS.values() if r.get(field) in (None, '')]
                if absent:
                    reasons.append(f"missing_stats:{r['teamId']}:{','.join(absent)}")
                    continue
                normalized.append(self._normalize(r, expected))
            if reasons:
                self.missing[gid] = reasons
            else:
                self.rows[gid] = normalized

    @staticmethod
    def _validate_game(raw, expected):
        for field, source in [('home_team_id', 'hometeamId'), ('away_team_id', 'awayteamId'),
                              ('home_score', 'homeScore'), ('away_score', 'awayScore')]:
            if integer(raw[source]) != expected[field]:
                raise AnalysisError(f'Game/manifest mismatch: {expected["game_id"]}:{field}')
        if (raw['gameDateTimeEst'][:10] != expected['date']
                or raw['gameType'] != 'Playoffs'
                or raw['gameLabel'] != 'West First Round'
                or raw['seriesGameNumber'] != f"Game {expected['number']}"):
            raise AnalysisError('Game date/round/number mismatch')

    @staticmethod
    def _normalize(raw, expected):
        tid = integer(raw['teamId'])
        home = tid == expected['home_team_id']
        opposite = expected['away_team_id'] if home else expected['home_team_id']
        if (integer(raw['opponentTeamId']) != opposite or integer(raw['home']) != int(home)
                or raw['gameDateTimeEst'][:10] != expected['date']
                or raw['gameType'] != 'Playoffs'
                or raw['gameLabel'] != 'West First Round'
                or integer(raw['seriesGameNumber']) != expected['number']):
            raise AnalysisError('Team identity/date/round/home-side mismatch')
        r = {k: integer(raw[v]) for k, v in FIELDS.items()}
        own = expected['home_score'] if home else expected['away_score']
        against = expected['away_score'] if home else expected['home_score']
        if r['points'] != own or r['opponent_points'] != against:
            raise AnalysisError('Team score mismatch')
        if integer(raw['win']) != int(own > against) or int(raw['plusMinusPoints']) != own-against:
            raise AnalysisError('Win or signed margin mismatch')
        if (any(r[m] > r[a] for m, a in [('fgm','fga'),('fg3m','fg3a'),('ftm','fta')])
                or r['fg3m'] > r['fgm'] or r['fg3a'] > r['fga']
                or r['fgm']-r['fg3m'] > r['fga']-r['fg3a']
                or 2*r['fgm']+r['fg3m']+r['ftm'] != own
                or r['oreb']+r['dreb'] != r['reb']):
            raise AnalysisError('Inconsistent scoring/shooting/rebound arithmetic')
        return {'game_id': expected['game_id'], 'team_id': tid, 'is_home': int(home),
                'win': int(own > against), **r}

    @classmethod
    def load(cls, manifest_path=DEFAULT_MANIFEST, root=ROOT):
        manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
        loaded = {}
        for key in ('games', 'team_stats'):
            src = manifest['sources'][key]
            path = Path(root) / src['path']
            if sha256(path) != src['sha256']:
                raise AnalysisError(f'Source hash mismatch: {src["path"]}')
            with path.open(encoding='utf-8-sig', newline='') as f:
                loaded[key] = list(csv.DictReader(f))
        return cls(manifest, loaded['games'], loaded['team_stats'], {
            'manifest_sha256': sha256(manifest_path), 'sources': manifest['sources'],
            'stat_field_mapping': FIELDS,
            'row_key': ['gameId', 'teamId'],
            'sql_sha256': sha256(Path(__file__).with_name('aggregate.sql'))})

    def summarize(self, selected=None):
        ids = list(self.expected) if selected is None else list(selected)
        if not ids or len(ids) != len(set(ids)) or any(g not in self.expected for g in ids):
            raise AnalysisError('Select unique, known series game IDs')
        # Selected order is not allowed to change the series timeline.
        ids.sort(key=lambda gid: self.expected[gid]['number'])
        available = [gid for gid in ids if gid in self.rows]
        con = sqlite3.connect(':memory:')
        con.row_factory = sqlite3.Row
        cols = ['game_id', 'team_id', 'is_home', 'win', *FIELDS]
        try:
            con.execute('CREATE TABLE team_game (game_id TEXT, '+
                        ', '.join(c+' INTEGER NOT NULL' for c in cols[1:])+', PRIMARY KEY(game_id,team_id))')
            con.executemany('INSERT INTO team_game VALUES ('+','.join('?' for _ in cols)+')',
                            [tuple(r[c] for c in cols) for gid in available for r in self.rows[gid]])
            totals = [dict(r) for r in con.execute(Path(__file__).with_name('aggregate.sql').read_text())]
        finally:
            con.close()
        teams = []
        for row in totals:
            n = row['games']
            teams.append({'team_id': row['team_id'], 'team': self.manifest['teams'][str(row['team_id'])],
                          'games': n, 'wins': row['wins'], 'losses': n-row['wins'],
                          'totals': {k:v for k,v in row.items() if k not in ('team_id','games','wins')},
                          'per_team_game': {k: row[k]/n for k in FIELDS},
                          'mean_margin': row['margin']/n,
                          'shooting': {label: ratio(row[m],row[a]) for label,m,a in
                                       [('fg','fgm','fga'),('three_point','fg3m','fg3a'),('ft','ftm','fta')]}})
        complete = len(available) == len(self.expected)
        timeline = []
        wins = dict.fromkeys(self.manifest['teams'], 0)
        for gid in available:
            e = self.expected[gid]
            winner = str(e['home_team_id'] if e['home_score'] > e['away_score'] else e['away_team_id'])
            wins[winner] += 1
            timeline.append({**e, 'selected_games_wins_after': dict(wins)})
        return {'mode': 'postgame_deterministic', 'series': self.manifest['series'],
                'complete_series': complete, 'requested_game_ids': ids,
                'omitted_series_game_ids': [g for g in self.expected if g not in ids],
                'missing_game_ids': [g for g in ids if g not in self.rows],
                'missing_reasons': {g:self.missing[g] for g in ids if g in self.missing},
                'expected_series_games': len(self.expected), 'analyzed_games': len(available),
                'teams': teams, 'games': timeline,
                'series_winner': next((t['team'] for t in teams if t['wins']==4),None) if complete else None,
                'player_summary': {'status':'unavailable', 'covered_games':0,
                    'missing_game_ids':ids, 'averages':None,
                    'denominator_policy':'Actual appearances and team games must be separate; no player average without validated appearance records.'},
                'definitions': {'per_team_game':'sum statistic / analyzed complete team games; includes overtime',
                    'reb':'player rebounds only; team_reb reported separately',
                    'tov':'player turnovers only; team_tov reported separately',
                    'incomplete':'All available-game aggregates are partial, never full-series estimates'},
                'provenance': self.provenance}

    def compare(self, first, second, team_id):
        if first == second:
            raise AnalysisError('Comparison requires two different games')
        result = self.summarize([first, second])
        if str(team_id) not in self.manifest['teams']:
            raise AnalysisError('Unknown perspective team')
        result['perspective_team_id'] = team_id
        result['comparison_order'] = [first, second]
        result['delta_second_minus_first'] = None
        if not result['missing_game_ids']:
            a,b = [next(r for r in self.rows[g] if r['team_id']==team_id) for g in (first,second)]
            result['perspective_games'] = [a,b]
            result['delta_second_minus_first'] = {k:b[k]-a[k] for k in FIELDS}
            result['delta_second_minus_first']['margin'] = (b['points']-b['opponent_points'])-(a['points']-a['opponent_points'])
            result['shooting_by_game'] = [{k:ratio(r[m],r[n]) for k,m,n in
                [('fg','fgm','fga'),('three_point','fg3m','fg3a'),('ft','ftm','fta')]} for r in (a,b)]
        return result
