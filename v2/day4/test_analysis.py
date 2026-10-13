import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

from .analysis import AnalysisError, DEFAULT_MANIFEST, ROOT, SeriesAnalysis, ratio


class Day4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest=json.loads(DEFAULT_MANIFEST.read_text())
        ids={g['game_id'] for g in cls.manifest['games']}
        def read(name):
            with (ROOT/'data/raw'/name).open(encoding='utf-8-sig',newline='') as f:
                return [r for r in csv.DictReader(f) if r['gameId'].zfill(10) in ids]
        cls.games=read('Games.csv');cls.rows=read('TeamStatistics.csv')

    def engine(self,rows=None,games=None,manifest=None):
        return SeriesAnalysis(manifest or self.manifest, self.games if games is None else games,
                              self.rows if rows is None else rows)

    def test_real_full_series_independent_totals(self):
        r=SeriesAnalysis.load().summarize();self.assertTrue(r['complete_series'])
        for t in r['teams']:
            raw=[x for x in self.rows if int(x['teamId'])==t['team_id']]
            self.assertEqual(t['totals']['points'],sum(int(x['teamScore']) for x in raw))
            self.assertEqual(t['totals']['reb'],sum(int(x['reboundsTotal']) for x in raw))
        lal=next(t for t in r['teams'] if t['team']=='LAL')
        self.assertEqual((lal['wins'],lal['totals']['points'],lal['totals']['opponent_points']), (4,607,592))
        self.assertEqual(lal['shooting']['three_point']['numerator'],59)
        self.assertEqual(lal['shooting']['three_point']['denominator'],153)

    def test_home_away_perspective(self):
        r=self.engine().compare('0042500171','0042500176',1610612747)
        self.assertEqual([x['is_home'] for x in r['perspective_games']],[1,0])
        self.assertEqual(r['delta_second_minus_first']['margin'],11)
        self.assertEqual(r['delta_second_minus_first']['points'],-9)
        self.assertFalse(r['complete_series'])

    def test_reverse_comparison(self):
        self.assertEqual(self.engine().compare('0042500176','0042500171',1610612747)['delta_second_minus_first']['margin'],-11)

    def test_opponent_perspective(self):
        self.assertEqual(self.engine().compare('0042500171','0042500176',1610612745)['delta_second_minus_first']['margin'],-11)

    def test_pooled_rate_not_average_percentages(self):
        r=self.engine().summarize(['0042500171','0042500176'])
        lal=next(t for t in r['teams'] if t['team']=='LAL')
        self.assertAlmostEqual(lal['shooting']['three_point']['value'],22/47)
        self.assertNotAlmostEqual(lal['shooting']['three_point']['value'],(10/19+12/28)/2)

    def test_zero_denominator(self):
        self.assertIsNone(ratio(0,0)['value'])

    def test_missing_game(self):
        r=self.engine(games=self.games[:-1]).summarize()
        self.assertFalse(r['complete_series']);self.assertEqual(r['analyzed_games'],5)
        self.assertEqual(len(r['missing_game_ids']),1);self.assertIsNone(r['series_winner'])

    def test_missing_team_discards_both_sides(self):
        r=self.engine(rows=self.rows[:-1]).summarize()
        self.assertEqual([t['games'] for t in r['teams']],[5,5])

    def test_missing_stat_not_zero(self):
        rows=copy.deepcopy(self.rows);rows[0]['threePointersAttempted']=''
        r=self.engine(rows=rows).summarize();self.assertEqual(r['analyzed_games'],5)
        self.assertFalse(r['complete_series'])

    def test_missing_comparison_has_no_delta(self):
        rows=[r for r in self.rows if r['gameId'].zfill(10)!='0042500171']
        self.assertIsNone(self.engine(rows=rows).compare('0042500171','0042500176',1610612747)['delta_second_minus_first'])

    def test_no_rows_no_fabricated_zero_totals(self):
        r=self.engine(rows=[]).summarize();self.assertEqual(r['teams'],[])
        self.assertEqual(len(r['missing_game_ids']),6)

    def test_duplicate_team_row(self):
        with self.assertRaises(AnalysisError):self.engine(rows=self.rows+[self.rows[0]])

    def test_duplicate_game_row(self):
        with self.assertRaises(AnalysisError):self.engine(games=self.games+[self.games[0]])

    def test_duplicate_selection(self):
        with self.assertRaises(AnalysisError):self.engine().summarize(['0042500171']*2)

    def test_unknown_game(self):
        with self.assertRaises(AnalysisError):self.engine().summarize(['0022500564'])

    def test_unknown_team(self):
        with self.assertRaises(AnalysisError):self.engine().compare('0042500171','0042500176',123)

    def test_wrong_home_side(self):
        rows=copy.deepcopy(self.rows);rows[0]['home']=str(1-int(rows[0]['home']))
        with self.assertRaises(AnalysisError):self.engine(rows=rows)

    def test_wrong_opponent(self):
        rows=copy.deepcopy(self.rows);rows[0]['opponentTeamId']='123'
        with self.assertRaises(AnalysisError):self.engine(rows=rows)

    def test_negative_nan_fractional_stats_rejected(self):
        for bad in ['NaN','-1','2.5']:
            with self.subTest(bad=bad):
                rows=copy.deepcopy(self.rows);rows[0]['assists']=bad
                with self.assertRaises(AnalysisError):self.engine(rows=rows)

    def test_scoring_arithmetic(self):
        rows=copy.deepcopy(self.rows);rows[0]['fieldGoalsMade']=str(int(rows[0]['fieldGoalsMade'])+1)
        with self.assertRaises(AnalysisError):self.engine(rows=rows)

    def test_incomplete_manifest_rejected(self):
        m=copy.deepcopy(self.manifest);m['games']=m['games'][:-1]
        with self.assertRaises(AnalysisError):self.engine(manifest=m)

    def test_source_hash_tampering(self):
        m=copy.deepcopy(self.manifest);m['sources']['games']['sha256']='0'*64
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'m.json';p.write_text(json.dumps(m))
            with self.assertRaises(AnalysisError):SeriesAnalysis.load(p)

    def test_player_data_unavailable_not_dnp(self):
        r=self.engine().summarize()['player_summary']
        self.assertEqual(r['status'],'unavailable');self.assertIsNone(r['averages'])
        self.assertEqual(len(r['missing_game_ids']),6)

    def test_single_game_and_overtime_count_once(self):
        r=self.engine().summarize(['0042500173'])
        self.assertEqual(r['analyzed_games'],1)
        self.assertEqual(sorted(t['totals']['points'] for t in r['teams']),[108,112])
        self.assertIsNone(r['series_winner'])

    def test_score_disagreement(self):
        rows=copy.deepcopy(self.rows);rows[0]['teamScore']='79'
        with self.assertRaises(AnalysisError):self.engine(rows=rows)

    def test_illegal_rebound_sum(self):
        rows=copy.deepcopy(self.rows);rows[0]['reboundsTotal']='999'
        with self.assertRaises(AnalysisError):self.engine(rows=rows)

    def test_cli_unknown_game_no_output(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'bad.json'
            result=subprocess.run([sys.executable,'-m','v2.day4.run_analysis','--games','unknown','--output',str(out)],cwd=ROOT,capture_output=True)
            self.assertEqual(result.returncode,2);self.assertFalse(out.exists())

    def test_cli_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'old.json';out.write_text('preserve me')
            result=subprocess.run([sys.executable,'-m','v2.day4.run_analysis','--output',str(out)],cwd=ROOT,capture_output=True)
            self.assertEqual(result.returncode,2);self.assertEqual(out.read_text(),'preserve me')


if __name__=='__main__':unittest.main()
