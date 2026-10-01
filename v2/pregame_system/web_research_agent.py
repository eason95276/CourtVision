"""Bounded DeepSeek tool loop for current pregame web evidence."""
from __future__ import annotations

import json
import re
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .storage import BASE, clean, content_hash, now, save
from .skill_provenance import prompt_skill_hashes


ALLOWED_CATEGORIES = {
    "availability",
    "lineup",
    "roster_news",
    "game_story",
    "market",
}

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_pregame_web",
        "description": (
            "Search current pregame web evidence for this exact NBA game. "
            "Results remain subject to source, identity, and cutoff validation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "categories": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(ALLOWED_CATEGORIES)},
                    "minItems": 1,
                    "maxItems": 5,
                },
                "reason": {"type": "string", "maxLength": 240},
                "focus_category": {
                    "type": "string",
                    "enum": sorted(ALLOWED_CATEGORIES),
                    "description": "Optional category for the focus argument; must belong to categories.",
                },
                "focus": {
                    "type": "string",
                    "maxLength": 100,
                    "description": "Optional specific evidence gap for the selected focus_category.",
                },
            },
            "required": ["categories", "reason"],
            "additionalProperties": False,
        },
    },
}

FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_sources",
        "description": (
            "Fetch full text for up to four URLs returned by the preceding search. "
            "Only URLs included in the supplied sources are accepted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 4,
                },
                "reason": {"type": "string", "maxLength": 240},
            },
            "required": ["urls", "reason"],
            "additionalProperties": False,
        },
    },
}


