"""One-request, credential-safe diagnostics for NBA Stats boxscore responses."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


NBA_STATS_URL = "https://stats.nba.com/stats/boxscoretraditionalv3"
NBA_STATS_PARAMETERS = {
    "EndPeriod": "0",
    "EndRange": "0",
    "GameID": "",
    "RangeType": "0",
    "StartPeriod": "0",
    "StartRange": "0",
}
NBA_STATS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Referer": "https://stats.nba.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
MAX_DIAGNOSTIC_BODY_BYTES = 1_000_000
BODY_PREFIX_CHARACTERS = 900
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|set-cookie|access[_-]?token|token|secret|password)"
    r"(\s*[:=]\s*)([^\s,;\"']+)"
)
_SAFE_QUERY_KEYS = {"GameID", "EndPeriod", "EndRange", "RangeType", "StartPeriod", "StartRange"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def safe_url(value: str | None) -> str | None:
    if not value:
        return value
    try:
        parts = urlsplit(value)
        query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
                 if key in _SAFE_QUERY_KEYS]
        return urlunsplit((parts.scheme, parts.hostname or "", parts.path,
                           urlencode(query), ""))
    except Exception:
        return "<unparseable-url>"


def redact_text(value: str) -> str:
    return _SENSITIVE_ASSIGNMENT.sub(r"\1\2<redacted>", value)


def _is_html(content_type: str | None, body_text: str) -> bool:
    lower = body_text.lstrip().lower()
    return bool(
        (content_type or "").lower().startswith("text/html")
        or lower.startswith("<!doctype html")
        or lower.startswith("<html")
    )


def classify_response(
    status_code: int | None,
    content_type: str | None,
    body: bytes,
    *,
    exception_type: str | None = None,
    proxy_configured: bool = False,
) -> dict[str, Any]:
    body_text = body.decode("utf-8", errors="replace")
    html = _is_html(content_type, body_text)
    parsed_json: Any = None
    is_json = False
    try:
        parsed_json = json.loads(body_text) if body else None
        is_json = body != b""
    except (ValueError, TypeError):
        pass
    challenge = html and any(
        marker in body_text.casefold()
        for marker in ("cloudflare", "captcha", "checking your browser", "access denied", "akamai")
    )

    if status_code == 401:
        classification = "http_401_unauthorized"
    elif status_code == 403:
        classification = "http_403_forbidden"
    elif status_code == 429:
        classification = "http_429_rate_limited"
    elif status_code is not None and 500 <= status_code <= 599:
        classification = "http_5xx_server_error"
    elif status_code is None and exception_type:
        classification = "network_proxy_or_intermediary" if proxy_configured else "network_error"
    elif not body:
        classification = "empty_response"
    elif challenge:
        classification = "html_challenge"
    elif html:
        classification = "html_response"
    elif is_json and isinstance(parsed_json, dict) and isinstance(parsed_json.get("boxScoreTraditional"), dict):
        classification = "json_expected_payload"
    elif is_json:
        classification = "json_unexpected_payload"
    elif "json" in (content_type or "").lower():
        classification = "invalid_json"
    else:
        classification = "non_json_response"

    return {
        "classification": classification,
        "is_empty": not body,
        "is_html": html,
        "is_challenge": challenge,
        "is_json": is_json,
        "is_expected_boxscore_payload": classification == "json_expected_payload",
    }


class _RedirectRecorder(HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.events.append({
            "status_code": int(code),
            "from_url": safe_url(req.full_url),
            "to_url": safe_url(newurl),
        })
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def diagnose_nba_stats_request(
    game_id: str,
    *,
    timeout_seconds: int = 20,
    max_body_bytes: int = MAX_DIAGNOSTIC_BODY_BYTES,
    header_profile: str = "stats_default",
) -> tuple[dict[str, Any], dict[str, Any] | None, bytes]:
    """Issue one raw HTTP GET without cookies/auth and return a sanitized report."""
    if not str(game_id).isdigit() or len(str(game_id)) != 10:
        raise ValueError("game_id_must_be_ten_digit_nba_stats_id")
    if timeout_seconds < 1 or max_body_bytes < 1024:
        raise ValueError("invalid_diagnostic_bounds")

    parameters = dict(NBA_STATS_PARAMETERS)
    parameters["GameID"] = str(game_id)
    request_url = NBA_STATS_URL + "?" + urlencode(parameters)
    request_headers = dict(NBA_STATS_HEADERS)
    if header_profile == "nba_site":
        request_headers["Origin"] = "https://www.nba.com"
        request_headers["Referer"] = "https://www.nba.com/"
    elif header_profile != "stats_default":
        raise ValueError("unsupported_header_profile")
    request = Request(request_url, headers=request_headers, method="GET")
    redirect_recorder = _RedirectRecorder()
    opener = build_opener(redirect_recorder)
    response = None
    exception_type = None
    exception_message = None
    started_at = _utc_now()
    body = b""
    final_url = None
    status_code = None
    content_type = None
    content_length = None
    selected_response_headers: dict[str, str] = {}

    proxy_configured = any(
        os.environ.get(key)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    )
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except HTTPError as exc:
        response = exc
        exception_type = type(exc).__name__
        exception_message = redact_text(str(exc))[:500]
    except Exception as exc:
        exception_type = type(exc).__name__
        exception_message = redact_text(str(exc))[:500]

    if response is not None:
        try:
            status_code = int(response.getcode())
        except Exception:
            status_code = None
        try:
            final_url = safe_url(response.geturl())
        except Exception:
            final_url = None
        headers = getattr(response, "headers", {}) or {}
        content_type = headers.get("Content-Type")
        content_length = headers.get("Content-Length")
        selected_response_headers = {
            name: str(headers[name]) for name in ("Content-Type", "Content-Length", "Date", "Retry-After", "Server")
            if headers.get(name) is not None
        }
        try:
            body = response.read(max_body_bytes + 1)
        except Exception as exc:
            exception_type = exception_type or type(exc).__name__
            exception_message = exception_message or redact_text(str(exc))[:500]
        finally:
            try:
                response.close()
            except Exception:
                pass

    truncated = len(body) > max_body_bytes
    body = body[:max_body_bytes]
    body_text = body.decode("utf-8", errors="replace")
    parsed_payload = None
    try:
        parsed_payload = json.loads(body_text) if body else None
    except (ValueError, TypeError):
        pass
    shape_valid = (
        isinstance(parsed_payload, dict)
        and isinstance(parsed_payload.get("boxScoreTraditional"), dict)
    )
    classification = classify_response(
        status_code,
        content_type,
        body,
        exception_type=exception_type,
        proxy_configured=bool(proxy_configured),
    )
    if classification["classification"] == "json_expected_payload" and truncated:
        classification["classification"] = "response_body_truncated"
        classification["is_expected_boxscore_payload"] = False
        shape_valid = False
    report = {
        "diagnostic_version": "nba-stats-http-diagnostic-v1",
        "provider": "NBA Stats",
        "endpoint": "boxscoretraditionalv3",
        "request_url": safe_url(request_url),
        "final_url": final_url,
        "request_parameters": parameters,
        "http_status_code": status_code,
        "response_headers": selected_response_headers,
        "content_type": content_type,
        "content_length_header": content_length,
        "actual_response_bytes_read": len(body),
        "response_body_truncated": truncated,
        "response_body_sha256": hashlib.sha256(body).hexdigest() if body else None,
        "response_body_prefix": redact_text(body_text[:BODY_PREFIX_CHARACTERS]),
        **classification,
        "redirects": redirect_recorder.events,
        "redirect_count": len(redirect_recorder.events),
        "user_agent": request_headers["User-Agent"],
        "referer": request_headers["Referer"],
        "origin": request_headers.get("Origin"),
        "header_profile": header_profile,
        "timeout_seconds": timeout_seconds,
        "exception_type": exception_type,
        "exception_message": exception_message,
        "proxy_environment_configured": bool(proxy_configured),
        "request_started_at": started_at,
        "request_finished_at": _utc_now(),
        "payload_shape_valid": bool(shape_valid),
        "credentials_logged": False,
        "cookies_sent": False,
    }
    return report, parsed_payload if shape_valid else None, body
