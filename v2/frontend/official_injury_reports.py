"""Append-only NBA official injury PDFs, game identity bindings and point-in-time reads."""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from v2.pregame_system.prediction_versions import DEFAULT_DATABASE, _instant
from v2.pregame_system.storage import content_hash

PARSER_VERSION = "nba-official-injury-live-v3"
ET = ZoneInfo("America/New_York")
PDF_RE = re.compile(r"https://ak-static\.cms\.nba\.com/referee/injury/Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})(?:_(\d{2}))?(AM|PM)\.pdf", re.I)
STATUSES = {"available", "active", "probable", "questionable", "doubtful", "out"}


def fetch_official_bytes(url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"official.nba.com", "ak-static.cms.nba.com"} or parsed.username or parsed.password:
        raise ValueError("official_injury_source_not_allowed")
    if parsed.hostname == "ak-static.cms.nba.com" and not PDF_RE.fullmatch(url):
        raise ValueError("official_injury_pdf_url_invalid")
    agent='Mozilla/5.0 (compatible; CourtVision/1.0)'
    try:
        with urlopen(Request(url, headers={"User-Agent": agent}), timeout=8) as response:
            if urlparse(response.url).hostname != parsed.hostname:
                raise ValueError("official_injury_redirect_invalid")
            body = response.read(8_000_001)
    except OSError:
        native = shutil.which("curl.exe")
        if not native:
            raise
        response = subprocess.run([native, "--fail", "--silent", "--show-error", "--max-time", "8",
            "--user-agent",agent,"--max-redirs", "0", "--proto", "=https", url], capture_output=True, timeout=10, check=True)
        body = response.stdout
    if not body or len(body) > 8_000_000:
        raise ValueError("official_injury_body_invalid")
    return body


def publication_from_url(url: str) -> datetime:
    match = PDF_RE.fullmatch(url)
    if not match:
        raise ValueError("official_injury_pdf_url_invalid")
    day, hour, minute, meridian = match.groups()
    local = datetime.strptime(f"{day} {hour}:{minute or '00'} {meridian.upper()}", "%Y-%m-%d %I:%M %p")
    from v2.pregame_system.storage import local_wall_time_to_utc
    return local_wall_time_to_utc(local, ET.key)


def parse_official_pdf(body: bytes, url: str) -> dict:
    if not body.startswith(b"%PDF-"):
        raise ValueError("official_injury_not_pdf")
    import pdfplumber
    # Reuse the already-tested header/name normalization from the archive importer.
    from v2.scripts.import_official_injury_report import _report_timestamp, _readable, _player_name, _compact
    records, heading = [], None
    context = dict(game_date=None, game_time=None, matchup=None, team=None)
    with pdfplumber.open(io.BytesIO(body)) as document:
        for page in document.pages:
            timestamp = _report_timestamp(page).astimezone(timezone.utc)
            if heading is not None and heading != timestamp:
                raise ValueError("official_injury_header_time_conflict")
            heading = timestamp
            table = page.extract_table({"vertical_strategy": "explicit", "explicit_vertical_lines": [page.width * x
                for x in (0.024, 0.137, 0.232, 0.307, 0.500, 0.690, 0.785, 0.980)],
                "horizontal_strategy": "lines", "snap_tolerance": 5, "join_tolerance": 5})
            if not table:
                raise ValueError("official_injury_table_unreadable")
            header_index=next((i for i,row in enumerate(table) if row and _compact(row[0])=='gamedate'),None)
            for raw in table[header_index+1 if header_index is not None else 0:]:
                cells = (list(raw or []) + [None] * 7)[:7]
                # Continuation pages can include the title inside the extracted
                # table without repeating its column headings. It is not a team.
                if _compact("".join(str(cell or "") for cell in cells)).startswith("injuryreport"):
                    continue
                if _compact(cells[0]) == "gamedate":
                    continue
                for key, value in zip(context, cells[:4]):
                    if value and _compact(value):
                        context[key] = _readable(value)
                player, status, reason = cells[4:]
                if "notyetsubmitted" in _compact(' '.join(str(cell or '') for cell in cells)):
                    records.append({**context, "player_name": None, "status": "not_submitted", "reason": ""})
                elif player and "," in player and status:
                    normalized = _readable(status).casefold()
                    if normalized not in STATUSES:
                        raise ValueError("official_injury_status_unknown:" + normalized)
                    records.append({**context, "player_name": _player_name(player), "status": normalized,
                                    "reason": _readable(reason)})
            # Unsubmitted teams can sit below the ruled table's final horizontal
            # border. Read these explicit status rows from the same PDF text.
            for line in (page.extract_text() or '').splitlines():
                if 'notyetsubmitted' not in _compact(line):
                    continue
                match=re.match(r'(?:(\d{2}/\d{2}/\d{4})\s+)?(?:(\d{2}:\d{2}\s*\(ET\))\s+)?(?:([A-Z]{2,3}@[A-Z]{2,3})\s+)?(.+?)\s+NOT\s*YET\s*SUBMITTED$',line.strip(),re.I)
                if not match:
                    raise ValueError('official_injury_submission_row_unreadable')
                day,game_time,matchup,team=match.groups()
                for key,value in [('game_date',day),('game_time',game_time),('matchup',matchup),('team',team)]:
                    if value:context[key]=_readable(value)
                if not any(r.get('team')==context['team'] and r.get('matchup')==context['matchup'] and r['status']=='not_submitted' for r in records):
                    records.append({**context,'player_name':None,'status':'not_submitted','reason':''})
    if heading is None or not records or heading.astimezone(ET).date() != publication_from_url(url).astimezone(ET).date():
        raise ValueError("official_injury_publication_or_rows_invalid")
    return {"published_at": heading.isoformat(), "publication_basis":"pdf_header",
            "advertised_at":publication_from_url(url).isoformat(), "records": records}


