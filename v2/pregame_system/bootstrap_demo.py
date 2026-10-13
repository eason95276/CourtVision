"""Restore the offline, versioned demo bundle. No training or network calls."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

from .runtime_assets import BASE, BUNDLE_VERSION, DEMO_ROOT, checked_file

BUNDLE = BASE / 'demo_assets' / 'runtime-v1.zip'
MANIFEST = BASE / 'demo_assets' / 'manifest.json'


def bootstrap(destination=DEMO_ROOT, bundle=BUNDLE, manifest_path=MANIFEST):
    destination = Path(destination)
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    if manifest.get('version') != BUNDLE_VERSION:
        raise ValueError('demo_version_mismatch')
    raw = Path(bundle).read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest['bundle_sha256']:
        raise ValueError('demo_bundle_hash_mismatch')
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(manifest['files']):
            raise ValueError('demo_bundle_file_inventory_mismatch')
        payload = {}
        for name in names:
            checked_file(destination, name)
            data = archive.read(name)
            info = manifest['files'][name]
            if len(data) != info['size'] or hashlib.sha256(data).hexdigest() != info['sha256']:
                raise ValueError('demo_file_hash_mismatch:' + name)
            payload[name] = data
    payload['manifest.json'] = Path(manifest_path).read_bytes()
    # Validate the complete package before publishing individual atomic files.
    for name, data in payload.items():
        path = checked_file(destination, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() == data:
            continue
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, suffix='.tmp') as handle:
            handle.write(data)
            temporary = Path(handle.name)
        temporary.replace(path)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=DEMO_ROOT)
    args = parser.parse_args()
    print(json.dumps({'version': BUNDLE_VERSION, 'restored': str(bootstrap(args.destination)),
                      'next': 'python -m v2.pregame_system.demo'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
