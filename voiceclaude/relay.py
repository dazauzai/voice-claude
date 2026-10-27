"""Replies from the user's other Claude Code sessions (the Desktop app's Code tab, say). A line puts the user's words
into such a session with Claude Code's own SendMessage, after the user said yes out loud; the answer is then read by
the service, not by the line's model, from Claude Code's own files, and handed to the front end as untrusted text.

Read only: ~/.claude/sessions/<pid>.json (the registry of live sessions: sessionId, cwd, name, status; never the
.key files next to them, which hold the pipe's secret) and the target's transcript
~/.claude/projects/<folder slug>/<sessionId>.jsonl. Their formats are Claude Code's own and may change."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import paths

log = logging.getLogger(__name__)

SESSIONS_DIR = paths.CLAUDE_PROJECTS_DIR.parent / "sessions"  # the sessions this service's Claude Code can reach
DEFAULT_SESSIONS_DIR = Path.home() / ".claude" / "sessions"  # where the desktop app's sessions are, by default
QUIET = 3.0  # an answer that ended (end_turn) is complete after this long with nothing new
ANCHOR_CHARS = 40  # how much of our message must be found in the target's transcript


def _strip_ref(to: str) -> str:
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", to or "").strip()


def live_sessions(sessions_dir: Path | None = None) -> list[dict]:
    out = []
    for f in sorted((sessions_dir or SESSIONS_DIR).glob("*.json")):  # *.json only: never the .key files
        try:
            d = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("sessionId"):
            out.append({**d, "_file": str(f)})
    return out


def find_target(to: str, sessions_dir: Path | None = None) -> dict | None:
    """The live session SendMessage's `to` names: its session id (CLI or Desktop), else its name; the newest wins."""
    live = live_sessions(sessions_dir)
    name = _strip_ref(to)
    found = [d for d in live if to in (d.get("sessionId"), d.get("hostSessionId"))] \
        or [d for d in live if str(d.get("name") or "") == name] \
        or [d for d in live if str(d.get("name") or "").casefold() == name.casefold()]
    if not found:
        return None
    return max(found, key=lambda d: d.get("updatedAt") or d.get("startedAt") or 0)


MODE_TAIL = 4 * 1024 * 1024  # how much of a transcript's end is searched for the session's permission mode
_MODE = re.compile(r'"permissionMode"\s*:\s*"([A-Za-z]+)"')


def permission_mode(path: Path | None) -> str:
    """The session's permission mode as its transcript last recorded it (Claude Code notes it with every prompt);
    '' when it cannot be read."""
    if path is None:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - MODE_TAIL))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    found = _MODE.findall(tail)
    return found[-1] if found else ""


def mode_class(mode: str) -> str:
    """Claude Code's mode parity for messages between sessions: one is delivered at once only when sender and
    recipient are of the same class ("bypass" or "prompting"); otherwise the recipient holds it for its user's
    approval, while the sender's SendMessage still reports success. '' when unknown (plan mode can be either)."""
    if mode == "bypassPermissions":
        return "bypass"
    return "" if mode in ("", "plan") else "prompting"


def slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd or "")


def transcript_path(target: dict, projects_dir: Path | None = None) -> Path:
    base = projects_dir or paths.CLAUDE_PROJECTS_DIR
    sid = str(target.get("sessionId") or "")
    p = base / slug(str(target.get("cwd") or "")) / f"{sid}.jsonl"
    if not p.exists():
        found = list(base.glob(f"*/{sid}.jsonl"))
        if found:
            return found[0]
    return p


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _key(text: str) -> str:
    """A message's words only: Claude Code rewrites some markup inside a message it hands over (the envelope's own tag
    becomes <\\ or <\\~ ...), so messages are compared without punctuation and spacing."""
    return re.sub(r"\W+", "", str(text))


def anchor_of(message: str) -> str:
    """The part of our message looked for in the target's transcript: its first line's words."""
    first = next((ln for ln in message.splitlines() if ln.strip()), "")
    return _key(first)[:ANCHOR_CHARS]


def _contains(entry: dict, anchor: str) -> bool:
    return bool(anchor) and any(anchor in _key(s) for s in _strings(entry))


def _received(entry: dict, anchor: str) -> bool:
    """Our message queued in the target (it was busy): proof it arrived, not yet that its Claude has it."""
    return entry.get("type") == "queue-operation" and entry.get("operation") == "enqueue" and _contains(entry, anchor)