def _compact(value):
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _abbr(value):
    return {"NY": "NYK", "GS": "GSW", "SA": "SAS", "NO": "NOP", "WSH": "WAS", "UTAH":"UTA"}.get(value, value)


def bind_report_games(parsed: dict, games: list[dict]) -> list[dict]:
    from .export_data import team as team_payload
    from v2.pregame_system.forecasting_pilot import TEAM_NAMES as ENGLISH_NAMES, TEAM_ALIASES
    bindings = []
    for game in games:
        tipoff = _instant(game["scheduled_tipoff"], "scheduled_tipoff")
        day = tipoff.astimezone(ET).date()
        home, away = int(game["home_team_id"]), int(game["away_team_id"])
        teams = {tid: team_payload(tid) for tid in (home, away)}
        expected = f"{_abbr(teams[away]['abbreviation'])}@{_abbr(teams[home]['abbreviation'])}"
        rows, submitted, matched_context = [], set(), False
        for record in parsed["records"]:
            try:
                record_day = datetime.strptime(str(record["game_date"]), "%m/%d/%Y").date()
            except (TypeError, ValueError):
                continue
            matchup = str(record.get("matchup") or "").upper().replace(" ", "")
            pair = matchup.split("@")
            if len(pair) == 2:
                matchup = "@".join(_abbr(part) for part in pair)
            if record_day != day or matchup != expected:
                continue
            time_match=re.search(r'(\d{1,2}):(\d{2})',str(record.get('game_time') or ''))
            if not time_match or (int(time_match[1])%12,int(time_match[2])) != (tipoff.astimezone(ET).hour%12,tipoff.astimezone(ET).minute):
                continue
            matched_context = True
            def aliases(tid):
                abbreviation=teams[tid]['abbreviation'].lower()
                canonical=ENGLISH_NAMES.get(TEAM_ALIASES.get(abbreviation,abbreviation),'')
                source_name=str(game.get('home_team_name' if tid==home else 'away_team_name') or '')
                return {_compact(value) for value in [teams[tid]['name'],canonical,source_name,
                    canonical.replace('Los Angeles','LA'),source_name.replace('Los Angeles','LA')] if value}
            team_id = next((tid for tid in (home, away) if _compact(record.get("team")) in aliases(tid)), None)
            if team_id is None:
                raise ValueError("official_injury_team_identity_unmatched")
            if record["status"] != "not_submitted":
                submitted.add(team_id)
                rows.append({**record, "team_id": team_id})
        if matched_context:
            keys = [(r["team_id"], _compact(r["player_name"])) for r in rows]
            if len(keys) != len(set(keys)):
                raise ValueError("official_injury_duplicate_player")
            bindings.append({"game_id": game["game_id"], "home_team_id": home, "away_team_id": away,
                "scheduled_tipoff": tipoff.isoformat(), "submitted_teams": sorted(submitted), "records": rows})
    keys=[(_instant(b['scheduled_tipoff'],'tipoff').astimezone(ET).date(),b['home_team_id'],b['away_team_id']) for b in bindings]
    if len(keys)!=len(set(keys)):
        raise ValueError('official_injury_ambiguous_game_identity')
    return bindings


