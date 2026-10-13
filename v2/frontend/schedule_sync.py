"""Bounded ESPN schedule sync. This runtime writes schedule facts only, never AI tasks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from v2.pregame_system.market import team_id as nba_team_id

from .schedule_catalog import (
    NBA_TEAM_SCHEDULE_ROOT,
    ScheduleCatalog,
)


CHINA = ZoneInfo("Asia/Shanghai")
ESPN_API_HOST = "site.api.espn.com"
NBA_TEAMS_URL = f"{NBA_TEAM_SCHEDULE_ROOT}/teams?limit=30"
OFFICIAL_REGULAR_SCHEDULE_URL = "https://pr.nba.com/2026-27-nba-regular-season-schedule"
MAX_RESPONSE_BYTES = 12 * 1024 * 1024


def current_season_year(at: datetime | None = None) -> int:
    instant = at or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("timezone_required")
    local = instant.astimezone(CHINA)
    return local.year + 1 if local.month >= 7 else local.year


def _native_https_client():
    client = Path(os.environ.get("SystemRoot", '<local-configured-path>')) / "System32/curl.exe"
    return client if os.name == "nt" and client.is_file() else None


def _native_https_response(client, url, timeout):
    # Native Windows TLS works with this provider where the Python TLS route
    # can stall. Keep certificate verification, disallow redirects and bound IO.
    with tempfile.TemporaryDirectory(prefix="courtvision-schedule-https-") as temporary:
        body = Path(temporary) / "response.json"
        result = subprocess.run([str(client), "--disable", "--silent", "--show-error", "--proto", "=https",
            "--connect-timeout", str(min(timeout, 8)), "--max-time", str(timeout),
            "--max-filesize", str(MAX_RESPONSE_BYTES), "--output", str(body),
            "--write-out", "%{http_code}", "--url", url],
            capture_output=True, timeout=timeout + 2, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise OSError(f"native_https_exit_{result.returncode}")
        return int(result.stdout.decode("ascii").strip()), body.read_bytes()


def fetch_json(url: str, *, timeout: int = 20) -> dict:
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname != ESPN_API_HOST or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise ValueError("schedule_source_not_allowlisted")
    captured_at = datetime.now(timezone.utc).isoformat()
    receipt = {"url": url, "captured_at": captured_at, "status": "unavailable", "http_status": None,
               "raw_sha256": hashlib.sha256(b"").hexdigest(), "error": None}
    try:
        client = _native_https_client()
        if client:
            receipt["transport"] = "windows_native_https"
            status, raw = _native_https_response(client, url, timeout)
        else:
            receipt["transport"] = "python_https"
            request = Request(url, headers={"Accept": "application/json", "User-Agent": "25th-second-schedule-sync/1.0"})
            with urlopen(request, timeout=timeout) as response:
                status, raw = response.status, response.read(MAX_RESPONSE_BYTES + 1)
        receipt["http_status"] = status
        receipt["captured_at"] = datetime.now(timezone.utc).isoformat()
        if len(raw) > MAX_RESPONSE_BYTES:
            receipt["error"] = "response_too_large"
            return {**receipt, "payload": None}
        receipt["raw_sha256"] = hashlib.sha256(raw).hexdigest()
        if status != 200:
            receipt["error"] = "http_status_not_success"
            return {**receipt, "payload": None}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            receipt["error"] = "json_root_not_object"
            return {**receipt, "payload": None}
        receipt["status"] = "success"
        return {**receipt, "payload": payload}
    except HTTPError as exc:
        receipt.update({"http_status": exc.code, "error": f"http_{exc.code}"})
    except URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.gaierror):
            receipt["error"] = f"dns_resolution_failed:{reason.errno}"
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            receipt["error"] = "network_timeout"
        else:
            receipt["error"] = f"url_error:{type(reason).__name__}"
    except (TimeoutError, OSError) as exc:
        receipt["error"] = f"network_error:{type(exc).__name__}"
    except subprocess.TimeoutExpired:
        receipt["error"] = "native_https_timeout"
    except (UnicodeDecodeError, json.JSONDecodeError):
        receipt["error"] = "invalid_json"
    return {**receipt, "payload": None}


def _team_items(payload: dict) -> list[dict]:
    result=[]
    for sport in payload.get("sports") or []:
        for league in sport.get("leagues") or []:
            for entry in league.get("teams") or []:
                team = entry.get("team") or entry
                espn_id = str(team.get("id") or "")
                abbreviation = str(team.get("abbreviation") or "").upper()
                if not espn_id or not abbreviation:
                    continue
                try:
                    mapped_id = int(nba_team_id({"team": {"abbreviation": abbreviation}}))
                except (KeyError, TypeError, ValueError):
                    continue
                result.append({"espn_team_id": espn_id, "nba_team_id": mapped_id,
                               "abbreviation": abbreviation, "name": team.get("displayName")})
    return result


def _source_date_range(today: date, *, days_before: int, days_after: int) -> tuple[date, date]:
    # ESPN scoreboards key event dates in the source's U.S. calendar. A two-day
    # lead covers the Beijing day-boundary without adding a fixed-hour offset.
    return today - timedelta(days=days_before), today + timedelta(days=days_after)


def _regular_schedule_response(item: dict, response: dict, season_year: int) -> dict:
    if response.get("status") != "success":
        return response
    payload = response.get("payload") or {}
    requested = payload.get("requestedSeason") or {}
    try:
        requested_year, requested_type = int(requested.get("year", -1)), int(requested.get("type", -1))
    except (TypeError, ValueError):
        requested_year, requested_type = -1, -1
    if requested_year != season_year or requested_type != 2:
        return {**response, "status": "unavailable", "payload": None,
                "error": "requested_season_contract_mismatch"}
    payload = dict(payload)
    events=[]
    for event in payload.get("events") or []:
        competitions=event.get("competitions") or []
        competitors=(competitions[0].get("competitors") or []) if competitions else []
        team_ids={str(c.get("team", {}).get("id") or c.get("id") or "") for c in competitors}
        if item["espn_team_id"] not in team_ids:
            events.append({**event, "_schedule_validation_error": "team_schedule_membership_mismatch"})
        else:
            events.append(event)
    payload["events"] = events
    return {**response, "payload": payload, "requested_season": requested,
            "expected_game_type": "regular_season"}


def _daily_scoreboards(fetcher, start: date, end: date) -> list[dict]:
    """Use supported single-date requests and retain a receipt for every day."""
    if end < start or (end - start).days > 31:
        raise ValueError("scoreboard_window_out_of_bounds")
    urls = [f"{NBA_TEAM_SCHEDULE_ROOT}/scoreboard?dates={start + timedelta(days=offset):%Y%m%d}"
            for offset in range((end - start).days + 1)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        return list(pool.map(fetcher, urls))


def _preseason_scoreboard(fetcher, today: date, regular_first_day: date | None) -> list[dict]:
    start, _ = _source_date_range(today, days_before=2, days_after=0)
    end = regular_first_day - timedelta(days=1) if regular_first_day else today + timedelta(days=14)
    if end < start:
        end = start
    return [_preseason_response(response) for response in _daily_scoreboards(fetcher, start, end)]


def _preseason_response(response: dict) -> dict:
    if response.get("status") != "success":
        return response
    payload=dict(response.get("payload") or {})
    preseason_events=[]
    for event in payload.get("events") or []:
        try:
            kind=int(((event.get("seasonType") or {}).get("type") or
                      (event.get("seasonType") or {}).get("id") or
                      (event.get("season") or {}).get("type") or -1))
        except (AttributeError,TypeError,ValueError):
            continue
        if kind == 1:preseason_events.append(event)
    payload["events"] = preseason_events
    return {**response, "payload": payload, "expected_game_type": "preseason"}


def import_current_schedule(
    catalog: ScheduleCatalog,
    *,
    fetcher=fetch_json,
    season_year: int | None = None,
    now: datetime | None = None,
    team_ids: set[str] | None = None,
) -> dict:
    """Trial-importable current season bootstrap; it never registers generation tasks."""
    instant=now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("timezone_required")
    year=season_year or current_season_year(instant)
    today=instant.astimezone(CHINA).date()
    teams_response=fetcher(NBA_TEAMS_URL)
    if teams_response.get("status") != "success":
        return catalog.ingest("current_season_bootstrap", [teams_response], requested_start_date=today.isoformat())
    teams=_team_items(teams_response.get("payload") or {})
    if len(teams) != 30 or len({row["nba_team_id"] for row in teams}) != 30:
        failed={**teams_response,"status":"unavailable","payload":None,"error":"team_directory_not_30_unique_nba_teams"}
        return catalog.ingest("current_season_bootstrap", [failed], requested_start_date=today.isoformat())
    if team_ids is not None:
        teams=[team for team in teams if team["espn_team_id"] in team_ids]
    responses=[teams_response]
    def fetch_team(team):
        url=f"{NBA_TEAM_SCHEDULE_ROOT}/teams/{team['espn_team_id']}/schedule?season={year}&seasontype=2"
        return team, _regular_schedule_response(team, fetcher(url), year)
    team_schedule_responses=[]
    with ThreadPoolExecutor(max_workers=5) as pool:
        pending=[pool.submit(fetch_team, team) for team in teams]
        for future in as_completed(pending):
            team,response=future.result()
            # Rows with explicit wrong-team evidence are retained as validation conflicts.
            response={**response,"expected_espn_team_id":team["espn_team_id"],
                      "expected_nba_team_id":team["nba_team_id"],
                      "expected_team_abbreviation":team["abbreviation"],
                      "requested_season":response.get("requested_season") or {"year":year,"type":2},
                      "expected_game_type":"regular_season"}
            team_schedule_responses.append((team,response))
            responses.append(response)
    # Derive preseason cutoff from the regular schedule response itself, not the
    # top-level league season label (which may still say "Preseason").
    regular_dates=[]
    for response in responses:
        if response.get("status") != "success":
            continue
        for event in (response.get("payload") or {}).get("events") or []:
            value=event.get("date")
            if value:
                try: regular_dates.append(datetime.fromisoformat(value.replace("Z","+00:00")).date())
                except (AttributeError, ValueError): pass
    first_regular_day=min(regular_dates) if regular_dates else None
    preseason=_preseason_scoreboard(fetcher,today,first_regular_day)
    responses.extend(preseason)
    source_dates=[today-timedelta(days=2), first_regular_day or today+timedelta(days=14)]
    result=catalog.ingest(
        "current_season_bootstrap", responses,
        requested_start_date=min(source_dates).isoformat(),
        requested_end_date=max(source_dates).isoformat(),
        include_game_types={"preseason","regular_season"},
    )
    coverage=[]
    for team,response in sorted(team_schedule_responses,key=lambda item:int(item[0]["nba_team_id"])):
        payload=response.get("payload") or {}
        entries=payload.get("events") or []
        event_ids=set()
        for event in entries:
            for comp in event.get("competitions") or []:
                for competitor in comp.get("competitors") or []:
                    if str(competitor.get("team",{}).get("id") or competitor.get("id") or "") == team["espn_team_id"]:
                        if event.get("id"):
                            event_ids.add(str(event["id"]))
        coverage.append({"nba_team_id":team["nba_team_id"],"team":team["abbreviation"],
                         "espn_team_id":team["espn_team_id"],"source_status":response.get("status"),
                         "season_request":response.get("requested_season"),"games_returned":len(event_ids),
                         "error":response.get("error")})
    additions={"season_year":year,"requested_teams":len(teams),
               "teams_complete":sum(item["source_status"]=="success" and item["games_returned"]>0 for item in coverage),
               "team_coverage":coverage,
               "preseason_source_status":("success" if all(r.get("status") == "success" for r in preseason)
                                          else "partial" if any(r.get("status") == "success" for r in preseason) else "failed"),
               "preseason_events_returned":len({str(event.get("id")) for r in preseason
                                                for event in (r.get("payload") or {}).get("events") or []
                                                if event.get("id")})}
    result=catalog.update_run_result(result["run_id"],additions)
    return result


def refresh_scoreboard_window(catalog: ScheduleCatalog, *, fetcher=fetch_json, today: date | None = None,
                              days_after: int = 14) -> dict:
    local_today=today or datetime.now(CHINA).date()
    start,end=_source_date_range(local_today,days_before=2,days_after=days_after)
    responses = _daily_scoreboards(fetcher, start, end)
    return catalog.ingest("recent_scoreboard_sync",responses,requested_start_date=start.isoformat(),
                          requested_end_date=end.isoformat(),
                          include_game_types={"preseason","regular_season","playoffs"})


class ScheduleSyncRuntime:
    """Startup/daily/T-5-minute scoreboard and postgame refresh, isolated from AI Scheduler."""
    def __init__(self, catalog: ScheduleCatalog, *, fetcher=fetch_json, clock=None, sleeper=time.sleep,
                 postgame_sync: Callable[[ScheduleCatalog], list[dict]] | None = None,
                 injury_sync: Callable[[ScheduleCatalog], dict] | None = None):
        self.catalog=catalog
        self.fetcher=fetcher
        self.clock=clock or (lambda: datetime.now(timezone.utc))
        self.sleeper=sleeper
        self.postgame_sync=postgame_sync
        self.injury_sync=injury_sync
        self._thread: threading.Thread | None=None
        self._stop=threading.Event()
        self._calendar_day: date | None=None
        self._last_daily_attempt: datetime | None=None
        self._last_recent_attempt: datetime | None=None
        self._last_postgame_attempt: datetime | None=None
        self._last_cycle_at: datetime | None=None
        self._last_daily_status: str | None=None

    def cycle(self) -> list[dict]:
        now=self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("timezone_required")
        local=now.astimezone(CHINA)
        today=local.date()
        if today != self._calendar_day:
            self._calendar_day=today
            self._last_daily_attempt=None
            self._last_recent_attempt=None
            self._last_daily_status=None
        actions=[]
        resumed_after_pause = self._last_cycle_at is not None and (now-self._last_cycle_at).total_seconds() >= 90
        retry_delay = 5*60 if self._last_daily_status in {"failed", "partial"} else 24*60*60
        daily_due=(self._last_daily_attempt is None or resumed_after_pause or
                   (now-self._last_daily_attempt).total_seconds() >= retry_delay)
        if daily_due:
            self._last_daily_attempt=now
            daily_result=refresh_scoreboard_window(self.catalog,fetcher=self.fetcher,today=today,days_after=14)
            actions.append(daily_result)
            self._last_daily_status=daily_result["status"]
            self._last_recent_attempt=now
        window=self.catalog.homepage_window(now)
        has_today_games=window["today_count"] > 0
        recent_due=(self._last_recent_attempt is None or
                    (now-self._last_recent_attempt).total_seconds() >= 5*60)
        if has_today_games and recent_due:
            self._last_recent_attempt=now
            actions.append(refresh_scoreboard_window(self.catalog,fetcher=self.fetcher,today=today,days_after=1))
        postgame_due=(self._last_postgame_attempt is None or resumed_after_pause or
                      (now-self._last_postgame_attempt).total_seconds() >= 5*60)
        if actions or postgame_due:
            self._last_postgame_attempt=now
            try:
                if self.injury_sync is not None:
                    injury_result=self.injury_sync(self.catalog)
                elif self.fetcher is fetch_json:
                    from .official_injury_reports import OfficialInjuryReportStore
                    injury_result=OfficialInjuryReportStore(self.catalog.path).refresh()
                else:
                    injury_result={'status':'not_requested'}
                if injury_result.get('status')!='not_requested':
                    print(f'[25th-second] official injury sync {json.dumps(injury_result,ensure_ascii=False)}',flush=True)
            except Exception as exc:
                print(f'[25th-second] official injury sync failed {type(exc).__name__}',flush=True)
            try:
                if self.postgame_sync is not None:
                    postgame_results=self.postgame_sync(self.catalog)
                else:
                    from v2.postgame_system.auto_publish import sync_pending_postgames
                    postgame_results=sync_pending_postgames(self.catalog)
            except Exception as exc:
                postgame_results=[{"status":"failed","error_type":type(exc).__name__}]
            if postgame_results:
                actions.append({"postgame_sync":postgame_results})
                print(f"[25th-second] postgame sync {json.dumps(postgame_results,ensure_ascii=False)}",flush=True)
        # Count inactivity after completion, not time spent fetching. A slow
        # provider must not look like a suspended computer on the next tick.
        self._last_cycle_at=self.clock()
        return actions

    def _run(self):
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception as exc:
                # The catalog stores source failures; an unexpected local error
                # is printed for the existing service log and never erases rows.
                print(f"[25th-second] schedule sync runtime error: {type(exc).__name__}",flush=True)
            self._stop.wait(30)

    def start(self) -> "ScheduleSyncRuntime":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread=threading.Thread(target=self._run,name="schedule-facts-sync",daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",default=None)
    parser.add_argument("--import-current",action="store_true")
    parser.add_argument("--regular-team-ids",help="comma-separated ESPN team IDs for an isolated trial import")
    parser.add_argument("--refresh-window",action="store_true")
    args=parser.parse_args(argv)
    catalog=ScheduleCatalog(args.db) if args.db else ScheduleCatalog()
    if args.import_current:
        selected=set(args.regular_team_ids.split(",")) if args.regular_team_ids else None
        result=import_current_schedule(catalog,team_ids=selected)
    elif args.refresh_window:
        result=refresh_scoreboard_window(catalog)
    else:
        parser.error("choose --import-current or --refresh-window")
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
