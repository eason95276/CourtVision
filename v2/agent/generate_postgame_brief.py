"""Generate one prediction-aware, evidence-bounded Chinese postgame brief."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


V2_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = V2_DIR.parent
DATABASE_PATH = V2_DIR / "db" / "courtvision_v2.sqlite"
ANALYSIS_DATABASE_PATH = (
    PROJECT_DIR / "analysis_v1" / "data" / "courtvision_analysis.sqlite"
)
FACT_GENERATOR_PATH = V2_DIR / "scripts" / "build_single_game_facts.py"
PREDICTION_AUDIT_GENERATOR_PATH = (
    V2_DIR / "scripts" / "build_single_game_prediction_audit.py"
)
OUTPUTS_DIR = V2_DIR / "outputs"

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT_SECONDS = 60.0
GENERATOR_TIMEOUT_SECONDS = 60.0

REQUIRED_FACT_KEYS = {
    "game",
    "score_by_period",
    "game_flow",
    "four_factors",
    "player_facts",
    "shot_zone_facts",
    "key_runs",
    "limitations",
}

REQUIRED_PREDICTION_AUDIT_KEYS = {
    "game",
    "historical_prediction",
    "historical_market_benchmark",
    "direction_evaluation",
    "limitations",
}

REQUIRED_HEADINGS = (
    "## 1. 赛前预测与赛后验收",
    "## 2. 比赛走势",
    "## 3. 数据支持的赛后解释",
    "## 4. 关键球员",
    "## 5. 数据边界",
)

FORBIDDEN_CAUSAL_CLAIMS = (
    "这证明了",
    "这就是获胜的根本原因",
    "该战术直接导致了胜利",
)

FORBIDDEN_OUTPUT_CONTENT = (
    "中国体彩官方盘口",
    "投注建议",
    "下注建议",
    "推荐投注",
    "连红",
    "高置信",
    "低置信",
    "盈利",
)

STAGE_SPECIFIC_TERMS = (
    "第一节",
    "第二节",
    "第三节",
    "第四节",
    "首节",
    "次节",
    "末节",
    "上半场",
    "下半场",
    "追分阶段",
    "反扑阶段",
    "收官阶段",
    "最后阶段",
)

STRONG_CAUSAL_TERMS = (
    "导致",
    "造成",
    "决定了",
    "直接带来",
    "根本原因",
)

DIRECTION_LABELS = {
    "home": "主队方向",
    "away": "客队方向",
    "over": "大分",
    "under": "小分",
    "no_disagreement": "模型与市场无分歧",
    "push": "走盘",
}

SYSTEM_PROMPT = """你是 CourtVision 的中文赛后复盘内容撰写器。

你会收到一份由 Python 从同一场比赛两份事实 JSON 和逐回合数据库整理出的精简证据。Python 程序独立生成第一部分“赛前预测与赛后验收”、第二部分“比赛走势”和第四部分“关键球员”。你只负责为第三部分提供全场数据解释、选择二至四名候选球员的 person_id，并为第五部分提供数据边界文字。

事实边界：
1. 只能使用用户消息中的目标比赛精简证据。不得搜索网络，不得使用记忆中的篮球知识、球员背景、伤病、战术或其他比赛信息。
2. 只能使用 prediction_audit.json 中已经确定性计算的历史预测和市场事实，不得自行生成新的预测、盘口或赛前信息。
3. 不得重新计算、修改或猜测预测数字、市场数字、实际比分、让分方向、大小分方向和命中结果。
4. 每个核心判断都必须有 JSON 中的具体比分、计数、比率或球员数据支撑。没有证据时省略，或明确说明现有数据无法判断。
5. 单场数据不能代表球队或球员的长期能力。四因素、投篮区域和结果之间只能表述为单场同时出现的数据关系，不得写成确定因果。
6. 不得分析录像、教练意图、防守对位、伤病、完整阵容组合或追踪数据。
7. 不得输出投注建议、推荐、连续命中宣传、置信等级、收益或盈利相关内容。
8. 必须遵守精简证据中的 limitations。
9. four_factors 和 shot_zones 都是全场汇总。不得用这些全场汇总解释某一节、某个半场、追分、反扑或收官阶段的表现。

输出要求：
1. 只输出一个 JSON 对象，不要使用 Markdown、代码围栏或额外说明。
2. JSON 必须严格包含三个字段：
   postgame_explanation：约 220 至 360 个汉字，只选二至三个最有解释价值的全场四因素或投篮区域差异。
   selected_player_ids：从 key_player_candidates 中选择二至四个不重复的 person_id，不要求两队人数相同。
   data_boundary：约 120 至 220 个汉字，说明缺少录像、伤病、完整阵容、追踪数据和真实赛前预测时间戳；同时说明历史市场基准缺少可靠采集时间，无法确认属于初盘或终盘。
