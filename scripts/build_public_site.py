"""Build the existing native product renderer; no local backend or models."""

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    for name in (
        'index.html', 'product.css', 'editorial.css', 'final.css',
        'broadcast.css', 'app.js', 'editorial.js', 'broadcast.js',
    ):
        source = ROOT / 'v2/frontend/static' / name
        text = source.read_text(encoding='utf-8')
        if name == 'index.html':
            text = text.replace(
                '<script src="app.js"',
                '<script src="static-data.js" defer></script>\n    <script src="app.js"',
            )
        if name == 'app.js':
            start = text.index('const LOCAL_LOGO_TEAMS = new Set(')
            end = text.index('\n\nconst PLAYER_DISPLAY_NAMES', start)
            text = text[:start] + 'const LOCAL_LOGO_TEAMS = new Set();' + text[end:]
            text = text.replace(
                'verdict.append(refresh);',
                'if (!window.COURTVISION_PUBLIC) verdict.append(refresh);',
            )
        if name == 'final.css':
            text += '\n/* Public read-only release, no internal diagnostics. */\n.technical-details{display:none}\n'
        (out / name).write_text(text, encoding='utf-8')
    shutil.copy2(ROOT / 'web/static-data.js', out / 'static-data.js')
    shutil.copytree(ROOT / 'v2/frontend/static/fonts', out / 'fonts', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'public_data', out / 'public_data', dirs_exist_ok=True)
    (out / '.nojekyll').touch()
    print(json.dumps({
        'built': str(out),
        'games': len(json.loads(
            (out / 'public_data/index.json').read_text(encoding='utf-8')
        )['games']),
    }))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output', default=str(ROOT / '_site'))
    a = p.parse_args()
    build(a.output)
