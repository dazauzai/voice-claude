"""One line = one Claude Code conversation for one project, with a state and a mailbox for what happened while
the user was not listening."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import uuid
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


FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}  # the tools whose edits file checkpointing can undo


def file_hash(path: str) -> str | None:
    """A content fingerprint, to tell later whether a file was changed after Claude last wrote it (None: unreadable,
    missing, or too big to bother)."""
    try:
        if os.path.getsize(path) > 50 * 1024 * 1024:
            return None
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


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
        self.turns_since_record = 0  # the user's turns since this line last wrote a record (records.py)
        self.record_asked = False  # nudged since; a new turn of the user's clears it
        self.pending_nudged = False  # nudged for the model / provider switch waiting to be made
        self.quiet, self.nudged_at = False, 0.0  # a turn of the service's own is running: nothing of it shown or said
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
        # One record per user turn of the current conversation, for rewinding: our id, the words, the engine's ids
        # (Claude Code: the prompt's uuid and the turn's last transcript entry; Codex: the turn id), files changed.
        self.turn_log: list[dict] = []
        self.log_from_start = True  # turn_log reaches back to the conversation's first message
        self.base_point: dict | None = None  # where a resumed conversation stood before this process's first message
        self.rewinding = False  # a rewind is running: no new words until it is done
        self.pending_note = ""  # said to the model with the user's next words (a rewind that only half worked)
        self.sending = False  # inside session.send (a Codex send waits there until its thread is ready)
        self._say_seq = 0

    # ---------- lifecycle ----------

    def start(self, resume: str = "") -> None:
        """Launch the Claude process. It is usable at once: stdin is buffered, and Claude Code only reports
        'ready' (system/init) AFTER it has accepted the first message, so nothing may wait for that event."""
        self.started_resume, self.ready_seen, self.started_at = resume or "", False, self.now()
        self.turn_log = []  # turns of an earlier process are not known here, so they cannot be rewound
        self.log_from_start, self.base_point = not resume, None
        self.session = self.session_factory(self, resume)
        self.session.start()
        self.state = IDLE

    def say(self, text: str, turn: str | None = None) -> bool:
        """Send the user's words to Claude now. `turn` is the switchboard's id for this user message. If the session
        refuses (it is restarting), nothing is kept: no record entry, the state and the note for the model as before."""
        entry = {"turn": turn or "t" + uuid.uuid4().hex[:10], "text": text, "prompt": None, "last": None,
                 "codex_turn": None, "files": set(), "created": set(), "hashes": {}}
        before = (self.state, self.busy_since, self.reply, self.last_said)
        self._say_seq += 1
        mine = self._say_seq
        self.turn_log.append(entry)  # before sending: the engine's ids for it may come back at once
        self.reply = ""
        self.last_said = text
        self.state = WORKING
        self.busy_since = self.now()
        self.sending = True
        if self.quiet and not getattr(self.session, "turn_active", False):
            self.quiet = False  # a quiet turn that never ended (its process went away): this is the user's turn
        try:
            self.session.send(f"{self.pending_note}\n{text}" if self.pending_note else text)
        except Exception:
            self.turn_log[:] = [e for e in self.turn_log if e is not entry]
            if self._say_seq == mine:  # nothing said since: undo this call's own changes
                self.sending = False
                if self.state == WORKING:  # a DEAD from the process going away meanwhile stays
                    self.state, self.busy_since = before[0], before[1]
                self.reply, self.last_said = before[2], before[3]
            raise
        self.sending = False
        self.pending_note = ""
        self.last_used = self.now()
        self.turns += 1
        self.turns_since_record += 1
        self.record_asked = False
        if len(self.turn_log) > 200:
            del self.turn_log[:-200]
            self.log_from_start, self.base_point = False, None  # the oldest turns are no longer known
        return True

    def interrupt(self) -> None:
        if self.session is not None:
            self.session.interrupt()

    def kill_and_resume(self) -> None:
        """The emergency brake: kill the process tree, reconnect to the same conversation."""
        if self.session is None:
            return
        self.session.kill()
        self.quiet = False
        try:
            self.session.restart()
            self.state = IDLE
        except Exception:
            log.exception("[%s] could not resume after kill", self.id)
            self.state = DEAD

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
        self.state, self.quiet = CLOSED, False

    def nudge(self, text: str) -> bool:
        """A quiet turn of the service's own (the record nudge): nothing of it is shown, read out or kept as news, and
        it is no turn of the user's (no rewind point, not counted). Only when the line is idle."""
        if self.session is None or self.state != IDLE:
            return False
        self.quiet, self.nudged_at = True, self.now()
        self.state, self.busy_since = WORKING, self.now()
        try:
            self.session.send(text)
        except Exception:
            self.quiet, self.state, self.busy_since = False, IDLE, None
            return False
        return True

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
        if self.quiet and kind in ("text", "tool", "writing"):
            return  # the service's own turn: not the user's conversation
        if self.quiet and kind == "mark":
            # its end moves the last turn's end, so a rewind of the next sentence drops that sentence alone; a rewind
            # of the last turn drops the quiet turn with it (two turns: the switchboard's rewind knows)
            if self.turn_log and data.get("last"):
                self.turn_log[-1]["last"] = data["last"]
                self.turn_log[-1]["quiet_after"] = True
            return
        if self.quiet and kind == "done":
            self.quiet = False
            if self.state != DEAD and not self.sending:  # (the user may have spoken meanwhile: their turn is next)
                self.state = IDLE
                self.busy_since = None
            self.on_event(self.id, "done", {"status": data.get("status", "ok"), "last": "", "kind": None, "quiet": True})
            return
        if kind == "ready":
            # Arrives after every message in stream-json mode (not at process start); the first one marks the
            # moment Claude Code accepted the first message.
            first = not self.session_id
            self.session_id = data.get("session_id") or self.session_id
            self.ready_seen = True
            if data.get("base_turn") and not self.log_from_start and self.base_point is None \
                    and not any(e.get("codex_turn") for e in self.turn_log):
                # where a resumed Codex thread stood; a sentence said while it was being resumed has no Codex turn yet
                self.base_point = {"codex_turn": data["base_turn"]}
            if self.state == STARTING:
                self.state = IDLE
            self.on_event(self.id, "ready", {"session_id": self.session_id, "first": first})
        elif kind == "text":
            self.reply += data
            self.last_text = self.reply
            self.on_event(self.id, "text", {"text": data})
        elif kind == "mark":
            # the engine's ids for the current turn (Claude Code prompt / last entry uuid, Codex turn id)
            if self.turn_log:
                e = self.turn_log[-1]
                for k in ("prompt", "codex_turn"):
                    if data.get(k) and not e[k]:
                        e[k] = data[k]
                if data.get("last"):
                    e["last"] = data["last"]
        elif kind == "tool":
            inp = data.get("input", {})
            if data.get("name") in FILE_TOOLS and self.turn_log:
                path = inp.get("file_path") or inp.get("notebook_path")
                e = self.turn_log[-1]
                for f in inp.get("files") or ([path] if path else []):
                    full = os.path.join(self.cwd, f)
                    if f not in e["files"] and not os.path.exists(full):
                        e.setdefault("created", set()).add(f)  # the tool call comes before it runs: a new file
                    e["files"].add(f)
            detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("description") \
                or inp.get("name") or inp.get("target") or inp.get("text") or ""
            self.on_event(self.id, "tool", {"name": data.get("name", ""), "detail": str(detail)[:120]})
        elif kind == "done":
            status = data.get("status", "ok")
            last = last_sentence(data.get("message") or self.reply)
            what = classify_done(status, data.get("message") or self.reply)
            if self.state != DEAD:  # a turn ending after its process went away: the line stays dead (and reconnects)
                self.state = WAITING if what == "needs_you" else IDLE
            self.busy_since = None
            if self.turn_log:  # how the files this turn wrote look now: later edits by anyone else can be told apart
                e = self.turn_log[-1]
                e["hashes"] = {f: file_hash(os.path.join(self.cwd, f)) for f in e["files"]}
            if what and self.kind != "manager":  # the manager's replies are conversation, not news to keep
                self.post(what, last if what != "error" else f"出错了：{(data.get('message') or '')[:80]}")
            self.on_event(self.id, "done", {"status": status, "last": last, "kind": what})
        elif kind == "usage":
            self.on_event(self.id, "usage", {**data, "turn": self.turn_log[-1]["turn"] if self.turn_log else None})
        elif kind in ("limits", "gated_result", "writing"):
            self.on_event(self.id, kind, data)
        elif kind == "rewind_failed":
            self.on_event(self.id, "rewind_failed", data or {})
        elif kind == "exit":
            busy = self.state in (WORKING, WAITING)  # the user is waiting for an answer that will never come
            self.state, self.quiet = DEAD, False  # a quiet turn that went with the process never ends
            self.busy_since = None
            self.post("error", "程序退出了")
            self.on_event(self.id, "dead", {"code": data, "busy": busy})
