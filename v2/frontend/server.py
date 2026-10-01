"""Serve the CourtVision V2 home and game detail frontend."""
from __future__ import annotations

import argparse
import functools
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from v2.pregame_system.web_search import PregameWebSearch, select_public_market_reference

from .analysis_service import (
    DEFAULT_PUBLIC_MARKET_CACHE,
    analysis_cutoff,
    cached_public_market_context,
    game_identity,
)
from .export_data import DEFAULT_OUTPUT, STATIC_ROOT, export_app_data, read_json
from .prediction_version_service import PredictionVersionService
from v2.pregame_system.prediction_versions import VersionStoreError

_PREDICTION_VERSION_SERVICE: PredictionVersionService | None = None


def _prediction_version_service() -> PredictionVersionService:
    global _PREDICTION_VERSION_SERVICE
    if _PREDICTION_VERSION_SERVICE is None:
        _PREDICTION_VERSION_SERVICE = PredictionVersionService()
    return _PREDICTION_VERSION_SERVICE


def build_prediction_versions_response(
    *, data: dict[str, Any] | None = None, version_service: PredictionVersionService | None = None
) -> tuple[int, dict[str, Any]]:
    payload = data or read_json(DEFAULT_OUTPUT)
    service = version_service or _prediction_version_service()
    selections: dict[str, Any] = {}
    for game in payload.get("games", []):
        game_id = game.get("id")
        if not isinstance(game_id, str):
            continue
        try:
            selections[game_id] = service.selected(game)
        except VersionStoreError as exc:
            selections[game_id] = {
                "status": str(exc), "game_id": game_id, "version": None,
                "version_id": None, "state_revision": 0, "locked_at": None,
                "lock_reason": None,
            }
    return 200, {"status": "success", "predictions": selections}


def _version_update_response(selection: dict[str, Any], *, update_status: str,
                             message: str | None = None) -> dict[str, Any]:
    return {
        **selection,
        "status": "success" if selection.get("version") else selection.get("status", "unavailable"),
        "update_status": update_status,
        "message": message,
    }


def load_local_env(path: str | Path | None = None) -> None:
    """Load an ignored project-local .env without overriding shell values."""
    env_path = Path(path or Path(__file__).parents[2] / ".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def build_ai_response(
    game_id: str,
    *,
    api_key: str | None = None,
    data: dict[str, Any] | None = None,
    request_id: str | None = None,
    version_service: PredictionVersionService | None = None,
) -> tuple[int, dict[str, Any]]:
    payload = data or read_json(DEFAULT_OUTPUT)
    game = next((item for item in payload["games"] if item.get("id") == game_id), None)
    if game is None:
        return 404, {"status": "error", "message": "没有找到这场比赛的前端数据。"}
    service = version_service or _prediction_version_service()
    try:
        selected = service.selected(game)
    except Exception as exc:
        return 503, {
            "status": "unavailable",
            "message": f"版本状态暂不可用：{type(exc).__name__}。",
        }
    if selected["status"] in {"locked", "locked_unavailable", "schedule_changed"}:
        return 200, _version_update_response(selected, update_status="locked")
    key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        status = 200 if selected.get("version") else 503
        return status, _version_update_response(
            selected, update_status="failed", message="本地尚未配置分析模型凭据。当前预测保持不变。"
        )
    try:
        result = service.generate(payload, game_id, request_id=request_id)
        return 200, result
    except Exception as exc:
        try:
            current = service.selected(game)
        except Exception:
            current = selected
        status = 200 if current.get("version") else 503
        return status, _version_update_response(
            current,
            update_status="failed",
            message=f"第25秒分析暂不可用：{type(exc).__name__}。",
        )


def build_market_response(
    game_id: str,
    *,
    data: dict[str, Any] | None = None,
    searcher: Any | None = None,
    cache_path: str | Path = DEFAULT_PUBLIC_MARKET_CACHE,
) -> tuple[int, dict[str, Any]]:
    """Resolve a display-only public quote without invoking the LLM."""
    payload = data or read_json(DEFAULT_OUTPUT)
    game = next((item for item in payload.get("games", []) if item.get("id") == game_id), None)
    if game is None:
        return 404, {"status": "error", "message": "没有找到这场比赛的前端数据。"}
    structured = (game.get("background") or {}).get("market")
    if structured:
        return 200, {
            "status": "structured_available",
            "game_id": game_id,
            "public_market_reference": None,
        }
    try:
        cutoff = analysis_cutoff(game)
    except (KeyError, TypeError, ValueError):
        return 200, {
            "status": "unavailable",
            "game_id": game_id,
            "message": "缺少可核验的市场时间截止点。",
            "lookup": {"source": "cutoff_unavailable"},
        }
    cached = cached_public_market_context(game, cache_path, cutoff=cutoff)
    cached_reference = select_public_market_reference(cached)
    if cached_reference:
        return 200, {
            "status": "available",
            "game_id": game_id,
            "public_market_reference": cached_reference,
            "lookup": {"source": "verified_cache"},
        }

    if searcher is None and not os.environ.get("TAVILY_API_KEY"):
        return 200, {
            "status": "unavailable",
            "game_id": game_id,
            "message": "暂无可核验的公开赛前市场数据。",
            "lookup": {"source": "not_configured"},
        }
    try:
        client = searcher or PregameWebSearch()
        context = client.search(
            game_identity(game),
            cutoff,
            replay=game.get("status") != "scheduled",
            categories={"market"},
        )
        reference = select_public_market_reference(context)
        if reference:
            return 200, {
                "status": "available",
                "game_id": game_id,
                "public_market_reference": reference,
                "lookup": {"source": "web_search"},
            }
    except Exception as exc:
        return 200, {
            "status": "unavailable",
            "game_id": game_id,
            "message": "公开市场参考暂时无法取得。",
            "lookup": {"source": "web_search", "error_type": type(exc).__name__},
        }
    return 200, {
        "status": "unavailable",
        "game_id": game_id,
        "message": "暂无可核验的公开赛前市场数据。",
        "lookup": {"source": "web_search", "records": 0},
    }


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"[25th-second] {self.address_string()} {format % args}")

    def json_response(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/api/current-analyses":
            status, payload = build_prediction_versions_response()
            self.json_response(status, payload)
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path not in {"/api/ai-analysis", "/api/market-reference"}:
            self.json_response(404, {"status": "error", "message": "接口不存在。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError("invalid body length")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            game_id = payload.get("game_id")
            if not isinstance(game_id, str):
                raise ValueError("game_id required")
            request_id = payload.get("request_id")
            if request_id is not None and not isinstance(request_id, str):
                raise ValueError("request_id must be a string")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self.json_response(400, {"status": "error", "message": "请求格式无效。"})
            return
        if self.path == "/api/market-reference":
            status, response = build_market_response(game_id)
        else:
            status, response = build_ai_response(game_id, request_id=request_id)
        self.json_response(status, response)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8625)
    parser.add_argument("--refresh-data", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_local_env()
    if args.refresh_data:
        export_app_data()
    handler = functools.partial(Handler, directory=str(STATIC_ROOT))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"第25秒 running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
