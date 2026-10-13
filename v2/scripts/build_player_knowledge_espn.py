"""Import validated 2025-26 ESPN player boxscores with resumable, sequential requests."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from v2.pregame_system.espn_boxscore_normalizer import (
    ESPN_PROVIDER,
    ESPN_SCOREBOARD_URL,
    ESPN_SUMMARY_ENDPOINT,
    ESPN_SUMMARY_URL,
    build_nba_team_nickname_map,
    map_scoreboard_events_to_nba_games,
    normalize_espn_summary_payload,
)
from v2.pregame_system.espn_player_source import ESPNSourceError, fetch_espn_scoreboard, fetch_espn_summary
from v2.pregame_system.player_knowledge import DATA_VERSION, DEFAULT_DATABASE, PlayerKnowledgeStore, read_games_csv


PROBE_MANIFEST = PROJECT_ROOT / "v2" / "data" / "player_knowledge_diagnostics" / "espn_boxscore_probe_manifest.json"
DEFAULT_EVENT_INDEX = PROJECT_ROOT / "v2" / "data" / "player_knowledge_diagnostics" / "espn_event_index_2025-26.json"
DEFAULT_RAW_DIR = PROJECT_ROOT / "v2" / "data" / "raw" / "player_knowledge_espn"
DEFAULT_DIAGNOSTICS_DIR = PROJECT_ROOT / "v2" / "data" / "player_knowledge_diagnostics" / "espn_failures"
ESPN_DATA_VERSION = "player-knowledge-v1-espn-summary-1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games-csv", type=Path, required=True,
                        help="Explicit schedule index for NBA game IDs, dates, and NBA team IDs")
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--event-index", type=Path, default=DEFAULT_EVENT_INDEX)
    parser.add_argument("--probe-manifest", type=Path, default=PROBE_MANIFEST)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--diagnostics-dir", type=Path, default=DEFAULT_DIAGNOSTICS_DIR)
    parser.add_argument("--import-probes", action="store_true",
                        help="Import the already validated three local ESPN probe responses")
    parser.add_argument("--collect", action="store_true",
                        help="Fetch missing game summaries after the three-probe validation gate passes")
    parser.add_argument("--max-games", type=int, help="Limit this run to at most N new game summary requests")
    parser.add_argument("--delay-seconds", type=float, default=1.5)
    parser.add_argument("--timeout-seconds", type=int, default=20)
    return parser.parse_args(argv)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_artifact_path(value: str) -> Path:
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _probe_manifest_valid(manifest_path: Path) -> tuple[list[dict[str, Any]], str | None]:
    if not manifest_path.is_file():
        return [], "three_game_espn_probe_manifest_missing"
    manifest = _read_json(manifest_path)
    probes = manifest.get("probes") or []
    by_game: dict[str, dict[str, Any]] = {}
    for probe in probes:
        game_id = str(probe.get("nba_game_id") or "")
        if not game_id:
            continue
        mismatches = int(probe.get("mismatch_count", -1))
        if probe.get("http_status") != 200 or mismatches != 0:
            continue
        if int(probe.get("player_rows") or probe.get("espn_rows") or 0) <= 0:
            continue
        raw_value = probe.get("espn_raw_path") or probe.get("raw_path")
        if not raw_value:
            continue
        raw_path = _resolve_artifact_path(str(raw_value))
        if not raw_path.is_file():
            continue
        digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        if digest != probe.get("sha256"):
            continue
        by_game[game_id] = probe
    valid = sorted(by_game.values(), key=lambda row: str(row.get("nba_game_id")))
    if len(valid) < 3:
        return valid, f"validated_espn_probe_count_below_three:{len(valid)}"
    return valid, None


def import_validated_probes(
    store: PlayerKnowledgeStore,
    games: list[dict[str, Any]],
    manifest_path: Path,
) -> list[dict[str, Any]]:
    probes, error = _probe_manifest_valid(manifest_path)
    if error:
        raise ValueError(error)
    game_by_id = {str(game["game_id"]): game for game in games}
    results = []
    for probe in probes:
        game_id = str(probe["nba_game_id"])
        game = game_by_id.get(game_id)
        if game is None:
            raise ValueError(f"probe_game_not_in_schedule:{game_id}")
        raw_path = _resolve_artifact_path(str(probe.get("espn_raw_path") or probe.get("raw_path")))
        raw_bytes = raw_path.read_bytes()
        payload = json.loads(raw_bytes.decode("utf-8"))
        event_id = str(probe["espn_event_id"])
        normalized = normalize_espn_summary_payload(game, payload, expected_event_id=event_id)
        captured_at = probe.get("captured_at") or probe.get("captured_at_upper_bound")
        if not captured_at:
            raise ValueError(f"probe_capture_time_missing:{game_id}")
        result = store.ingest_boxscore(
            game,
            payload,
            normalized_rows=normalized,
            collection_time_utc=captured_at,
            source_path=raw_path,
            response_sha256=str(probe["sha256"]),
            response_bytes=len(raw_bytes),
            provider=ESPN_PROVIDER,
            endpoint=ESPN_SUMMARY_ENDPOINT,
            source_url=f"{ESPN_SUMMARY_URL}?event={event_id}",
            client_version="stdlib urllib; ESPN summary adapter v1",
            request_parameters={
                "game_id": game_id,
                "espn_event_id": event_id,
                "validation_probe": True,
                "capture_time_boundary": "response complete before local raw file write",
                "http_status_code": probe.get("http_status"),
                "content_type": probe.get("content_type"),
                "source_http_date": probe.get("source_date"),
                "raw_status": "stored_uncompressed_probe_json",
                "normalized_status": "success",
            },
            data_version=ESPN_DATA_VERSION,
        )
        results.append({"game_id": game_id, **result})
    store.set_active_provider(ESPN_PROVIDER)
    return results


def _load_event_index(path: Path, season: str) -> dict[str, Any]:
    if path.is_file():
        value = _read_json(path)
        if value.get("season") == season and isinstance(value.get("dates"), dict):
            return value
    return {
        "schema_version": "1.0",
        "season": season,
        "provider": ESPN_PROVIDER,
        "schedule_source": "caller-provided Games.csv",
        "dates": {},
    }


def _save_source_failure(
    store: PlayerKnowledgeStore,
    game_id: str,
    report: dict[str, Any],
    diagnostics_dir: Path,
    *,
    request_parameters: dict[str, Any] | None = None,
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = diagnostics_dir / f"{game_id}_{stamp}.json"
    _write_json(path, report)
    store.record_failure(
        game_id,
        error_type=str(report.get("classification") or "espn_source_failure"),
        error_message=json.dumps({
            "classification": report.get("classification"),
            "http_status_code": report.get("http_status_code"),
            "content_type": report.get("content_type"),
            "diagnostic_file": str(path),
        }, ensure_ascii=False, separators=(",", ":")),
        collection_time_utc=report.get("request_finished_at") or datetime.now(timezone.utc),
        request_parameters={**(request_parameters or {}), "diagnostic_file": str(path)},
        source_url=str(report.get("request_url") or ESPN_SUMMARY_URL),
        provider=ESPN_PROVIDER,
        endpoint=ESPN_SUMMARY_ENDPOINT,
        client_version="stdlib urllib; ESPN summary adapter v1",
        data_version=ESPN_DATA_VERSION,
    )
    return path


def _date_event_mapping(
    game_date: str,
    games: list[dict[str, Any]],
    nickname_to_nba_id: dict[str, int],
    index: dict[str, Any],
    store: PlayerKnowledgeStore,
    args: argparse.Namespace,
) -> dict[str, dict[str, Any]]:
    cached = (index.get("dates") or {}).get(game_date)
    if cached and cached.get("status") == "success":
        return cached.get("games") or {}
    try:
        response = fetch_espn_scoreboard(game_date, timeout_seconds=args.timeout_seconds)
        mapped = map_scoreboard_events_to_nba_games(
            game_date, games, response.payload, nickname_to_nba_id
        )
        entry = {
            "status": "success",
            "source_url": response.report["request_url"],
            "http_status_code": response.report["http_status_code"],
            "content_type": response.report["content_type"],
            "compressed_response_bytes": response.report["compressed_response_bytes"],
            "response_sha256": response.report["response_body_sha256"],
            "source_published_at": response.report["response_headers"].get("Date"),
            "captured_at": response.report["request_finished_at"],
            "games": mapped,
        }
        index["dates"][game_date] = entry
        index["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write_json(args.event_index, index)
        time.sleep(args.delay_seconds)
        return mapped
    except ESPNSourceError as exc:
        report = dict(exc.report)
        report["provider"] = ESPN_PROVIDER
        report["request_kind"] = "scoreboard_schedule_mapping"
        report["game_date"] = game_date
        index["dates"][game_date] = {"status": "failed", "failure_classification": report.get("classification")}
        _write_json(args.event_index, index)
        day_games = [game for game in games if str(game["game_date"]) == game_date]
        for game in day_games:
            _save_source_failure(store, str(game["game_id"]), report, args.diagnostics_dir,
                                 request_parameters={"game_date": game_date, "nba_game_id": game["game_id"]})
        raise
    except Exception as exc:
        report = {
            "provider": ESPN_PROVIDER,
            "endpoint": "site.api.espn.com/nba/scoreboard",
            "request_kind": "scoreboard_schedule_mapping",
            "game_date": game_date,
            "classification": "schedule_mapping_error",
            "exception_type": type(exc).__name__,
            "exception_message": str(exc)[:800],
            "request_finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        index["dates"][game_date] = {"status": "failed", "failure_classification": report["classification"]}
        _write_json(args.event_index, index)
        day_games = [game for game in games if str(game["game_date"]) == game_date]
        for game in day_games:
            _save_source_failure(store, str(game["game_id"]), report, args.diagnostics_dir,
                                 request_parameters={"game_date": game_date, "nba_game_id": game["game_id"]})
        raise


def _import_one_summary(
    store: PlayerKnowledgeStore,
    game: dict[str, Any],
    mapping: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    game_id = str(game["game_id"])
    event_id = str(mapping["event_id"])
    response = None
    try:
        response = fetch_espn_summary(event_id, timeout_seconds=args.timeout_seconds)
        normalized = normalize_espn_summary_payload(
            game, response.payload, expected_event_id=event_id
        )
        stamp = response.report["request_finished_at"].replace(":", "").replace("-", "")
        compressed = response.report.get("content_encoding") == "gzip"
        extension = ".json.gz" if compressed else ".json"
        raw_path = args.raw_dir / f"{game_id}_{event_id}_{stamp}{extension}"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(response.raw_body)
        result = store.ingest_boxscore(
            game,
            response.payload,
            normalized_rows=normalized,
            collection_time_utc=response.report["request_finished_at"],
            source_path=raw_path,
            response_sha256=response.report["response_body_sha256"],
            response_bytes=response.report["compressed_response_bytes"],
            provider=ESPN_PROVIDER,
            endpoint=ESPN_SUMMARY_ENDPOINT,
            source_url=response.report["request_url"],
            client_version="stdlib urllib; ESPN summary adapter v1",
            request_parameters={
                "game_id": game_id,
                "espn_event_id": event_id,
                "scoreboard_date": game["game_date"],
                "scoreboard_captured_at": (args.event_index_data.get("dates", {}).get(game["game_date"]) or {}).get("captured_at"),
                "summary_content_encoding": response.report.get("content_encoding"),
                "summary_decoded_bytes": response.report.get("decoded_response_bytes"),
                "http_status_code": response.report.get("http_status_code"),
                "content_type": response.report.get("content_type"),
                "source_http_date": (response.report.get("response_headers") or {}).get("Date"),
                "raw_status": "stored_gzip_http_body" if compressed else "stored_identity_http_body",
                "normalized_status": "success",
                "player_rows": len(normalized),
            },
            data_version=ESPN_DATA_VERSION,
        )
        return {"game_id": game_id, "event_id": event_id, **result,
                "compressed_response_bytes": response.report["compressed_response_bytes"],
                "decoded_response_bytes": response.report["decoded_response_bytes"]}
    except ESPNSourceError as exc:
        report = dict(exc.report)
        report["provider"] = ESPN_PROVIDER
        report["nba_game_id"] = game_id
        report["espn_event_id"] = event_id
        report["request_kind"] = "summary_boxscore"
        diagnostic_path = _save_source_failure(
            store, game_id, report, args.diagnostics_dir,
            request_parameters={"game_id": game_id, "espn_event_id": event_id},
        )
        report["diagnostic_file"] = str(diagnostic_path)
        raise
    except Exception as exc:
        report = dict(response.report) if response is not None else {}
        report.update({
            "provider": ESPN_PROVIDER,
            "endpoint": ESPN_SUMMARY_ENDPOINT,
            "request_kind": "summary_normalization_or_ingest",
            "nba_game_id": game_id,
            "espn_event_id": event_id,
            "classification": "espn_summary_identity_or_normalization_error",
            "exception_type": type(exc).__name__,
            "exception_message": str(exc)[:800],
            "request_finished_at": report.get("request_finished_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        if response is not None:
            raw_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            raw_suffix = ".json.gz" if response.report.get("content_encoding") == "gzip" else ".json"
            raw_path = args.raw_dir / "failed_normalization" / f"{game_id}_{event_id}_{raw_stamp}{raw_suffix}"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(response.raw_body)
            report["raw_failure_path"] = str(raw_path)
            report["raw_failure_sha256"] = response.report.get("response_body_sha256")
        _save_source_failure(
            store, game_id, report, args.diagnostics_dir,
            request_parameters={"game_id": game_id, "espn_event_id": event_id},
        )
        raise


def run(args: argparse.Namespace) -> int:
    games = read_games_csv(args.games_csv, args.season)
    store = PlayerKnowledgeStore(args.database)
    store.initialize(games, args.season)

    imported_probes = []
    if args.import_probes or args.collect:
        imported_probes = import_validated_probes(store, games, args.probe_manifest)
        print(json.dumps({"probe_imports": imported_probes}, ensure_ascii=False), flush=True)

    if not args.collect:
        print(json.dumps(store.coverage(args.season, provider=ESPN_PROVIDER), ensure_ascii=False), flush=True)
        return 0

    index = _load_event_index(args.event_index, args.season)
    args.event_index_data = index
    nickname_map = build_nba_team_nickname_map(args.games_csv, args.season)
    completed = store.successful_game_ids(args.season, provider=ESPN_PROVIDER)
    processed = 0
    source_error: str | None = None
    for game in games:
        game_id = str(game["game_id"])
        if game_id in completed:
            continue
        if args.max_games is not None and processed >= args.max_games:
            break
        date = str(game["game_date"])
        try:
            mappings = _date_event_mapping(date, games, nickname_map, index, store, args)
            mapping = mappings.get(game_id)
            if mapping is None:
                raise RuntimeError(f"espn_event_mapping_missing:{game_id}")
            result = _import_one_summary(store, game, mapping, args)
        except Exception as exc:
            source_error = f"{type(exc).__name__}:{str(exc)[:500]}"
            print(json.dumps({"game_id": game_id, "status": "stopped_on_source_error",
                              "error_type": type(exc).__name__, "error": str(exc)[:500]}, ensure_ascii=False), flush=True)
            break
        store.set_active_provider(ESPN_PROVIDER)
        completed.add(game_id)
        processed += 1
        coverage = store.coverage(args.season, provider=ESPN_PROVIDER)
        print(json.dumps({
            "game_id": game_id,
            "event_id": mapping["event_id"],
            "status": result.get("status"),
            "player_rows": result.get("player_rows"),
            "coverage": {
                "collected_games": coverage.get("collected_games"),
                "expected_games": coverage.get("expected_games"),
                "failed_requests": coverage.get("failed_requests"),
                "not_collected_games": coverage.get("not_collected_games"),
            },
        }, ensure_ascii=False), flush=True)
        if args.delay_seconds > 0:
            time.sleep(args.delay_seconds)

    final = store.coverage(args.season, provider=ESPN_PROVIDER)
    print(json.dumps(final, ensure_ascii=False), flush=True)
    return 2 if source_error else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games-csv", type=Path, required=True)
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--event-index", type=Path, default=DEFAULT_EVENT_INDEX)
    parser.add_argument("--probe-manifest", type=Path, default=PROBE_MANIFEST)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--diagnostics-dir", type=Path, default=DEFAULT_DIAGNOSTICS_DIR)
    parser.add_argument("--import-probes", action="store_true")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--max-games", type=int)
    parser.add_argument("--delay-seconds", type=float, default=1.5)
    parser.add_argument("--timeout-seconds", type=int, default=20)
    args = parser.parse_args(argv)
    if args.max_games is not None and args.max_games < 1:
        raise SystemExit("--max-games must be positive")
    if args.delay_seconds < 0 or args.timeout_seconds < 1:
        raise SystemExit("invalid_collection_limits")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
