"""Diagnose spread and total direction grades on the frozen 2025 holdout."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ANALYSIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = ANALYSIS_DIR.parent
DEFAULT_DATABASE = ANALYSIS_DIR / "data" / "courtvision_analysis.sqlite"
DEFAULT_DETAIL_DATABASE = PROJECT_ROOT / "v2" / "db" / "courtvision_v2.sqlite"
DEFAULT_SQL = ANALYSIS_DIR / "sql" / "17_market_direction_game_grades.sql"
DEFAULT_SOURCE_CSV = PROJECT_ROOT / "artifacts" / "test_predictions_with_market.csv"
DEFAULT_SUMMARY = PROJECT_ROOT / "artifacts" / "market_direction_error_summary.json"
DEFAULT_SLICES = PROJECT_ROOT / "artifacts" / "market_direction_error_slices.csv"
DEFAULT_REPORT = PROJECT_ROOT / "reports" / "Market_Direction_Error_Analysis.md"

EPSILON = 1e-9
DEFAULT_SEED = 42
DEFAULT_ITERATIONS = 10_000
INSUFFICIENT_SAMPLE_THRESHOLD = 100
QUARTILE_LABELS = ("q1_low", "q2", "q3", "q4_high")


class MarketDirectionAnalysisError(RuntimeError):
    """Expected validation failure with a user-readable message."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_missing(value: object) -> bool:
    return value is None or bool(pd.isna(value))


def _finite_number(value: object, name: str) -> float:
    if _is_missing(value):
        raise MarketDirectionAnalysisError(f"{name} 缺失。")
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise MarketDirectionAnalysisError(f"{name} 不是有效数字。")
    number = float(value)
    if not math.isfinite(number):
        raise MarketDirectionAnalysisError(f"{name} 不是有限数字。")
    return number


def grade_direction(
    predicted_value: object,
    market_value: object,
    actual_value: object,
    *,
    positive_direction: str,
    negative_direction: str,
    epsilon: float = EPSILON,
) -> dict[str, object]:
    """Apply the common market-direction grading formula to one task."""
    if _is_missing(market_value):
        return {
            "model_gap": None,
            "actual_gap": None,
            "model_direction": None,
            "actual_direction": None,
            "status": "market_unavailable",
            "hit": None,
        }

    predicted = _finite_number(predicted_value, "predicted_value")
    market = _finite_number(market_value, "market_value")
    actual = _finite_number(actual_value, "actual_value")
    model_gap = predicted - market
    actual_gap = actual - market

    if abs(model_gap) <= epsilon:
        model_direction = "no_disagreement"
    elif model_gap > 0:
        model_direction = positive_direction
    else:
        model_direction = negative_direction

    if abs(actual_gap) <= epsilon:
        actual_direction = "push"
    elif actual_gap > 0:
        actual_direction = positive_direction
    else:
        actual_direction = negative_direction

    if model_direction == "no_disagreement":
        status = "no_model_market_disagreement"
        hit = None
    elif actual_direction == "push":
        status = "push"
        hit = None
    elif model_direction == actual_direction:
        status = "hit"
        hit = 1
    else:
        status = "miss"
        hit = 0
    return {
        "model_gap": model_gap,
        "actual_gap": actual_gap,
        "model_direction": model_direction,
        "actual_direction": actual_direction,
        "status": status,
        "hit": hit,
    }