class OfficialInjuryReportStore:
    def __init__(self, path=DEFAULT_DATABASE, *, fetcher=fetch_official_bytes, parser=parse_official_pdf, clock=None):
        self.path = Path(path)
        self.fetcher, self.parser = fetcher, parser
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def initialize(self):
        with self.connect() as db:
            existing=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='official_injury_reports'").fetchone()
            if existing and 'unique(source_url,source_sha256)' in existing['sql'].lower().replace(' ','').replace('\n',''):
                # Upgrade only this task's newly added fact table. Copy and
                # compare every row before replacing its old unique constraint.
                db.execute('BEGIN IMMEDIATE')
                current=db.execute("SELECT sql FROM sqlite_master WHERE name='official_injury_reports'").fetchone()['sql']
                if 'unique(source_url,source_sha256)' in current.lower().replace(' ','').replace('\n',''):
                    new_sql=current.replace('official_injury_reports','official_injury_reports_parser_v2',1)
                    new_sql=re.sub(r'UNIQUE\s*\(source_url\s*,\s*source_sha256\s*\)',
                                   'UNIQUE(source_url,source_sha256,parser_version)',new_sql,flags=re.I)
                    db.execute(new_sql)
                    db.execute('INSERT INTO official_injury_reports_parser_v2 SELECT * FROM official_injury_reports')
                    before=[tuple(r) for r in db.execute('SELECT * FROM official_injury_reports ORDER BY report_id')]
                    after=[tuple(r) for r in db.execute('SELECT * FROM official_injury_reports_parser_v2 ORDER BY report_id')]
                    if before!=after: raise ValueError('official_injury_migration_row_mismatch')
                    db.execute('DROP TABLE official_injury_reports')
                    db.execute('ALTER TABLE official_injury_reports_parser_v2 RENAME TO official_injury_reports')
                db.commit()
            db.executescript('''
            CREATE TABLE IF NOT EXISTS official_injury_reports(
              report_id TEXT PRIMARY KEY, report_date TEXT NOT NULL, report_time TEXT NOT NULL,
              published_at TEXT NOT NULL, source_url TEXT NOT NULL, fetched_at TEXT NOT NULL,
              source_sha256 TEXT NOT NULL, raw_path TEXT NOT NULL, parser_version TEXT NOT NULL,
              parsed_json TEXT NOT NULL, UNIQUE(source_url,source_sha256,parser_version));
            CREATE TABLE IF NOT EXISTS official_injury_report_games(
              report_id TEXT NOT NULL, game_id TEXT NOT NULL, identity_json TEXT NOT NULL,
              PRIMARY KEY(report_id,game_id));
            CREATE TABLE IF NOT EXISTS official_injury_records(
              report_id TEXT NOT NULL, game_id TEXT NOT NULL, team_id INTEGER NOT NULL,
              player_name TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
              PRIMARY KEY(report_id,game_id,team_id,player_name));
            CREATE TABLE IF NOT EXISTS official_injury_fetch_runs(
              run_id TEXT PRIMARY KEY, attempted_at TEXT NOT NULL, source_url TEXT NOT NULL,
              status TEXT NOT NULL, report_id TEXT, error TEXT);
            CREATE INDEX IF NOT EXISTS official_injury_published ON official_injury_reports(published_at);
            CREATE TRIGGER IF NOT EXISTS injury_report_immutable_update BEFORE UPDATE ON official_injury_reports
              BEGIN SELECT RAISE(ABORT,'immutable_injury_report'); END;
            CREATE TRIGGER IF NOT EXISTS injury_report_immutable_delete BEFORE DELETE ON official_injury_reports
              BEGIN SELECT RAISE(ABORT,'immutable_injury_report'); END;
            CREATE TRIGGER IF NOT EXISTS injury_record_immutable_update BEFORE UPDATE ON official_injury_records
              BEGIN SELECT RAISE(ABORT,'immutable_injury_record'); END;
            CREATE TRIGGER IF NOT EXISTS injury_record_immutable_delete BEFORE DELETE ON official_injury_records
              BEGIN SELECT RAISE(ABORT,'immutable_injury_record'); END;
            CREATE TRIGGER IF NOT EXISTS injury_game_immutable_update BEFORE UPDATE ON official_injury_report_games
              BEGIN SELECT RAISE(ABORT,'immutable_injury_binding'); END;
            CREATE TRIGGER IF NOT EXISTS injury_game_immutable_delete BEFORE DELETE ON official_injury_report_games
              BEGIN SELECT RAISE(ABORT,'immutable_injury_binding'); END;
            ''')

    def _games(self, extra_game=None):
        with self.connect() as db:
            try:
                games = [dict(r) for r in db.execute("SELECT game_id,home_team_id,away_team_id,home_team_name,away_team_name,scheduled_tipoff FROM schedule_catalog_games WHERE status NOT IN ('cancelled','postponed')")]
            except sqlite3.OperationalError:
                games = []
        if extra_game:
            old = next((g for g in games if g['game_id']==extra_game['id']), {})
            item = {**old,"game_id": extra_game['id'], "home_team_id": extra_game['home']['id'], "away_team_id": extra_game['away']['id'],
                    "home_team_name":old.get('home_team_name') or extra_game['home'].get('name'),
                    "away_team_name":old.get('away_team_name') or extra_game['away'].get('name'), "scheduled_tipoff": extra_game['tipoff']}
            games = [g for g in games if g['game_id'] != item['game_id']] + [item]
        return games

    def ingest(self, url, body, *, extra_game=None, fetched_at=None):
        self.initialize()
        parsed = self.parser(body, url)
        published = _instant(parsed["published_at"], "report_published_at")
        if published.astimezone(ET).date() != publication_from_url(url).astimezone(ET).date() or published > self.clock():
            raise ValueError("official_injury_report_time_invalid")
        bindings = bind_report_games(parsed, self._games(extra_game))
        digest = hashlib.sha256(body).hexdigest()
        report_id = content_hash({"url": url, "sha256": digest, "parser_version":PARSER_VERSION})
        raw = self.path.parent / "official-injury-reports" / f"{digest}.pdf"
        raw.parent.mkdir(parents=True, exist_ok=True)
        if not raw.exists():
            with raw.open("xb") as stream:
                stream.write(body)
        if hashlib.sha256(raw.read_bytes()).hexdigest() != digest:
            raise ValueError("official_injury_raw_hash_mismatch")
        local = published.astimezone(ET)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO official_injury_reports VALUES(?,?,?,?,?,?,?,?,?,?)",
                (report_id, local.date().isoformat(), local.strftime('%H:%M'), published.isoformat(), url,
                 (fetched_at or self.clock().isoformat()), digest, str(raw.resolve()), PARSER_VERSION, json.dumps(parsed, ensure_ascii=False)))
            for binding in bindings:
                db.execute("INSERT OR IGNORE INTO official_injury_report_games VALUES(?,?,?)",
                    (report_id, binding['game_id'], json.dumps({k:v for k,v in binding.items() if k!='records'}, sort_keys=True)))
                db.executemany("INSERT OR IGNORE INTO official_injury_records VALUES(?,?,?,?,?,?)", [
                    (report_id,binding['game_id'],r['team_id'],r['player_name'],r['status'],r['reason']) for r in binding['records']])
        return {"report_id": report_id, "published_at": published.isoformat(), "matched_games": len(bindings),
                "records": sum(len(b['records']) for b in bindings)}

    def read_as_of(self, game, cutoff=None, *, captured_before_cutoff=False):
        limit = min(self.clock(), _instant(game['tipoff'], 'tipoff'),
                    _instant(cutoff, 'cutoff') if cutoff else self.clock())
        if not self.path.is_file():
            return {"status": "unavailable", "records": []}
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='official_injury_reports'").fetchone():
                return {"status": "unavailable", "records": []}
            matches = db.execute("SELECT r.*,g.identity_json FROM official_injury_reports r JOIN official_injury_report_games g USING(report_id) WHERE g.game_id=? AND r.published_at<=? ORDER BY r.published_at DESC,r.fetched_at DESC, r.report_id DESC", (game['id'],limit.isoformat())).fetchall()
            for row in matches:
                revisions=[candidate for candidate in matches if candidate['source_url']==row['source_url'] and candidate['published_at']==row['published_at']]
                # The same nominal report can be revised without changing its
                # header. Known later bodies cannot replace an earlier observed state.
                if len({candidate['source_sha256'] for candidate in revisions})>1 and _instant(row['fetched_at'],'fetched_at')>limit:
                    continue
                identity = json.loads(row['identity_json'])
                if (identity['home_team_id'] != game['home']['id'] or identity['away_team_id'] != game['away']['id'] or
                    _instant(identity['scheduled_tipoff'],'scheduled_tipoff') != _instant(game['tipoff'],'tipoff') or
                    (captured_before_cutoff and _instant(row['fetched_at'],'fetched_at') > limit)):
                    continue
                raw = Path(row['raw_path'])
                if not raw.is_file() or hashlib.sha256(raw.read_bytes()).hexdigest() != row['source_sha256']:
                    continue
                records = [dict(r) for r in db.execute("SELECT team_id,player_name,status,reason FROM official_injury_records WHERE report_id=? AND game_id=? ORDER BY team_id,player_name",(row['report_id'],game['id']))]
                meta = {"game_id":game['id'],"source_url":row['source_url'],"source_published_at":row['published_at'],
                        "captured_at":row['fetched_at'],"source_sha256":row['source_sha256'],
                        "raw_sha256":row['source_sha256'],"raw_path":row['raw_path'],"parser_version":row['parser_version']}
                return {**meta,"status":"published","report_id":row['report_id'],"report_date":row['report_date'],
                        "report_time":row['report_time'],"coverage":"complete" if len(identity['submitted_teams'])==2 else "partial",
                        "records":[{**r,**meta} for r in records]}
        return {"status":"unavailable","records":[]}

    def refresh(self, game=None, *, max_seconds=30, max_reports=3):
        """Controlled background/pre-generation collection. Never called by an HTTP read."""
        self.initialize()
        started=time.monotonic()
        at = self.clock()
        cutoff = min(at, _instant(game['tipoff'],'tipoff')) if game else at
        local_day = cutoff.astimezone(ET).date()
        urls, errors = set(), []
        year = local_day.year if local_day.month >= 7 else local_day.year-1
        pages = ["https://official.nba.com/",f"https://official.nba.com/nba-injury-report-{year}-{str(year+1)[-2:]}-season/"]
        seen_pages = set()
        for page in pages:
            if time.monotonic()-started >= max_seconds:
                errors.append({'url':page,'error':'collection_budget_exhausted'})
                break
            if page in seen_pages:
                continue
            seen_pages.add(page)
            try:
                html = self.fetcher(page).decode('utf-8',errors='replace')
                urls.update(m.group(0) for m in PDF_RE.finditer(html))
                if page == 'https://official.nba.com/':
                    linked = re.findall(r'https://official\.nba\.com/nba-injury-report-\d{4}-\d{2}-season/',html)
                    for url in reversed(list(dict.fromkeys(linked))):
                        if url not in seen_pages and url not in pages: pages.insert(1,url)
            except Exception as exc:
                details=getattr(exc,'stderr',b'')
                details=details.decode('utf-8',errors='replace') if isinstance(details,bytes) else str(details)
                errors.append({'url':page,'error':type(exc).__name__+':'+(details or str(exc))[:160]})
            if urls or len(seen_pages) >= 3:
                break
        eligible = sorted((url for url in urls if local_day-timedelta(days=1) <= publication_from_url(url).astimezone(ET).date() <= local_day and publication_from_url(url) <= cutoff), key=publication_from_url, reverse=True)[:max_reports]
        imported = []
        for url in eligible:
            if time.monotonic()-started >= max_seconds:
                errors.append({'url':url,'error':'collection_budget_exhausted'})
                break
            try:
                # Bind a previously downloaded league report to newly registered games, too.
                with self.connect() as db:
                    previous=db.execute('SELECT raw_path,fetched_at FROM official_injury_reports WHERE source_url=? ORDER BY fetched_at DESC LIMIT 1',(url,)).fetchone()
                fetched_at=None
                try:
                    body=self.fetcher(url)
                except Exception as exc:
                    if not previous: raise
                    errors.append({'url':url,'error':type(exc).__name__+':cached_previous_body'})
                    body=Path(previous['raw_path']).read_bytes();fetched_at=previous['fetched_at']
                result=self.ingest(url,body,extra_game=game,fetched_at=fetched_at)
                imported.append(result)
            except Exception as exc:
                errors.append({'url':url,'error':type(exc).__name__+':'+str(exc)[:160]})
        status='success' if imported and not errors else 'partial' if imported else 'unavailable' if not errors else 'failed'
        with self.connect() as db:
            db.execute('INSERT INTO official_injury_fetch_runs VALUES(?,?,?,?,?,?)',
                (str(uuid.uuid4()),at.isoformat(),'https://official.nba.com/',status,
                 imported[0]['report_id'] if imported else None,json.dumps(errors) if errors else None))
        return {'status':status,'reports':imported,'errors':errors}
