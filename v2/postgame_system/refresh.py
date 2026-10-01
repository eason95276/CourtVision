"""One-game postgame refresh with independent basic and enhancement status."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from courtvision.data import season_start_from_game_id

from v2.pregame_system.storage import clean, content_hash, digest, now, save

from . import ROOT
from .identity import GameIdentity, IdentityRegistry


@dataclass(frozen=True)
class RefreshResult:
    status: str
    game_id: str
    canonical_game_id: str
    components: dict[str, str]
    bundle_hash: str
    bundle_path: Path
    missing_data: list[dict[str, str]]


class CachedRawClient:
    """Read any locally cached V2 nba_api game using the existing file naming."""

    def __init__(self, raw_root: Path | None = None):
        self.raw_root = Path(raw_root or ROOT / "v2/data/raw/nba_stats")

    def _read(self, game_id: str, suffix: str) -> tuple[dict, dict]:
        path = self.raw_root / f"{game_id}_{suffix}.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        captured = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        return payload, {
            "status": "cached",
            "captured_at": captured,
            "raw_path": str(path),
            "sha256": digest(path),
            "retry_count": 0,
        }

    def fetch_boxscore(self, game_id: str):
        return self._read(game_id, "boxscore_traditional_v3")

    def fetch_play_by_play(self, game_id: str):
        return self._read(game_id, "play_by_play_v3")

    def fetch_shots(self, game_id: str, season: str, season_type: str):
        return self._read(game_id, "shot_chart_detail")


class NBAApiClient:
    """Bounded live adapter. Each dataset is requested once and has no auto retry."""

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    @staticmethod
    def _receipt(kind: str, started: str) -> dict:
        return {
            "status": "success",
            "source": "nba_api",
            "dataset": kind,
            "requested_at": started,
            "captured_at": now(),
            "retry_count": 0,
        }

    def fetch_boxscore(self, game_id: str):
        from nba_api.stats.endpoints import boxscoretraditionalv3

        started = now()
        payload = boxscoretraditionalv3.BoxScoreTraditionalV3(
            game_id=game_id, timeout=self.timeout
        ).get_dict()
        return payload, self._receipt("boxscore_traditional_v3", started)

    def fetch_play_by_play(self, game_id: str):
        from nba_api.stats.endpoints import playbyplayv3

        started = now()
        payload = playbyplayv3.PlayByPlayV3(
            game_id=game_id, timeout=self.timeout
        ).get_dict()
        return payload, self._receipt("play_by_play_v3", started)

    def fetch_shots(self, game_id: str, season: str, season_type: str):
        from nba_api.stats.endpoints import shotchartdetail

        started = now()
        payload = shotchartdetail.ShotChartDetail(
            team_id=0,
            player_id=0,
            game_id_nullable=game_id,
            context_measure_simple="FGA",
            season_nullable=season,
            season_type_all_star=season_type,
            timeout=self.timeout,
        ).get_dict()
        return payload, self._receipt("shot_chart_detail", started)


def _team_boxscore(team: dict[str, Any], side: str) -> dict[str, Any]:
    stats = team.get("statistics") or {}
    return clean(
        {
            "side": side,
            "team_id": int(team["teamId"]),
            "team_city": team.get("teamCity"),
            "team_name": team.get("teamName"),
            "team_tricode": team.get("teamTricode"),
            **stats,
        }
    )


def _player_rows(team: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for player in team.get("players") or []:
        stats = player.get("statistics") or {}
        rows.append(
            clean(
                {
                    "team_id": int(team["teamId"]),
                    "team_tricode": team.get("teamTricode"),
                    "person_id": int(player["personId"]),
                    "first_name": player.get("firstName"),
                    "family_name": player.get("familyName"),
                    "full_name": " ".join(
                        value
                        for value in [player.get("firstName"), player.get("familyName")]
                        if value
                    ),
                    "position": player.get("position"),
                    "comment": player.get("comment"),
                    "jersey_num": player.get("jerseyNum"),
                    **stats,
                }
            )
        )
    return rows


def _normalize_boxscore(
    payload: dict[str, Any], identity: GameIdentity
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    box = payload.get("boxScoreTraditional") or {}
    if str(box.get("gameId")) != identity.nba_game_id:
        raise ValueError("refresh_identity_mismatch")
    home = box.get("homeTeam") or {}
    away = box.get("awayTeam") or {}
    if (
        int(box.get("homeTeamId", home.get("teamId", -1))) != identity.home_team_id
        or int(box.get("awayTeamId", away.get("teamId", -1))) != identity.away_team_id
        or int(home.get("teamId", -1)) != identity.home_team_id
        or int(away.get("teamId", -1)) != identity.away_team_id
    ):
        raise ValueError("refresh_identity_mismatch")
    team_rows = [_team_boxscore(away, "away"), _team_boxscore(home, "home")]
    away_points = int(team_rows[0].get("points") or 0)
    home_points = int(team_rows[1].get("points") or 0)
    if away_points <= 0 or home_points <= 0 or away_points == home_points:
        raise ValueError("refresh_result_not_final")
    prefix = identity.nba_game_id[:3]
    season_type = "Playoffs" if prefix == "004" else (
        "Play-in Tournament" if prefix == "005" else "Regular Season"
    )
    game = {
        "game_id": identity.nba_game_id,
        "canonical_game_id": identity.canonical_game_id,
        "game_date": identity.eastern_date,
        "scheduled_tipoff": identity.scheduled_tipoff,
        "season_start": season_start_from_game_id(identity.nba_game_id),
        "season": f"{season_start_from_game_id(identity.nba_game_id)}-{str(season_start_from_game_id(identity.nba_game_id)+1)[-2:]}",
        "season_type": season_type,
        "home_team_id": identity.home_team_id,
        "away_team_id": identity.away_team_id,
        "home_team": team_rows[1].get("team_name"),
        "away_team": team_rows[0].get("team_name"),
        "home_team_tricode": team_rows[1].get("team_tricode"),
        "away_team_tricode": team_rows[0].get("team_tricode"),
        "home_points": home_points,
        "away_points": away_points,
        "actual_home_margin": home_points - away_points,
        "actual_total": home_points + away_points,
        "winner_team_id": identity.home_team_id if home_points > away_points else identity.away_team_id,
    }
    players = [*_player_rows(away), *_player_rows(home)]
    return game, team_rows, players


def _normalize_pbp(payload: dict[str, Any], game_id: str) -> list[dict[str, Any]]:
    game = payload.get("game") or {}
    if str(game.get("gameId")) != game_id:
        raise ValueError("refresh_pbp_identity_mismatch")
    rows = game.get("actions") or []
    return [clean(row) for row in rows]


def _normalize_shots(payload: dict[str, Any], game_id: str) -> list[dict[str, Any]]:
    sets = payload.get("resultSets") or []
    selected = next((item for item in sets if item.get("name") == "Shot_Chart_Detail"), None)
    if selected is None:
        raise ValueError("refresh_shot_dataset_missing")
    headers = selected.get("headers") or []
    rows = [dict(zip(headers, row, strict=True)) for row in selected.get("rowSet") or []]
    if any(str(row.get("GAME_ID")) != game_id for row in rows):
        raise ValueError("refresh_shot_identity_mismatch")
    return [clean(row) for row in rows]


def refresh_postgame(
    game_id: str,
    *,
    registry: IdentityRegistry,
    client: CachedRawClient | NBAApiClient | Any,
    output_root: Path,
) -> RefreshResult:
    identity = registry.resolve(game_id)
    if not identity.nba_game_id:
        raise ValueError("refresh_requires_nba_game_id")
    receipts: dict[str, Any] = {}
    errors: dict[str, dict[str, str]] = {}

    box_payload, box_receipt = client.fetch_boxscore(identity.nba_game_id)
    receipts["boxscore"] = clean({**box_receipt,"normalized_payload_hash":content_hash(box_payload)})
    game, teams, players = _normalize_boxscore(box_payload, identity)
    components = {
        "result": "available",
        "team_boxscores": "available",
        "player_boxscores": "available" if players else "missing",
        "play_by_play": "missing",
        "shots": "missing",
    }

    try:
        pbp_payload, pbp_receipt = client.fetch_play_by_play(identity.nba_game_id)
        receipts["play_by_play"] = clean({**pbp_receipt,"normalized_payload_hash":content_hash(pbp_payload)})
        play_by_play = _normalize_pbp(pbp_payload, identity.nba_game_id)
        components["play_by_play"] = "available" if play_by_play else "missing"
    except Exception as exc:
        play_by_play = []
        components["play_by_play"] = "failed"
        errors["play_by_play"] = {
            "error_type": type(exc).__name__,
            "message": str(exc)[:240],
        }

    try:
        shot_payload, shot_receipt = client.fetch_shots(
            identity.nba_game_id, game["season"], game["season_type"]
        )
        receipts["shots"] = clean({**shot_receipt,"normalized_payload_hash":content_hash(shot_payload)})
        shots = _normalize_shots(shot_payload, identity.nba_game_id)
        components["shots"] = "available" if shots else "missing"
    except Exception as exc:
        shots = []
        components["shots"] = "failed"
        errors["shots"] = {
            "error_type": type(exc).__name__,
            "message": str(exc)[:240],
        }

    core = {
        "schema_version": "1.0",
        "identity": identity.model_dump(),
        "game": game,
        "team_boxscores": teams,
        "player_boxscores": players,
        "play_by_play": play_by_play,
        "shots": shots,
        "components": components,
        "errors": errors,
    }
    bundle_hash = content_hash(core)
    bundle = {
        **core,
        "bundle_hash": bundle_hash,
        "refreshed_at": now(),
        "receipts": receipts,
    }
    folder = Path(output_root) / "games" / identity.nba_game_id
    path = folder / f"{bundle_hash}.json"
    if not path.exists():
        save(path, bundle)
    status = (
        "enhanced_available"
        if components["play_by_play"] == components["player_boxscores"] == components["shots"] == "available"
        else "basic_available"
    )
    missing = [
        {"component": name, "status": value, **errors.get(name, {})}
        for name, value in components.items()
        if value != "available"
    ]
    return RefreshResult(
        status=status,
        game_id=identity.nba_game_id,
        canonical_game_id=identity.canonical_game_id,
        components=components,
        bundle_hash=bundle_hash,
        bundle_path=path,
        missing_data=missing,
    )
