"""Build/resume the local 2025-26 regular-season player knowledge store."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from v2.pregame_system.player_knowledge import (
    DEFAULT_DATABASE,
    ENDPOINT,
    PROVIDER,
    SOURCE_URL,
    PlayerKnowledgeStore,
    _sha256_file,
    _sha256_json,
    read_games_csv,
)


def _access_log(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row.get("game_id")): row
        for row in payload.get("requests", [])
        if row.get("dataset") == "boxscore_traditional_v3" and row.get("game_id")
    }


def _source_manifest(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        Path(row.get("path", "")).name: row
        for row in payload.get("raw_files", [])
        if row.get("path")
    }


def _fetch_boxscore(game_id: str, timeout_seconds: int) -> dict[str, Any]:
    """Import the already-pinned project client only when collection is requested."""
    try:
        from nba_api.stats.endpoints import boxscoretraditionalv3
    except ImportError as exc:
        raise RuntimeError(
            "nba_api_missing: install the repository-pinned v2/requirements.txt in the collection environment"
        ) from exc
    return boxscoretraditionalv3.BoxScoreTraditionalV3(
        game_id=game_id,
        timeout=timeout_seconds,
    ).get_dict()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games-csv", type=Path, required=True,
                        help="Explicit schedule/result index used only for game ID, date and teams")
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--raw-dir", type=Path,
                        help="Import existing BoxScoreTraditionalV3 JSON files from this folder")
    parser.add_argument("--access-log", type=Path,
                        help="Existing per-game request times for raw files")
    parser.add_argument("--source-manifest", type=Path,
                        help="Existing raw-file SHA/size manifest used to verify imported samples")
    parser.add_argument("--collect", action="store_true",
                        help="Request only missing game boxes from NBA Stats; off by default")
    parser.add_argument("--refresh-existing", action="store_true",
                        help="Fetch/import new response versions even for games with a prior successful receipt")
    parser.add_argument("--max-games", type=int,
                        help="Bound this run to at most N raw imports or network requests")
    parser.add_argument("--delay-seconds", type=float, default=1.5)
    parser.add_argument("--timeout-seconds", type=int, default=20)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    games = read_games_csv(args.games_csv, args.season)
    store = PlayerKnowledgeStore(args.database)
    store.initialize(games, args.season)
    access = _access_log(args.access_log)
    manifest = _source_manifest(args.source_manifest)
    completed = store.successful_game_ids(args.season, provider=PROVIDER)
    processed_this_run = 0

    for game in games:
        if game["game_id"] in completed and not args.refresh_existing:
            continue
        raw_path = (args.raw_dir / f"{game['game_id']}_boxscore_traditional_v3.json") if args.raw_dir else None
        use_raw = bool(raw_path and raw_path.is_file())
        if not use_raw and not args.collect:
            continue
        if args.max_games is not None and processed_this_run >= args.max_games:
            break

        try:
            if use_raw:
                assert raw_path is not None
                manifest_row = manifest.get(raw_path.name)
                digest = _sha256_file(raw_path)
                byte_count = raw_path.stat().st_size
                if manifest_row is not None and (
                    manifest_row.get("sha256") != digest
                    or int(manifest_row.get("bytes") or -1) != byte_count
                ):
                    raise ValueError("raw_boxscore_manifest_hash_or_size_mismatch")
                payload = json.loads(raw_path.read_text(encoding="utf-8"))
                receipt = access.get(str(game["game_id"])) or {}
                fetched_at = receipt.get("fetched_at_utc")
                source_path = raw_path
                source_status = receipt.get("status")
                if source_status not in (None, "success", "cached_no_request"):
                    store.record_failure(
                        game["game_id"], error_type="cached_source_not_successful",
                        error_message=f"source log status: {source_status}",
                        collection_time_utc=datetime.now(timezone.utc),
                    )
                    print(json.dumps({"game_id": game["game_id"], "status": "raw_source_rejected"}), flush=True)
                    processed_this_run += 1
                    continue
            else:
                started = time.monotonic()
                payload = _fetch_boxscore(str(game["game_id"]), args.timeout_seconds)
                fetched_at = datetime.now(timezone.utc).isoformat()
                digest = _sha256_json(payload)
                byte_count = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                source_path = None
                source_status = "success"
                elapsed = round(time.monotonic() - started, 3)

            result = store.ingest_boxscore(
                game,
                payload,
                collection_time_utc=fetched_at,
                source_path=source_path,
                response_sha256=digest,
                response_bytes=byte_count,
                provider=PROVIDER,
                endpoint=ENDPOINT,
                source_url=SOURCE_URL,
                client_version="nba_api 1.10.2" if not use_raw else "source_manifest_recorded_nba_api_1.10.2",
            )
            print(json.dumps({"game_id": game["game_id"], **result,
                              "source_status": source_status,
                              "elapsed_seconds": elapsed if not use_raw else None}, ensure_ascii=False), flush=True)
        except Exception as exc:
            store.record_failure(
                game["game_id"],
                error_type=type(exc).__name__,
                error_message=str(exc),
                collection_time_utc=datetime.now(timezone.utc),
            )
            print(json.dumps({"game_id": game["game_id"], "status": "failed",
                              "error_type": type(exc).__name__, "error": str(exc)[:500]}, ensure_ascii=False), flush=True)
            if args.collect:
                print("stopping_on_first_source_error; resume only after reviewing the failure", flush=True)
                break
        processed_this_run += 1
        if args.collect and args.delay_seconds > 0:
            time.sleep(args.delay_seconds)

    print(json.dumps(store.coverage(args.season), ensure_ascii=False), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_games is not None and args.max_games < 1:
        raise SystemExit("--max-games must be positive")
    if args.delay_seconds < 0 or args.timeout_seconds < 1:
        raise SystemExit("invalid collection timing settings")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
