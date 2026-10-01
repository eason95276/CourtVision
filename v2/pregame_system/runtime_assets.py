"""Small runtime contract shared by bootstrap and prediction startup."""
from pathlib import Path
import hashlib
import json

BASE = Path(__file__).resolve().parent
DEMO_ROOT = BASE / 'runtime_demo'
BUNDLE_VERSION = 'courtvision-demo-v1'
SETUP_COMMAND = 'python -m v2.pregame_system.bootstrap_demo'


class RuntimeAssetError(ValueError):
    pass


def asset_error(detail):
    return RuntimeAssetError(f'CourtVision runtime asset missing or invalid: {detail}. Run: {SETUP_COMMAND}')


def checked_file(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('demo_manifest_unsafe_path')
    return path


def verify_runtime_file(root, relative):
    root = Path(root)
    try:
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        if manifest['version'] != BUNDLE_VERSION:
            raise ValueError('demo_version_mismatch')
        entry = manifest['files'][relative]
        path = checked_file(root, relative)
        raw = path.read_bytes()
        if len(raw) != entry['size'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('demo_hash_mismatch')
        return path
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise asset_error(relative) from exc
