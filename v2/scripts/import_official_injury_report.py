"""Import one timestamped NBA official injury-report PDF for historical replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from v2.pregame_system.storage import local_wall_time_to_utc

try:
    import pdfplumber
except ImportError as exc:  # pragma: no cover - exercised by the CLI environment check
    raise SystemExit("缺少 pdfplumber，请先安装 v2/requirements.txt 中的依赖。") from exc

try:
    from v2.scripts import collect_pregame_information as snapshots
except ModuleNotFoundError:  # Preserve direct script execution from the scripts directory.
    import collect_pregame_information as snapshots


V2_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = V2_DIR.parent
DEFAULT_ANALYSIS_DATABASE = (
    PROJECT_DIR / "analysis_v1" / "data" / "courtvision_analysis.sqlite"
)
DEFAULT_SNAPSHOT_DATABASE = V2_DIR / "db" / "courtvision_pregame_snapshots.sqlite"
OFFICIAL_PDF_PREFIX = (
    "https://ak-static.cms.nba.com/referee/injury/Injury-Report_"
)
SOURCE = "nba_official_injury_report_archive"
SOURCE_TIME_ZONE = ZoneInfo("America/New_York")


class InjuryImportError(RuntimeError):
    """A safe validation or import error."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise InjuryImportError(f"找不到历史分析数据库：{path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def load_game_identity(game_id: str, database_path: Path) -> dict:
    query = """
        SELECT
            game.game_id,
            game.game_date,
            home.team_id AS home_team_id,
            home.team_name AS home_team_name,
            home.team_abbr AS home_team_abbr,
            away.team_id AS away_team_id,
            away.team_name AS away_team_name,
            away.team_abbr AS away_team_abbr
        FROM dim_game AS game
        JOIN dim_team AS home ON home.team_id = game.home_team_id
        JOIN dim_team AS away ON away.team_id = game.away_team_id
        WHERE game.game_id = :game_id
    """
    with closing(_connect_read_only(database_path)) as connection:
        rows = connection.execute(query, {"game_id": game_id}).fetchall()
    if len(rows) != 1:
        raise InjuryImportError(f"比赛 {game_id} 无法在历史分析数据库中唯一匹配。")
    return dict(rows[0])


def _compact(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9@]", "", value or "").lower()


def _readable(value: str | None) -> str:
    text = (value or "").replace("\n", " ").strip()
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", text)
    text = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", text)
    text = re.sub(r"\s*;\s*", "; ", text)
    text = re.sub(r"\s*-\s*", " - ", text)
    text = re.sub(r"\s+", " ", text)
    text = text.replace("GLeague", "G League").replace("Two - Way", "Two-Way")
    text = re.sub(r"(\d)\s+(st|nd|rd|th)\b", r"\1\2", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=[A-Za-z])\(", " (", text)
    return text.strip()


def _player_name(value: str) -> str:
    readable = _readable(value)
    if "," not in readable:
        raise InjuryImportError(f"官方报告中的球员姓名格式无法识别：{readable}")
    family, given = (part.strip() for part in readable.split(",", 1))
    if not family or not given:
        raise InjuryImportError(f"官方报告中的球员姓名不完整：{readable}")
    return f"{given} {family}"


def _report_timestamp(page) -> datetime:
    words = sorted(
        (word for word in page.extract_words() if float(word["top"]) < 80),
        key=lambda word: (round(float(word["top"]), 1), float(word["x0"])),
    )
    heading = " ".join(str(word["text"]) for word in words)
    match = re.search(
        r"Injury\s*Report:\s*(\d{2}/\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(AM|PM)",
        heading,
        flags=re.IGNORECASE,
    )
    if not match:
        raise InjuryImportError("无法从 PDF 标题读取官方报告时间。")
    local = datetime.strptime(" ".join(match.groups()), "%m/%d/%y %I:%M %p")
    try:
        return local_wall_time_to_utc(local, SOURCE_TIME_ZONE.key).astimezone(SOURCE_TIME_ZONE)
    except ValueError as exc:
        raise InjuryImportError(f"报告标题中的本地时间无效：{exc}") from exc


def _extract_rows(pdf_path: Path) -> tuple[datetime, list[dict]]:
    contexts = {"game_date": None, "game_time": None, "matchup": None, "team": None}
    records: list[dict] = []
    report_time: datetime | None = None
    with pdfplumber.open(pdf_path) as document:
        if not document.pages:
            raise InjuryImportError("官方伤病报告 PDF 没有页面。")
        for page_number, page in enumerate(document.pages, start=1):
            current_report_time = _report_timestamp(page)
            if report_time is None:
                report_time = current_report_time
            elif current_report_time != report_time:
                raise InjuryImportError("PDF 各页报告时间不一致。")
            width = float(page.width)
            table = page.extract_table(
                {
                    "vertical_strategy": "explicit",
                    "explicit_vertical_lines": [
                        width * ratio
                        for ratio in (0.024, 0.137, 0.232, 0.307, 0.500, 0.690, 0.785, 0.980)
                    ],
                    "horizontal_strategy": "lines",
                    "snap_tolerance": 5,
                    "join_tolerance": 5,
                }
            )
            if not table:
                raise InjuryImportError(f"PDF 第 {page_number} 页无法提取表格。")
            header_index = next(
                (
                    index
                    for index, row in enumerate(table)
                    if row and _compact(row[0]) == "gamedate" and _compact(row[4]) == "playername"
                ),
                None,
            )
            start_index = header_index + 1 if header_index is not None else 0
            for row in table[start_index:]:
                cells = list(row or []) + [None] * 7
                game_date, game_time, matchup, team, player, status, reason = cells[:7]
                values = {
                    "game_date": game_date,
                    "game_time": game_time,
                    "matchup": matchup,
                    "team": team,
                }
                for key, value in values.items():
                    if value and _compact(value):
                        contexts[key] = _readable(value)
                if (
                    not player
                    or not _compact(player)
                    or "," not in player
                    or not status
                    or not _compact(status)
                ):
                    continue
                records.append(
                    {
                        **contexts,
                        "player_name": _player_name(player),
                        "availability_status": _readable(status),
                        "reason": _readable(reason),
                        "page": page_number,
                    }
                )
    if report_time is None or not records:
        raise InjuryImportError("官方伤病报告没有可用记录。")
    return report_time, records


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cutoff_to_utc(cutoff_time: str) -> tuple[datetime, str]:
    try:
        local = datetime.fromisoformat(cutoff_time)
    except ValueError as exc:
        raise InjuryImportError("截止时间格式无效，请使用 YYYY-MM-DDTHH:MM:SS。") from exc
    if local.tzinfo is not None:
        raise InjuryImportError("截止时间应使用与比赛数据库一致的无时区本地时间。")
    try:
        aware = local_wall_time_to_utc(local, SOURCE_TIME_ZONE.key).astimezone(SOURCE_TIME_ZONE)
    except ValueError as exc:
        raise InjuryImportError(f"截止时间无法无歧义地转换：{exc}") from exc
    return aware, _utc_text(aware)


def _install_archive_eligibility_view(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP VIEW IF EXISTS agent_eligible_injury_snapshots;
        CREATE VIEW agent_eligible_injury_snapshots AS
        SELECT injury.*
        FROM injury_snapshots AS injury
        WHERE injury.source_published_at IS NOT NULL
          AND injury.source_published_at <= injury.cutoff_time
          AND (
              (injury.normalized_status = 'available' AND injury.captured_at <= injury.cutoff_time)
              OR
              (
                  injury.normalized_status = 'available_historical_archive'
                  AND injury.source = 'nba_official_injury_report_archive'
                  AND injury.source_url LIKE
                      'https://ak-static.cms.nba.com/referee/injury/Injury-Report_%.pdf'
                  AND injury.raw_path IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM source_fetch_runs AS fetch
                      WHERE fetch.game_id = injury.game_id
                        AND fetch.source_type = 'injuries'
                        AND fetch.source = injury.source
                        AND fetch.source_url = injury.source_url
                        AND fetch.captured_at = injury.captured_at
                        AND fetch.raw_path = injury.raw_path
                        AND fetch.fetch_status = 'success'
                        AND fetch.normalized_status = 'available_historical_archive'
                        AND fetch.response_sha256 IS NOT NULL
                  )
              )
          );
        UPDATE schema_metadata SET value = '1.1' WHERE key = 'schema_version';
        """
    )


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
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


def import_report(
    *,
    game_id: str,
    cutoff_time: str,
    pdf_path: Path,
    source_url: str,
    analysis_database: Path = DEFAULT_ANALYSIS_DATABASE,
    snapshot_database: Path = DEFAULT_SNAPSHOT_DATABASE,
    log_path: Path | None = None,
) -> dict:
    pdf_path = Path(pdf_path).resolve()
    if not pdf_path.is_file():
        raise InjuryImportError(f"找不到官方伤病报告 PDF：{pdf_path}")
    if not source_url.startswith(OFFICIAL_PDF_PREFIX) or not source_url.endswith(".pdf"):
        raise InjuryImportError("来源 URL 不是 NBA 官方伤病报告 PDF 地址。")
    if not Path(snapshot_database).is_file():
        raise InjuryImportError(f"找不到赛前快照数据库：{snapshot_database}")

    identity = load_game_identity(game_id, Path(analysis_database))
    report_local, all_rows = _extract_rows(pdf_path)
    cutoff_local, cutoff_utc = _cutoff_to_utc(cutoff_time)
    report_utc = _utc_text(report_local)
    if report_local > cutoff_local:
        raise InjuryImportError("官方报告发布时间晚于赛前截止时间，拒绝导入。")

    expected_matchup = (
        f"{identity['away_team_abbr']}@{identity['home_team_abbr']}".upper()
    )
    expected_date = datetime.fromisoformat(identity["game_date"]).strftime("%m/%d/%Y")
    expected_teams = {
        _compact(identity["away_team_name"]): (
            str(identity["away_team_id"]),
            identity["away_team_name"],
        ),
        _compact(identity["home_team_name"]): (
            str(identity["home_team_id"]),
            identity["home_team_name"],
        ),
    }
    selected = [
        row
        for row in all_rows
        if _compact(row["matchup"]) == _compact(expected_matchup)
        and _compact(row["game_date"]) == _compact(expected_date)
        and _compact(row["team"]) in expected_teams
    ]
    if not selected:
        raise InjuryImportError("官方报告中没有匹配目标比赛、日期和两支球队的球员记录。")
    if {_compact(row["team"]) for row in selected} != set(expected_teams):
        raise InjuryImportError("官方报告没有同时匹配目标比赛的两支球队。")
    player_keys = [(_compact(row["team"]), _compact(row["player_name"])) for row in selected]
    if len(player_keys) != len(set(player_keys)):
        raise InjuryImportError("官方报告的目标比赛球员记录存在重复。")

    digest = sha256_file(pdf_path)
    captured_at = datetime.fromtimestamp(pdf_path.stat().st_mtime, timezone.utc)
    captured_utc = _utc_text(captured_at)
    source_records = []
    for row in selected:
        team_id, team_name = expected_teams[_compact(row["team"])]
        record_id = "|".join(
            [game_id, report_utc, _compact(team_name), _compact(row["player_name"])]
        )
        source_records.append(
            snapshots.make_injury_record(
                game_id=game_id,
                source=SOURCE,
                source_record_id=record_id,
                source_published_at=report_utc,
                captured_at=captured_utc,
                cutoff_time=cutoff_utc,
                raw_status=row["availability_status"],
                normalized_status="available_historical_archive",
                team_id=team_id,
                team_name=team_name,
                player_id=None,
                player_name=row["player_name"],
                availability_status=row["availability_status"],
                reason=row["reason"],
                source_url=source_url,
                raw_path=str(pdf_path),
            )
        )

    run_id = f"official-injury-{game_id}-{digest[:16]}"
    snapshot_database = Path(snapshot_database)
    with closing(sqlite3.connect(snapshot_database)) as connection:
        _install_archive_eligibility_view(connection)
        existing_run = connection.execute(
            "SELECT 1 FROM source_fetch_runs WHERE run_id = ? LIMIT 1", (run_id,)
        ).fetchone()
        if existing_run is None:
            connection.execute(
                """
                INSERT INTO source_fetch_runs (
                    run_id, game_id, source_type, source, source_url,
                    requested_at, captured_at, cutoff_time, http_status,
                    fetch_status, normalized_status, normalized_records,
                    response_sha256, raw_path, error_type, error_message,
                    suggested_manual_url, suggested_save_path
                ) VALUES (?, ?, 'injuries', ?, ?, ?, ?, ?, 200,
                          'success', 'available_historical_archive', ?, ?, ?,
                          NULL, NULL, ?, ?)
                """,
                (
                    run_id,
                    game_id,
                    SOURCE,
                    source_url,
                    captured_utc,
                    captured_utc,
                    cutoff_utc,
                    len(source_records),
                    digest,
                    str(pdf_path),
                    source_url,
                    str(pdf_path),
                ),
            )
        columns = snapshots.SNAPSHOT_TABLES["injury_snapshots"]
        placeholders = ", ".join(f":{column}" for column in columns)
        connection.executemany(
            f"INSERT OR IGNORE INTO injury_snapshots ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            source_records,
        )
        connection.commit()

    eligible = snapshots.load_agent_eligible_records(
        snapshot_database, "injury_snapshots", game_id, cutoff_utc
    )
    eligible_ids = {row["source_record_id"] for row in eligible}
    if {row["source_record_id"] for row in source_records} - eligible_ids:
        raise InjuryImportError("导入记录未全部通过历史归档时间与来源门禁。")

    result = {
        "schema_version": "1.1",
        "game_id": game_id,
        "matchup": expected_matchup,
        "game_date": expected_date,
        "source": SOURCE,
        "source_url": source_url,
        "source_report_time_local": report_local.isoformat(timespec="seconds"),
        "source_published_at_utc": report_utc,
        "cutoff_time_source_local": cutoff_local.isoformat(timespec="seconds"),
        "cutoff_time_utc": cutoff_utc,
        "captured_at_utc": captured_utc,
        "capture_relation_to_cutoff": "after_cutoff_historical_archive",
        "raw_path": str(pdf_path),
        "raw_sha256": digest,
        "normalized_status": "available_historical_archive",
        "record_count": len(source_records),
        "records": [
            {
                "team_id": row["team_id"],
                "team_name": row["team_name"],
                "player_name": row["player_name"],
                "availability_status": row["availability_status"],
                "reason": row["reason"],
                "source_record_id": row["source_record_id"],
            }
            for row in source_records
        ],
        "boundary_note": (
            "这是今天获取的 NBA 官方历史归档，报告标题证明其在截止时间前发布；"
            "它不构成 CourtVision 当时已经持久化该快照的证据。"
        ),
    }
    if log_path is not None:
        _atomic_write_json(Path(log_path), result)
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="导入一份 NBA 官方历史伤病报告 PDF。")
    parser.add_argument("--game-id", required=True)
    parser.add_argument("--cutoff-time", required=True)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--analysis-database", type=Path, default=DEFAULT_ANALYSIS_DATABASE)
    parser.add_argument("--snapshot-database", type=Path, default=DEFAULT_SNAPSHOT_DATABASE)
    parser.add_argument("--log", type=Path)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    log_path = args.log or (
        V2_DIR / "data" / "pregame_snapshots" / f"injury_import_{args.game_id}.json"
    )
    try:
        result = import_report(
            game_id=args.game_id,
            cutoff_time=args.cutoff_time,
            pdf_path=args.pdf,
            source_url=args.source_url,
            analysis_database=args.analysis_database,
            snapshot_database=args.snapshot_database,
            log_path=log_path,
        )
    except (InjuryImportError, snapshots.CollectionError, sqlite3.Error) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    print(f"官方报告已验证并导入：{result['record_count']} 条")
    print(f"报告时间：{result['source_report_time_local']}")
    print(f"原始文件 SHA256：{result['raw_sha256']}")
    print(f"导入日志：{log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
