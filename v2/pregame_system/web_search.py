"""Cutoff-aware, source-filtered web context for the 第25秒 analyst."""
from __future__ import annotations

import hashlib
import json
import os
import re
import ipaddress
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .free_search_mcp_client import FreeSearchMCPError, FreeSearchMCPTransport
from .storage import BASE, content_hash, now, save


TRUSTED_DOMAINS = (
    "nba.com",
    "espn.com",
    "apnews.com",
    "reuters.com",
    "cbssports.com",
    "sports.yahoo.com",
)

# Public market pages are a display-only fallback. They are deliberately kept
# separate from the bookmaker allowlist used by the numeric prediction path.
MARKET_DOMAINS = (
    "sports.yahoo.com",
    "actionnetwork.com",
    "fanduel.com",
    "betmgm.com",
    "foxsports.com",
    "oddschecker.com",
    "sportsline.com",
    "cbssports.com",
    "espn.com",
)
MARKET_SOURCE_TIER = {
    "sports.yahoo.com": 2,
    "actionnetwork.com": 2,
    "fanduel.com": 1,
    "betmgm.com": 1,
    "foxsports.com": 2,
    "oddschecker.com": 2,
    "sportsline.com": 2,
    "cbssports.com": 2,
    "espn.com": 2,
}
ALLOWED_FOCUS_CATEGORIES = frozenset({"availability", "lineup", "roster_news", "game_story", "market"})


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").casefold().removeprefix("www.")


