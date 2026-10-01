from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from .free_search_mcp_client import (
    FreeSearchMCPError,
    FreeSearchMCPTransport,
    _normalize_fetch,
    _normalize_result,
    _search_arguments,
)


def test_mcp_search_arguments_are_bounded_and_keyless():
    arguments = _search_arguments(
        {
            "query": "Knicks championship",
            "max_results": 200,
            "include_domains": ["nba.com"],
        }
    )
    assert arguments == {
        "query": "Knicks championship",
        "engines": ["anysearch"],
        "max_results": 12,
        "format": "json",
        "max_age_hours": 0.0,
        "include_domains": ["nba.com"],
    }
    assert "api_key" not in arguments


def test_mcp_result_is_normalized_to_existing_web_transport_shape():
    result = _normalize_result(
        {
            "engines": ["anysearch"],
            "cached": False,
            "results": [
                {
                    "title": "2026 NBA Finals",
                    "url": "https://www.nba.com/playoffs/2026/nba-finals",
                    "snippet": "New York Knicks win the championship.",
                    "score": 0.8,
                    "engines": ["anysearch"],
                }
            ],
        }
    )
    assert result["provider"] == "free-search-mcp"
    assert result["results"][0]["content"].startswith("New York Knicks")
    assert result["results"][0]["published_date"] is None


def test_fetch_result_keeps_page_provenance():
    result = _normalize_fetch(
        {
            "url": "https://www.nba.com/news/example",
            "title": "Example",
            "content": "Article body",
            "published_date": "2026-09-21",
            "method": "http",
        }
    )
    assert result["provider"] == "free-search-mcp"
    assert result["published_date"] == "2026-09-21"
    assert result["content"] == "Article body"


def test_transport_validates_child_response(monkeypatch):
    payload = [{"query": "Knicks", "max_results": 3}]
    response = [{"provider": "free-search-mcp", "results": []}]

    def run(*args, **kwargs):
        assert json.loads(kwargs["input"].decode("utf-8")) == payload
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(response).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr("subprocess.run", run)
    transport = FreeSearchMCPTransport(uv_command="uv", timeout_seconds=1)
    assert transport.search_many(payload) == response


def test_transport_reports_child_failure(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=b"", stderr=b"failed"),
    )
    transport = FreeSearchMCPTransport(uv_command="uv", timeout_seconds=1)
    with pytest.raises(FreeSearchMCPError, match="free_search_mcp_process_failed"):
        transport.search_many([{"query": "Knicks"}])
