from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from nba_api.stats.endpoints import (
    boxscoretraditionalv3,
    playbyplayv3,
    shotchartdetail,
)


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "nba_stats"
RAW_DIR.mkdir(parents=True, exist_ok=True)

GAMES = [
    {
        "phase": "early",
        "game_id": "0022500166",
        "date": "2025-11-04",
        "away": "ORL",
        "home": "ATL",
    },
    {
        "phase": "middle",
        "game_id": "0022500564",
        "date": "2026-01-13",
        "away": "PHX",
        "home": "MIA",
    },
    {
        "phase": "late",
        "game_id": "0022501099",
        "date": "2026-03-31",
        "away": "PHX",
        "home": "ORL",
    },
]


def save_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_game(game: dict) -> list[dict]:
    game_id = game["game_id"]
    calls = [
        (
            "boxscore_traditional_v3",
            lambda: boxscoretraditionalv3.BoxScoreTraditionalV3(
                game_id=game_id,
                timeout=20,
            ).get_dict(),
        ),
        (
            "play_by_play_v3",
            lambda: playbyplayv3.PlayByPlayV3(
                game_id=game_id,
                timeout=20,
            ).get_dict(),
        ),
        (
            "shot_chart_detail",
            lambda: shotchartdetail.ShotChartDetail(
                team_id=0,
                player_id=0,
                game_id_nullable=game_id,
                context_measure_simple="FGA",
                season_nullable="2025-26",
                season_type_all_star="Regular Season",
                timeout=20,
            ).get_dict(),
        ),
    ]

    log_rows: list[dict] = []
    for label, call in calls:
        output_path = RAW_DIR / f"{game_id}_{label}.json"
        if output_path.exists():
            log_rows.append(
                {
                    "game_id": game_id,
                    "dataset": label,
                    "status": "cached_no_request",
                    "bytes": output_path.stat().st_size,
                    "path": str(output_path.relative_to(ROOT)),
                }
            )
            continue
        started = time.perf_counter()
        try:
            payload = call()
            elapsed = round(time.perf_counter() - started, 3)
            save_json(output_path, payload)
            log_rows.append(
                {
                    "game_id": game_id,
                    "dataset": label,
                    "status": "success",
                    "elapsed_seconds": elapsed,
                    "bytes": output_path.stat().st_size,
                    "path": str(output_path.relative_to(ROOT)),
                    "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception as exc:
            elapsed = round(time.perf_counter() - started, 3)
            log_rows.append(
                {
                    "game_id": game_id,
                    "dataset": label,
                    "status": "failed",
                    "elapsed_seconds": elapsed,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                }
            )
        time.sleep(1.5)
    return log_rows


def main() -> None:
    access_log: list[dict] = []
    for game in GAMES:
        access_log.extend(fetch_game(game))
    access_log_path = ROOT / "data" / "raw" / "access_log.json"
    if any(row["status"] != "cached_no_request" for row in access_log):
        save_json(
            access_log_path,
            {
                "selection": GAMES,
                "requests": access_log,
            },
        )
    print(json.dumps(access_log, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
