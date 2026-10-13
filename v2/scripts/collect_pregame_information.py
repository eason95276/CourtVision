"""Collect and gate timestamped pregame injuries, lineups, and market records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


V2_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PATH = V2_DIR / "db" / "courtvision_pregame_snapshots.sqlite"
DEFAULT_RAW_DIR = V2_DIR / "data" / "pregame_snapshots" / "raw"
DEFAULT_LOG_PATH = V2_DIR / "data" / "pregame_snapshots" / "collection_log.json"

NBA_INJURY_URL = "https://official.nba.com/nba-injury-report-2025-26-season/"
NBA_LINEUPS_URL = "https://www.nba.com/players/todays-lineups?hidenav=true"
ODDS_API_DOCS_URL = "https://the-odds-api.com/liveapi/guides/v4/"
ODDS_API_URL = (
    "https://api.the-odds-api.com/v4/sports/basketball_nba/odds/"
    "?regions=us&markets=spreads,totals&oddsFormat=decimal&dateFormat=iso"
)

SNAPSHOT_TABLES = {
    "injury_snapshots": (
        "game_id",
        "source",
        "source_record_id",
        "source_published_at",
        "captured_at",
        "cutoff_time",
        "raw_status",
        "normalized_status",
        "team_id",
        "team_name",
        "player_id",
        "player_name",
        "availability_status",
        "reason",
        "source_url",
        "raw_path",
    ),
    "lineup_snapshots": (
        "game_id",
        "source",
        "source_record_id",
        "source_published_at",
        "captured_at",
        "cutoff_time",
        "raw_status",
        "normalized_status",
        "lineup_kind",
        "team_id",
        "team_name",
        "player_id",
        "player_name",
        "position",
        "source_url",
        "raw_path",
    ),
    "market_snapshots": (
        "game_id",
        "source",
        "source_record_id",
        "source_published_at",
        "captured_at",
        "cutoff_time",
        "raw_status",
        "normalized_status",
        "provider_event_id",
        "bookmaker",
        "home_spread",
        "total",
        "source_url",
        "raw_path",
    ),
}


class CollectionError(RuntimeError):
    """A safe, expected collection or validation failure."""


class FetchResponse:
    def __init__(self, status_code: int, body: bytes, headers: dict[str, str]):
        self.status_code = status_code
        self.body = body
        self.headers = headers


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_utc(value: str | None, field: str, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CollectionError(f"{field} 必须是带 Z 的 UTC 时间。")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CollectionError(f"{field} 不是有效 UTC 时间。") from exc


def _base_record(
    *,
    game_id: str,
    source: str,
    source_record_id: str,
    source_published_at: str | None,
    captured_at: str,
    cutoff_time: str,
    raw_status: str,
    normalized_status: str,
) -> dict:
    required_strings = {
        "game_id": game_id,
        "source": source,
        "source_record_id": source_record_id,
        "captured_at": captured_at,
        "cutoff_time": cutoff_time,
        "raw_status": raw_status,
        "normalized_status": normalized_status,
    }
    for field, value in required_strings.items():
        if not isinstance(value, str) or not value.strip():
            raise CollectionError(f"{field} 不能为空。")
    _validate_utc(source_published_at, "source_published_at", allow_none=True)
    _validate_utc(captured_at, "captured_at")
    _validate_utc(cutoff_time, "cutoff_time")
    if normalized_status.startswith("available") and source_published_at is None:
        raise CollectionError("可用记录必须具有 source_published_at。")
    return {**required_strings, "source_published_at": source_published_at}


def make_injury_record(
    *,
    game_id: str,
    source_record_id: str,
    source_published_at: str | None,
    captured_at: str,
    cutoff_time: str,
    player_name: str,
    team_name: str,
    raw_status: str,
    normalized_status: str,
    source: str = "nba_official_injury_report",
    team_id: str | None = None,
    player_id: str | None = None,
    availability_status: str | None = None,
    reason: str | None = None,
    source_url: str | None = NBA_INJURY_URL,
    raw_path: str | None = None,
) -> dict:
    record = _base_record(
        game_id=game_id,
        source=source,
        source_record_id=source_record_id,
        source_published_at=source_published_at,
        captured_at=captured_at,
        cutoff_time=cutoff_time,
        raw_status=raw_status,
        normalized_status=normalized_status,
    )
    if not player_name or not team_name:
        raise CollectionError("伤病记录必须包含球员和球队名称。")
    return {
        **record,
        "team_id": team_id,
        "team_name": team_name,
        "player_id": player_id,
        "player_name": player_name,
        "availability_status": availability_status,
        "reason": reason,
        "source_url": source_url,
        "raw_path": raw_path,
    }


def make_lineup_record(
    *,
    game_id: str,
    source_record_id: str,
    source_published_at: str | None,
    captured_at: str,
    cutoff_time: str,
    team_name: str,
    player_name: str,
    lineup_kind: str,
    raw_status: str,
    normalized_status: str,
    source: str = "nba_official_today_lineups",
    team_id: str | None = None,
    player_id: str | None = None,
    position: str | None = None,
    source_url: str | None = NBA_LINEUPS_URL,
    raw_path: str | None = None,
) -> dict:
    record = _base_record(
        game_id=game_id,
        source=source,
        source_record_id=source_record_id,
        source_published_at=source_published_at,
        captured_at=captured_at,
        cutoff_time=cutoff_time,
        raw_status=raw_status,
        normalized_status=normalized_status,
    )
    if lineup_kind not in {"expected", "confirmed"}:
        raise CollectionError("lineup_kind 只能是 expected 或 confirmed。")
    if not player_name or not team_name:
        raise CollectionError("首发记录必须包含球员和球队名称。")
    return {
        **record,
        "lineup_kind": lineup_kind,
        "team_id": team_id,
        "team_name": team_name,
        "player_id": player_id,
        "player_name": player_name,
        "position": position,
        "source_url": source_url,
        "raw_path": raw_path,
    }


def make_market_record(
    *,
    game_id: str,
    source_record_id: str,
    source_published_at: str | None,
    captured_at: str,
    cutoff_time: str,
    bookmaker: str,
    home_spread: float | None,
    total: float | None,
    raw_status: str,
    normalized_status: str,
    source: str = "the_odds_api",
    provider_event_id: str | None = None,
    source_url: str | None = ODDS_API_DOCS_URL,
    raw_path: str | None = None,
) -> dict:
    record = _base_record(
        game_id=game_id,
        source=source,
        source_record_id=source_record_id,
        source_published_at=source_published_at,
        captured_at=captured_at,
        cutoff_time=cutoff_time,
        raw_status=raw_status,
        normalized_status=normalized_status,
    )
    if not bookmaker:
        raise CollectionError("bookmaker 不能为空。")
    return {
        **record,
        "provider_event_id": provider_event_id,
        "bookmaker": bookmaker,
        "home_spread": home_spread,
        "total": total,
        "source_url": source_url,
        "raw_path": raw_path,
    }


def insert_records(database_path: Path, table: str, records: list[dict]) -> int:
    if table not in SNAPSHOT_TABLES:
        raise CollectionError(f"不允许写入未知表：{table}")
    if not records:
        return 0
    columns = SNAPSHOT_TABLES[table]
    placeholders = ", ".join(f":{column}" for column in columns)
    sql = f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    for record in records:
        missing = set(columns) - set(record)
        if missing:
            raise CollectionError(f"{table} 记录缺少字段：{sorted(missing)}")
    with closing(sqlite3.connect(database_path)) as connection:
        before = connection.total_changes
        connection.executemany(sql, records)
        connection.commit()
        return connection.total_changes - before


def load_agent_eligible_records(
    database_path: Path,
    table: str,
    game_id: str,
    cutoff_time: str,
) -> list[dict]:
    if table not in SNAPSHOT_TABLES:
        raise CollectionError(f"不允许读取未知表：{table}")
    _validate_utc(cutoff_time, "cutoff_time")
    source_name = (
        "agent_eligible_injury_snapshots"
        if table == "injury_snapshots"
        else table
    )
    eligibility_sql = ""
    if table != "injury_snapshots":
        eligibility_sql = """
                  AND normalized_status = 'available'
                  AND source_published_at IS NOT NULL
                  AND source_published_at <= :cutoff_time
                  AND captured_at <= :cutoff_time
        """
    uri = f"{Path(database_path).resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            f"""
                SELECT *
                FROM {source_name}
                WHERE game_id = :game_id
                  AND source_published_at <= :cutoff_time
                  {eligibility_sql}
                ORDER BY source_published_at, source_record_id
            """,
            {"game_id": game_id, "cutoff_time": cutoff_time},
        ).fetchall()
    return [dict(row) for row in rows]


def fetch_url(
    url: str,
    timeout_seconds: float,
    api_key: str | None = None,
) -> FetchResponse:
    request_url = url
    if api_key:
        separator = "&" if "?" in request_url else "?"
        request_url = f"{request_url}{separator}apiKey={api_key}"
    request = urllib.request.Request(
        request_url,
        headers={
            "User-Agent": "CourtVision/1.0 pregame-source-audit",
            "Accept": "application/json,text/html,application/pdf,*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return FetchResponse(
                status_code=int(response.status),
                body=response.read(),
                headers={key.lower(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        raise CollectionError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise CollectionError(f"{type(exc).__name__}: source request failed") from exc


def _archive_raw(raw_dir: Path, source: str, captured_at: str, body: bytes, suffix: str) -> tuple[Path, str]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = captured_at.replace(":", "").replace("-", "")
    destination = raw_dir / f"{source}_{stamp}.{suffix}"
    digest = hashlib.sha256(body).hexdigest()
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=raw_dir,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(body)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return destination, digest


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(value, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _status_template(source: str, url: str, suggested_path: Path) -> dict:
    return {
        "source": source,
        "source_url": url,
        "fetch_status": "not_requested",
        "http_status": None,
        "captured_at": None,
        "normalized_status": "unavailable",
        "normalized_records": 0,
        "availability": "unavailable",
        "response_sha256": None,
        "raw_path": None,
        "error_type": None,
        "error_message": None,
        "suggested_manual_url": url,
        "suggested_save_path": str(suggested_path),
    }


def _record_fetch_runs(database_path: Path, result: dict) -> None:
    sql = """
        INSERT INTO source_fetch_runs (
            run_id, game_id, source_type, source, source_url,
            requested_at, captured_at, cutoff_time, http_status,
            fetch_status, normalized_status, normalized_records,
            response_sha256, raw_path, error_type, error_message,
            suggested_manual_url, suggested_save_path
        ) VALUES (
            :run_id, :game_id, :source_type, :source, :source_url,
            :requested_at, :captured_at, :cutoff_time, :http_status,
            :fetch_status, :normalized_status, :normalized_records,
            :response_sha256, :raw_path, :error_type, :error_message,
            :suggested_manual_url, :suggested_save_path
        )
    """
    rows = []
    for source_type, status in result["sources"].items():
        rows.append(
            {
                "run_id": result["run_id"],
                "game_id": result["game_id"],
                "source_type": source_type,
                "source": status["source"],
                "source_url": status["source_url"],
                "requested_at": result["requested_at"],
                "captured_at": status["captured_at"],
                "cutoff_time": result["cutoff_time"],
                "http_status": status["http_status"],
                "fetch_status": status["fetch_status"],
                "normalized_status": status["normalized_status"],
                "normalized_records": status["normalized_records"],
                "response_sha256": status["response_sha256"],
                "raw_path": status["raw_path"],
                "error_type": status["error_type"],
                "error_message": status["error_message"],
                "suggested_manual_url": status["suggested_manual_url"],
                "suggested_save_path": status["suggested_save_path"],
            }
        )
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executemany(sql, rows)
        connection.commit()


def collect_sources(
    *,
    game_id: str | None,
    cutoff_time: str | None,
    database_path: Path,
    raw_dir: Path,
    log_path: Path,
    sources: list[str],
    injury_preflight_timeout: bool = False,
    odds_api_key: str | None = None,
    timeout_seconds: float = 15.0,
    fetcher: Callable = fetch_url,
) -> dict:
    database_path = Path(database_path)
    raw_dir = Path(raw_dir)
    log_path = Path(log_path)
    if not database_path.is_file():
        raise CollectionError(f"找不到赛前信息快照数据库：{database_path}")
    allowed_sources = {"injuries", "lineups", "market"}
    if any(source not in allowed_sources for source in sources):
        raise CollectionError("sources 包含未知来源类型。")
    if (game_id is None) != (cutoff_time is None):
        raise CollectionError("game_id 与 cutoff_time 必须同时提供，或同时省略进行来源探测。")
    if cutoff_time is not None:
        _validate_utc(cutoff_time, "cutoff_time")

    requested_at = utc_now()
    result = {
        "schema_version": "1.0",
        "run_id": str(uuid.uuid4()),
        "mode": "source_probe" if game_id is None else "game_snapshot_collection",
        "game_id": game_id,
        "cutoff_time": cutoff_time,
        "requested_at": requested_at,
        "retry_count": 0,
        "sources": {
            "injuries": _status_template(
                "nba_official_injury_report",
                NBA_INJURY_URL,
                raw_dir / "nba_official_injury_report_<captured_at>.html",
            ),
            "lineups": _status_template(
                "nba_official_today_lineups",
                NBA_LINEUPS_URL,
                raw_dir / "nba_official_today_lineups_<captured_at>.html",
            ),
            "market": _status_template(
                "the_odds_api",
                ODDS_API_DOCS_URL,
                raw_dir / "the_odds_api_<captured_at>.json",
            ),
        },
    }

    if injury_preflight_timeout:
        status = result["sources"]["injuries"]
        status.update(
            {
                "fetch_status": "timeout",
                "error_type": "TaskCanceledException",
                "error_message": "官方赛季伤病报告页预检在 15 秒上限内未返回；本次采集抑制重试。",
            }
        )
    elif "injuries" in sources:
        status = result["sources"]["injuries"]
        try:
            response = fetcher(NBA_INJURY_URL, timeout_seconds, None)
            captured = utc_now()
            raw_path, digest = _archive_raw(
                raw_dir, "nba_official_injury_report", captured, response.body, "html"
            )
            status.update(
                {
                    "fetch_status": "success",
                    "http_status": response.status_code,
                    "captured_at": captured,
                    "response_sha256": digest,
                    "raw_path": str(raw_path),
                    "error_message": "页面已归档，但没有解析并匹配到带发布时间的目标比赛伤病记录。",
                }
            )
        except CollectionError as exc:
            status.update(
                {
                    "fetch_status": "failed",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )

    if "lineups" in sources:
        status = result["sources"]["lineups"]
        try:
            response = fetcher(NBA_LINEUPS_URL, timeout_seconds, None)
            captured = utc_now()
            raw_path, digest = _archive_raw(
                raw_dir, "nba_official_today_lineups", captured, response.body, "html"
            )
            status.update(
                {
                    "fetch_status": "success",
                    "http_status": response.status_code,
                    "captured_at": captured,
                    "response_sha256": digest,
                    "raw_path": str(raw_path),
                    "error_message": (
                        "页面已归档，但响应没有形成可证明发布时间且可匹配目标比赛的"
                        "预计或正式首发记录。"
                    ),
                }
            )
        except CollectionError as exc:
            status.update(
                {
                    "fetch_status": "failed",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )

    market_status = result["sources"]["market"]
    if not odds_api_key:
        market_status.update(
            {
                "fetch_status": "credentials_missing",
                "error_type": "MissingCredential",
                "error_message": "未配置 ODDS_API_KEY，未向付费盘口接口发送请求。",
            }
        )
    elif "market" in sources:
        try:
            response = fetcher(ODDS_API_URL, timeout_seconds, odds_api_key)
            captured = utc_now()
            raw_path, digest = _archive_raw(
                raw_dir, "the_odds_api", captured, response.body, "json"
            )
            market_status.update(
                {
                    "fetch_status": "success",
                    "http_status": response.status_code,
                    "captured_at": captured,
                    "response_sha256": digest,
                    "raw_path": str(raw_path),
                    "error_message": "响应已归档；本轮没有可安全匹配的目标比赛，因此未生成盘口快照。",
                }
            )
        except CollectionError as exc:
            market_status.update(
                {
                    "fetch_status": "failed",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )

    _record_fetch_runs(database_path, result)
    _atomic_write_json(log_path, result)
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="采集伤病、预计或正式首发和盘口的赛前来源，并执行时间门禁。"
    )
    parser.add_argument("--game-id", help="目标比赛 ID；来源探测模式可省略。")
    parser.add_argument("--cutoff-time", help="UTC 截止时间，例如 2026-10-20T23:00:00Z。")
    parser.add_argument(
        "--sources",
        default="injuries,lineups,market",
        help="逗号分隔：injuries,lineups,market。",
    )
    parser.add_argument(
        "--injury-preflight-timeout",
        action="store_true",
        help="记录本轮此前已经发生的 NBA 官方伤病页超时，并抑制重复请求。",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    sources = [item.strip() for item in args.sources.split(",") if item.strip()]
    try:
        result = collect_sources(
            game_id=args.game_id,
            cutoff_time=args.cutoff_time,
            database_path=args.database,
            raw_dir=args.raw_dir,
            log_path=args.log,
            sources=sources,
            injury_preflight_timeout=args.injury_preflight_timeout,
            odds_api_key=os.environ.get("ODDS_API_KEY", ""),
            timeout_seconds=args.timeout,
        )
    except CollectionError as exc:
        print(f"采集失败：{exc}", file=sys.stderr)
        return 1
    print(f"采集日志已写入：{args.log}")
    for source_type, status in result["sources"].items():
        print(
            f"{source_type}: fetch_status={status['fetch_status']}, "
            f"availability={status['availability']}, records={status['normalized_records']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
