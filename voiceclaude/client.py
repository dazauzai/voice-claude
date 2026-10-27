"""The front end's only door to the background service. See docs/protocol.md."""
from __future__ import annotations

import json
import os
import logging
import queue
import socket
import threading
import time
from typing import Any, Callable

from . import daemon as daemonmod

log = logging.getLogger(__name__)

COMMAND_TIMEOUT = 15.0


class DaemonClient:
    def __init__(self, on_event: Callable[[str, dict], None]):
        self.on_event = on_event
        self.sock: socket.socket | None = None
        self.wfile = None
        self.lock = threading.Lock()
        self.waiting: dict[int, dict] = {}
        self.next_id = 0
        self.active = ""
        self.connected = False
        self.closing = False
        self.said_bye = False  # the service's own 'bye' arrived: the reader must not add a second one
        self.info: dict = {}
        # Events are handed to on_event on their own thread, so a handler may call commands (answer, reply,
        # say ...) without deadlocking the socket reader.
        self.events: queue.Queue = queue.Queue()
        threading.Thread(target=self._dispatch, daemon=True, name="daemon-events").start()

    # ---------- connection ----------

    def connect(self, start_if_missing: bool = True, timeout: float = 60.0) -> dict:
        info = daemonmod.read_info()
        if info is None and start_if_missing:
            log.info("starting the background service")
            daemonmod.spawn_detached()
            deadline = time.time() + timeout
            while info is None and time.time() < deadline:
                time.sleep(0.2)
                info = daemonmod.read_info()
        if info is None:
            raise ConnectionError("后台服务起不来（看 ~/.voice-claude/daemon.log）")
        return self.connect_to(int(info["port"]), str(info["token"]))

    def connect_to(self, port: int, token: str) -> dict:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.settimeout(None)
        self.sock = sock
        self.wfile = sock.makefile("w", encoding="utf-8", newline="\n")
        rfile = sock.makefile("r", encoding="utf-8")
        self._send({"hello": token, "role": "frontend", "pid": os.getpid()})
        reply = json.loads(rfile.readline() or "{}")
        if not reply.get("ok"):
            sock.close()
            raise ConnectionError(reply.get("message") or "后台服务拒绝了连接")
        self.info = reply
        self.active = str(reply.get("active", ""))
        self.connected = True
        self.closing = False
        self.said_bye = False
        threading.Thread(target=self._reader, args=(rfile,), daemon=True, name="daemon-client").start()
        return reply

    def close(self) -> None:
        self.closing = True
        self.connected = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    # ---------- commands ----------

    def switch(self, line: str) -> dict:
        return self._call("switch", line=line)

    # The service's say waits for the line to take the words: a Codex line up to 90 s for its thread, a Claude Code
    # line up to 30 s for an interrupted turn. Giving up sooner would report "没发出去" while it still goes out.
    SAY_TIMEOUT = 120.0

    def say(self, line: str, text: str) -> dict:
        return self._call("say", timeout=self.SAY_TIMEOUT, line=line, text=text)

    def interrupt(self, line: str) -> dict:
        return self._call("interrupt", line=line)

    def usage(self) -> dict:
        """Token use and quotas: ok, today / since_start ({provider: totals}), limits ({provider: snapshot or None}),
        last_turn ({line: its latest usage event})."""
        return self._call("usage")

    def rewind(self, line: str = "", turn: str | None = None, steps: int = 1) -> dict:
        """Back to just before a sentence: `turn` (from say's reply) and everything after it, or the last `steps`
        user turns. May ask out loud whether to put changed files back, so it can take a while: call it off the UI
        thread. Reply: ok, message (spoken), text (the first dropped sentence), files_restored."""
        return self._call("rewind", timeout=120.0, line=line, turn=turn, steps=steps)

    def stop_all(self) -> dict:
        return self._call("stop_all")

    def answer(self, request: str, yes: bool, text: str = "") -> dict:
        return self._call("answer", request=request, yes=bool(yes), text=text)

    def phrase(self, key: str) -> dict:
        return self._call("phrase", key=key)

    def status(self) -> dict:
        return self._call("status")

    # ---------- access (how the daemon reaches Claude); used by the front end's AI 服务商 page ----------

    def access_status(self) -> dict:
        return self._call("access_status")

    def set_api_key(self, key: str) -> dict:
        return self._call("set_api_key", key=key)

    def clear_api_key(self) -> dict:
        return self._call("clear_api_key")

    def test_access(self) -> dict:
        """Starts a real claude process once (5-20 s); the daemon answers within its own 90 s limit, so call it off
        the UI thread."""
        return self._call("test_access", timeout=120.0)

    # ---------- providers (Claude / DeepSeek / Kimi / GPT): the AI 服务商 page, see protocol.md 7b ----------

    def list_providers(self) -> dict:
        return self._call("list_providers")

    def set_provider_key(self, provider: str, key: str, switch: bool = False) -> dict:
        """switch=True: save and make it the default right away (the page's "save and use")."""
        return self._call("set_provider_key", provider=provider, key=key, switch=bool(switch))

    def clear_provider_key(self, provider: str) -> dict:
        return self._call("clear_provider_key", provider=provider)

    def test_provider(self, provider: str) -> dict:
        """Starts a real claude process (and, for openai, the translation gateway) once: 5-40 s, off the UI thread."""
        return self._call("test_provider", provider=provider, timeout=240.0)  # gateway start + a real turn

    def switch_provider(self, provider: str, line: str = "") -> dict:
        """line="" = the default (new lines; open lines follow after their turns); a line id = that line only."""
        return self._call("switch_provider", provider=provider, line=line)

    def login_provider(self, provider: str, mode: str = "browser") -> dict:
        """Run the vendor's own login (claude auth login / codex login). mode "device" returns a code and url to
        show and read out; "browser" opens the browser. Off the UI thread (up to ~20 s)."""
        return self._call("login_provider", provider=provider, mode=mode, timeout=60.0)

    def logout_provider(self, provider: str) -> dict:
        return self._call("logout_provider", provider=provider, timeout=60.0)

    def reply(self, request: str, result: dict) -> None:
        self._send({"cmd": "reply", "request": request, "result": result})

    def shutdown(self, jobs: str = "ask", lines_confirmed: bool = False) -> dict:
        """jobs: "ask" = the service asks out loud about running background jobs (an 'ask' event follows);
        "stop" = stop them and exit; "keep" = leave them running and exit. The window asks with its own dialog.
        lines_confirmed: the window already asked about lines that are mid-turn; otherwise the service asks."""
        return self._call("shutdown", jobs=jobs, lines_confirmed=lines_confirmed)

    # ---------- plumbing ----------

    def _send(self, obj: dict) -> None:
        if self.wfile is None:
            raise ConnectionError("没有连上后台服务")
        with self.lock:
            try:
                self.wfile.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self.wfile.flush()
            except (OSError, ValueError) as e:
                self.connected = False
                raise ConnectionError("后台服务的连接断了") from e

    def _call(self, cmd: str, timeout: float = COMMAND_TIMEOUT, **kw: Any) -> dict:
        if not self.connected:
            raise ConnectionError("没有连上后台服务")
        with self.lock:
            self.next_id += 1
            cid = self.next_id
            slot = {"event": threading.Event(), "result": None}
            self.waiting[cid] = slot
        try:
            self._send({"id": cid, "cmd": cmd, **kw})
            if not slot["event"].wait(timeout):
                raise TimeoutError(f"后台服务 {timeout:.0f} 秒没有回应（{cmd}）")
        finally:
            with self.lock:
                self.waiting.pop(cid, None)
        if slot["result"] is None:
            raise ConnectionError("后台服务的连接断了")
        return slot["result"]

    def _dispatch(self) -> None:
        while True:
            kind, data = self.events.get()
            try:
                self.on_event(kind, data)
            except Exception:
                log.exception("event handler failed: %s", kind)

    def _reader(self, rfile) -> None:
        try:
            for line in rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "event" in d:
                    kind = d.pop("event")
                    if kind == "line" and d.get("active"):
                        self.active = str(d.get("line", ""))
                    if kind == "bye":
                        self.said_bye = True
                    self.events.put((kind, d))
                    continue
                cid = d.pop("id", None)
                with self.lock:
                    slot = self.waiting.get(cid)
                if slot:
                    slot["result"] = d
                    slot["event"].set()
        except (OSError, ValueError):
            pass
        finally:
            was = self.connected
            self.connected = False
            with self.lock:
                for slot in self.waiting.values():
                    slot["event"].set()
            if was and not self.closing and not self.said_bye:  # one bye only: the service's own, when it said one
                self.events.put(("bye", {"reason": "后台服务的连接断了"}))
