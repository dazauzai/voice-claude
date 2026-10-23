"""A tiny local HTTP endpoint (127.0.0.1, random port) through which the MCP tool server reaches the app."""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

log = logging.getLogger(__name__)


class ControlServer:
    def __init__(self, handler: Callable[[str, dict], dict[str, Any]]):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
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
