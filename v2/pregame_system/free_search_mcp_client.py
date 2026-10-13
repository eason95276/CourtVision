"""Small stdio bridge for the optional free-search-mcp runtime.

The CourtVision process does not import the MCP SDK.  A child Python process
created by ``uv run --with free-search-mcp`` owns the SDK and the MCP session,
then returns only normalized search results as JSON.  This keeps the search
dependency isolated from the prediction runtime and makes absence of ``uv`` a
normal, recoverable capability downgrade.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


PACKAGE_SPEC = "free-search-mcp==0.11.0"
PARSER_SPEC = "selectolax==0.4.13"  # 0.11.0 imports the pre-1.0 Modest parser.
DEFAULT_ENGINE = "anysearch"


class FreeSearchMCPError(RuntimeError):
    """Raised when the local MCP bridge cannot return a valid response."""
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


def redact(message):
    value = str(message or "")
    for key, secret in os.environ.items():
        if secret and ("API_KEY" in key or key.endswith("_TOKEN")):
            value = value.replace(secret, "[REDACTED]")
    return value


def error_diagnostic(exc, *, operation, url=None, provider="free-search-mcp"):
    details = dict(getattr(exc, "details", {}) or {})
    if "failure_kind" not in details:
        details["failure_kind"] = ("timeout" if isinstance(exc,(TimeoutError,subprocess.TimeoutExpired))
            else "http" if getattr(exc,"code",None) else "connection" if isinstance(exc,(ConnectionError,OSError))
            else "protocol" if isinstance(exc,(ValueError,TypeError)) else "provider")
    details.setdefault("exit_code",None)
    details.setdefault("stderr",None)
    if getattr(exc,"code",None):details["http_status"]=exc.code
    return {**details, "operation": operation, "url": url, "provider": provider,
            "exception_type": type(exc).__name__, "message": redact(exc)[:2000],
            "cause": redact(exc.__cause__)[:1200] if exc.__cause__ else None}


def _search_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "query": str(payload["query"]),
        "engines": [str(payload.get("engine") or DEFAULT_ENGINE)],
        "max_results": max(1, min(int(payload.get("max_results") or 6), 12)),
        "format": "json",
        "max_age_hours": float(payload.get("max_age_hours", 0)),
    }
    domains = [str(item) for item in payload.get("include_domains") or [] if item]
    if domains:
        arguments["include_domains"] = domains
    return arguments


def _normalize_result(data: dict[str, Any]) -> dict[str, Any]:
    results = []
    for item in data.get("results") or []:
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("snippet") or "",
                "score": item.get("score"),
                "published_date": item.get("published_date"),
                "engines": item.get("engines") or [],
                "provider_temporal_metadata": {k:item[k] for k in ("published_at","published_date","datePublished","updated_at","dateModified","date","timestamp") if k in item},
            }
        )
    return {
        "results": results,
        "provider": "free-search-mcp",
        "engines": data.get("engines") or [],
        "cached": data.get("cached"),
        "errors": data.get("errors"),
        "gated_engines": data.get("gated_engines") or [],
        "hint": data.get("hint"),
        "filter_diagnostics": data.get("filter_diagnostics"),
    }


def _normalize_fetch(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": data.get("url"),
        "title": data.get("title"),
        "content": data.get("content") or "",
        "published_date": data.get("published_date"),
        "author": data.get("author"),
        "sitename": data.get("sitename"),
        "method": data.get("method"),
        "truncated": data.get("truncated"),
        "provider": "free-search-mcp",
        **{k:data[k] for k in ("published_at","updated_at","datePublished","dateModified","date_published","updated_date","date","timestamp","timestamp_type") if k in data},
        **({"errors": data.get("errors") or {"fetch": data.get("error")}}
           if data.get("errors") or data.get("error") else {}),
    }


def _tool_payload(result: Any) -> dict[str, Any] | None:
    structured = result.structured_content or {}
    data = structured.get("result")
    if isinstance(data, dict):
        return data
    for block in result.content or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def _bridge_search(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "search_mcp"],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    output: list[dict[str, Any]] = []
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            async def call_one(payload: dict[str, Any]) -> dict[str, Any]:
                try:
                    operation = str(payload.get("operation") or "search")
                    if operation == "fetch":
                        tool_name = "fetch"
                        arguments = {
                            "url": str(payload["url"]),
                            "render": str(payload.get("render") or "http"),
                            "force_refresh": bool(payload.get("force_refresh", False)),
                            "format": "json",
                        }
                    else:
                        tool_name = "search"
                        arguments = _search_arguments(payload)
                    result = await session.call_tool(
                        tool_name,
                        arguments,
                    )
                    data = _tool_payload(result)
                    if result.is_error or not isinstance(data, dict):
                        return {
                            "url": payload.get("url") if operation == "fetch" else None,
                            "results": [],
                            "provider": "free-search-mcp",
                            "errors": {"mcp": "tool_error_or_invalid_result"},
                            "diagnostic": {"operation": operation, "url": payload.get("url"),
                                "provider": "free-search-mcp", "failure_kind": "protocol",
                                "message": redact(" ".join(str(getattr(b, "text", "")) for b in result.content or []))[:2000]},
                        }
                    return _normalize_fetch(data) if operation == "fetch" else _normalize_result(data)
                except Exception as exc:
                    return {
                        "url": payload.get("url") if payload.get("operation") == "fetch" else None,
                        "results": [],
                        "provider": "free-search-mcp",
                        "errors": {"mcp": type(exc).__name__},
                        "diagnostic": error_diagnostic(exc, operation=payload.get("operation", "search"), url=payload.get("url")),
                    }

            output = list(await asyncio.gather(*(call_one(payload) for payload in payloads)))
    return output


def _bridge_main() -> int:
    try:
        payloads = json.loads(sys.stdin.read())
        if not isinstance(payloads, list) or not all(isinstance(item, dict) for item in payloads):
            raise ValueError("payload_list_required")
        result = asyncio.run(_bridge_search(payloads))
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {str(exc)[:400]}\n")
        return 1


class FreeSearchMCPTransport:
    """Invoke one local MCP session for a batch of search payloads."""

    def __init__(
        self,
        *,
        uv_command: str | None = None,
        package_spec: str = PACKAGE_SPEC,
        timeout_seconds: int = 55,
    ) -> None:
        self.uv_command = uv_command or os.environ.get("FREE_SEARCH_MCP_UV") or shutil.which("uv")
        self.package_spec = package_spec
        self.timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        return bool(self.uv_command) and os.environ.get("FREE_SEARCH_MCP_ENABLED", "1") != "0"

    def _run_many(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.available:
            raise FreeSearchMCPError("free_search_mcp_unavailable")
        command = [
            str(self.uv_command),
            "run",
            "--quiet",
            "--with",
            self.package_spec,
            "--with",
            PARSER_SPEC,
            "python",
            "-m",
            "v2.pregame_system.free_search_mcp_client",
            "--bridge",
        ]
        try:
            completed = subprocess.run(
                command,
                input=json.dumps(payloads, ensure_ascii=False).encode("utf-8"),
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                cwd=Path(__file__).resolve().parents[2],
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except subprocess.TimeoutExpired as exc:
            raise FreeSearchMCPError("free_search_mcp_timeout", failure_kind="timeout",
                timeout_seconds=self.timeout_seconds, exit_code=None,
                stdout=redact((exc.stdout or b"").decode("utf-8", errors="replace"))[-2000:],
                stderr=redact((exc.stderr or b"").decode("utf-8", errors="replace"))[-4000:]) from exc
        if completed.returncode != 0:
            detail = redact(completed.stderr.decode("utf-8", errors="replace"))[-4000:]
            raise FreeSearchMCPError(f"free_search_mcp_process_failed:{detail}", failure_kind="process_exit",
                exit_code=completed.returncode, stderr=detail,
                stdout=redact(completed.stdout.decode("utf-8", errors="replace"))[-2000:])
        try:
            responses = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FreeSearchMCPError("free_search_mcp_invalid_json", failure_kind="protocol",
                exit_code=completed.returncode, stderr=redact(completed.stderr.decode("utf-8",errors="replace"))[-2000:],
                stdout=redact(completed.stdout.decode("utf-8",errors="replace"))[-2000:]) from exc
        if not isinstance(responses, list) or len(responses) != len(payloads):
            raise FreeSearchMCPError("free_search_mcp_response_count_mismatch", failure_kind="protocol",
                                    exit_code=completed.returncode, expected_count=len(payloads), actual_count=len(responses) if isinstance(responses,list) else None)
        return responses

    def search_many(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._run_many(payloads)

    def fetch_many(self, urls: list[str]) -> list[dict[str, Any]]:
        unique = list(dict.fromkeys(str(url) for url in urls if url))
        def fetch_one(url):
            try:
                return self._run_many([{"operation": "fetch", "url": url, "render": "http", "force_refresh": True}])[0]
            except Exception as exc:
                return {"url": url, "content": "", "provider": "free-search-mcp",
                        "errors": {"fetch": type(exc).__name__},
                        "diagnostic": error_diagnostic(exc, operation="fetch", url=url)}
        # Each URL owns its timeout/process; a slow or broken source cannot
        # discard successful responses from the same requested batch.
        with ThreadPoolExecutor(max_workers=min(4, max(1,len(unique)))) as pool:
            return list(pool.map(fetch_one, unique))

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.search_many([payload])[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", action="store_true")
    args = parser.parse_args()
    if not args.bridge:
        parser.error("--bridge is required")
    return _bridge_main()


if __name__ == "__main__":
    raise SystemExit(main())