def _handover(entry: dict) -> bool:
    """An entry that hands a message to the target's Claude: an injected peer message, or a queued command attached
    mid-turn. Never the queue's own record (the target's work in hand comes after that)."""
    if entry.get("type") == "user":
        return (entry.get("origin") or {}).get("kind") == "peer" or bool(entry.get("isMeta"))
    if entry.get("type") == "attachment":
        return (entry.get("attachment") or {}).get("type") == "queued_command"
    return False


_BLOCK = re.compile(r'<cross-session-message from="([^"]*)"[^>]*>(.*?)</cross-session-message>', re.S)


def _squeeze(text: str) -> str:
    return " ".join(str(text).split())


def _handover_text(entry: dict) -> str:
    """What a hand-over gives the target's Claude, each message once (a queued command also keeps a rendered copy)."""
    if entry.get("type") == "user":
        c = (entry.get("message") or {}).get("content")
        if isinstance(c, list):
            return "\n".join(str(b.get("text") or "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        return c if isinstance(c, str) else ""
    prompt = (entry.get("attachment") or {}).get("prompt")
    return prompt if isinstance(prompt, str) else ""


def _blocks(entry: dict) -> list[tuple[str, str]]:
    """The messages a hand-over carries, in order, as (sender, body): several when the target held them while busy
    and handed them over together."""
    return [(m.group(1), _squeeze(m.group(2))) for m in _BLOCK.finditer(_handover_text(entry))]


def _sender(entry: dict) -> str:
    """Our address as the target sees it (a reply by SendMessage goes to it)."""
    origin = entry.get("origin") or {}
    if origin.get("from"):
        return str(origin["from"])
    for s in _strings(entry):
        m = re.search(r'<cross-session-message from="([^"]+)"', s)
        if m:
            return m.group(1)
    return ""


def _epoch_ms(stamp) -> float:
    try:
        return datetime.fromisoformat(str(stamp)).timestamp() * 1000
    except (ValueError, OverflowError):
        return 0.0


def _new_prompt(entry: dict) -> bool:
    """Someone typed in the target session (not a tool result, not an injected message)."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    c = (entry.get("message") or {}).get("content")
    return isinstance(c, str) or (isinstance(c, list) and not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c))


NOTICE_AFTER = 45.0  # no trace of our message by then: say so once (it may wait for approval in the app)
UNRECEIVED_LIMIT = 600.0  # never reached the target: give up
QUIET_LIMIT = 600.0  # nothing new in the target's transcript this long: give up
HARD_LIMIT = 3 * 3600.0  # still at it after this long: give up, the answer is in that session
UNWATCHED_LIMIT = 300.0  # an approved message whose watch never started (the send never ran) stops counting


@dataclass(eq=False)
class Expected:
    """One message on its way to a session, noted at the spoken yes together with the transcript's size then.
    skip: copies of the same words that older messages still wait for; start: where to read from so those copies are
    seen too; copy: (entry position, block) of the hand-over this message was matched to."""
    key: tuple[str, str]
    offset: int
    skip: int = 0
    start: int = 0
    copy: tuple[int, int] | None = None
    made: float = 0.0
    watched: bool = False


# Per transcript and message words: the messages on their way there, oldest first. A second message with the same
# words to the same session is the next copy; which hand-over is whose is decided by position, once, for all watches.
_EXPECTED: dict[tuple[str, str], list[Expected]] = {}
_CLAIMED: dict[tuple[str, str], set[tuple[int, int]]] = {}  # hand-overs matched, kept while any of those words waits
_EXPECTED_LOCK = threading.Lock()


def expect(path: Path, message: str, offset: int, now: Callable[[], float] = time.monotonic) -> Expected:
    """Called at the spoken yes, with the transcript's size at that moment."""
    k = (str(path), _key(message))
    with _EXPECTED_LOCK:
        alive = [e for e in _EXPECTED.get(k, []) if e.watched or now() - e.made < UNWATCHED_LIMIT]
        older = [e for e in alive if e.copy is None]
        e = Expected(k, offset, len(older), min([o.offset for o in older] + [offset]), made=now())
        _EXPECTED[k] = alive + [e]
    return e


def forget(expected: Expected | None) -> None:
    """The message is no longer waited for (answered, given up, or never sent). A copy of it that still turns up is
    taken for what it is: an older one (see Watch)."""
    if expected is None:
        return
    with _EXPECTED_LOCK:
        lst = _EXPECTED.get(expected.key, [])
        if expected in lst:
            lst.remove(expected)
        if not lst:
            _EXPECTED.pop(expected.key, None)
            _CLAIMED.pop(expected.key, None)


class Watch:
    """Read the target's answer to our message: what its Claude writes after the entry that hands our message to it.
    The answer is its replies to us by SendMessage (Claude Code tells a receiver to answer that way) or, if none,
    its text. Complete when the target is idle again in the registry (a status newer than our message) or its turn
    ended, with nothing new for QUIET seconds. No fixed limit while it keeps working: it gives up after QUIET_LIMIT
    with nothing new (HARD_LIMIT at most), or UNRECEIVED_LIMIT when our message never shows up there.

    Which hand-over is ours: the one carrying our msg_id when Claude Code gives one (its direct route), else by words
    and place. Copies of the same words go to the messages in the order they were approved; a copy whose message is
    no longer waited for (it gave up) is passed over for our own; in a batch handed over together, the last of ours
    answers for all of them. A later message from us ends our part at once (its own watch reads the rest), so one
    answer is never read out twice.

    run() -> {text, done, session, why}; why: "" when done ("superseded" when a later message of ours took over),
    else "unreceived", "timeout" (nothing more came), "working" (still busy when we stopped), "gone" (the session
    closed) or "stopped". on_state("unreceived") once when there is no trace of our message after NOTICE_AFTER s."""

    def __init__(self, target: dict, path: Path, offset: int, message: str, *, quiet: float = QUIET,
                 notice_after: float | None = NOTICE_AFTER, unreceived_limit: float = UNRECEIVED_LIMIT,
                 quiet_limit: float = QUIET_LIMIT, hard_limit: float = HARD_LIMIT, poll: float = 0.5,
                 on_state: Callable[[str], None] | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, stop: threading.Event | None = None,
                 expected: Expected | None = None, msg_id: str = ""):
        self.target, self.path, self.anchor = target, path, anchor_of(message)
        self.body, self.key, self.msg_id = _squeeze(message), _key(message), str(msg_id or "")
        self.expected = expected or expect(path, message, offset)
        self.expected.watched = True
        self.pos, self.skip = self.expected.start, self.expected.skip
        self.quiet, self.notice_after, self.unreceived_limit = quiet, notice_after, unreceived_limit
        self.quiet_limit, self.hard_limit, self.poll = quiet_limit, hard_limit, poll
        self.on_state, self.clock, self.sleep = on_state, clock, sleep
        self.stop = stop or threading.Event()
        self._buf = b""

    def _new_entries(self) -> list[tuple[int, dict]]:
        """The complete lines written since the last look, as (position in the file, entry)."""
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return []
        base, buf = self.pos - len(self._buf), self._buf + data
        self.pos += len(data)
        out, at = [], 0
        while True:
            nl = buf.find(b"\n", at)
            if nl < 0:
                break
            try:
                d = json.loads(buf[at:nl].decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                d = None
            if isinstance(d, dict):
                out.append((base + at, d))
            at = nl + 1
        self._buf = buf[at:]
        return out

    def _status(self) -> tuple[str, float]:
        f = self.target.get("_file")
        try:
            d = json.loads(Path(f).read_text("utf-8")) if f else {}
        except (OSError, ValueError):
            return "", 0.0
        return str(d.get("status") or ""), float(d.get("statusUpdatedAt") or 0)

    def _ours(self, blocks: list[tuple[str, str]]) -> list[int]:
        return [i for i, (_, body) in enumerate(blocks) if body == self.body or _key(body) == self.key]

    def _copies(self, entry: dict, blocks: list[tuple[str, str]]) -> list[int]:
        """The blocks of a hand-over that carry our words (no blocks: a format we do not know, matched by the first line)."""
        return self._ours(blocks) if blocks else [0] if _contains(entry, self.anchor) else []

    def _free(self, at: int, copies: list[int]) -> tuple[list[int], bool]:
        """(the copies no other message has been matched to, whether another message of these words still waits)"""
        with _EXPECTED_LOCK:
            others = [e for e in _EXPECTED.get(self.expected.key, []) if e is not self.expected]
            taken = _CLAIMED.get(self.expected.key, set()) - {self.expected.copy}
            return [i for i in copies if (at, i) not in taken], any(e.copy is None for e in others)

    def _pick(self, at: int, entry: dict, blocks: list[tuple[str, str]], first: bool) -> int | None:
        """Our copy in this hand-over, or None. first: before we have one (older messages' copies come first)."""
        origin = str((entry.get("origin") or {}).get("msg_id") or "")
        if self.msg_id and origin:
            if origin != self.msg_id:
                return None  # another message (of ours or anyone's), whatever its words
            mine = self._ours(blocks)
            return mine[-1] if mine else 0
        copies = self._copies(entry, blocks)
        if first:
            taken = min(self.skip, len(copies))
            self.skip -= taken
            copies = copies[taken:]
        free, others_wait = self._free(at, copies)
        if not free or (not first and others_wait):
            return None  # (later: a copy another message of these words is still waiting for is that one's)
        return free[0] if others_wait else free[-1]

    def run(self) -> dict:
        try:
            return self._run()
        finally:
            forget(self.expected)

    def _run(self) -> dict:
        start = last = self.clock()
        received = anchored = noted = ended = closed = False
        anchor_ms, sender = 0.0, ""
        texts: list[str] = []
        replies: list[tuple[str, str]] = []  # (to, message) of the target's SendMessage calls
        registry = Path(self.target["_file"]) if self.target.get("_file") else None

        def result(done: bool, why: str = "") -> dict:
            to_us = [m for to, m in replies if not sender or _strip_ref(to) == _strip_ref(sender)]
            why = why or ("superseded" if done and closed else "")
            return {"text": "\n\n".join(to_us or texts), "done": done, "session": self.target.get("sessionId"), "why": why}

        while not self.stop.is_set():
            gone = registry is not None and not registry.exists()  # checked before the last read of the transcript
            entries = self._new_entries()
            if entries:
                last = self.clock()
            for at, e in entries:
                handover = _handover(e)
                blocks = _blocks(e) if handover else []
                if handover and anchored and sender and (any(frm == sender for frm, _ in blocks) or _sender(e) == sender):
                    pick = self._pick(at, e, blocks, first=False)  # our own copy after all (we took an older one first)?
                    if pick is None:
                        closed = True  # our next message reached it: the rest is that one's answer
                        return result(True)
                    anchored = False  # take this one instead
                if not anchored:
                    pick = self._pick(at, e, blocks, first=True) if handover else None
                    if pick is not None:
                        anchored, received, ended, texts, replies = True, True, False, [], []
                        with _EXPECTED_LOCK:
                            self.expected.copy = (at, pick)
                            _CLAIMED.setdefault(self.expected.key, set()).add((at, pick))
                        anchor_ms = _epoch_ms(e.get("timestamp")) or time.time() * 1000
                        sender = (blocks[pick][0] if pick < len(blocks) else "") or _sender(e)
                        if blocks and any(frm == sender for frm, _ in blocks[pick + 1:]):
                            closed = True  # handed over together with later ones of ours: the last takes the answer
                            return result(True)
                    elif _received(e, self.anchor):
                        received = True
                    continue
                if ended and _new_prompt(e):  # the user went on in that session: our answer is complete
                    return result(True)
                if e.get("type") != "assistant" or e.get("isSidechain"):
                    continue
                if 0 < _epoch_ms(e.get("timestamp")) < anchor_ms:
                    continue  # written out late, but from before our message reached it
                m = e.get("message") or {}
                for b in m.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and str(b.get("text") or "").strip():
                        texts.append(str(b["text"]).strip())
                    elif b.get("type") == "tool_use" and b.get("name") == "SendMessage":
                        inp = b.get("input") or {}
                        if str(inp.get("message") or "").strip():
                            replies.append((str(inp.get("to") or ""), str(inp["message"]).strip()))
                ended = m.get("stop_reason") == "end_turn"
            now = self.clock()
            if anchored and now - last >= self.quiet:
                status, since = self._status()
                if ended or (status == "idle" and since >= anchor_ms):
                    return result(True)
            if gone:
                log.info("relay: %s closed while we waited for its answer", self.target.get("name"))
                return result(True) if anchored and ended else result(False, "gone")
            if not received:
                if not noted and self.notice_after is not None and now - start >= self.notice_after:
                    noted = True
                    if self.on_state:
                        self.on_state("unreceived")
                if now - start >= self.unreceived_limit:
                    return result(False, "unreceived")
            elif now - last >= self.quiet_limit or now - start >= self.hard_limit:
                busy = self._status()[0] == "busy"
                log.info("relay: giving up on %s (%s)", self.target.get("name"), "still busy" if busy else "quiet")
                return result(False, "working" if busy or now - last < self.quiet_limit else "timeout")
            self.sleep(self.poll)
        return result(False, "stopped")
