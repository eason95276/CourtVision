"""Offline production preflight: python -m v2.pregame_system.runtime_check."""
import json
from .prediction import Predictor
from .runtime_assets import RuntimeAssetError


def main():
    try:
        predictor = Predictor(require_production=True)
    except RuntimeAssetError as exc:
        print(f"Production runtime: NOT READY\n{exc}")
        return 1
    identity = predictor.runtime_asset_identity
    print("Production runtime: READY")
    print(f"Model: {predictor.production} ({identity['model_version']})")
    for path in predictor.source_paths:
        print(f"{path.name}: {path} ({path.stat().st_size} bytes)")
    print(f"Feature schema: {identity['feature_schema_hash']}")
    print(f"Teams covered: {len(identity['teams_covered'])}")
    from .player_knowledge import DEFAULT_DATABASE
    print(f"Player Knowledge: {'present' if DEFAULT_DATABASE.is_file() else 'unavailable'} ({DEFAULT_DATABASE})")
    print(json.dumps(identity, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
