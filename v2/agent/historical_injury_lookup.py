"""Bounded historical NBA report lookup; no paid model calls or database writes."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "v2/scripts")) if str(ROOT / "v2/scripts") not in sys.path else None
from v2.scripts import import_official_injury_report as official

RULE_VERSION = "historical_injury_lookup_v1"
TRANSIENT_TTL_SECONDS = 3600
MAX_BYTES = 8 * 1024 * 1024
OFFICIAL_URL = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-01-13_02_45PM.pdf"
# A verified archive address, not a claim to discover the latest pre-cutoff report.
TRUSTED_REPORTS = {"0022500564": (OFFICIAL_URL,)}


class LookupRejected(ValueError):
    def __init__(self, reason, *, source_published_at=None):
        super().__init__(reason)
        self.source_published_at = source_published_at


def utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def official_url(url):
    import re
    return bool(re.fullmatch(r"https://ak-static\.cms\.nba\.com/referee/injury/Injury-Report_\d{4}-\d{2}-\d{2}_\d{2}_\d{2}(?:AM|PM)\.pdf", url))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LookupRejected("redirect_not_allowed")


def fetch_bytes(url):
    """One GET, bounded body and elapsed time, no redirects or retries."""
    request = urllib.request.Request(url, headers={"User-Agent": "CourtVision-Historical-Research/1.0"})
    start = time.monotonic()
    with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
        chunks, size = [], 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                raise LookupRejected("response_too_large")
            if time.monotonic() - start > 25:
                raise TimeoutError()
            chunks.append(chunk)
        return b"".join(chunks)


def parse_report(path, url, game_id, cutoff_time, captured_at):
    if not official_url(url):
        raise LookupRejected("untrusted_source")
    identity = official.load_game_identity(game_id, official.DEFAULT_ANALYSIS_DATABASE)
    try:
        report_time, rows = official._extract_rows(Path(path))
    except Exception as exc:
        raise LookupRejected("unparseable_report") from exc
    cutoff, cutoff_utc = official._cutoff_to_utc(cutoff_time)
    if report_time > cutoff:
        raise LookupRejected("published_after_cutoff", source_published_at=utc(report_time))
    if report_time > captured_at:
        raise LookupRejected("publication_after_capture")
    # Require agreement between the official document heading and archive name.
    expected_name = report_time.strftime("Injury-Report_%Y-%m-%d_%I_%M%p.pdf")
    if not url.endswith("/" + expected_name):
        raise LookupRejected("publication_url_mismatch")
    matchup = identity["away_team_abbr"] + "@" + identity["home_team_abbr"]
    day = datetime.fromisoformat(identity["game_date"]).strftime("%m/%d/%Y")
    teams = {official._compact(identity[f"{side}_team_name"]):
             (str(identity[f"{side}_team_id"]), identity[f"{side}_team_name"])
             for side in ("away", "home")}
    selected = [r for r in rows if official._compact(r["matchup"]) == official._compact(matchup)
                and r["game_date"] == day]
    if not selected or {official._compact(r["team"]) for r in selected} != set(teams):
        raise LookupRejected("identity_mismatch")
    keys = [(official._compact(r["team"]), official._compact(r["player_name"])) for r in selected]
    if len(set(keys)) != len(keys):
        raise LookupRejected("duplicate_player")
    records = []
    for row, (team_key, player_key) in zip(selected, keys):
        if row["availability_status"] not in {"Out", "Questionable", "Available", "Doubtful", "Probable"}:
            raise LookupRejected("unknown_official_status")
        team_id, team_name = teams[team_key]
        records.append({"game_id": game_id, "source": official.SOURCE,
            "source_record_id": "|".join([game_id, utc(report_time), team_key, player_key]),
            "source_published_at": utc(report_time), "captured_at": utc(captured_at),
            "cutoff_time": cutoff_utc, "normalized_status": "available_historical_archive",
            "team_id": team_id, "team_name": team_name, "player_id": None,
            "player_name": row["player_name"], "availability_status": row["availability_status"],
            "reason": row["reason"], "source_url": url})
    records.sort(key=lambda r: (r["team_name"], r["player_name"], r["source_record_id"]))
    return {"records": records, "source_url": url, "source_published_at": utc(report_time),
            "captured_at": utc(captured_at), "raw_path": str(Path(path).resolve()),
            "raw_sha256": official.sha256_file(Path(path)),
            "publication_basis": "official PDF heading agrees with official archive filename",
            "historical_archive_downloaded_later": captured_at > cutoff,
            "persisted_before_cutoff_proven": False}


def local_report(game_id, cutoff_time, database=official.DEFAULT_SNAPSHOT_DATABASE):
    """Read existing fetch provenance and reparse raw bytes; never trust cards alone."""
    with closing(official._connect_read_only(Path(database))) as connection:
        rows = connection.execute("""SELECT DISTINCT source_url, raw_path,
            response_sha256, captured_at FROM source_fetch_runs
            WHERE game_id=? AND fetch_status='success' AND source_type='injuries'
            ORDER BY captured_at DESC""", (game_id,)).fetchall()
    for row in rows:
        path = Path(row["raw_path"])
        if path.is_file() and official.sha256_file(path) == row["response_sha256"]:
            try:
                return parse_report(path, row["source_url"], game_id, cutoff_time,
                                    datetime.fromisoformat(row["captured_at"].replace("Z", "+00:00")))
            except LookupRejected:
                continue
    return None


class InjuryResolver:
    def __init__(self, cache_dir, *, fetch=fetch_bytes, search=None, local=local_report,
                 clock=lambda: datetime.now(timezone.utc)):
        self.cache_dir = Path(cache_dir)
        self.fetch, self.search, self.local, self.clock = fetch, search, local, clock

    def resolve(self, game_id, cutoff_time, *, replay=False):
        now = self.clock()
        cutoff, cutoff_utc = official._cutoff_to_utc(cutoff_time)
        if cutoff >= now:
            raise LookupRejected("future_not_supported")
        official.load_game_identity(game_id, official.DEFAULT_ANALYSIS_DATABASE)
        query = {"game_id": game_id, "cutoff_time": cutoff_utc,
                 "query_target": "official player availability for the target game",
                 "rule_version": RULE_VERSION, "trusted_sources": list(TRUSTED_REPORTS.get(game_id, ())),
                 "search_policy": (
                     "local source captured before cutoff only; no network"
                     if replay else "public_bing_rss_discovery_official_pdf_only"
                 )}
        key = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
        folder = self.cache_dir / key
        folder.mkdir(parents=True, exist_ok=True)
        # Concurrent identical queries must not initiate duplicate network requests.
        lock = folder / "active.lock"
        try:
            handle = lock.open("x")
        except FileExistsError as exc:
            raise LookupRejected("query_in_progress_or_interrupted_check_lock") from exc
        try:
            with handle:
                handle.write(utc(now))
            return self._resolve(
                query,
                folder,
                now,
                game_id,
                cutoff_time,
                replay=replay,
            )
        finally:
            lock.unlink()

    def _resolve(self, query, folder, now, game_id, cutoff_time, *, replay=False):
        run = folder / "runs" / (now.strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:10])
        run.mkdir(parents=True)
        result = {**query, "status": "attempt_started", "queried_at": utc(now),
                  "request_count": 0, "retry_count": 0, "cache_hit": False,
                  "records": [], "events": [], "failure_kind": None,
                  "source_published_at": None, "captured_at": None}
        save(run / "attempt.json", result)

        def finish(**values):
            result.update(values)
            result["audit_path"] = str((run / "result.json").resolve())
            save(run / "result.json", result)
            return result

        def event(stage, **values):
            item = {"stage": stage, "time": utc(self.clock()), **values}
            result["events"].append(item)
            save(run / f"event_{len(result['events']):02}.json", item)

        try:
            local = self.local(game_id, cutoff_time)
        except Exception as exc:
            local = None
            event("local", status="unusable", reason=type(exc).__name__)
        else:
            event("local", status="hit" if local else "miss")
        if local:
            if replay:
                cutoff, _ = official._cutoff_to_utc(cutoff_time)
                try:
                    captured = datetime.fromisoformat(str(local.get("captured_at", "")).replace("Z", "+00:00"))
                except ValueError:
                    captured = None
                if (
                    captured is None
                    or captured.tzinfo is None
                    or captured.astimezone(timezone.utc) > cutoff.astimezone(timezone.utc)
                ):
                    event("local", status="rejected", reason="capture_after_cutoff")
                    local = None
                else:
                    return finish(**local, status="success", route="local_before_cutoff")
            else:
                return finish(**local, status="success", route="local")

        if replay:
            return finish(
                status="unavailable",
                failure_kind="deterministic",
                reason="historical_source_not_captured_before_cutoff",
                route="historical_no_network",
            )

        previous = sorted(folder.glob("runs/*/result.json"), key=lambda p: p.stat().st_mtime_ns)
        for path in reversed(previous):
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if any(cached.get(k) != v for k, v in query.items()):
                    raise ValueError("cached_query_mismatch")
            except (ValueError, OSError):
                return finish(status="rejected", failure_kind="deterministic",
                              reason="cache_integrity_failure", cache_hit=True)
            if cached.get("cache_hit") or cached.get("route") == "local":
                continue
            if cached.get("failure_kind") == "transient" and now >= datetime.fromisoformat(cached["expires_at"].replace("Z", "+00:00")):
                continue
            event("cache", status="hit", source_audit=str(path))
            if cached["status"] == "success":
                raw = Path(cached["raw_path"])
                if not raw.is_file() or official.sha256_file(raw) != cached["raw_sha256"]:
                    return finish(status="rejected", failure_kind="deterministic", reason="cache_integrity_failure", cache_hit=True)
                try:
                    bundle = parse_report(raw, cached["source_url"], game_id, cutoff_time,
                                          datetime.fromisoformat(cached["captured_at"].replace("Z", "+00:00")))
                except LookupRejected:
                    return finish(status="rejected", failure_kind="deterministic", reason="cache_integrity_failure", cache_hit=True)
                return finish(**bundle, status="success", route="cache", cache_hit=True)
            return finish(status=cached["status"], reason=cached["reason"],
                          failure_kind=cached["failure_kind"], expires_at=cached.get("expires_at"),
                          route="cache", cache_hit=True)
        event("cache", status="miss_or_expired")
        transient = False
        seen = set()
        # Retain deterministic source decisions even if a different source had
        # a transient error and the overall query is eligible after its TTL.
        source_rejections = {}
        for path in previous:
            old = json.loads(path.read_text(encoding="utf-8"))
            for item in old.get("events", []):
                if item.get("failure_kind") == "deterministic" and item.get("source_url"):
                    source_rejections[item["source_url"]] = item["reason"]

        def request(url, stage):
            result["request_count"] += 1
            event(stage, status="request_started", source_url=url)
            path = run / f"response_{result['request_count']:02}.raw"
            try:
                body = self.fetch(url)
            except urllib.error.HTTPError as exc:
                # Keep available error body, never request headers or exception text.
                try:
                    body = exc.read(MAX_BYTES + 1)
                    if body is not None and len(body) <= MAX_BYTES:
                        with path.open("xb") as out:
                            out.write(body)
                        event(stage, status="http_error_response", http_status=exc.code,
                              captured_at=utc(self.clock()), raw_path=str(path),
                              response_sha256=official.sha256_file(path))
                except (OSError, AttributeError):
                    pass
                finally:
                    exc.close()
                raise
            with path.open("xb") as out:
                out.write(body)
            event(stage, status="response_received", source_url=url, raw_path=str(path),
                  captured_at=utc(self.clock()),
                  response_sha256=official.sha256_file(path))
            return path

        def candidate(url, stage):
            nonlocal transient
            if url in seen:
                return None
            seen.add(url)
            if not official_url(url):
                event(stage, status="rejected", reason="untrusted_source")
                return None
            if url in source_rejections:
                event(stage, status="rejected_from_cache", source_url=url,
                      reason=source_rejections[url], failure_kind="deterministic")
                return None
            try:
                path = request(url, stage)
                return parse_report(path, url, game_id, cutoff_time, self.clock())
            except Exception as exc:
                temporary = isinstance(exc, (TimeoutError, ConnectionError, urllib.error.URLError, OSError)) and not isinstance(exc, LookupRejected)
                transient |= temporary
                reason = str(exc) if isinstance(exc, LookupRejected) else type(exc).__name__
                event(stage, status="failed" if temporary else "rejected", source_url=url,
                      reason=reason, error_type=type(exc).__name__,
                      http_status=getattr(exc, "code", None),
                      source_published_at=getattr(exc, "source_published_at", None),
                      failure_kind="transient" if temporary else "deterministic")
                return None

        for url in TRUSTED_REPORTS.get(game_id, ())[:1]:
            bundle = candidate(url, "trusted_source")
            if bundle:
                return finish(**bundle, status="success", route="trusted_source")
        try:
            event("open_search", status="started_after_trusted_failure")
            identity = official.load_game_identity(game_id, official.DEFAULT_ANALYSIS_DATABASE)
            text = f"NBA injury report {identity['game_date']} {identity['away_team_abbr']} {identity['home_team_abbr']}"
            if self.search is not None:
                urls = self.search(text)
            else:
                url = "https://www.bing.com/search?format=rss&q=" + urllib.parse.quote(text)
                raw = request(url, "open_search")
                urls = [x.text for x in ET.fromstring(raw.read_bytes()).findall("./channel/item/link") if x.text]
            save(run / "search_candidates.json", {"query": text, "urls": urls[:3]})
            for url in urls[:3]:
                bundle = candidate(url, "search_candidate")
                if bundle:
                    return finish(**bundle, status="success", route="search_candidate")
        except Exception as exc:
            transient = True
            event("open_search", status="failed", reason=type(exc).__name__)
        kind = "transient" if transient else "deterministic"
        return finish(status="failed" if transient else "rejected", failure_kind=kind,
                      reason="no_eligible_report", route="exhausted",
                      expires_at=utc(now + timedelta(seconds=TRANSIENT_TTL_SECONDS)) if transient else None)
