"""Bounded, unauthenticated HTTP access to ESPN's structured NBA endpoints."""
from __future__ import annotations

import gzip
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .espn_boxscore_normalizer import ESPN_SCOREBOARD_URL, ESPN_SUMMARY_URL, ESPN_TEAMS_URL


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
REFERER = "https://www.espn.com/nba/"
MAX_COMPRESSED_BYTES = 2_000_000
MAX_DECODED_BYTES = 8_000_000
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|set-cookie|token|secret|password)"
    r"(\s*[:=]\s*)([^\s,;\"']+)"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.hostname or "", parts.path, parts.query, ""))


def _safe_text(text: str, limit: int = 900) -> str:
    return _SECRET_RE.sub(r"\1\2<redacted>", text[:limit])


class _RedirectRecorder(HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.events.append({
            "status_code": int(code),
            "from_url": _safe_url(req.full_url),
            "to_url": _safe_url(newurl),
        })
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class ESPNResponse:
    payload: dict[str, Any]
    raw_body: bytes
    decoded_body: bytes
    report: dict[str, Any]


class ESPNSourceError(RuntimeError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__(str(report.get("classification") or "espn_source_error"))


def fetch_json(url: str, *, timeout_seconds: int = 20) -> ESPNResponse:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    request = Request(url, headers=headers, method="GET")
    redirect_recorder = _RedirectRecorder()
    opener = build_opener(redirect_recorder)
    started = _utc_now()
    response = None
    exception_type = None
    exception_message = None
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except HTTPError as exc:
        response = exc
        exception_type = type(exc).__name__
        exception_message = _safe_text(str(exc), 500)
    except Exception as exc:
        exception_type = type(exc).__name__
        exception_message = _safe_text(str(exc), 500)

    status = None
    response_headers: dict[str, str] = {}
    raw_body = b""
    final_url = None
    if response is not None:
        try:
            status = int(response.getcode())
        except Exception:
            pass
        try:
            final_url = _safe_url(response.geturl())
        except Exception:
            pass
        raw_headers = getattr(response, "headers", {}) or {}
        response_headers = {
            key: str(raw_headers[key])
            for key in ("Content-Type", "Content-Length", "Content-Encoding", "Date", "Retry-After", "Server")
            if raw_headers.get(key) is not None
        }
        try:
            raw_body = response.read(MAX_COMPRESSED_BYTES + 1)
        except Exception as exc:
            exception_type = exception_type or type(exc).__name__
            exception_message = exception_message or _safe_text(str(exc), 500)
        finally:
            try:
                response.close()
            except Exception:
                pass

    content_encoding = response_headers.get("Content-Encoding", "").casefold()
    decoded_body = raw_body
    decode_error = None
    if content_encoding == "gzip" and raw_body:
        try:
            decoded_body = gzip.decompress(raw_body)
        except Exception as exc:
            decode_error = f"{type(exc).__name__}:{_safe_text(str(exc), 300)}"
    if len(raw_body) > MAX_COMPRESSED_BYTES:
        decode_error = decode_error or "compressed_body_exceeds_diagnostic_bound"
    if len(decoded_body) > MAX_DECODED_BYTES:
        decoded_body = decoded_body[:MAX_DECODED_BYTES]
        decode_error = decode_error or "decoded_body_exceeds_diagnostic_bound"

    body_text = decoded_body.decode("utf-8", errors="replace")
    payload = None
    try:
        payload = json.loads(body_text) if decoded_body else None
    except (ValueError, TypeError) as exc:
        decode_error = decode_error or f"{type(exc).__name__}:{_safe_text(str(exc), 300)}"

    content_type = response_headers.get("Content-Type", "")
    is_html = "html" in content_type.casefold() or body_text.lstrip().lower().startswith(("<html", "<!doctype html"))
    if status is None:
        classification = "network_error"
    elif status == 401:
        classification = "http_401_unauthorized"
    elif status == 403:
        classification = "http_403_forbidden"
    elif status == 429:
        classification = "http_429_rate_limited"
    elif 500 <= status <= 599:
        classification = "http_5xx_server_error"
    elif not decoded_body:
        classification = "empty_response"
    elif is_html:
        classification = "html_response"
    elif decode_error:
        classification = "invalid_json_or_compression"
    elif not isinstance(payload, dict):
        classification = "json_unexpected_shape"
    else:
        classification = "json_response"

    report = {
        "request_url": _safe_url(url),
        "final_url": final_url,
        "http_status_code": status,
        "response_headers": response_headers,
        "content_type": content_type or None,
        "content_length_header": response_headers.get("Content-Length"),
        "content_encoding": content_encoding or "identity",
        "compressed_response_bytes": len(raw_body),
        "decoded_response_bytes": len(decoded_body),
        "response_body_sha256": __import__("hashlib").sha256(raw_body).hexdigest() if raw_body else None,
        "response_body_prefix": _safe_text(body_text, 900),
        "is_empty": not decoded_body,
        "is_html": is_html,
        "is_json": isinstance(payload, dict),
        "classification": classification,
        "redirects": redirect_recorder.events,
        "redirect_count": len(redirect_recorder.events),
        "user_agent": USER_AGENT,
        "referer": REFERER,
        "timeout_seconds": timeout_seconds,
        "exception_type": exception_type,
        "exception_message": exception_message,
        "decode_error": decode_error,
        "proxy_environment_configured": any(os.environ.get(key) for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"
        )),
        "request_started_at": started,
        "request_finished_at": _utc_now(),
        "credentials_logged": False,
        "cookies_sent": False,
    }
    if classification != "json_response" or not isinstance(payload, dict):
        raise ESPNSourceError(report)
    return ESPNResponse(payload=payload, raw_body=raw_body, decoded_body=decoded_body, report=report)


def fetch_espn_team_directory(timeout_seconds: int = 20) -> ESPNResponse:
    return fetch_json(ESPN_TEAMS_URL, timeout_seconds=timeout_seconds)


def fetch_espn_scoreboard(game_date: str, timeout_seconds: int = 20) -> ESPNResponse:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", game_date):
        raise ValueError("invalid_local_game_date")
    yyyymmdd = game_date.replace("-", "")
    return fetch_json(ESPN_SCOREBOARD_URL + "?" + urlencode({"dates": yyyymmdd}), timeout_seconds=timeout_seconds)


def fetch_espn_summary(event_id: str, timeout_seconds: int = 20) -> ESPNResponse:
    if not str(event_id).isdigit():
        raise ValueError("invalid_espn_event_id")
    return fetch_json(ESPN_SUMMARY_URL + "?" + urlencode({"event": str(event_id)}), timeout_seconds=timeout_seconds)
