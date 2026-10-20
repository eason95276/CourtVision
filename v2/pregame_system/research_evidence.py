"""Quote-bound research evidence; acquisition receipts are not basketball facts.

Live acquisition may finish after the requested publication cutoff. Both clocks
are retained and the caller must freeze inputs after acquisition. Historical
replay never uses this live pipeline.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .storage import content_hash, now
from .web_search import _host, _mentions, _team_aliases, _public_discovery_url, _mentions_game_date
from .source_temporal_metadata import extract_temporal_metadata


CLAIM_CLASSES = {
    "personnel_change": "BACKGROUND", "season_background": "BACKGROUND",
    "coach_change": "BACKGROUND", "role_background": "BACKGROUND",
    "long_term_injury": "BACKGROUND",
    "current_roster": "CURRENT", "projected_starters": "CURRENT",
    "practice_update": "CURRENT", "coach_comments": "CURRENT",
    "rotation_update": "CURRENT", "game_news": "CURRENT",
    "availability": "LIVE", "confirmed_starter": "LIVE",
    "minutes_restriction": "LIVE", "eligibility": "LIVE",
    "market_reference": "LIVE",
}
CATEGORIES = {
    "personnel_change": "roster_news", "season_background": "game_story",
    "coach_change": "roster_news", "role_background": "game_story",
    "long_term_injury": "roster_news", "current_roster": "roster_news",
    "projected_starters": "lineup", "practice_update": "availability",
    "coach_comments": "game_story", "rotation_update": "lineup",
    "game_news": "game_story", "availability": "availability",
    "confirmed_starter": "lineup", "minutes_restriction": "availability",
    "eligibility": "availability", "market_reference": "market",
}
# Source competence, not a fixed NBA > ESPN > Chinese ranking. Unrecognized
# publishers remain discovery; accepted facts come from a fetched original or
# independently fetched accountable publication, not the linking community.
PROFESSIONAL_DOMAINS = {
    "espn.com", "apnews.com", "reuters.com", "cbssports.com", "sports.yahoo.com",
    "sportsnet.ca", "tsn.ca", "nbcsports.com", "si.com", "theathletic.com",
    "nytimes.com", "miamiherald.com", "thestar.com", "sun-sentinel.com",
    "celticsblog.com", "heavy.com", "nesn.com", "detroitnews.com", "freep.com",
    "news.zhibo8.com", "news.zhibo8.cc", "news.zhibo8.net", "sports.sina.com.cn",
    "voice.hupu.com", "nba.hupu.com", "qiumiwu.com",
    "rotowire.com", "rotoballer.com", "basketballmonster.com", "fantasypros.com",
}
OFFICIAL_TEAM_DOMAINS = {"raptors.com"}
VOLATILE_ASSERTION = re.compile(
    r"\b(?:available|questionable|doubtful|probable|ruled out|will (?:play|start)|"
    r"confirmed starter|late scratch|minutes restriction|minutes limit)\b|"
    r"确认首发|确定出战|确认缺阵|出战成疑|分钟限制|限时出场", re.I)
CURRENT_ASSERTION = re.compile(r"\b(?:projected starters?|practice today|training today)\b|预计首发|今日训练", re.I)
PREGAME_GAME_CONTEXT = re.compile(
    r"\b(?:pre[- ]?season|game preview|pre[- ]?game|pregame|tonight|today|tomorrow|"
    r"open(?:s|ing)? their schedule|begin(?:s|ning)? their schedule)\b|"
    r"季前赛|赛前前瞻|今晚|今天|明天|本场",
    re.I,
)


def instant(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone_missing")
    return parsed.astimezone(timezone.utc)


def source_role(url):
    host = _host(url)
    if host == "nba.com" or host.endswith(".nba.com") or host in OFFICIAL_TEAM_DOMAINS:
        return "primary"
    if any(host == d or host.endswith("." + d) for d in PROFESSIONAL_DOMAINS):
        return "professional"
    return "secondary_discovery"


def publication_bounds(value):
    """Conservative bounds; a date without zone is not fictitious UTC midnight."""
    if not value:
        return {"precision": "unknown", "earliest": None, "latest": None}
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None and ("T" in raw or " " in raw):
            stamp = parsed.astimezone(timezone.utc)
            return {"precision": "timestamp", "earliest": stamp, "latest": stamp}
        # Unknown timezone is bounded over UTC+14 .. UTC-12. Date-level proof
        # requires the entire possible publication day to precede cutoff.
        midnight = datetime.combine(parsed.date(), datetime.min.time(), timezone.utc)
        return {"precision": "date", "earliest": midnight - timedelta(hours=14),
                "latest": midnight + timedelta(days=1, hours=12)}
    except ValueError:
        return {"precision": "unknown", "earliest": None, "latest": None}


def _publication(item, candidate):
    for value, origin in ((item.get("published_date"), "fetched_metadata"),
                          (item.get("date_published"), "fetched_metadata"),
                          (candidate.get("published_at"), "search_metadata")):
        if publication_bounds(value)["precision"] != "unknown":
            return str(value), origin
    match = re.search(r"(?:/|[-_])(20\d{2})[-/](\d{2})[-/](\d{2})(?:/|[-_.])", candidate["url"])
    if match:
        value = "-".join(match.groups())
        if publication_bounds(value)["precision"] != "unknown":
            return value, "url_date"
    return None, "unavailable"


def _game_specific_pregame_capture(source, quote, game):
    """Recognize a directly game-anchored pregame article without inventing its publication time.

    This is deliberately unavailable to high-volatility LIVE claims. It says only
    that an accountable source's captured page discussed this scheduled matchup
    before tipoff; it does not turn captured_at into published_at.
    """
    captured = source.get("fetched_at") or source.get("captured_at")
    try:
        captured_at = instant(captured)
        tipoff = instant(game.get("scheduled_tipoff"))
    except (TypeError, ValueError):
        return False
    if captured_at >= tipoff or source.get("source_role") not in {"primary", "professional"}:
        return False

    title = str(source.get("title") or "")
    body = str(source.get("body") or "")
    url = str(source.get("url") or "")
    page_text = "\n".join((title, body, quote, url))
    if not all(_mentions(page_text, _team_aliases(game, side)) for side in ("home", "away")):
        return False
    if not PREGAME_GAME_CONTEXT.search(page_text):
        return False

    if _mentions_game_date(page_text, game, captured_at=str(captured), allow_yearless=True):
        return True

    # Some article publishers put the publication date in the URL path rather
    # than the page metadata. Treat it only as a date-level game anchor here.
    match = re.search(r"/(20\d{2})/(\d{2})/(\d{2})(?:/|[-_.]|$)", url)
    if not match:
        return False
    try:
        path_date = datetime(*(int(part) for part in match.groups())).date()
        tipoff_date = tipoff.date()
    except ValueError:
        return False
    return abs((path_date - tipoff_date).days) <= 1


def _normal(text):
    # Compare rendered text, not Markdown link/emphasis syntax. This is exact
    # normalization, not fuzzy matching or acceptance of paraphrased numbers.
    rendered = re.sub(r"\[([^\]]+)\]\(https?://[^\s)]+\)", r"\1", str(text or ""))
    rendered = re.sub(r"[*`]", "", rendered)
    return " ".join(rendered.split())


class ResearchEvidencePipeline:
    def __init__(self, game, cutoff, *, stage="EARLY_PREVIEW", analysis_questions=None):
        self.game, self.cutoff, self.stage = game, cutoff, stage
        self.analysis_questions = list(analysis_questions or [])
        self.analysis_question_ids = {str(q.get("id")) for q in self.analysis_questions if q.get("id")}
        self.sources = {}
        self.claims = []
        self.decisions = []

    def add_source(self, candidate, fetched):
        url = candidate["url"]
        if url in self.sources:
            return self.sources[url]
        body = str(fetched.get("content") or "").strip()
        title = str(fetched.get("title") or candidate.get("title") or "")
        captured = fetched.get("captured_at") or fetched.get("fetched_at") or now()
        provider={**(candidate.get("provider_temporal_metadata") or {}),**{k:v for k,v in fetched.items()if v is not None}}
        if not provider.get("published_date") and candidate.get("published_at"):
            provider["published_date"]=candidate["published_at"]
        temporal=fetched.get("temporal_metadata") or extract_temporal_metadata(url,provider=provider,
            body=body,title=title,fetched_at=captured)
        if not temporal.get("published_at") or not temporal.get("updated_at"):
            search_provider=dict(candidate.get("provider_temporal_metadata") or {})
            if candidate.get("published_at"):search_provider.setdefault("published_at",candidate["published_at"])
            search_time=extract_temporal_metadata(url,provider=search_provider,title=title,fetched_at=captured)
            temporal=dict(temporal)
            for field in ("published_at","updated_at"):
                if not temporal.get(field) and search_time.get(field):
                    for key in (field,field+"_precision",field+"_origin"):temporal[key]=search_time[key]
                    temporal[field+"_origin"]="search_"+search_time[field+"_origin"]
            temporal["temporal_candidates"]=(temporal.get("temporal_candidates") or [])+search_time["temporal_candidates"]
        published=temporal.get("published_at")
        origin=temporal.get("published_at_origin") or "unavailable"
        bounds = publication_bounds(published)
        text = title + "\n" + body
        relevant = any(_mentions(text, _team_aliases(self.game, side)) for side in ("home", "away"))
        returned_url = fetched.get("url")
        reason = None
        game_context_capture = False
        if not _public_discovery_url(url): reason = "unsafe_source_url"
        elif returned_url and returned_url.rstrip("/") != url.rstrip("/"): reason = "fetch_url_identity_mismatch"
        elif len(body) < 100 or re.search(r"^\s*(?:JavaScript is disabled|Access Denied|Just a moment)", body, re.I):
            reason = "full_source_text_unavailable"
        elif not relevant: reason = "fetched_team_identity_unverified"
        elif source_role(url) == "secondary_discovery": reason = "original_or_independent_verification_unavailable"
        links = list(dict.fromkeys(re.findall(r'https://[^\s\)\]<>"\']+', body)))
        source = {
            "id": content_hash({"url": url, "body": content_hash(body), "captured_at": captured}),
            "url": url, "title": title, "body": body, "body_sha256": content_hash(body),
            "raw_response_sha256": content_hash(fetched), "content_chars": len(body),
            "published_at": published, "publication_precision": bounds["precision"],
            "publication_time_source": origin,
            "publication_earliest": bounds["earliest"].isoformat() if bounds["earliest"] else None,
            "publication_latest": bounds["latest"].isoformat() if bounds["latest"] else None,
            "captured_at": captured, "source_role": source_role(url),
            "updated_at":temporal.get("updated_at"),"observed_at":temporal.get("observed_at"),
            "fetched_at":captured,"temporal_metadata":temporal,
            "fetch_provider": fetched.get("provider") or "free-search-mcp",
            "fetch_method": fetched.get("method"), "fetch_errors": fetched.get("errors"),
            "fetch_attempts": fetched.get("fetch_attempts"), "fetch_diagnostic": fetched.get("diagnostic"),
            "fetch_status": "readable" if len(body) >= 100 else "failed",
            "qualification_reason": reason, "original_source_links": links,
            "discovered_via": candidate.get("discovered_via"),
            "discovery_categories": candidate.get("discovery_categories", [candidate.get("category")]),
            "analysis_question_ids": [str(qid) for qid in candidate.get("analysis_question_ids", [])
                                      if str(qid) in self.analysis_question_ids],
        }
        self.sources[url] = source
        return source

    def validate_claim(self, proposed):
        source = self.sources.get(str(proposed.get("url") or ""))
        kind = str(proposed.get("claim_type") or "")
        question_ids = proposed.get("analysis_question_ids") or []
        temporal_class = CLAIM_CLASSES.get(kind)
        # A source's projection about workload is not a confirmed LIVE
        # restriction. Keep confirmation strict, but let a dated/captured
        # game-specific preview contribute it as a projection.
        if kind == "minutes_restriction" and proposed.get("assertion_kind") == "reported_projection":
            temporal_class = "CURRENT"
        quote = _normal(proposed.get("quote"))
        reason = None
        confidence = "insufficient"
        temporal_basis = None
        cutoff = instant(self.cutoff)
        if not source: reason = "claim_source_not_fetched"
        elif not isinstance(question_ids, list) or any(str(qid) not in self.analysis_question_ids for qid in question_ids):
            reason = "claim_analysis_question_id_unknown"
        elif source["qualification_reason"]: reason = source["qualification_reason"]
        elif not temporal_class: reason = "unknown_claim_type"
        elif kind == "market_reference": reason = "market_claim_requires_existing_parser"
        elif len(quote) < 15 or len(quote) > 1600 or quote not in _normal(source["body"]):
            reason = "claim_quote_not_in_fetched_body"
        elif proposed.get("team_id") not in {self.game.get("home_team_id"), self.game.get("away_team_id")}:
            reason = "claim_team_identity_mismatch"
        elif proposed.get("assertion_kind") not in {"confirmed_fact", "reported_projection", "source_opinion"}:
            reason = "claim_assertion_kind_missing"
        elif kind == "projected_starters" and proposed.get("assertion_kind") != "reported_projection":
            reason = "projected_starters_not_confirmed_fact"
        elif temporal_class != "LIVE" and VOLATILE_ASSERTION.search(quote):
            reason = "high_volatility_claim_cannot_use_weaker_time_class"
        elif temporal_class == "BACKGROUND" and CURRENT_ASSERTION.search(quote):
            reason = "current_claim_cannot_use_background_time_class"
        else:
            team_side = next(s for s in ("home", "away") if self.game.get(s + "_team_id") == proposed["team_id"])
            if not _mentions(source["title"] + "\n" + quote, _team_aliases(self.game, team_side)):
                reason = "claim_team_not_supported_by_excerpt_or_title"
            temporal_basis="published_at"
            basis_time=source["published_at"]
            if temporal_class in {"CURRENT","LIVE"} and source.get("updated_at"):
                temporal_basis="updated_at";basis_time=source["updated_at"]
            bounds = publication_bounds(basis_time)
            latest, earliest = bounds["latest"], bounds["earliest"]
            same_day_background=(temporal_class=="BACKGROUND" and bounds["precision"]=="date"
                and str(source["published_at"])[:10]<=cutoff.date().isoformat()
                and instant(source["fetched_at"])<instant(self.game["scheduled_tipoff"]))
            game_context_capture=(temporal_class=="CURRENT" and kind!="current_roster"
                and bounds["precision"] in {"unknown","date"}
                and not source.get("updated_at")
                and _game_specific_pregame_capture(source,quote,self.game))
            snapshot_kind=(source.get("temporal_metadata") or {}).get("page_kind")
            snapshot=(temporal_class=="CURRENT" and source["source_role"] in {"primary","professional"}
                and ((kind=="current_roster" and snapshot_kind=="current_roster_snapshot")
                  or(kind in {"projected_starters","rotation_update"} and snapshot_kind=="current_depth_snapshot")
                  or(kind=="game_news" and snapshot_kind=="current_status_snapshot")))
            if reason is None and latest and latest > cutoff and not same_day_background and not game_context_capture:
                reason = "publication_not_proven_before_cutoff"
            if reason is None and temporal_class == "BACKGROUND":
                year = int(self.game.get("season_start") or cutoff.year)
                # Draft-week personnel moves belong to the coming season's
                # offseason; other BACKGROUND and all CURRENT/LIVE clocks stay unchanged.
                season_floor = datetime(year, 6 if kind == "personnel_change" else 7, 1, tzinfo=timezone.utc)
                if not latest or not earliest: reason = "background_publication_date_unavailable"
                elif earliest < season_floor: reason = "previous_season_source_not_current_background"
                else: confidence = "sufficient_for_background"
            if reason is None and temporal_class == "CURRENT":
                max_age = timedelta(hours=24 if self.stage == "FINAL_PREGAME" else 72)
                if snapshot and source.get("observed_at") and not latest:
                    observed=instant(source["observed_at"])
                    if observed>=instant(self.game["scheduled_tipoff"]):reason="snapshot_observed_not_pregame"
                    elif cutoff-observed>max_age:reason="stale_current_claim"
                    else:
                        confidence="observed_current_snapshot";temporal_basis="observed_at"
                elif game_context_capture:
                    confidence="game_specific_pregame_capture";temporal_basis="captured_game_context"
                elif not latest or not earliest: reason = "current_publication_time_unavailable"
                elif cutoff - earliest > max_age: reason = "stale_current_claim"
                else: confidence = "sufficient_for_current_date" if bounds["precision"] == "date" else "precise_current"
            if reason is None and temporal_class == "LIVE":
                max_age = timedelta(hours=12 if self.stage == "FINAL_PREGAME" else 36)
                game_reference = _normal(proposed.get("game_reference"))
                if bounds["precision"] != "timestamp": reason = "live_precise_zoned_publication_required"
                elif cutoff - earliest > max_age: reason = "stale_live_claim"
                elif not game_reference or game_reference not in _normal(source["body"] + "\n" + source["title"]):
                    reason = "live_game_reference_not_in_source"
                elif not _mentions_game_date(game_reference, self.game) and str(self.game.get("game_id", "")).removeprefix("espn:") not in game_reference:
                    reason = "live_exact_game_association_unverified"
                else: confidence = "precise_game_specific"
        accepted = reason is None
        decision = {**proposed, "accepted": accepted, "reason": reason,
                    "temporal_class": temporal_class, "temporal_confidence": confidence,
                    "temporal_basis":temporal_basis}
        self.decisions.append(decision)
        if accepted:
            ident = content_hash({"source_id": source["id"], "quote": quote, "claim_type": kind, "team_id": proposed["team_id"]})
            if not any(r["id"] == ident for r in self.claims):
                self.claims.append({
                    "id": ident, "source_id": source["id"], "url": source["url"], "title": source["title"],
                    "category": CATEGORIES[kind], "claim_type": kind, "temporal_class": temporal_class,
                    "temporal_scope": "current_season_background" if temporal_class == "BACKGROUND" else "current_pregame",
                    "assertion_kind": proposed["assertion_kind"], "team_id": proposed["team_id"],
                    "snippet": quote, "quote": quote, "published_at": source["published_at"],
                    "updated_at":source.get("updated_at"),"observed_at":source.get("observed_at"),
                    "fetched_at":source.get("fetched_at"),"temporal_basis":temporal_basis,
                    "temporal_metadata":source.get("temporal_metadata"),
                    "analysis_question_ids":[str(qid) for qid in question_ids],
                    "publication_precision": source["publication_precision"],
                    "publication_time_source": source["publication_time_source"],
                    "publication_earliest": source["publication_earliest"], "publication_latest": source["publication_latest"],
                    "captured_at": source["captured_at"], "temporal_confidence": confidence,
                    "eligible_for_fact": True, "discovery_only": False, "verification_status": "quote_bound_claim_verified",
                    "source_role": source["source_role"], "verified_by": source["url"],
                    "discovered_via": source["discovered_via"], "full_text_sha256": source["body_sha256"],
                    "raw_response_sha256": source["raw_response_sha256"],
                    "game_reference": proposed.get("game_reference") or None,
                    "claim_boundary": "This excerpt supports only its typed claim; background does not confirm this-game participation.",
                })
        return decision

    def summary(self, *, checked_at=None):
        checked_at = checked_at or now()
        coverage = {}
        for kind, temporal in CLAIM_CLASSES.items():
            relevant = [s["url"] for s in self.sources.values()
                        if CATEGORIES[kind] in s["discovery_categories"] or temporal == "BACKGROUND"]
            relevant = list(dict.fromkeys(relevant + [c["url"] for c in self.decisions
                if c.get("claim_type") == kind and c.get("url") in self.sources]))
            found = [c for c in self.claims if c["claim_type"] == kind]
            rejected = [c["reason"] for c in self.decisions if c.get("claim_type") == kind and not c["accepted"]]
            coverage[kind] = {"status": "FOUND" if found else "UNKNOWN", "evidence_ids": [c["id"] for c in found],
                "unknown_reason": None if found else (rejected[-1] if rejected else "not_confirmed_in_checked_sources"),
                "last_checked": checked_at, "sources_checked": relevant}
        usable = [c for c in self.claims if c["category"] != "market" and c["assertion_kind"] != "source_opinion"]
        # EARLY does not require game-day statuses which may not yet exist.
        # FINAL needs a fresh current/live fact; missing starters remain UNKNOWN.
        if self.stage == "FINAL_PREGAME":
            usable = [c for c in usable if c["temporal_class"] != "BACKGROUND"]
        sufficient = bool(usable)
        question_coverage = []
        for question in self.analysis_questions:
            question_id = str(question.get("id"))
            evidence_ids = [claim["id"] for claim in self.claims
                            if question_id in claim.get("analysis_question_ids", [])]
            question_coverage.append({"id": question_id, "priority": question.get("priority", "supporting"),
                "question": question.get("question"), "status": "covered" if evidence_ids else "uncovered",
                "evidence_ids": evidence_ids})
        core_questions = [q for q in question_coverage if q["priority"] == "core"]
        analysis_readiness = {"status": "not_requested" if not question_coverage else
                "covered" if all(q["status"] == "covered" for q in core_questions) else "uncovered",
            "questions": question_coverage,
            "uncovered_core_question_ids": [q["id"] for q in core_questions if q["status"] != "covered"]}
        reasons = Counter(c.get("reason") for c in self.decisions if not c["accepted"])
        source_reasons = Counter(s["qualification_reason"] for s in self.sources.values() if s["qualification_reason"])
        fact_readiness = {
            "status": "ready" if sufficient else "unavailable",
            "usable_claims": len(usable),
            "coverage": coverage,
        }
        return {"stage": self.stage, "sufficient": sufficient, "accepted_claims": len(self.claims),
            "usable_claims": len(usable), "coverage": coverage,
            "fact_readiness": fact_readiness,
            "analysis_readiness": analysis_readiness,
            "reason": None if sufficient else "minimum_research_not_met",
            "failure_detail": {"claim_rejections": dict(reasons), "source_failures": dict(source_reasons),
                "readable_sources": sum(s["qualification_reason"] is None for s in self.sources.values()),
                "extraction_empty": not self.decisions and bool(self.sources)}}


def rank_candidates(records, game, *, attempted=()):
    """Accountable source + relevance + recency + readability; no publisher ladder."""
    grouped = {}
    for r in records:
        url = str(r.get("url") or "")
        if url in attempted or not _public_discovery_url(url): continue
        if url not in grouped: grouped[url] = {**r, "discovery_categories": []}
        if r.get("category") not in grouped[url]["discovery_categories"]:
            grouped[url]["discovery_categories"].append(r.get("category"))
    def score(r):
        text = str(r.get("title") or "") + " " + str(r.get("snippet") or "")
        value = 4 if source_role(r["url"]) != "secondary_discovery" else 0
        value += sum(_mentions(text, _team_aliases(game, s)) for s in ("home", "away")) * 2
        value += bool(publication_bounds(r.get("published_at"))["latest"])
        value += bool(re.search(r"preview|training|practice|injury|roster|acquire|trade|sign|采访|训练|伤病", text, re.I))
        if re.search(r"/game/_/|/scoreboard|/nba/$|/forum|bbs\.", r["url"]): value -= 3
        return -value, r["url"]
    return sorted(grouped.values(), key=score)
