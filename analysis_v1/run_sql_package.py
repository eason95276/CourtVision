from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DEFAULT_SQLITE = ROOT / "data" / "courtvision_analysis.sqlite"
DEFAULT_DUCKDB = ROOT / "data" / "courtvision_analysis.duckdb"


QUERY_META = {
    "01_season_game_counts": {
        "question": "每个赛季有多少场已完成比赛，常规赛与季后赛如何分布？",
        "explanation": "先按赛季条件聚合，再连接赛季维表，检查覆盖范围和异常赛季样本量。",
        "boundary": "比赛数来自固定历史快照；疫情赛季和赛制变化会改变跨季可比性。",
        "interview": "为什么不能把赛季比赛数下降直接解释成数据缺失？",
    },
    "02_team_home_away_win_rate": {
        "question": "各队主客场胜率是否存在稳定差异？",
        "explanation": "使用条件聚合把主客场两行透视为一行，并保留两侧样本量。",
        "boundary": "这是描述性相关，包含阵容年代、赛程与联盟环境差异，不能解释主场优势的因果来源。",
        "interview": "如果限定最近三季，应把筛选条件放在聚合前还是聚合后？",
    },
    "03_team_scoring_profile": {
        "question": "各队长期场均得分、失分和净胜分如何？",
        "explanation": "在球队比赛粒度上聚合进攻、防守和胜率，验证一场两队的事实表设计。",
        "boundary": "跨 12 季平均会掩盖版本变化，适合数据结构演示，不适合当前实力排名。",
        "interview": "为什么净胜分比只看场均得分更完整，但仍不能代表阵容质量？",
    },
    "04_recent_rolling_form": {
        "question": "每队最近 5 场与 10 场滚动表现如何？",
        "explanation": "用窗口函数按比赛时间和 game_id 排序，只展示每队最新的成熟窗口。",
        "boundary": "该查询包含当前比赛结果，属于赛后滚动描述；做赛前特征时窗口必须截止到上一场。",
        "interview": "如何把窗口改成严格赛前可用，并处理同日双赛的排序？",
    },
    "05_regular_vs_playoffs": {
        "question": "常规赛与季后赛或附加赛表现有何差异？",
        "explanation": "按比赛阶段条件聚合，并要求季后赛至少 10 场，减少极小样本噪声。",
        "boundary": "对手强度、阵容年份和晋级选择偏差没有控制，不能据此断言某队更擅长季后赛。",
        "interview": "怎样加入对手强度和年代控制，减少选择偏差？",
    },
    "06_model_win_performance": {
        "question": "冻结测试季胜负模型表现如何？",
        "explanation": "连接预测、误差与比赛事实，同时报告准确率、对数损失和平均概率偏差。",
        "boundary": "只有一个冻结测试季，且没有真实前向预测时间戳。",
        "interview": "为什么准确率不能代替 Log Loss 与校准指标？",
    },
    "07_margin_error": {
        "question": "分差预测误差如何分布？",
        "explanation": "用 NTILE 将误差按样本排序分为四组，展示尾部误差而非只报一个均值。",
        "boundary": "四分位是样本内相对分桶，跨数据版本阈值会变化。",
        "interview": "生产监控更适合固定阈值还是每批次 NTILE，原因是什么？",
    },
    "08_total_error": {
        "question": "总分预测误差如何分布？",
        "explanation": "同一套分位框架用于总分误差，便于比较误差尾部和大误差占比。",
        "boundary": "没有伤病、预计分钟或实时阵容，不能把大误差归因于单一因素。",
        "interview": "如何验证节奏特征是否真的降低总分尾部误差？",
    },
    "09_worst_prediction_games": {
        "question": "哪些比赛构成最高优先级 Bad Case？",
        "explanation": "分别排名胜负、分差与总分误差，取任一维度前十，保留多任务反例。",
        "boundary": "排名只能发现应复核的比赛，不能自动生成赛后因果解释。",
        "interview": "为什么使用三个独立排名，而非把量纲不同的误差直接相加？",
    },
    "10_confidence_calibration": {
        "question": "置信度提高时实际准确率是否同步提高？",
        "explanation": "按预测一侧的置信度分桶，报告样本量、实际准确率和置信差。",
        "boundary": "这是粗分桶诊断，正式 ECE 仍以既有校准产物为准。",
        "interview": "分桶边界如何影响校准结论，等宽与等频各有什么代价？",
    },
    "11_error_trend_by_season": {
        "question": "锁定配置在滚动赛季中是否稳定？",
        "explanation": "读取已审计的滚动起点评估事实，按任务和赛季排列。",
        "boundary": "各任务训练窗口不同；这些是历史滚动评估，仍不等于真实在线前向验证。",
        "interview": "为什么每个评估赛季都必须重新限定训练截止时间？",
    },
    "12_error_by_team": {
        "question": "哪些球队参与的比赛更易出现大误差？",
        "explanation": "把每场预测展开到两支参赛球队，再按球队统计多任务误差。",
        "boundary": "一场比赛会计入两队，球队切片之间不独立；不能作为球队因果标签。",
        "interview": "如何防止同一场比赛在总体加权时被重复计算？",
    },
    "13_model_market_same_sample": {
        "question": "模型与市场在同样本中的误差差距是多少？",
        "explanation": "严格内连接到共同样本，现场计算市场绝对误差并与模型配对比较。",
        "boundary": "市场线缺少采集时间和开收盘口身份，只能称历史同场基准。",
        "interview": "为什么同样本配对比较比各自全样本均值更可信？",
    },
    "14_model_market_disagreement": {
        "question": "模型与市场分歧最大的比赛有哪些？",
        "explanation": "保留有方向的分差与总分差值，并用绝对最大分歧排名。",
        "boundary": "分歧不代表市场错误、投注价值或模型洞察，必须结合时间戳和后续证据复核。",
        "interview": "若有 opening 与 closing 两条线，数据模型应如何扩展？",
    },
    "15_data_quality_events": {
        "question": "当前仍有哪些数据质量警告？",
        "explanation": "直接查询质量事实表，只返回未通过事件及其严重度和原始说明。",
        "boundary": "质量检查通过只覆盖已编码规则，不能证明所有数据都正确。",
        "interview": "error 与 warning 的门禁策略怎样影响流水线？",
    },
    "16_manual_review_priority": {
        "question": "哪些赛后比赛应优先人工复核？",
        "explanation": "用透明加分规则综合错胜负、大分差误差、大总分误差和市场分歧。",
        "boundary": "阈值是 V1 规则，没有真实用户成本或标注数据验证，不能冒充最优优先级模型。",
        "interview": "怎样用人工复核时长和发现率重新标定阈值？",
    },
    "17_market_direction_game_grades": {
        "question": "模型相对历史市场基准选择的让分与大小分方向，逐场如何验收？",
        "explanation": "在一场一行的粒度上分别计算模型与市场 gap、实际与市场 gap，再按统一容差标记命中、未命中、走盘、无分歧或市场缺失。",
        "boundary": "历史市场基准缺少可靠采集时间且无法确认初盘或终盘；方向结果只用于冻结 holdout 诊断，不代表投注价值或真实赛前发布表现。",
        "interview": "为什么走盘与模型无分歧必须从命中率分母排除？",
    },
}


