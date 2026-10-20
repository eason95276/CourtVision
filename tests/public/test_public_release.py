import copy
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from publish_game import clean, sha, public_selection, lineup_reference_names, home_record


class PublicRelease(unittest.TestCase):

    def setUp(self):
        self.index = json.loads((ROOT / 'public_data/index.json').read_text(encoding='utf-8'))
        self.rows = [json.loads((ROOT / 'public_data' / r['file']).read_text(encoding='utf-8')) for r in self.index['games']]

    def test_hashes(self):
        for index, row in zip(self.index['games'], self.rows):
            payload = copy.deepcopy(row)
            checksum = payload.pop('public_sha256')
            self.assertEqual(checksum, sha(payload))
            self.assertEqual(checksum, index['public_sha256'])

    def test_unique_identities(self):
        ids = [r['game']['id'] for r in self.rows]
        self.assertEqual(len(ids), len(set(ids)))
        for row in self.rows:
            self.assertNotEqual(row['game']['home']['id'], row['game']['away']['id'])

    def test_version_binding(self):
        for row in self.rows:
            version = row['prediction']['version']
            if not version:
                continue
            self.assertEqual(row['game']['id'], version['game_id'])
            self.assertEqual(row['publication']['source_prediction_version_id'], version['version_id'])
            self.assertEqual(row['publication']['source_prediction_payload_sha256'], version['payload_sha256'])
            self.assertLess(datetime.fromisoformat(version['input_cutoff_at']), datetime.fromisoformat(row['game']['tipoff']))
            self.assertIsInstance(version['judgment']['analysis_article'], str)

    def test_facts_only_not_fabricated(self):
        # Exercise the missing-version path even when every published game has a report.
        from publish_game import game_projection
        game = game_projection(self.rows[0]['game'])
        game.pop('prediction', None)
        selection = public_selection({'status': 'unavailable', 'version_id': None, 'version': None, 'state_revision': 0}, game)
        self.assertIsNone(selection['version'])
        rows = [r for r in self.rows if r['prediction']['version'] is None] + [{'game': game, 'prediction': selection}]
        for row in rows:
            self.assertNotIn('prediction', row['game'])

    def test_postgame_identity(self):
        for row in self.rows:
            if row['postgame']['status'] == 'available':
                self.assertEqual(row['postgame']['postgame']['game_id'], row['game']['id'])

    def test_private_fields_removed(self):
        self.assertEqual(clean({'password': 'secret', 'raw_response': 'private', 'home': {'path': 'private', 'name': 'team'}}), {'home': {'name': 'team'}})
        self.assertNotIn('localhost', json.dumps(self.rows))
        self.assertNotRegex(json.dumps(self.rows), '[A-Z]:[\\\\/]+Users')

    def test_static_adapter_blocks_writes(self):
        js = (ROOT / 'web/static-data.js').read_text(encoding='utf-8')
        self.assertIn("method !== 'GET' && method !== 'HEAD'", js)
        self.assertIn('405', js)

    def test_public_https_preserved(self):
        url = 'https://a.espncdn.com/i/teamlogos/nba/500/gs.png'
        self.assertEqual(clean(url), url)
        private_path = 'C:' + '/Users/' + 'private/data.json'
        self.assertNotEqual(clean(private_path), private_path)

    def test_no_embedded_generation_credentials(self):
        for p in (ROOT / 'web').glob('*'):
            self.assertNotIn('DEEPSEEK_API_KEY', p.read_text(encoding='utf-8'))

    def test_local_export_only_gets(self):
        code = (ROOT / 'publish_game.py').read_text(encoding='utf-8')
        self.assertNotIn('method="POST"', code)
        self.assertNotIn('import openai', code)

    def test_lineup_names_survive_without_optional_display_names(self):
        source = {'input_snapshot': {'roster_context': {'teams': {
            'away': {'current_team_roster': {'status': 'available', 'players': [
                {'name': 'Payton Pritchard'}, {'name': 'Jayson Tatum'}, {'name': 'Not In Quote'}]}},
            'home': {'official_team_roster': {'status': 'unavailable', 'players': [
                {'name': 'Unverified Player'}]}}}},
            'historical_players': [{'name': 'Historical Player'}]}}
        quote = 'Projected: Payton Pritchard, Jayson Tatum. Unverified Player. Historical Player.'
        self.assertEqual(lineup_reference_names(source, quote), ['Jayson Tatum', 'Payton Pritchard'])
        self.assertNotIn('Projected', ' / '.join(lineup_reference_names(source, quote)))

    def test_title_overlay_keeps_frozen_prediction_and_home_title_consistent(self):
        for row in self.rows:
            selection = row['prediction']
            overlay = (selection.get('display_projection') or {}).get('portfolio_editorial')
            if not overlay or not str(overlay.get('artifact_id', '')).startswith('poem-title-'):
                continue
            frozen = copy.deepcopy(selection['version'])
            self.assertEqual(overlay['base_prediction_version_id'], frozen['version_id'])
            self.assertEqual(overlay['base_prediction_payload_sha256'], frozen['payload_sha256'])
            editorial = overlay['editorial']
            self.assertRegex(editorial['pregame_story']['title'], r'^[\u4e00-\u9fff]{7}\n[\u4e00-\u9fff]{7}$')
            self.assertEqual(editorial['analysis_article'], frozen['judgment']['analysis_article'])
            self.assertEqual(editorial['player_analysis'], frozen['judgment'].get('player_analysis', []))
            summary = home_record(row)
            self.assertEqual(summary['prediction']['display_projection']['portfolio_editorial']['editorial']['pregame_story'], editorial['pregame_story'])
            indexed = next(item['home_record'] for item in self.index['games'] if item['game_id'] == row['game']['id'])
            self.assertEqual(indexed['prediction']['display_projection']['portfolio_editorial']['editorial']['pregame_story'], editorial['pregame_story'])
            self.assertEqual(summary['prediction']['version']['judgment']['final_prediction'], frozen['judgment']['final_prediction'])
            self.assertEqual(selection['version'], frozen)

    def test_old_version_cannot_receive_another_versions_title(self):
        row = copy.deepcopy(next(r for r in self.rows if r['prediction']['version']))
        row['prediction'].setdefault('display_projection', {})['portfolio_editorial'] = {
            'base_prediction_version_id': 'another-version',
            'base_prediction_payload_sha256': row['prediction']['version']['payload_sha256'],
            'editorial': {'pregame_story': {'title': 'Wrong title'}, 'analysis_article': 'Wrong article'}}
        self.assertNotIn('display_projection', home_record(row)['prediction'])

    def test_identity_conflict_rejected(self):
        row = copy.deepcopy(next((r for r in self.rows if r['prediction']['version'])))
        row['game']['home']['id'] += 1
        with self.assertRaisesRegex(ValueError, 'identity'):
            public_selection(row['prediction'], row['game'])


if __name__ == '__main__':
    unittest.main()
