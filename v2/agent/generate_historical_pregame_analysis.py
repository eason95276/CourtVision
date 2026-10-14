"""Generate one historical, leakage-bounded pregame analysis with DeepSeek."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
import re
import socket
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from v2.pregame_system.storage import local_wall_time_to_utc


V2_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = V2_DIR.parent
ANALYSIS_DATABASE_PATH = (
    PROJECT_DIR / "analysis_v1" / "data" / "courtvision_analysis.sqlite"
)
OUTPUTS_DIR = V2_DIR / "outputs"
PREGAME_SNAPSHOT_DATABASE_PATH = (
    V2_DIR / "db" / "courtvision_pregame_snapshots.sqlite"
)
PLAYER_ROLE_EVIDENCE_PATH = (
    V2_DIR
    / "data"
    / "player_evidence"
    / "0022500564_evidence_final_corrected"
    / "player_evidence.json"
)
PLAYER_ROLE_EVIDENCE_MANIFEST_PATH = (
    PLAYER_ROLE_EVIDENCE_PATH.parent / "source_manifest.json"
)

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT_SECONDS = 60.0
PLANNER_MAX_TOKENS = 400
WRITER_MAX_TOKENS = 1000
OPTIONAL_INFORMATION_TYPES = ("injuries", "starters", "market")

FORBIDDEN_TARGET_OUTCOME_KEYS = {
    "home_score",
    "away_score",
    "home_win",
    "point_margin",
    "total_points",
    "actual_home_margin",
    "actual_total",
    "actual_winner",
    "direction_evaluation",
}

FORBIDDEN_WRITER_TERMS = (
    "最终比分",
    "实际比分",
    "赛后复盘",
    "本场赢了",
    "本场输了",
    "击败",
    "不敌",
    "中国体彩官方",
    "投注建议",
    "推荐投注",
    "盈利",
)

INJURY_ROLE_TERMS = (
    "主力",
    "核心",
    "首发",
    "轮换",
)

INJURY_POSITION_TERMS = (
    "后卫",
    "前锋",
    "中锋",
)

INJURY_ADJUSTMENT_REASON_TERMS = (
    "伤病",
    "受伤",
    "伤势",
    "伤缺",
    "缺阵",
    "出赛",
    "出场",
    "上场",
    "出战",
    "参赛",
    "激活名单",
    "人员可用性",
    "Questionable",
    "Out",
)

NON_INJURY_MISLABEL_TERMS = (
    "因伤",
    "受伤",
    "伤病缺阵",
    "伤病影响",
    "伤势",
    "伤缺",
    "伤情",
    "伤患",
)

INJURY_PREDICTION_TREATMENT = "not_separately_adjusted"
MAX_INJURY_ANALYSIS_CANDIDATES = 4
OUTPUT_TAG_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
FORBIDDEN_PLAYER_EVIDENCE_KEYS = FORBIDDEN_TARGET_OUTCOME_KEYS | {
    "actual_score",
    "final_score",
    "final_home_score",
    "final_away_score",
    "postgame",
    "target_outcome",
    "play_by_play",
}


PLANNER_SYSTEM_PROMPT = """你是 CourtVision 历史赛前分析流程的查询规划器。

Python 会固定查询目标比赛的历史回测基础预测、双方赛前特征和截止时间前的最近比赛。你只决定是否还需要检查伤病、首发或市场信息的可用性。

只输出一个 JSON 对象，结构必须是：
{"optional_information_requests":["injuries","starters","market"]}

数组可以为空，只能使用上述三个英文值，不得请求网络搜索、目标比赛结果、当场球员统计、逐回合、赛后复盘或其他工具。当前步骤只决定检查哪些信息，不要生成预测或解释。"""


WRITER_SYSTEM_PROMPT = """你是 CourtVision 的历史单场赛前分析器。

你只能使用用户消息中的 pregame_evidence。它由 Python 通过白名单只读工具生成，目标比赛结果、当场球员统计、逐回合和赛后复盘没有进入上下文。

规则：
1. historical_backtest 基础预测只作参考。你可以根据赛前证据调整主队分差和总分。
2. player_availability_context 是 Python 根据 NBA 官方历史报告生成的完整球员可用性上下文。你必须把所有 Out 理解为报告时确定无法出场，把 Questionable 理解为出赛状态不确定，把 Available 理解为报告时可以出场。不得生成或猜测首发、盘口、新闻、其他球员状态或外部背景。
3. player_role_evidence_context 是同一场比赛的完整球员证据，包含全部十名球员的历史球队分段、出场、分钟、首发、得分、出手、助攻、篮板统计、近期球队比赛窗口、来源、截止时间和缺失说明。必须保留十名球员，不能按详细影响候选数量裁剪。
4. Available 只表示官方报告时可以出场，不代表之后实际登场或进入激活名单。没有出现在报告中的球员也不能据此视为确定出场。长期缺席可能已经反映在近期球队数据中，缺少独立依据时不要重复扣减。
5. 不得使用知识记忆补充事实，不得搜索网络。
6. primary_evidence_refs 只引用 evidence_reference_catalog 中的基础预测、球队赛前指标或此前比赛字段。伤病候选使用 injury_impact_assessments 中的 record_ref 引用。
7. 调整理由只写定性中文，不要在理由中写任何数字，也不要把伤病记录写成分差或总分调整原因。Python 会单独渲染全部数字并重新计算调整量。
8. Out 的原因类型必须依据 reason_classification 区分。G League、Two-Way、Not With Team 等非健康原因不能写成受伤，但对应的 Out 球员仍是报告时确定无法出场。
9. 报告没有提供球员位置，禁止推断后卫、前锋或中锋。角色用语必须由该球员截止日前的首发、分钟和出场频率共同支持，并写成带有赛季、近期或样本边界的使用程度描述；不得把历史记录直接改写为当前确定的主力、核心、首发或轮换身份。
10. injury_analysis_candidates 是全部官方报告记录的可见索引，Python 没有按健康原因或固定人数预筛选。你可以从这十条记录中自行选择零到四条展开详细影响判断，选择顺序由你决定，未选记录仍然属于已查看的完整证据。Questionable 可以用“若无法出场”“如果最终缺席”等条件句讨论情景，同时必须说明出赛仍存疑；不得把条件情景写成确定缺阵。
11. impact_inference 只写影响推断和证据不足，不要复述或补充球员姓名、英文出赛状态、伤病原因、消息来源、网址或报告发布时间。Python 会从候选记录确定性渲染这些事实。
12. player_role_evidence_context 只提供历史使用记录和统计样本，不提供位置、当前角色标签、预计本场上场时间或伤病造成的独立贡献估计。可以据此作有边界的历史使用程度描述；证据不足时只陈述统计事实。球队近期窗口中的未出场记录没有附带原因时，不得自行归因为伤病、轮休、停赛、个人原因或名单原因。每条详细记录的 prediction_treatment 必须为 not_separately_adjusted，不得声称单条记录证明预测应变化具体分值。
13. 单场赛前分析不能证明预测准确率，不得给出投注、盈利或置信等级表述。

只输出一个 JSON 对象，不要使用 Markdown 或代码围栏。字段必须严格为：
{
  "final_predicted_home_margin": 有限数字,
  "final_predicted_total": 有限数字,
  "margin_adjustment_reason": "不含数字且不归因于伤病的简短中文",
  "total_adjustment_reason": "不含数字且不归因于伤病的简短中文",
  "primary_evidence_refs": ["有效非伤病证据字段", "有效非伤病证据字段"],
  "injury_impact_assessments": [
    {
      "record_ref": "injuries.序号",
      "impact_inference": "明确区分报告事实、影响推断和未知项的定性中文",
      "prediction_treatment": "not_separately_adjusted"
    }
  ]
}

主队分差必须位于负八十到正八十之间，总分必须位于一百到四百之间。主要证据引用应有二至六个且不得重复。injury_impact_assessments 可以为空，最多四条；每条 record_ref 必须来自 injury_analysis_candidates，不能重复。"""


class PregameAgentError(RuntimeError):
    """An expected error that is safe to display to a command-line user."""


class DeepSeekRequestError(PregameAgentError):
    """A typed API failure carrying only persistence-safe response evidence."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        safe_summary: str,
        response_received: bool,
        raw_response_text: str | None = None,
        raw_response_sha256: str | None = None,
        raw_response_byte_count: int | None = None,
        raw_response_redacted: bool = False,
        http_status: int | None = None,
        empty_response_kind: str | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.safe_summary = safe_summary
        self.response_received = response_received
        self.raw_response_text = raw_response_text
        self.raw_response_sha256 = raw_response_sha256
        self.raw_response_byte_count = raw_response_byte_count
        self.raw_response_redacted = raw_response_redacted
        self.http_status = http_status
        self.empty_response_kind = empty_response_kind


class ApiResult:
    def __init__(self, content: str, finish_reason: str, usage: dict | None = None,
                 response_metadata: dict | None = None):
        self.content = content
        self.finish_reason = finish_reason
        self.usage = usage or {}
        self.response_metadata = response_metadata or {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成一份严格排除目标比赛赛后数据的历史赛前分析。"
    )
    parser.add_argument("--game-id", required=True, help="历史分析数据库中的比赛 ID。")
    parser.add_argument(
        "--cutoff-time",
        required=True,
        help="赛前截止时间，格式示例：2026-01-13T19:00:00。按数据库来源时间尺度比较。",
    )
    parser.add_argument(
        "--model",
        help=(
            "DeepSeek 模型名。未传入时优先读取 DEEPSEEK_MODEL，"
            f"否则使用 {DEFAULT_MODEL}。"
        ),
    )
    parser.add_argument(
        "--output-tag",
        help=(
            "追加到新产物文件名的版本标记，只允许小写字母、数字、下划线和连字符。"
            "已有同名产物时流程会在调用 DeepSeek 前停止。"
        ),
    )
    parser.add_argument(
        "--audit-input-only",
        action="store_true",
        help=(
            "只生成实际 writer 输入的离线审计文件，不读取密钥、不调用 DeepSeek。"
        ),
    )
    parser.add_argument(
        "--player-evidence-path",
        type=Path,
        default=PLAYER_ROLE_EVIDENCE_PATH,
        help="已保存球员出场证据 JSON 的路径。",
    )
    parser.add_argument(
        "--player-evidence-manifest-path",
        type=Path,
        default=PLAYER_ROLE_EVIDENCE_MANIFEST_PATH,
        help="球员出场证据来源清单的路径。",
    )
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise PregameAgentError(f"找不到历史分析数据库：{path}")
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            connection.close()
            raise PregameAgentError("数据库没有进入只读查询模式。")
        return connection
    except sqlite3.Error as exc:
        raise PregameAgentError("无法以只读模式打开历史分析数据库。") from exc


def connect_snapshot_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise PregameAgentError(f"找不到赛前信息快照数据库：{path}")
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            connection.close()
            raise PregameAgentError("赛前信息快照数据库没有进入只读查询模式。")
        return connection
    except sqlite3.Error as exc:
        raise PregameAgentError("无法以只读模式打开赛前信息快照数据库。") from exc


def load_target_identity(game_id: str, database_path: Path) -> dict:
    query = """
        SELECT
            g.game_id,
            g.game_date AS scheduled_game_time,
            g.game_type,
            g.home_team_id,
            home.team_name AS home_team_name,
            g.away_team_id,
            away.team_name AS away_team_name
        FROM dim_game AS g
        JOIN dim_team AS home ON home.team_id = g.home_team_id
        JOIN dim_team AS away ON away.team_id = g.away_team_id
        WHERE g.game_id = :game_id
    """
    with closing(connect_read_only(database_path)) as connection:
        rows = connection.execute(query, {"game_id": game_id}).fetchall()
    if not rows:
        raise PregameAgentError(f"比赛 {game_id} 不存在于历史分析数据库。")
    if len(rows) != 1:
        raise PregameAgentError(f"比赛 {game_id} 无法唯一确定。")
    return dict(rows[0])


