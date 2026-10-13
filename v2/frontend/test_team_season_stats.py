from __future__ import annotations

import json
import sqlite3
from urllib.parse import urlparse

from .server import _schedule_fact_game
from .team_season_stats import (
    PARSER_VERSION,
    attach_previous_season_team_stats,
    parse_hollinger_team_stats,
    parse_team_three_pct,
    previous_season,
    read_team_season_stats,
    sync_previous_season_team_stats,
)


def _hollinger_html() -> bytes:
    rows = ["<h1>2025-2026 Hollinger Team Statistics</h1><table>",
            "<tr><th>RK</th><th>TEAM</th><th>PACE</th><th>OFF EFF</th><th>DEF EFF</th></tr>"]
    for index in range(1, 31):
        slug = f"T{index:02d}"
        rows.append(
            f'<tr><td>{index}</td><td><a href="/nba/team/_/name/{slug.lower()}/sample">Team {index}</a></td>'
            f'<td>{90 + index / 10:.1f}</td><td>{110 + index:.1f}</td><td>{105 + index / 2:.1f}</td></tr>'
        )
    rows.append("</table>")
    return "".join(rows).encode("utf-8")


def _team_stats_payload(espn_team_id: str, index: int) -> bytes:
    payload = {
        # ESPN's top-level active-season metadata can be preseason; the explicit
        # requestedSeason below is the season to validate and use.
        "season": {"year": 2027, "type": 1, "name": "Preseason", "displayName": "2026-27"},
        "requestedSeason": {"year": 2026, "type": 2, "name": "Regular Season", "displayName": "2025-26"},
        "team": {"id": espn_team_id, "abbreviation": f"T{index:02d}",
                 "displayName": f"Fixture Team {index}"},
        "results": {"stats": {"categories": [
            {"name": "general", "stats": [{"name": "gamesPlayed", "value": 82}]},
            {"name": "offensive", "stats": [{"name": "threePointPct", "value": 30.0 + index}]},
        ]}},
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _seed_schedule_crosswalk(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schedule_catalog_games (home_team_id INTEGER, home_espn_team_id TEXT, away_team_id INTEGER, away_espn_team_id TEXT)")
    for index in range(1, 31, 2):
        db.execute("INSERT INTO schedule_catalog_games VALUES(?,?,?,?)",
                   (index, str(100 + index), index + 1, str(101 + index)))
    db.commit()
    db.close()


def test_previous_season_is_derived_from_game_season_not_wall_clock():
    assert previous_season("2026-27") == ("2025-26", 2026)
    assert previous_season(2027) == ("2025-26", 2026)
    assert previous_season("2030-31") == ("2029-30", 2030)
    assert previous_season("not-a-season") is None


def test_espn_parsers_use_requested_regular_season_and_return_all_30_hollinger_rows():
    html = _hollinger_html().decode("utf-8")
    rows = parse_hollinger_team_stats(html, "2025-26")
    assert len(rows) == 30
    assert rows["T01"] == {"pace": 90.1, "off_rating": 111.0, "def_rating": 105.5}
    assert parse_team_three_pct(json.loads(_team_stats_payload("101", 1)),
                                espn_team_id="101", season_year=2026) == (31.0, "2025-26")
    wrong = json.loads(_team_stats_payload("101", 1))
    wrong["requestedSeason"] = {"year": 2027, "type": 1, "displayName": "2026-27"}
    try:
        parse_team_three_pct(wrong, espn_team_id="101", season_year=2026)
    except ValueError as exc:
        assert str(exc) == "espn_requested_season_mismatch"
    else:
        raise AssertionError("current preseason metadata must not qualify as previous regular season")


def test_espn_sync_persists_five_metrics_with_provenance_and_projects_by_game_season(tmp_path):
    database = tmp_path / "versions.sqlite"
    _seed_schedule_crosswalk(database)
    hollinger = _hollinger_html()

    def fake_fetch(url: str) -> bytes:
        if "hollinger/teamstats" in url:
            return hollinger
        espn_id = urlparse(url).path.split("/")[-2]
        index = int(espn_id) - 100
        return _team_stats_payload(espn_id, index)

    result = sync_previous_season_team_stats(str(database), current_season="2026-27", fetch_bytes=fake_fetch)
    assert result["season"] == "2025-26"
    assert result["teams_imported"] == 30
    assert result["metric_rows_written"] == 150
    assert result["errors"] == []

    selection = read_team_season_stats(str(database), current_season=2027, home_team_id=1, away_team_id=2)
    assert selection["status"] == "available"
    assert selection["season"] == "2025-26"
    home = selection["teams"]["home"]
    away = selection["teams"]["away"]
    assert home["metrics"] == {"off_rating": 111.0, "def_rating": 105.5, "net_rating": 5.5, "pace": 90.1, "three_pct": 31.0}
    assert away["metrics"]["three_pct"] == 32.0
    assert all(row["source"] == "ESPN" and row["parser_version"] == PARSER_VERSION and
               row["source_url"] and row["captured_at"] and len(row["response_sha256"]) == 64
               for row in home["sources"])
    assert next(row for row in home["sources"] if row["metric"] == "net_rating")["calculation"] == "round(off_rating - def_rating, 1)"

    game = {"season": "2026-27", "home": {"id": 1}, "away": {"id": 2}}
    attach_previous_season_team_stats(game, str(database))
    assert game["previous_season_team_stats"]["season"] == "2025-26"
    assert game["previous_season_team_stats"]["teams"]["home"]["metrics"]["net_rating"] == 5.5


def test_missing_season_stats_remain_unavailable_without_recent_form_fallback(tmp_path):
    database = tmp_path / "empty.sqlite"
    sqlite3.connect(database).close()
    result = read_team_season_stats(str(database), current_season="2026-27", home_team_id=1, away_team_id=2)
    assert result["status"] == "unavailable"
    assert result["season"] == "2025-26"
    assert result["reason"] == "team_season_stats_not_imported"
    assert "team_season_stats" not in {row[0] for row in sqlite3.connect(database).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_current_season_label_survives_schedule_merge_for_selected_historical_game():
    record = {
        "game_id": "espn:401898390", "scheduled_tipoff": "2026-10-07T02:00:00+00:00",
        "status": "final", "game_type": "preseason", "season": 2027, "season_label": "2026-27",
        "home_team_id": 1610612744, "home_team_name": "Golden State Warriors", "home_espn_team_id": "9",
        "away_team_id": 1610612747, "away_team_name": "Los Angeles Lakers", "away_espn_team_id": "13",
        "schedule_revision": 1,
    }
    base = {"id": record["game_id"], "home": {"id": 1610612744}, "away": {"id": 1610612747},
            "schedule_revision": 1, "registered_game": True}
    merged = _schedule_fact_game(record, base)
    assert merged["season"] == 2027
    assert merged["season_label"] == "2026-27"
    unlabelled = {**record}
    unlabelled.pop("season_label")
    derived = _schedule_fact_game(unlabelled, base)
    assert derived["season_label"] == "2026-27"
