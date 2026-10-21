import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path

from scripts.sync_public_schedule import build_schedule,sync

ROOT=Path(__file__).resolve().parents[2]


def event(eid='1',home='19',away='1',tip='2026-10-21T23:00:00Z'):
    return {'id':eid,'date':tip,'season':{'year':2027,'type':2},'competitions':[{
        'competitors':[{'homeAway':s,'team':{'id':tid}}for s,tid in [('home',home),('away',away)]],
        'status':{'type':{'state':'pre','name':'STATUS_SCHEDULED','completed':False}}}]}


class PublicSchedule(unittest.TestCase):
    def setUp(self):
        self.teams=json.loads((ROOT/'config/nba_teams.json').read_text(encoding='utf-8'))
        self.selection={'slates':{'2026-10-22':[{'game_id':'espn:1'}]}}
        self.at=datetime(2026,10,21,14,tzinfo=timezone.utc)

    def build(self,events):
        return build_schedule([{'url':'https://example.test/scoreboard','payload':{'events':events}}],self.teams,self.selection,self.at)

    def test_all_games_display_but_only_explicit_pick_is_marked(self):
        events=[event(),event('2','27','15')];before=copy.deepcopy(events)
        result=self.build(events)
        self.assertEqual(len(result['games']),2)
        self.assertEqual([g['id']for g in result['games']if g['report_selected']],['espn:1'])
        self.assertTrue(all(not g['registered_game']and'prediction'not in g for g in result['games']))
        self.assertEqual(result['generation_calls'],0)
        self.assertEqual(events,before)

    def test_scores_are_facts_only_and_missing_is_not_zero(self):
        fixture=event()
        for row in fixture['competitions'][0]['competitors']:row['score']='0'
        self.assertNotIn('score',self.build([fixture])['games'][0]['home'])
        fixture['competitions'][0]['status']['type']={'state':'post','name':'STATUS_FINAL','completed':True}
        fixture['competitions'][0]['competitors'][0]['score']='106'
        fixture['competitions'][0]['competitors'][1]['score']='100'
        game=self.build([fixture])['games'][0]
        self.assertEqual((game['home']['score'],game['away']['score']),(106,100))
        fixture['competitions'][0]['competitors'][1]['score']=None
        self.assertNotIn('score',self.build([fixture])['games'][0]['away'])

    def test_beijing_boundary_and_rescheduled_date(self):
        result=self.build([event(tip='2026-10-21T16:00:00Z')])
        self.assertEqual(result['games'][0]['beijing_date'],'2026-10-22')
        self.at=datetime(2026,10,21,16,1,tzinfo=timezone.utc)
        result=self.build([event(tip='2026-10-21T15:59:00Z'),event('2',tip='2026-10-22T23:00:00Z')])
        self.assertEqual([g['id']for g in result['games']],['espn:2'])
        self.assertEqual(result['games'][0]['beijing_date'],'2026-10-23')

    def test_unknown_or_conflicting_identity_and_naive_time_fail_closed(self):
        for rows in ([event(home='999')],[event(),event(home='27')],[event(tip='2026-10-21T23:00:00')]):
            with self.assertRaises((KeyError,ValueError)):self.build(rows)

    def test_failed_refresh_preserves_previous_facts(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);(root/'public_data').mkdir();path=root/'public_data/schedule.json';path.write_text('old')
            def fail(day):raise OSError('source unavailable')
            with self.assertRaises(OSError):sync(root,self.at,fail)
            self.assertEqual(path.read_text(),'old')

    def test_sync_never_rewrites_published_reports(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);(root/'config').mkdir();(root/'public_data/games').mkdir(parents=True)
            (root/'config/nba_teams.json').write_text(json.dumps(self.teams))
            (root/'config/daily_picks.json').write_text(json.dumps(self.selection))
            protected=root/'public_data/games/published.json';protected.write_text('immutable report')
            index=root/'public_data/index.json';index.write_text('published index')
            sync(root,self.at,lambda d:{'url':'https://example.test/'+str(d),'payload':{'events':[event()]}})
            self.assertEqual(protected.read_text(),'immutable report');self.assertEqual(index.read_text(),'published index')

    def test_static_adapter_rollover_and_facts_only_detail(self):
        subprocess.run([shutil.which('node'),str(Path(__file__).with_name('public_schedule.cjs')),
            str(ROOT/'web/static-data.js')],check=True,cwd=ROOT)
