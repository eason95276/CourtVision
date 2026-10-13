"""Materialize auditable player-season/team-stint summaries from a complete provider dataset."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from v2.pregame_system.player_knowledge import (
    DEFAULT_DATABASE,
    DATA_VERSION,
    PlayerKnowledgeStore,
    _aggregate_player_rows,
    _factual_role_evidence,
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _compact_profile(profile: dict[str, Any]) -> dict[str, Any]:
    result = dict(profile)
    result.pop("sample_game_ids", None)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--cutoff", required=True,
                        help="UTC cutoff used to select only eligible captures and games")
    parser.add_argument("--provider", help="Defaults to the database's active provider")
    parser.add_argument("--output", type=Path,
                        default=PROJECT_ROOT / "v2" / "data" / "player_knowledge_summaries" / "2025-26_espn.json")
    parser.add_argument("--allow-partial", action="store_true",
                        help="Generate a clearly marked partial summary instead of requiring a full season")
    return parser.parse_args(argv)


def build(args: argparse.Namespace) -> dict[str, Any]:
    store = PlayerKnowledgeStore(args.database)
    provider = args.provider or store.active_provider()
    coverage = store.coverage(args.season, args.cutoff, provider=provider)
    if coverage.get("status") != "complete" and not args.allow_partial:
        raise ValueError("summary_requires_complete_provider_season")
    facts, selected_receipts = store._facts_as_of(args.season, args.cutoff, provider=provider)
    source_data_versions = (coverage.get("source") or {}).get("data_versions") or [DATA_VERSION]

    by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_identity_rows = []
    participation_counts: dict[str, int] = defaultdict(int)
    missing_status_counts: dict[str, int] = defaultdict(int)
    starter_counts: dict[str, int] = defaultdict(int)
    minutes_precision_counts: dict[str, int] = defaultdict(int)
    field_missing_counts: dict[str, int] = defaultdict(int)
    required_stat_fields = (
        "points", "fgm", "fga", "three_pm", "three_pa", "ftm", "fta",
        "rebounds", "offensive_rebounds", "defensive_rebounds", "assists",
        "turnovers", "steals", "blocks", "fouls",
    )
    for fact in facts:
        participation_counts[str(fact.get("participation_status"))] += 1
        missing_status_counts[str(fact.get("missing_status"))] += 1
        starter_counts["unknown" if fact.get("starter") is None else ("true" if fact["starter"] else "false")] += 1
        field_status = json.loads(fact.get("field_status_json") or "{}")
        minute_status = field_status.get("minutes_seconds", "unknown")
        minutes_precision_counts[minute_status] += 1
        for name in required_stat_fields:
            if fact.get(name) is None and fact.get("participation_status") == "played":
                field_missing_counts[name] += 1
        if fact.get("player_id") is None:
            missing_identity_rows.append({
                "game_id": fact["game_id"],
                "team_id": int(fact["team_id"]),
                "player_name": fact.get("player_name"),
                "participation_status": fact.get("participation_status"),
                "missing_status": fact.get("missing_status"),
                "missing_fields": json.loads(fact.get("missing_fields_json") or "[]"),
                "normalization_warnings": json.loads(fact.get("normalization_warnings_json") or "[]"),
            })
        else:
            by_player[str(fact["player_id"])].append(fact)

    player_profiles = []
    for player_id, rows in sorted(by_player.items()):
        profile = _aggregate_player_rows(player_id, rows)
        profile["team_role_evidence"] = []
        player_profiles.append(_compact_profile(profile))

    team_profiles: dict[str, Any] = {}
    stint_roles_by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw_team_id, team_coverage in sorted(coverage.get("team_coverage", {}).items(), key=lambda pair: int(pair[0])):
        team_id = int(raw_team_id)
        summary = store.team_player_summaries(team_id, args.season, args.cutoff)
        profiles = []
        for profile in summary.get("profiles", []):
            role_evidence = _factual_role_evidence(profile)
            item = _compact_profile(profile)
            item["factual_role_evidence"] = role_evidence
            profiles.append(item)
            stint_roles_by_player[str(profile["player_id"])].append({
                "team_id": team_id,
                "team_tricode": profile.get("team_tricode"),
                "team_minutes_share": profile.get("team_minutes_share"),
                "team_minutes_denominator_seconds": profile.get("team_minutes_denominator_seconds"),
                "team_ranks": profile.get("team_ranks"),
                "minutes_precision": profile.get("minutes_precision"),
                "factual_role_evidence": role_evidence,
            })
        team_profiles[str(team_id)] = {
            "coverage": team_coverage,
            "profile_count": len(profiles),
            "team_minutes_denominator_seconds": summary.get("team_minutes_denominator_seconds"),
            "team_minutes_source_precision": sorted({
                p.get("minutes_precision", {}).get("status") for p in profiles if p.get("minutes_precision")
            }),
            "profiles": profiles,
        }

    for profile in player_profiles:
        profile["team_role_evidence"] = stint_roles_by_player.get(str(profile["player_id"]), [])

    report = {
        "schema_version": "player-knowledge-summary-v1",
        "season": args.season,
        "season_type": "Regular Season",
        "provider": provider,
        "data_version": source_data_versions[0] if len(source_data_versions) == 1 else source_data_versions,
        "source_data_versions": source_data_versions,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cutoff": coverage.get("cutoff"),
        "coverage": coverage,
        "player_profile_count": len(player_profiles),
        "team_count": len(team_profiles),
        "player_season_summaries": player_profiles,
        "team_summaries": team_profiles,
        "row_status_counts": dict(sorted(participation_counts.items())),
        "missing_status_counts": dict(sorted(missing_status_counts.items())),
        "starter_status_counts": dict(sorted(starter_counts.items())),
        "minutes_field_status_counts": dict(sorted(minutes_precision_counts.items())),
        "played_row_missing_basic_field_counts": dict(sorted(field_missing_counts.items())),
        "rows_without_stable_player_id": missing_identity_rows,
        "minutes_resolution_note": (
            "ESPN summary boxscore reports minutes at whole-minute resolution; minutes_seconds values for ESPN facts are minute-level conversions, not second-precision measurements."
            if provider == "ESPN" else None
        ),
        "identity_note": (
            "ESPN athlete IDs are kept in the ESPN namespace. NBA roster crosswalk is unique-name-only after full-league coverage; ambiguous names remain unresolved."
            if provider == "ESPN" else "NBA Stats personId namespace."
        ),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build(args)
    _write_json(args.output, report)
    print(json.dumps({
        "output": str(args.output),
        "provider": report["provider"],
        "coverage_status": report["coverage"]["status"],
        "games": report["coverage"]["collected_games"],
        "players": report["player_profile_count"],
        "teams": report["team_count"],
        "rows_without_stable_player_id": len(report["rows_without_stable_player_id"]),
        "summary_bytes": args.output.stat().st_size,
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