def _public_discovery_url(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or host in {"localhost"} or host.endswith((".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host


def _trusted(url: str) -> bool:
    host = _host(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in TRUSTED_DOMAINS)


def _market_domain(url: str) -> str | None:
    host = _host(url)
    return next(
        (domain for domain in MARKET_DOMAINS if host == domain or host.endswith(f".{domain}")),
        None,
    )


def is_allowed_public_market_source(url: str) -> bool:
    """Return whether a display-only public market source is allowlisted."""
    return _market_domain(url) is not None


def _team_aliases(game: dict[str, Any], side: str) -> tuple[str, ...]:
    name = str(game.get(f"{side}_team") or game.get(f"{side}_team_name") or "").strip()
    aliases = {name, name.split()[-1] if name else ""}
    known = {
        1610612755: ("PHI", "76ers", "Sixers"),
        1610612752: ("NYK", "Knicks"),
    }
    aliases.update(known.get(game.get(f"{side}_team_id"), ()))
    return tuple(sorted((item for item in aliases if item), key=len, reverse=True))


def _mentions(text: str, aliases: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(alias.casefold() in folded for alias in aliases)


def _official_transaction_date(result: dict[str, Any]) -> datetime | None:
    """Accept a date printed in the lead of an NBA official transaction."""
    url = str(result.get("url") or "")
    title = str(result.get("title") or "")
    if _host(url) != "nba.com" or not re.search(
        r"\b(signs?|signed|acquires?|acquired|trades?|traded)\b", title, re.IGNORECASE
    ):
        return None
    lead = str(result.get("content") or "")[:350]
    match = re.search(
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}\b",
        lead,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime.strptime(match.group(), "%B %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _nearby_spreads(
    text: str,
    aliases: tuple[str, ...],
    stop_aliases: tuple[str, ...] = (),
) -> list[float]:
    values: list[float] = []
    folded = text.casefold()
    for alias in aliases:
        for match in re.finditer(re.escape(alias.casefold()), folded):
            tail = text[match.end():match.end() + 48]
            folded_tail = tail.casefold()
            stops = [
                position
                for other in stop_aliases
                if (position := folded_tail.find(other.casefold())) >= 0
            ]
            if stops:
                tail = tail[:min(stops)]
            for raw in re.findall(r"[+-]\s*\d{1,3}(?:[.,]\d+)?", tail):
                try:
                    value = float(raw.replace(" ", "").replace(",", "."))
                except ValueError:
                    continue
                if 0 < abs(value) <= 40 and value not in values:
                    values.append(value)
    return values


def _totals(text: str) -> list[float]:
    pattern = re.compile(
        r"(?:\btotal\b|over\s*/?\s*under|\bo\s*/\s*u\b|\bover\b|\bunder\b|\b[ou]\b)"
        r"[^\d]{0,12}(\d{3}(?:[.,]\d+)?)",
        flags=re.IGNORECASE,
    )
    values: list[float] = []
    for raw in pattern.findall(text):
        value = float(raw.replace(",", "."))
        if 150 <= value <= 300 and value not in values:
            values.append(value)
    return values


def _prose_home_spread(content, game):
    """Explicit team favorites/underdog prose, not author predictions or scores."""
    candidates=[]
    for side in ('home','away'):
        aliases=_team_aliases(game,side)
        pattern=r'(?<![A-Za-z])(?:'+'|'.join(re.escape(a)for a in aliases)+r')(?![A-Za-z])[^\n.!?]{0,55}?\b(\d{1,2}(?:\.\d+)?)\s*[- ]point\s+(?:[\"\x27]?home[\"\x27]?\s+)?(favorites?|favourites?|underdogs?)\b'
        for match in re.finditer(pattern,content,re.I):
            around=content[max(0,match.start()-10):match.end()+80]
            if not re.search(r'spread|handicap',around,re.I):continue
            sentence_start=max(content.rfind('\n',0,match.start()),content.rfind('.',0,match.start()),content.rfind('!',0,match.start()))+1
            if re.search(r'predict|expect|project|would|could|should|if\b',content[sentence_start:match.end()],re.I):continue
            points=float(match.group(1))
            if not 0<points<=40:continue
            own=points if match.group(2).lower().startswith('underdog') else -points
            candidates.append(own if side=='home' else -own)
    return candidates[0]if candidates and len(set(candidates))==1 else None


def _prose_moneyline(content,game,side):
    aliases=set(_team_aliases(game,side));other=set(_team_aliases(game,'away'if side=='home'else'home'))
    for s,destination in ((side,aliases),('away'if side=='home'else'home',other)):
        name=game.get(s+'_team')or game.get(s+'_team_name')or''
        if len(name.split())>1:destination.add(' '.join(name.split()[:-1]))
    aliases-=other
    pattern=r'(?<![A-Za-z])(?:'+'|'.join(re.escape(a)for a in sorted(aliases,key=len,reverse=True))+r')(?![A-Za-z])[^\n.!?]{0,45}?([+-]\d{3,4})(?:\s+moneyline|\s+(?:underdog|favorite|favourite))'
    values=[float(m.group(1))for m in re.finditer(pattern,content,re.I)]
    return values[0]if values and len(set(values))==1 else None


def _mentions_game_date(
    text: str,
    game: dict[str, Any],
    *,
    captured_at: str | None = None,
    allow_yearless: bool = False,
) -> bool:
    raw = str(game.get("scheduled_tipoff") or "")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return not raw
    month = value.strftime("%B").casefold()
    short_month = value.strftime("%b").casefold()
    folded = text.casefold()
    tokens = (
        value.strftime("%Y-%m-%d"),
        value.strftime("%m/%d/%Y"),
        value.strftime("%m/%d/%y"),
        value.strftime("%d/%m/%Y"),
        f"{month} {value.day}, {value.year}",
        f"{short_month} {value.day}, {value.year}",
    )
    if any(token.casefold() in folded for token in tokens):
        return True
    if not allow_yearless or not captured_at:
        return False
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        captured = captured.astimezone(timezone.utc)
        tipoff = value.astimezone(timezone.utc)
    except ValueError:
        return False
    if captured >= tipoff or captured.year != tipoff.year:
        return False
    yearless_tokens = (
        f"{month} {value.day}",
        f"{short_month} {value.day}",
        value.strftime("%m/%d"),
    )
    return any(token.casefold() in folded for token in yearless_tokens)


def parse_public_market_reference(
    result: dict[str, Any],
    game: dict[str, Any],
    *,
    captured_at: str,
) -> dict[str, Any] | None:
    """Conservatively parse one source without merging lines across pages."""
    url = str(result.get("url") or "")
    domain = _market_domain(url)
    if domain is None:
        return None
    title = str(result.get("title") or "")
    content = str(result.get("content") or "")
    combined = f"{title}\n{content}"
    away_aliases = _team_aliases(game, "away")
    home_aliases = _team_aliases(game, "home")
    if not (_mentions(combined, away_aliases) and _mentions(combined, home_aliases)):
        return None
    if not _mentions_game_date(
        combined,
        game,
        captured_at=captured_at,
        allow_yearless=MARKET_SOURCE_TIER[domain] == 1,
    ):
        return None
    # Titles commonly repeat both team names before any line value. Including
    # them in the proximity scan can bind the first signed number to the wrong
    # team. Only parse prices from the result body; title/date remain useful
    # for matchup and event-date validation above.
    away_values = _nearby_spreads(content, away_aliases, home_aliases)
    home_values = _nearby_spreads(content, home_aliases, away_aliases)
    pair = next(
        (
            (away, home)
            for home in home_values
            for away in away_values
            if home * away < 0 and abs(abs(home) - abs(away)) <= 0.01
        ),
        None,
    )
    total_values = _totals(combined)
    # A complete paired spread and a total from the same page are required.
    # Generic multi-game boards otherwise let a preceding game's total bind to
    # the target matchup merely because both team names appear later on page.
    prose_spread=_prose_home_spread(content,game)if pair is None else None
    if (pair is None and prose_spread is None) or not total_values:
        return None
    home_spread = pair[1]if pair is not None else prose_spread
    return {
        "status": "available",
        "usage": "display_only_public_market_reference",
        "eligible_prediction_input": False,
        "perspective": "home",
        "home_spread": home_spread,
        "away_spread": pair[0]if pair is not None else -home_spread,
        "home_moneyline": _prose_moneyline(content,game,'home'),
        "away_moneyline": _prose_moneyline(content,game,'away'),
        "expected_home_margin": -home_spread if home_spread is not None else None,
        "total": total_values[0] if total_values else None,
        "source_title": title[:300],
        "source_url": url,
        "source_domain": _host(url),
        "source_tier": MARKET_SOURCE_TIER[domain],
        "published_at": result.get("published_date"),
        "captured_at": captured_at,
        "time_semantics": (
            "source publication time and CourtVision capture time"
            if result.get("published_date")
            else "CourtVision capture time; source publication time unavailable"
        ),
    }


def select_public_market_reference(web_context: dict[str, Any] | None) -> dict[str, Any] | None:
    """Select one complete source record; never merge prices across pages."""
    candidates = []
    for record in (web_context or {}).get("records") or []:
        market = record.get("market_reference")
        if record.get("category") != "market" or not market or market.get("status") != "available":
            continue
        candidates.append((
            int(market.get("source_tier") or 99),
            -float(record.get("score") or 0),
            str(market.get("source_url") or ""),
            str(record.get("id") or ""),
            record,
        ))
    if not candidates:
        return None
    record = min(candidates)[-1]
    return {**record["market_reference"], "evidence_id": record["id"]}


class PregameWebSearch:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        transport=None,
        replay_transport=None,
        local_transport: FreeSearchMCPTransport | None = None,
    ):
        self.root = Path(root or BASE / "outputs/web_search")
        self.transport = transport
        self.replay_transport = replay_transport
        self.local_transport = local_transport or FreeSearchMCPTransport()
        from .source_body_fetch import SourceBodyFetcher
        self.body_fetcher = SourceBodyFetcher(self.local_transport) if isinstance(self.local_transport,FreeSearchMCPTransport) else None

    def fetch_bodies(self,urls,*,budget_seconds=60):
        if self.body_fetcher is not None:
            return self.body_fetcher.fetch_many(urls,budget_seconds=budget_seconds)
        return self.local_transport.fetch_many(urls)

    @staticmethod
    def _tavily_transport(payload: dict[str, Any]) -> dict[str, Any]:
        key = os.environ.get("TAVILY_API_KEY", "")
        if not key:
            raise ValueError("web_search_api_key_missing")
        from .cost_ledger import active
        body = json.dumps({"api_key": key, **payload, **({'include_usage':True}if active()else{})}).encode("utf-8")
        request = urllib.request.Request(
            "https://api.tavily.com/search",
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "25th-second/1.0",
            },
            method="POST",
        )
        from .cost_ledger import component,begin_call,finish_call
        with component('Research Search'):
            entry=begin_call('Tavily',None,detail={'operation':'search','search_depth':payload.get('search_depth'),'unit_price':'account_plan_not_verified'})
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    result=json.loads(response.read().decode("utf-8"))
            except Exception as error:
                finish_call(entry,model=None,usage=None,success=False,error_type=type(error).__name__);raise
            finish_call(entry,model=None,usage=result.get('usage'),success=True)
            return result

    @staticmethod
    def _date(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _has_precise_zoned_time(value: str | None) -> bool:
        if not value or not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None and (
            "T" in value or " " in value
        )

    @staticmethod
    def _team_name(game: dict[str, Any], side: str) -> str:
        return str(
            game.get(f"{side}_team")
            or game.get(f"{side}_team_name")
            or game.get(f"{side}_team_id")
        )

    def queries(
        self,
        game: dict[str, Any],
        categories: set[str] | None = None,
        focus: tuple[str, str] | None = None,
        analysis_questions: list[dict[str, Any]] | None = None,
    ) -> list[tuple[str, str]]:
        away = self._team_name(game, "away")
        home = self._team_name(game, "home")
        if game.get("registered_game") and game.get("status") == "scheduled":
            from .agent import TEAM_NAMES
            from zoneinfo import ZoneInfo
            date = self._date(game["scheduled_tipoff"]).astimezone(ZoneInfo("America/New_York")).date().isoformat()
            year = int(game.get("season_start") or date[:4])
            phase = "preseason" if game.get("game_type") == "preseason" else "pregame"
            en = f"{away} {home} {year}-{str(year+1)[-2:]} {phase} {date}"
            zh = f"{TEAM_NAMES[game['away_team_id']]} {TEAM_NAMES[game['home_team_id']]} {year}-{year+1} {date} {'季前赛' if phase=='preseason' else '赛前'}"
            topics = {"availability": ("injury status minutes restriction practice update", "伤病 出战 缺阵 限时 训练"),
                "lineup": ("projected confirmed starters rotation coach comments", "预计首发 确认首发 轮换 教练采访"),
                "roster_news": ("current roster personnel changes training camp", "当前名单 人员变动 训练营"),
                "game_story": ("preview recent coach interview matchup", "赛前看点 教练采访 对位"),
                "market": ("odds spread total", "让分 总分 盘口")}
            if phase == "preseason":
                topics.update(availability=("preseason core players playing tonight expected minutes coach preseason usage plan", "季前赛 核心出场 预计分钟 教练计划"),
                    lineup=("preseason projected starters training camp depth chart rotation bench competition", "季前赛 预计首发 训练营 轮换 替补竞争"),
                    roster_news=("offseason major trades acquisitions new core training camp current roster", "休赛期 重大交易 新援 核心组合 当前名单"),
                    game_story=("preseason coach interview practice experimental lineups frontcourt matchup", "季前赛 教练采访 训练 试阵容 对位"))
            rows = [(category, f"{prefix} {words}") for category, terms in topics.items()
                    if categories is None or category in categories
                    for prefix, words in ((en, terms[0]), (zh, terms[1]))]
            if categories is None or "roster_news" in categories:
                rows.extend([("roster_news", f"{zh} site:zhibo8.com 伤病 名单 教练"),
                             ("roster_news", f"{zh} site:hupu.com 伤病 名单 教练")])
                if phase == "preseason":
                    rows.extend(("roster_news", f"{team} {year} offseason acquired trade current roster training camp")
                                for team in (away, home))
                    rows.extend(("roster_news", f"{team} official current roster {year}-{str(year+1)[-2:]}")
                                for team in (away, home))
            if focus and focus[0] in (categories or ALLOWED_FOCUS_CATEGORIES):
                rows.append((focus[0], f"{en} {focus[1]}"))
            for question in analysis_questions or []:
                category = str(question.get("category") or "")
                text = " ".join(str(question.get("question") or "").split())
                if text and category in ALLOWED_FOCUS_CATEGORIES and (categories is None or category in categories):
                    rows.append((category, f"{en} {text[:220]}"))
            return rows
        matchup = f"{away} vs {home} NBA"
        raw_tipoff = str(game.get("scheduled_tipoff") or "")
        game_date = raw_tipoff[:10]
        try:
            natural_date = datetime.fromisoformat(raw_tipoff.replace("Z", "+00:00")).strftime("%B %d %Y")
        except ValueError:
            natural_date = game_date
        away_slug = re.sub(r"[^a-z0-9]+", "-", away.casefold()).strip("-")
        candidates = [
            ("availability", f"{matchup} official injury report player status"),
            ("lineup", f"{matchup} confirmed projected starting lineup"),
            ("roster_news", f"{away} {home} offseason roster changes coach signing trade"),
            (
                "game_story",
                f"{away} {home} NBA previous meeting playoffs {game_date[:4]}",
            ),
            (
                "game_story",
                f"{away} {home} {game_date[:4]} playoff series takeaways how won defense shooting rebounds NBA",
            ),
            (
                "game_story",
                f"{home} {game_date[:4]} NBA Champions",
            ),
            (
                "game_story",
                f"{away} {game_date[:4]} NBA Champions",
            ),
            ("market", f"{away} at {home} {game_date} NBA odds spread total sportsbook"),
            (
                "market",
                f"site:sports.betmgm.com/en/blog/team/{away_slug} {away} {home} "
                f"{natural_date} Odds Tonight spread total",
            ),
        ]
        if focus and focus[0] in ALLOWED_FOCUS_CATEGORIES and focus[1].strip():
            candidates.append((
                focus[0],
                f"{away} {home} {game_date[:4]} {focus[1].strip()} NBA",
            ))
        for question in analysis_questions or []:
            category = str(question.get("category") or "")
            text = " ".join(str(question.get("question") or "").split())
            if text and category in ALLOWED_FOCUS_CATEGORIES and (categories is None or category in categories):
                candidates.append((category, f"{away} {home} {game_date[:4]} {text[:220]} NBA"))
        return [item for item in candidates if categories is None or item[0] in categories]

    def _batch_responses(
        self,
        payloads: list[dict[str, Any]],
        *,
        replay: bool = False,
    ) -> tuple[list[dict[str, Any] | None], list[str], str | None]:
        if replay:
            if self.replay_transport is None:
                return [None] * len(payloads), ["pregame_archive"] * len(payloads), "historical_source_archive_unavailable"
            try:
                return (
                    [self.replay_transport(payload) for payload in payloads],
                    ["pregame_archive"] * len(payloads),
                    None,
                )
            except (OSError, ValueError, TimeoutError) as exc:
                return [None] * len(payloads), ["pregame_archive"] * len(payloads), type(exc).__name__
        if self.transport is not None:
            return [self.transport(payload) for payload in payloads], ["custom"] * len(payloads), None
        try:
            responses = self.local_transport.search_many(payloads)
            return responses, ["free-search-mcp"] * len(payloads), None
        except (FreeSearchMCPError, OSError, ValueError) as exc:
            if not os.environ.get("TAVILY_API_KEY"):
                return [None] * len(payloads), ["free-search-mcp"] * len(payloads), type(exc).__name__
            responses = [self._tavily_transport(payload) for payload in payloads]
            return responses, ["tavily"] * len(payloads), type(exc).__name__

    @staticmethod
    def _story_is_relevant(result: dict[str, Any], game: dict[str, Any]) -> bool:
        url = str(result.get("url") or "")
        if _host(url) not in {"nba.com", "api-hub.nba.com"} or "/watch/video/" in urlparse(url).path:
            return False
        title = str(result.get("title") or "")
        content = str(result.get("content") or "")
        combined = f"{title}\n{content}"
        away = _team_aliases(game, "away")
        home = _team_aliases(game, "home")
        if not (_mentions(combined, away) or _mentions(combined, home)):
            return False
        direct_game = (_mentions(title, away) and _mentions(title, home)) or (
            _mentions(content, away) and _mentions(content, home)
            and bool(re.search(r"semifinal|series|sweep|defeat|beat|\bwon\b|\bwin\b|\b\d{2,3}\s*[-–]\s*\d{2,3}\b", content, re.IGNORECASE))
        )
        title_context = bool(re.search(r"champion|season review|finals?", title, re.IGNORECASE))
        championship = title_context and bool(re.search(
            r"champion|title|finals?|won|win|victory",
            content,
            re.IGNORECASE,
        ))
        return direct_game or championship

    @classmethod
    def _story_without_date_is_eligible(
        cls,
        result: dict[str, Any],
        game: dict[str, Any],
        *,
        captured_dt: datetime | None,
        cutoff_dt: datetime,
        replay: bool,
    ) -> bool:
        if replay or captured_dt is None or captured_dt > cutoff_dt + timedelta(seconds=120):
            return False
        return cls._story_is_relevant(result, game)

    def _prior_discovery_urls(self, game, cutoff_dt, categories, *, limit=8):
        """Retry URLs from this game's recent discovery; no old claim becomes evidence."""
        rows, seen = [], set()
        paths = sorted(self.root.glob("*/evidence.json"), key=lambda p: p.parent.name, reverse=True)[:80]
        for path in paths:
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
                at = self._date(previous.get("captured_at"))
                if (previous.get("replay") or previous.get("game_id") != game.get("game_id")
                        or at is None or not cutoff_dt - timedelta(hours=12) <= at <= cutoff_dt):
                    continue
                for item in previous.get("records", []):
                    url = item.get("url", "")
                    if (item.get("category") not in categories or item.get("discovery_only") is not True
                            or item.get("eligible_for_fact") is not False or url in seen
                            or not item.get("title") or not _public_discovery_url(url)):
                        continue
                    seen.add(url)
                    row = {key: item[key] for key in ("category", "url", "title", "domain", "snippet",
                        "published_at", "captured_at", "provider_temporal_metadata") if key in item}
                    row.update(id=content_hash({"category": item["category"], "url": url,
                        "cutoff": cutoff_dt.isoformat(), "mode": "prior_discovery_url_retry"}),
                        search_provider="prior_discovery_url_retry", eligible_for_fact=False,
                        discovery_only=True, verification_status="prior_url_requires_fresh_fetch",
                        prior_discovery_at=previous["captured_at"], discovery_retried_at=now())
                    rows.append(row)
            except (OSError, ValueError, TypeError, KeyError):
                continue
        # Prefer established sports providers for rediscovery, then cover requested categories.
        # Authority here selects URLs to fetch; it never accepts a claim by itself.
        rows.sort(key=lambda row: not _trusted(row["url"]))
        chosen = []
        for category in sorted(categories):
            candidate = next((row for row in rows if row["category"] == category and _trusted(row["url"])), None)
            if candidate is not None and len(chosen) < limit:
                chosen.append(candidate)
        chosen_ids = {row["id"] for row in chosen}
        return (chosen + [row for row in rows if row["id"] not in chosen_ids])[:limit]

    def search(
        self,
        game: dict[str, Any],
        cutoff: str,
        *,
        replay: bool = False,
        categories: set[str] | None = None,
        focus: tuple[str, str] | None = None,
        analysis_questions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        cutoff_dt = self._date(cutoff)
        if cutoff_dt is None:
            raise ValueError("web_search_invalid_cutoff")
        records: list[dict[str, Any]] = []
        seen_records: set[tuple[str, str]] = set()
        trace: list[dict[str, Any]] = []
        response_hashes: list[str] = []
        captured_at = now()
        captured_dt = self._date(captured_at)
        query_rows = self.queries(game, categories, focus, analysis_questions)
        payloads: list[dict[str, Any]] = []
        for category, query in query_rows:
            is_market = category == "market"
            payload: dict[str, Any] = {
                "query": query,
                "topic": "general" if is_market else "news",
                "search_depth": "advanced",
                "max_results": 6,
                "include_answer": False,
                "include_raw_content": False,
                "include_domains": list(MARKET_DOMAINS if is_market else TRUSTED_DOMAINS),
            }
            if game.get("registered_game") and not replay and not is_market:
                payload.pop("include_domains", None)  # Broad discovery, not broad fact acceptance.
            if replay:
                payload["end_date"] = cutoff_dt.date().isoformat()
            payloads.append(payload)

        responses, providers, batch_error = self._batch_responses(payloads, replay=replay)
        discovery = bool(game.get("registered_game") and not replay)
        candidates = []
        for (category, query), payload, response, provider in zip(
            query_rows, payloads, responses, providers, strict=True
        ):
            is_market = category == "market"
            attempts: list[dict[str, Any]] = []

            def accept(current: dict[str, Any], current_provider: str) -> tuple[int, int]:
                raw = json.dumps(current, ensure_ascii=False, sort_keys=True).encode("utf-8")
                response_hashes.append(hashlib.sha256(raw).hexdigest())
                accepted_count = 0
                rejected_count = 0
                away_aliases = _team_aliases(game, "away")
                home_aliases = _team_aliases(game, "home")
                live_capture_ok = (
                    not replay
                    and captured_dt is not None
                    and captured_dt <= cutoff_dt + timedelta(seconds=120)
                )
                for result in current.get("results") or []:
                    url = str(result.get("url") or "")
                    title = str(result.get("title") or "")
                    content = str(result.get("content") or "")
                    combined = f"{title}\n{content}"
                    if discovery:
                        from .agent import TEAM_NAMES
                        mentions_team = (_mentions(combined, away_aliases) or _mentions(combined, home_aliases)
                            or any(TEAM_NAMES[game[f'{side}_team_id']] in combined for side in ('home','away')))
                        if _public_discovery_url(url) and title and mentions_team:
                            ident = (category, url)
                            item = {"id": content_hash({"category": category, "url": url, "cutoff": cutoff}),
                                "category": category, "url": url, "title": title, "domain": _host(url),
                                "snippet": content[:1600], "published_at": result.get("published_date"),
                                "captured_at": now(), "search_provider": current_provider,
                                "eligible_for_fact": False, "discovery_only": True,
                                "verification_status": "search_snippet_requires_fetch", "discovered_via": query}
                            item["provider_temporal_metadata"]=result.get("provider_temporal_metadata") or {}
                            candidates.append(item)
                            if ident not in seen_records:
                                records.append(item); seen_records.add(ident); accepted_count += 1
                        else:
                            candidates.append({"category": category, "url": url, "title": title,
                                "snippet": content[:1600], "rejection_reason": "identity_or_url_unverified", "discovered_via": query})
                            rejected_count += 1
                        continue
                    raw_published = result.get("published_date")
                    if replay and raw_published and not self._has_precise_zoned_time(str(raw_published)):
                        rejected_count += 1
                        continue
                    published = self._date(raw_published)
                    result_captured_at = (
                        self._date(result.get("captured_at")) if replay else captured_dt
                    )
                    if replay and (
                        not self._has_precise_zoned_time(str(result.get("captured_at") or ""))
                        or result_captured_at is None
                        or result_captured_at > cutoff_dt
                    ):
                        rejected_count += 1
                        continue
                    transaction_date = (
                        _official_transaction_date(result) if category == "game_story" else None
                    )
                    if transaction_date is not None and published is None:
                        published = transaction_date
                    record_category = "roster_news" if transaction_date is not None else category
                    trusted = _market_domain(url) is not None if is_market else _trusted(url)
                    mentions_team = _mentions(combined, away_aliases) or _mentions(combined, home_aliases)
                    if not url or not title or not trusted or not mentions_team:
                        rejected_count += 1
                        continue
                    if category == "game_story" and transaction_date is None and not self._story_is_relevant(result, game):
                        rejected_count += 1
                        continue
                    if published is not None and published > cutoff_dt:
                        rejected_count += 1
                        continue
                    if transaction_date is not None and transaction_date.date() >= cutoff_dt.date():
                        rejected_count += 1
                        continue
                    if published is None:
                        undated_story = category == "game_story" and self._story_without_date_is_eligible(
                            result,
                            game,
                            captured_dt=captured_dt,
                            cutoff_dt=cutoff_dt,
                            replay=replay,
                        )
                        undated_market = is_market and live_capture_ok
                        if not (undated_story or undated_market):
                            rejected_count += 1
                            continue
                    market_reference = (
                        parse_public_market_reference(
                            result,
                            game,
                            captured_at=(
                                result_captured_at.isoformat() if replay and result_captured_at
                                else captured_at
                            ),
                        )
                        if is_market
                        else None
                    )
                    if is_market and market_reference is None:
                        rejected_count += 1
                        continue
                    identity = (record_category, url)
                    if identity in seen_records:
                        rejected_count += 1
                        continue
                    record = {
                        "id": content_hash(
                            {
                                "category": record_category,
                                "url": url,
                                "title": title,
                                "published_at": published.isoformat() if published else None,
                                "captured_at": (
                                    result_captured_at.isoformat() if replay and result_captured_at
                                    else captured_at
                                ),
                                "cutoff": cutoff,
                            }
                        ),
                        "category": record_category,
                        "title": title[:300],
                        "url": url,
                        "domain": _host(url),
                        "published_at": published.isoformat() if published else None,
                        "publication_time_semantics": "date_only" if transaction_date is not None else "source_timestamp_or_unavailable",
                        "captured_at": (
                            result_captured_at.isoformat() if replay and result_captured_at
                            else captured_at
                        ),
                        "snippet": content[:900],
                        "score": result.get("score"),
                        "search_provider": current_provider,
                        "eligible_for_fact": True,
                    }
                    if market_reference is not None:
                        market_reference["published_at"] = published.isoformat() if published else None
                        record["market_reference"] = market_reference
                    records.append(record)
                    seen_records.add(identity)
                    accepted_count += 1
                return accepted_count, rejected_count

            accepted = 0
            rejected = 0
            if response is not None:
                accepted, rejected = accept(response, provider)
                attempts.append({"provider": provider, "accepted": accepted, "rejected": rejected})
            else:
                attempts.append(
                    {
                        "provider": provider,
                        "accepted": 0,
                        "rejected": 0,
                        "error": batch_error,
                    }
                )

            if (
                accepted == 0
                and self.transport is None
                and provider == "free-search-mcp"
                and os.environ.get("TAVILY_API_KEY")
            ):
                try:
                    fallback = self._tavily_transport(payload)
                    fallback_accepted, fallback_rejected = accept(fallback, "tavily")
                    accepted += fallback_accepted
                    rejected += fallback_rejected
                    attempts.append(
                        {
                            "provider": "tavily",
                            "accepted": fallback_accepted,
                            "rejected": fallback_rejected,
                        }
                    )
                except Exception as exc:
                    attempts.append(
                        {
                            "provider": "tavily",
                            "accepted": 0,
                            "rejected": 0,
                            "error": type(exc).__name__,
                        }
                    )

            trace.append(
                {
                    "category": category,
                    "query": query,
                    "accepted": accepted,
                    "rejected": rejected,
                    "attempts": attempts,
                    "accepted_semantics": "retained_discovery_candidates" if discovery else "accepted_evidence",
                }
            )

        if discovery and not records:
            recovered = self._prior_discovery_urls(game, cutoff_dt, {category for category, _ in query_rows if category != "market"})
            if recovered:
                records.extend(recovered)
                candidates.extend(recovered)
                trace.append({"provider": "prior_discovery_url_retry", "accepted": len(recovered),
                    "accepted_semantics": "discovery_urls_only_fresh_fetch_required"})

        artifact = {
            "status": "available" if records else "unavailable",
            "game_id": game.get("game_id"),
            "cutoff": cutoff,
            "replay": replay,
            "search_run_at": captured_at,
            "captured_at": None if replay else captured_at,
            "records": records,
            "trace": trace,
            "raw_response_sha256": response_hashes,
            "parser_version": "pregame-web-v5-historical-archive",
            "policy": (
                "historical archive transport only; each accepted record must include a precise archive capture "
                "at or before cutoff; current search and current page fetch are disabled"
                if replay
                else "local free-search-mcp first, Tavily fallback; trusted domains; publication timestamp at or before cutoff, "
                     "or bounded current capture for official game-story and public-market evidence; "
                     "public market reference is display-only and never a numeric prediction input"
            ),
        }
        folder = self.root / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "_"
            + content_hash(artifact)[:8]
        )
        folder.mkdir(parents=True, exist_ok=False)
        save(folder / "evidence.json", artifact)
        if discovery:
            artifact["live_registered"] = True
            artifact["game"] = dict(game)
            artifact["policy"] = "broad discovery only; no snippet is a fact; fetched source identity/time/provenance verification is required"
            save(folder / "discovery_receipt.json", {"queried_at": captured_at, "cutoff": cutoff,
                "queries": payloads, "providers": providers, "candidates": candidates,
                "raw_responses": responses, "roles": "discovery only; fetched verification required"})
            artifact["discovery_receipt_path"] = str(folder / "discovery_receipt.json")
        return artifact
