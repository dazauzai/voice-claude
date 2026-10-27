"""A tiny local HTTP endpoint (127.0.0.1, random port) through which the MCP tool server reaches the app."""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

log = logging.getLogger(__name__)


def is_board(path: str) -> bool:
    """The whiteboard page and its API: /board, /board/... or /board?... (not /board_show: that is a tool's request)."""
    return path == "/board" or path.startswith(("/board/", "/board?"))


class ControlServer:
    """POST /<path> with JSON: the tool servers' requests (handler). GET and POST under /board: the whiteboard
    window's page and its API (web: (method, path, headers, body) -> (status, content type, body))."""

    def __init__(self, handler: Callable[[str, dict], dict[str, Any]],
                 web: Callable[[str, str, dict, bytes | None], tuple[int, str, bytes]] | None = None):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _web(self, method: str):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else None
                try:
                    status, ctype, payload = web(method, self.path, dict(self.headers), body) if web else \
                        (404, "text/plain; charset=utf-8", b"no board")
                except Exception as e:
                    log.exception("board request %s failed", self.path)
                    status, ctype, payload = 500, "application/json; charset=utf-8", \
                        json.dumps({"ok": False, "message": f"出错了：{e}"}, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass  # the page went away while it waited

            def do_GET(self):
                if is_board(self.path):
                    return self._web("GET")
                self.send_error(404)

            def do_POST(self):
                if is_board(self.path):
                    return self._web("POST")
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(length) or b"{}")
                    result = handler(self.path.strip("/"), data)
                    status = 200
                except Exception as e:  # report the problem to Claude instead of dropping the request
                    log.exception("control request %s failed", self.path)
                    result, status = {"ok": False, "message": f"出错了：{e}"}, 500
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                log.debug("control: " + fmt, *args)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True, name="control").start()
        log.info("control endpoint on 127.0.0.1:%d", self.port)

    def close(self):
        self.server.shutdown()