3. postgame_explanation 使用“数据上与比赛结果同时出现的优势”“可能帮助解释本场结果”等克制措辞。不得出现任何节次、半场或阶段性归因。
4. 不得输出球员数据文字。Python 会根据通过校验的 person_id 渲染球员原始数据。
5. 禁止使用“这证明了”“这就是获胜的根本原因”“该战术直接导致了胜利”。
"""


class PostgameBriefError(RuntimeError):
    """An expected, user-readable failure in the brief pipeline."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="根据 CourtVision 单场赛后事实和历史预测验收事实生成中文简报。"
    )
    parser.add_argument(
        "--game-id",
        help="详细数据库中已有的 10 位比赛 ID。",
    )
    parser.add_argument(
        "--model",
        help=(
            "DeepSeek 模型名。未传入时优先读取 DEEPSEEK_MODEL，"
            f"否则使用 {DEFAULT_MODEL}。"
        ),
    )
    args = parser.parse_args(argv)
    if not args.game_id:
        parser.error(
            "缺少 --game-id。示例：python v2/agent/generate_postgame_brief.py "
            "--game-id GAME_ID"
        )
    return args


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_contains_game(game_id: str, database_path: Path = DATABASE_PATH) -> bool:
    if not database_path.is_file():
        raise PostgameBriefError(f"找不到详细数据库：{database_path}")

    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(database_uri, uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            return (
                connection.execute(
                    "SELECT 1 FROM games WHERE game_id = :game_id",
                    {"game_id": game_id},
                ).fetchone()
                is not None
            )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise PostgameBriefError("无法只读检查详细数据库，请确认数据库文件有效。") from exc


def _run_generator(game_id: str, script_path: Path, label: str) -> None:
    if not script_path.is_file():
        raise PostgameBriefError(f"找不到{label}生成器：{script_path}")

    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            [sys.executable, str(script_path), "--game-id", game_id],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
            timeout=GENERATOR_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PostgameBriefError(
            f"{label}生成超过 60 秒，流程已停止，未调用 DeepSeek。"
        ) from exc

    if result.returncode != 0:
        raise PostgameBriefError(
            f"{label}生成失败，退出码为 {result.returncode}，未调用 DeepSeek。"
        )


def run_fact_generator(
    game_id: str,
    script_path: Path = FACT_GENERATOR_PATH,
) -> None:
    _run_generator(game_id, script_path, "赛后事实")


def run_prediction_audit_generator(
    game_id: str,
    script_path: Path = PREDICTION_AUDIT_GENERATOR_PATH,
) -> None:
    _run_generator(game_id, script_path, "预测验收事实")


def _collect_game_ids(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "game_id":
                found.add(str(child))
            found.update(_collect_game_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_game_ids(child))
    return found


def _read_json_object(path: Path, label: str) -> dict:
    if not path.is_file():
        raise PostgameBriefError(
            f"{label}文件没有生成：{path}。流程已停止，未调用 DeepSeek。"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PostgameBriefError(
            f"{label} JSON 无法读取或解析。流程已停止，未调用 DeepSeek。"
        ) from exc
    if not isinstance(value, dict):
        raise PostgameBriefError(
            f"{label} JSON 顶层格式错误。流程已停止，未调用 DeepSeek。"
        )
    return value


def _validate_single_target_game(
    document: dict,
    game_id: str,
    label: str,
) -> None:
    document_game_id = document.get("game", {}).get("game_id")
    if document_game_id != game_id:
        raise PostgameBriefError(
            f"{label} JSON 的比赛 ID 为 {document_game_id!r}，"
            f"与请求的 {game_id!r} 不一致。流程已停止，未调用 DeepSeek。"
        )
    referenced_game_ids = _collect_game_ids(document)
    if referenced_game_ids != {game_id}:
        raise PostgameBriefError(
            f"{label} JSON 引用了目标比赛之外的比赛 ID。"
            "流程已停止，未调用 DeepSeek。"
        )


def load_target_facts(
    game_id: str,
    outputs_dir: Path = OUTPUTS_DIR,
) -> dict:
    facts = _read_json_object(
        outputs_dir / f"game_{game_id}_facts.json",
        "赛后事实",
    )
    missing = REQUIRED_FACT_KEYS - set(facts)
    if missing:
        raise PostgameBriefError(
            "赛后事实 JSON 缺少简报所需字段："
            + "、".join(sorted(missing))
            + "。流程已停止，未调用 DeepSeek。"
        )
    _validate_single_target_game(facts, game_id, "赛后事实")
    limitations = facts.get("limitations")
    if not isinstance(limitations, list) or not limitations:
        raise PostgameBriefError(
            "赛后事实 JSON 缺少有效的 limitations。流程已停止，未调用 DeepSeek。"
        )
    return facts


def load_target_prediction_audit(
    game_id: str,
    outputs_dir: Path = OUTPUTS_DIR,
) -> dict:
    audit = _read_json_object(
        outputs_dir / f"game_{game_id}_prediction_audit.json",
        "预测验收事实",
    )
    missing = REQUIRED_PREDICTION_AUDIT_KEYS - set(audit)
    if missing:
        raise PostgameBriefError(
            "预测验收事实 JSON 缺少简报所需字段："
            + "、".join(sorted(missing))
            + "。流程已停止，未调用 DeepSeek。"
        )
    _validate_single_target_game(audit, game_id, "预测验收事实")

    prediction = audit.get("historical_prediction")
    market = audit.get("historical_market_benchmark")
    evaluation = audit.get("direction_evaluation")
    limitations = audit.get("limitations")
    if not isinstance(prediction, dict) or not isinstance(market, dict):
        raise PostgameBriefError(
            "预测验收事实 JSON 的预测或市场结构无效。流程已停止，未调用 DeepSeek。"
        )
    if not isinstance(evaluation, dict) or not all(
        isinstance(evaluation.get(key), dict) for key in ("spread", "total")
    ):
        raise PostgameBriefError(
            "预测验收事实 JSON 的方向验收结构无效。流程已停止，未调用 DeepSeek。"
        )
    if not isinstance(limitations, list) or not limitations:
        raise PostgameBriefError(
            "预测验收事实 JSON 缺少有效的 limitations。流程已停止，未调用 DeepSeek。"
        )
    if prediction.get("prediction_record_type") != "historical_backtest":
        raise PostgameBriefError(
            "预测记录没有明确标记为历史回测。流程已停止，未调用 DeepSeek。"
        )
    if prediction.get("live_pregame_snapshot") is not False:
        raise PostgameBriefError(
            "预测记录的真实赛前快照边界无效。流程已停止，未调用 DeepSeek。"
        )
    if market.get("market_source_label") != "historical_market_benchmark":
        raise PostgameBriefError(
            "历史市场来源标签无效。流程已停止，未调用 DeepSeek。"
        )
    if market.get("official_china_sports_lottery_verified") is not False:
        raise PostgameBriefError(
            "市场来源核验边界无效。流程已停止，未调用 DeepSeek。"
        )
    return audit


def validate_fact_consistency(facts: dict, audit: dict) -> None:
    postgame = facts["game"]
    prediction_game = audit["game"]
    comparisons = {
        "比赛日期": (postgame.get("date"), prediction_game.get("game_date")),
        "客队 ID": (
            postgame.get("away", {}).get("team_id"),
            prediction_game.get("away", {}).get("team_id"),
        ),
        "客队简称": (
            postgame.get("away", {}).get("tricode"),
            prediction_game.get("away", {}).get("tricode"),
        ),
        "客队得分": (
            postgame.get("away", {}).get("points"),
            prediction_game.get("away", {}).get("points"),
        ),
        "主队 ID": (
            postgame.get("home", {}).get("team_id"),
            prediction_game.get("home", {}).get("team_id"),
        ),
        "主队简称": (
            postgame.get("home", {}).get("tricode"),
            prediction_game.get("home", {}).get("tricode"),
        ),
        "主队得分": (
            postgame.get("home", {}).get("points"),
            prediction_game.get("home", {}).get("points"),
        ),
        "实际总分": (
            postgame.get("actual_total"),
            prediction_game.get("actual_total"),
        ),
    }
    mismatches = [label for label, values in comparisons.items() if values[0] != values[1]]
    if mismatches:
        raise PostgameBriefError(
            "两份事实 JSON 的比赛内容不一致："
            + "、".join(mismatches)
            + "。流程已停止，未调用 DeepSeek。"
        )


def _read_play_by_play(
    game_id: str,
    database_path: Path = DATABASE_PATH,
) -> list[dict[str, object]]:
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                """
                SELECT action_id, period, clock, score_away, score_home
                FROM play_by_play
                WHERE game_id = :game_id
                  AND score_away IS NOT NULL
                  AND score_home IS NOT NULL
                ORDER BY action_id
                """,
                {"game_id": game_id},
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise PostgameBriefError("无法只读取逐回合数据，未调用 DeepSeek。") from exc
    if not rows:
        raise PostgameBriefError("目标比赛没有可用的逐回合比分，未调用 DeepSeek。")
    return [dict(row) for row in rows]


def _event(row: dict[str, object]) -> dict[str, object]:
    return {
        "action_id": int(row["action_id"]),
        "period": int(row["period"]),
        "clock": str(row["clock"]),
        "score_away": int(row["score_away"]),
        "score_home": int(row["score_home"]),
    }


def _leader(row: dict[str, object], away: str, home: str) -> str | None:
    away_score = int(row["score_away"])
    home_score = int(row["score_home"])
    if away_score > home_score:
        return away
    if home_score > away_score:
        return home
    return None


def build_game_flow_evidence(
    game_id: str,
    facts: dict,
    database_path: Path = DATABASE_PATH,
) -> dict:
    game = facts.get("game", {})
    if game.get("game_id") != game_id:
        raise PostgameBriefError("赛后事实与比赛 ID 不一致，未调用 DeepSeek。")
    try:
        away = game["away"]["tricode"]
        home = game["home"]["tricode"]
        final_away = int(game["away"]["points"])
        final_home = int(game["home"]["points"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PostgameBriefError("赛后事实缺少构造比赛走势所需字段。") from exc

    rows = _read_play_by_play(game_id, database_path)
    period_end_rows: dict[int, dict[str, object]] = {}
    for row in rows:
        period_end_rows[int(row["period"])] = row
    period_end_scores = [_event(period_end_rows[p]) for p in sorted(period_end_rows)]
    final_score = _event(rows[-1])
    if (
        final_score["score_away"] != final_away
        or final_score["score_home"] != final_home
    ):
        raise PostgameBriefError("逐回合最终比分与赛后事实不一致，未调用 DeepSeek。")

    halftime = period_end_rows.get(2)
    if halftime is None:
        raise PostgameBriefError("逐回合数据缺少半场比分，未调用 DeepSeek。")
    halftime_score = _event(halftime)
    second_half_rows = [row for row in rows if int(row["period"]) >= 3]

    halftime_leader = _leader(halftime, away, home)
    first_second_half_change = None
    for row in second_half_rows:
        current_leader = _leader(row, away, home)
        if current_leader is not None and current_leader != halftime_leader:
            first_second_half_change = {
                "from_team_tricode": halftime_leader,
                "to_team_tricode": current_leader,
                "event": _event(row),
                "status": (
                    "lead_established_after_halftime_tie"
                    if halftime_leader is None
                    else "lead_changed"
                ),
            }
            break

    halftime_away = int(halftime["score_away"])
    halftime_home = int(halftime["score_home"])
    if halftime_away == halftime_home:
        comeback = {
            "status": "tied_at_halftime",
            "team_tricode": None,
            "tied_after_halftime": None,
            "first_tie_event": None,
            "first_lead_event": None,
        }
    else:
        trailing_team = away if halftime_away < halftime_home else home
        first_tie = next(
            (
                _event(row)
                for row in second_half_rows
                if int(row["score_away"]) == int(row["score_home"])
            ),
            None,
        )
        first_lead = next(
            (
                _event(row)
                for row in second_half_rows
                if _leader(row, away, home) == trailing_team
            ),
            None,
        )
        comeback = {
            "status": "took_lead" if first_lead else ("tied_only" if first_tie else "never_tied"),
            "team_tricode": trailing_team,
            "tied_after_halftime": first_tie is not None,
            "first_tie_event": first_tie,
            "first_lead_event": first_lead,
        }

    maximum_leads: dict[str, dict[str, object]] = {}
    for team in (away, home):
        lead_rows = []
        for row in rows:
            margin = (
                int(row["score_away"]) - int(row["score_home"])
                if team == away
                else int(row["score_home"]) - int(row["score_away"])
            )
            if margin > 0:
                lead_rows.append((margin, row))
        if not lead_rows:
            maximum_leads[team] = {
                "maximum_lead": 0,
                "first_event": None,
                "last_event": None,
                "status": "never_led",
            }
            continue
        maximum = max(margin for margin, _ in lead_rows)
        matching = [row for margin, row in lead_rows if margin == maximum]
        maximum_leads[team] = {
            "maximum_lead": maximum,
            "first_event": _event(matching[0]),
            "last_event": _event(matching[-1]),
            "status": "available",
        }

    winner = game.get("winner_tricode")
    loser = home if winner == away else away if winner == home else None
    loser_segment = None
    if loser is not None and maximum_leads[loser]["maximum_lead"] > 0:
        last_max_event = maximum_leads[loser]["last_event"]
        loser_segment = {
            "status": "available",
            "team_tricode": loser,
            "event": last_max_event,
            "scoring_after_event": {
                away: final_away - int(last_max_event["score_away"]),
                home: final_home - int(last_max_event["score_home"]),
            },
            "final_score": final_score,
        }
    else:
        loser_segment = {
            "status": "not_applicable",
            "team_tricode": loser,
            "event": None,
            "scoring_after_event": None,
            "final_score": final_score,
        }

    halves = [
        {
            "half": 1,
            "score_away": halftime_score["score_away"],
            "score_home": halftime_score["score_home"],
        },
        {
            "half": 2,
            "score_away": final_away - int(halftime_score["score_away"]),
            "score_home": final_home - int(halftime_score["score_home"]),
        },
    ]
    evidence = {
        "game_id": game_id,
        "away_team_tricode": away,
        "home_team_tricode": home,
        "period_end_scores": period_end_scores,
        "halftime_score": halftime_score,
        "halves": halves,
        "maximum_leads": maximum_leads,
        "second_half_first_lead_change": first_second_half_change,
        "third_quarter_end": (
            {
                **_event(period_end_rows[3]),
                "status": (
                    "tie"
                    if int(period_end_rows[3]["score_away"])
                    == int(period_end_rows[3]["score_home"])
                    else "away_lead"
                    if int(period_end_rows[3]["score_away"])
                    > int(period_end_rows[3]["score_home"])
                    else "home_lead"
                ),
            }
            if 3 in period_end_rows
            else None
        ),
        "halftime_trailing_team_comeback": comeback,
        "eventual_loser_last_maximum_lead": loser_segment,
        "final_score": final_score,
    }
    validate_game_flow_evidence(evidence, game_id, database_path)
    return evidence


def _collect_evidence_events(value: object) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    if isinstance(value, dict):
        required = {"action_id", "period", "clock", "score_away", "score_home"}
        if required.issubset(value):
            events.append(value)
        for child in value.values():
            events.extend(_collect_evidence_events(child))
    elif isinstance(value, list):
        for child in value:
            events.extend(_collect_evidence_events(child))
    return events


def validate_game_flow_evidence(
    evidence: dict,
    game_id: str,
    database_path: Path = DATABASE_PATH,
) -> None:
    if evidence.get("game_id") != game_id:
        raise PostgameBriefError("比赛走势证据的 game_id 不一致。")
    rows = _read_play_by_play(game_id, database_path)
    rows_by_action = {int(row["action_id"]): _event(row) for row in rows}
    for event in _collect_evidence_events(evidence):
        expected = rows_by_action.get(event.get("action_id"))
        observed = {key: event.get(key) for key in expected} if expected else None
        if expected is None or observed != expected:
            raise PostgameBriefError(
                f"逐回合证据不一致：action_id={event.get('action_id')!r}。"
            )


def _format_clock(clock: str) -> str:
    match = re.fullmatch(r"PT(\d{2})M(\d{2})(?:\.\d+)?S", clock)
    if not match:
        return clock
    return f"{int(match.group(1))}:{match.group(2)}"


def _period_label(period: int) -> str:
    labels = {1: "第一节", 2: "第二节", 3: "第三节", 4: "第四节"}
    return labels.get(period, f"第 {period - 4} 个加时")


def _score_text(event: dict[str, object], away: str, home: str) -> str:
    return f"{away} {event['score_away']}，{home} {event['score_home']}"


def build_game_flow_section(evidence: dict) -> str:
    away = str(evidence["away_team_tricode"])
    home = str(evidence["home_team_tricode"])
    halftime = evidence["halftime_score"]
    third = evidence.get("third_quarter_end")
    final_score = evidence["final_score"]
    lines = ["## 2. 比赛走势", ""]

    halftime_leader = _leader(halftime, away, home)
    if halftime_leader is None:
        lines.append(f"上半场结束时双方战成 {_score_text(halftime, away, home)}。")
    else:
        halftime_margin = abs(int(halftime["score_away"]) - int(halftime["score_home"]))
        lines.append(
            f"上半场结束时比分为 {_score_text(halftime, away, home)}，"
            f"{halftime_leader} 领先 {halftime_margin} 分。"
        )

    max_parts = []
    for team in (away, home):
        maximum = evidence["maximum_leads"][team]
        event = maximum["first_event"]
        if event is None:
            max_parts.append(f"{team} 全场没有取得领先")
        else:
            max_parts.append(
                f"{team} 的最大领先为 {maximum['maximum_lead']} 分，首次出现在"
                f"{_period_label(int(event['period']))} {_format_clock(str(event['clock']))}，"
                f"当时 {_score_text(event, away, home)}"
            )
    lines.append("；".join(max_parts) + "。")

    if third is not None:
        third_status = "双方战平" if third["status"] == "tie" else "比赛仍有领先方"
        lines.append(
            f"第三节结束，比分为 {_score_text(third, away, home)}，{third_status}。"
        )

    comeback = evidence["halftime_trailing_team_comeback"]
    trailing_team = comeback.get("team_tricode")
    first_lead = comeback.get("first_lead_event")
    if trailing_team and first_lead:
        lines.append(
            f"半场落后的 {trailing_team} 在下半场完成追平，首次反超发生在"
            f"{_period_label(int(first_lead['period']))} {_format_clock(str(first_lead['clock']))}，"
            f"比分为 {_score_text(first_lead, away, home)}。"
        )
    elif trailing_team and comeback.get("first_tie_event"):
        tie_event = comeback["first_tie_event"]
        lines.append(
            f"半场落后的 {trailing_team} 曾在"
            f"{_period_label(int(tie_event['period']))} {_format_clock(str(tie_event['clock']))}"
            "追平，但此后没有取得领先。"
        )
    elif trailing_team:
        lines.append(f"半场落后的 {trailing_team} 下半场没有追平或反超。")

    segment = evidence["eventual_loser_last_maximum_lead"]
    if segment["status"] == "available":
        event = segment["event"]
        scoring = segment["scoring_after_event"]
        eventual_winner = home if segment["team_tricode"] == away else away
        lines.append(
            f"{segment['team_tricode']} 最后一次达到本队最大领先是在"
            f"{_period_label(int(event['period']))} {_format_clock(str(event['clock']))}，"
            f"比分为 {_score_text(event, away, home)}。此后到终场，"
            f"{eventual_winner} 得到 {scoring[eventual_winner]} 分，"
            f"{segment['team_tricode']} 得到 {scoring[segment['team_tricode']]} 分，"
            f"最终比分为 {_score_text(final_score, away, home)}。"
        )
    else:
        lines.append(f"最终比分为 {_score_text(final_score, away, home)}。")
    return "\n\n".join([lines[0], "".join(lines[2:])]).strip() + "\n"


def build_key_player_candidates(facts: dict) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    teams = facts.get("player_facts", {}).get("teams", {})
    for team_tricode, players in teams.items():
        if not isinstance(players, list):
            continue
        ranked = sorted(
            players,
            key=lambda row: (
                -int(row.get("points", 0)),
                -int(row.get("rebounds", 0)),
                -int(row.get("assists", 0)),
                int(row.get("person_id", 0)),
            ),
        )[:4]
        for player in ranked:
            candidates.append(
                {
                    "person_id": player["person_id"],
                    "team_tricode": team_tricode,
                    "name": player["name"],
                    "minutes": player["minutes"],
                    "points": player["points"],
                    "rebounds": player["rebounds"],
                    "assists": player["assists"],
                    "field_goals_made": player["field_goals"]["made"],
                    "field_goals_attempted": player["field_goals"]["attempted"],
                    "field_goal_percentage": player["field_goal_percentage"]["raw"],
                    "true_shooting_percentage": player["true_shooting_percentage"]["raw"],
                    "plus_minus": player["plus_minus"],
                }
            )
    return candidates


def _numbers_equal(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
    return left == right


def validate_selected_players(
    selected_player_ids: object,
    candidates: list[dict[str, object]],
    game_id: str,
    database_path: Path = DATABASE_PATH,
) -> list[dict[str, object]]:
    if not isinstance(selected_player_ids, list) or not 2 <= len(selected_player_ids) <= 4:
        raise PostgameBriefError("关键球员总数必须在 2 至 4 人之间。")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in selected_player_ids):
        raise PostgameBriefError("关键球员 person_id 必须为整数。")
    if len(set(selected_player_ids)) != len(selected_player_ids):
        raise PostgameBriefError("关键球员名单包含重复球员。")

    candidates_by_id = {int(row["person_id"]): row for row in candidates}
    missing = [value for value in selected_player_ids if value not in candidates_by_id]
    if missing:
        raise PostgameBriefError("关键球员不在本场候选球员中。")

    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    placeholders = ",".join("?" for _ in selected_player_ids)
    query = f"""
        SELECT person_id, team_tricode, full_name, minutes, points,
               rebounds_total, assists, field_goals_made, field_goals_attempted,
               field_goals_percentage, free_throws_attempted, plus_minus_points
        FROM player_boxscores
        WHERE game_id = ? AND person_id IN ({placeholders})
    """
    try:
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(query, [game_id, *selected_player_ids]).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise PostgameBriefError("无法只读核对关键球员数据。") from exc
    database_rows = {int(row["person_id"]): dict(row) for row in rows}
    selected: list[dict[str, object]] = []
    for person_id in selected_player_ids:
        candidate = candidates_by_id[person_id]
        source = database_rows.get(person_id)
        if source is None:
            raise PostgameBriefError("关键球员不在本场 player_boxscores 中。")
        denominator = 2 * (
            int(source["field_goals_attempted"]) + 0.44 * int(source["free_throws_attempted"])
        )
        field_goal_percentage = (
            int(source["field_goals_made"]) / int(source["field_goals_attempted"])
            if int(source["field_goals_attempted"])
            else None
        )
        true_shooting = int(source["points"]) / denominator if denominator else None
        expected = {
            "person_id": source["person_id"],
            "team_tricode": source["team_tricode"],
            "name": source["full_name"],
            "minutes": source["minutes"],
            "points": source["points"],
            "rebounds": source["rebounds_total"],
            "assists": source["assists"],
            "field_goals_made": source["field_goals_made"],
            "field_goals_attempted": source["field_goals_attempted"],
            "field_goal_percentage": field_goal_percentage,
            "true_shooting_percentage": true_shooting,
            "plus_minus": source["plus_minus_points"],
        }
        if any(not _numbers_equal(candidate.get(key), value) for key, value in expected.items()):
            raise PostgameBriefError(f"球员数据与 player_boxscores 不一致：person_id={person_id}。")
        selected.append(candidate)
    return selected


def build_key_players_section(selected_players: list[dict[str, object]]) -> str:
    lines = ["## 4. 关键球员", ""]
    for player in selected_players:
        field_goal = format_decimal(
            float(player["field_goal_percentage"]) * 100,
            "field_goal_percentage",
            digits=1,
        )
        true_shooting = format_decimal(
            float(player["true_shooting_percentage"]) * 100,
            "true_shooting_percentage",
            digits=1,
        )
        plus_minus = format_decimal(player["plus_minus"], "plus_minus", digits=1, signed=True)
        lines.append(
            f"- {player['team_tricode']} 的 {player['name']}："
            f"{player['points']} 分、{player['rebounds']} 个篮板、{player['assists']} 次助攻，"
            f"投篮命中率 {field_goal}%，真实命中率 {true_shooting}%，正负值 {plus_minus}。"
        )
    lines.extend(["", "以上只描述本场表现，不能外推为球员的长期能力。"])
    return "\n".join(lines).strip() + "\n"


def build_compact_evidence(facts: dict, prediction_audit: dict, flow: dict) -> dict:
    return {
        "game": facts["game"],
        "prediction_review": {
            "historical_prediction": prediction_audit["historical_prediction"],
            "historical_market_benchmark": prediction_audit["historical_market_benchmark"],
            "direction_evaluation": prediction_audit["direction_evaluation"],
        },
        "game_flow_evidence": flow,
        "four_factors": facts["four_factors"],
        "shot_zones": facts["shot_zone_facts"],
        "key_player_candidates": build_key_player_candidates(facts),
        "limitations": [
            *facts["limitations"],
            "历史回测预测缺少真实赛前持久化时间戳。",
            "历史市场基准缺少可靠采集时间，无法确认属于初盘或终盘。",
            "四因素和投篮区域均为全场汇总，不能支持分阶段因果判断。",
        ],
    }


def evidence_character_counts(facts: dict, prediction_audit: dict, compact: dict) -> dict[str, int]:
    options = {"ensure_ascii": False, "separators": (",", ":"), "allow_nan": False}
    full = json.dumps(facts, **options) + json.dumps(prediction_audit, **options)
    compact_text = json.dumps(compact, **options)
    return {"full_characters": len(full), "compact_characters": len(compact_text)}


def build_messages(compact_evidence: dict) -> list[dict[str, str]]:
    try:
        evidence_json = json.dumps(
            compact_evidence,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PostgameBriefError(
            "事实 JSON 含有无法发送的数值或结构，未调用 DeepSeek。"
        ) from exc

    user_prompt = (
        "请只根据下面的目标比赛精简证据返回规定的 JSON 对象。"
        "Python 将独立生成第 1、2、4 部分。\n\n"
        "<brief_evidence>\n"
        f"{evidence_json}\n"
        "</brief_evidence>"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def request_deepseek(
    messages: list[dict[str, str]],
    api_key: str,
    model: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable | None = None,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 1800,
        "thinking": {"type": "disabled"},
        "stream": False,
    }
    request = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    open_request = opener or urllib.request.urlopen

    try:
        with open_request(request, timeout=timeout_seconds) as response:
            raw_response = response.read()
    except urllib.error.HTTPError as exc:
        raise PostgameBriefError(
            f"DeepSeek 返回 HTTP {exc.code}，未生成新简报。"
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise PostgameBriefError(
            "DeepSeek 请求超时或网络不可用，未生成新简报。"
        ) from exc

    try:
        response_data = json.loads(raw_response.decode("utf-8"))
        choice = response_data["choices"][0]
        content = choice["message"]["content"]
    except (UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise PostgameBriefError(
            "DeepSeek 返回的数据格式无效，未生成新简报。"
        ) from exc

    if not isinstance(content, str) or not content.strip():
        finish_reason = choice.get("finish_reason")
        if finish_reason not in {"stop", "length", "content_filter", "tool_calls"}:
            finish_reason = "unknown"
        completion_tokens = response_data.get("usage", {}).get("completion_tokens")
        if not isinstance(completion_tokens, int) or completion_tokens < 0:
            completion_tokens = "unknown"
        raise PostgameBriefError(
            "DeepSeek 返回的最终文本为空，"
            f"finish_reason={finish_reason}，completion_tokens={completion_tokens}，"
            "未生成新简报。"
        )
    return content


def extract_headings(content: str) -> list[str]:
    return re.findall(r"(?m)^##\s+\d+\.\s+.+$", content)


def _validate_text_safety(content: str, game_id: str, api_key: str = "") -> None:
    if api_key and api_key in content:
        raise PostgameBriefError("DeepSeek 返回内容包含敏感信息，已拒绝保存。")
    if re.search(r"(?i)authorization\s*:|bearer\s+[a-z0-9._-]+", content):
        raise PostgameBriefError("DeepSeek 返回内容疑似包含请求凭据，已拒绝保存。")
    for phrase in (*FORBIDDEN_CAUSAL_CLAIMS, *FORBIDDEN_OUTPUT_CONTENT):
        if phrase in content:
            raise PostgameBriefError(f"简报包含禁止的内容：“{phrase}”。")
    other_game_ids = {
        value
        for value in re.findall(r"(?<!\d)\d{10}(?!\d)", content)
        if value != game_id
    }
    if other_game_ids:
        raise PostgameBriefError("简报引用了目标比赛之外的比赛 ID。")


def _validate_sections(content: str, headings: tuple[str, ...], label: str) -> None:
    if tuple(extract_headings(content)) != headings:
        count_label = {4: "四", 5: "五"}.get(len(headings), str(len(headings)))
        raise PostgameBriefError(f"{label}没有严格包含{count_label}个固定章节。")
    for index, heading in enumerate(headings):
        start = content.index(heading) + len(heading)
        end = content.find(headings[index + 1], start) if index + 1 < len(headings) else len(content)
        if len(content[start:end].strip()) < 20:
            raise PostgameBriefError(f"章节“{heading}”内容为空或过短。")


def validate_postgame_explanation(content: object) -> str:
    if not isinstance(content, str) or not content.strip():
        raise PostgameBriefError("DeepSeek 返回的赛后解释为空。")
    explanation = content.strip()
    stage_terms = [term for term in STAGE_SPECIFIC_TERMS if term in explanation]
    if stage_terms:
        raise PostgameBriefError(
            "全场汇总数据出现无证据的阶段性表述：" + "、".join(stage_terms) + "。"
        )
    causal_terms = [term for term in STRONG_CAUSAL_TERMS if term in explanation]
    if causal_terms:
        raise PostgameBriefError(
            "赛后解释包含无法由汇总数据证明的因果表述：" + "、".join(causal_terms) + "。"
        )
    chinese_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", explanation))
    if chinese_count < 80 or chinese_count > 500:
        raise PostgameBriefError("赛后解释长度明显偏离约 220 至 360 个汉字的目标。")
    return explanation


def validate_model_response(
    content: str,
    game_id: str,
    api_key: str = "",
) -> dict[str, object]:
    generated = content.strip()
    if not generated:
        raise PostgameBriefError("DeepSeek 返回了空内容，未生成新简报。")
    if generated.startswith("```") or generated.endswith("```"):
        raise PostgameBriefError("DeepSeek 返回了代码围栏，结构化内容不合格。")
    _validate_text_safety(generated, game_id, api_key)
    try:
        value = json.loads(generated)
    except json.JSONDecodeError as exc:
        raise PostgameBriefError("DeepSeek 返回内容不是有效的 JSON 对象。") from exc
    required = {"postgame_explanation", "selected_player_ids", "data_boundary"}
    if not isinstance(value, dict) or set(value) != required:
        raise PostgameBriefError("DeepSeek 返回的 JSON 字段不符合固定结构。")

    explanation = validate_postgame_explanation(value["postgame_explanation"])
    boundary = value["data_boundary"]
    if not isinstance(boundary, str) or not boundary.strip():
        raise PostgameBriefError("DeepSeek 返回的数据边界为空。")
    boundary = boundary.strip()
    required_boundary_terms = (
        "录像",
        "伤病",
        "完整阵容",
        "追踪数据",
        "赛前",
        "采集时间",
        "初盘",
        "终盘",
    )
    missing = [term for term in required_boundary_terms if term not in boundary]
    if missing:
        raise PostgameBriefError(
            "数据边界缺少必要限制：" + "、".join(missing) + "。"
        )
    boundary_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", boundary))
    if boundary_count < 70 or boundary_count > 350:
        raise PostgameBriefError("数据边界长度明显偏离约 120 至 220 个汉字的目标。")
    _validate_text_safety(explanation + "\n" + boundary, game_id, api_key)
    return {
        "postgame_explanation": explanation,
        "selected_player_ids": value["selected_player_ids"],
        "data_boundary": boundary,
    }


def _number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PostgameBriefError(f"预测验收事实中的 {field_name} 不是有效数字。")
    number = float(value)
    if not math.isfinite(number):
        raise PostgameBriefError(f"预测验收事实中的 {field_name} 不是有限数字。")
    return number


def format_decimal(
    value: object,
    field_name: str,
    digits: int = 4,
    signed: bool = False,
) -> str:
    number = _number(value, field_name)
    formatted = f"{abs(number):.{digits}f}".rstrip("0").rstrip(".")
    if signed:
        if number > 0:
            return "+" + formatted
        if number < 0:
            return "-" + formatted
    return formatted


def _direction_paragraph(
    name: str,
    evaluation: dict,
    hit_key: str,
) -> str:
    status = evaluation.get("status")
    model_direction = evaluation.get("model_direction")
    actual_result = evaluation.get("actual_result")
    hit = evaluation.get(hit_key)

    if status == "market_unavailable" or model_direction is None:
        return f"{name}：缺少可靠市场基准，无法验收。"
    if model_direction == "no_disagreement":
        return f"{name}：模型与市场无分歧，不判断命中或未命中。"
    if actual_result == "push" or status == "push":
        model_label = DIRECTION_LABELS.get(model_direction)
        if model_label is None:
            raise PostgameBriefError(f"{name}包含未知模型方向：{model_direction!r}。")
        return f"{name}：模型方向为{model_label}，实际结果为走盘，不计入命中或未命中。"
    model_label = DIRECTION_LABELS.get(model_direction)
    actual_label = DIRECTION_LABELS.get(actual_result)
    if model_label is None or actual_label is None or hit not in {True, False}:
        raise PostgameBriefError(f"{name}的方向或命中状态无效。")
    hit_label = "命中" if hit else "未命中"
    status_label = "已验收" if status == "graded" else str(status)
    return (
        f"{name}：模型方向为{model_label}，实际方向为{actual_label}，"
        f"状态为{status_label}，结果为{hit_label}。"
    )


def build_prediction_review_section(prediction_audit: dict) -> str:
    try:
        game = prediction_audit["game"]
        prediction = prediction_audit["historical_prediction"]
        market = prediction_audit["historical_market_benchmark"]
        spread = prediction_audit["direction_evaluation"]["spread"]
        total = prediction_audit["direction_evaluation"]["total"]

        home = game["home"]
        away = game["away"]
        probability = _number(
            prediction["home_win_probability"], "home_win_probability"
        )
        probability_text = format_decimal(
            probability * 100,
            "home_win_probability_percentage",
            digits=2,
        )
        predicted_margin = format_decimal(
            prediction["predicted_home_margin"],
            "predicted_home_margin",
            signed=True,
        )
        predicted_total = format_decimal(
            prediction["predicted_total"], "predicted_total"
        )
        actual_margin = format_decimal(
            game["actual_home_margin"], "actual_home_margin", signed=True
        )
        actual_total = format_decimal(game["actual_total"], "actual_total")
    except (KeyError, TypeError) as exc:
        raise PostgameBriefError(
            "预测验收事实缺少生成第一部分所需的字段。"
        ) from exc

    lines = [
        "## 1. 赛前预测与赛后验收",
        "",
        (
            f"{away['name']}（{away['tricode']}）客场对阵"
            f"{home['name']}（{home['tricode']}），最终比分 "
            f"{home['tricode']} {home['points']}，{away['tricode']} {away['points']}；"
            f"实际主队分差 {actual_margin} 分，实际总分 {actual_total} 分。"
        ),
        "",
        (
            "本场记录属于历史回测预测，缺少真实赛前持久化时间戳，"
            "不能作为真实赛前发布记录。"
        ),
        "",
        (
            f"模型预测：主队获胜概率 {probability_text}%，"
            f"预测主队分差 {predicted_margin} 分，预测总分 {predicted_total} 分。"
        ),
        "",
    ]

    market_margin = market.get("market_expected_home_margin")
    market_total = market.get("market_total")
    if market_margin is None and market_total is None:
        lines.append("历史市场基准：当前缺少可靠市场数值。")
    else:
        margin_text = (
            "空值"
            if market_margin is None
            else format_decimal(
                market_margin,
                "market_expected_home_margin",
                signed=True,
            )
        )
        total_text = (
            "空值"
            if market_total is None
            else format_decimal(market_total, "market_total")
        )
        lines.append(
            f"历史市场基准：预期主队分差 {margin_text} 分，总分 {total_text} 分。"
        )

    lines.extend(
        [
            "",
            _direction_paragraph(
                "让分方向", spread, "spread_direction_hit"
            ),
            "",
            _direction_paragraph(
                "大小分方向", total, "total_direction_hit"
            ),
        ]
    )
    return "\n".join(lines).strip() + "\n"


def compose_complete_brief(
    prediction_section: str,
    flow_section: str,
    postgame_explanation: str,
    key_players_section: str,
    data_boundary: str,
) -> str:
    sections = (
        prediction_section.strip(),
        flow_section.strip(),
        "## 3. 数据支持的赛后解释\n\n" + postgame_explanation.strip(),
        key_players_section.strip(),
        "## 5. 数据边界\n\n" + data_boundary.strip(),
    )
    return "\n\n".join(sections) + "\n"


def validate_complete_brief(
    content: str,
    game_id: str,
    api_key: str = "",
) -> str:
    brief = content.strip()
    _validate_text_safety(brief, game_id, api_key)
    _validate_sections(brief, REQUIRED_HEADINGS, "完整简报")
    chinese_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", brief))
    if chinese_count < 600 or chinese_count > 1500:
        raise PostgameBriefError(
            "完整简报长度明显偏离约 800 至 1200 个汉字的目标。"
        )
    return brief + "\n"


def atomic_write_brief(
    game_id: str,
    content: str,
    outputs_dir: Path = OUTPUTS_DIR,
) -> Path:
    outputs_dir.mkdir(parents=True, exist_ok=True)
    output_path = outputs_dir / f"game_{game_id}_prediction_review_brief.md"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".game_{game_id}_prediction_review_brief_",
        suffix=".tmp",
        dir=outputs_dir,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return output_path


def _existing_source_hashes(game_id: str, outputs_dir: Path) -> dict[Path, str | None]:
    paths = (
        outputs_dir / f"game_{game_id}_facts.json",
        outputs_dir / f"game_{game_id}_prediction_audit.json",
    )
    return {path: sha256_file(path) if path.is_file() else None for path in paths}


def _assert_existing_sources_unchanged(source_hashes: dict[Path, str | None]) -> None:
    changed = [
        path.name
        for path, hash_before in source_hashes.items()
        if hash_before is not None
        and (not path.is_file() or sha256_file(path) != hash_before)
    ]
    if changed:
        raise PostgameBriefError(
            "已有事实 JSON 在流程中发生变化："
            + "、".join(changed)
            + "。已拒绝调用或保存。"
        )


def generate_postgame_brief(
    game_id: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    database_path: Path = DATABASE_PATH,
    analysis_database_path: Path = ANALYSIS_DATABASE_PATH,
    outputs_dir: Path = OUTPUTS_DIR,
    fact_generator: Callable[[str], None] | None = None,
    prediction_audit_generator: Callable[[str], None] | None = None,
    api_client: Callable[[list[dict[str, str]], str, str, float], str] | None = None,
) -> Path:
    if not database_contains_game(game_id, database_path):
        raise PostgameBriefError(
            f"比赛 ID {game_id} 不在 CourtVision V2 详细数据库中，未调用 DeepSeek。"
        )
    if not api_key or not api_key.strip():
        raise PostgameBriefError(
            "缺少 DEEPSEEK_API_KEY。请先配置该环境变量，再重新运行命令。"
        )
    if not analysis_database_path.is_file():
        raise PostgameBriefError(
            f"找不到历史分析数据库：{analysis_database_path}。未调用 DeepSeek。"
        )

    database_hashes_before = {
        database_path: sha256_file(database_path),
        analysis_database_path: sha256_file(analysis_database_path),
    }
    source_hashes_before = _existing_source_hashes(game_id, outputs_dir)

    if fact_generator is None:
        run_fact_generator(game_id)
    else:
        fact_generator(game_id)

    if prediction_audit_generator is None:
        run_prediction_audit_generator(game_id)
    else:
        prediction_audit_generator(game_id)

    for path, hash_before in database_hashes_before.items():
        if sha256_file(path) != hash_before:
            raise PostgameBriefError(
                f"数据库 {path.name} 在事实生成过程中发生变化，未调用 DeepSeek。"
            )
    _assert_existing_sources_unchanged(source_hashes_before)

    facts = load_target_facts(game_id, outputs_dir)
    prediction_audit = load_target_prediction_audit(game_id, outputs_dir)
    validate_fact_consistency(facts, prediction_audit)
    prediction_section = build_prediction_review_section(prediction_audit)
    flow_evidence = build_game_flow_evidence(game_id, facts, database_path)
    flow_section = build_game_flow_section(flow_evidence)
    compact_evidence = build_compact_evidence(facts, prediction_audit, flow_evidence)
    messages = build_messages(compact_evidence)

    if api_client is None:
        raw_response = request_deepseek(
            messages,
            api_key,
            model,
            timeout_seconds,
        )
    else:
        raw_response = api_client(messages, api_key, model, timeout_seconds)

    model_response = validate_model_response(raw_response, game_id, api_key)
    selected_players = validate_selected_players(
        model_response["selected_player_ids"],
        compact_evidence["key_player_candidates"],
        game_id,
        database_path,
    )
    key_players_section = build_key_players_section(selected_players)
    complete_brief = compose_complete_brief(
        prediction_section,
        flow_section,
        model_response["postgame_explanation"],
        key_players_section,
        model_response["data_boundary"],
    )
    checked_brief = validate_complete_brief(complete_brief, game_id, api_key)

    for path, hash_before in database_hashes_before.items():
        if sha256_file(path) != hash_before:
            raise PostgameBriefError(
                f"数据库 {path.name} 在简报生成过程中发生变化，已拒绝保存。"
            )
    _assert_existing_sources_unchanged(source_hashes_before)

    try:
        return atomic_write_brief(game_id, checked_brief, outputs_dir)
    except OSError as exc:
        raise PostgameBriefError(
            "完整简报保存失败，已有有效文件保持不变。"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    model = args.model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")

    try:
        output_path = generate_postgame_brief(
            game_id=args.game_id,
            api_key=api_key,
            model=model,
        )
    except PostgameBriefError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(f"已生成预测验收赛后简报：{output_path.relative_to(PROJECT_DIR)}")
    print(f"使用模型：{model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

