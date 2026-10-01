"""Deterministic, prediction-aware postgame record."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from v2.pregame_system.storage import clean, content_hash, now, save


def _read(value: str | Path | dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return json.loads(json.dumps(clean(value), ensure_ascii=False))
    path = Path(value)
    return json.loads(path.read_text(encoding="utf-8"))


def _prediction_values(value: dict[str, Any] | None) -> dict[str, float] | None:
    if not value:
        return None
    aliases = {
        "win_probability": ("win_probability", "home_win_probability"),
        "expected_margin": ("expected_margin", "predicted_home_margin"),
        "expected_total": ("expected_total", "predicted_total"),
    }
    result = {}
    for target, names in aliases.items():
        selected = next((value.get(name) for name in names if value.get(name) is not None), None)
        if isinstance(selected, bool) or not isinstance(selected, (int, float)) or not math.isfinite(selected):
            return None
        result[target] = float(selected)
    result["away_win_probability"] = 1.0 - result["win_probability"]
    result["expected_home_score"] = (
        result["expected_total"] + result["expected_margin"]
    ) / 2.0
    result["expected_away_score"] = (
        result["expected_total"] - result["expected_margin"]
    ) / 2.0
    return result


def _errors(prediction: dict[str, float] | None, game: dict[str, Any]) -> dict | None:
    if prediction is None:
        return None
    actual_home_win = int(game["actual_home_margin"] > 0)
    predicted_home_win = int(prediction["win_probability"] >= 0.5)
    margin_error = prediction["expected_margin"] - game["actual_home_margin"]
    total_error = prediction["expected_total"] - game["actual_total"]
    return {
        "predicted_home_win": bool(predicted_home_win),
        "actual_home_win": bool(actual_home_win),
        "win_correct": predicted_home_win == actual_home_win,
        "probability_error": prediction["win_probability"] - actual_home_win,
        "signed_margin_error": margin_error,
        "absolute_margin_error": abs(margin_error),
        "signed_total_error": total_error,
        "absolute_total_error": abs(total_error),
    }


def _market_actual(market: dict[str, Any] | None, game: dict[str, Any]) -> dict:
    if not market:
        return {"status": "unavailable", "reason": "market_missing"}
    margin = market.get("expected_home_margin")
    total = market.get("total", market.get("market_total"))
    result = {"status": "available", "quote": clean(market)}
    if isinstance(margin, (int, float)):
        result["margin"] = {
            "market_expected_home_margin": float(margin),
            "actual_home_margin": game["actual_home_margin"],
            "signed_error": float(margin) - game["actual_home_margin"],
            "absolute_error": abs(float(margin) - game["actual_home_margin"]),
        }
    if isinstance(total, (int, float)):
        result["total"] = {
            "market_total": float(total),
            "actual_total": game["actual_total"],
            "signed_error": float(total) - game["actual_total"],
            "absolute_error": abs(float(total) - game["actual_total"]),
        }
    return result


def _prediction_review(
    bundle: dict[str, Any],
    official: dict[str, Any] | None,
    audit: dict[str, Any] | None,
) -> dict[str, Any]:
    identity = bundle["identity"]
    game = bundle["game"]
    valid_ids = {
        identity.get("canonical_game_id"),
        identity.get("espn_game_id"),
        identity.get("nba_game_id"),
    }
    def feature_snapshot(report: dict[str, Any]) -> tuple[dict | None, str | None]:
        snapshot = report.get("snapshot") or {}
        candidates = []
        for evidence in report.get("evidence") or []:
            if evidence.get("section") == "prediction":
                folder = (evidence.get("value") or {}).get("path")
                if folder:
                    candidates.append(Path(folder) / "inputs.json")
        for source in report.get("sources") or []:
            path = source.get("path")
            if path and str(path).endswith("inputs.json"):
                candidates.append(Path(path))
        for path in candidates:
            if not path.is_file():
                continue
            inputs = json.loads(path.read_text(encoding="utf-8"))
            expected = snapshot.get("input_hash")
            if expected and content_hash(inputs) != expected:
                raise ValueError("pregame_feature_snapshot_hash_mismatch")
            return clean(inputs.get("features")), str(path)
        return None, None
    if official:
        if official.get("game_id") not in valid_ids:
            raise ValueError("prediction_identity_mismatch")
        report = official.get("report") or {}
        public = _prediction_values(
            official.get("public_prediction")
            or report.get("prediction")
        )
        basketball = _prediction_values(
            official.get("basketball_only_prediction")
            or report.get("basketball_only_prediction")
            or public
        )
        informed = _prediction_values(
            official.get("market_informed_prediction")
            or report.get("market_informed_prediction")
        )
        market_wrapper = report.get("market") or {}
        market = market_wrapper.get("as_of_cutoff")
        source_type = "official_pregame_final"
        fallback = bool(official.get("fallback_used"))
        source_hash = official.get("document_hash") or content_hash(official)
        market_usage = official.get("market_usage") or report.get("market_usage")
        prediction_snapshot = clean(report.get("snapshot"))
        pregame_features, feature_input_path = feature_snapshot(report)
    elif audit:
        if audit.get("game", {}).get("game_id") != identity.get("nba_game_id"):
            raise ValueError("prediction_identity_mismatch")
        basketball = _prediction_values(audit.get("historical_prediction"))
        informed = None
        public = basketball
        market = audit.get("historical_market_benchmark")
        source_type = "historical_replay"
        fallback = False
        source_hash = content_hash(audit)
        market_usage = {
            "status": "historical_reference_only",
            "exact_quote_time_verified": False,
        }
        prediction_snapshot = {
            "historical_replay": True,
            "model_version": audit.get("historical_prediction", {}).get("model_scope"),
            "input_hash": None,
        }
        pregame_features = None
        feature_input_path = None
    else:
        basketball = informed = public = market = None
        source_type = "prediction_unavailable"
        fallback = False
        source_hash = None
        market_usage = {"status": "unavailable", "reason": "prediction_missing"}
        prediction_snapshot = None
        pregame_features = None
        feature_input_path = None
    return {
        "source_type": source_type,
        "fallback_used": fallback,
        "prediction_reference_hash": source_hash,
        "prediction_snapshot": prediction_snapshot,
        "pregame_features": pregame_features,
        "feature_input_path": feature_input_path,
        "basketball_only": {
            "prediction": basketball,
            "errors": _errors(basketball, game),
        }
        if basketball
        else None,
        "market_informed": {
            "prediction": informed,
            "errors": _errors(informed, game),
        }
        if informed
        else None,
        "public_prediction": {
            "prediction": public,
            "errors": _errors(public, game),
        }
        if public
        else None,
        "market_usage": clean(market_usage),
        "market_actual_comparison": _market_actual(market, game),
        "historical_direction_evaluation": clean(audit.get("direction_evaluation")) if audit else None,
    }


def _metric(value: Any, key: str) -> float:
    raw = value.get(key) if isinstance(value, dict) else None
    if isinstance(raw, dict):
        raw = raw.get("raw")
    return float(raw) if isinstance(raw, (int, float)) and math.isfinite(raw) else 0.0


def _four_factors(bundle: dict[str, Any], facts: dict[str, Any] | None) -> dict[str, dict]:
    if facts and isinstance(facts.get("four_factors"), dict):
        return facts["four_factors"]
    teams = bundle["team_boxscores"]
    by_side = {row["side"]: row for row in teams}
    result = {}
    for side, own, opponent in [
        ("away", by_side["away"], by_side["home"]),
        ("home", by_side["home"], by_side["away"]),
    ]:
        fga = float(own.get("fieldGoalsAttempted") or 0)
        fgm = float(own.get("fieldGoalsMade") or 0)
        threes = float(own.get("threePointersMade") or 0)
        fta = float(own.get("freeThrowsAttempted") or 0)
        tov = float(own.get("turnovers") or 0)
        orb = float(own.get("reboundsOffensive") or 0)
        opp_drb = float(opponent.get("reboundsDefensive") or 0)
        possessions = fga + 0.44 * fta + tov
        tricode = own.get("team_tricode") or side
        result[tricode] = {
            "side": side,
            "team_id": own["team_id"],
            "effective_field_goal_percentage": {"raw": (fgm + 0.5 * threes) / fga if fga else None},
            "turnover_rate": {"raw": tov / possessions if possessions else None},
            "offensive_rebound_rate": {"raw": orb / (orb + opp_drb) if orb + opp_drb else None},
            "free_throw_rate": {"raw": fta / fga if fga else None},
        }
    return result


def _shot_zone_data(bundle: dict[str, Any], facts: dict[str, Any] | None) -> dict[str, Any]:
    if facts and facts.get("shot_zone_facts"):
        return clean(facts["shot_zone_facts"])
    team_names = {
        int(row["team_id"]): row.get("team_tricode")
        for row in bundle.get("team_boxscores") or []
    }
    grouped: dict[str, dict[str, dict[str, float]]] = {}
    for row in bundle.get("shots") or []:
        try:
            team = team_names[int(row["TEAM_ID"])]
            zone = str(row["SHOT_ZONE_BASIC"])
            made = int(row["SHOT_MADE_FLAG"])
        except (KeyError, TypeError, ValueError):
            continue
        current = grouped.setdefault(team, {}).setdefault(
            zone, {"attempts": 0, "made": 0}
        )
        current["attempts"] += 1
        current["made"] += made
    for zones in grouped.values():
        for values in zones.values():
            values["field_goal_percentage"] = (
                values["made"] / values["attempts"] if values["attempts"] else None
            )
    teams = list(grouped)
    largest = None
    if len(teams) == 2:
        common = set(grouped[teams[0]]) & set(grouped[teams[1]])
        candidates = []
        for zone in common:
            left = grouped[teams[0]][zone]
            right = grouped[teams[1]][zone]
            if left["attempts"] < 3 or right["attempts"] < 3:
                continue
            candidates.append(
                {
                    "zone": zone,
                    "teams": {
                        teams[0]: left,
                        teams[1]: right,
                    },
                    "absolute_gap": abs(
                        left["field_goal_percentage"] - right["field_goal_percentage"]
                    ),
                }
            )
        if candidates:
            largest = max(candidates, key=lambda value: (value["absolute_gap"], value["zone"]))
    return {
        "status": "available" if grouped else "unavailable",
        "zones": grouped,
        "largest_field_goal_percentage_difference": largest,
        "minimum_attempts_per_team_for_comparison": 3,
    }


def _key_factors(
    factors: dict[str, dict], game: dict[str, Any], shot_zones: dict[str, Any]
) -> list[dict[str, Any]]:
    away = game["away_team_tricode"]
    home = game["home_team_tricode"]
    labels = {
        "effective_field_goal_percentage": "有效投篮命中率",
        "turnover_rate": "失误率",
        "offensive_rebound_rate": "进攻篮板率",
        "free_throw_rate": "罚球率",
    }
    rows = []
    for metric_name, label in labels.items():
        away_value = _metric(factors.get(away, {}), metric_name)
        home_value = _metric(factors.get(home, {}), metric_name)
        gap = home_value - away_value
        favorable_home = gap < 0 if metric_name == "turnover_rate" else gap > 0
        favored = home if favorable_home else away
        rows.append(
            {
                "factor": metric_name,
                "label": label,
                "home_value": home_value,
                "away_value": away_value,
                "absolute_gap": abs(gap),
                "favored_team": favored,
                "relationship": f"{label}差异与本场结果同时出现，可帮助解释结果，不能单独证明战术因果。",
                "evidence_id": content_hash(
                    {
                        "game_id": game["game_id"],
                        "metric": metric_name,
                        "home": home_value,
                        "away": away_value,
                    }
                ),
            }
        )
    shot = shot_zones.get("largest_field_goal_percentage_difference")
    if shot and isinstance(shot.get("teams"), dict):
        team_values = shot["teams"]
        home_values = team_values.get(home)
        away_values = team_values.get(away)
        if home_values and away_values:
            home_value = float(home_values["field_goal_percentage"])
            away_value = float(away_values["field_goal_percentage"])
            rows.append(
                {
                    "factor": "shot_zone_field_goal_percentage",
                    "label": f"{shot['zone']} 区域命中率",
                    "home_value": home_value,
                    "away_value": away_value,
                    "absolute_gap": abs(home_value - away_value),
                    "favored_team": home if home_value > away_value else away,
                    "relationship": "该投篮区域命中率差异与本场结果同时出现，出手量有限时波动可能较大。",
                    "evidence_id": content_hash(
                        {
                            "game_id": game["game_id"],
                            "zone": shot["zone"],
                            "home": home_values,
                            "away": away_values,
                        }
                    ),
                }
            )
    return sorted(rows, key=lambda row: row["absolute_gap"], reverse=True)[:5]


def _flow(bundle: dict[str, Any], facts: dict[str, Any] | None) -> dict[str, Any]:
    if facts:
        return {
            "score_by_period": clean(facts.get("score_by_period") or []),
            "game_flow": clean(facts.get("game_flow") or {}),
            "key_runs": clean(facts.get("key_runs") or []),
        }
    rows = bundle.get("play_by_play") or []
    period_end = {}
    maximum = {"home": 0, "away": 0}
    for row in rows:
        period = row.get("period")
        away = row.get("scoreAway")
        home = row.get("scoreHome")
        try:
            away_score = int(away)
            home_score = int(home)
        except (TypeError, ValueError):
            continue
        period_end[int(period)] = {"period": int(period), "away": away_score, "home": home_score}
        maximum["home"] = max(maximum["home"], home_score - away_score)
        maximum["away"] = max(maximum["away"], away_score - home_score)
    return {
        "score_by_period_end": [period_end[key] for key in sorted(period_end)],
        "maximum_leads": maximum,
        "status": "available" if period_end else "unavailable",
    }


def _players(bundle: dict[str, Any], facts: dict[str, Any] | None) -> list[dict[str, Any]]:
    if facts:
        candidates = []
        for team, rows in (facts.get("player_facts", {}).get("teams") or {}).items():
            for row in rows:
                candidates.append(
                    {
                        "team": team,
                        "person_id": row.get("person_id"),
                        "name": row.get("name"),
                        "points": row.get("points"),
                        "rebounds": row.get("rebounds"),
                        "assists": row.get("assists"),
                    }
                )
    else:
        candidates = [
            {
                "team": row.get("team_tricode"),
                "person_id": row.get("person_id"),
                "name": row.get("full_name"),
                "points": row.get("points") or 0,
                "rebounds": row.get("reboundsTotal") or 0,
                "assists": row.get("assists") or 0,
            }
            for row in bundle.get("player_boxscores") or []
            if row.get("minutes") is not None
        ]
    return sorted(candidates, key=lambda row: (row.get("points") or 0), reverse=True)[:4]


def _judgments(review: dict[str, Any], game: dict[str, Any]) -> list[dict[str, str]]:
    public = review.get("public_prediction")
    if not public:
        return [{"judgment": "pregame_prediction", "status": "not_verifiable", "reason": "赛前预测不可用"}]
    prediction = public["prediction"]
    errors = public["errors"]
    rows = [
        {
            "judgment": "winner",
            "status": "supported" if errors["win_correct"] else "not_supported",
            "reason": "赛前胜负方向与实际赛果一致" if errors["win_correct"] else "赛前胜负方向与实际赛果不一致",
        },
        {
            "judgment": "home_margin_direction",
            "status": "supported"
            if (prediction["expected_margin"] > 0) == (game["actual_home_margin"] > 0)
            else "not_supported",
            "reason": "比较预测主队分差与实际主队分差的正负方向",
        },
    ]
    market = review["market_actual_comparison"]
    rows.append(
        {
            "judgment": "model_market_total_direction",
            "status": "not_verifiable" if "total" not in market else (
                "supported"
                if (prediction["expected_total"] > market["total"]["market_total"])
                == (game["actual_total"] > market["total"]["market_total"])
                else "not_supported"
            ),
            "reason": "缺少可用市场总分" if "total" not in market else "比较模型与市场分歧方向及实际结果",
        }
    )
    return rows


def render_postgame_markdown(record: dict[str, Any]) -> str:
    game = record["game"]
    review = record["prediction_review"]
    lines = [
        f"# {game['away_team_tricode']} {game['away_points']} 比 {game['home_points']} {game['home_team_tricode']} 赛后复盘",
        "",
        "## 比赛结果",
        "",
        f"{game['winner_team_tricode']} 获胜，主队视角分差为 {game['actual_home_margin']:+g}，总分 {game['actual_total']}。",
        "",
        "## 关键数据因素",
        "",
    ]
    for factor in record["key_factors"]:
        lines.append(
            f"1. {factor['label']}：主队 {factor['home_value']:.3f}，客队 {factor['away_value']:.3f}。{factor['relationship']}"
        )
    lines.extend(["", "## CourtVision 赛前预测复核", ""])
    public = review.get("public_prediction")
    if public:
        prediction = public["prediction"]
        errors = public["errors"]
        lines.append(
            f"主队胜率 {prediction['win_probability']:.1%}，预测主队分差 {prediction['expected_margin']:+.2f}，预测总分 {prediction['expected_total']:.2f}。"
        )
        lines.append(
            f"胜负方向{'正确' if errors['win_correct'] else '错误'}，分差绝对误差 {errors['absolute_margin_error']:.2f}，总分绝对误差 {errors['absolute_total_error']:.2f}。"
        )
    else:
        lines.append("没有可关联的赛前预测，本节只保留赛后事实。")
    lines.extend(["", "## 数据完整度", ""])
    if record["missing_data"]:
        lines.append("缺失或不可用：" + "、".join(row["component"] for row in record["missing_data"]) + "。")
    else:
        lines.append("基础与增强数据均可用。")
    lines.append("所有关键因素均为结构化数据关系，未使用录像、追踪或对位证据推断战术因果。")
    return "\n".join(lines) + "\n"


def build_postgame_record(
    bundle_value: str | Path | dict[str, Any],
    *,
    official_prediction: str | Path | dict[str, Any] | None = None,
    historical_audit_path: str | Path | None = None,
    detailed_facts_path: str | Path | None = None,
    output_root: str | Path | None = None,
) -> dict[str, Any]:
    bundle = _read(bundle_value)
    official = _read(official_prediction)
    audit = _read(historical_audit_path)
    facts = _read(detailed_facts_path)
    if facts and facts.get("game", {}).get("game_id") != bundle["game"]["game_id"]:
        raise ValueError("facts_identity_mismatch")
    factors = _four_factors(bundle, facts)
    shot_zones = _shot_zone_data(bundle, facts)
    review = _prediction_review(bundle, official, audit)
    game = clean(
        {
            **bundle["game"],
            "final_score": {
                bundle["game"]["away_team_tricode"]: bundle["game"]["away_points"],
                bundle["game"]["home_team_tricode"]: bundle["game"]["home_points"],
            },
            "winner_team_tricode": bundle["game"]["home_team_tricode"]
            if bundle["game"]["home_points"] > bundle["game"]["away_points"]
            else bundle["game"]["away_team_tricode"],
        }
    )
    missing = [
        {"component": name, "status": status, **(bundle.get("errors") or {}).get(name, {})}
        for name, status in bundle["components"].items()
        if status != "available"
    ]
    if review["source_type"] == "prediction_unavailable":
        missing.append({"component": "official_prediction", "status": "missing"})
    if review["market_actual_comparison"].get("status") != "available":
        missing.append({"component": "market", "status": "missing"})
    record_core = {
        "schema_version": "1.0",
        "status": "enhanced" if not missing else "basic",
        "identity": clean(bundle["identity"]),
        "game": game,
        "game_flow": _flow(bundle, facts),
        "team_boxscores": clean(bundle["team_boxscores"]),
        "key_team_data": clean(factors),
        "shot_zone_data": shot_zones,
        "key_factors": _key_factors(factors, game, shot_zones),
        "key_players": _players(bundle, facts),
        "prediction_review": review,
        "pregame_judgments": _judgments(review, game),
        "data_completeness": clean(bundle["components"]),
        "missing_data": clean(missing),
        "evidence": {
            "identity_hash": bundle["identity"]["identity_hash"],
            "refresh_bundle_hash": bundle.get("bundle_hash") or content_hash(bundle),
            "detailed_facts_hash": content_hash(facts) if facts else None,
            "prediction_reference_hash": review["prediction_reference_hash"],
            "receipts": clean(bundle.get("receipts") or {}),
        },
        "limitations": [
            "结构化比赛数据可以支持比分、效率、事件顺序和预测误差，不能单独证明战术因果。",
            "缺少录像、追踪、防守距离或可靠阵容回合时，不输出对应归因。",
            "Historical Replay 与真实官方赛前预测保持独立来源标签。",
        ],
    }
    record = {**record_core, "record_hash": content_hash(record_core), "generated_at": now()}
    if output_root is not None:
        folder = Path(output_root) / game["game_id"]
        folder.mkdir(parents=True, exist_ok=True)
        json_path = folder / f"{record['record_hash']}.json"
        markdown_path = folder / f"{record['record_hash']}.md"
        if not json_path.exists():
            save(json_path, record)
        if not markdown_path.exists():
            markdown_path.write_text(render_postgame_markdown(record), encoding="utf-8")
        record["output"] = {"json": str(json_path), "markdown": str(markdown_path)}
    return record
