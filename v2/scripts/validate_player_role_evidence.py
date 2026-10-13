"""Offline acceptance checks for the 0022500564 player evidence artifact."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "data" / "player_evidence" / "0022500564_evidence_final_corrected"
GAME_ID = "0022500564"
CUTOFF_DATE = date(2026, 1, 13)

_COLLECTOR_SPEC = importlib.util.spec_from_file_location(
    "collect_player_role_evidence", ROOT / "scripts" / "collect_player_role_evidence.py"
)
assert _COLLECTOR_SPEC and _COLLECTOR_SPEC.loader
_COLLECTOR = importlib.util.module_from_spec(_COLLECTOR_SPEC)
_COLLECTOR_SPEC.loader.exec_module(_COLLECTOR)


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    errors: list[str] = []
    evidence_path = output_dir / "player_evidence.json"
    markdown_path = output_dir / "player_evidence.md"
    manifest_path = output_dir / "source_manifest.json"
    for path in (evidence_path, markdown_path, manifest_path):
        if not path.exists():
            errors.append(f"missing formal output: {path}")
    if errors:
        return {"status": "FAIL", "errors": errors, "checks": {}}

    evidence = load(evidence_path)
    manifest = load(manifest_path)
    players = evidence.get("players") or []
    checks: dict[str, Any] = {}
    checks["artifact_status_complete"] = evidence.get("status") == "complete"
    checks["ten_players"] = len(players) == 10
    checks["unique_names"] = len({p.get("player_name") for p in players}) == 10
    checks["cutoff_timezone_and_utc"] = evidence.get("cutoff", {}).get("utc") == "2026-01-14T00:00:00Z"
    checks["target_game_declared_excluded"] = evidence.get("cutoff", {}).get("target_game_excluded") == GAME_ID

    expected_names = {
        "Jamaree Bouyea", "Jordan Goodwin", "Jalen Green", "Myron Gardner", "Vladislav Goldin",
        "Tyler Herro", "Pelle Larsson", "Norman Powell", "Terry Rozier", "Jahmir Young",
    }
    checks["expected_names"] = {p.get("player_name") for p in players} == expected_names
    status_counts = Counter(p.get("official_availability", {}).get("status") for p in players)
    checks["status_counts"] = dict(status_counts)
    checks["status_counts_expected"] = status_counts == Counter({"Out": 6, "Available": 3, "Questionable": 1})
    out_players = [p for p in players if p.get("official_availability", {}).get("status") == "Out"]
    out_reason_groups = Counter(p.get("official_availability", {}).get("reason_classification", {}).get("group") for p in out_players)
    checks["out_reason_groups"] = dict(out_reason_groups)
    checks["out_health_two_non_health_four"] = out_reason_groups == Counter({"health_related": 2, "non_health": 4})
    checks["identity_resolved"] = all(p.get("identity", {}).get("match_status") == "exact_unique" for p in players)

    raw_paths = sorted((output_dir / "raw").glob("*.json"))
    raw_statuses: list[str] = []
    raw_hashes_valid = True
    for path in raw_paths:
        raw = load(path)
        raw_statuses.append(raw.get("status"))
        if raw.get("status") == "success":
            response_hash = raw.get("response_sha256")
            if not response_hash or hashlib.sha256(_COLLECTOR.canonical_json(raw.get("response"))).hexdigest() != response_hash:
                raw_hashes_valid = False
    checks["raw_artifact_count"] = len(raw_paths)
    checks["raw_all_success"] = bool(raw_paths) and all(status == "success" for status in raw_statuses)
    checks["raw_response_hashes_valid"] = raw_hashes_valid
    checks["manifest_request_count"] = manifest.get("request_count") == len(raw_paths)
    checks["manifest_no_credentials"] = manifest.get("credentials_saved") is False

    current_date_violations: list[tuple[str, str, str]] = []
    player_log_counts: dict[int, int] = {}
    for p in players:
        pid = p.get("player_id")
        if pid is None:
            continue
        raw_path = output_dir / "raw" / f"player_game_logs_{pid}_2025-26.json"
        raw = load(raw_path)
        rows = _COLLECTOR.extract_rows(raw.get("response") or {}, "PlayerGameLogs")
        filtered = _COLLECTOR.cutoff_player_rows(rows, season="2025-26")
        player_log_counts[int(pid)] = len(filtered)
        for row in filtered:
            game_date = _COLLECTOR.parse_date(row.get("GAME_DATE"))
            if game_date is None or game_date >= CUTOFF_DATE or str(row.get("GAME_ID")) == GAME_ID:
                current_date_violations.append((p.get("player_name"), str(row.get("GAME_ID")), str(row.get("GAME_DATE"))))
        count_from_stints = sum(s.get("games_played", 0) for s in p.get("current_season", {}).get("team_stints", []))
        if count_from_stints != len(filtered):
            errors.append(f"{p.get('player_name')} current stint GP {count_from_stints} != raw filtered rows {len(filtered)}")
    checks["current_rows_strictly_before_cutoff"] = not current_date_violations
    checks["current_date_violations"] = current_date_violations

    recent_window_violations: list[tuple[str, str, str]] = []
    recent_sizes: dict[str, int] = {}
    for p in players:
        recent = p.get("recent_team_window", {})
        games = recent.get("games", [])
        recent_sizes[p.get("player_name")] = len(games)
        if len(games) != 10:
            errors.append(f"{p.get('player_name')} recent team window has {len(games)} games")
        for game in games:
            game_date = _COLLECTOR.parse_date(game.get("game_date"))
            if game_date is None or game_date >= CUTOFF_DATE or game.get("game_id") == GAME_ID:
                recent_window_violations.append((p.get("player_name"), str(game.get("game_id")), str(game.get("game_date"))))
    checks["recent_windows_all_ten"] = all(size == 10 for size in recent_sizes.values())
    checks["recent_window_dates_strict"] = not recent_window_violations
    checks["recent_window_violations"] = recent_window_violations

    # Three direct spot checks connect the summary to the raw player log and
    # the boxscore row used for the starter field.
    spot_checks: dict[str, Any] = {}
    for name in ("Jalen Green", "Norman Powell", "Tyler Herro"):
        player = next(p for p in players if p.get("player_name") == name)
        pid = player["player_id"]
        raw = load(output_dir / "raw" / f"player_game_logs_{pid}_2025-26.json")
        rows = _COLLECTOR.cutoff_player_rows(_COLLECTOR.extract_rows(raw["response"], "PlayerGameLogs"), season="2025-26")
        recent = player["recent_team_window"]
        expected_appearances = sum(_COLLECTOR.row_is_appearance(row) and str(row.get("TEAM_ID")) == str(player["report_team"]["team_id"]) and str(row.get("GAME_ID")) in {g.get("game_id") for g in recent["games"]} for row in rows)
        actual_appearances = recent["appearance_count_in_recent_team_window"]
        spot_checks[name] = {
            "status": player["official_availability"]["status"],
            "raw_log_rows_before_cutoff": len(rows),
            "expected_recent_appearance_count_from_raw": expected_appearances,
            "reported_recent_appearance_count": actual_appearances,
            "matches": expected_appearances == actual_appearances,
        }
    checks["spot_checks"] = spot_checks
    checks["spot_checks_pass"] = all(item["matches"] for item in spot_checks.values())

    formal_hashes = manifest.get("formal_output_sha256") or {}
    checks["formal_hashes_match_manifest"] = all(
        formal_hashes.get(path.name) == hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (evidence_path, markdown_path)
    )
    if not all(value for key, value in checks.items() if isinstance(value, bool)):
        errors.append("one or more boolean acceptance checks failed")
    if errors:
        return {"status": "FAIL", "errors": errors, "checks": checks}
    return {"status": "PASS", "errors": [], "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = validate(args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
