"""Stable, compact display model for the 第25秒 frontend.

The browser receives only fields needed for product rendering and analyst
context. Raw roster payloads and training assets stay outside the runtime
snapshot.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


PLAYER_NAMES_ZH_BY_ID = {
    "2544": "勒布朗·詹姆斯",
    "203114": "克里斯·米德尔顿",
    "203497": "鲁迪·戈贝尔",
    "203507": "扬尼斯·阿德托昆博",
    "203954": "乔尔·恩比德",
    "1626157": "卡尔-安东尼·唐斯",
    "1627759": "杰伦·布朗",
    "1628368": "达龙·福克斯",
    "1628369": "杰森·塔图姆",
    "1628384": "OG·阿奴诺比",
    "1628389": "巴姆·阿德巴约",
    "1628404": "约什·哈特",
    "1628969": "米卡尔·布里奇斯",
    "1628973": "杰伦·布伦森",
    "1628983": "谢伊·吉尔杰斯-亚历山大",
    "1629027": "特雷·杨",
    "1629632": "科比·怀特",
    "1629639": "泰勒·希罗",
    "1629651": "尼古拉斯·克拉克斯顿",
    "1630162": "安东尼·爱德华兹",
    "1630163": "拉梅洛·鲍尔",
    "1630178": "泰瑞斯·马克西",
    "1630193": "伊曼纽尔·奎克利",
    "1630532": "弗朗茨·瓦格纳",
    "1630552": "杰伦·约翰逊",
    "1630567": "斯科蒂·巴恩斯",
    "1630595": "凯德·坎宁安",
    "1631094": "保罗·班凯罗",
    "1631096": "切特·霍姆格伦",
    "1631105": "杰伦·杜伦",
    "1631114": "杰伦·威廉姆斯",
    "1641705": "维克托·文班亚马",
    "1641706": "布兰登·米勒",
    "203083": "安德烈·德拉蒙德",
    "203484": "肯塔维奥斯·考德威尔-波普",
    "203903": "乔丹·克拉克森",
    "1629013": "兰德里·沙梅特",
    "1629014": "安芬尼·西蒙斯",
    "1630540": "迈尔斯·麦克布赖德",
    "1630631": "何塞·阿尔瓦拉多",
    "1641755": "凯文·麦克卡勒",
    "1642278": "泰勒·科勒克",
    "1642845": "VJ·埃奇库姆",
    "1642885": "穆罕默德·迪亚瓦拉",
    "1643583": "杰克·凯伊尔",
    "1643589": "泰勒·尼克尔",
}

SOURCE_FIELDS = (
    "url",
    "captured_at",
    "published_at",
    "sha256",
    "source_type",
    "parser_version",
    "status",
)

COLD_FIELDS = (
    "status",
    "cold_start",
    "current_season_games_observed",
    "rolling_5_current_season_games",
    "rolling_5_previous_season_games",
    "rolling_5_older_season_games",
    "rolling_15_current_season_games",
    "rolling_15_previous_season_games",
    "rolling_15_older_season_games",
    "previous_season_is_majority",
)

CONTINUITY_FIELDS = (
    "status",
    "retained_minutes_ratio",
    "top8_retained_count",
    "top8_count",
    "top8_retained_minutes_ratio",
    "roster_identity_coverage",
    "minute_rows_coverage",
    "reason",
    "newly_listed_players",
    "previous_participants_not_listed",
)


def display_name(player: dict[str, Any] | str | None) -> str:
    if isinstance(player, str):
        return player
    if not player:
        return "球员"
    canonical = str(player.get("name") or "").strip()
    return PLAYER_NAMES_ZH_BY_ID.get(str(player.get("player_id")), canonical or "球员")


def _source(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    result = {key: value.get(key) for key in SOURCE_FIELDS if value.get(key) is not None}
    return result or None


def _player(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in {
            "player_id": str(value.get("player_id")) if value.get("player_id") is not None else None,
            "name": value.get("name"),
            "display_name": display_name(value),
            "team_id": value.get("team_id"),
            "position": value.get("position"),
            "number": value.get("number"),
        }.items()
        if item not in (None, "")
    }


def _players(values: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [_player(value) for value in values or [] if value.get("name")]


def compact_roster_context(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    result: dict[str, Any] = {
        "game_id": value.get("game_id"),
        "context_cutoff": value.get("context_cutoff") or value.get("cutoff"),
        "numeric_model_usage": False,
        "teams": {},
    }
    for side, team in (value.get("teams") or {}).items():
        team = team or {}
        roster = team.get("official_team_roster") or team.get("roster") or {}
        continuity = team.get("continuity") or {}
        cold = team.get("cold_start") or {}
        assignment = team.get("league_team_assignment") or {}
        current_rotation = team.get("current_rotation") or {}
        result["teams"][side] = {
            "team_id": team.get("team_id"),
            "roster_basis": team.get("roster_basis"),
            "official_team_roster": {
                "status": roster.get("status"),
                "players": _players(roster.get("players")),
                "source": _source(roster.get("source")),
            },
            "league_team_assignment": {
                "status": assignment.get("status"),
                "player_count": len(assignment.get("players") or []),
            } if assignment else None,
            "continuity": {
                key: deepcopy(continuity.get(key))
                for key in CONTINUITY_FIELDS
                if key in continuity
            },
            "cold_start": {
                key: deepcopy(cold.get(key))
                for key in COLD_FIELDS
                if key in cold
            },
            "key_player_status": deepcopy(team.get("key_player_status")),
            "confirmed_starters": _players(team.get("confirmed_starters")),
            "projected_starters": _players(
                team.get("projected_starters")
                or current_rotation.get("projected_starters")
            ),
            "missing": deepcopy(team.get("missing") or []),
        }
    return result


def compact_app_data(payload: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(payload)
    value["schema_version"] = "2.0"
    value["product"]["name"] = "第25秒"
    value["product"]["slogan"] = "看见比分之外"
    value["product"]["stage"] = "final product demo"
    for game in value.get("games") or []:
        game["roster_context"] = compact_roster_context(game.get("roster_context"))
        game.setdefault("postgame", {"status": "unavailable"})
    return value
