"""Deterministic bounded brief queries over postgame records."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v2.pregame_system.storage import clean, content_hash, now


Topic = Literal[
    "largest_prediction_error",
    "largest_team_change",
    "largest_model_market_disagreement",
]


class BriefQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["daily", "team", "series", "topic"]
    game_date: str | None = None
    team_id: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    recent_n: int | None = Field(None, ge=1, le=82)
    team_ids: list[int] = Field(default_factory=list, max_length=2)
    ordered_game_ids: list[str] = Field(default_factory=list)
    topic: Topic | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if self.mode == "daily" and not self.game_date:
            raise ValueError("daily_date_required")
        if self.mode == "team" and self.team_id is None:
            raise ValueError("team_id_required")
        if self.mode == "series" and (
            len(self.team_ids) != 2 or not self.ordered_game_ids
        ):
            raise ValueError("series_scope_required")
        if self.mode == "topic" and self.topic is None:
            raise ValueError("topic_required")
        return self


def _finalize(payload: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    core = {
        "schema_version": "1.0",
        **clean(payload),
        "source_record_hashes": [record["record_hash"] for record in records],
    }
    return {**core, "brief_hash": content_hash(core), "generated_at": now()}


def _public(record: dict[str, Any]) -> dict[str, Any] | None:
    return record.get("prediction_review", {}).get("public_prediction")


def _prediction_error_item(record: dict[str, Any]) -> dict[str, Any] | None:
    public = _public(record)
    if not public:
        return None
    errors = public["errors"]
    score = max(
        float(errors.get("absolute_margin_error") or 0),
        float(errors.get("absolute_total_error") or 0),
    )
    return {
        "game_id": record["game"]["game_id"],
        "error_score": score,
        "absolute_margin_error": errors.get("absolute_margin_error"),
        "absolute_total_error": errors.get("absolute_total_error"),
        "win_correct": errors.get("win_correct"),
    }


def _market_disagreement_item(record: dict[str, Any]) -> dict[str, Any] | None:
    public = _public(record)
    market = record.get("prediction_review", {}).get("market_actual_comparison") or {}
    if not public or market.get("status") != "available":
        return None
    prediction = public["prediction"]
    margin_line = (market.get("margin") or {}).get("market_expected_home_margin")
    total_line = (market.get("total") or {}).get("market_total")
    margin_gap = (
        abs(float(prediction["expected_margin"]) - float(margin_line))
        if margin_line is not None
        else None
    )
    total_gap = (
        abs(float(prediction["expected_total"]) - float(total_line))
        if total_line is not None
        else None
    )
    available = [value for value in (margin_gap, total_gap) if value is not None]
    if not available:
        return None
    return {
        "game_id": record["game"]["game_id"],
        "disagreement_score": max(available),
        "margin_gap": margin_gap,
        "total_gap": total_gap,
    }


def build_daily_brief(
    game_date: str, records: list[dict[str, Any]]
) -> dict[str, Any]:
    date.fromisoformat(game_date)
    selected = [record for record in records if record["game"]["game_date"] == game_date]
    if not selected:
        raise ValueError("daily_brief_no_games")
    errors = [item for record in selected if (item := _prediction_error_item(record))]
    disagreements = [
        item for record in selected if (item := _market_disagreement_item(record))
    ]
    noteworthy = sorted(
        [
            {
                "game_id": record["game"]["game_id"],
                "actual_margin": abs(record["game"]["actual_home_margin"]),
                "actual_total": record["game"]["actual_total"],
                "reason": "close_game"
                if abs(record["game"]["actual_home_margin"]) <= 5
                else "large_result_or_prediction_error",
            }
            for record in selected
        ],
        key=lambda item: (
            item["reason"] != "close_game",
            -item["actual_total"],
            item["game_id"],
        ),
    )
    return _finalize(
        {
            "mode": "daily",
            "game_date": game_date,
            "game_count": len(selected),
            "games": [record["game"] for record in selected],
            "largest_prediction_errors": sorted(
                errors, key=lambda item: (-item["error_score"], item["game_id"])
            ),
            "model_market_disagreements": sorted(
                disagreements,
                key=lambda item: (-item["disagreement_score"], item["game_id"]),
            ),
            "noteworthy_games": noteworthy,
            "missing_prediction_games": [
                record["game"]["game_id"] for record in selected if not _public(record)
            ],
        },
        selected,
    )


def _team_view(team_id: int, record: dict[str, Any]) -> dict[str, Any] | None:
    game = record["game"]
    if team_id == game["home_team_id"]:
        points = game["home_points"]
        opponent_points = game["away_points"]
        opponent = game["away_team_id"]
        home = True
    elif team_id == game["away_team_id"]:
        points = game["away_points"]
        opponent_points = game["home_points"]
        opponent = game["home_team_id"]
        home = False
    else:
        return None
    return {
        "game_id": game["game_id"],
        "game_date": game["game_date"],
        "opponent_team_id": opponent,
        "home": home,
        "points": points,
        "opponent_points": opponent_points,
        "margin": points - opponent_points,
        "win": points > opponent_points,
    }


def _averages(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {
        key: sum(float(row[key]) for row in rows) / len(rows)
        for key in ("points", "opponent_points", "margin")
    }


def build_team_brief(
    team_id: int,
    records: list[dict[str, Any]],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    recent_n: int | None = None,
) -> dict[str, Any]:
    if recent_n is not None and (start_date or end_date):
        raise ValueError("team_brief_scope_conflict")
    if recent_n is None and not (start_date and end_date):
        raise ValueError("team_brief_scope_required")
    rows = sorted(
        [view for record in records if (view := _team_view(team_id, record))],
        key=lambda row: (row["game_date"], row["game_id"]),
    )
    if recent_n is not None:
        if not 1 <= recent_n <= 82:
            raise ValueError("team_recent_n_out_of_range")
        selected = rows[-recent_n:]
        scope = {"recent_n": recent_n, "start_date": None, "end_date": None}
    else:
        date.fromisoformat(start_date)
        date.fromisoformat(end_date)
        if start_date > end_date:
            raise ValueError("team_date_range_reversed")
        selected = [row for row in rows if start_date <= row["game_date"] <= end_date]
        scope = {"recent_n": None, "start_date": start_date, "end_date": end_date}
    if not selected:
        raise ValueError("team_brief_no_games")
    midpoint = len(selected) // 2
    if midpoint:
        prior = selected[:midpoint]
        recent = selected[midpoint:]
        prior_avg = _averages(prior)
        recent_avg = _averages(recent)
        trend = {
            "status": "available",
            "prior_games": len(prior),
            "recent_games": len(recent),
            "prior": prior_avg,
            "recent": recent_avg,
            "change": {key: recent_avg[key] - prior_avg[key] for key in prior_avg},
        }
    else:
        trend = {"status": "insufficient_games", "prior_games": 0, "recent_games": 1}
    source_records = [
        record for record in records if record["game"]["game_id"] in {row["game_id"] for row in selected}
    ]
    return _finalize(
        {
            "mode": "team",
            "team_id": team_id,
            "scope": scope,
            "games": len(selected),
            "wins": sum(row["win"] for row in selected),
            "losses": sum(not row["win"] for row in selected),
            "totals": {
                key: sum(float(row[key]) for row in selected)
                for key in ("points", "opponent_points", "margin")
            },
            "per_game": _averages(selected),
            "trend": trend,
            "game_rows": selected,
        },
        source_records,
    )


def build_series_brief(
    *,
    team_ids: list[int],
    ordered_game_ids: list[str],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(team_ids) != 2 or len(set(team_ids)) != 2:
        raise ValueError("series_requires_two_teams")
    if not ordered_game_ids or len(set(ordered_game_ids)) != len(ordered_game_ids):
        raise ValueError("series_game_ids_invalid")
    lookup = {record["game"]["game_id"]: record for record in records}
    included = []
    missing = []
    expected_teams = set(team_ids)
    for game_id in ordered_game_ids:
        record = lookup.get(game_id)
        if record is None:
            missing.append(game_id)
            continue
        game = record["game"]
        if {game["home_team_id"], game["away_team_id"]} != expected_teams:
            raise ValueError("series_game_team_mismatch")
        included.append(record)
    teams = []
    for team_id in team_ids:
        rows = [_team_view(team_id, record) for record in included]
        teams.append(
            {
                "team_id": team_id,
                "games": len(rows),
                "wins": sum(row["win"] for row in rows),
                "losses": sum(not row["win"] for row in rows),
                "totals": {
                    key: sum(float(row[key]) for row in rows)
                    for key in ("points", "opponent_points", "margin")
                },
                "per_team_game": _averages(rows) if rows else None,
            }
        )
    return _finalize(
        {
            "mode": "series",
            "team_ids": team_ids,
            "ordered_game_ids": ordered_game_ids,
            "expected_series_games": len(ordered_game_ids),
            "analyzed_games": len(included),
            "complete_series": not missing,
            "missing_game_ids": missing,
            "teams": teams,
            "games": [record["game"] for record in included],
            "denominator_policy": "all team averages use analyzed complete games; both teams share the same denominator",
        },
        included,
    )


def build_series_brief_from_manifest(
    manifest_path: str,
    *,
    selected_game_ids: list[str] | None = None,
    data_root: str | None = None,
) -> dict[str, Any]:
    """Reuse Day4's validated SQL aggregation with any conforming manifest."""
    from pathlib import Path
    from v2.day4.analysis import ROOT as DAY4_ROOT, SeriesAnalysis

    engine = SeriesAnalysis.load(
        manifest_path=Path(manifest_path),
        root=Path(data_root) if data_root else DAY4_ROOT,
    )
    result = engine.summarize(selected_game_ids)
    core = {
        "schema_version": "1.0",
        "mode": "series",
        "source_mode": "validated_series_manifest",
        **clean(result),
    }
    return {**core, "brief_hash": content_hash(core), "generated_at": now()}


