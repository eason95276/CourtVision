from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd


APP_ROOT = Path(__file__).resolve().parent
ANALYSIS_ROOT = APP_ROOT.parent
OUTPUT_ROOT = ANALYSIS_ROOT / "outputs" / "sql_results"


DATASETS = {
    "seasons": "01_season_game_counts.csv",
    "home_away": "02_team_home_away_win_rate.csv",
    "team_profiles": "03_team_scoring_profile.csv",
    "recent_form": "04_recent_rolling_form.csv",
    "phase_comparison": "05_regular_vs_playoffs.csv",
    "model_summary": "06_model_win_performance.csv",
    "margin_error": "07_margin_error.csv",
    "total_error": "08_total_error.csv",
    "worst_games": "09_worst_prediction_games.csv",
    "confidence": "10_confidence_calibration.csv",
    "stability": "11_error_trend_by_season.csv",
    "team_error": "12_error_by_team.csv",
    "market_summary": "13_model_market_same_sample.csv",
    "market_disagreement": "14_model_market_disagreement.csv",
    "quality_warnings": "15_data_quality_events.csv",
    "review_queue": "16_manual_review_priority.csv",
}


def records(path: Path) -> list[dict[str, object]]:
    frame = pd.read_csv(path)
    return json.loads(frame.to_json(orient="records", force_ascii=False))


def export_app_data() -> Path:
    payload: dict[str, object] = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "product": {
            "name": "CourtVision",
            "positioning": "面向篮球内容创作者和 NBA 深度球迷的历史数据分析与赛后复盘平台",
            "scope": "历史固定快照，本地演示",
            "database_backend": "SQLite fallback",
            "duckdb_status": "外部依赖下载受阻，DuckDB 构建脚本已保留但本轮未生成 DuckDB 文件",
        },
        "limits": [
            "当前没有真实新赛季前向验证，也没有持久化预测生成时间和特征截止时间。",
            "市场线缺少可靠采集时间戳和 opening 或 closing 身份，只能作为历史同场基准。",
            "胜率测试集没有 moneyline，无法比较模型概率与市场概率。",
            "不提供投注建议、跟单、盈利预测或模型战胜市场的证明。",
        ],
    }
    for key, filename in DATASETS.items():
        payload[key] = records(OUTPUT_ROOT / filename)

    build_report = ANALYSIS_ROOT / "outputs" / "database_build_report.json"
    if build_report.exists():
        payload["database"] = json.loads(build_report.read_text(encoding="utf-8"))
    series_path = ANALYSIS_ROOT / "research" / "lakers_rockets_2026" / "series_data.json"
    if series_path.exists():
        payload["flagship_series"] = json.loads(series_path.read_text(encoding="utf-8"))
    else:
        payload["flagship_series"] = {
            "status": "research_pending",
            "title": "湖人对火箭系列赛证据包仍在核验",
        }

    output = APP_ROOT / "static" / "app_data.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print(export_app_data())

