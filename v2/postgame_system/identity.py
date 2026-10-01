"""Strict cross-provider NBA game identity registry."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from v2.pregame_system.storage import clean, content_hash, instant, now


EASTERN = ZoneInfo("America/New_York")


def _eastern_date(value: str) -> str:
    if len(str(value)) == 10:
        return str(value)
    return instant(value).astimezone(EASTERN).date().isoformat()


def identity_hash(value: dict[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "identity_hash"}
    return content_hash(payload)


class GameIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_game_id: str
    eastern_date: str
    scheduled_tipoff: str | None = None
    home_team_id: int
    away_team_id: int
    espn_game_id: str | None = None
    nba_game_id: str | None = None
    sources: list[dict[str, Any]]
    updated_at: str
    identity_hash: str


class IdentityRegistry:
    def __init__(self, path: Path, *, tipoff_tolerance_hours: float = 6.0):
        self.path = Path(path)
        self.tipoff_tolerance_hours = float(tipoff_tolerance_hours)
        self._records: list[dict[str, Any]] = []
        if self.path.is_file():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            records = payload.get("identities")
            if not isinstance(records, list):
                raise ValueError("identity_registry_invalid")
            for record in records:
                if record.get("identity_hash") != identity_hash(record):
                    raise ValueError("identity_hash_mismatch")
                GameIdentity.model_validate(record)
            self._records = records

    @property
    def records(self) -> list[GameIdentity]:
        return [GameIdentity.model_validate(record) for record in self._records]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".identity_registry_", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    clean({"schema_version": "1.0", "identities": self._records}),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _replace(self, index: int, record: dict[str, Any]) -> GameIdentity:
        record = clean(record)
        record["identity_hash"] = identity_hash(record)
        validated = GameIdentity.model_validate(record)
        self._records[index] = validated.model_dump()
        self._save()
        return validated

    def register_schedule(self, game: dict[str, Any]) -> GameIdentity:
        game_id = str(game.get("game_id") or "")
        if not game_id.startswith("espn:"):
            raise ValueError("schedule_requires_espn_game_id")
        existing = [
            (index, record)
            for index, record in enumerate(self._records)
            if record.get("espn_game_id") == game_id
        ]
        if len(existing) > 1:
            raise ValueError("identity_ambiguous")
        eastern_date = _eastern_date(game["scheduled_tipoff"])
        if existing:
            index, record = existing[0]
            if (
                record["eastern_date"] != eastern_date
                or int(record["home_team_id"]) != int(game["home_team_id"])
                or int(record["away_team_id"]) != int(game["away_team_id"])
            ):
                raise ValueError("identity_schedule_changed")
            return GameIdentity.model_validate(record)
        record = {
            "canonical_game_id": "cv:" + game_id,
            "eastern_date": eastern_date,
            "scheduled_tipoff": game["scheduled_tipoff"],
            "home_team_id": int(game["home_team_id"]),
            "away_team_id": int(game["away_team_id"]),
            "espn_game_id": game_id,
            "nba_game_id": None,
            "sources": [clean(game.get("source") or {"kind": "espn_schedule"})],
            "updated_at": now(),
        }
        record["identity_hash"] = identity_hash(record)
        validated = GameIdentity.model_validate(record)
        self._records.append(validated.model_dump())
        self._save()
        return validated

    def attach_nba_game(self, game: dict[str, Any]) -> GameIdentity:
        nba_game_id = str(game.get("game_id") or "")
        if len(nba_game_id) != 10 or not nba_game_id.isdigit():
            raise ValueError("invalid_nba_game_id")
        direct = [
            record for record in self._records if record.get("nba_game_id") == nba_game_id
        ]
        if len(direct) > 1:
            raise ValueError("identity_ambiguous")
        if direct:
            record = direct[0]
            if (
                int(record["home_team_id"]) != int(game["home_team_id"])
                or int(record["away_team_id"]) != int(game["away_team_id"])
            ):
                raise ValueError("identity_team_mismatch")
            return GameIdentity.model_validate(record)

        eastern_date = _eastern_date(game.get("game_date") or game["scheduled_tipoff"])
        home = int(game["home_team_id"])
        away = int(game["away_team_id"])
        reversed_pair = [
            record
            for record in self._records
            if record["eastern_date"] == eastern_date
            and int(record["home_team_id"]) == away
            and int(record["away_team_id"]) == home
        ]
        if reversed_pair:
            raise ValueError("identity_team_mismatch")
        candidates = [
            (index, record)
            for index, record in enumerate(self._records)
            if record["eastern_date"] == eastern_date
            and int(record["home_team_id"]) == home
            and int(record["away_team_id"]) == away
        ]
        if len(candidates) > 1:
            raise ValueError("identity_ambiguous")
        if candidates:
            index, record = candidates[0]
            incoming_tipoff = game.get("scheduled_tipoff")
            if record.get("scheduled_tipoff") and incoming_tipoff:
                delta = abs(
                    (
                        instant(record["scheduled_tipoff"])
                        - instant(incoming_tipoff)
                    ).total_seconds()
                )
                if delta > self.tipoff_tolerance_hours * 3600:
                    raise ValueError("identity_tipoff_mismatch")
            updated = dict(record)
            updated["nba_game_id"] = nba_game_id
            updated["updated_at"] = now()
            updated["sources"] = [
                *record["sources"],
                clean(game.get("source") or {"kind": "nba_game"}),
            ]
            return self._replace(index, updated)

        record = {
            "canonical_game_id": "cv:nba:" + nba_game_id,
            "eastern_date": eastern_date,
            "scheduled_tipoff": game.get("scheduled_tipoff"),
            "home_team_id": home,
            "away_team_id": away,
            "espn_game_id": None,
            "nba_game_id": nba_game_id,
            "sources": [clean(game.get("source") or {"kind": "nba_game"})],
            "updated_at": now(),
        }
        record["identity_hash"] = identity_hash(record)
        validated = GameIdentity.model_validate(record)
        self._records.append(validated.model_dump())
        self._save()
        return validated

    def resolve(self, value: str) -> GameIdentity:
        matches = [
            record
            for record in self._records
            if value
            in {
                record["canonical_game_id"],
                record.get("espn_game_id"),
                record.get("nba_game_id"),
            }
        ]
        if len(matches) != 1:
            raise ValueError("identity_not_unique")
        record = matches[0]
        if record.get("identity_hash") != identity_hash(record):
            raise ValueError("identity_hash_mismatch")
        return GameIdentity.model_validate(record)