class WebResearchAgentError(RuntimeError):
    """Raised when the bounded research planner cannot produce a safe tool call."""


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AutonomousWebResearcher:
    def __init__(
        self,
        *,
        searcher: Any,
        root: str | Path | None = None,
        client=None,
        max_tool_calls: int = 4,
    ) -> None:
        self.searcher = searcher
        self.root = Path(root or BASE / "outputs" / "web_research_agent")
        self.client = client
        self.max_tool_calls = max(1, min(int(max_tool_calls), 6))

    def _folder(self) -> Path:
        folder = self.root / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "_"
            + uuid.uuid4().hex[:8]
        )
        folder.mkdir(parents=True, exist_ok=False)
        return folder

    @staticmethod
    def _messages(
        game: dict[str, Any],
        cutoff: str,
        categories: set[str],
        local_context: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        system = (
            "你是第25秒网页分析链路的研究规划器。使用调用方提供的比赛身份、截止时间、candidate_categories "
            "和 local_context，并按当前 Research Skill 决定联网查询。"
            "工具类别和参数必须符合给定 schema；最多调用一次 search_pregame_web。"
            "如需详情，可调用 fetch_sources，但 URL 必须来自本轮 search_pregame_web 的结果。"
            "不得改变比赛、球队或球员身份；联网事实须符合给定 cutoff 和来源要求。"
            "当前官方名单属于带抓取时间的名单快照，不等同于已确认交易或本场可出战。"
            "你负责补充研究证据，不生成或修改任何数值预测。"
        )
        from .research_skill import load_pregame_research_skill

        system += "\n\n以下是每次研究规划时加载的 Pregame Research Skill：\n" + load_pregame_research_skill()
        payload = {
            "game": game,
            "cutoff": cutoff,
            "candidate_categories": sorted(categories & ALLOWED_CATEGORIES),
            "local_history": "already_available",
            "local_context": clean(local_context or {}),
        }
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    def _tool_calls(
        self,
        *,
        stage: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        required: bool,
    ) -> list[dict[str, Any]]:
        if self.client is not None:
            calls = self.client(stage, messages, tools, required)
            return list(calls or [])
        from langchain_openai import ChatOpenAI

        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise WebResearchAgentError("research_agent_api_key_missing")
        llm = ChatOpenAI(
            model="deepseek-v4-flash",
            api_key=key,
            base_url="https://api.deepseek.com",
            max_retries=0,
            timeout=30,
            max_tokens=900,
            temperature=0,
            use_responses_api=False,
            disabled_params={"parallel_tool_calls": None},
            extra_body={"thinking": {"type": "disabled"}, "max_tokens": 900},
        )
        response = llm.bind_tools(
            tools,
            tool_choice="required" if required else "auto",
        ).invoke(messages)
        return list(response.tool_calls or [])

    @staticmethod
    def _search_summary(context: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": record.get("id"),
                "category": record.get("category"),
                "title": record.get("title"),
                "url": record.get("url"),
                "snippet": str(record.get("snippet") or "")[:500],
                "published_at": record.get("published_at"),
            }
            for record in context.get("records") or []
        ]

    def _enrich_fetches(
        self,
        context: dict[str, Any],
        urls: list[str],
        cutoff: str,
        *,
        replay: bool = False,
    ) -> list[dict[str, Any]]:
        if replay:
            return []
        records = context.get("records") or []
        allowed = {str(record.get("url")) for record in records if record.get("url")}
        selected = list(dict.fromkeys(str(url) for url in urls if str(url) in allowed))[:4]
        if not selected:
            return []
        fetched = self.searcher.local_transport.fetch_many(selected)
        cutoff_dt = _instant(cutoff)
        by_url = {str(item.get("url")): item for item in fetched if item.get("url")}
        accepted: list[dict[str, Any]] = []
        for record in records:
            item = by_url.get(str(record.get("url")))
            if not item:
                continue
            published = item.get("published_date")
            if published:
                try:
                    if _instant(str(published)) > cutoff_dt:
                        continue
                except ValueError:
                    continue
            content = str(item.get("content") or "").strip()
            if content:
                record["snippet"] = content[:4000]
            if published and not record.get("published_at"):
                record["published_at"] = str(published)
            record["fetched_at"] = now()
            record["fetch_provider"] = "free-search-mcp"
            accepted.append(
                {
                    "url": record.get("url"),
                    "content_chars": len(content),
                    "published_at": record.get("published_at"),
                }
            )
        return accepted

    def investigate(
        self,
        game: dict[str, Any],
        cutoff: str,
        *,
        categories: set[str],
        replay: bool = False,
        local_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        if replay:
            try:
                context = self.searcher.search(
                    game,
                    cutoff,
                    replay=True,
                    categories=categories,
                )
            except Exception as exc:
                context = {
                    "status": "unavailable",
                    "reason": "historical_source_archive_unavailable",
                    "records": [],
                    "trace": [],
                }
                return context, {
                    "status": "unavailable",
                    "mode": "historical_archive",
                    "error_type": type(exc).__name__,
                    "calls": [],
                }
            return context, {
                "status": context.get("status", "unavailable"),
                "mode": "historical_archive",
                "game_id": game.get("game_id"),
                "cutoff": cutoff,
                "record_count": len(context.get("records") or []),
                "calls": [],
            }

        folder = self._folder()
        messages = self._messages(game, cutoff, categories, local_context)
        attempt = {
            "created_at": now(),
            "game_id": game.get("game_id"),
            "cutoff": cutoff,
            "replay": replay,
            "max_tool_calls": self.max_tool_calls,
            "input_hash": content_hash(messages),
            "skill_hashes": prompt_skill_hashes(messages[0]["content"]),
            "planner_model": "injected_test_client" if self.client is not None else "deepseek-v4-flash",
        }
        save(folder / "attempt.json", attempt)
        trace: dict[str, Any] = {
            "status": "started",
            "path": str(folder),
            "calls": [],
        }
        try:
            search_calls = self._tool_calls(
                stage="search",
                messages=messages,
                tools=[SEARCH_TOOL],
                required=True,
            )
            if not search_calls:
                raise WebResearchAgentError("research_agent_search_call_missing")
            call = search_calls[0]
            if call.get("name") != "search_pregame_web":
                raise WebResearchAgentError("research_agent_unknown_search_tool")
            args = call.get("args") or {}
            selected = {
                str(item)
                for item in args.get("categories") or []
                if str(item) in categories and str(item) in ALLOWED_CATEGORIES
            }
            if not selected:
                raise WebResearchAgentError("research_agent_empty_categories")
            focus = str(args.get("focus") or "").strip()
            focus_category = str(args.get("focus_category") or "").strip()
            if focus and (focus_category not in selected or len(focus) > 100 or "\n" in focus or "://" in focus):
                raise WebResearchAgentError("research_agent_invalid_focus")
            context = self.searcher.search(
                game,
                cutoff,
                replay=replay,
                categories=selected,
                focus=(focus_category, focus) if focus else None,
            )
            trace["calls"].append(
                {
                    "tool": "search_pregame_web",
                    "args": clean(args),
                    "selected_categories": sorted(selected),
                    "record_count": len(context.get("records") or []),
                    "source_trace": clean(context.get("trace") or []),
                }
            )

            fetch_candidates = [
                record
                for record in context.get("records") or []
                if record.get("category") in {"availability", "lineup", "roster_news", "game_story"}
                or not str(record.get("snippet") or "").strip()
            ]
            if fetch_candidates and len(trace["calls"]) < self.max_tool_calls:
                fetch_messages = [
                    {
                        "role": "system",
                        "content": (
                            messages[0]["content"]
                            + "\n\n当前为来源抓取阶段，仅可调用 fetch_sources。"
                            "URL 必须来自下方 sources；无需调用工具时回复完成。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "local_context": clean(local_context or {}),
                                "sources": self._search_summary({"records": fetch_candidates}),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
                fetch_calls = self._tool_calls(
                    stage="fetch",
                    messages=fetch_messages,
                    tools=[FETCH_TOOL],
                    required=False,
                )
                if fetch_calls:
                    fetch_call = fetch_calls[0]
                    if fetch_call.get("name") == "fetch_sources":
                        fetch_args = fetch_call.get("args") or {}
                        fetched = self._enrich_fetches(
                            context,
                            list(fetch_args.get("urls") or []),
                            cutoff,
                            replay=replay,
                        )
                        trace["calls"].append(
                            {
                                "tool": "fetch_sources",
                                "args": clean(fetch_args),
                                "accepted": fetched,
                            }
                        )

            trace["status"] = "success"
            trace["record_count"] = len(context.get("records") or [])
            trace["trace_hash"] = content_hash(trace)
            save(folder / "trace.json", trace)
            return context, trace
        except Exception as exc:
            trace["status"] = "unavailable"
            trace["error_type"] = type(exc).__name__
            trace["message"] = str(exc)[:400]
            save(folder / "failure.json", trace)
            raise
