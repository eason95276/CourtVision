from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class HomeCollections(unittest.TestCase):
    def test_animated_tab_handler_refreshes_heading_and_cards_together(self):
        code = (ROOT / 'v2/frontend/static/editorial.js').read_text(encoding='utf-8')
        handler = code.split('const originalDateFilters = setupDateFilters;', 1)[1].split('const originalRenderDetail', 1)[0]
        self.assertIn('renderHome();', handler)
        self.assertNotIn('renderGames();', handler)

    def test_real_renderer_collection_membership(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for renderer regression')
        subprocess.run([node, str(Path(__file__).with_name('home_collections.cjs')),
                        str(ROOT / 'v2/frontend/static/app.js')], check=True, cwd=ROOT)

    def test_public_build_keeps_collection_filters(self):
        from scripts.build_public_site import build
        with tempfile.TemporaryDirectory() as folder:
            build(folder)
            code = (Path(folder) / 'app.js').read_text(encoding='utf-8')
            self.assertIn('gameInHomeCollection(game, state.selectedDate)', code)
            self.assertNotIn('state.data.public_archive || chinaDateKey(game.tipoff)', code)
            self.assertIn('homeCollectionOptions()', code)


if __name__ == '__main__':
    unittest.main()
