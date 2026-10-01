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
from pathlib import Path
from typing import Any


PACKAGE_SPEC = "free-search-mcp==0.11.0"
DEFAULT_ENGINE = "anysearch"


class FreeSearchMCPError(RuntimeError):
    """Raised when the local MCP bridge cannot return a valid response."""


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
                            "results": [],
                            "provider": "free-search-mcp",
                            "errors": {"mcp": "tool_error_or_invalid_result"},
                        }
                    return _normalize_fetch(data) if operation == "fetch" else _normalize_result(data)
                except Exception as exc:
                    return {
                        "results": [],
                        "provider": "free-search-mcp",
                        "errors": {"mcp": type(exc).__name__},
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
            raise FreeSearchMCPError("free_search_mcp_timeout") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")[-400:].replace("\n", " ")
            raise FreeSearchMCPError(f"free_search_mcp_process_failed:{detail}")
        try:
            responses = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FreeSearchMCPError("free_search_mcp_invalid_json") from exc
        if not isinstance(responses, list) or len(responses) != len(payloads):
            raise FreeSearchMCPError("free_search_mcp_response_count_mismatch")
        return responses

    def search_many(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._run_many(payloads)

    def fetch_many(self, urls: list[str]) -> list[dict[str, Any]]:
        unique = list(dict.fromkeys(str(url) for url in urls if url))
        return self._run_many(
            [{"operation": "fetch", "url": url, "render": "http"} for url in unique]
        )

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