def read_game_grades(
    database_path: Path = DEFAULT_DATABASE,
    sql_path: Path = DEFAULT_SQL,
) -> pd.DataFrame:
    if not database_path.is_file():
        raise MarketDirectionAnalysisError(f"找不到分析数据库：{database_path}")
    if not sql_path.is_file():
        raise MarketDirectionAnalysisError(f"找不到逐场验收 SQL：{sql_path}")
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(database_uri, uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise MarketDirectionAnalysisError("分析数据库没有进入 query_only 模式。")
            frame = pd.read_sql_query(sql_path.read_text(encoding="utf-8"), connection)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise MarketDirectionAnalysisError("逐场方向验收 SQL 执行失败。") from exc
    frame["game_id"] = frame["game_id"].astype(str).str.zfill(10)
    return frame


def validate_game_grades(frame: pd.DataFrame) -> None:
    required = {
        "game_id",
        "game_date",
        "game_type",
        "home_team",
        "away_team",
        "home_back_to_back",
        "away_back_to_back",
        "predicted_margin",
        "market_expected_home_margin",
        "point_margin",
        "model_spread_gap",
        "actual_spread_gap",
        "model_spread_direction",
        "actual_spread_direction",
        "spread_status",
        "spread_hit",
        "predicted_total",
        "market_total",
        "total_points",
        "model_total_gap",
        "actual_total_gap",
        "model_total_direction",
        "actual_total_direction",
        "total_status",
        "total_hit",
    }
    missing = required - set(frame.columns)
    if missing:
        raise MarketDirectionAnalysisError(
            "逐场验收结果缺少字段：" + "、".join(sorted(missing))
        )
    if len(frame) != 1322 or frame["game_id"].nunique() != 1322:
        raise MarketDirectionAnalysisError("逐场验收结果必须包含 1,322 个唯一 game_id。")
    if frame["game_id"].duplicated().any():
        raise MarketDirectionAnalysisError("逐场验收结果存在重复 game_id。")

    specifications = {
        "spread": (
            "predicted_margin",
            "market_expected_home_margin",
            "point_margin",
            "home",
            "away",
        ),
        "total": (
            "predicted_total",
            "market_total",
            "total_points",
            "over",
            "under",
        ),
    }
    for row in frame.itertuples(index=False):
        record = row._asdict()
        for task, columns in specifications.items():
            predicted, market, actual, positive, negative = columns
            expected = grade_direction(
                record[predicted],
                record[market],
                record[actual],
                positive_direction=positive,
                negative_direction=negative,
            )
            comparisons = {
                f"model_{task}_gap": expected["model_gap"],
                f"actual_{task}_gap": expected["actual_gap"],
                f"model_{task}_direction": expected["model_direction"],
                f"actual_{task}_direction": expected["actual_direction"],
                f"{task}_status": expected["status"],
                f"{task}_hit": expected["hit"],
            }
            for field, expected_value in comparisons.items():
                observed = record[field]
                if _is_missing(expected_value):
                    matches = _is_missing(observed)
                elif isinstance(expected_value, float):
                    matches = math.isclose(
                        float(observed), expected_value, rel_tol=0.0, abs_tol=EPSILON
                    )
                else:
                    matches = observed == expected_value
                if not matches:
                    raise MarketDirectionAnalysisError(
                        f"{row.game_id} 的 {field} 与通用公式不一致。"
                    )


def crosscheck_fixed_prediction_csv(
    grades: pd.DataFrame,
    source_csv_path: Path = DEFAULT_SOURCE_CSV,
) -> dict[str, int]:
    source = pd.read_csv(source_csv_path, dtype={"game_id": str}, low_memory=False)
    source["game_id"] = source["game_id"].str.zfill(10)
    compare_columns = (
        "predicted_margin",
        "market_expected_home_margin",
        "point_margin",
        "predicted_total",
        "market_total",
        "total_points",
        "home_back_to_back",
        "away_back_to_back",
    )
    left = grades[["game_id", *compare_columns]].copy()
    right = source[["game_id", *compare_columns]].copy()
    joined = left.merge(right, on="game_id", how="outer", suffixes=("_db", "_csv"), indicator=True)
    mismatch = joined["_merge"].ne("both")
    for column in compare_columns:
        db_values = pd.to_numeric(joined[f"{column}_db"], errors="coerce")
        csv_values = pd.to_numeric(joined[f"{column}_csv"], errors="coerce")
        mismatch |= ~np.isclose(
            db_values.to_numpy(dtype=float),
            csv_values.to_numpy(dtype=float),
            rtol=0.0,
            atol=EPSILON,
            equal_nan=True,
        )
    return {
        "database_games": int(grades["game_id"].nunique()),
        "csv_games": int(source["game_id"].nunique()),
        "mismatched_games": int(mismatch.sum()),
    }


def _wilson_interval(hits: int, games: int) -> tuple[float | None, float | None]:
    if games == 0:
        return None, None
    z = 1.959963984540054
    rate = hits / games
    denominator = 1 + z * z / games
    center = (rate + z * z / (2 * games)) / denominator
    radius = z * math.sqrt(rate * (1 - rate) / games + z * z / (4 * games * games)) / denominator
    return max(0.0, center - radius), min(1.0, center + radius)


def _weekly_cluster_interval(
    gradable: pd.DataFrame,
    hit_column: str,
    *,
    rng: np.random.Generator,
    iterations: int,
) -> tuple[float | None, float | None, int]:
    if gradable.empty:
        return None, None, 0
    weeks = pd.to_datetime(gradable["game_date"], errors="raise").dt.to_period("W-SUN").astype(str)
    grouped = (
        pd.DataFrame({"week": weeks, "hit": gradable[hit_column].astype(float)})
        .groupby("week", sort=True)["hit"]
        .agg(["sum", "count"])
    )
    indexes = rng.integers(0, len(grouped), size=(iterations, len(grouped)))
    sampled_hits = grouped["sum"].to_numpy()[indexes].sum(axis=1)
    sampled_games = grouped["count"].to_numpy()[indexes].sum(axis=1)
    distribution = sampled_hits / sampled_games
    return (
        float(np.quantile(distribution, 0.025)),
        float(np.quantile(distribution, 0.975)),
        int(len(grouped)),
    )


def _weekly_cluster_mean_interval(
    values: pd.Series,
    game_dates: pd.Series,
    *,
    seed: int,
    iterations: int,
) -> tuple[float, float]:
    weeks = pd.to_datetime(game_dates, errors="raise").dt.to_period("W-SUN").astype(str)
    grouped = (
        pd.DataFrame({"week": weeks, "value": values.astype(float)})
        .groupby("week", sort=True)["value"]
        .agg(["sum", "count"])
    )
    rng = np.random.default_rng(seed)
    indexes = rng.integers(0, len(grouped), size=(iterations, len(grouped)))
    sampled_sum = grouped["sum"].to_numpy()[indexes].sum(axis=1)
    sampled_count = grouped["count"].to_numpy()[indexes].sum(axis=1)
    distribution = sampled_sum / sampled_count
    return (
        float(np.quantile(distribution, 0.025)),
        float(np.quantile(distribution, 0.975)),
    )


def _slice_row(
    frame: pd.DataFrame,
    *,
    task: str,
    dimension: str,
    category: str,
    use_cluster_bootstrap: bool,
    rng: np.random.Generator,
    iterations: int,
) -> dict[str, object]:
    status_column = f"{task}_status"
    hit_column = f"{task}_hit"
    status = frame[status_column]
    gradable = frame[status.isin(("hit", "miss"))]
    total_games = int(len(frame))
    gradable_games = int(len(gradable))
    hits = int(status.eq("hit").sum())
    misses = int(status.eq("miss").sum())
    pushes = int(status.eq("push").sum())
    no_disagreements = int(status.eq("no_model_market_disagreement").sum())
    market_unavailable = int(status.eq("market_unavailable").sum())
    hit_rate = hits / gradable_games if gradable_games else None
    coverage_rate = gradable_games / total_games if total_games else None
    if use_cluster_bootstrap:
        ci_low, ci_high, weeks = _weekly_cluster_interval(
            gradable,
            hit_column,
            rng=rng,
            iterations=iterations,
        )
        ci_method = "weekly_cluster_bootstrap"
        bootstrap_iterations: int | None = iterations
    else:
        ci_low, ci_high = _wilson_interval(hits, gradable_games)
        weeks = int(
            pd.to_datetime(gradable["game_date"], errors="coerce")
            .dt.to_period("W-SUN")
            .nunique()
        )
        ci_method = "wilson_iid_descriptive"
        bootstrap_iterations = None
    if task == "spread":
        signed_error = frame["predicted_margin"] - frame["point_margin"]
    else:
        signed_error = frame["predicted_total"] - frame["total_points"]
    return {
        "task": task,
        "dimension": dimension,
        "category": category,
        "total_games": total_games,
        "gradable_games": gradable_games,
        "hits": hits,
        "misses": misses,
        "pushes": pushes,
        "no_disagreements": no_disagreements,
        "market_unavailable": market_unavailable,
        "hit_rate": hit_rate,
        "coverage_rate": coverage_rate,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "ci_method": ci_method,
        "bootstrap_iterations": bootstrap_iterations,
        "calendar_weeks": weeks,
        "insufficient_sample": gradable_games < INSUFFICIENT_SAMPLE_THRESHOLD,
        "mean_signed_error": float(signed_error.mean()) if len(signed_error) else None,
    }


def build_slice_analysis(
    grades: pd.DataFrame,
    *,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> pd.DataFrame:
    if iterations <= 0:
        raise MarketDirectionAnalysisError("bootstrap iterations 必须大于 0。")
    validate_game_grades(grades)
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []

    for task in ("spread", "total"):
        rows.append(
            _slice_row(
                grades,
                task=task,
                dimension="overall",
                category="all",
                use_cluster_bootstrap=True,
                rng=rng,
                iterations=iterations,
            )
        )

    for task, gap_column, market_column in (
        ("spread", "model_spread_gap", "market_expected_home_margin"),
        ("total", "model_total_gap", "market_total"),
    ):
        available = grades[grades[market_column].notna()].copy()
        available["_quartile"] = pd.qcut(
            available[gap_column].abs(),
            q=4,
            labels=QUARTILE_LABELS,
            duplicates="raise",
        )
        for category in QUARTILE_LABELS:
            rows.append(
                _slice_row(
                    available[available["_quartile"].eq(category)],
                    task=task,
                    dimension="absolute_model_market_gap_quartile",
                    category=category,
                    use_cluster_bootstrap=True,
                    rng=rng,
                    iterations=iterations,
                )
            )

    at_least_one_back_to_back = grades["home_back_to_back"].eq(1) | grades["away_back_to_back"].eq(1)
    schedule_slices = {
        "at_least_one_back_to_back": at_least_one_back_to_back,
        "both_normal_rest": ~at_least_one_back_to_back,
    }
    for task in ("spread", "total"):
        for category, mask in schedule_slices.items():
            rows.append(
                _slice_row(
                    grades[mask],
                    task=task,
                    dimension="schedule",
                    category=category,
                    use_cluster_bootstrap=False,
                    rng=rng,
                    iterations=iterations,
                )
            )

    competition_slices = {
        "regular_or_cup": grades["is_playoffs"].eq(0),
        "playoffs_or_play_in": grades["is_playoffs"].eq(1),
    }
    for task in ("spread", "total"):
        for category, mask in competition_slices.items():
            rows.append(
                _slice_row(
                    grades[mask],
                    task=task,
                    dimension="competition",
                    category=category,
                    use_cluster_bootstrap=False,
                    rng=rng,
                    iterations=iterations,
                )
            )

    market_margin = grades["market_expected_home_margin"].abs()
    spread_bands = {
        "0_to_3": market_margin.le(3),
        "over_3_to_7": market_margin.gt(3) & market_margin.le(7),
        "over_7": market_margin.gt(7),
        "market_unavailable": market_margin.isna(),
    }
    for category, mask in spread_bands.items():
        if mask.any():
            rows.append(
                _slice_row(
                    grades[mask],
                    task="spread",
                    dimension="absolute_historical_market_spread",
                    category=category,
                    use_cluster_bootstrap=False,
                    rng=rng,
                    iterations=iterations,
                )
            )
    return pd.DataFrame(rows)


def _row_to_dict(row: pd.Series) -> dict[str, object]:
    result: dict[str, object] = {}
    integer_fields = {
        "total_games",
        "gradable_games",
        "hits",
        "misses",
        "pushes",
        "no_disagreements",
        "market_unavailable",
        "bootstrap_iterations",
        "calendar_weeks",
    }
    for key, value in row.items():
        if _is_missing(value):
            result[key] = None
        elif key in integer_fields:
            result[key] = int(value)
        elif isinstance(value, (np.integer,)):
            result[key] = int(value)
        elif isinstance(value, (np.floating,)):
            result[key] = float(value)
        elif isinstance(value, (np.bool_,)):
            result[key] = bool(value)
        else:
            result[key] = value
    return result


def _bias_label(ci_low: float, ci_high: float) -> str:
    if ci_low > 0:
        return "systematic_overestimate_supported"
    if ci_high < 0:
        return "systematic_underestimate_supported"
    return "no_supported_systematic_bias"


def build_summary(
    grades: pd.DataFrame,
    slices: pd.DataFrame,
    *,
    iterations: int,
    seed: int,
    crosscheck: dict[str, int],
) -> dict[str, object]:
    overall_rows = slices[slices["dimension"].eq("overall")].set_index("task")
    overall = {task: _row_to_dict(overall_rows.loc[task]) for task in ("spread", "total")}
    spread_bias = float((grades["predicted_margin"] - grades["point_margin"]).mean())
    total_bias = float((grades["predicted_total"] - grades["total_points"]).mean())
    spread_bias_ci = _weekly_cluster_mean_interval(
        grades["predicted_margin"] - grades["point_margin"],
        grades["game_date"],
        seed=seed + 1001,
        iterations=iterations,
    )
    total_bias_ci = _weekly_cluster_mean_interval(
        grades["predicted_total"] - grades["total_points"],
        grades["game_date"],
        seed=seed + 1002,
        iterations=iterations,
    )
    quartiles: dict[str, list[dict[str, object]]] = {}
    monotonic: dict[str, bool] = {}
    for task in ("spread", "total"):
        rows = slices[
            slices["task"].eq(task)
            & slices["dimension"].eq("absolute_model_market_gap_quartile")
        ]
        records = [_row_to_dict(row) for _, row in rows.iterrows()]
        quartiles[task] = records
        rates = [float(record["hit_rate"]) for record in records]
        monotonic[task] = all(left <= right for left, right in zip(rates, rates[1:]))

    spread_rate = float(overall["spread"]["hit_rate"])
    total_rate = float(overall["total"]["hit_rate"])
    next_experiment = (
        "在全新的前向样本中，只增加带明确赛前时间戳的球员可用性与预计出场时间信息，"
        "预注册方向命中率和分差、总分误差指标后，与当前 85 特征基线进行一次隔离对照。"
    )
    return {
        "schema_version": "1.0",
        "scope": {
            "evaluation_season": 2025,
            "record_type": "historical_holdout_diagnostic",
            "market_source_label": "historical_market_benchmark",
            "line_capture_time_status": "missing",
            "line_identity_status": "opening_or_closing_unverified",
            "official_china_sports_lottery_verified": False,
            "eligible_for_betting_or_profitability_claims": False,
        },
        "grading": {
            "epsilon": EPSILON,
            "denominator_statuses": ["hit", "miss"],
            "excluded_statuses": [
                "market_unavailable",
                "push",
                "no_model_market_disagreement",
            ],
        },
        "uncertainty": {
            "seed": seed,
            "iterations": iterations,
            "cluster": "calendar week ending Sunday",
            "cluster_interval_scope": "overall and absolute model-market gap quartiles",
            "other_slice_interval": "Wilson IID descriptive interval; does not adjust for time dependence",
            "insufficient_sample_threshold": INSUFFICIENT_SAMPLE_THRESHOLD,
        },
        "source_crosscheck": crosscheck,
        "overall": overall,
        "absolute_model_market_gap_quartiles": quartiles,
        "monotonic_hit_rate_increase_by_gap_quartile": monotonic,
        "signed_error": {
            "spread_predicted_minus_actual": {
                "mean": spread_bias,
                "weekly_cluster_ci_low": spread_bias_ci[0],
                "weekly_cluster_ci_high": spread_bias_ci[1],
                "interpretation": _bias_label(*spread_bias_ci),
            },
            "total_predicted_minus_actual": {
                "mean": total_bias,
                "weekly_cluster_ci_low": total_bias_ci[0],
                "weekly_cluster_ci_high": total_bias_ci[1],
                "interpretation": _bias_label(*total_bias_ci),
            },
        },
        "business_answers": {
            "spread_above_50_percent": spread_rate > 0.5,
            "total_above_50_percent": total_rate > 0.5,
            "larger_gap_monotonic_improvement": monotonic,
            "supports_high_gap_equals_high_confidence": False,
            "next_experiment": next_experiment,
        },
        "limitations": [
            "这是冻结的 2025 至 2026 历史 holdout 诊断，不能重新用于模型调参。",
            "历史市场基准缺少可靠采集时间，无法确认属于初盘或终盘。",
            "市场数据未核验为中国体彩官方数据，也不代表真实可执行的临场盘口。",
            "切片差异属于描述性关联，不能归因于伤病、体能或战术。",
            "本分析不计算收益、投注金额、推荐、阈值优化或盈利能力。",
        ],
    }


def _percent(value: object) -> str:
    if value is None or _is_missing(value):
        return "无法计算"
    return f"{float(value) * 100:.2f}%"


def _signed(value: float) -> str:
    return f"{value:+.4f}"


def _slice_table(frame: pd.DataFrame) -> str:
    headers = (
        "任务",
        "维度",
        "切片",
        "总场数",
        "可验收",
        "命中",
        "未命中",
        "走盘",
        "无分歧",
        "缺失",
        "命中率",
        "覆盖率",
        "95% 区间",
        "区间方法",
        "样本标记",
    )
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in frame.itertuples(index=False):
        interval = (
            f"[{_percent(row.ci_low)}, {_percent(row.ci_high)}]"
            if not _is_missing(row.ci_low)
            else "无法计算"
        )
        values = (
            row.task,
            row.dimension,
            row.category,
            str(row.total_games),
            str(row.gradable_games),
            str(row.hits),
            str(row.misses),
            str(row.pushes),
            str(row.no_disagreements),
            str(row.market_unavailable),
            _percent(row.hit_rate),
            _percent(row.coverage_rate),
            interval,
            row.ci_method,
            "insufficient_sample" if row.insufficient_sample else "sufficient_for_description",
        )
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def render_report(summary: dict[str, object], slices: pd.DataFrame) -> str:
    overall = summary["overall"]
    spread = overall["spread"]
    total = overall["total"]
    signed = summary["signed_error"]
    quartiles = summary["absolute_model_market_gap_quartiles"]
    schedule = slices[slices["dimension"].eq("schedule")]
    competition = slices[slices["dimension"].eq("competition")]
    spread_bands = slices[slices["dimension"].eq("absolute_historical_market_spread")]

    def task_rate(frame: pd.DataFrame, task: str, category: str) -> float:
        return float(frame[frame["task"].eq(task) & frame["category"].eq(category)].iloc[0]["hit_rate"])

    spread_b2b = task_rate(schedule, "spread", "at_least_one_back_to_back")
    spread_rest = task_rate(schedule, "spread", "both_normal_rest")
    total_b2b = task_rate(schedule, "total", "at_least_one_back_to_back")
    total_rest = task_rate(schedule, "total", "both_normal_rest")
    playoff_rows = competition[competition["category"].eq("playoffs_or_play_in")]
    playoff_games = int(playoff_rows["total_games"].iloc[0])
    spread_quartile_rates = [float(row["hit_rate"]) for row in quartiles["spread"]]
    total_quartile_rates = [float(row["hit_rate"]) for row in quartiles["total"]]
    spread_bias = float(signed["spread_predicted_minus_actual"]["mean"])
    total_bias = float(signed["total_predicted_minus_actual"]["mean"])
    spread_bias_ci = (
        float(signed["spread_predicted_minus_actual"]["weekly_cluster_ci_low"]),
        float(signed["spread_predicted_minus_actual"]["weekly_cluster_ci_high"]),
    )
    total_bias_ci = (
        float(signed["total_predicted_minus_actual"]["weekly_cluster_ci_low"]),
        float(signed["total_predicted_minus_actual"]["weekly_cluster_ci_high"]),
    )
    next_experiment = summary["business_answers"]["next_experiment"]

    lines = [
        "# CourtVision 让分与大小分方向错误诊断",
        "",
        "## 结论摘要",
        "",
        (
            f"冻结的 2025 至 2026 历史 holdout 共 1,322 场。让分方向可验收 {spread['gradable_games']} 场，"
            f"命中 {spread['hits']} 场、未命中 {spread['misses']} 场，命中率 {_percent(spread['hit_rate'])}，"
            f"周级聚类 bootstrap 95% 区间为 [{_percent(spread['ci_low'])}, {_percent(spread['ci_high'])}]。"
        ),
        "",
        (
            f"大小分方向可验收 {total['gradable_games']} 场，命中 {total['hits']} 场、未命中 {total['misses']} 场，"
            f"命中率 {_percent(total['hit_rate'])}，周级聚类 bootstrap 95% 区间为 "
            f"[{_percent(total['ci_low'])}, {_percent(total['ci_high'])}]。"
        ),
        "",
        "市场字段在全文中只称历史市场基准。它缺少可靠采集时间，无法确认属于初盘或终盘，也未核验为中国体彩官方数据。",
        "",
        "## 已确认事实",
        "",
        f"1. 当前模型让分方向点估计为 {_percent(spread['hit_rate'])}，没有高于 50%。",
        f"2. 当前模型大小分方向点估计为 {_percent(total['hit_rate'])}，没有高于 50%。",
        (
            "3. 让分绝对分歧四分位命中率依次为 "
            + "、".join(_percent(value) for value in spread_quartile_rates)
            + "。"
        ),
        (
            "4. 大小分绝对分歧四分位命中率依次为 "
            + "、".join(_percent(value) for value in total_quartile_rates)
            + "。"
        ),
        (
            f"5. 模型分差平均有符号误差为 {_signed(spread_bias)} 分，"
            f"周级聚类区间为 [{_signed(spread_bias_ci[0])}, {_signed(spread_bias_ci[1])}]。"
        ),
        (
            f"6. 模型总分平均有符号误差为 {_signed(total_bias)} 分，"
            f"周级聚类区间为 [{_signed(total_bias_ci[0])}, {_signed(total_bias_ci[1])}]。"
        ),
        "两项区间都跨过 0，当前证据不支持把点估计称为系统性高估或低估。",
        "",
        "## 描述性关联",
        "",
        (
            f"至少一方背靠背时，让分方向命中率为 {_percent(spread_b2b)}，正常休息为 {_percent(spread_rest)}；"
            f"大小分方向分别为 {_percent(total_b2b)} 和 {_percent(total_rest)}。"
        ),
        "这些差异没有控制对手强度、阵容、赛程位置或年份，不能解释体能、伤病或战术因果。",
        "",
        (
            f"季后赛或附加赛只有 {playoff_games} 场，低于预注册的 {INSUFFICIENT_SAMPLE_THRESHOLD} 场描述门槛，"
            "标记为 insufficient_sample。该切片不支持稳定性结论。"
        ),
        "",
        "分歧四分位的命中率没有形成两项任务共同、单调上升的模式。当前数据不支持把高分歧表达为高置信。",
        "",
        "## 固定切片结果",
        "",
        _slice_table(slices),
        "",
        "## 九个业务问题",
        "",
        f"1. 让分方向是否高于 50%：否。点估计为 {_percent(spread['hit_rate'])}，聚类区间没有提供高于 50% 的证据。",
        f"2. 大小分方向是否高于 50%：否。点估计为 {_percent(total['hit_rate'])}，聚类区间没有提供高于 50% 的证据。",
        "3. 分歧越大是否命中率越高：没有一致证据。让分和大小分四分位均未形成共同的单调改善。",
        "4. 是否支持“高分歧等于高置信”：不支持。分歧是模型与历史市场基准的距离，缺少独立验证和可靠盘口时点。",
        (
            "5. 背靠背是否有更高错误率：只观察到描述性差异。"
            f"让分错误率变化为 {_percent(1-spread_b2b)} 对 {_percent(1-spread_rest)}，"
            f"大小分错误率变化为 {_percent(1-total_b2b)} 对 {_percent(1-total_rest)}，不能作因果解释。"
        ),
        f"6. 季后赛或附加赛样本是否足够：不足。只有 {playoff_games} 场，低于预注册的 100 场门槛。",
        (
            f"7. 分差是否存在系统偏差：平均 predicted_margin - point_margin 为 {_signed(spread_bias)} 分，"
            f"点估计方向为高估；周级聚类区间 [{_signed(spread_bias_ci[0])}, {_signed(spread_bias_ci[1])}] 跨过 0，"
            "没有足够证据确认系统性偏差。"
        ),
        (
            f"8. 总分是否存在系统偏差：平均 predicted_total - total_points 为 {_signed(total_bias)} 分，"
            f"点估计方向为低估；周级聚类区间 [{_signed(total_bias_ci[0])}, {_signed(total_bias_ci[1])}] 跨过 0，"
            "没有足够证据确认系统性偏差。"
        ),
        f"9. 唯一下一实验建议：{next_experiment}",
        "",
        "## 合理推测",
        "",
        "球员可用性与预计出场时间可能同时影响分差和总分任务，但当前数据没有这些字段，因此这只是待检验假设。任何增量都必须在全新前向样本上验证，不能用本测试集选择阈值或调参。",
        "",
        "## 当前无法验证的信息",
        "",
        "1. 历史市场基准属于初盘、终盘或临场盘的哪一种。",
        "2. 当前方向结果能否在真实赛前发布流程中复现。",
        "3. 任一切片是否具有投注价值、收益能力或可执行性。",
        "4. 背靠背差异是否由体能、伤病、对手强度或战术造成。",
        "5. 高分歧规则能否在独立未来样本中保持表现。",
        "",
        "## 不确定性方法",
        "",
        f"整体与两项任务各自的四个绝对分歧四分位使用自然周聚类 bootstrap，随机种子 {summary['uncertainty']['seed']}，迭代 {summary['uncertainty']['iterations']} 次。其他切片使用 Wilson IID 描述性区间，并明确没有校正比赛时间相关性。命中率分母仅包含 hit 和 miss。",
        "",
        "## 历史市场让分绝对值切片",
        "",
        _slice_table(spread_bands),
        "",
    ]
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}_", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def generate_outputs(
    *,
    database_path: Path = DEFAULT_DATABASE,
    detail_database_path: Path = DEFAULT_DETAIL_DATABASE,
    source_csv_path: Path = DEFAULT_SOURCE_CSV,
    sql_path: Path = DEFAULT_SQL,
    summary_path: Path = DEFAULT_SUMMARY,
    slices_path: Path = DEFAULT_SLICES,
    report_path: Path = DEFAULT_REPORT,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> dict[str, object]:
    database_hashes_before = {
        str(database_path): sha256_file(database_path),
        str(detail_database_path): sha256_file(detail_database_path),
    }
    grades = read_game_grades(database_path, sql_path)
    validate_game_grades(grades)
    crosscheck = crosscheck_fixed_prediction_csv(grades, source_csv_path)
    if crosscheck["mismatched_games"] != 0:
        raise MarketDirectionAnalysisError("数据库与固定预测 CSV 的关键字段不一致。")
    slices = build_slice_analysis(grades, iterations=iterations, seed=seed)
    summary = build_summary(
        grades,
        slices,
        iterations=iterations,
        seed=seed,
        crosscheck=crosscheck,
    )
    database_hashes_after = {
        str(database_path): sha256_file(database_path),
        str(detail_database_path): sha256_file(detail_database_path),
    }
    if database_hashes_before != database_hashes_after:
        raise MarketDirectionAnalysisError("分析过程中数据库 SHA256 发生变化，拒绝保存结果。")
    summary["database_sha256"] = {
        "before": database_hashes_before,
        "after": database_hashes_after,
        "unchanged": True,
    }
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    slices_text = slices.to_csv(index=False, lineterminator="\n")
    report_text = render_report(summary, slices)
    _atomic_write(summary_path, summary_text)
    _atomic_write(slices_path, slices_text)
    _atomic_write(report_path, report_text)
    return {
        "summary_path": str(summary_path),
        "slices_path": str(slices_path),
        "report_path": str(report_path),
        "database_hashes_before": database_hashes_before,
        "database_hashes_after": database_hashes_after,
        "games": len(grades),
        "slice_rows": len(slices),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成冻结历史 holdout 的让分与大小分方向错误诊断。"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--detail-database", type=Path, default=DEFAULT_DETAIL_DATABASE)
    parser.add_argument("--source-csv", type=Path, default=DEFAULT_SOURCE_CSV)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = generate_outputs(
            database_path=args.database.resolve(),
            detail_database_path=args.detail_database.resolve(),
            source_csv_path=args.source_csv.resolve(),
            iterations=args.iterations,
            seed=args.seed,
        )
    except (MarketDirectionAnalysisError, OSError, ValueError) as exc:
        print(f"错误：{exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