def open_database(path: Path, backend: str):
    if backend == "sqlite":
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
        )
        connection.execute("PRAGMA query_only = ON")
        return connection
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError("DuckDB backend requested but duckdb is not installed") from exc
    return duckdb.connect(str(path), read_only=True)


def query_frame(connection: Any, sql: str, backend: str) -> pd.DataFrame:
    if backend == "sqlite":
        return pd.read_sql_query(sql, connection)
    return connection.execute(sql).fetchdf()


def markdown_table(frame: pd.DataFrame, rows: int = 5) -> str:
    sample = frame.head(rows).copy().where(pd.notna(frame.head(rows)), "")
    if sample.empty:
        return "（无结果）"
    headers = [str(column) for column in sample.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in sample.itertuples(index=False, name=None):
        values = [str(value).replace("|", "\\|").replace("\n", " ") for value in row]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def run_package(database: Path, backend: str) -> dict[str, Any]:
    sql_dir = ROOT / "sql"
    result_dir = ROOT / "outputs" / "sql_results"
    result_dir.mkdir(parents=True, exist_ok=True)
    connection = open_database(database, backend)
    results: list[dict[str, Any]] = []
    catalog = [
        "# CourtVision V1 SQL 分析目录",
        "",
        f"生成时间：{datetime.now(UTC).isoformat()}",
        "",
        f"实际执行后端：`{backend}`",
        "",
        "所有查询均已真实执行。CSV 结果位于 `outputs/sql_results/`。市场字段只作为无可靠采集时间戳的历史同场基准。",
        "",
    ]
    try:
        for path in sorted(sql_dir.glob("*.sql")):
            key = path.stem
            meta = QUERY_META[key]
            sql = path.read_text(encoding="utf-8")
            frame = query_frame(connection, sql, backend)
            output = result_dir / f"{key}.csv"
            frame.to_csv(output, index=False)
            item = {
                "id": key,
                "sql_file": str(path.relative_to(PROJECT_ROOT)),
                "output_file": str(output.relative_to(PROJECT_ROOT)),
                "rows": len(frame),
                "columns": list(frame.columns),
                "sample": frame.head(3).where(pd.notna(frame.head(3)), None).to_dict("records"),
            }
            results.append(item)
            catalog.extend(
                [
                    f"## {key}  {meta['question']}",
                    "",
                    f"SQL：`{path.relative_to(PROJECT_ROOT)}`",
                    "",
                    f"中文解释：{meta['explanation']}",
                    "",
                    f"输出字段：{', '.join(frame.columns)}",
                    "",
                    f"实际结果：{len(frame)} 行。",
                    "",
                    markdown_table(frame),
                    "",
                    f"适用边界：{meta['boundary']}",
                    "",
                    f"面试追问：{meta['interview']}",
                    "",
                ]
            )
    finally:
        connection.close()
    manifest = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "backend": backend,
        "database": str(database.relative_to(PROJECT_ROOT)),
        "query_count": len(results),
        "passed": len(results),
        "failed": 0,
        "queries": results,
    }
    manifest_path = ROOT / "outputs" / "sql_run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "SQL_QUERY_CATALOG.md").write_text("\n".join(catalog), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run all CourtVision V1 SQL queries")
    parser.add_argument("--backend", choices=("sqlite", "duckdb"), default="sqlite")
    parser.add_argument("--database", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database = args.database or (DEFAULT_SQLITE if args.backend == "sqlite" else DEFAULT_DUCKDB)
    result = run_package(database.resolve(), args.backend)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
