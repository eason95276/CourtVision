"""ESPN season-level team statistics stored beside CourtVision's SQLite state."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PARSER_VERSION = "espn-team-season-stats-v1"
SOURCE = "ESPN"
METRICS = ("off_rating", "def_rating", "net_rating", "pace", "three_pct")
_TEAM_STATS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/statistics?season={season_year}&seasontype=2"
_HOLLINGER_URL = "https://www.espn.com/nba/hollinger/teamstats/_/year/{season_year}"


def _season_end_year(value: Any) -> int | None:
    """Read a schedule season value as its ending year (ESPN season convention)."""
    if isinstance(value, int) and 1900 < value < 2200:
        return value
    text = str(value or "").strip()
    if text.isdigit() and 1900 < int(text) < 2200:
        return int(text)
    match = re.fullmatch(r"(\d{4})\s*[-/]\s*(\d{2}|\d{4})", text)
    if not match:
        return None
    start = int(match.group(1))
    suffix = match.group(2)
    end = int(suffix) if len(suffix) == 4 else (start // 100) * 100 + int(suffix)
    if end <= start:
        end += 100
    return end if end == start + 1 else None


def previous_season(current_season: Any) -> tuple[str, int] | None:
    """Return (display label, ESPN year parameter) for the prior full NBA season."""
    current_end = _season_end_year(current_season)
    if current_end is None:
        return None
    prior_end = current_end - 1
    return f"{prior_end - 1}-{prior_end % 100:02d}", prior_end


class _HollingerHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, Any]]] = []
        self._row: list[dict[str, Any]] | None = None
        self._cell: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = {"text": [], "hrefs": []}
        elif tag == "a" and self._cell is not None and attrs_map.get("href"):
            self._cell["hrefs"].append(attrs_map["href"])

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._cell["text"] = " ".join(" ".join(self._cell["text"]).split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def parse_hollinger_team_stats(html: str, season_label: str) -> dict[str, dict[str, float]]:
    """Parse ESPN's public Hollinger table, keyed by the ESPN team URL slug."""
    years = season_label.split("-")
    if len(years) != 2:
        raise ValueError("hollinger_season_label_invalid")
    heading_pattern = re.compile(
        rf"{re.escape(years[0])}\s*[-–]\s*{re.escape(str(int(years[0]) + 1))}\s+Hollinger Team Statistics",
        re.IGNORECASE,
    )
    if not heading_pattern.search(html):
        raise ValueError("hollinger_season_not_confirmed")

    parser = _HollingerHTMLParser()
    parser.feed(html)
    for header_index, row in enumerate(parser.rows):
        labels = [str(cell["text"]).strip().upper() for cell in row]
        if not {"TEAM", "PACE", "OFF EFF", "DEF EFF"}.issubset(set(labels)):
            continue
        indexes = {label: labels.index(label) for label in ("TEAM", "PACE", "OFF EFF", "DEF EFF")}
        output: dict[str, dict[str, float]] = {}
        for team_row in parser.rows[header_index + 1:]:
            if max(indexes.values()) >= len(team_row):
                continue
            team_cell = team_row[indexes["TEAM"]]
            slug = None
            for href in team_cell.get("hrefs", []):
                match = re.search(r"/_/name/([a-z0-9]+)/", str(href), re.IGNORECASE)
                if match:
                    slug = match.group(1).upper()
                    break
            if not slug:
                continue
            try:
                output[slug] = {
                    "pace": float(team_row[indexes["PACE"]]["text"]),
                    "off_rating": float(team_row[indexes["OFF EFF"]]["text"]),
                    "def_rating": float(team_row[indexes["DEF EFF"]]["text"]),
                }
            except (TypeError, ValueError):
                continue
        if output:
            return output
    raise ValueError("hollinger_team_table_not_found")


def _stat_index(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    results = payload.get("results") or {}
    stats = results.get("stats") or {}
    categories = stats.get("categories") or []
    general = next((category for category in categories if category.get("name") == "general"), {})
    offensive = next((category for category in categories if category.get("name") == "offensive"), {})
    by_name = {row.get("name"): row for row in (general.get("stats") or []) + (offensive.get("stats") or [])}
    return by_name, payload.get("requestedSeason") or {}


def parse_team_three_pct(payload: dict[str, Any], *, espn_team_id: str, season_year: int) -> tuple[float, str]:
    team = payload.get("team") or {}
    if str(team.get("id")) != str(espn_team_id):
        raise ValueError("espn_team_identity_mismatch")
    stats, requested = _stat_index(payload)
    if int(requested.get("year", -1)) != season_year or int(requested.get("type", -1)) != 2:
        raise ValueError("espn_requested_season_mismatch")
    if not requested.get("displayName"):
        raise ValueError("espn_requested_season_unlabeled")
    games = stats.get("gamesPlayed") or {}
    if not isinstance(games.get("value"), (int, float)) or games["value"] <= 0:
        raise ValueError("espn_team_games_unavailable")
    stat = stats.get("threePointPct")
    value = stat.get("value") if isinstance(stat, dict) else None
    if not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ValueError("espn_three_point_pct_unavailable")
    return float(value), str(requested["displayName"])


def _request_bytes(url: str, *, timeout: float = 20.0) -> bytes:
    request = Request(url, headers={"User-Agent": "CourtVision/1.0 (ESPN season team stats)"})
    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"http_status_{response.status}")
        return response.read()


