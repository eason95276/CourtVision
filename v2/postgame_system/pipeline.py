"""Unified manual postgame refresh and deterministic record pipeline."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import PACKAGE_ROOT, ROOT
from .identity import IdentityRegistry
from .learning import build_learning_record
from .record import build_postgame_record
from .refresh import CachedRawClient, NBAApiClient, RefreshResult, refresh_postgame


DEFAULT_OUTPUT_ROOT = PACKAGE_ROOT / "outputs"
DEFAULT_IDENTITY_PATH = DEFAULT_OUTPUT_ROOT / "identity_registry.json"


@dataclass(frozen=True)
class PipelineResult:
    refresh: RefreshResult
    postgame_record: dict[str, Any]
    learning_record: dict[str, Any]


def _existing(path: Path) -> Path | None:
    return path if path.is_file() else None


def run_postgame_pipeline(
    *,
    nba_game_id: str,
    game_date: str,
    home_team_id: int,
    away_team_id: int,
    scheduled_tipoff: str | None = None,
    espn_game_id: str | None = None,
    schedule_source: dict[str, Any] | None = None,
    identity_path: str | Path = DEFAULT_IDENTITY_PATH,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    client: Any | None = None,
    official_prediction: str | Path | dict[str, Any] | None = None,
) -> PipelineResult:
    output_root = Path(output_root)
    registry = IdentityRegistry(Path(identity_path))
    if espn_game_id:
        if not scheduled_tipoff:
            raise ValueError("espn_identity_requires_tipoff")
        registry.register_schedule(
            {
                "game_id": espn_game_id,
                "event_id": espn_game_id.removeprefix("espn:"),
                "scheduled_tipoff": scheduled_tipoff,
                "home_team_id": int(home_team_id),
                "away_team_id": int(away_team_id),
                "source": schedule_source or {"kind": "manual_schedule_identity"},
            }
        )
    identity = registry.attach_nba_game(
        {
            "game_id": nba_game_id,
            "game_date": game_date,
            "scheduled_tipoff": scheduled_tipoff,
            "home_team_id": int(home_team_id),
            "away_team_id": int(away_team_id),
            "source": {"kind": "postgame_refresh"},
        }
    )
    refresh = refresh_postgame(
        identity.nba_game_id,
        registry=registry,
        client=client or NBAApiClient(),
        output_root=output_root / "refresh",
    )
    if official_prediction is None and identity.espn_game_id:
        candidate = (
            ROOT
            / "v2/pregame_system/outputs/official_final"
            / (identity.espn_game_id.replace(":", "_") + ".json")
        )
        official_prediction = _existing(candidate)
    facts = _existing(ROOT / f"v2/outputs/game_{nba_game_id}_facts.json")
    audit = _existing(ROOT / f"v2/outputs/game_{nba_game_id}_prediction_audit.json")
    record = build_postgame_record(
        refresh.bundle_path,
        official_prediction=official_prediction,
        historical_audit_path=audit if official_prediction is None else None,
        detailed_facts_path=facts,
        output_root=output_root / "records",
    )
    learning = build_learning_record(record, output_root=output_root / "learning")
    return PipelineResult(refresh=refresh, postgame_record=record, learning_record=learning)


def load_records(root: str | Path) -> list[dict[str, Any]]:
    records = []
    for path in Path(root).glob("*/*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("record_hash"):
            records.append(value)
    selected = {}
    priority = {"prediction_unavailable": 0, "historical_replay": 1, "official_pregame_final": 2}
    for record in records:
        game_id = record["game"]["game_id"]
        rank = (
            priority.get(record.get("prediction_review", {}).get("source_type"), -1),
            record.get("generated_at") or "",
            record["record_hash"],
        )
        current = selected.get(game_id)
        if current is None or rank > current[0]:
            selected[game_id] = (rank, record)
    return sorted(
        [item[1] for item in selected.values()],
        key=lambda value: (value["game"]["game_date"], value["game"]["game_id"]),
    )
