from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    manifest_path = PROJECT_ROOT / "artifacts" / "reproducibility_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            "reproducibility manifest is missing; run scripts/run_baseline.py first"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mismatches: list[str] = []

    for item in manifest["raw_files"].values():
        path = PROJECT_ROOT / item["path"]
        if not path.exists() or path.stat().st_size != item["bytes"]:
            mismatches.append(f"raw size: {path}")
        elif sha256(path) != item["sha256"]:
            mismatches.append(f"raw sha256: {path}")

    for relative_path, expected in manifest["rebuildable_artifacts"].items():
        path = PROJECT_ROOT / relative_path
        if not path.exists() or path.stat().st_size != expected["bytes"]:
            mismatches.append(f"artifact size: {relative_path}")
        elif sha256(path) != expected["sha256"]:
            mismatches.append(f"artifact sha256: {relative_path}")

    for package, expected in manifest["packages"].items():
        actual = importlib.metadata.version(package)
        if actual != expected:
            mismatches.append(f"package {package}: expected={expected}, actual={actual}")

    result = {"status": "passed" if not mismatches else "failed", "mismatches": mismatches}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
