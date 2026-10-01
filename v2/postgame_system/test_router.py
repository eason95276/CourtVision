from __future__ import annotations

import pytest

from .router import CourtVisionRouter
from .test_briefs import RECORDS


@pytest.mark.parametrize(
    ("question", "route"),
    [
        ("明天湖人赛前预测", "pregame"),
        ("复盘 T1 对 T2 的比赛", "postgame"),
        ("2026-01-01 每日比赛简报", "daily_brief"),
        ("湖人最近十场", "team_brief"),
        ("湖人火箭系列赛简报", "series_brief"),
        ("今天哪些比赛模型错得最多", "topic_brief"),
        ("最近哪些球队变化最大", "topic_brief"),
        ("哪些比赛模型和盘口分歧最大", "topic_brief"),
    ],
)
def test_route(question, route):
    router = CourtVisionRouter(records=RECORDS)
    assert router.plan(question).route == route


def test_postgame_route_executes_unique_team_pair():
    router = CourtVisionRouter(records=RECORDS)
    result = router.ask("复盘 1 对 2 在 2026-01-01 的比赛")
    assert result["route"] == "postgame"
    assert result["result"]["game"]["game_id"] == "g1"


def test_daily_and_topic_routes_execute_deterministic_tools():
    router = CourtVisionRouter(records=RECORDS)
    daily = router.ask("2026-01-01 每日比赛简报")
    topic = router.ask("哪些比赛模型和盘口分歧最大")
    assert daily["result"]["game_count"] == 2
    assert topic["result"]["topic"] == "largest_model_market_disagreement"


def test_team_route_without_identifiable_team_returns_clarify():
    router = CourtVisionRouter(records=RECORDS)
    result = router.ask("最近十场球队简报")
    assert result["route"] == "clarify"
    assert "球队" in result["message"]


def test_pregame_route_delegates_without_copying_pregame_logic():
    class Pregame:
        def ask(self, question, **kwargs):
            return {"delegated": question}, {"session": "pregame"}

    router = CourtVisionRouter(records=RECORDS, pregame_agent=Pregame())
    result = router.ask("明天湖人赛前预测")
    assert result["route"] == "pregame"
    assert result["result"]["delegated"] == "明天湖人赛前预测"


def test_team_router_preserves_explicit_date_range():
    router = CourtVisionRouter(records=RECORDS)
    plan = router.plan("湖人 2026-01-01 到 2026-01-31 球队简报")
    assert plan.route == "team_brief"
    assert plan.start_date == "2026-01-01"
    assert plan.end_date == "2026-01-31"
    assert plan.clarification is None


def test_topic_router_applies_explicit_day_scope():
    router = CourtVisionRouter(records=RECORDS)
    result = router.ask("2026-01-01 哪些比赛模型错得最多")
    assert result["route"] == "topic_brief"
    assert {item["game_id"] for item in result["result"]["items"]} == {"g1", "g2"}
