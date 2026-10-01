"""Create the independent CourtVision pregame-information snapshot database."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


V2_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = V2_DIR / "sql" / "03_pregame_information_schema.sql"
DEFAULT_DATABASE_PATH = V2_DIR / "db" / "courtvision_pregame_snapshots.sqlite"


class DatabaseBuildError(RuntimeError):
    """A safe, expected database-build failure."""


def build_database(database_path: Path, schema_path: Path = DEFAULT_SCHEMA_PATH) -> Path:
    database_path = Path(database_path)
    schema_path = Path(schema_path)
    if database_path.exists():
        raise DatabaseBuildError(f"目标数据库已经存在，拒绝覆盖：{database_path}")
    if not schema_path.is_file():
        raise DatabaseBuildError(f"找不到建库脚本：{schema_path}")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    schema = schema_path.read_text(encoding="utf-8")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=database_path.parent,
            prefix=f".{database_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        connection = sqlite3.connect(temporary_path)
        try:
            connection.executescript(schema)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise DatabaseBuildError(f"新数据库完整性检查失败：{integrity}")
            connection.commit()
        finally:
            connection.close()
        os.replace(temporary_path, database_path)
        return database_path
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseBuildError(f"赛前信息数据库创建失败：{exc}") from exc
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="创建独立的赛前信息快照数据库。")
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help=f"输出数据库路径，默认：{DEFAULT_DATABASE_PATH}",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA_PATH,
        help=f"建库 SQL 路径，默认：{DEFAULT_SCHEMA_PATH}",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        path = build_database(args.database, args.schema)
    except DatabaseBuildError as exc:
        print(f"建库失败：{exc}", file=sys.stderr)
        return 1
    print(f"赛前信息快照数据库已创建：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