def build_topic_brief(topic: Topic, records: list[dict[str, Any]]) -> dict[str, Any]:
    if topic == "largest_prediction_error":
        items = [item for record in records if (item := _prediction_error_item(record))]
        items.sort(key=lambda item: (-item["error_score"], item["game_id"]))
    elif topic == "largest_model_market_disagreement":
        items = [item for record in records if (item := _market_disagreement_item(record))]
        items.sort(key=lambda item: (-item["disagreement_score"], item["game_id"]))
    elif topic == "largest_team_change":
        by_team = defaultdict(list)
        for record in records:
            for team_id in (record["game"]["home_team_id"], record["game"]["away_team_id"]):
                by_team[team_id].append(_team_view(team_id, record))
        items = []
        for team_id, rows in by_team.items():
            rows.sort(key=lambda row: (row["game_date"], row["game_id"]))
            rows = rows[-10:]
            if len(rows) < 2:
                continue
            change = rows[-1]["margin"] - rows[0]["margin"]
            items.append(
                {
                    "team_id": team_id,
                    "first_game_id": rows[0]["game_id"],
                    "last_game_id": rows[-1]["game_id"],
                    "margin_change": change,
                    "absolute_change": abs(change),
                }
            )
        items.sort(key=lambda item: (-item["absolute_change"], item["team_id"]))
    else:
        raise ValueError("unsupported_topic")
    return _finalize(
        {"mode": "topic", "topic": topic, "items": items[:10],
         "scope": "last 10 available team games" if topic == "largest_team_change" else "supplied record set"},
        records,
    )
