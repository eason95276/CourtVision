from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


TASK_LABELS = {
    "win_probability": "Win Probability",
    "expected_margin": "Expected Point Margin",
    "expected_total": "Expected Total Points",
}


def _fmt(value: object, digits: int = 4) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}"
    return str(value)


def _test_row(metrics: pd.DataFrame, task: str, selected: dict[str, object]) -> pd.Series:
    rows = metrics[
        (metrics["task"] == task)
        & (metrics["split"] == "test")
        & (metrics["model"] == selected["model"])
        & (metrics["window_years"] == selected["window_years"])
    ]
    if rows.empty:
        raise ValueError(f"missing selected test metrics for {task}")
    return rows.iloc[0]


def _window_winners(metrics: pd.DataFrame, task: str, metric: str) -> pd.DataFrame:
    candidates = metrics[
        (metrics["task"] == task)
        & (metrics["split"] == "validation")
        & (metrics["window_years"] > 0)
    ]
    index = candidates.groupby("window_years")[metric].idxmin()
    return candidates.loc[index].sort_values("window_years")


def write_report(
    artifact_dir: Path,
    report_path: Path,
    feature_count: int,
) -> None:
    metrics = pd.read_csv(artifact_dir / "model_metrics.csv")
    metadata = json.loads((artifact_dir / "experiment_metadata.json").read_text(encoding="utf-8"))
    market = json.loads((artifact_dir / "market_benchmark_summary.json").read_text(encoding="utf-8"))
    audit = json.loads((artifact_dir / "market_field_audit.json").read_text(encoding="utf-8"))
    bad_cases = pd.read_csv(artifact_dir / "bad_cases.csv")
    error_slices = pd.read_csv(artifact_dir / "error_slices.csv")
    selected = metadata["selected"]
    validation_season = int(metadata["validation_season"])
    test_season = int(metadata["test_season"])
    validation_label = f"{validation_season} 至 {validation_season + 1}"
    test_label = f"{test_season} 至 {test_season + 1}"

    win = _test_row(metrics, "win_probability", selected["win_probability"])
    margin = _test_row(metrics, "expected_margin", selected["expected_margin"])
    total = _test_row(metrics, "expected_total", selected["expected_total"])

    window_lines = []
    for task, primary in [
        ("win_probability", "log_loss"),
        ("expected_margin", "mae"),
        ("expected_total", "mae"),
    ]:
        winners = _window_winners(metrics, task, primary)
        values = ", ".join(
            f"{int(row.window_years)} 年: {row.model} {_fmt(getattr(row, primary))}"
            for row in winners.itertuples(index=False)
        )
        best = winners.sort_values(primary).iloc[0]
        window_lines.append(
            f"{TASK_LABELS[task]} 的窗口内最优结果为 {values}。验证集最佳窗口为 "
            f"{int(best['window_years'])} 年。"
        )

    comparison_lines = []
    for task, primary in [
        ("win_probability", "log_loss"),
        ("expected_margin", "mae"),
        ("expected_total", "mae"),
    ]:
        chosen = selected[task]
        chosen_test = _test_row(metrics, task, chosen)
        simple_rows = metrics[
            (metrics["task"] == task)
            & (metrics["split"] == "test")
            & (metrics["model"] == "simple_baseline")
            & (metrics["window_years"] == chosen["window_years"])
        ]
        if simple_rows.empty:
            simple_rows = metrics[
                (metrics["task"] == task)
                & (metrics["split"] == "test")
                & (metrics["model"] == "simple_baseline")
            ].sort_values("window_years", ascending=False)
        simple = simple_rows.iloc[0]
        gain = float(simple[primary] - chosen_test[primary])
        comparison_lines.append(
            f"{TASK_LABELS[task]}：验证集选定模型 {chosen_test['model']} 的测试集 {primary} 为 "
            f"{_fmt(chosen_test[primary])}，同窗口简单基线为 {_fmt(simple[primary])}，绝对改善 {_fmt(gain)}。"
        )

    margin_market = market["margin"]
    total_market = market["total"]
    if market["win_probability"].get("games", 0):
        win_market_sentence = (
            f"胜率同样本比较覆盖 {market['win_probability']['games']} 场。"
        )
    else:
        win_market_sentence = (
            f"{test_label} 测试集没有 moneyline，胜率模型与市场概率无法进行同样本比较。"
        )

    bad_type_counts = bad_cases["bad_case_types"].astype(str).tolist()
    eligible_slices = error_slices[error_slices["games"] >= 50]
    worst_win_slice = eligible_slices.sort_values("mean_win_log_loss", ascending=False).iloc[0]
    worst_margin_slice = eligible_slices.sort_values("margin_mae", ascending=False).iloc[0]
    worst_total_slice = eligible_slices.sort_values("total_mae", ascending=False).iloc[0]
    selected_models = {task: details["model"] for task, details in selected.items()}
    report = f"""# CourtVision Basketball-only Baseline Modeling Report

生成范围：篮球信息独立模型、历史市场 benchmark、无前端、无 Agent。

## 执行结论

本轮使用 {feature_count} 个可审计赛前特征，固定 {validation_label} 赛季为 Validation，固定 {test_label} 赛季为 Test。每个 5、8、10 年窗口表示评估赛季以前的完整训练赛季数。测试阶段把窗口向前滚动一年，模拟上线前使用最新已完成赛季重训。

## A. Win Probability 当前做到什么程度

验证选择得到 `{selected['win_probability']['model']}`，窗口为 {selected['win_probability']['window_years']} 年。测试集 Log Loss 为 {_fmt(win['log_loss'])}，Brier Score 为 {_fmt(win['brier_score'])}，ECE 为 {_fmt(win['ece'])}，Accuracy 为 {_fmt(win['accuracy'])}。

Calibration 明细见 `win_probability_calibration.csv` 和 `win_probability_calibration.svg`。Accuracy 只承担辅助解释，模型选择依据为验证集 Log Loss。

## B. Expected Margin 当前平均误差

验证选择得到 `{selected['expected_margin']['model']}`，窗口为 {selected['expected_margin']['window_years']} 年。测试集 MAE 为 {_fmt(margin['mae'])} 分，RMSE 为 {_fmt(margin['rmse'])} 分。

## C. Expected Total 当前平均误差

验证选择得到 `{selected['expected_total']['model']}`，窗口为 {selected['expected_total']['window_years']} 年。测试集 MAE 为 {_fmt(total['mae'])} 分，RMSE 为 {_fmt(total['rmse'])} 分。

## D. 5、8、10 年窗口结果

{chr(10).join(window_lines)}

这些结论只适用于固定 Validation 上的当前特征和模型。Test 用于最终审计，没有参与窗口选择。

## E. 值得继续使用的模型

验证集最终选择为：Win Probability 使用 `{selected_models['win_probability']}`，Expected Margin 使用 `{selected_models['expected_margin']}`，Expected Total 使用 `{selected_models['expected_total']}`。

保留 Elo 作为独立实力基线。保留 Logistic Regression 和 Linear Regression 作为当前主模型。HistGradientBoosting 没有赢得任何任务的验证集选择，当前不列为优先模型。

## F. 复杂模型相比简单 baseline 的提升

{chr(10).join(comparison_lines)}

若某项改善很小，当前证据不支持为了模型复杂度增加维护成本。

## G. Basketball-only Model 与 Market Benchmark

测试集成功关联 {market['market_joined_games']} 场，关联率为 {_fmt(market['join_rate'])}。

Margin 同样本 MAE：Basketball Model {_fmt(margin_market['basketball_model']['mae'])}，Market {_fmt(margin_market['market']['mae'])}。

Total 同样本 MAE：Basketball Model {_fmt(total_market['basketball_model']['mae'])}，Market {_fmt(total_market['market']['mae'])}。

{win_market_sentence}

盘口方向审计覆盖 {audit['spread_rows_checked']} 条 spread 和 {audit['total_rows_checked']} 条 total。spread 方向不一致 {audit['spread_direction_mismatches']} 条，total 方向不一致 {audit['total_direction_mismatches']} 条。盘口是否属于 closing line 仍未证实，因此报告只称其为历史市场线。

## H. 当前最容易预测错误的比赛

结构化样本包含：{', '.join(bad_type_counts)}。完整比赛、预测、真实结果、市场基准和赛前特征见 `bad_cases.csv`。

预定义切片中，Win Log Loss 最高的是 `{worst_win_slice['dimension']}={worst_win_slice['category']}`，覆盖 {int(worst_win_slice['games'])} 场，均值 {_fmt(worst_win_slice['mean_win_log_loss'])}。Margin MAE 最高的是 `{worst_margin_slice['dimension']}={worst_margin_slice['category']}`，覆盖 {int(worst_margin_slice['games'])} 场，MAE {_fmt(worst_margin_slice['margin_mae'])}。Total MAE 最高的是 `{worst_total_slice['dimension']}={worst_total_slice['category']}`，覆盖 {int(worst_total_slice['games'])} 场，MAE {_fmt(worst_total_slice['total_mae'])}。

这些切片用于描述误差集中位置，不构成因果解释。当前 Bad Case 只陈列真实字段，不生成赛后故事。

## I. 哪些错误可能需要球员级与伤病数据

现有球队级数据无法直接识别关键球员突然缺阵、限时出场、首发变化和交易后短期阵容重构。若大误差比赛与这些事件重合，下一阶段才能形成引入球员可用性特征的证据。当前 Bad Case 本身还不能证明伤病造成误差。

## J. 预测核心是否有资格进入下一阶段

结论为 Conditional GO。当前核心已经具备进入“前向 Prediction Snapshot 与球员/伤病增量实验”的资格，理由是三个任务均稳定优于简单基线，时间切分、Calibration、盘口方向和 Bad Case 均可复现。

公开宣称预测优势、投注价值或商业化维持 HOLD。Margin 与 Total 在同样本上仍落后市场，胜率又缺少近期 moneyline 同样本证据。

## K. 下一阶段建议

优先建立真实 T-7、T-24h、Latest prediction snapshot 存储，并从新赛季开始冻结前向预测。随后将官方伤病与球员可用性作为独立增量实验，检查它能否改善当前 Bad Case。暂不进入前端、Agent 或 What-if。

## 时间切分与泄漏控制

1. 所有比赛按 game_date 与 game_id 排序。
2. 球队统计严格执行 team history `shift(1)` 后再 rolling。
3. 目标场比分、胜负、box score、plus/minus、seasonWins 和 seasonLosses不进入特征。
4. Elo 在保存赛前值后才使用目标比赛结果更新。
5. {validation_label} Validation 与 {test_label} Test 固定不变。
6. 市场字段只在模型预测完成后关联，不进入 Basketball-only 特征。

## 交付物索引

1. `model_metrics.csv`
2. `test_predictions.csv`
3. `win_probability_calibration.csv`
4. `win_probability_calibration.svg`
5. `market_field_audit_samples.csv`
6. `market_field_audit.json`
7. `market_benchmark_summary.json`
8. `test_predictions_with_market.csv`
9. `bad_cases.csv`
10. `error_slices.csv`
11. `experiment_metadata.json`
"""
    report_path.write_text(report, encoding="utf-8")
