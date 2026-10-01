"""Normalize existing CourtVision schedule and Agent artifacts for the UI."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from v2.pregame_system import ROOT
from v2.pregame_system.agent import TEAM_NAMES
from courtvision.data import TEAM_ID_TO_ABBR

from .display_data import compact_app_data


STATIC_ROOT = Path(__file__).resolve().parent / "static"
DEFAULT_OUTPUT = STATIC_ROOT / "app_data.json"
VERSIONED_SNAPSHOT = Path(__file__).resolve().parent / "data" / "current_games.json"
MATCHUP_SNAPSHOT = Path(__file__).resolve().parent / "data" / "matchup_history.json"
OUTPUTS = ROOT / "v2/pregame_system/outputs"

TEAM_COLORS = {
    1610612737: "#E03A3E", 1610612738: "#007A33", 1610612739: "#860038",
    1610612740: "#0C2340", 1610612741: "#CE1141", 1610612742: "#00538C",
    1610612743: "#0E2240", 1610612744: "#1D428A", 1610612745: "#CE1141",
    1610612746: "#C8102E", 1610612747: "#552583", 1610612748: "#98002E",
    1610612749: "#00471B", 1610612750: "#0C2340", 1610612751: "#A7A9AC",
    1610612752: "#F58426", 1610612753: "#0077C0", 1610612754: "#FDBB30",
    1610612755: "#006BB6", 1610612756: "#E56020", 1610612757: "#E03A3E",
    1610612758: "#5A2D81", 1610612759: "#C4CED4", 1610612760: "#007AC1",
    1610612761: "#CE1141", 1610612762: "#F9A01B", 1610612763: "#5D76A9",
    1610612764: "#002B5C", 1610612765: "#C8102E", 1610612766: "#1D1160",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def latest_file(folder: Path, name: str) -> Path | None:
    paths = sorted(folder.glob(f"*/{name}"))
    return paths[-1] if paths else None


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def output_path(path: Path, outputs: Path) -> str:
    try:
        relative = path.resolve().relative_to(outputs.resolve())
        return f"v2/pregame_system/outputs/{relative.as_posix()}"
    except ValueError:
        return path.name


def logo_url(team_id: int) -> str:
    return f"https://a.espncdn.com/i/teamlogos/nba/500/{TEAM_ID_TO_ABBR[team_id]}.png"


def team(team_id: int, english_name: str | None = None) -> dict[str, Any]:
    abbreviation = TEAM_ID_TO_ABBR[team_id].upper()
    return {
        "id": team_id,
        "name": english_name or TEAM_NAMES[team_id],
        "name_zh": TEAM_NAMES[team_id],
        "abbreviation": abbreviation,
        "logo_url": logo_url(team_id),
        "accent": TEAM_COLORS[team_id],
    }


def latest_reports(outputs: Path = OUTPUTS) -> dict[str, tuple[Path, dict[str, Any]]]:
    selected: dict[str, tuple[float, Path, dict[str, Any]]] = {}
    for path in (outputs / "agent_runs").glob("*/report.json"):
        try:
            report = read_json(path)
            game = report.get("game") or {}
            prediction = report.get("official_numeric_prediction") or report.get("prediction")
            if not str(game.get("game_id", "")).startswith("espn:") or not prediction:
                continue
            if (report.get("snapshot") or {}).get("demo_runtime"):
                continue
            market = report.get('market') or {}
            quote = market.get('as_of_cutoff') or {}
            sources = [market.get('source') or {}, quote.get('source') or {}]
            if (market.get('demo_fixture') or quote.get('demo_fixture')
                    or any(any(label in str(source.get('kind', '')).lower()
                               for label in ('fixture', 'synthetic')) for source in sources)):
                continue
            current = selected.get(game["game_id"])
            row = (path.stat().st_mtime, path, report)
            if current is None or row[0] > current[0]:
                selected[game["game_id"]] = row
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return {game_id: (row[1], row[2]) for game_id, row in selected.items()}


def value(report: dict[str, Any], key: str) -> float | None:
    sources = [
        report.get("residual_feature_values") or {},
        (report.get("team_form") or {}).get("values") or {},
        (report.get("matchup") or {}).get("values") or {},
    ]
    for source in sources:
        raw = source.get(key)
        if isinstance(raw, (int, float)):
            return float(raw)
    return None


def comparison_factor(
    report: dict[str, Any], game: dict[str, Any], key: str, label: str, kind: str,
    threshold: float,
) -> dict[str, Any] | None:
    home_value = value(report, f"home_{key}")
    away_value = value(report, f"away_{key}")
    if home_value is None or away_value is None:
        return None
    if not all(math.isfinite(item) for item in (home_value, away_value)):
        return None
    difference = abs(home_value - away_value)
    if difference + 1e-9 < threshold:
        return None
    leader = game["home_team_id"] if home_value > away_value else game["away_team_id"]
    name = TEAM_NAMES[leader]
    if kind == "percent":
        detail = f"{name}近 15 场胜率高 {difference * 100:.1f} 个百分点"
    elif kind == "rating":
        detail = f"{name}近 15 场净效率领先 {difference:.1f}"
    else:
        detail = f"{name}多休息 {difference:.0f} 天"
    return {
        "label": label,
        "status": "available",
        "home_value": home_value,
        "away_value": away_value,
        "leader_team_id": leader,
        "detail": detail,
        "display_threshold": threshold,
    }


def select_key_factors(report: dict[str, Any], game: dict[str, Any]) -> list[dict[str, Any]]:
    # Editorial display thresholds, not significance tests or model attribution.
    # Fixed semantic priority avoids comparing differences with unlike units.
    candidates = [
        ("net_rating_roll15", "近期攻防表现差异", "rating", 3.0),
        ("win_rate_roll15", "近期战绩差异", "percent", 2 / 15),
        ("rest_days", "休息条件差异", "days", 1.0),
    ]
    return [factor for key, label, kind, threshold in candidates
            if (factor := comparison_factor(report, game, key, label, kind, threshold)) is not None][:3]


def normalize_availability(report: dict[str, Any]) -> dict[str, Any]:
    availability = report.get("availability")
    if not availability:
        return {"status": "unavailable", "message": "可靠伤病信息暂不可用。"}
    records = availability.get("records") or []
    return {
        "status": "available" if records else "unavailable",
        "message": f"已连接 {len(records)} 条有时间边界的球员状态。" if records else "当前没有可展示的合格记录。",
        "records": [
            {
                "player": row.get("player_name"),
                "team_id": row.get("team_id"),
                "status": row.get("availability_status") or row.get("status"),
                "reason": row.get("reason"),
            }
            for row in records[:8]
        ],
    }


def normalize_market(report: dict[str, Any]) -> dict[str, Any] | None:
    raw = report.get("market") or {}
    quote = raw.get("as_of_cutoff") or raw.get("historical_reference")
    if not quote:
        return None
    expected_home_margin = quote.get("expected_home_margin")
    home_spread = quote.get("home_spread")
    if home_spread is None and expected_home_margin is not None:
        home_spread = -float(expected_home_margin)
    total = quote.get("total")
    if home_spread is None and total is None:
        return None
    source = quote.get("source") or raw.get("source") or {}
    return {
        "perspective": "home",
        "home_spread": home_spread,
        "expected_home_margin": expected_home_margin,
        "total": total,
        "provider": quote.get("provider") or source.get("provider"),
        "captured_at": quote.get("captured_at") or source.get("captured_at"),
        "source_url": source.get("url"),
    }


def normalize_game(
    schedule: dict[str, Any],
    path: Path,
    report: dict[str, Any],
    *,
    outputs: Path = OUTPUTS,
) -> dict[str, Any]:
    game = report.get("game") or schedule
    prediction = report.get("official_numeric_prediction") or report.get("prediction")
    market = report.get("market_benchmark")
    residual = report.get("residual_prediction")
    pure_source = next((key for key in ("pure_prediction", "basketball_only_prediction") if report.get(key)), None)
    pure = report.get(pure_source) if pure_source else None
    if pure is None and report.get("prediction_method") == "pure_basketball_fallback" and market is None and residual is None:
        pure, pure_source = prediction, "declared_pure_basketball_fallback"
    pure_provenance = {"market_independent": pure is not None, "source_field": pure_source}
    if pure is None:
        pure = {"status": "unavailable", "reason": "Saved report has no separately identified independent model output."}
    home_probability = float(prediction["win_probability"])
    predicted_home = home_probability >= 0.5
    predicted_team_id = int(game["home_team_id"] if predicted_home else game["away_team_id"])
    predicted_probability = home_probability if predicted_home else 1 - home_probability
    factors = select_key_factors(report, game)
    missing = report.get("missing_data") or []
    tipoff = game["scheduled_tipoff"]
    return {
        "id": game["game_id"],
        "status": game.get("status") or "scheduled",
        "tipoff": tipoff,
        "home": team(int(game["home_team_id"]), game.get("home_team")),
        "away": team(int(game["away_team_id"]), game.get("away_team")),
        "prediction": {
            "available": True,
            "winner_team_id": predicted_team_id,
            "winner_probability": predicted_probability,
            "expected_home_margin": prediction.get("expected_margin"),
            "expected_total": prediction.get("expected_total"),
            "expected_home_score": prediction.get("expected_home_score"),
            "expected_away_score": prediction.get("expected_away_score"),
        },
        "key_factors": factors,
        "relationship": {
            "basketball": pure,
            "basketball_provenance": pure_provenance,
            "market": market,
            "divergence": report.get("divergence"),
            "residual": residual,
            "final": prediction,
            "market_message": "当前没有符合时间和来源要求的市场数据。" if market is None else "市场信息已通过来源与时间校验。",
        },
        "background": {
            "market": normalize_market(report),
            "availability": normalize_availability(report),
            "recent_form": {
                "home": {
                    "win_rate_15": value(report, "home_win_rate_roll15"),
                    "off_rating_15": value(report, "home_off_rating_roll15"),
                    "def_rating_15": value(report, "home_def_rating_roll15"),
                    "net_rating_15": value(report, "home_net_rating_roll15"),
                    "pace_15": value(report, "home_pace_roll15"),
                    "three_pct_15": value(report, "home_three_pct_roll15"),
                    "rebound_share_15": value(report, "home_rebound_share_roll15"),
                    "turnover_rate_15": value(report, "home_turnover_rate_roll15"),
                },
                "away": {
                    "win_rate_15": value(report, "away_win_rate_roll15"),
                    "off_rating_15": value(report, "away_off_rating_roll15"),
                    "def_rating_15": value(report, "away_def_rating_roll15"),
                    "net_rating_15": value(report, "away_net_rating_roll15"),
                    "pace_15": value(report, "away_pace_roll15"),
                    "three_pct_15": value(report, "away_three_pct_roll15"),
                    "rebound_share_15": value(report, "away_rebound_share_roll15"),
                    "turnover_rate_15": value(report, "away_turnover_rate_roll15"),
                },
            },
            "schedule": {
                "home_rest_days": value(report, "home_rest_days"),
                "away_rest_days": value(report, "away_rest_days"),
                "home_back_to_back": value(report, "home_back_to_back"),
                "away_back_to_back": value(report, "away_back_to_back"),
            },
        },
        "missing": missing,
        "technical": {
            "prediction_method": report.get("prediction_method") or "saved_pure_prediction",
            "model_version": (report.get("snapshot") or {}).get("model_version"),
            "cutoff": (report.get("snapshot") or {}).get("cutoff"),
            "official_final": bool((report.get("snapshot") or {}).get("official_prediction")),
            "source_label": "开发期赛前预测快照",
        },
        "source": {
            "report_path": output_path(path, outputs),
            "report_sha256": sha256(path),
            "schedule_source": (schedule.get("source") or {}).get("url"),
            "schedule_captured_at": (schedule.get("source") or {}).get("captured_at"),
        },
    }


def build_app_data(
    outputs: str | Path = OUTPUTS,
    *,
    fallback: str | Path | None = VERSIONED_SNAPSHOT,
    roster_overlay: str | Path | None = None,
    matchup_snapshot: str | Path | None = MATCHUP_SNAPSHOT,
) -> dict[str, Any]:
    outputs = Path(outputs)
    schedule_path = latest_file(outputs / "future_schedule", "games.json")
    if schedule_path is None:
        if fallback and Path(fallback).is_file():
            return read_json(Path(fallback))
        raise ValueError("future schedule artifact unavailable")
    schedule = read_json(schedule_path)
    reports = latest_reports(outputs)
    schedule_by_id = {game["game_id"]: game for game in schedule}
    games = [
        normalize_game(schedule_by_id[game_id], path, report, outputs=outputs)
        for game_id, (path, report) in reports.items()
        if game_id in schedule_by_id
    ]
    games.sort(key=lambda row: (row["tipoff"], row["id"]))
    from v2.pregame_system.matchup_history import (
        build_matchup_history,
        build_season_form_context,
        season_phase_context,
    )
    from v2.pregame_system.roster_context import local_roster_context
    from v2.pregame_system.features import history
    from v2.pregame_system.storage import now
    context_cutoff = now()
    try:
        history_games, history_stats = history()
    except (OSError, ValueError):
        history_games, history_stats = None, None
    overlay = {}
    if roster_overlay and Path(roster_overlay).is_file():
        overlay = {
            item["id"]: item.get("roster_context")
            for item in read_json(Path(roster_overlay)).get("games", [])
        }
    matchup_overlay = {}
    season_form_overlay = {}
    if matchup_snapshot and Path(matchup_snapshot).is_file():
        for item in read_json(Path(matchup_snapshot)).get("games", []):
            matchup_overlay[item["id"]] = item.get("matchup_history")
            season_form_overlay[item["id"]] = item.get("season_form")
    for row in games:
        # New display/analysis context; never backfill the saved prediction cutoff.
        identity = schedule_by_id[row['id']]
        row['roster_context'] = overlay.get(row['id']) or local_roster_context(
            identity, context_cutoff, history_games, stats=history_stats
        )
        row['technical']['roster_context_cutoff'] = context_cutoff
        row['roster_notice'] = ('近期数据仍包含上赛季比赛，当前阵容代表性需结合名单确认。'
                                if any(t['cold_start'].get('cold_start') for t in row['roster_context']['teams'].values()) else None)
        row['matchup_history'] = (
            build_matchup_history(identity, context_cutoff, history_games, history_stats)
            if history_games is not None
            else matchup_overlay.get(row['id'])
            or {
                'status': 'unavailable',
                'reason': 'historical_games_unavailable',
                'numeric_model_usage': False,
            }
        )
        row['season_form'] = (
            build_season_form_context(identity, context_cutoff, history_stats)
            if history_stats is not None
            else season_form_overlay.get(row['id'])
            or {
                'status': 'unavailable',
                'reason': 'historical_team_stats_unavailable',
                'numeric_model_usage': False,
            }
        )
        row['season_phase'] = season_phase_context(
            (row['roster_context'].get('teams') or {}).get('home'),
            (row['roster_context'].get('teams') or {}).get('away'),
            row['season_form'],
        )
    used_team_ids = {side["id"] for game in games for side in (game["home"], game["away"])}
    catalog = [team(team_id) for team_id in sorted(TEAM_NAMES)]
    return compact_app_data({
        "schema_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "product": {
            "name": "第25秒",
            "stage": "frontend integration v1",
            "notice": "当前展示开发期赛前预测快照。新赛季真实 T-5 官方预测尚未发生。",
        },
        "coverage": {
            "schedule_games": len(schedule),
            "games_with_saved_prediction": len(games),
            "games_without_saved_prediction": len(schedule) - len(games),
            "displayed_team_count": len(used_team_ids),
        },
        "games": games,
        "teams": catalog,
        "source": {
            "schedule_path": output_path(schedule_path, outputs),
            "schedule_sha256": sha256(schedule_path),
            "report_selection": "latest non-demo, non-fixture saved report per ESPN game ID",
            "synthetic_market_included": False,
        },
    })


def export_app_data(
    path: str | Path = DEFAULT_OUTPUT,
    *,
    outputs: str | Path = OUTPUTS,
    snapshot_path: str | Path | None = None,
    roster_overlay: str | Path | None = None,
    matchup_snapshot: str | Path | None = MATCHUP_SNAPSHOT,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    value = build_app_data(
        outputs,
        fallback=VERSIONED_SNAPSHOT,
        roster_overlay=roster_overlay,
        matchup_snapshot=matchup_snapshot,
    )
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".frontend_data_", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if snapshot_path:
        snapshot = Path(snapshot_path)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.resolve() != target.resolve():
            snapshot.write_bytes(payload)
    return target


if __name__ == "__main__":
    print(export_app_data(snapshot_path=VERSIONED_SNAPSHOT))