def _parse_naive_datetime(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PregameAgentError(f"{label}不能为空。")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise PregameAgentError(
            f"{label}格式无效，请使用 YYYY-MM-DDTHH:MM:SS。"
        ) from exc
    if parsed.tzinfo is not None:
        raise PregameAgentError(
            f"{label}不能携带时区，因为历史数据库没有可核验的时区字段。"
        )
    return parsed


def validate_cutoff(cutoff_time: str, identity: dict) -> datetime:
    cutoff = _parse_naive_datetime(cutoff_time, "赛前截止时间")
    scheduled = _parse_naive_datetime(identity["scheduled_game_time"], "数据库比赛时间")
    if cutoff >= scheduled:
        raise PregameAgentError(
            "赛前截止时间必须严格早于比赛时间，流程已在调用 DeepSeek 前停止。"
        )
    return cutoff


class PregameReadOnlyTools:
    """A fixed, parameterized whitelist over pregame-safe database columns."""

    def __init__(
        self,
        database_path: Path,
        game_id: str,
        cutoff_time: str,
    ):
        self.database_path = Path(database_path)
        self.game_id = game_id
        self.identity = load_target_identity(game_id, self.database_path)
        self.cutoff = validate_cutoff(cutoff_time, self.identity)
        self.cutoff_sql = self.cutoff.strftime("%Y-%m-%d %H:%M:%S")
        self.trace: list[dict] = []
        self._record(
            "get_target_identity",
            {"game_id": game_id},
            self.identity,
            "required_contract",
        )
        self._validate_frozen_snapshot_cutoff()

    def _record(self, name: str, arguments: dict, result: object, source: str) -> None:
        self.trace.append(
            {
                "sequence": len(self.trace) + 1,
                "tool": name,
                "source": source,
                "read_only": True,
                "arguments": arguments,
                "result": result,
            }
        )

    def _validate_frozen_snapshot_cutoff(self) -> None:
        """Reject a cutoff older than games already included in target-row features."""
        scheduled_sql = self.identity["scheduled_game_time"]
        query = """
            SELECT ftg.game_id, ftg.team_id, ftg.game_date
            FROM fact_team_game AS ftg
            WHERE ftg.team_id IN (:home_team_id, :away_team_id)
              AND ftg.game_id <> :game_id
              AND ftg.game_date >= :cutoff_time
              AND ftg.game_date < :scheduled_game_time
            ORDER BY ftg.game_date, ftg.game_id
        """
        with closing(connect_read_only(self.database_path)) as connection:
            rows = connection.execute(
                query,
                {
                    "home_team_id": self.identity["home_team_id"],
                    "away_team_id": self.identity["away_team_id"],
                    "game_id": self.game_id,
                    "cutoff_time": self.cutoff_sql,
                    "scheduled_game_time": scheduled_sql,
                },
            ).fetchall()
        if rows:
            first = dict(rows[0])
            raise PregameAgentError(
                "截止时间之后仍有球队比赛，而目标行赛前特征可能已经包含这些结果；"
                f"最早冲突比赛为 {first['game_id']}，时间 {first['game_date']}。"
            )
        self._record(
            "validate_frozen_snapshot_as_of_cutoff",
            {
                "cutoff_time": self.cutoff_sql,
                "scheduled_game_time": scheduled_sql,
                "team_ids": [
                    self.identity["home_team_id"],
                    self.identity["away_team_id"],
                ],
            },
            {"intervening_games": 0, "snapshot_usable_at_cutoff": True},
            "required_time_boundary",
        )

    def get_base_prediction(self) -> dict:
        query = """
            SELECT
                p.game_id,
                p.win_probability AS home_win_probability,
                p.predicted_home_win,
                p.predicted_margin AS predicted_home_margin,
                p.predicted_total,
                p.model_scope,
                p.prediction_time_status
            FROM fact_prediction AS p
            WHERE p.game_id = :game_id
        """
        with closing(connect_read_only(self.database_path)) as connection:
            rows = connection.execute(query, {"game_id": self.game_id}).fetchall()
        if len(rows) != 1:
            raise PregameAgentError("目标比赛缺少唯一的历史回测基础预测。")
        result = dict(rows[0])
        result["predicted_home_win"] = bool(result["predicted_home_win"])
        result["prediction_record_type"] = "historical_backtest"
        result["live_pregame_snapshot"] = False
        self._record(
            "get_base_prediction",
            {"game_id": self.game_id},
            result,
            "required_contract",
        )
        return result

    def get_team_pregame_profile(self, team_id: int, side: str) -> dict:
        query = """
            SELECT
                ftg.team_id,
                team.team_name,
                ftg.is_home,
                ftg.pregame_elo,
                ftg.pregame_rest_days,
                ftg.pregame_back_to_back,
                ftg.pregame_games_played_season,
                ftg.pregame_win_rate_roll5,
                ftg.pregame_points_for_roll5,
                ftg.pregame_points_against_roll5,
                ftg.pregame_off_rating_roll5,
                ftg.pregame_def_rating_roll5,
                ftg.pregame_net_rating_roll5,
                ftg.pregame_pace_roll5,
                ftg.pregame_win_rate_roll15,
                ftg.pregame_net_rating_roll15,
                ftg.pregame_pace_roll15
            FROM fact_team_game AS ftg
            JOIN dim_team AS team ON team.team_id = ftg.team_id
            WHERE ftg.game_id = :game_id AND ftg.team_id = :team_id
        """
        with closing(connect_read_only(self.database_path)) as connection:
            rows = connection.execute(
                query,
                {"game_id": self.game_id, "team_id": team_id},
            ).fetchall()
        if len(rows) != 1:
            raise PregameAgentError(f"{side}缺少唯一的赛前球队状态。")
        result = dict(rows[0])
        result["pregame_back_to_back"] = bool(result["pregame_back_to_back"])
        self._record(
            "get_team_pregame_profile",
            {"game_id": self.game_id, "team_id": team_id, "side": side},
            result,
            "required_contract",
        )
        return result

    def get_prior_games(self, team_id: int, side: str, limit: int = 5) -> list[dict]:
        if limit < 1 or limit > 10:
            raise PregameAgentError("历史比赛查询数量必须位于 1 到 10。")
        query = """
            SELECT
                ftg.game_id,
                ftg.game_date,
                opponent.team_name AS opponent_name,
                ftg.is_home,
                ftg.win AS won,
                ftg.points_for AS team_score,
                ftg.points_against AS opponent_score,
                ftg.point_margin AS team_margin
            FROM fact_team_game AS ftg
            JOIN dim_team AS opponent ON opponent.team_id = ftg.opponent_team_id
            WHERE ftg.team_id = :team_id
              AND ftg.game_id <> :game_id
              AND ftg.game_date < :cutoff_time
            ORDER BY ftg.game_date DESC, ftg.game_id DESC
            LIMIT :limit
        """
        with closing(connect_read_only(self.database_path)) as connection:
            rows = connection.execute(
                query,
                {
                    "team_id": team_id,
                    "game_id": self.game_id,
                    "cutoff_time": self.cutoff_sql,
                    "limit": limit,
                },
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["is_home"] = bool(item["is_home"])
            item["won"] = bool(item["won"])
            if item["game_date"] >= self.cutoff_sql or item["game_id"] == self.game_id:
                raise PregameAgentError("历史比赛工具返回了不满足截止时间边界的记录。")
            result.append(item)
        self._record(
            "get_prior_games",
            {
                "team_id": team_id,
                "side": side,
                "strictly_before": self.cutoff_sql,
                "limit": limit,
            },
            result,
            "required_contract",
        )
        return result

    def build_core_evidence(self) -> tuple[dict, list[dict]]:
        base_prediction = self.get_base_prediction()
        home = self.get_team_pregame_profile(
            int(self.identity["home_team_id"]), "home"
        )
        away = self.get_team_pregame_profile(
            int(self.identity["away_team_id"]), "away"
        )
        recent_home = self.get_prior_games(int(self.identity["home_team_id"]), "home")
        recent_away = self.get_prior_games(int(self.identity["away_team_id"]), "away")
        evidence = {
            "game": self.identity,
            "time_boundary": {
                "cutoff_time": self.cutoff.isoformat(timespec="seconds"),
                "scheduled_game_time": self.identity["scheduled_game_time"].replace(" ", "T"),
                "comparison": "cutoff_time < scheduled_game_time",
                "comparison_passed": True,
                "time_scale": "database_source_local_time",
                "source_timezone_status": "unverified",
            },
            "base_prediction": base_prediction,
            "team_pregame_profiles": {"home": home, "away": away},
            "recent_games": {"home": recent_home, "away": recent_away},
        }
        _validate_core_evidence(evidence, self.game_id, self.cutoff_sql)
        return evidence, list(self.trace)


def _validate_core_evidence(evidence: dict, game_id: str, cutoff_sql: str) -> None:
    def walk(value: object, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in FORBIDDEN_TARGET_OUTCOME_KEYS:
                    raise PregameAgentError(
                        "赛前证据包含目标比赛赛后字段：" + ".".join((*path, key))
                    )
                walk(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, (*path, str(index)))

    walk(evidence)
    for side in ("home", "away"):
        for row in evidence["recent_games"][side]:
            if row["game_id"] == game_id or row["game_date"] >= cutoff_sql:
                raise PregameAgentError("赛前历史记录越过目标比赛截止时间。")


def _cutoff_source_local_to_utc(cutoff_time: str) -> str:
    local = _parse_naive_datetime(cutoff_time, "赛前截止时间")
    try:
        aware = local_wall_time_to_utc(local, "America/New_York")
    except ValueError as exc:
        raise PregameAgentError(f"赛前截止时间无法无歧义地转换：{exc}") from exc
    return aware.isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def load_verified_injury_snapshots(
    snapshot_database_path: Path,
    game_id: str,
    cutoff_time: str,
) -> tuple[list[dict], str]:
    cutoff_utc = _cutoff_source_local_to_utc(cutoff_time)
    query = """
        SELECT
            game_id,
            source,
            source_record_id,
            source_published_at,
            captured_at,
            cutoff_time,
            normalized_status,
            team_id,
            team_name,
            player_id,
            player_name,
            availability_status,
            reason,
            source_url
        FROM agent_eligible_injury_snapshots
        WHERE game_id = :game_id
          AND source_published_at <= :cutoff_time
        ORDER BY team_name, player_name, source_record_id
    """
    with closing(connect_snapshot_read_only(snapshot_database_path)) as connection:
        rows = connection.execute(
            query, {"game_id": game_id, "cutoff_time": cutoff_utc}
        ).fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        if row["game_id"] != game_id or row["source_published_at"] > cutoff_utc:
            raise PregameAgentError("伤病快照越过目标比赛或截止时间边界。")
    return result, cutoff_utc


def assess_optional_information(
    requested: list[str],
    *,
    game_id: str | None = None,
    cutoff_time: str | None = None,
    snapshot_database_path: Path = PREGAME_SNAPSHOT_DATABASE_PATH,
) -> dict[str, dict]:
    requested_set = set(requested)
    reasons = {
        "injuries": "当前仓库没有带有 known_at 时间且可证明早于截止时间的伤病记录。",
        "starters": "当前仓库没有带有 known_at 时间且可证明早于截止时间的首发记录。",
        "market": (
            "现有历史市场基准缺少可靠采集时间，且无法确认初盘或终盘；"
            "为防止事后信息进入赛前上下文，数值未向 Agent 暴露。"
        ),
    }
    availability = {
        name: {
            "requested_by_model": name in requested_set,
            "checked_by_program": True,
            "status": "unavailable",
            "reason": reasons[name],
        }
        for name in OPTIONAL_INFORMATION_TYPES
    }
    if game_id is not None and cutoff_time is not None:
        rows, cutoff_utc = load_verified_injury_snapshots(
            Path(snapshot_database_path), game_id, cutoff_time
        )
        if rows:
            availability["injuries"] = {
                "requested_by_model": "injuries" in requested_set,
                "checked_by_program": True,
                "status": "available_historical_archive",
                "reason": (
                    "NBA 官方历史伤病报告标题显示其发布时间早于截止时间；"
                    "原文件在本次历史回放中才采集，因此不代表 CourtVision 当时已持久化。"
                ),
                "cutoff_time_utc": cutoff_utc,
                "records": rows,
            }
    return availability


def _load_json_file(path: Path, label: str) -> dict:
    path = Path(path)
    if not path.is_file():
        raise PregameAgentError(f"找不到{label}：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PregameAgentError(f"无法读取{label}：{path}") from exc
    if not isinstance(value, dict):
        raise PregameAgentError(f"{label}必须是 JSON 对象：{path}")
    return value


def _validate_player_role_raw_manifest(
    evidence_path: Path,
    manifest_path: Path,
    manifest: dict,
    game_id: str,
    cutoff_time: str,
) -> tuple[Path, int]:
    if manifest.get("game_id") != game_id:
        raise PregameAgentError("球员证据来源清单的比赛编号不一致。")
    expected_cutoff_utc = _cutoff_source_local_to_utc(cutoff_time)
    if manifest.get("cutoff_time_utc") != expected_cutoff_utc:
        raise PregameAgentError("球员证据来源清单的截止时间不一致。")
    if manifest.get("credentials_saved") is not False:
        raise PregameAgentError("球员证据来源清单未明确排除凭据保存。")
    requests = manifest.get("requests")
    if not isinstance(requests, list) or not requests:
        raise PregameAgentError("球员证据来源清单缺少原始请求记录。")
    if manifest.get("request_count") != len(requests):
        raise PregameAgentError("球员证据来源清单的请求计数不一致。")
    if manifest.get("success_count") != len(requests) or manifest.get("failure_count") != 0:
        raise PregameAgentError("球员证据来源清单包含失败的原始请求。")

    raw_directory_value = manifest.get("raw_directory", "raw")
    raw_directory = Path(raw_directory_value)
    if not raw_directory.is_absolute():
        raw_directory = evidence_path.parent / raw_directory
    if not raw_directory.is_dir():
        raise PregameAgentError(f"找不到球员证据 raw 目录：{raw_directory}")

    for request in requests:
        if not isinstance(request, dict):
            raise PregameAgentError("球员证据来源清单包含无效请求记录。")
        request_status = request.get("status")
        stored_status = request.get("stored_status", request_status)
        if request_status not in {"success", "cached_no_request"} or stored_status != "success":
            raise PregameAgentError("球员证据来源清单包含未成功保存的响应。")
        raw_name = Path(str(request.get("path", ""))).name
        raw_path = raw_directory / raw_name
        if not raw_name or not raw_path.is_file():
            raise PregameAgentError(f"来源清单引用的 raw 响应不存在：{raw_path}")
        envelope = _load_json_file(raw_path, "raw 响应")
        if envelope.get("status") != "success":
            raise PregameAgentError(f"raw 响应没有成功状态：{raw_path}")
        if envelope.get("response_sha256") != request.get("response_sha256"):
            raise PregameAgentError(f"raw 响应哈希与来源清单不一致：{raw_path}")
    return raw_directory, len(requests)


def _walk_forbidden_player_evidence_keys(value: object, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_PLAYER_EVIDENCE_KEYS:
                raise PregameAgentError(
                    "球员证据包含目标比赛赛后字段：" + ".".join((*path, key))
                )
            _walk_forbidden_player_evidence_keys(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden_player_evidence_keys(child, (*path, str(index)))


def _parse_evidence_date(value: object, label: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise PregameAgentError(f"球员证据缺少{label}。")
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise PregameAgentError(f"球员证据的{label}格式无效：{value}") from exc


def _validate_player_role_evidence_payload(
    player_evidence: dict,
    game_id: str,
    cutoff_time: str,
) -> list[dict]:
    if player_evidence.get("status") != "complete":
        raise PregameAgentError("球员证据状态不是 complete，拒绝送入 Agent。")
    if player_evidence.get("game_id") != game_id:
        raise PregameAgentError("球员证据的比赛编号不一致。")
    cutoff = player_evidence.get("cutoff")
    if not isinstance(cutoff, dict):
        raise PregameAgentError("球员证据缺少截止时间。")
    for cutoff_field in (
        "source_local",
        "source_timezone",
        "utc",
        "target_game_excluded",
        "boundary_rule",
    ):
        if not cutoff.get(cutoff_field):
            raise PregameAgentError(f"球员证据的截止时间缺少 {cutoff_field}。")
    expected_cutoff_utc = _cutoff_source_local_to_utc(cutoff_time)
    if cutoff.get("utc") != expected_cutoff_utc:
        raise PregameAgentError("球员证据的 UTC 截止时间不一致。")
    try:
        requested_cutoff = _parse_naive_datetime(cutoff_time, "赛前截止时间")
        evidence_cutoff = datetime.fromisoformat(
            str(cutoff.get("source_local", "")).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise PregameAgentError("球员证据的本地截止时间格式无效。") from exc
    if evidence_cutoff.tzinfo is None or evidence_cutoff.replace(tzinfo=None) != requested_cutoff:
        raise PregameAgentError("球员证据的本地截止时间不一致。")
    if cutoff.get("target_game_excluded") != game_id:
        raise PregameAgentError("球员证据未明确排除目标比赛。")

    _walk_forbidden_player_evidence_keys(player_evidence)
    players = player_evidence.get("players")
    if not isinstance(players, list) or len(players) != 10:
        raise PregameAgentError("球员证据必须包含全部 10 名球员。")
    cutoff_date = requested_cutoff.date()
    for index, player in enumerate(players):
        if not isinstance(player, dict):
            raise PregameAgentError(f"第 {index} 条球员证据不是对象。")
        for required in (
            "player_name",
            "player_id",
            "report_team",
            "official_availability",
            "current_season",
            "previous_season",
            "recent_team_window",
            "interpretation_boundary",
        ):
            if required not in player:
                raise PregameAgentError(
                    f"球员 {player.get('player_name', index)} 缺少字段 {required}。"
                )
        official = player["official_availability"]
        if not isinstance(official, dict) or official.get("status") not in {
            "Out",
            "Questionable",
            "Available",
        }:
            raise PregameAgentError(f"球员 {player['player_name']} 的官方状态无效。")
        source_published_at = official.get("source_report_time_local")
        if not isinstance(source_published_at, str):
            raise PregameAgentError(f"球员 {player['player_name']} 缺少报告时间。")
        try:
            report_time = datetime.fromisoformat(source_published_at)
        except ValueError as exc:
            raise PregameAgentError(
                f"球员 {player['player_name']} 的报告时间格式无效。"
            ) from exc
        if report_time.tzinfo is None or report_time.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") > expected_cutoff_utc:
            raise PregameAgentError(
                f"球员 {player['player_name']} 的官方报告时间越过截止时间。"
            )

        current = player["current_season"]
        if not isinstance(current, dict):
            raise PregameAgentError(f"球员 {player['player_name']} 的当前赛季证据无效。")
        previous = player["previous_season"]
        for season_label, season_data in (
            ("当前赛季", current),
            ("上赛季", previous),
        ):
            if not isinstance(season_data.get("team_stints"), list):
                raise PregameAgentError(
                    f"球员 {player['player_name']} 的{season_label}球队分段无效。"
                )
            if not isinstance(season_data.get("source"), dict) or "missingness" not in season_data:
                raise PregameAgentError(
                    f"球员 {player['player_name']} 的{season_label}证据缺少来源或缺失说明。"
                )
        for season_label, season_data in (
            ("当前赛季", current),
            ("上赛季", previous),
        ):
            for stint in season_data["team_stints"]:
                if not isinstance(stint, dict):
                    raise PregameAgentError(
                        f"球员 {player['player_name']} 的{season_label}球队分段无效。"
                    )
                required_stint_fields = (
                    "team_id",
                    "team_tricode",
                    "team_name",
                    "games_played",
                    "starts",
                    "minutes_per_appearance",
                    "points_per_appearance",
                    "field_goals_attempted_per_appearance",
                    "assists_per_appearance",
                    "rebounds_per_appearance",
                    "sample",
                    "missingness",
                )
                missing_stint_fields = [
                    field for field in required_stint_fields if field not in stint
                ]
                if missing_stint_fields:
                    raise PregameAgentError(
                        f"球员 {player['player_name']} 的{season_label}球队分段缺少字段："
                        + ",".join(missing_stint_fields)
                    )
                sample = stint.get("sample", {})
                if not isinstance(sample, dict):
                    raise PregameAgentError(
                        f"球员 {player['player_name']} 的{season_label}样本说明无效。"
                    )
                for date_key in ("first_game_date", "last_game_date"):
                    if sample.get(date_key) is not None and _parse_evidence_date(
                        sample[date_key], f"{player['player_name']} {season_label} {date_key}"
                    ) >= cutoff_date:
                        raise PregameAgentError(
                            f"球员 {player['player_name']} 的{season_label}统计越过截止日期。"
                        )

        recent = player["recent_team_window"]
        if not isinstance(recent, dict) or recent.get("team_window_games_returned") != 10:
            raise PregameAgentError(
                f"球员 {player['player_name']} 的最近球队窗口不是 10 场。"
            )
        games = recent.get("games")
        if not isinstance(games, list) or len(games) != 10:
            raise PregameAgentError(
                f"球员 {player['player_name']} 缺少完整最近 10 场明细。"
            )
        if not isinstance(recent.get("source"), dict) or "missingness" not in recent:
            raise PregameAgentError(
                f"球员 {player['player_name']} 的最近球队窗口缺少来源或缺失说明。"
            )
        for game in games:
            required_game_fields = (
                "game_id",
                "game_date",
                "appearance",
                "minutes",
                "minutes_decimal",
                "starter_record",
                "missingness",
            )
            missing_game_fields = [
                field for field in required_game_fields if field not in game
            ]
            if missing_game_fields:
                raise PregameAgentError(
                    f"球员 {player['player_name']} 的最近球队窗口记录缺少字段："
                    + ",".join(missing_game_fields)
                )
            if game.get("game_id") == game_id:
                raise PregameAgentError(
                    f"球员 {player['player_name']} 的近期窗口包含目标比赛。"
                )
            if _parse_evidence_date(
                game.get("game_date"), f"{player['player_name']} 最近比赛日期"
            ) >= cutoff_date:
                raise PregameAgentError(
                    f"球员 {player['player_name']} 的近期窗口越过截止日期。"
                )
    return players


def _build_player_role_evidence_source(
    evidence_path: Path,
    manifest_path: Path,
    manifest: dict,
    raw_directory: Path,
    raw_artifact_count: int,
) -> dict:
    return {
        "evidence_json_path": str(Path(evidence_path).resolve()),
        "evidence_json_sha256": sha256_file(Path(evidence_path)),
        "source_manifest_path": str(Path(manifest_path).resolve()),
        "source_manifest_sha256": sha256_file(Path(manifest_path)),
        "raw_directory": str(raw_directory.resolve()),
        "raw_artifact_count": raw_artifact_count,
        "raw_response_hashes_verified": True,
        "official_data_provider": manifest.get("official_data_provider"),
        "client": deepcopy(manifest.get("client", {})),
        "capture_relation_to_cutoff": manifest.get("capture_relation_to_cutoff"),
        "captured_at_utc": manifest.get("captured_at_utc"),
        "formal_output_sha256": deepcopy(manifest.get("formal_output_sha256", {})),
        "credentials_saved": False,
    }


def build_player_role_evidence_context(
    player_evidence: dict,
    injury_records: list[dict],
    game_id: str,
    cutoff_time: str,
) -> dict:
    players = _validate_player_role_evidence_payload(
        player_evidence, game_id, cutoff_time
    )
    by_key: dict[tuple[str, int], dict] = {}
    for player in players:
        report_team = player.get("report_team") or {}
        try:
            key = (str(player["player_name"]).strip(), int(report_team["team_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PregameAgentError("球员证据缺少可核对的报告球队。") from exc
        if key in by_key:
            raise PregameAgentError(f"球员证据身份重复：{key[0]}。")
        by_key[key] = player

    context_players: list[dict] = []
    expected_keys: set[tuple[str, int]] = set()
    for index, record in enumerate(injury_records):
        try:
            key = (str(record["player_name"]).strip(), int(record["team_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PregameAgentError("伤病可用性记录缺少球员或球队身份。") from exc
        expected_keys.add(key)
        player = by_key.get(key)
        if player is None:
            raise PregameAgentError(f"球员证据缺少官方报告球员：{key[0]}。")
        official = player["official_availability"]
        for field, expected in (
            ("status", record.get("availability_status")),
            ("reason", record.get("reason")),
            ("source_url", record.get("source_url")),
        ):
            if official.get(field) != expected:
                raise PregameAgentError(
                    f"球员 {key[0]} 的角色证据与官方可用性记录不一致：{field}。"
                )
        item = deepcopy(player)
        item["record_ref"] = f"injuries.{index}"
        item["official_report_record"] = {
            "source_record_id": record.get("source_record_id"),
            "availability_status": record.get("availability_status"),
            "reason": record.get("reason"),
            "source": record.get("source"),
            "source_url": record.get("source_url"),
            "source_published_at": record.get("source_published_at"),
            "captured_at": record.get("captured_at"),
        }
        context_players.append(item)
    if expected_keys != set(by_key):
        extra = sorted(set(by_key) - expected_keys)
        raise PregameAgentError(f"球员证据包含不在官方报告中的人员：{extra}。")
    return {
        "artifact_type": player_evidence.get("artifact_type"),
        "schema_version": player_evidence.get("schema_version"),
        "status": player_evidence.get("status"),
        "game_id": game_id,
        "cutoff": deepcopy(player_evidence["cutoff"]),
        "historical_acquisition": deepcopy(
            player_evidence.get("historical_acquisition", {})
        ),
        "injury_report": deepcopy(player_evidence.get("injury_report", {})),
        "source_boundary": deepcopy(player_evidence.get("source_boundary", {})),
        "players": context_players,
        "limitations": deepcopy(player_evidence.get("limitations", [])),
    }


def load_player_role_evidence(
    evidence_path: Path,
    manifest_path: Path,
    injury_records: list[dict],
    game_id: str,
    cutoff_time: str,
) -> tuple[dict, dict]:
    evidence_path = Path(evidence_path)
    manifest_path = Path(manifest_path)
    player_evidence = _load_json_file(evidence_path, "球员出场证据")
    manifest = _load_json_file(manifest_path, "球员出场证据来源清单")
    raw_directory, raw_artifact_count = _validate_player_role_raw_manifest(
        evidence_path, manifest_path, manifest, game_id, cutoff_time
    )
    context = build_player_role_evidence_context(
        player_evidence, injury_records, game_id, cutoff_time
    )
    source = _build_player_role_evidence_source(
        evidence_path, manifest_path, manifest, raw_directory, raw_artifact_count
    )
    return context, source


def build_planner_messages(identity: dict, cutoff: datetime) -> list[dict[str, str]]:
    safe_input = {
        "game_id": identity["game_id"],
        "home_team_name": identity["home_team_name"],
        "away_team_name": identity["away_team_name"],
        "scheduled_game_time": identity["scheduled_game_time"],
        "cutoff_time": cutoff.isoformat(timespec="seconds"),
        "available_optional_checks": list(OPTIONAL_INFORMATION_TYPES),
    }
    return [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(safe_input, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def validate_planner_response(content: str) -> list[str]:
    try:
        value = json.loads(content.strip())
    except (AttributeError, json.JSONDecodeError) as exc:
        raise PregameAgentError("DeepSeek 查询计划不是有效 JSON。") from exc
    if not isinstance(value, dict) or set(value) != {"optional_information_requests"}:
        raise PregameAgentError("DeepSeek 查询计划字段不符合固定结构。")
    requested = value["optional_information_requests"]
    if not isinstance(requested, list) or len(requested) > len(OPTIONAL_INFORMATION_TYPES):
        raise PregameAgentError("DeepSeek 查询计划中的可选信息数量无效。")
    if any(not isinstance(item, str) or item not in OPTIONAL_INFORMATION_TYPES for item in requested):
        raise PregameAgentError("DeepSeek 请求了不允许的工具或信息类型。")
    if len(set(requested)) != len(requested):
        raise PregameAgentError("DeepSeek 查询计划包含重复请求。")
    return requested


def _evidence_reference_catalog(evidence: dict) -> dict[str, object]:
    catalog: dict[str, object] = {}
    for key, value in evidence["base_prediction"].items():
        if key != "game_id":
            catalog[f"base.{key}"] = value
    for side in ("home", "away"):
        for key, value in evidence["team_pregame_profiles"][side].items():
            catalog[f"{side}.{key}"] = value
        for index, game in enumerate(evidence["recent_games"][side]):
            for key, value in game.items():
                catalog[f"recent.{side}.{index}.{key}"] = value
    return catalog


def build_writer_messages(
    evidence: dict,
    availability: dict,
    injury_candidates: list[dict] | None = None,
    player_role_evidence_context: dict | None = None,
    player_role_evidence_source: dict | None = None,
) -> list[dict[str, str]]:
    if player_role_evidence_context is None:
        raise PregameAgentError("Agent 输入缺少完整球员角色证据。")
    if player_role_evidence_source is None:
        raise PregameAgentError("球员角色证据缺少来源清单元数据。")
    if len(player_role_evidence_context.get("players", [])) != 10:
        raise PregameAgentError("Agent 输入的球员角色证据必须包含全部 10 名球员。")
    injuries = availability.get("injuries", {}).get("records", [])
    candidates = build_injury_analysis_candidates(injuries)
    if injury_candidates is not None:
        provided_refs = [item.get("record_ref") for item in injury_candidates]
        complete_refs = [item["record_ref"] for item in candidates]
        if provided_refs != complete_refs:
            raise PregameAgentError(
                "Agent 输入阶段不得预筛选官方报告记录，必须保留全部记录。"
            )
    availability_context = build_player_availability_context(injuries)
    availability_summary = summarize_player_availability(availability_context)
    core_evidence = {
        key: value
        for key, value in evidence.items()
        if key != "verified_pregame_injuries"
    }
    safe_availability = {
        name: {
            key: value
            for key, value in item.items()
            if key != "records"
        }
        for name, item in availability.items()
    }
    if "injuries" in safe_availability:
        safe_availability["injuries"]["record_count"] = len(injuries)
    writer_evidence = {
        **core_evidence,
        "injury_fact_record_count": len(injuries),
        "player_availability_summary": availability_summary,
        "player_availability_context": availability_context,
        "injury_analysis_candidates": candidates,
        "injury_analysis_selection_policy": (
            "Agent 从全部官方报告记录中自行选择详细分析对象；未选记录仍在完整输入中。"
        ),
        "injury_analysis_detail_limit": MAX_INJURY_ANALYSIS_CANDIDATES,
        "optional_information_availability": safe_availability,
        "evidence_reference_catalog": _evidence_reference_catalog(evidence),
        "explicit_exclusions": [
            "目标比赛结果",
            "目标比赛球员统计",
            "目标比赛逐回合",
            "赛后复盘",
            "缺少截止时间证据的伤病、首发和盘口信息",
            "未出现在官方报告中的球员不能据此视为确定出场或进入激活名单",
        ],
    }
    writer_evidence["player_role_evidence_context"] = deepcopy(
        player_role_evidence_context
    )
    writer_evidence["player_role_evidence_source"] = deepcopy(
        player_role_evidence_source
    )
    serialized = json.dumps(
        writer_evidence,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return [
        {"role": "system", "content": WRITER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"<pregame_evidence>\n{serialized}\n</pregame_evidence>",
        },
    ]


def _safe_raw_response_evidence(raw_response: bytes, api_key: str) -> dict:
    text = raw_response.decode("utf-8", errors="replace")
    sanitized = text
    redacted = False
    if api_key and api_key in sanitized:
        sanitized = sanitized.replace(api_key, "[REDACTED_API_KEY]")
        redacted = True
    authorization_pattern = re.compile(
        r'(?i)(["\']?authorization["\']?\s*[:=]\s*["\']?)(bearer\s+)?([^\s,"\'}]+)'
    )
    sanitized, replacement_count = authorization_pattern.subn(
        "[REDACTED_CREDENTIAL_HEADER]", sanitized
    )
    redacted = redacted or replacement_count > 0
    return {
        "text": sanitized,
        "sha256": sha256_bytes(raw_response),
        "byte_count": len(raw_response),
        "credential_redaction_applied": redacted,
    }


def _request_deepseek_impl(
    messages: list[dict[str, str]],
    api_key: str,
    model: str,
    max_tokens: int,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable | None = None,
) -> ApiResult:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
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
        try:
            error_body = exc.read()
        except Exception:
            error_body = None
        raw_evidence = (
            _safe_raw_response_evidence(error_body, api_key)
            if isinstance(error_body, bytes)
            else {}
        )
        raise DeepSeekRequestError(
            f"DeepSeek 返回 HTTP {exc.code}，没有生成新分析。",
            error_type="http_error",
            safe_summary=f"DeepSeek HTTP status {exc.code}",
            response_received=True,
            raw_response_text=raw_evidence.get("text"),
            raw_response_sha256=raw_evidence.get("sha256"),
            raw_response_byte_count=raw_evidence.get("byte_count"),
            raw_response_redacted=bool(
                raw_evidence.get("credential_redaction_applied", False)
            ),
            http_status=int(exc.code),
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise DeepSeekRequestError(
            "DeepSeek 请求超时或网络不可用，没有生成新分析。",
            error_type="network_error",
            safe_summary="DeepSeek network request failed before an HTTP response was received",
            response_received=False,
        ) from exc
    raw_evidence = _safe_raw_response_evidence(raw_response, api_key)
    if not raw_response:
        raise DeepSeekRequestError(
            "DeepSeek 返回空 HTTP 响应正文，没有生成新分析。",
            error_type="empty_response",
            safe_summary="DeepSeek returned an empty HTTP response body",
            response_received=True,
            raw_response_text=raw_evidence["text"],
            raw_response_sha256=raw_evidence["sha256"],
            raw_response_byte_count=raw_evidence["byte_count"],
            raw_response_redacted=raw_evidence["credential_redaction_applied"],
            empty_response_kind="http_body_empty",
        )
    try:
        response_data = json.loads(raw_response.decode("utf-8"))
        choice = response_data["choices"][0]
        content = choice["message"]["content"]
        finish_reason = choice.get("finish_reason", "unknown")
        usage = response_data.get("usage", {})
    except (UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise DeepSeekRequestError(
            "DeepSeek 返回的数据格式无效，没有生成新分析。",
            error_type="response_json_error",
            safe_summary="DeepSeek HTTP response could not be parsed as the required response JSON",
            response_received=True,
            raw_response_text=raw_evidence["text"],
            raw_response_sha256=raw_evidence["sha256"],
            raw_response_byte_count=raw_evidence["byte_count"],
            raw_response_redacted=raw_evidence["credential_redaction_applied"],
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekRequestError(
            "DeepSeek 返回正文为空，"
            f"finish_reason={finish_reason}，没有生成新分析。",
            error_type="empty_response",
            safe_summary=f"DeepSeek returned empty model content; finish_reason={finish_reason}",
            response_received=True,
            raw_response_text=raw_evidence["text"],
            raw_response_sha256=raw_evidence["sha256"],
            raw_response_byte_count=raw_evidence["byte_count"],
            raw_response_redacted=raw_evidence["credential_redaction_applied"],
            empty_response_kind="model_content_empty",
        )
    safe_usage = {
        key: int(value)
        for key, value in usage.items()
        if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
        and isinstance(value, int)
        and value >= 0
    }
    return ApiResult(content.strip(), str(finish_reason), safe_usage, {
        key: response_data[key] for key in ("id", "model", "created", "system_fingerprint")
        if isinstance(response_data.get(key), (str, int))
    })


def request_deepseek(messages, api_key, model, max_tokens,
                     timeout_seconds=DEFAULT_TIMEOUT_SECONDS, opener=None):
    # Dormant without an explicitly scoped production run. Historical Evaluation
    # receives the original parser/result and never gains billing side effects.
    from v2.pregame_system.cost_ledger import metered_deepseek
    return metered_deepseek(lambda observed_opener: _request_deepseek_impl(
        messages, api_key, model, max_tokens, timeout_seconds, observed_opener), model, opener)


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PregameAgentError(f"DeepSeek 返回的{label}不是有效数字。")
    number = float(value)
    if not math.isfinite(number):
        raise PregameAgentError(f"DeepSeek 返回的{label}不是有限数字。")
    return number


def classify_injury_record(record: dict) -> str:
    status = str(record.get("availability_status", "")).strip().casefold()
    reason = str(record.get("reason", "")).strip().casefold()
    if status == "questionable":
        return "questionable_availability"
    if status == "available":
        return "confirmed_available"
    if status == "out":
        if reason.startswith("injury/illness") or "concussion protocol" in reason:
            return "health_related_out"
        return "non_injury_out"
    return "other_reported_status"


AVAILABILITY_GROUP_LABELS = {
    "confirmed_out": "报告时确定无法出场",
    "questionable": "报告时出赛状态不确定",
    "available": "报告时可以出场",
    "other_reported_status": "其他官方报告状态",
}


AVAILABILITY_REASON_LABELS = {
    "injury_illness": "伤病或疾病",
    "concussion_protocol": "脑震荡协议",
    "g_league_two_way": "G League 与 Two-Way 名单原因",
    "g_league": "G League 名单原因",
    "two_way": "Two-Way 名单原因",
    "not_with_team": "不随队",
    "other_explicit_reason": "其他明确报告原因",
    "reason_not_reported": "报告未提供原因",
}


def classify_availability_group(record: dict) -> str:
    status = str(record.get("availability_status", "")).strip().casefold()
    if status == "out":
        return "confirmed_out"
    if status == "questionable":
        return "questionable"
    if status == "available":
        return "available"
    return "other_reported_status"


def classify_availability_reason(record: dict) -> str:
    reason = str(record.get("reason", "")).strip().casefold()
    if "concussion protocol" in reason:
        return "concussion_protocol"
    if reason.startswith("injury/illness"):
        return "injury_illness"
    if "g league" in reason and "two-way" in reason:
        return "g_league_two_way"
    if "g league" in reason:
        return "g_league"
    if "two-way" in reason:
        return "two_way"
    if "not with team" in reason:
        return "not_with_team"
    if reason:
        return "other_explicit_reason"
    return "reason_not_reported"


def build_player_availability_context(records: list[dict]) -> list[dict]:
    context: list[dict] = []
    for index, record in enumerate(records):
        availability_group = classify_availability_group(record)
        reason_classification = classify_availability_reason(record)
        context.append(
            {
                "record_ref": f"injuries.{index}",
                "team_name": record["team_name"],
                "player_name": record["player_name"],
                "availability_status": record["availability_status"],
                "availability_group": availability_group,
                "availability_group_label": AVAILABILITY_GROUP_LABELS[
                    availability_group
                ],
                "reason": record["reason"],
                "reason_classification": reason_classification,
                "reason_classification_label": AVAILABILITY_REASON_LABELS[
                    reason_classification
                ],
                "is_health_related_reason": reason_classification
                in {"injury_illness", "concussion_protocol"},
                "source": record["source"],
                "source_url": record["source_url"],
                "source_published_at": record["source_published_at"],
                "captured_at": record["captured_at"],
            }
        )
    return context


def summarize_player_availability(context: list[dict]) -> dict:
    grouped_refs = {
        group: [
            item["record_ref"]
            for item in context
            if item["availability_group"] == group
        ]
        for group in (
            "confirmed_out",
            "questionable",
            "available",
            "other_reported_status",
        )
    }
    confirmed_out = [
        item for item in context if item["availability_group"] == "confirmed_out"
    ]
    health_related_out_count = sum(
        1 for item in confirmed_out if item["is_health_related_reason"]
    )
    return {
        "total_records": len(context),
        "confirmed_out_count": len(grouped_refs["confirmed_out"]),
        "questionable_count": len(grouped_refs["questionable"]),
        "available_count": len(grouped_refs["available"]),
        "other_reported_status_count": len(grouped_refs["other_reported_status"]),
        "health_related_out_count": health_related_out_count,
        "non_health_related_out_count": len(confirmed_out)
        - health_related_out_count,
        "confirmed_out_refs": grouped_refs["confirmed_out"],
        "questionable_refs": grouped_refs["questionable"],
        "available_refs": grouped_refs["available"],
        "other_reported_status_refs": grouped_refs["other_reported_status"],
    }


def build_injury_analysis_candidates(records: list[dict]) -> list[dict]:
    """Expose every official report record to the Agent.

    The name is retained for the existing output schema, but this function
    deliberately performs no health or fixed-count prefilter.  The Agent may
    select a small subset for detailed prose; the complete list remains in its
    input and is validated separately from the final response.
    """
    candidates: list[dict] = []
    for index, record in enumerate(records):
        classification = classify_injury_record(record)
        candidate = {
            "record_ref": f"injuries.{index}",
            "team_name": record["team_name"],
            "player_name": record["player_name"],
            "availability_status": record["availability_status"],
            "reason": record["reason"],
            "fact_classification": classification,
            "availability_group": classify_availability_group(record),
            "reason_classification": classify_availability_reason(record),
            "selection_scope": "all_reported_records_visible_to_agent",
            "source": record["source"],
            "source_url": record["source_url"],
            "source_published_at": record["source_published_at"],
            "captured_at": record["captured_at"],
        }
        candidates.append(candidate)
    return candidates


INJURY_FACT_CLASSIFICATION_LABELS = {
    "questionable_availability": "出赛存疑，不能视为确定缺阵",
    "confirmed_available": "报告时可出赛",
    "health_related_out": "健康相关原因的确定缺阵",
    "non_injury_out": "非伤病原因的确定不可出赛状态",
    "other_reported_status": "其他原始报告状态",
}


def _injury_record_for_ref(reference: object, evidence: dict) -> tuple[int, dict]:
    if not isinstance(reference, str):
        raise PregameAgentError("伤病影响判断缺少有效记录引用。")
    match = re.fullmatch(r"injuries\.(\d+)", reference)
    if not match:
        raise PregameAgentError("伤病影响判断必须引用完整的 injuries.序号 记录。")
    index = int(match.group(1))
    records = evidence.get("verified_pregame_injuries", [])
    if index >= len(records):
        raise PregameAgentError("伤病影响判断引用了不存在的记录。")
    return index, records[index]


def _validate_generated_text(
    value: object,
    label: str,
    *,
    minimum: int = 10,
    maximum: int = 180,
    reject_unscoped_role_language: bool = True,
) -> str:
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise PregameAgentError(f"DeepSeek 返回的{label}为空或长度异常。")
    text = value.strip()
    if re.search(r"[0-9]", text):
        raise PregameAgentError(f"{label}包含未由 Python 渲染的数字。")
    if any(term in text for term in FORBIDDEN_WRITER_TERMS):
        raise PregameAgentError(f"{label}包含目标比赛赛后信息或禁止表述。")
    if reject_unscoped_role_language and any(
        term in text for term in (*INJURY_ROLE_TERMS, *INJURY_POSITION_TERMS)
    ):
        raise PregameAgentError(f"{label}包含伤病报告没有提供的位置或角色推断。")
    return text


def _validate_injury_inference_fact_boundaries(
    inference: str,
    evidence: dict,
) -> None:
    known_player_names = {
        str(record.get("player_name", "")).strip()
        for record in evidence.get("verified_pregame_injuries", [])
        if str(record.get("player_name", "")).strip()
    }
    english_full_name = re.search(
        r"\b[A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+)+\b", inference
    )
    if any(name in inference for name in known_player_names) or english_full_name:
        raise PregameAgentError(
            "伤病影响推断不得生成或复述球员事实，球员姓名由 Python 渲染。"
        )
    if re.search(r"\b(?:Out|Questionable|Available)\b", inference, re.IGNORECASE):
        raise PregameAgentError(
            "伤病影响推断不得生成或复述英文出赛状态，状态由 Python 渲染。"
        )
    if any(
        term in inference
        for term in (
            "ESPN",
            "Shams",
            "记者",
            "媒体",
            "来源",
            "网址",
            "据报道",
            "据消息",
            "官方报道",
            "http://",
            "https://",
            "www.",
        )
    ):
        raise PregameAgentError(
            "伤病影响推断不得生成外部来源或网址，来源事实由 Python 渲染。"
        )
    if re.search(r"[零一二两三四五六七八九十百]+(?:点[零一二三四五六七八九]+)?分", inference):
        raise PregameAgentError(
            "伤病影响推断不得声称单条记录证明具体分数影响。"
        )


QUESTIONABLE_ABSENCE_PHRASE = (
    r"(?:确定缺阵|确认缺阵|已经缺阵|已确认缺阵|必然缺阵|"
    r"无法出场|不会出场|不能出场|无法出赛|不会出赛|"
    r"不能出赛|未能出场|未能出赛|未能出战|不会出战|缺席|缺阵)"
)
QUESTIONABLE_CONDITIONAL_ABSENCE_PATTERN = re.compile(
    rf"(?:若|如果|假如|倘若|一旦|若是|假设|即便|即使)"
    rf"(?:(?![，,；;。！？!?]).){{0,30}}?{QUESTIONABLE_ABSENCE_PHRASE}"
)
QUESTIONABLE_CONCESSIVE_DEFINITE_PATTERN = re.compile(
    rf"(?:即便|即使)(?:(?![，,；;。！？!?]).){{0,30}}?[，,]?"
    rf"(?:也|仍|还是)(?:(?![，,；;。！？!?]).){{0,12}}?"
    rf"{QUESTIONABLE_ABSENCE_PHRASE}"
)
QUESTIONABLE_DEFINITE_PREFIX_PATTERN = re.compile(
    rf"(?:将|会|必然|肯定|确定|确认|已经|已|注定|预计)"
    rf"(?:会|将)?(?:(?![，,；;。！？!?]).){{0,12}}?{QUESTIONABLE_ABSENCE_PHRASE}"
)
QUESTIONABLE_BARE_DEFINITE_PATTERN = re.compile(
    r"(?:无法出场|不会出场|不能出场|无法出赛|不会出赛|"
    r"不能出赛|不会出战|确定缺阵|确认缺阵|已经缺阵|已确认缺阵|必然缺阵)"
)
QUESTIONABLE_UNCERTAINTY_TERMS = (
    "存疑",
    "不确定",
    "尚未确定",
    "仍待确认",
    "无法确认",
    "能否出赛",
    "能否出场",
)


def _questionable_contains_definite_absence(inference: str) -> bool:
    if QUESTIONABLE_CONCESSIVE_DEFINITE_PATTERN.search(inference):
        return True
    without_conditionals = QUESTIONABLE_CONDITIONAL_ABSENCE_PATTERN.sub(
        "[条件缺席场景]", inference
    )
    return bool(
        QUESTIONABLE_DEFINITE_PREFIX_PATTERN.search(without_conditionals)
        or QUESTIONABLE_BARE_DEFINITE_PATTERN.search(without_conditionals)
    )


def _player_evidence_for_record_ref(
    record_ref: str,
    player_role_evidence_context: dict | None,
) -> dict | None:
    if not isinstance(player_role_evidence_context, dict):
        return None
    players = player_role_evidence_context.get("players")
    if not isinstance(players, list):
        return None
    matches = [
        player
        for player in players
        if isinstance(player, dict) and player.get("record_ref") == record_ref
    ]
    return matches[0] if len(matches) == 1 else None


def _usage_metrics(player_evidence: dict | None) -> dict:
    if not isinstance(player_evidence, dict):
        return {}
    current = player_evidence.get("current_season")
    recent = player_evidence.get("recent_team_window")
    if not isinstance(current, dict) or not isinstance(recent, dict):
        return {}
    stints = current.get("team_stints")
    report_team = player_evidence.get("report_team")
    report_team_id = (
        report_team.get("team_id") if isinstance(report_team, dict) else None
    )
    stint_list = stints if isinstance(stints, list) else []
    matching_stints = [
        stint
        for stint in stint_list
        if isinstance(stint, dict)
        and stint.get("team_id") == report_team_id
    ]
    stint = matching_stints[0] if len(matching_stints) == 1 else {}
    if not isinstance(stint, dict):
        stint = {}
    games = stint.get("games_played")
    starts = stint.get("starts")
    minutes = stint.get("minutes_per_appearance")
    points = stint.get("points_per_appearance")
    recent_appearances = recent.get("appearance_count_in_recent_team_window")
    recent_minutes = recent.get("minutes_average_when_appeared")
    numeric = (games, starts, minutes, recent_appearances, recent_minutes)
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in numeric
    ):
        return {}
    return {
        "games": float(games),
        "starts": float(starts),
        "minutes": float(minutes),
        "points": float(points) if isinstance(points, (int, float)) else None,
        "recent_appearances": float(recent_appearances),
        "recent_minutes": float(recent_minutes),
        "start_rate": float(starts) / float(games) if games else 0.0,
    }


def _has_bounded_usage_wording(inference: str) -> bool:
    evidence_anchors = (
        "本赛季",
        "近期",
        "样本",
        "记录",
        "出场频率",
        "分钟",
        "首发",
    )
    boundary_terms = (
        "使用程度",
        "使用记录",
        "出场负荷",
        "呈现",
        "接近",
        "可能",
        "样本",
    )
    identity_assertion = re.search(
        r"(?:是|属于|作为|确认|确定)(?:球队|当前)?(?:的)?"
        r"(?:主力|核心|重要轮换|轮换球员|首发球员)",
        inference,
    )
    return (
        any(term in inference for term in evidence_anchors)
        and any(term in inference for term in boundary_terms)
        and identity_assertion is None
        and not any(term in inference for term in ("预计首发", "本场首发", "将首发"))
    )


def _role_term_supported(term: str, metrics: dict, player_evidence: dict) -> bool:
    explicit_roles = player_evidence.get("explicit_role_evidence")
    if isinstance(explicit_roles, list) and term in explicit_roles:
        return True
    if not metrics:
        return False
    if term == "首发":
        return metrics["starts"] > 0
    if term == "轮换":
        return (
            metrics["games"] >= 20
            and metrics["minutes"] >= 18
            and metrics["recent_appearances"] >= 7
            and metrics["recent_minutes"] >= 18
        )
    if term == "主力":
        return (
            metrics["games"] >= 20
            and metrics["start_rate"] >= 0.6
            and metrics["minutes"] >= 24
            and metrics["recent_appearances"] >= 5
        )
    if term == "核心":
        return (
            metrics["games"] >= 20
            and metrics["minutes"] >= 28
            and metrics["recent_appearances"] >= 5
            and metrics.get("points") is not None
            and metrics["points"] >= 18
        )
    return False


def _validate_role_inference(
    inference: str,
    record_ref: str,
    player_role_evidence_context: dict | None,
) -> None:
    used_role_terms = [term for term in INJURY_ROLE_TERMS if term in inference]
    used_position_terms = [term for term in INJURY_POSITION_TERMS if term in inference]
    if not used_role_terms and not used_position_terms:
        return
    player_evidence = _player_evidence_for_record_ref(
        record_ref, player_role_evidence_context
    )
    if used_position_terms:
        position_evidence = (
            player_evidence.get("explicit_position_evidence")
            if isinstance(player_evidence, dict)
            else None
        )
        if not isinstance(position_evidence, list) or any(
            term not in position_evidence for term in used_position_terms
        ):
            raise PregameAgentError(
                "伤病影响推断包含缺少球员位置证据的位置或角色推断。"
            )
    if used_role_terms:
        if not isinstance(player_evidence, dict) or not _has_bounded_usage_wording(
            inference
        ):
            raise PregameAgentError(
                "伤病影响推断包含缺少有界使用证据的位置或角色推断。"
            )
        metrics = _usage_metrics(player_evidence)
        unsupported = [
            term
            for term in used_role_terms
            if not _role_term_supported(term, metrics, player_evidence)
        ]
        if unsupported:
            raise PregameAgentError(
                "伤病影响推断包含当前出场、分钟或首发样本不足以支持的"
                f"位置或角色推断：{','.join(unsupported)}。"
            )


HISTORICAL_ABSENCE_TIME_PATTERN = re.compile(
    r"(?:近期|最近|此前|过去|历史|上一场|前一场|连续|先前)"
)
UNSUPPORTED_ABSENCE_CAUSE_PATTERN = re.compile(
    r"(?:因(?:为)?(?:伤|伤病|轮休|停赛|个人原因|名单原因)"
    r"(?:(?![，,；;。！？!?]).){0,8}?(?:缺阵|缺席|未出场|未出战)|"
    r"(?:伤病|轮休|停赛|个人原因|名单原因)"
    r"(?:(?![，,；;。！？!?]).){0,6}?(?:导致|造成)"
    r"(?:(?![，,；;。！？!?]).){0,6}?(?:缺阵|缺席|未出场|未出战))"
)


def _has_explicit_historical_absence_reason(player_evidence: dict | None) -> bool:
    if not isinstance(player_evidence, dict):
        return False
    recent = player_evidence.get("recent_team_window")
    games = recent.get("games") if isinstance(recent, dict) else None
    if not isinstance(games, list):
        return False
    return any(
        isinstance(game, dict)
        and isinstance(game.get("absence_reason"), str)
        and bool(game["absence_reason"].strip())
        and isinstance(game.get("absence_reason_source"), str)
        and bool(game["absence_reason_source"].strip())
        for game in games
    )


def _validate_historical_absence_reason(
    inference: str,
    record_ref: str,
    player_role_evidence_context: dict | None,
) -> None:
    clauses = re.split(r"[，,；;。！？!?]", inference)
    unsupported_claim = any(
        HISTORICAL_ABSENCE_TIME_PATTERN.search(clause)
        and UNSUPPORTED_ABSENCE_CAUSE_PATTERN.search(clause)
        for clause in clauses
    )
    if not unsupported_claim:
        return
    player_evidence = _player_evidence_for_record_ref(
        record_ref, player_role_evidence_context
    )
    if not _has_explicit_historical_absence_reason(player_evidence):
        raise PregameAgentError(
            "无证据缺席原因：未出场记录不能推断为伤病、轮休、停赛或其他具体原因。"
        )


def validate_writer_response(
    content: str,
    evidence: dict,
    injury_candidates: list[dict] | None = None,
    *,
    player_role_evidence_context: dict | None = None,
) -> dict:
    generated = content.strip()
    if generated.startswith("```") or generated.endswith("```"):
        raise PregameAgentError("DeepSeek 返回了代码围栏，结构化结果不合格。")
    try:
        value = json.loads(generated)
    except json.JSONDecodeError as exc:
        raise PregameAgentError("DeepSeek 最终结果不是有效 JSON。") from exc
    required = {
        "final_predicted_home_margin",
        "final_predicted_total",
        "margin_adjustment_reason",
        "total_adjustment_reason",
        "primary_evidence_refs",
        "injury_impact_assessments",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise PregameAgentError("DeepSeek 最终结果字段不符合固定结构。")
    margin = _finite_number(value["final_predicted_home_margin"], "最终预测主队分差")
    total = _finite_number(value["final_predicted_total"], "最终预测总分")
    if not -80 <= margin <= 80:
        raise PregameAgentError("最终预测主队分差超出程序允许范围。")
    if not 100 <= total <= 400:
        raise PregameAgentError("最终预测总分超出程序允许范围。")

    reasons = []
    for key in ("margin_adjustment_reason", "total_adjustment_reason"):
        reason = _validate_generated_text(value[key], "调整理由")
        if any(term in reason for term in INJURY_ADJUSTMENT_REASON_TERMS):
            raise PregameAgentError(
                "调整理由不得把伤病记录作为分差或总分的单独调整依据。"
            )
        reasons.append(reason)

    refs = value["primary_evidence_refs"]
    catalog = _evidence_reference_catalog(evidence)
    if (
        not isinstance(refs, list)
        or not 2 <= len(refs) <= 6
        or len(set(refs)) != len(refs)
        or any(not isinstance(ref, str) or ref not in catalog for ref in refs)
    ):
        raise PregameAgentError("DeepSeek 返回的证据引用无效或不在白名单中。")
    if any(ref.startswith("injuries.") for ref in refs):
        raise PregameAgentError(
            "主要预测依据不得使用零散伤病字段；伤病判断必须引用完整记录。"
        )

    assessments = value["injury_impact_assessments"]
    injury_records = evidence.get("verified_pregame_injuries", [])
    all_records = build_injury_analysis_candidates(injury_records)
    all_record_refs = [record["record_ref"] for record in all_records]
    if injury_candidates is not None:
        provided_refs = [item.get("record_ref") for item in injury_candidates]
        if provided_refs != all_record_refs:
            raise PregameAgentError(
                "程序候选记录不得在 Agent 调用前裁剪，必须覆盖全部官方报告记录。"
            )
    if not isinstance(assessments, list):
        raise PregameAgentError("DeepSeek 返回的伤病影响判断结构无效。")
    if len(assessments) > MAX_INJURY_ANALYSIS_CANDIDATES:
        raise PregameAgentError(
            f"最终详细影响判断最多展开 {MAX_INJURY_ANALYSIS_CANDIDATES} 条。"
        )
    received_record_refs: list[str] = []
    for assessment in assessments:
        required_assessment_fields = {
            "record_ref",
            "impact_inference",
            "prediction_treatment",
        }
        if not isinstance(assessment, dict) or set(assessment) != required_assessment_fields:
            raise PregameAgentError("DeepSeek 返回的单条伤病影响判断字段无效。")
        record_ref = assessment["record_ref"]
        if not isinstance(record_ref, str):
            raise PregameAgentError("伤病影响判断缺少有效记录引用。")
        if record_ref in received_record_refs:
            raise PregameAgentError("伤病影响判断包含重复记录引用。")
        if record_ref not in all_record_refs:
            raise PregameAgentError(
                "伤病影响判断的记录引用不在全部官方报告记录中。"
            )
        received_record_refs.append(record_ref)

    if player_role_evidence_context is None:
        embedded_role_context = evidence.get("player_role_evidence_context")
        if isinstance(embedded_role_context, dict):
            player_role_evidence_context = embedded_role_context

    validated_assessments = []
    semantic_errors: list[str] = []
    for assessment in assessments:
        record_ref = assessment["record_ref"]
        _, record = _injury_record_for_ref(record_ref, evidence)
        record_error_count_before = len(semantic_errors)
        try:
            inference = _validate_generated_text(
                assessment["impact_inference"],
                "伤病影响推断",
                maximum=220,
                reject_unscoped_role_language=False,
            )
        except PregameAgentError as exc:
            semantic_errors.append(f"{record_ref}: {exc}")
            inference = None
        classification = classify_injury_record(record)
        if inference is not None:
            semantic_checks: list[Callable[[], None]] = [
                lambda: _validate_injury_inference_fact_boundaries(
                    inference, evidence
                ),
                lambda: _validate_historical_absence_reason(
                    inference, record_ref, player_role_evidence_context
                ),
                lambda: _validate_role_inference(
                    inference, record_ref, player_role_evidence_context
                ),
            ]
            for check in semantic_checks:
                try:
                    check()
                except PregameAgentError as exc:
                    semantic_errors.append(f"{record_ref}: {exc}")

            if classification == "questionable_availability":
                if _questionable_contains_definite_absence(inference):
                    semantic_errors.append(
                        f"{record_ref}: Questionable 只代表出赛存疑，"
                        "不能写成确定缺阵。"
                    )
                if not any(
                    term in inference for term in QUESTIONABLE_UNCERTAINTY_TERMS
                ):
                    semantic_errors.append(
                        f"{record_ref}: Questionable 的影响推断必须明确说明"
                        "出赛存疑或不确定。"
                    )
            if classification == "confirmed_available" and any(
                term in inference
                for term in ("缺阵", "伤缺", "无法出场", "不会出场", "无法出赛")
            ):
                semantic_errors.append(
                    f"{record_ref}: Available 记录不能解释为缺阵。"
                )
            if classification == "non_injury_out" and any(
                term in inference for term in NON_INJURY_MISLABEL_TERMS
            ):
                semantic_errors.append(
                    f"{record_ref}: 非伤病 Out 原因不能解释为受伤或伤病缺阵。"
                )
        if assessment["prediction_treatment"] != INJURY_PREDICTION_TREATMENT:
            semantic_errors.append(
                f"{record_ref}: 缺少球员贡献与独立性证据时，"
                "不得对单条伤病记录单独量化调整。"
            )
        if len(semantic_errors) == record_error_count_before and inference is not None:
            validated_assessments.append(
                {
                    "record_ref": record_ref,
                    "impact_inference": inference,
                    "prediction_treatment": INJURY_PREDICTION_TREATMENT,
                }
            )
    if semantic_errors:
        raise PregameAgentError("；".join(dict.fromkeys(semantic_errors)))
    return {
        "final_predicted_home_margin": margin,
        "final_predicted_total": total,
        "margin_adjustment_reason": reasons[0],
        "total_adjustment_reason": reasons[1],
        "primary_evidence_refs": refs,
        "injury_impact_assessments": validated_assessments,
    }


EVIDENCE_LABELS = {
    "base.home_win_probability": "基础模型主队胜率",
    "base.predicted_home_margin": "基础模型主队分差",
    "base.predicted_total": "基础模型总分",
    "home.pregame_elo": "主队赛前 Elo",
    "away.pregame_elo": "客队赛前 Elo",
    "home.pregame_rest_days": "主队休息天数",
    "away.pregame_rest_days": "客队休息天数",
    "home.pregame_back_to_back": "主队背靠背",
    "away.pregame_back_to_back": "客队背靠背",
    "home.pregame_win_rate_roll5": "主队此前五场胜率",
    "away.pregame_win_rate_roll5": "客队此前五场胜率",
    "home.pregame_net_rating_roll5": "主队此前五场净效率",
    "away.pregame_net_rating_roll5": "客队此前五场净效率",
    "home.pregame_win_rate_roll15": "主队此前十五场胜率",
    "away.pregame_win_rate_roll15": "客队此前十五场胜率",
    "home.pregame_net_rating_roll15": "主队此前十五场净效率",
    "away.pregame_net_rating_roll15": "客队此前十五场净效率",
    "home.pregame_pace_roll15": "主队此前十五场节奏",
    "away.pregame_pace_roll15": "客队此前十五场节奏",
}


def _render_evidence_value(reference: str, value: object) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if reference.endswith("win_rate_roll5") or reference.endswith("win_rate_roll15"):
        return f"{float(value) * 100:.1f}%"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _reported_injury_fact(index: int, record: dict, cutoff_time_utc: str) -> dict:
    classification = classify_injury_record(record)
    availability_group = classify_availability_group(record)
    reason_classification = classify_availability_reason(record)
    captured_at = record["captured_at"]
    capture_timing = (
        "captured_after_cutoff_historical_archive"
        if captured_at > cutoff_time_utc
        else "captured_by_cutoff"
    )
    return {
        "record_ref": f"injuries.{index}",
        "source_record_id": record["source_record_id"],
        "team_name": record["team_name"],
        "player_name": record["player_name"],
        "availability_status": record["availability_status"],
        "availability_group": availability_group,
        "availability_group_label": AVAILABILITY_GROUP_LABELS[availability_group],
        "reason": record["reason"],
        "reason_classification": reason_classification,
        "reason_classification_label": AVAILABILITY_REASON_LABELS[
            reason_classification
        ],
        "is_health_related_reason": reason_classification
        in {"injury_illness", "concussion_protocol"},
        "fact_classification": classification,
        "fact_classification_label": INJURY_FACT_CLASSIFICATION_LABELS[classification],
        "source": record["source"],
        "source_url": record["source_url"],
        "source_published_at": record["source_published_at"],
        "captured_at": captured_at,
        "capture_timing": capture_timing,
    }


def build_analysis_package(
    evidence: dict,
    final: dict,
    availability: dict,
    *,
    artifact_set_id: str,
    generated_at_utc: str,
    model: str,
    model_response_sha256: str,
    injury_candidates: list[dict] | None = None,
) -> dict:
    base = evidence["base_prediction"]
    catalog = _evidence_reference_catalog(evidence)
    cited = []
    for ref in final["primary_evidence_refs"]:
        label = EVIDENCE_LABELS.get(ref, ref)
        injury_match = re.fullmatch(r"injuries\.(\d+)\.(\w+)", ref)
        if injury_match:
            index = int(injury_match.group(1))
            row = evidence["verified_pregame_injuries"][index]
            field_labels = {
                "team_name": "球队",
                "player_name": "球员",
                "availability_status": "出赛状态",
                "reason": "报告原因",
            }
            label = (
                f"{row['team_name']} {row['player_name']} "
                f"{field_labels.get(injury_match.group(2), injury_match.group(2))}"
            )
        cited.append({"reference": ref, "label": label, "value": catalog[ref]})
    injury_available = (
        availability.get("injuries", {}).get("status")
        == "available_historical_archive"
    )
    injury_limitation = (
        "伤病来自本次获取的 NBA 官方带报告时间历史归档；"
        "该证据不证明 CourtVision 在比赛前已经采集和持久化。"
        if injury_available
        else "没有可证明在截止时间前发布的可靠伤病记录进入分析。"
    )
    injury_availability = availability.get("injuries", {})
    cutoff_time_utc = injury_availability.get("cutoff_time_utc", "")
    reported_facts = [
        _reported_injury_fact(index, row, cutoff_time_utc)
        for index, row in enumerate(evidence.get("verified_pregame_injuries", []))
    ]
    player_availability_context = build_player_availability_context(
        evidence.get("verified_pregame_injuries", [])
    )
    player_availability_summary = summarize_player_availability(
        player_availability_context
    )
    candidates = (
        build_injury_analysis_candidates(
            evidence.get("verified_pregame_injuries", [])
        )
        if injury_candidates is None
        else injury_candidates
    )
    facts_by_ref = {fact["record_ref"]: fact for fact in reported_facts}
    impact_assessments = [
        {
            "record_ref": item["record_ref"],
            "reported_fact": facts_by_ref[item["record_ref"]],
            "impact_inference": item["impact_inference"],
            "prediction_treatment": item["prediction_treatment"],
            "prediction_treatment_label": (
                "未依据该单条记录对分差或总分作独立数值调整"
            ),
        }
        for item in final["injury_impact_assessments"]
    ]
    package = {
        "schema_version": "1.2",
        "analysis_type": "historical_pregame_single_game_demo",
        "artifact_set_id": artifact_set_id,
        "generated_at_utc": generated_at_utc,
        "generation": {
            "model": model,
            "api_request_count": 1,
            "retry_count": 0,
            "model_response_sha256": model_response_sha256,
        },
        "game": evidence["game"],
        "time_boundary": evidence["time_boundary"],
        "base_prediction": base,
        "agent_prediction": {
            "final_predicted_home_margin": final["final_predicted_home_margin"],
            "final_predicted_total": final["final_predicted_total"],
            "home_margin_adjustment": (
                final["final_predicted_home_margin"] - base["predicted_home_margin"]
            ),
            "total_adjustment": final["final_predicted_total"] - base["predicted_total"],
            "margin_adjustment_reason": final["margin_adjustment_reason"],
            "total_adjustment_reason": final["total_adjustment_reason"],
        },
        "primary_evidence": cited,
        "injury_analysis": {
            "source_boundary": {
                "source": (
                    reported_facts[0]["source"] if reported_facts else None
                ),
                "source_url": (
                    reported_facts[0]["source_url"] if reported_facts else None
                ),
                "source_published_at": (
                    reported_facts[0]["source_published_at"]
                    if reported_facts
                    else None
                ),
                "captured_at": (
                    reported_facts[0]["captured_at"] if reported_facts else None
                ),
                "cutoff_time_utc": cutoff_time_utc or None,
                "archive_status": injury_availability.get("status", "unavailable"),
                "boundary_statement": injury_limitation,
            },
            "availability_summary": player_availability_summary,
            "reported_facts": reported_facts,
            "analysis_candidates": candidates,
            "agent_impact_inferences": impact_assessments,
            "unsupported_judgments": [
                "官方报告没有提供球员位置、主力身份或轮换角色。",
                "当前证据没有提供球员贡献、预计上场时间或替代方案，不能量化单条记录对应的分差或总分影响。",
                "Questionable 只说明报告时出赛存疑，不能据此确认最终缺阵。",
                "G League、Two-Way、Not With Team 等原因不能解释为受伤。",
                "没有出现在官方报告中的球员不能据此确认出场、进入激活名单或获得上场时间。",
                "长期不可出赛状态可能已经反映在球队近期数据中，缺少独立依据时不重复调整。",
            ],
        },
        "information_availability": availability,
        "limitations": [
            "基础预测是历史回测记录，缺少真实赛前生成与持久化时间戳。",
            "数据库比赛时间没有可核验的时区字段，截止时间按同一来源时间尺度比较。",
            injury_limitation,
            "首发和历史市场基准缺少可证明早于截止时间的可靠时间戳，因此没有进入分析。",
            "目标比赛结果、当场球员统计、逐回合和赛后复盘没有进入 Agent 上下文。",
            "本次单场演示只验证流程和数据边界，不能证明预测准确率。",
        ],
        "validation": {
            "target_outcome_excluded": True,
            "all_prior_games_before_cutoff": True,
            "numeric_adjustments_recomputed_by_python": True,
            "injury_facts_rendered_from_source_records": True,
            "complete_player_availability_context_provided_to_model": True,
            "complete_player_role_evidence_provided_to_model": True,
            "all_out_records_grouped_as_confirmed_unavailable": True,
            "questionable_not_treated_as_confirmed_out": True,
            "non_injury_out_separated_from_health_related_out": True,
            "injury_records_not_separately_quantified": True,
            "injury_candidates_prefiltered_by_program": False,
            "model_assessments_within_detail_limit": len(
                final["injury_impact_assessments"]
            )
            <= MAX_INJURY_ANALYSIS_CANDIDATES,
            "model_assessments_reference_visible_records": True,
            "read_only_database": True,
        },
    }
    return package


def format_number(value: object, digits: int = 4, signed: bool = False) -> str:
    number = _finite_number(value, "输出数字")
    text = f"{abs(number):.{digits}f}".rstrip("0").rstrip(".")
    if signed and number > 0:
        return "+" + text
    if signed and number < 0:
        return "−" + text
    return text


def render_markdown(package: dict) -> str:
    game = package["game"]
    boundary = package["time_boundary"]
    base = package["base_prediction"]
    final = package["agent_prediction"]
    lines = [
        "# CourtVision 单场历史赛前分析",
        "",
        f"**产物组标识：** `{package['artifact_set_id']}`",
        f"**比赛：** {game['away_team_name']} 客场对阵 {game['home_team_name']}",
        f"**比赛编号：** `{game['game_id']}`",
        f"**赛前截止时间：** `{boundary['cutoff_time']}`",
        f"**数据库比赛时间：** `{boundary['scheduled_game_time']}`",
        "",
        "> 时间比较采用数据库来源时间尺度，来源时区未经验证。本分析在技术上严格排除了目标比赛赛后字段。",
        "",
        "## 历史回测基础预测",
        "",
        f"- 主队获胜概率：{float(base['home_win_probability']) * 100:.2f}%",
        f"- 预测主队分差：{format_number(base['predicted_home_margin'], signed=True)} 分",
        f"- 预测总分：{format_number(base['predicted_total'])} 分",
        "- 记录性质：历史回测基础预测，缺少真实赛前生成和持久化时间戳。",
        "",
        "## Agent 最终预测",
        "",
        f"- 最终预测主队分差：{format_number(final['final_predicted_home_margin'], signed=True)} 分",
        f"- 最终预测总分：{format_number(final['final_predicted_total'])} 分",
        f"- 分差调整：{format_number(final['home_margin_adjustment'], signed=True)} 分",
        f"- 总分调整：{format_number(final['total_adjustment'], signed=True)} 分",
        "",
        f"**分差调整理由：** {final['margin_adjustment_reason']}",
        "",
        f"**总分调整理由：** {final['total_adjustment_reason']}",
        "",
        "## 主要依据",
        "",
    ]
    for item in package["primary_evidence"]:
        lines.append(
            f"- {item['label']}：{_render_evidence_value(item['reference'], item['value'])}"
        )

    injury_analysis = package["injury_analysis"]
    source_boundary = injury_analysis["source_boundary"]
    reported_facts = injury_analysis["reported_facts"]
    availability_summary = injury_analysis["availability_summary"]
    lines.extend(
        [
            "",
            "## 球员可用性汇总",
            "",
            f"- 官方报告记录：{availability_summary['total_records']} 条。",
            f"- 报告时确定无法出场：{availability_summary['confirmed_out_count']} 条。",
            f"- 报告时出赛状态不确定：{availability_summary['questionable_count']} 条。",
            f"- 报告时可以出场：{availability_summary['available_count']} 条。",
            f"- 健康相关原因的确定无法出场：{availability_summary['health_related_out_count']} 条。",
            f"- 非健康原因的确定无法出场：{availability_summary['non_health_related_out_count']} 条。",
            "- 未出现在官方报告中的球员，不能据此确认出场或进入激活名单。",
        ]
    )
    lines.extend(["", "## 官方报告明确事实", ""])
    if reported_facts:
        lines.extend(
            [
                f"- 报告发布时间：`{source_boundary['source_published_at']}`",
                f"- CourtVision 实际采集时间：`{source_boundary['captured_at']}`",
                f"- 赛前截止时间：`{source_boundary['cutoff_time_utc']}`",
                f"- 时间边界：{source_boundary['boundary_statement']}",
                "",
            ]
        )
        for fact in reported_facts:
            lines.append(
                f"- `{fact['record_ref']}` {fact['team_name']}，{fact['player_name']}："
                f"{fact['availability_status']}；报告原因：{fact['reason']}；"
                f"可用性分类：{fact['availability_group_label']}；"
                f"原因分类：{fact['reason_classification_label']}；"
                f"来源时间：`{fact['source_published_at']}`。"
            )
    else:
        lines.append("- 没有通过赛前时点门禁的官方报告记录。")

    lines.extend(["", "## Agent 影响推断", ""])
    if injury_analysis["agent_impact_inferences"]:
        for item in injury_analysis["agent_impact_inferences"]:
            fact = item["reported_fact"]
            lines.extend(
                [
                    f"- `{item['record_ref']}` {fact['team_name']}，{fact['player_name']}，"
                    f"{fact['availability_status']}，{fact['reason']}",
                    f"  - 影响推断：{item['impact_inference']}",
                    f"  - 预测处理：{item['prediction_treatment_label']}。",
                ]
            )
    else:
        lines.append("- Agent 没有生成伤病影响推断。")

    lines.extend(["", "## 当前证据无法支持", ""])
    for limitation in injury_analysis["unsupported_judgments"]:
        lines.append(f"- {limitation}")

    lines.extend(["", "## 信息可用性与缺口", ""])
    labels = {"injuries": "历史伤病", "starters": "历史首发", "market": "历史盘口"}
    for key, item in package["information_availability"].items():
        if key == "injuries" and item["status"] == "available_historical_archive":
            lines.append(
                f"- {labels[key]}：官方历史归档可用，共 {len(item.get('records', []))} 条。"
                f"{item['reason']}"
            )
        else:
            lines.append(f"- {labels[key]}：不可用。{item['reason']}")
    lines.extend(
        [
            "",
            "## 验证范围",
            "",
            "目标比赛结果、当场球员统计、逐回合和赛后复盘均未进入 Agent 上下文。",
            "本次仅用一场历史比赛验证查询、时间边界、模型调整与输出链路，不能据此判断预测准确率。",
            "",
        ]
    )
    return "\n".join(lines)


def _output_suffix(output_tag: str | None) -> str:
    if output_tag is not None and not OUTPUT_TAG_PATTERN.fullmatch(output_tag):
        raise PregameAgentError(
            "输出标记格式无效，只允许小写字母、数字、下划线和连字符。"
        )
    return f"_{output_tag}" if output_tag else ""


def build_output_paths(
    game_id: str,
    outputs_dir: Path,
    output_tag: str | None,
) -> dict[str, Path]:
    suffix = _output_suffix(output_tag)
    prefix = f"game_{game_id}_historical_pregame"
    return {
        "json": outputs_dir / f"{prefix}_analysis{suffix}.json",
        "markdown": outputs_dir / f"{prefix}_analysis{suffix}.md",
        "trace": outputs_dir / f"{prefix}_tool_trace{suffix}.json",
    }


def build_rejected_output_path(
    game_id: str,
    outputs_dir: Path,
    output_tag: str | None,
) -> Path:
    suffix = _output_suffix(output_tag)
    prefix = f"game_{game_id}_historical_pregame"
    return outputs_dir / f"{prefix}_rejected{suffix}.json"


def build_api_attempt_output_path(
    game_id: str,
    outputs_dir: Path,
    output_tag: str | None,
) -> Path:
    suffix = _output_suffix(output_tag)
    prefix = f"game_{game_id}_historical_pregame"
    return Path(outputs_dir) / f"{prefix}_api_attempt{suffix}.json"


def build_writer_input_snapshot_path(
    game_id: str,
    outputs_dir: Path,
    output_tag: str | None,
) -> Path:
    suffix = _output_suffix(output_tag)
    prefix = f"game_{game_id}_historical_pregame"
    return Path(outputs_dir) / f"{prefix}_writer_input{suffix}.json"


def build_raw_response_output_path(
    game_id: str,
    outputs_dir: Path,
    output_tag: str | None,
) -> Path:
    suffix = _output_suffix(output_tag)
    prefix = f"game_{game_id}_historical_pregame"
    return Path(outputs_dir) / f"{prefix}_raw_response{suffix}.json"


def build_rejected_artifact(
    *,
    artifact_set_id: str,
    generated_at_utc: str,
    output_tag: str | None,
    game_id: str,
    cutoff_time: str,
    model: str,
    writer_result: ApiResult,
    model_response_sha256: str,
    validation_error: str,
    database_path: Path,
    database_hash_before: str,
    database_hash_after: str,
    snapshot_database_path: Path,
    snapshot_hash_before: str,
    snapshot_hash_after: str,
    rejected_path: Path,
    player_availability_context: list[dict],
    player_availability_summary: dict,
    injury_candidates: list[dict],
    attempt_path: Path | None = None,
    writer_input_path: Path | None = None,
    writer_input_sha256: str | None = None,
) -> dict:
    return {
        "schema_version": "1.2",
        "artifact_type": "rejected_model_response",
        "status": "rejected",
        "artifact_set_id": artifact_set_id,
        "generated_at_utc": generated_at_utc,
        "output_tag": output_tag,
        "game_id": game_id,
        "cutoff_time": cutoff_time,
        "model": model,
        "api_request_count": 1,
        "retry_count": 0,
        "validation_error": validation_error,
        "player_availability_summary": player_availability_summary,
        "player_availability_context": player_availability_context,
        "injury_analysis_candidates": injury_candidates,
        "model_response": {
            "content": writer_result.content,
            "sha256": model_response_sha256,
            "finish_reason": writer_result.finish_reason,
            "usage": writer_result.usage,
        },
        "successful_analysis_artifacts_written": False,
        "credential_material_persisted": False,
        "api_attempt_artifact": str(attempt_path) if attempt_path else None,
        "writer_input_artifact": {
            "path": str(writer_input_path) if writer_input_path else None,
            "sha256": writer_input_sha256,
        },
        "database": {
            "path": str(database_path),
            "mode": "read_only_query_only",
            "sha256_before": database_hash_before,
            "sha256_after": database_hash_after,
            "unchanged": database_hash_before == database_hash_after,
        },
        "pregame_snapshot_database": {
            "path": str(snapshot_database_path),
            "mode": "read_only_query_only",
            "view": "agent_eligible_injury_snapshots",
            "sha256_before": snapshot_hash_before,
            "sha256_after": snapshot_hash_after,
            "unchanged": snapshot_hash_before == snapshot_hash_after,
        },
        "artifact_file": str(rejected_path),
    }


def _refuse_existing_outputs(paths: dict[str, Path]) -> None:
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        joined = "；".join(str(path) for path in existing)
        raise PregameAgentError(f"目标产物已经存在，拒绝覆盖：{joined}")


def _atomic_publish_texts(texts: dict[Path, str]) -> None:
    temporary_paths: dict[Path, Path] = {}
    try:
        _refuse_existing_outputs(
            {str(index): destination for index, destination in enumerate(texts)}
        )
        for destination, content in texts.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_paths[destination] = Path(temporary.name)
        _refuse_existing_outputs(
            {str(index): destination for index, destination in enumerate(texts)}
        )
        for destination, temporary_path in temporary_paths.items():
            os.replace(temporary_path, destination)
    finally:
        for temporary_path in temporary_paths.values():
            if temporary_path.exists():
                temporary_path.unlink()


def _exclusive_create_text(destination: Path, content: str) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        )
    except FileExistsError as exc:
        raise PregameAgentError(
            f"目标产物已经存在，拒绝覆盖：{destination}"
        ) from exc
    try:
        with os.fdopen(
            descriptor, mode="w", encoding="utf-8", newline="\n"
        ) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        # Even an incomplete reservation must keep blocking the same output tag.
        raise


def _atomic_replace_text(destination: Path, content: str) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _json_artifact_text(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def build_agent_input_audit_path(
    game_id: str,
    outputs_dir: Path,
    output_tag: str | None,
) -> Path:
    suffix = _output_suffix(output_tag)
    return Path(outputs_dir) / f"game_{game_id}_historical_pregame_agent_input{suffix}.json"


def _extract_writer_evidence_from_messages(messages: list[dict[str, str]]) -> dict:
    if len(messages) != 2 or messages[1].get("role") != "user":
        raise PregameAgentError("Agent 输入消息结构不符合固定格式。")
    content = messages[1].get("content", "")
    prefix = "<pregame_evidence>\n"
    suffix = "\n</pregame_evidence>"
    if not content.startswith(prefix) or not content.endswith(suffix):
        raise PregameAgentError("Agent 输入缺少 pregame_evidence 边界。")
    try:
        value = json.loads(content[len(prefix) : -len(suffix)])
    except json.JSONDecodeError as exc:
        raise PregameAgentError("Agent 输入中的 pregame_evidence 不是有效 JSON。") from exc
    if not isinstance(value, dict):
        raise PregameAgentError("Agent 输入中的 pregame_evidence 必须是对象。")
    return value


def _validate_agent_input_safety(
    messages: list[dict[str, str]],
    game_id: str,
    cutoff_time: str,
) -> dict:
    writer_evidence = _extract_writer_evidence_from_messages(messages)
    _walk_forbidden_player_evidence_keys(writer_evidence)
    if writer_evidence.get("game", {}).get("game_id") != game_id:
        raise PregameAgentError("Agent 输入的比赛编号不一致。")
    role_context = writer_evidence.get("player_role_evidence_context")
    if not isinstance(role_context, dict) or len(role_context.get("players", [])) != 10:
        raise PregameAgentError("Agent 输入没有包含完整 10 人球员证据。")
    _validate_player_role_evidence_payload(
        {
            **role_context,
            "status": role_context.get("status", "complete"),
            "game_id": role_context.get("game_id", game_id),
        },
        game_id,
        cutoff_time,
    )
    for side in ("home", "away"):
        for row in writer_evidence.get("recent_games", {}).get(side, []):
            if row.get("game_id") == game_id:
                raise PregameAgentError("Agent 输入的近期历史包含目标比赛。")
    return writer_evidence


def _prepare_historical_pregame_agent_input(
    game_id: str,
    cutoff_time: str,
    *,
    database_path: Path = ANALYSIS_DATABASE_PATH,
    snapshot_database_path: Path = PREGAME_SNAPSHOT_DATABASE_PATH,
    player_evidence_path: Path = PLAYER_ROLE_EVIDENCE_PATH,
    player_evidence_manifest_path: Path = PLAYER_ROLE_EVIDENCE_MANIFEST_PATH,
) -> dict:
    database_path = Path(database_path)
    snapshot_database_path = Path(snapshot_database_path)
    player_evidence_path = Path(player_evidence_path)
    player_evidence_manifest_path = Path(player_evidence_manifest_path)
    database_hash_before = sha256_file(database_path)
    snapshot_hash_before = sha256_file(snapshot_database_path)

    identity = load_target_identity(game_id, database_path)
    cutoff = validate_cutoff(cutoff_time, identity)
    requested = list(OPTIONAL_INFORMATION_TYPES)

    tools = PregameReadOnlyTools(database_path, game_id, cutoff_time)
    evidence, tool_trace = tools.build_core_evidence()
    availability = assess_optional_information(
        requested,
        game_id=game_id,
        cutoff_time=cutoff_time,
        snapshot_database_path=snapshot_database_path,
    )
    injury_records = availability["injuries"].get("records", [])
    player_role_evidence_context, player_role_evidence_source = load_player_role_evidence(
        player_evidence_path,
        player_evidence_manifest_path,
        injury_records,
        game_id,
        cutoff_time,
    )
    evidence["verified_pregame_injuries"] = injury_records
    player_availability_context = build_player_availability_context(injury_records)
    player_availability_summary = summarize_player_availability(
        player_availability_context
    )
    injury_candidates = build_injury_analysis_candidates(injury_records)
    tool_trace.append(
        {
            "sequence": len(tool_trace) + 1,
            "tool": "load_player_role_evidence_context",
            "source": "cached_player_evidence_json_and_raw_manifest",
            "read_only": True,
            "arguments": {
                "evidence_json": str(player_evidence_path),
                "source_manifest": str(player_evidence_manifest_path),
                "raw_directory": player_role_evidence_source["raw_directory"],
            },
            "result": {
                "player_count": len(player_role_evidence_context["players"]),
                "raw_artifact_count": player_role_evidence_source["raw_artifact_count"],
                "raw_response_hashes_verified": player_role_evidence_source[
                    "raw_response_hashes_verified"
                ],
                "cutoff": player_role_evidence_context["cutoff"],
            },
        }
    )
    tool_trace.append(
        {
            "sequence": len(tool_trace) + 1,
            "tool": "build_player_availability_context",
            "source": "deterministic_official_status_and_reason_classification",
            "read_only": True,
            "arguments": {
                "official_record_count": len(injury_records),
                "availability_groups": [
                    "confirmed_out",
                    "questionable",
                    "available",
                    "other_reported_status",
                ],
            },
            "result": {
                "summary": player_availability_summary,
                "records": player_availability_context,
            },
        }
    )
    tool_trace.append(
        {
            "sequence": len(tool_trace) + 1,
            "tool": "check_optional_information_availability",
            "source": "deterministic_required_boundary_check",
            "read_only": True,
            "arguments": {
                "required_checks": requested,
                "snapshot_database": str(snapshot_database_path),
            },
            "result": availability,
        }
    )
    tool_trace.append(
        {
            "sequence": len(tool_trace) + 1,
            "tool": "expose_all_injury_analysis_records",
            "source": "deterministic_complete_report_record_index",
            "read_only": True,
            "arguments": {
                "report_record_count": len(injury_records),
                "prefilter_by_reason_or_count": False,
                "final_detail_limit": MAX_INJURY_ANALYSIS_CANDIDATES,
            },
            "result": {
                "full_fact_record_count": len(injury_records),
                "visible_to_agent_record_count": len(injury_candidates),
                "detail_selection_by": "Agent",
                "records": injury_candidates,
            },
        }
    )

    writer_messages = build_writer_messages(
        evidence,
        availability,
        injury_candidates,
        player_role_evidence_context,
        player_role_evidence_source,
    )
    _validate_agent_input_safety(writer_messages, game_id, cutoff_time)
    database_hash_after = sha256_file(database_path)
    snapshot_hash_after = sha256_file(snapshot_database_path)
    if database_hash_before != database_hash_after:
        raise PregameAgentError("历史分析数据库 SHA256 在构造 Agent 输入期间发生变化。")
    if snapshot_hash_before != snapshot_hash_after:
        raise PregameAgentError("赛前信息快照数据库 SHA256 在构造 Agent 输入期间发生变化。")
    return {
        "database_path": database_path,
        "snapshot_database_path": snapshot_database_path,
        "database_hash_before": database_hash_before,
        "database_hash_after": database_hash_after,
        "snapshot_hash_before": snapshot_hash_before,
        "snapshot_hash_after": snapshot_hash_after,
        "identity": identity,
        "cutoff": cutoff,
        "evidence": evidence,
        "availability": availability,
        "player_role_evidence_context": player_role_evidence_context,
        "player_role_evidence_source": player_role_evidence_source,
        "player_availability_context": player_availability_context,
        "player_availability_summary": player_availability_summary,
        "injury_candidates": injury_candidates,
        "tool_trace": tool_trace,
        "writer_messages": writer_messages,
    }


def _build_cutoff_audit(prepared: dict, game_id: str, cutoff_time: str) -> dict:
    cutoff = prepared["cutoff"]
    cutoff_date = cutoff.date().isoformat()
    cutoff_utc = _cutoff_source_local_to_utc(cutoff_time)
    return {
        "exact_timestamp_evidence": {
            "scope": "official injury report source_published_at",
            "comparison": "source_published_at <= cutoff_time_utc",
            "cutoff_time_utc": cutoff_utc,
            "validated": True,
            "capture_time_claim": (
                "Historical files may have been captured after the cutoff; "
                "source publication time is the pregame-publication evidence."
            ),
        },
        "player_game_statistics": {
            "field": "GAME_DATE",
            "filter_granularity": "calendar_date",
            "filter_rule": f"GAME_DATE < {cutoff_date} and GAME_ID != {game_id}",
            "exact_intraday_cutoff_validated": False,
            "limitation": (
                "Player game rows are excluded by date. They are not certified "
                f"to the exact {cutoff.strftime('%H:%M')} ET cutoff within the day."
            ),
        },
        "team_game_fields": {
            "field": "fact_team_game.game_date",
            "filter_rule": f"game_date < {cutoff.isoformat(timespec='seconds')}",
            "source_timezone_status": "unverified",
            "exact_eastern_time_cutoff_validated": False,
            "limitation": (
                "The database field is compared on its existing source time scale; "
                "its source timezone has not been independently verified."
            ),
        },
    }


def _build_writer_input_snapshot(
    *,
    artifact_set_id: str,
    attempt_id: str,
    generated_at_utc: str,
    game_id: str,
    cutoff_time: str,
    model: str,
    output_tag: str | None,
    writer_messages: list[dict[str, str]],
    prepared: dict,
    snapshot_path: Path,
) -> dict:
    writer_evidence = _extract_writer_evidence_from_messages(writer_messages)
    return {
        "schema_version": "1.0",
        "artifact_type": "historical_pregame_writer_input_snapshot",
        "status": "request_input_frozen",
        "artifact_set_id": artifact_set_id,
        "attempt_id": attempt_id,
        "generated_at_utc": generated_at_utc,
        "game_id": game_id,
        "cutoff_time": cutoff_time,
        "model": model,
        "output_tag": output_tag,
        "agent_messages": writer_messages,
        "agent_messages_sha256": sha256_text(
            json.dumps(
                writer_messages,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        ),
        "input_validation": {
            "complete_player_role_evidence_count": len(
                writer_evidence["player_role_evidence_context"]["players"]
            ),
            "candidate_count": len(prepared["injury_candidates"]),
            "target_outcome_keys_excluded": True,
            "target_game_recent_rows_excluded": True,
            "official_status_source_join_validated": True,
        },
        "cutoff_audit": _build_cutoff_audit(prepared, game_id, cutoff_time),
        "database": {
            "path": str(prepared["database_path"]),
            "mode": "read_only_query_only",
            "sha256_before": prepared["database_hash_before"],
            "sha256_after_input_build": prepared["database_hash_after"],
            "unchanged": prepared["database_hash_before"]
            == prepared["database_hash_after"],
        },
        "pregame_snapshot_database": {
            "path": str(prepared["snapshot_database_path"]),
            "mode": "read_only_query_only",
            "sha256_before": prepared["snapshot_hash_before"],
            "sha256_after_input_build": prepared["snapshot_hash_after"],
            "unchanged": prepared["snapshot_hash_before"]
            == prepared["snapshot_hash_after"],
        },
        "credential_material_persisted": False,
        "artifact_path": str(snapshot_path),
    }


def _build_api_attempt_record(
    *,
    artifact_set_id: str,
    attempt_id: str,
    started_at_utc: str,
    game_id: str,
    cutoff_time: str,
    model: str,
    output_tag: str | None,
    attempt_path: Path,
    writer_input_path: Path,
    writer_input_sha256: str,
    agent_messages_sha256: str,
) -> dict:
    return {
        "schema_version": "1.0",
        "artifact_type": "historical_pregame_api_attempt",
        "status": "attempt_started",
        "artifact_set_id": artifact_set_id,
        "attempt_id": attempt_id,
        "started_at_utc": started_at_utc,
        "updated_at_utc": started_at_utc,
        "game_id": game_id,
        "cutoff_time": cutoff_time,
        "model": model,
        "output_tag": output_tag,
        "api_request_count": 1,
        "retry_count": 0,
        "request_slot_consumed": True,
        "response_received": False,
        "writer_input_artifact": {
            "path": str(writer_input_path),
            "sha256": writer_input_sha256,
            "agent_messages_sha256": agent_messages_sha256,
        },
        "status_history": [
            {"status": "attempt_started", "recorded_at_utc": started_at_utc}
        ],
        "credential_material_persisted": False,
        "artifact_path": str(attempt_path),
    }


def _update_api_attempt(
    attempt_path: Path,
    attempt_id: str,
    status: str,
    **updates: object,
) -> dict:
    try:
        attempt = json.loads(Path(attempt_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PregameAgentError("无法安全更新 DeepSeek 调用尝试记录。") from exc
    if attempt.get("attempt_id") != attempt_id:
        raise PregameAgentError("DeepSeek 调用尝试记录身份不一致，拒绝更新。")
    updated_at_utc = _utc_now()
    attempt.update(updates)
    attempt["status"] = status
    attempt["updated_at_utc"] = updated_at_utc
    history = attempt.setdefault("status_history", [])
    history.append({"status": status, "recorded_at_utc": updated_at_utc})
    _atomic_replace_text(Path(attempt_path), _json_artifact_text(attempt))
    return attempt


def _build_raw_response_artifact(
    *,
    attempt_id: str,
    game_id: str,
    cutoff_time: str,
    model: str,
    output_tag: str | None,
    failure: DeepSeekRequestError,
    raw_response_path: Path,
) -> dict:
    return {
        "schema_version": "1.0",
        "artifact_type": "historical_pregame_api_raw_response",
        "status": "api_failure_response_evidence",
        "attempt_id": attempt_id,
        "generated_at_utc": _utc_now(),
        "game_id": game_id,
        "cutoff_time": cutoff_time,
        "model": model,
        "output_tag": output_tag,
        "error_type": failure.error_type,
        "response_received": failure.response_received,
        "http_status": failure.http_status,
        "empty_response_kind": failure.empty_response_kind,
        "raw_response": {
            "text": failure.raw_response_text,
            "sha256": failure.raw_response_sha256,
            "byte_count": failure.raw_response_byte_count,
            "credential_redaction_applied": failure.raw_response_redacted,
            "explicitly_empty": failure.raw_response_byte_count == 0,
        },
        "credential_material_persisted": False,
        "artifact_path": str(raw_response_path),
    }


def build_historical_pregame_agent_input_audit(
    game_id: str,
    cutoff_time: str,
    *,
    database_path: Path = ANALYSIS_DATABASE_PATH,
    snapshot_database_path: Path = PREGAME_SNAPSHOT_DATABASE_PATH,
    player_evidence_path: Path = PLAYER_ROLE_EVIDENCE_PATH,
    player_evidence_manifest_path: Path = PLAYER_ROLE_EVIDENCE_MANIFEST_PATH,
    outputs_dir: Path = OUTPUTS_DIR,
    output_tag: str | None = None,
) -> Path:
    audit_path = build_agent_input_audit_path(game_id, outputs_dir, output_tag)
    if audit_path.exists():
        raise PregameAgentError(f"Agent 输入审计文件已经存在，拒绝覆盖：{audit_path}")
    prepared = _prepare_historical_pregame_agent_input(
        game_id,
        cutoff_time,
        database_path=database_path,
        snapshot_database_path=snapshot_database_path,
        player_evidence_path=player_evidence_path,
        player_evidence_manifest_path=player_evidence_manifest_path,
    )
    messages = prepared["writer_messages"]
    writer_evidence = _extract_writer_evidence_from_messages(messages)
    generated_at_utc = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    audit = {
        "schema_version": "1.0",
        "artifact_type": "historical_pregame_agent_input_audit",
        "status": "audit_only",
        "game_id": game_id,
        "cutoff_time": cutoff_time,
        "generated_at_utc": generated_at_utc,
        "deepseek_called": False,
        "api_request_count": 0,
        "retry_count": 0,
        "agent_messages": messages,
        "agent_user_message_sha256": sha256_text(messages[1]["content"]),
        "player_role_evidence_source": prepared["player_role_evidence_source"],
        "player_availability_summary": prepared["player_availability_summary"],
        "injury_analysis_candidates": prepared["injury_candidates"],
        "input_validation": {
            "complete_player_role_evidence_count": len(
                writer_evidence["player_role_evidence_context"]["players"]
            ),
            "candidate_count": len(prepared["injury_candidates"]),
            "target_outcome_keys_excluded": True,
            "target_game_recent_rows_excluded": True,
            "official_status_source_join_validated": True,
        },
        "cutoff_audit": _build_cutoff_audit(prepared, game_id, cutoff_time),
        "tool_trace": prepared["tool_trace"],
        "database": {
            "path": str(prepared["database_path"]),
            "mode": "read_only_query_only",
            "sha256_before": prepared["database_hash_before"],
            "sha256_after": prepared["database_hash_after"],
            "unchanged": prepared["database_hash_before"]
            == prepared["database_hash_after"],
        },
        "pregame_snapshot_database": {
            "path": str(prepared["snapshot_database_path"]),
            "mode": "read_only_query_only",
            "sha256_before": prepared["snapshot_hash_before"],
            "sha256_after": prepared["snapshot_hash_after"],
            "unchanged": prepared["snapshot_hash_before"]
            == prepared["snapshot_hash_after"],
        },
        "artifact_path": str(audit_path),
    }
    _atomic_publish_texts(
        {
            audit_path: json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n"
        }
    )
    return audit_path


def generate_historical_pregame_analysis(
    game_id: str,
    cutoff_time: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    database_path: Path = ANALYSIS_DATABASE_PATH,
    snapshot_database_path: Path = PREGAME_SNAPSHOT_DATABASE_PATH,
    player_evidence_path: Path = PLAYER_ROLE_EVIDENCE_PATH,
    player_evidence_manifest_path: Path = PLAYER_ROLE_EVIDENCE_MANIFEST_PATH,
    outputs_dir: Path = OUTPUTS_DIR,
    api_client: Callable = request_deepseek,
    output_tag: str | None = None,
) -> dict[str, Path]:
    if not api_key:
        raise PregameAgentError("缺少 DEEPSEEK_API_KEY，请先配置环境变量。")
    database_path = Path(database_path)
    snapshot_database_path = Path(snapshot_database_path)
    outputs_dir = Path(outputs_dir)
    paths = build_output_paths(game_id, outputs_dir, output_tag)
    rejected_path = build_rejected_output_path(game_id, outputs_dir, output_tag)
    attempt_path = build_api_attempt_output_path(game_id, outputs_dir, output_tag)
    writer_input_path = build_writer_input_snapshot_path(
        game_id, outputs_dir, output_tag
    )
    raw_response_path = build_raw_response_output_path(
        game_id, outputs_dir, output_tag
    )
    reserved_paths = {
        **paths,
        "rejected": rejected_path,
        "attempt": attempt_path,
        "writer_input": writer_input_path,
        "raw_response": raw_response_path,
    }
    _refuse_existing_outputs(reserved_paths)
    prepared = _prepare_historical_pregame_agent_input(
        game_id,
        cutoff_time,
        database_path=database_path,
        snapshot_database_path=snapshot_database_path,
        player_evidence_path=player_evidence_path,
        player_evidence_manifest_path=player_evidence_manifest_path,
    )
    database_hash_before = prepared["database_hash_before"]
    snapshot_hash_before = prepared["snapshot_hash_before"]
    identity = prepared["identity"]
    cutoff = prepared["cutoff"]
    evidence = prepared["evidence"]
    availability = prepared["availability"]
    player_availability_context = prepared["player_availability_context"]
    player_availability_summary = prepared["player_availability_summary"]
    injury_candidates = prepared["injury_candidates"]
    tool_trace = prepared["tool_trace"]
    writer_messages = prepared["writer_messages"]
    artifact_set_id = uuid.uuid4().hex
    attempt_id = uuid.uuid4().hex
    started_at_utc = _utc_now()
    normalized_cutoff = cutoff.isoformat(timespec="seconds")
    writer_input = _build_writer_input_snapshot(
        artifact_set_id=artifact_set_id,
        attempt_id=attempt_id,
        generated_at_utc=started_at_utc,
        game_id=game_id,
        cutoff_time=normalized_cutoff,
        model=model,
        output_tag=output_tag,
        writer_messages=writer_messages,
        prepared=prepared,
        snapshot_path=writer_input_path,
    )
    writer_input_text = _json_artifact_text(writer_input)
    writer_input_sha256 = sha256_text(writer_input_text)
    attempt = _build_api_attempt_record(
        artifact_set_id=artifact_set_id,
        attempt_id=attempt_id,
        started_at_utc=started_at_utc,
        game_id=game_id,
        cutoff_time=normalized_cutoff,
        model=model,
        output_tag=output_tag,
        attempt_path=attempt_path,
        writer_input_path=writer_input_path,
        writer_input_sha256=writer_input_sha256,
        agent_messages_sha256=writer_input["agent_messages_sha256"],
    )
    _exclusive_create_text(attempt_path, _json_artifact_text(attempt))
    _atomic_publish_texts({writer_input_path: writer_input_text})
    try:
        writer_result = api_client(
            writer_messages,
            api_key,
            model,
            WRITER_MAX_TOKENS,
            DEFAULT_TIMEOUT_SECONDS,
        )
    except DeepSeekRequestError as api_error:
        raw_response_reference = None
        if api_error.response_received:
            _update_api_attempt(
                attempt_path,
                attempt_id,
                "api_response_received",
                response_received=True,
                response={
                    "http_response_body_sha256": api_error.raw_response_sha256,
                    "http_response_body_byte_count": api_error.raw_response_byte_count,
                    "model_content_available": False,
                },
            )
            raw_artifact = _build_raw_response_artifact(
                attempt_id=attempt_id,
                game_id=game_id,
                cutoff_time=normalized_cutoff,
                model=model,
                output_tag=output_tag,
                failure=api_error,
                raw_response_path=raw_response_path,
            )
            _atomic_publish_texts(
                {raw_response_path: _json_artifact_text(raw_artifact)}
            )
            raw_response_reference = {
                "path": str(raw_response_path),
                "sha256": sha256_file(raw_response_path),
                "response_body_sha256": api_error.raw_response_sha256,
                "response_body_byte_count": api_error.raw_response_byte_count,
            }
        _update_api_attempt(
            attempt_path,
            attempt_id,
            "api_failure",
            response_received=api_error.response_received,
            error={
                "type": api_error.error_type,
                "summary": api_error.safe_summary,
                "http_status": api_error.http_status,
                "empty_response_kind": api_error.empty_response_kind,
            },
            raw_response_artifact=raw_response_reference,
        )
        raise PregameAgentError(
            f"{api_error}；调用失败证据已保存：{attempt_path}"
        ) from api_error
    except Exception as api_error:
        safe_type = type(api_error).__name__
        _update_api_attempt(
            attempt_path,
            attempt_id,
            "api_failure",
            response_received=False,
            error={
                "type": "api_client_error",
                "summary": f"api_client raised {safe_type}; exception text was not persisted",
                "http_status": None,
                "empty_response_kind": None,
            },
            raw_response_artifact=None,
        )
        raise PregameAgentError(
            f"DeepSeek 调用失败；调用失败证据已保存：{attempt_path}"
        ) from api_error

    if not isinstance(writer_result, ApiResult):
        _update_api_attempt(
            attempt_path,
            attempt_id,
            "api_failure",
            response_received=True,
            error={
                "type": "response_contract_error",
                "summary": "api_client returned a value outside the ApiResult contract",
                "http_status": None,
                "empty_response_kind": None,
            },
            raw_response_artifact=None,
        )
        raise PregameAgentError(
            f"DeepSeek 响应对象不符合程序契约；调用失败证据已保存：{attempt_path}"
        )
    generated_at_utc = _utc_now()
    model_response_sha256 = sha256_text(writer_result.content)
    _update_api_attempt(
        attempt_path,
        attempt_id,
        "api_response_received",
        response_received=True,
        response={
            "model_content_sha256": model_response_sha256,
            "finish_reason": writer_result.finish_reason,
            "usage": writer_result.usage,
        },
        error=None,
    )
    try:
        final = validate_writer_response(
            writer_result.content,
            evidence,
            injury_candidates,
            player_role_evidence_context=prepared["player_role_evidence_context"],
        )
    except PregameAgentError as validation_error:
        database_hash_after = sha256_file(database_path)
        snapshot_hash_after = sha256_file(snapshot_database_path)
        rejected = build_rejected_artifact(
            artifact_set_id=artifact_set_id,
            generated_at_utc=generated_at_utc,
            output_tag=output_tag,
            game_id=game_id,
            cutoff_time=normalized_cutoff,
            model=model,
            writer_result=writer_result,
            model_response_sha256=model_response_sha256,
            validation_error=str(validation_error),
            database_path=database_path,
            database_hash_before=database_hash_before,
            database_hash_after=database_hash_after,
            snapshot_database_path=snapshot_database_path,
            snapshot_hash_before=snapshot_hash_before,
            snapshot_hash_after=snapshot_hash_after,
            rejected_path=rejected_path,
            player_availability_context=player_availability_context,
            player_availability_summary=player_availability_summary,
            injury_candidates=injury_candidates,
            attempt_path=attempt_path,
            writer_input_path=writer_input_path,
            writer_input_sha256=writer_input_sha256,
        )
        _atomic_publish_texts(
            {
                rejected_path: json.dumps(
                    rejected, ensure_ascii=False, indent=2, allow_nan=False
                )
                + "\n"
            }
        )
        _update_api_attempt(
            attempt_path,
            attempt_id,
            "semantic_rejected",
            response_received=True,
            semantic_validation={
                "passed": False,
                "error": str(validation_error),
            },
            rejected_artifact={
                "path": str(rejected_path),
                "sha256": sha256_file(rejected_path),
            },
        )
        raise PregameAgentError(
            f"{validation_error}；失败证据已保存：{rejected_path}"
        ) from validation_error
    package = build_analysis_package(
        evidence,
        final,
        availability,
        artifact_set_id=artifact_set_id,
        generated_at_utc=generated_at_utc,
        model=model,
        model_response_sha256=model_response_sha256,
        injury_candidates=injury_candidates,
    )

    database_hash_after = sha256_file(database_path)
    snapshot_hash_after = sha256_file(snapshot_database_path)
    if database_hash_before != database_hash_after:
        raise PregameAgentError("历史分析数据库 SHA256 在运行期间发生变化，拒绝保存输出。")
    if snapshot_hash_before != snapshot_hash_after:
        raise PregameAgentError("赛前信息快照数据库 SHA256 在运行期间发生变化，拒绝保存输出。")

    trace = {
        "schema_version": "1.2",
        "artifact_set_id": artifact_set_id,
        "generated_at_utc": generated_at_utc,
        "output_tag": output_tag,
        "attempt_id": attempt_id,
        "game_id": game_id,
        "cutoff_time": normalized_cutoff,
        "model": model,
        "api_request_count": 1,
        "retry_count": 0,
        "planner_decision": "skipped_for_single_call_injury_integration",
        "api_requests": [
            {
                "stage": "final_prediction_generation",
                "finish_reason": writer_result.finish_reason,
                "usage": writer_result.usage,
                "response_sha256": model_response_sha256,
            },
        ],
        "tool_calls": tool_trace,
        "database": {
            "path": str(database_path),
            "mode": "read_only_query_only",
            "sha256_before": database_hash_before,
            "sha256_after": database_hash_after,
        },
        "pregame_snapshot_database": {
            "path": str(snapshot_database_path),
            "mode": "read_only_query_only",
            "view": "agent_eligible_injury_snapshots",
            "sha256_before": snapshot_hash_before,
            "sha256_after": snapshot_hash_after,
        },
        "excluded_sources": [
            "v2 detailed game database",
            "target game outcome",
            "target game player boxscores",
            "target game play by play",
            "postgame brief",
            "unverified web or memory claims",
        ],
        "artifact_files": {key: str(path) for key, path in paths.items()},
        "api_attempt_artifact": str(attempt_path),
        "writer_input_artifact": {
            "path": str(writer_input_path),
            "sha256": writer_input_sha256,
        },
    }
    texts = {
        paths["json"]: json.dumps(package, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n",
        paths["markdown"]: render_markdown(package),
        paths["trace"]: json.dumps(trace, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n",
    }
    _atomic_publish_texts(texts)
    _update_api_attempt(
        attempt_path,
        attempt_id,
        "success",
        response_received=True,
        semantic_validation={"passed": True, "error": None},
        successful_artifacts={
            key: {"path": str(path), "sha256": sha256_file(path)}
            for key, path in paths.items()
        },
    )
    return paths


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.audit_input_only:
            audit_path = build_historical_pregame_agent_input_audit(
                args.game_id,
                args.cutoff_time,
                player_evidence_path=args.player_evidence_path,
                player_evidence_manifest_path=args.player_evidence_manifest_path,
                output_tag=args.output_tag,
            )
            print(f"Agent 输入审计生成成功（未调用 DeepSeek）：{audit_path}")
            return 0
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        model = args.model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
        paths = generate_historical_pregame_analysis(
            args.game_id,
            args.cutoff_time,
            api_key,
            model=model,
            player_evidence_path=args.player_evidence_path,
            player_evidence_manifest_path=args.player_evidence_manifest_path,
            output_tag=args.output_tag,
        )
    except PregameAgentError as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1
    print("历史赛前分析生成成功：")
    print(f"机器事实：{paths['json']}")
    print(f"中文分析：{paths['markdown']}")
    print(f"工具记录：{paths['trace']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
