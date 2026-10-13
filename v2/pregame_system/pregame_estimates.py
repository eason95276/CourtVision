"""Typed analyst plans; no model features or numeric adjustments."""
from __future__ import annotations

import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer


class LineupPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["home", "away"]
    basis: Literal["confirmed", "source_expected", "agent_expected", "unavailable"]
    players: list[str] = Field(default_factory=list, max_length=5)
    candidates: list[str] = Field(default_factory=list, max_length=5,
        description='Ordered reserve lineup candidates with current identity and role/rotation basis; not confirmed starters. Do not fill from membership alone.')
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    rationale: str = Field(min_length=1, max_length=500)

    @model_serializer(mode='wrap')
    def omit_empty_candidates(self, handler):
        result=handler(self)
        if not self.candidates:
            result.pop('candidates',None)
        return result

    @model_validator(mode="after")
    def complete(self):
        if self.basis == "unavailable":
            if self.players:
                raise ValueError("unavailable_lineup_has_players")
        elif len(self.players) != 5 or len(set(self.players)) != 5 or not self.evidence_ids:
            raise ValueError("lineup_requires_five_distinct_players_and_evidence")
        return self


class PlayerUsagePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["home", "away"]
    player_name: str = Field(min_length=1, max_length=100)
    basis: Literal["source_confirmed", "agent_estimate"]
    minutes_min: int = Field(ge=0, le=48)
    minutes_max: int = Field(ge=0, le=48)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def ordered(self):
        if self.minutes_min > self.minutes_max:
            raise ValueError("minutes_range_reversed")
        return self


class PregamePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lineups: list[LineupPlan] = Field(min_length=2, max_length=2)
    player_usage: list[PlayerUsagePlan] = Field(default_factory=list, max_length=8)
    key_matchup: str = Field(min_length=1, max_length=700)
    uncertainty: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def both_teams(self):
        if {item.side for item in self.lineups} != {"home", "away"}:
            raise ValueError("lineup_sides_incomplete")
        return self


def _name_in(name, text):
    return bool(re.search(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])",
                          str(text), re.I))


def validate_pregame_plan(plan: PregamePlan | None, basketball: dict):
    """Validate identity/status only; fit and projected load remain analyst judgment."""
    if plan is None:
        return
    game = basketball.get("game") or {}
    evidence = {item["id"]: item for item in basketball.get("evidence") or [] if item.get("id")}
    team_ids = {side: (game.get(side) or {}).get("id", game.get(side+"_team_id")) for side in ("home", "away")}
    rosters = {"home": set(), "away": set()}
    for item in evidence.values():
        if item.get("section") != "roster_context":
            continue
        for side, team in (item.get("value", {}).get("teams") or {}).items():
            roster = team.get("official_team_roster") or team.get("current_team_roster") or team.get("roster") or {}
            if side in rosters and roster.get("status") == "available":
                rosters[side].update(p.get("name") or p.get("player_name") for p in roster.get("players") or [])

    def sources(refs):
        if not set(refs) <= set(evidence):
            raise ValueError("unknown_plan_evidence")
        return [evidence[ref] for ref in refs]

    def same_team(item, side):
        return (item.get("value") or {}).get("team_id") == team_ids[side]

    def current_member(side, name, refs):
        if name in rosters[side]:
            return True
        for item in sources(refs):
            value = item.get("value") or {}
            if item.get("section") != "web_context" or not same_team(item, side) or value.get("eligible_for_fact") is not True:
                continue
            quote = str(value.get("quote") or "")
            if not _name_in(name, quote):
                continue
            if value.get("claim_type") in {"current_roster", "projected_starters", "confirmed_starter"}:
                return True
            # A qualified report of this offseason's acquisition or current
            # core pairing can establish affiliation for an estimate. It does
            # not confirm tonight's availability, starters or minutes.
            if (value.get("temporal_scope") == "current_season_background"
                and value.get("source_role") in {"primary", "professional"}
                and not re.search(r"traded away|departed|left the|former|上赛季|离队", quote, re.I)):
                if value.get("claim_type") == "personnel_change":
                    transaction = re.search(r"(?:acquir\w*|signed|added|received|引进|得到)\s*(.+)", quote, re.I)
                    if transaction:
                        incoming = re.split(r"\b(?:in exchange|for|sending|sent)\b|送出|付出", transaction.group(1), maxsplit=1, flags=re.I)[0]
                        if _name_in(name, incoming):
                            return True
                    joined = re.search(r"joined|加盟", quote, re.I)
                    if joined and _name_in(name, quote[:joined.start()]):
                        return True
                if value.get("claim_type") == "role_background" and re.search(r"frontcourt|pairing|new core|will play|will lead|核心组合|前场组合|新阵容", quote, re.I):
                    return True
        return False

    def out(side, name):
        for item in evidence.values():
            value = item.get("value") or {}
            rows = value.get("records") or [] if item.get("section") == "availability" else [value]
            for row in rows:
                if row.get("team_id") != team_ids[side]:
                    continue
                if item.get("section") == "web_context" and (
                    row.get("claim_type") != "availability" or not row.get("eligible_for_fact")):
                    continue
                if row.get("game_id") and row["game_id"] != game.get("game_id"):
                    continue
                text = str(row.get("quote") or "")
                named = _name_in(name, row.get("player_name") or text)
                if named and (str(row.get("status") or "").casefold() in {"out", "inactive", "suspended"}
                    or re.search(r"\b(?:ruled out|is out|listed as out|will miss)\b|确认缺阵", text, re.I)):
                    return True
        return False

    for lineup in plan.lineups:
        refs = sources(lineup.evidence_ids)
        for name in [*lineup.players,*lineup.candidates]:
            if not current_member(lineup.side, name, lineup.evidence_ids):
                raise ValueError("estimated_lineup_current_identity_unverified:"+name)
            if out(lineup.side, name):
                raise ValueError("confirmed_out_in_projected_lineup:"+name)
            if name in lineup.players and lineup.basis in {"confirmed", "source_expected"}:
                kind = "confirmed_starter" if lineup.basis == "confirmed" else "projected_starters"
                if not any(same_team(item, lineup.side) and (item.get("value") or {}).get("claim_type") == kind
                    and (item.get("value") or {}).get("eligible_for_fact") is True
                    and _name_in(name, (item.get("value") or {}).get("quote", "")) for item in refs):
                    raise ValueError("lineup_basis_source_unverified:"+name)
    for usage in plan.player_usage:
        refs = sources(usage.evidence_ids)
        if not current_member(usage.side, usage.player_name, usage.evidence_ids):
            raise ValueError("estimated_usage_current_identity_unverified:"+usage.player_name)
        if out(usage.side, usage.player_name) and usage.minutes_max > 0:
            raise ValueError("confirmed_out_has_positive_estimated_minutes")
        if usage.basis == "source_confirmed":
            quotes = [str((item.get("value") or {}).get("quote") or "") for item in refs
                if same_team(item, usage.side) and (item.get("value") or {}).get("claim_type") == "minutes_restriction"
                and (item.get("value") or {}).get("eligible_for_fact") is True
                and _name_in(usage.player_name, (item.get("value") or {}).get("quote", ""))]
            if not quotes or not all(any(re.search(rf"(?<!\d){n}(?!\d)", quote) for quote in quotes)
                                     for n in {usage.minutes_min, usage.minutes_max}):
                raise ValueError("confirmed_minutes_source_unverified")
