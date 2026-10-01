"""Thin deterministic router across pregame, postgame and brief tools."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from v2.pregame_system.agent import TEAM_NAMES

from .briefs import build_daily_brief, build_series_brief, build_team_brief, build_topic_brief


EASTERN = ZoneInfo("America/New_York")
CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


class GlobalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: Literal[
        "pregame",
        "postgame",
        "daily_brief",
        "team_brief",
        "series_brief",
        "topic_brief",
        "clarify",
    ]
    game_id: str | None = None
    game_date: str | None = None
    team_ids: list[int] = Field(default_factory=list, max_length=2)
    recent_n: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    ordered_game_ids: list[str] = Field(default_factory=list)
    topic: Literal[
        "largest_prediction_error",
        "largest_team_change",
        "largest_model_market_disagreement",
    ] | None = None
    clarification: str | None = None


def _team_ids(question: str) -> list[int]:
    result = []
    for team_id, name in TEAM_NAMES.items():
        aliases = {name, str(team_id)}
        if any(alias in question for alias in aliases):
            result.append(team_id)
    for raw in re.findall(r"(?<!\d)(\d{1,10})(?!\d)", question):
        value = int(raw)
        if value in TEAM_NAMES and value not in result:
            result.append(value)
        elif 0 < value < 100 and value not in result:
            result.append(value)
    return result[:2]


def _date(question: str) -> str | None:
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", question)
    if match:
        datetime.fromisoformat(match.group(1))
        return match.group(1)
    if "今天" in question or "今日" in question:
        return datetime.now(EASTERN).date().isoformat()
    return None


def _recent_n(question: str) -> int | None:
    match = re.search(r"最近\s*(\d{1,2})\s*场", question)
    if match:
        return int(match.group(1))
    match = re.search(r"最近\s*([一二三四五六七八九十])\s*场", question)
    return CHINESE_NUMBERS.get(match.group(1)) if match else None


class CourtVisionRouter:
    def __init__(self, *, records: list[dict[str, Any]], pregame_agent=None):
        self.records = records
        self.pregame_agent = pregame_agent

    def plan(self, question: str) -> GlobalPlan:
        text = question.strip()
        teams = _team_ids(text)
        day = _date(text)
        game_match = re.search(r"(?:espn:\d+|\b\d{10}\b|\bg\d+\b)", text)
        game_id = game_match.group(0) if game_match else None
        if "错得最多" in text or "预测偏差最大" in text:
            return GlobalPlan(route="topic_brief", topic="largest_prediction_error",game_date=day)
        if "球队变化最大" in text or "哪些球队变化最大" in text:
            return GlobalPlan(route="topic_brief", topic="largest_team_change",recent_n=10)
        if "盘口分歧最大" in text or "模型和盘口分歧" in text:
            return GlobalPlan(route="topic_brief", topic="largest_model_market_disagreement",game_date=day)
        if "系列赛" in text:
            ids = re.findall(r"\b(?:00\d{8}|g\d+)\b", text)
            return GlobalPlan(
                route="series_brief",
                team_ids=teams,
                ordered_game_ids=ids,
                clarification=None if len(teams) == 2 and ids else "请提供两支球队和有序比赛清单。",
            )
        if "每日" in text or "今日简报" in text or "今天比赛简报" in text:
            return GlobalPlan(
                route="daily_brief",
                game_date=day,
                clarification=None if day else "请提供美国东部比赛日。",
            )
        recent = _recent_n(text)
        if recent or "球队简报" in text or "过去两周" in text:
            dates = re.findall(r"20\d{2}-\d{2}-\d{2}", text)
            start_date = dates[0] if len(dates) >= 2 else None
            end_date = dates[1] if len(dates) >= 2 else None
            if "过去两周" in text:
                end = datetime.now(EASTERN).date()
                start_date = (end - timedelta(days=13)).isoformat()
                end_date = end.isoformat()
            return GlobalPlan(
                route="team_brief",
                team_ids=teams[:1],
                recent_n=recent,
                start_date=start_date,
                end_date=end_date,
                clarification=None if teams and (recent or (start_date and end_date)) else "请明确球队和时间范围。",
            )
        if "复盘" in text or "赛后" in text:
            return GlobalPlan(route="postgame", game_id=game_id, game_date=day, team_ids=teams)
        if "赛前" in text or "预测" in text or "明天" in text:
            return GlobalPlan(route="pregame", team_ids=teams)
        return GlobalPlan(route="clarify", clarification="请明确需要赛前分析、单场复盘或哪一种简报。")

    def _postgame(self, plan: GlobalPlan) -> dict[str, Any]:
        candidates = self.records
        if plan.game_id:
            candidates = [record for record in candidates if record["game"]["game_id"] == plan.game_id]
        if plan.game_date:
            candidates = [record for record in candidates if record["game"]["game_date"] == plan.game_date]
        for team_id in plan.team_ids:
            candidates = [
                record
                for record in candidates
                if team_id in {record["game"]["home_team_id"], record["game"]["away_team_id"]}
            ]
        if len(candidates) != 1:
            raise ValueError(f"postgame_scope_not_unique:{len(candidates)}")
        return candidates[0]

    def ask(self, question: str, *, plan: GlobalPlan | dict | None = None) -> dict[str, Any]:
        selected = GlobalPlan.model_validate(plan) if plan is not None else self.plan(question)
        if selected.clarification or selected.route == "clarify":
            return {
                "route": "clarify",
                "message": selected.clarification or "请补充查询范围。",
                "plan": selected.model_dump(),
            }
        try:
            if selected.route == "pregame":
                if self.pregame_agent is None:
                    raise ValueError("pregame_agent_unavailable")
                result, session = self.pregame_agent.ask(question)
                return {"route": "pregame", "result": result, "session": session, "plan": selected.model_dump()}
            if selected.route == "postgame":
                result = self._postgame(selected)
            elif selected.route == "daily_brief":
                result = build_daily_brief(selected.game_date, self.records)
            elif selected.route == "team_brief":
                if not selected.team_ids:
                    raise ValueError("team_scope_missing")
                result = build_team_brief(
                    selected.team_ids[0],
                    self.records,
                    recent_n=selected.recent_n,
                    start_date=selected.start_date,
                    end_date=selected.end_date,
                )
            elif selected.route == "series_brief":
                result = build_series_brief(
                    team_ids=selected.team_ids,
                    ordered_game_ids=selected.ordered_game_ids,
                    records=self.records,
                )
            elif selected.route == "topic_brief":
                scoped_records=self.records
                if selected.game_date:
                    scoped_records=[record for record in scoped_records if record['game']['game_date']==selected.game_date]
                result = build_topic_brief(selected.topic, scoped_records)
            else:
                raise ValueError("unsupported_global_route")
            return {"route": selected.route, "result": result, "plan": selected.model_dump()}
        except ValueError as exc:
            return {
                "route": "clarify",
                "message": str(exc),
                "plan": selected.model_dump(),
            }
