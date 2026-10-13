"""Make one bounded NBA Stats boxscore request and persist sanitized diagnostics."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from v2.pregame_system.nba_stats_diagnostics import diagnose_nba_stats_request
from v2.pregame_system.player_knowledge import (
    DEFAULT_DATABASE,
    ENDPOINT,
    PROVIDER,
    SOURCE_URL,
    PlayerKnowledgeStore,
    _utc_iso,
    read_games_csv,
    normalize_boxscore_payload,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games-csv", type=Path, required=True)
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--game-id", default="0022500001")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output-dir", type=Path,
                        default=PROJECT_ROOT / "v2" / "data" / "player_knowledge_diagnostics")
    parser.add_argument("--timeout-seconds", type=int, default=20)
    parser.add_argument("--max-body-bytes", type=int, default=1_000_000)
    parser.add_argument("--header-profile", choices=("stats_default", "nba_site"), default="stats_default")
    return parser.parse_args(argv)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _prior_failed_receipt(database: Path, game_id: str) -> dict[str, object] | None:
    if not database.is_file():
        return None
    uri = database.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        row = connection.execute(
            "SELECT receipt_id, client_version, collection_time_utc, error_type, error_message "
            "FROM player_knowledge_source_receipts WHERE game_id=? AND status='failed' "
            "ORDER BY receipt_id DESC LIMIT 1",
            (game_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "receipt_id": row[0],
            "client_version": row[1],
            "collection_time_utc": row[2],
            "error_type": row[3],
            "error_message": str(row[4] or "")[:1000],
        }
    finally:
        connection.close()


def run(args: argparse.Namespace) -> int:
    game_id = str(args.game_id)
    games = read_games_csv(args.games_csv, args.season)
    game = next((row for row in games if row["game_id"] == game_id), None)
    if game is None:
        raise SystemExit("game_id_not_in_explicit_regular_season_schedule")

    report, payload, response_body = diagnose_nba_stats_request(
        game_id,
        timeout_seconds=args.timeout_seconds,
        max_body_bytes=args.max_body_bytes,
        header_profile=args.header_profile,
    )
    report["game_identity"] = game
    report["client"] = "stdlib urllib diagnostic; nba_api 1.10.2 request defaults inspected locally"
    report["previous_failed_receipt"] = _prior_failed_receipt(args.database, game_id)
    raw_path = None
    if payload is not None:
        try:
            normalized = normalize_boxscore_payload(game, payload)
            player_ids = [row["player_id"] for row in normalized if row.get("player_id") is not None]
            report["normalization"] = {
                "status": "valid",
                "player_rows": len(normalized),
                "unique_player_ids": len(set(player_ids)),
                "returned_team_ids": sorted({int(row["team_id"]) for row in normalized}),
                "starter_true_rows": sum(row["starter"] == 1 for row in normalized),
                "starter_false_rows": sum(row["starter"] == 0 for row in normalized),
                "starter_unknown_rows": sum(row["starter"] is None for row in normalized),
                "minutes_present_rows": sum(row["minutes_seconds"] is not None for row in normalized),
                "confirmed_dnp_rows": sum(row["participation_status"] == "did_not_play_confirmed" for row in normalized),
                "unknown_participation_rows": sum(row["participation_status"] == "unknown" for row in normalized),
            }
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            raw_path = PROJECT_ROOT / "v2" / "data" / "raw" / "player_knowledge_probes" / f"{game_id}_{stamp}.json"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(response_body)
            report["probe_raw_path"] = str(raw_path.relative_to(PROJECT_ROOT))
        except Exception as exc:
            report["normalization"] = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            }
            report["classification"] = "json_payload_identity_or_normalization_error"
            report["is_expected_boxscore_payload"] = False
            payload = None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.output_dir / f"{game_id}_{stamp}.json"
    report["diagnostic_file"] = str(report_path)
    _write_json(report_path, report)

    if payload is None and args.database.is_file():
        try:
            store = PlayerKnowledgeStore(args.database)
            summary = {
                "classification": report.get("classification"),
                "http_status_code": report.get("http_status_code"),
                "content_type": report.get("content_type"),
                "actual_response_bytes_read": report.get("actual_response_bytes_read"),
                "diagnostic_file": str(report_path),
            }
            store.record_failure(
                game_id,
                error_type=str(report.get("classification") or "diagnostic_failed"),
                error_message=json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                collection_time_utc=report.get("request_finished_at") or _utc_iso(datetime.now(timezone.utc)),
                request_parameters={
                    **(report.get("request_parameters") or {}),
                    "user_agent": report.get("user_agent"),
                    "referer": report.get("referer"),
                    "origin": report.get("origin"),
                    "header_profile": report.get("header_profile"),
                    "diagnostic_file": str(report_path),
                },
                source_url=SOURCE_URL,
                provider=PROVIDER,
                endpoint=ENDPOINT,
                client_version="stdlib urllib direct diagnostic; nba_api 1.10.2 headers",
            )
        except Exception as exc:
            report["database_failure_receipt_error"] = f"{type(exc).__name__}:{str(exc)[:300]}"
            _write_json(report_path, report)

    print(json.dumps({
        "game_id": game_id,
        "classification": report.get("classification"),
        "http_status_code": report.get("http_status_code"),
        "content_type": report.get("content_type"),
        "content_length_header": report.get("content_length_header"),
        "actual_response_bytes_read": report.get("actual_response_bytes_read"),
        "is_empty": report.get("is_empty"),
        "is_html": report.get("is_html"),
        "is_json": report.get("is_json"),
        "redirect_count": report.get("redirect_count"),
        "exception_type": report.get("exception_type"),
        "diagnostic_file": str(report_path),
        "probe_raw_path": report.get("probe_raw_path"),
        "normalization": report.get("normalization"),
    }, ensure_ascii=False), flush=True)
    return 0 if payload is not None else 2


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.timeout_seconds < 1 or args.max_body_bytes < 1024:
        raise SystemExit("invalid_diagnostic_limits")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
