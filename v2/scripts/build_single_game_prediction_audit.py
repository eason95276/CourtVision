"""生成单场历史预测验收事实，所有判断均由确定性代码完成。"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


V2_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = V2_DIR.parent
ANALYSIS_DATABASE_PATH = (
    PROJECT_DIR / "analysis_v1" / "data" / "courtvision_analysis.sqlite"
)
DETAIL_DATABASE_PATH = V2_DIR / "db" / "courtvision_v2.sqlite"
OUTPUTS_DIR = V2_DIR / "outputs"


class PredictionAuditError(Exception):
    """可安全展示给命令行用户的预测验收错误。"""


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def connect_read_only(path):
    if not path.is_file():
        raise PredictionAuditError(f"找不到数据库：{path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise PredictionAuditError(f"数据库未能进入只读查询模式：{path}")
    return connection


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="为同时存在于 CourtVision 两个数据库中的比赛生成历史预测验收事实。"
    )
    parser.add_argument(
        "--game-id",
        required=True,
        help="必填。需要验收的比赛 ID。",
    )
    return parser.parse_args(argv)


def load_detail_game(game_id):
    query = """
        SELECT
            g.game_id,
            g.game_date,
            g.season,
            g.season_type,
            g.away_team_id,
            g.away_team_tricode,
            away.team_city || ' ' || away.team_name AS away_team_name,
            g.away_points,
            g.home_team_id,
            g.home_team_tricode,
            home.team_city || ' ' || home.team_name AS home_team_name,
            g.home_points
        FROM games AS g
        JOIN team_boxscores AS away
            ON away.game_id = g.game_id AND away.side = 'away'
        JOIN team_boxscores AS home
            ON home.game_id = g.game_id AND home.side = 'home'
        WHERE g.game_id = :game_id
    """
    with closing(connect_read_only(DETAIL_DATABASE_PATH)) as connection:
        rows = connection.execute(query, {"game_id": game_id}).fetchall()
    if len(rows) == 0:
        raise PredictionAuditError(
            f"比赛 {game_id} 不存在于详细比赛数据库，未生成任何输出。"
        )
    if len(rows) != 1:
        raise PredictionAuditError(
            f"比赛 {game_id} 在详细比赛数据库中返回 {len(rows)} 行，无法唯一确定比赛。"
        )
    return dict(rows[0])


def load_analysis_game(game_id):
    game_query = "SELECT 1 FROM dim_game WHERE game_id = :game_id"
    prediction_query = "SELECT 1 FROM fact_prediction WHERE game_id = :game_id"
    audit_query = """
        SELECT
            g.game_id,
            g.game_date,
            g.home_team_id,
            g.away_team_id,
            g.home_score,
            g.away_score,
            g.home_win,
            g.point_margin,
            g.total_points,
            p.evaluation_season,
            p.win_probability,
            p.predicted_home_win,
            p.predicted_margin,
            p.predicted_total,
            p.model_scope,
            p.prediction_time_status,
            m.game_id AS market_game_id,
            m.market_expected_home_margin,
            m.market_total,
            m.line_capture_time_status,
            m.line_identity_status
        FROM dim_game AS g
        JOIN fact_prediction AS p ON p.game_id = g.game_id
        LEFT JOIN fact_market_benchmark AS m ON m.game_id = g.game_id
        WHERE g.game_id = :game_id
    """
    with closing(connect_read_only(ANALYSIS_DATABASE_PATH)) as connection:
        parameters = {"game_id": game_id}
        if connection.execute(game_query, parameters).fetchone() is None:
            raise PredictionAuditError(
                f"比赛 {game_id} 不存在于历史分析数据库，未生成任何输出。"
            )
        if connection.execute(prediction_query, parameters).fetchone() is None:
            raise PredictionAuditError(
                f"比赛 {game_id} 缺少历史回测预测记录，未生成任何输出。"
            )
        rows = connection.execute(audit_query, parameters).fetchall()
    if len(rows) != 1:
        raise PredictionAuditError(
            f"比赛 {game_id} 的历史预测查询返回 {len(rows)} 行，无法唯一确定记录。"
        )
    return dict(rows[0])


def validate_cross_database_identity(detail, analysis):
    comparisons = {
        "比赛 ID": (detail["game_id"], analysis["game_id"]),
        "比赛日期": (detail["game_date"], analysis["game_date"][:10]),
        "主队 ID": (detail["home_team_id"], analysis["home_team_id"]),
        "客队 ID": (detail["away_team_id"], analysis["away_team_id"]),
        "主队得分": (detail["home_points"], analysis["home_score"]),
        "客队得分": (detail["away_points"], analysis["away_score"]),
    }
    mismatches = [
        f"{label}（详细库={detail_value}，分析库={analysis_value}）"
        for label, (detail_value, analysis_value) in comparisons.items()
        if detail_value != analysis_value
    ]
    if mismatches:
        raise PredictionAuditError(
            "两个数据库的比赛身份或赛果不一致：" + "；".join(mismatches)
        )

    calculated_margin = detail["home_points"] - detail["away_points"]
    calculated_total = detail["home_points"] + detail["away_points"]
    if calculated_margin != analysis["point_margin"]:
        raise PredictionAuditError("分析库的实际主队分差与详细库比分不一致。")
    if calculated_total != analysis["total_points"]:
        raise PredictionAuditError("分析库的实际总分与详细库比分不一致。")


def grade_spread(predicted_margin, market_margin, actual_margin):
    if market_margin is None:
        return {
            "model_direction": None,
            "actual_result": None,
            "spread_direction_hit": None,
            "status": "market_unavailable",
            "reason": "缺少可用的历史市场让分基准，无法计算方向或命中结果。",
        }

    if predicted_margin > market_margin:
        model_direction = "home"
    elif predicted_margin < market_margin:
        model_direction = "away"
    else:
        model_direction = "no_disagreement"

    if actual_margin > market_margin:
        actual_result = "home"
    elif actual_margin < market_margin:
        actual_result = "away"
    else:
        actual_result = "push"

    if model_direction == "no_disagreement":
        return {
            "model_direction": model_direction,
            "actual_result": actual_result,
            "spread_direction_hit": None,
            "status": "no_model_market_disagreement",
            "reason": "模型预测主队分差与历史市场预期主队分差相等，无方向分歧。",
        }
    if actual_result == "push":
        return {
            "model_direction": model_direction,
            "actual_result": actual_result,
            "spread_direction_hit": None,
            "status": "push",
            "reason": "实际主队分差与历史市场预期主队分差相等，结果为走盘。",
        }
    return {
        "model_direction": model_direction,
        "actual_result": actual_result,
        "spread_direction_hit": model_direction == actual_result,
        "status": "graded",
        "reason": None,
    }


def grade_total(predicted_total, market_total, actual_total):
    if market_total is None:
        return {
            "model_direction": None,
            "actual_result": None,
            "total_direction_hit": None,
            "status": "market_unavailable",
            "reason": "缺少可用的历史市场总分基准，无法计算方向或命中结果。",
        }

    if predicted_total > market_total:
        model_direction = "over"
    elif predicted_total < market_total:
        model_direction = "under"
    else:
        model_direction = "no_disagreement"

    if actual_total > market_total:
        actual_result = "over"
    elif actual_total < market_total:
        actual_result = "under"
    else:
        actual_result = "push"

    if model_direction == "no_disagreement":
        return {
            "model_direction": model_direction,
            "actual_result": actual_result,
            "total_direction_hit": None,
            "status": "no_model_market_disagreement",
            "reason": "模型预测总分与历史市场总分相等，无方向分歧。",
        }
    if actual_result == "push":
        return {
            "model_direction": model_direction,
            "actual_result": actual_result,
            "total_direction_hit": None,
            "status": "push",
            "reason": "实际总分与历史市场总分相等，结果为走盘。",
        }
    return {
        "model_direction": model_direction,
        "actual_result": actual_result,
        "total_direction_hit": model_direction == actual_result,
        "status": "graded",
        "reason": None,
    }


def make_audit_package(detail, analysis, hashes_before, hashes_after):
    actual_margin = detail["home_points"] - detail["away_points"]
    actual_total = detail["home_points"] + detail["away_points"]
    if detail["home_points"] > detail["away_points"]:
        winner = detail["home_team_tricode"]
    elif detail["home_points"] < detail["away_points"]:
        winner = detail["away_team_tricode"]
    else:
        winner = "tie"

    market_margin = analysis["market_expected_home_margin"]
    market_total = analysis["market_total"]
    predicted_margin = analysis["predicted_margin"]
    predicted_total = analysis["predicted_total"]

    return {
        "schema_version": "1.0",
        "game": {
            "game_id": detail["game_id"],
            "game_date": detail["game_date"],
            "season": detail["season"],
            "season_type": detail["season_type"],
            "away": {
                "team_id": detail["away_team_id"],
                "tricode": detail["away_team_tricode"],
                "name": detail["away_team_name"],
                "points": detail["away_points"],
            },
            "home": {
                "team_id": detail["home_team_id"],
                "tricode": detail["home_team_tricode"],
                "name": detail["home_team_name"],
                "points": detail["home_points"],
            },
            "actual_home_margin": actual_margin,
            "actual_total": actual_total,
            "actual_winner": winner,
        },
        "historical_prediction": {
            "prediction_record_type": "historical_backtest",
            "live_pregame_snapshot": False,
            "evaluation_season": analysis["evaluation_season"],
            "home_win_probability": analysis["win_probability"],
            "predicted_home_win": bool(analysis["predicted_home_win"]),
            "predicted_home_margin": predicted_margin,
            "predicted_total": predicted_total,
            "model_scope": analysis["model_scope"],
            "prediction_time_status": analysis["prediction_time_status"],
        },
        "historical_market_benchmark": {
            "market_record_available": analysis["market_game_id"] is not None,
            "market_source_label": "historical_market_benchmark",
            "official_china_sports_lottery_verified": False,
            "eligible_for_public_china_sports_lottery_label": False,
            "market_expected_home_margin": market_margin,
            "market_total": market_total,
            "line_capture_time_status": analysis["line_capture_time_status"],
            "line_identity_status": analysis["line_identity_status"],
        },
        "model_market_disagreement": {
            "home_margin": (
                None if market_margin is None else predicted_margin - market_margin
            ),
            "total": None if market_total is None else predicted_total - market_total,
            "interpretation": "数值仅表示模型预测与历史市场基准的差值，不表示置信度。",
        },
        "direction_evaluation": {
            "spread": grade_spread(predicted_margin, market_margin, actual_margin),
            "total": grade_total(predicted_total, market_total, actual_total),
        },
        "calculation_definitions": {
            "home_margin_convention": "主队得分减客队得分。",
            "spread_disagreement": "predicted_home_margin - market_expected_home_margin",
            "total_disagreement": "predicted_total - market_total",
            "spread_direction": "正差值为主队方向，负差值为客队方向，零为无分歧。",
            "total_direction": "正差值为大分方向，负差值为小分方向，零为无分歧。",
            "push": "实际主队分差或实际总分与相应历史市场基准相等。",
        },
        "limitations": [
            "这是历史回测预测，缺少真实赛前持久化时间戳。",
            "当前历史市场基准缺少可靠采集时间。",
            "当前历史市场基准无法确认属于初盘或终盘。",
            "当前历史市场数据未经中国体育彩票官网来源核验，不能使用中国体彩官方数据标签。",
            "当前结果只能用于开发和历史分析，不能作为真实赛前发布记录。",
            "缺少可靠市场基准时保留模型预测与实际结果，市场方向和命中结果为空，并记录原因。",
            "本文件不包含收益、投注、推荐或连续表现类结论。",
        ],
        "evidence": {
            "analysis_database": {
                "path": "analysis_v1/data/courtvision_analysis.sqlite",
                "access_mode": "SQLite URI mode=ro with PRAGMA query_only=ON",
                "sha256_before": hashes_before["analysis"],
                "sha256_after": hashes_after["analysis"],
                "unchanged": hashes_before["analysis"] == hashes_after["analysis"],
            },
            "detail_database": {
                "path": "v2/db/courtvision_v2.sqlite",
                "access_mode": "SQLite URI mode=ro with PRAGMA query_only=ON",
                "sha256_before": hashes_before["detail"],
                "sha256_after": hashes_after["detail"],
                "unchanged": hashes_before["detail"] == hashes_after["detail"],
            },
        },
    }


def format_number(value, digits=4):
    if value is None:
        return "空值"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def direction_label(value):
    labels = {
        None: "无法判断",
        "home": "主队方向",
        "away": "客队方向",
        "over": "大分方向",
        "under": "小分方向",
        "no_disagreement": "无分歧",
        "push": "走盘",
    }
    return labels.get(value, str(value))


def hit_label(value):
    if value is None:
        return "不适用"
    return "命中" if value else "未命中"


def build_markdown(facts):
    game = facts["game"]
    prediction = facts["historical_prediction"]
    market = facts["historical_market_benchmark"]
    disagreement = facts["model_market_disagreement"]
    spread = facts["direction_evaluation"]["spread"]
    total = facts["direction_evaluation"]["total"]
    market_margin = market["market_expected_home_margin"]
    market_total = market["market_total"]

    lines = [
        "# CourtVision 单场历史预测验收事实",
        "",
        f"比赛：`{game['game_id']}`，{game['game_date']}，{game['away']['name']}客场对阵{game['home']['name']}。",
        "",
        "本文件由只读数据库查询和确定性 Python 公式生成。记录性质为历史回测，不能作为真实赛前发布凭证。",
        "",
        "## 1. 比赛信息",
        "",
        "| 字段 | 数值 |",
        "|---|---:|",
        f"| 最终比分 | {game['away']['tricode']} {game['away']['points']}，{game['home']['tricode']} {game['home']['points']} |",
        f"| 实际主队分差 | {game['actual_home_margin']} |",
        f"| 实际总分 | {game['actual_total']} |",
        f"| 实际胜方 | {game['actual_winner']} |",
        "",
        "## 2. 历史回测预测",
        "",
        "| 字段 | 数值 |",
        "|---|---:|",
        f"| 主队获胜概率 | {format_number(prediction['home_win_probability'], 6)} |",
        f"| 预测主队分差 | {format_number(prediction['predicted_home_margin'])} |",
        f"| 预测总分 | {format_number(prediction['predicted_total'])} |",
        f"| 模型范围 | `{prediction['model_scope']}` |",
        f"| 预测时间状态 | `{prediction['prediction_time_status']}` |",
        f"| 记录类型 | `{prediction['prediction_record_type']}` |",
        f"| 真实赛前快照 | {format_number(prediction['live_pregame_snapshot'])} |",
        "",
        "## 3. 历史市场基准",
        "",
        "| 字段 | 数值 |",
        "|---|---:|",
        f"| 历史市场预期主队分差 | {format_number(market_margin)} |",
        f"| 历史市场总分 | {format_number(market_total)} |",
        f"| 盘口采集时间状态 | `{market['line_capture_time_status']}` |",
        f"| 初盘或终盘身份 | `{market['line_identity_status']}` |",
        f"| 市场来源标签 | `{market['market_source_label']}` |",
        f"| 中国体彩官方来源已核验 | {format_number(market['official_china_sports_lottery_verified'])} |",
        "",
        "## 4. 模型与市场分歧及方向验收",
        "",
        "### 让分方向",
        "",
        f"模型与市场分歧 = {format_number(prediction['predicted_home_margin'])} - {format_number(market_margin)} = {format_number(disagreement['home_margin'])}。模型方向为{direction_label(spread['model_direction'])}，实际结果为{direction_label(spread['actual_result'])}，验收结果为{hit_label(spread['spread_direction_hit'])}。",
        "",
        "### 大小分方向",
        "",
        f"模型与市场分歧 = {format_number(prediction['predicted_total'])} - {format_number(market_total)} = {format_number(disagreement['total'])}。模型方向为{direction_label(total['model_direction'])}，实际结果为{direction_label(total['actual_result'])}，验收结果为{hit_label(total['total_direction_hit'])}。",
        "",
        "以上差值只表示模型与历史市场基准的数值差异，不能直接解释为置信度。",
        "",
        "## 5. 数据边界",
        "",
    ]
    for limitation in facts["limitations"]:
        lines.append(f"- {limitation}")
    lines.extend(
        [
            "",
            "## 6. 只读证据",
            "",
            "| 数据库 | 访问方式 | 生成前 SHA256 | 生成后 SHA256 | 未变化 |",
            "|---|---|---|---|---:|",
        ]
    )
    for database in ("analysis_database", "detail_database"):
        evidence = facts["evidence"][database]
        lines.append(
            f"| `{evidence['path']}` | `{evidence['access_mode']}` | `{evidence['sha256_before']}` | `{evidence['sha256_after']}` | {format_number(evidence['unchanged'])} |"
        )
    return "\n".join(lines) + "\n"


def write_temp_text(target_path, content):
    target_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target_path.parent,
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def restore_previous(path, previous_bytes):
    if previous_bytes is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(previous_bytes)


def atomic_write_pair(json_path, json_text, markdown_path, markdown_text):
    previous_json = json_path.read_bytes() if json_path.exists() else None
    previous_markdown = markdown_path.read_bytes() if markdown_path.exists() else None
    json_temp = write_temp_text(json_path, json_text)
    markdown_temp = None
    try:
        markdown_temp = write_temp_text(markdown_path, markdown_text)
        os.replace(json_temp, json_path)
        os.replace(markdown_temp, markdown_path)
    except Exception:
        restore_previous(json_path, previous_json)
        restore_previous(markdown_path, previous_markdown)
        raise
    finally:
        json_temp.unlink(missing_ok=True)
        if markdown_temp is not None:
            markdown_temp.unlink(missing_ok=True)


def generate_prediction_audit(game_id):
    hashes_before = {
        "analysis": sha256_file(ANALYSIS_DATABASE_PATH),
        "detail": sha256_file(DETAIL_DATABASE_PATH),
    }
    detail = load_detail_game(game_id)
    analysis = load_analysis_game(game_id)
    validate_cross_database_identity(detail, analysis)
    hashes_after = {
        "analysis": sha256_file(ANALYSIS_DATABASE_PATH),
        "detail": sha256_file(DETAIL_DATABASE_PATH),
    }
    if hashes_before != hashes_after:
        raise PredictionAuditError("数据库 SHA256 在只读查询期间发生变化，已停止生成。")

    facts = make_audit_package(detail, analysis, hashes_before, hashes_after)
    json_text = json.dumps(
        facts,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"
    markdown_text = build_markdown(facts)
    json_path = OUTPUTS_DIR / f"game_{game_id}_prediction_audit.json"
    markdown_path = OUTPUTS_DIR / f"game_{game_id}_prediction_audit.md"
    atomic_write_pair(json_path, json_text, markdown_path, markdown_text)
    return json_path, markdown_path, facts


def main(argv=None):
    args = parse_args(argv)
    try:
        json_path, markdown_path, _ = generate_prediction_audit(args.game_id)
    except (PredictionAuditError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(f"已生成预测验收 JSON：{json_path.relative_to(PROJECT_DIR)}")
    print(f"已生成预测验收说明：{markdown_path.relative_to(PROJECT_DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