def initialize_team_season_stats(db_path: str) -> None:
    db = sqlite3.connect(db_path, timeout=15)
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS team_season_stats (
            team_id INTEGER NOT NULL,
            espn_team_id TEXT NOT NULL,
            season TEXT NOT NULL,
            team_name TEXT NOT NULL,
            metric TEXT NOT NULL CHECK (metric IN ('off_rating','def_rating','net_rating','pace','three_pct')),
            value REAL NOT NULL,
            unit TEXT NOT NULL,
            source TEXT NOT NULL,
            source_url TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            response_sha256 TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            calculation TEXT,
            PRIMARY KEY (team_id, season, metric)
        )""")
        db.commit()
    finally:
        db.close()


def _team_crosswalk(db: sqlite3.Connection) -> dict[int, str]:
    rows = db.execute("""SELECT team_id, espn_team_id FROM (
        SELECT home_team_id AS team_id, home_espn_team_id AS espn_team_id FROM schedule_catalog_games
        UNION ALL
        SELECT away_team_id AS team_id, away_espn_team_id AS espn_team_id FROM schedule_catalog_games
    ) WHERE espn_team_id IS NOT NULL GROUP BY team_id, espn_team_id ORDER BY team_id""").fetchall()
    grouped: dict[int, set[str]] = {}
    reverse: dict[str, set[int]] = {}
    for team_id, espn_id in rows:
        grouped.setdefault(int(team_id), set()).add(str(espn_id))
        reverse.setdefault(str(espn_id), set()).add(int(team_id))
    if len(grouped) != 30 or any(len(ids) != 1 for ids in grouped.values()) or any(len(ids) != 1 for ids in reverse.values()):
        raise ValueError("schedule_espn_team_crosswalk_incomplete_or_conflicted")
    return {team_id: next(iter(ids)) for team_id, ids in grouped.items()}


def sync_previous_season_team_stats(
    db_path: str,
    *,
    current_season: Any,
    fetch_bytes: Callable[[str], bytes] = _request_bytes,
) -> dict[str, Any]:
    prior = previous_season(current_season)
    if prior is None:
        raise ValueError("current_season_invalid")
    season_label, season_year = prior
    db = sqlite3.connect(db_path, timeout=15)
    try:
        db.row_factory = sqlite3.Row
        crosswalk = _team_crosswalk(db)
    finally:
        db.close()

    hollinger_url = _HOLLINGER_URL.format(season_year=season_year)
    hollinger_raw = fetch_bytes(hollinger_url)
    hollinger_hash = hashlib.sha256(hollinger_raw).hexdigest()
    hollinger_captured = datetime.now(timezone.utc).isoformat()
    hollinger = parse_hollinger_team_stats(hollinger_raw.decode("utf-8", errors="replace"), season_label)

    imported: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for nba_team_id, espn_team_id in sorted(crosswalk.items()):
        url = _TEAM_STATS_URL.format(team_id=espn_team_id, season_year=season_year)
        try:
            raw = fetch_bytes(url)
            payload = json.loads(raw.decode("utf-8"))
            three_pct, requested_label = parse_team_three_pct(
                payload, espn_team_id=espn_team_id, season_year=season_year
            )
            if requested_label != season_label:
                raise ValueError("espn_requested_season_label_mismatch")
            team = payload.get("team") or {}
            abbreviation = str(team.get("abbreviation") or "").upper()
            advanced = hollinger.get(abbreviation)
            if advanced is None:
                raise ValueError("hollinger_team_identity_not_found")
            imported.append({
                "team_id": nba_team_id,
                "espn_team_id": espn_team_id,
                "team_name": str(team.get("displayName") or team.get("name") or abbreviation),
                "season": season_label,
                "metrics": {
                    **advanced,
                    "net_rating": round(advanced["off_rating"] - advanced["def_rating"], 1),
                    "three_pct": three_pct,
                },
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "three_pct_source_url": url,
                "three_pct_response_sha256": hashlib.sha256(raw).hexdigest(),
                "advanced_source_url": hollinger_url,
                "advanced_captured_at": hollinger_captured,
                "advanced_response_sha256": hollinger_hash,
            })
        except (HTTPError, URLError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            errors.append({"team_id": str(nba_team_id), "espn_team_id": espn_team_id,
                           "reason": f"{type(exc).__name__}:{str(exc)[:120]}"})

    if imported:
        initialize_team_season_stats(db_path)
        db = sqlite3.connect(db_path, timeout=15)
        try:
            rows = []
            for record in imported:
                for metric in METRICS:
                    if metric == "three_pct":
                        source_url = record["three_pct_source_url"]
                        captured_at = record["captured_at"]
                        response_sha = record["three_pct_response_sha256"]
                        calculation = None
                    else:
                        source_url = record["advanced_source_url"]
                        captured_at = record["advanced_captured_at"]
                        response_sha = record["advanced_response_sha256"]
                        calculation = "round(off_rating - def_rating, 1)" if metric == "net_rating" else None
                    rows.append((record["team_id"], record["espn_team_id"], season_label,
                                 record["team_name"], metric, record["metrics"][metric],
                                 "percent" if metric == "three_pct" else "per_100_possessions" if metric.endswith("rating") else "possessions_per_game" if metric == "pace" else "points_per_100_possessions",
                                 SOURCE, source_url, captured_at, response_sha, PARSER_VERSION, calculation))
            db.executemany("""INSERT INTO team_season_stats(
                team_id,espn_team_id,season,team_name,metric,value,unit,source,source_url,captured_at,
                response_sha256,parser_version,calculation)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(team_id,season,metric) DO UPDATE SET
                    espn_team_id=excluded.espn_team_id, team_name=excluded.team_name,
                    value=excluded.value, unit=excluded.unit, source=excluded.source,
                    source_url=excluded.source_url, captured_at=excluded.captured_at,
                    response_sha256=excluded.response_sha256, parser_version=excluded.parser_version,
                    calculation=excluded.calculation""", rows)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return {"season": season_label, "season_year": season_year, "teams_imported": len(imported),
            "metric_rows_written": len(imported) * len(METRICS), "errors": errors}


def read_team_season_stats(
    db_path: str,
    *,
    current_season: Any,
    home_team_id: int,
    away_team_id: int,
) -> dict[str, Any]:
    prior = previous_season(current_season)
    if prior is None:
        return {"status": "unavailable", "season": None, "reason": "game_season_unavailable", "teams": {}}
    season_label, _ = prior
    unavailable = {"status": "unavailable", "team_id": None, "metrics": {}}
    exists_db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        has_table = exists_db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_season_stats'"
        ).fetchone()
    finally:
        exists_db.close()
    if not has_table:
        return {"status": "unavailable", "season": season_label, "reason": "team_season_stats_not_imported",
                "teams": {"home": {**unavailable, "team_id": home_team_id},
                          "away": {**unavailable, "team_id": away_team_id}}}
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        by_team: dict[int, dict[str, sqlite3.Row]] = {}
        for row in db.execute("SELECT * FROM team_season_stats WHERE season=? AND team_id IN (?,?) AND source=?",
                              (season_label, home_team_id, away_team_id, SOURCE)):
            by_team.setdefault(int(row["team_id"]), {})[row["metric"]] = row
        teams = {}
        for side, team_id in (("home", home_team_id), ("away", away_team_id)):
            rows = by_team.get(int(team_id), {})
            metrics = {name: float(rows[name]["value"]) for name in METRICS if name in rows}
            sources = [dict(row) for row in rows.values()]
            teams[side] = {"team_id": team_id, "status": "available" if len(metrics) == len(METRICS) else "unavailable",
                           "metrics": metrics, "sources": sources}
        count = sum(row["status"] == "available" for row in teams.values())
        status = "available" if count == 2 else "partial" if count else "unavailable"
        return {"status": status, "season": season_label,
                **({} if status == "available" else {"reason": "one_or_more_team_season_stats_missing"}),
                "teams": teams}
    finally:
        db.close()


def attach_previous_season_team_stats(game: dict[str, Any], db_path: str) -> dict[str, Any]:
    home_id = (game.get("home") or {}).get("id") or game.get("home_team_id")
    away_id = (game.get("away") or {}).get("id") or game.get("away_team_id")
    try:
        home_id, away_id = int(home_id), int(away_id)
    except (TypeError, ValueError):
        game["previous_season_team_stats"] = {"status": "unavailable", "season": None,
                                               "reason": "team_identity_unavailable", "teams": {}}
        return game
    season = game.get("season_label") or game.get("season")
    game["previous_season_team_stats"] = read_team_season_stats(
        db_path, current_season=season, home_team_id=home_id, away_team_id=away_id
    )
    return game


def main() -> None:
    parser = argparse.ArgumentParser(description="Import ESPN complete-season team statistics into CourtVision SQLite")
    parser.add_argument("--database", required=True)
    parser.add_argument("--current-season", required=True, help="Current schedule season, e.g. 2026-27 or ESPN year 2027")
    args = parser.parse_args()
    result = sync_previous_season_team_stats(args.database, current_season=args.current_season)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
