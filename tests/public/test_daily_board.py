import copy
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.update_daily_board import render_board, replace_board, START, END


class DailyBoard(unittest.TestCase):
    def setUp(self):
        self.selection = {'slates': {'2026-10-21': [
            {'game_id': 'espn:1', 'update_status': 'awaiting_review'}]}}
        self.records = {'espn:1': {'game': {'id': 'espn:1', 'tipoff': '2026-10-20T23:00:00+00:00',
            'away': {'name_zh': '客队'}, 'home': {'name_zh': '主队'}},
            'prediction': {'version': {'input_cutoff_at': '2026-10-14T18:00:00+00:00'}}}}
        self.now = datetime(2026, 10, 20, 6, tzinfo=timezone.utc)

    def test_selected_slate_and_cutoff_are_separate(self):
        board = render_board(self.selection, self.records, self.now)
        self.assertIn('2026-10-21', board)
        self.assertIn('07:00', board)
        self.assertIn('10-15 02:00', board)
        self.assertIn('单场试稿待人工确认', board)
        self.assertIn('#game/espn%3A1', board)

    def test_beijing_midnight_does_not_reuse_yesterdays_slate(self):
        before = datetime(2026, 10, 20, 15, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 20, 16, tzinfo=timezone.utc)
        self.assertIn('客队 @ 主队', render_board(self.selection, self.records, before))
        board = render_board(self.selection, self.records, after)
        self.assertIn('2026-10-22', board)
        self.assertNotIn('客队 @ 主队', board)
        self.assertIn('不沿用昨日名单', board)

    def test_postponed_game_is_explicitly_excluded(self):
        self.records['espn:1']['game']['tipoff'] = '2026-10-21T23:00:00+00:00'
        board = render_board(self.selection, self.records, self.now)
        self.assertIn('不计入明日名单', board)
        self.assertNotIn('| 07:00 |', board)

    def test_unpublished_game_stays_facts_only(self):
        self.records['espn:1']['prediction']['version'] = None
        self.assertIn('事实页，尚无正式报告', render_board(self.selection, self.records, self.now))

    def test_other_records_are_not_selected(self):
        self.records['espn:2'] = copy.deepcopy(self.records['espn:1'])
        self.records['espn:2']['game']['away']['name_zh'] = '未选择的球队'
        self.assertNotIn('未选择的球队', render_board(self.selection, self.records, self.now))

    def test_missing_selected_record_fails_without_fabrication(self):
        with self.assertRaisesRegex(ValueError, 'not_in_public_records'):
            render_board(self.selection, {}, self.now)

    def test_manual_readme_edits_outside_markers_survive(self):
        old = f'手工简介\n{START}\n旧板块\n{END}\n手工结尾'
        revised = replace_board(old, '新板块')
        self.assertEqual(revised, f'手工简介\n{START}\n新板块\n{END}\n手工结尾')
        with self.assertRaises(ValueError):
            replace_board('没有标记的手工正文', '新板块')


if __name__ == '__main__':
    unittest.main()
