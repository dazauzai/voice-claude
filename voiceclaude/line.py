"""One line = one Claude Code conversation for one project, with a state and a mailbox for what happened while
the user was not listening."""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

STARTING, IDLE, WORKING, WAITING, DEAD, CLOSED = "starting", "idle", "working", "waiting", "dead", "closed"
_SENTENCE_END = re.compile(r"[。！？!?；;\n]")


def last_sentence(text: str, limit: int = 80) -> str:
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]
    s = parts[-1] if parts else text.strip()
    return s[:limit]


def classify_done(status: str, reply: str) -> str | None:
    """What a finished turn means for someone who was not listening: 'error' / 'needs_you' / 'report' / None."""
    if status == "interrupted":
        return None
    if status == "error":
        return "error"
    tail = reply.rstrip()
    if tail.endswith(("？", "?")):
        return "needs_you"
    return "report"


class Line:
    """session_factory(line, resume) -> an object with start/send/interrupt/close/kill/restart and that calls
    line.on_session(kind, data) for its events (ClaudeSession fits)."""

    def __init__(self, id: str, name: str, cwd: str, kind: str, session_factory: Callable[["Line", str], Any],
                 on_event: Callable[[str, str, dict], None], now: Callable[[], float] = time.monotonic):
        self.id, self.name, self.cwd, self.kind = id, name, cwd, kind
        self.session_factory = session_factory
        self.on_event = on_event
        self.now = now
        self.session: Any = None
        self.session_id = ""
        self.state = STARTING
        self.mailbox: list[dict] = []
        self.reply = ""  # text of the current / last turn
        self.last_text = ""
        self.busy_since: float | None = None
        self.last_used = now()
        self.turns = 0
        self.provider = "claude"  # who answers on this line; the switchboard sets it before start()
        self.engine = "claude"  # "claude" (Claude Code) or "codex" (the user's own Codex), chosen when the session is made
        self.pending_model: str | None = None  # applied once the current turn is over
        self.pending_provider: str | None = None
        self.pending_restart = False
        self.first_turn_after_switch = False  # the first turn on a new provider may fail to resume: then start afresh once
        self.started_resume = ""  # the conversation id this process was started to continue ("" = a new one)
        self.ready_seen = False  # the process has accepted a message since it was started
        self.started_at = 0.0
        self.last_said = ""  # the user's latest words, sent again if the process died before answering them

    # ---------- lifecycle ----------

    def start(self, resume: str = "") -> None:
        """Launch the Claude process. It is usable at once: stdin is buffered, and Claude Code only reports
        'ready' (system/init) AFTER it has accepted the first message, so nothing may wait for that event."""
        self.started_resume, self.ready_seen, self.started_at = resume or "", False, self.now()
        self.session = self.session_factory(self, resume)
        self.session.start()
        self.state = IDLE

    def say(self, text: str) -> bool:
        """Send the user's words to Claude now."""
        self.last_used = self.now()
        self.reply = ""
        self.last_said = text
        self.state = WORKING
        self.busy_since = self.now()
        self.turns += 1
        self.session.send(text)
        return True

    def interrupt(self) -> None:
        if self.session is not None:
            self.session.interrupt()

    def kill_and_resume(self) -> None:
        """The emergency brake: kill the process tree, reconnect to the same conversation."""
        if self.session is None:
            return
        self.session.kill()
        try:
            self.session.restart()
            self.state = IDLE
        except Exception:
            log.exception("[%s] could not resume after kill", self.id)
            self.state = DEAD

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
        self.state = CLOSED

    @property
    def busy(self) -> bool:
        return self.state == WORKING

    @property
    def keep_alive(self) -> bool:
        """Lines that must not be closed by the idle/limit rules."""
        return self.state in (STARTING, WORKING, WAITING) or bool(self.unread())

    # ---------- mailbox ----------

    def unread(self) -> list[dict]:
        return [m for m in self.mailbox if not m["read"]]

    def mark_read(self) -> None:
        for m in self.mailbox:
            m["read"] = True

    def post(self, kind: str, text: str) -> dict:
        item = {"kind": kind, "text": text, "time": time.time(), "read": False}
        self.mailbox.append(item)
        del self.mailbox[:-50]
        return item

    def summary_for_return(self) -> str:
        """Spoken when the user comes back: what happened meanwhile."""
        items = self.unread()
        if not items:
            return ""
        last = items[-1]["text"]
        if len(items) == 1:
            return f"你不在的时候，{self.name}说：{last}"
        return f"你不在的时候，{self.name}有 {len(items)} 条消息，最后一条：{last}"

    def status_text(self) -> str:
        word = {STARTING: "正在启动", IDLE: "闲着", WORKING: "在干活", WAITING: "等你回答", DEAD: "已退出", CLOSED: "已关闭"}[self.state]
        s = f"{self.name}{word}"
        if self.state == WORKING and self.busy_since is not None:
            s += f"（{int(self.now() - self.busy_since)} 秒）"
        if self.unread():
            s += f"，{len(self.unread())} 条没听的消息"
        return s

    # ---------- events from the session ----------

    def on_session(self, kind: str, data: Any) -> None:
        if kind == "ready":
            # Arrives after every message in stream-json mode (not at process start); the first one marks the
            # moment Claude Code accepted the first message.
            first = not self.session_id
            self.session_id = data.get("session_id") or self.session_id
            self.ready_seen = True
            if self.state == STARTING:
                self.state = IDLE
            self.on_event(self.id, "ready", {"session_id": self.session_id, "first": first})
        elif kind == "text":
            self.reply += data
            self.last_text = self.reply
            self.on_event(self.id, "text", {"text": data})
        elif kind == "tool":
            inp = data.get("input", {})
            detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("description") \
                or inp.get("name") or inp.get("target") or inp.get("text") or ""
            self.on_event(self.id, "tool", {"name": data.get("name", ""), "detail": str(detail)[:120]})
        elif kind == "done":
            status = data.get("status", "ok")
            last = last_sentence(data.get("message") or self.reply)
            what = classify_done(status, data.get("message") or self.reply)
            self.state = WAITING if what == "needs_you" else IDLE
            self.busy_since = None
            if what and self.kind != "manager":  # the manager's replies are conversation, not news to keep
                self.post(what, last if what != "error" else f"出错了：{(data.get('message') or '')[:80]}")
            self.on_event(self.id, "done", {"status": status, "last": last, "kind": what})
        elif kind == "exit":
            busy = self.state in (WORKING, WAITING)  # the user is waiting for an answer that will never come
            self.state = DEAD
            self.busy_since = None
            self.post("error", "程序退出了")
            self.on_event(self.id, "dead", {"code": data, "busy": busy})
