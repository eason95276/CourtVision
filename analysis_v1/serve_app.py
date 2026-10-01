from __future__ import annotations

import argparse
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.export_app_data import export_app_data


APP_ROOT = Path(__file__).resolve().parent / "app" / "static"


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"[courtvision-app] {self.address_string()} {format % args}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the local CourtVision V1 app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    export_app_data()
    handler = functools.partial(Handler, directory=str(APP_ROOT))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"CourtVision V1 running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

