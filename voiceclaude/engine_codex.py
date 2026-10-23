"""Codex (OpenAI) as a second engine: one `codex app-server` process per line, JSON-RPC 2.0 over stdio, driven
to look exactly like a ClaudeSession to the Line that owns it (events ready / text / tool / done / exit).

The user signs in with `codex login` themselves and Codex keeps the tokens in ~/.codex; we never read or write
that directory (account facts come from the app-server's account/read). Checked with scripts/probe_codex.py
against codex-cli 0.163.0.

How Codex's own powers meet our spoken gate:
- approvalPolicy on-request + sandbox workspace-write: commands the sandbox allows run on their own (as Claude
  Code's auto mode does); anything that needs more (outside the workspace, the network, a file change outside
  the writable roots) arrives as a server request and goes through `ask` -> the switchboard's gate
  (permissions codex_shell / codex_edit). Never acceptForSession, never `never`, never danger-full-access.
- Codex asks the client before EVERY MCP tool call (mcpServer/elicitation/request, codex_approval_kind
  mcp_tool_call): our own servers gate themselves out loud, so they are accepted; any other server is declined.
- The user's own MCP servers and Codex's computer-use / browser-use / apps features are switched off for our
  processes only (-c mcp_servers={} plus --disable ...): the desktop is reached through our desktop tools alone.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__, access, paths, testing
from .config import Config

log = logging.getLogger(__name__)

OUR_SERVERS = ("voiceclaude", "desktop", "manager")
DISABLED_FEATURES = ("computer_use", "browser_use", "apps")
HANDSHAKE_TIMEOUT = 60.0
READY_TIMEOUT = 90.0  # send() waits this long for the thread to exist (MCP servers start inside thread/start)
TURN_STATUS = {"completed": "ok", "interrupted": "interrupted", "failed": "error"}


def find_codex() -> str | None:
    """bin\\codex.exe of the official package first, then a codex.exe on PATH. Never a script shim (npm's codex.cmd or
    codex.ps1): an extra cmd.exe between us and the server would orphan it on exit."""
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Codex" / "bin" / "codex.exe"
    if local.is_file():
        return str(local)
    found = shutil.which("codex.exe")
    return found if found and found.lower().endswith(".exe") else None


def codex_shim() -> str:
    """A `codex` on PATH that is only a script shim (npm i -g @openai/codex), which we do not run; '' if none."""
    found = shutil.which("codex")
    return found if found and not found.lower().endswith(".exe") else ""


def login_status(exe: str | None = None, timeout: float = 15.0) -> tuple[bool, str]:
    """`codex login status`: (logged in, the line it printed). Never run under a unit-test runner."""
    exe = exe or find_codex()
    if not exe:
        return False, "没装 Codex"
    if testing.under_test_runner():
        return False, testing.REFUSAL
    try:
        r = subprocess.run([exe, "login", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return False, f"codex login status 跑不起来：{e}"
    text = (r.stdout or r.stderr or "").strip().splitlines()
    return r.returncode == 0, (text[0] if text else "")


class StdioTransport:
    """The real thing: a codex app-server child, one JSON object per line each way."""

    def __init__(self, args: list[str], cwd: str, env: dict[str, str], stderr_path: Path | None = None):
        err = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def send(self, line: str) -> None:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def readline(self) -> str | None:
        line = self.proc.stdout.readline()
        return line if line else None

    def poll(self):
        return self.proc.poll()

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=3)
            except Exception:
                self.kill()

    def kill(self) -> None:
        """The whole tree: the server, its MCP servers, the sandbox helpers and any command they run."""
        if self.proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass


class CodexSession:
    """Same surface as ClaudeSession: start / send / interrupt / close / kill / restart; events through on_event."""

    def __init__(self, cfg: Config, system_prompt: str, on_event: Callable[[str, Any], None], *,
                 cwd: str | None = None, line_id: str = "app", resume: str = "", model: str | None = None,
                 effort: str | None = None, servers: dict[str, dict] | None = None,
                 ask: Callable[[str, str, str], tuple[bool, str]] | None = None, exe: str | None = None,
                 transport_factory: Callable[..., Any] | None = None, stderr_path: Path | None = None):
        self.cfg = cfg
        self.system_prompt = system_prompt
        self.on_event = on_event
        self.cwd = os.path.expanduser(cwd or cfg.working_directory)
        self.line_id = line_id
        self.provider = "openai"
        self.model = model or access.provider_for("openai", cfg).model
        self.effort = cfg.effort if effort is None else effort
        self.servers = servers or {}
        self.ask = ask or (lambda action, question, detail, facts=None: (False, "没有人能批准这个申请"))
        self.file_changes: dict[str, list[dict]] = {}  # fileChange item id -> [{path, kind}] for the risk check
        self.exe = exe
        self.transport_factory = transport_factory
        self.stderr_path = stderr_path
        self.port = 0  # the control endpoint for our MCP servers (set by the switchboard)
        self.session_id = resume or ""  # Codex's thread id: resumed on restart
        self.transport: Any = None
        self.ready = threading.Event()
        self.turn_active = False
        self.turn_id: str | None = None
        self.interrupted = False
        self.text = ""
        self.last_error = ""
        self.next_id = 0
        self.pending: dict[int, dict] = {}
        self.write_lock = threading.Lock()
        self.generation = 0  # bumps on close(): a reader of an old transport stays quiet

    # ---------- arguments ----------

    def build_args(self) -> list[str]:
        exe = self.exe or find_codex()
        if not exe:
            raise FileNotFoundError("找不到 Codex 程序（codex.exe）")
        args = [exe, "app-server", "-c", "mcp_servers={}"]  # the user's own MCP servers stay out of our lines
        for f in DISABLED_FEATURES:
            args += ["--disable", f]
        for name, spec in self.servers.items():
            env = {**spec.get("env", {}), "VOICECLAUDE_PORT": str(self.port), "VOICECLAUDE_LINE": self.line_id}
            args += ["-c", f"mcp_servers.{name}.command={json.dumps(Path(spec['command']).as_posix())}",
                     "-c", f"mcp_servers.{name}.args={json.dumps([Path(a).as_posix() if os.sep in a else a for a in spec.get('args', [])])}",
                     "-c", f"mcp_servers.{name}.env={{{', '.join(f'{k} = {json.dumps(str(v))}' for k, v in env.items())}}}"]
        return args

    def build_env(self) -> dict[str, str]:
        """Codex finds the user's ChatGPT login in ~/.codex itself; a stray API key must not take precedence."""
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
        return env

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self.transport_factory is None and testing.under_test_runner():
            raise RuntimeError(testing.REFUSAL)
        args = self.build_args()
        log.info("[%s] starting codex app-server in %s (model %s, thread %s)", self.line_id, self.cwd, self.model,
                 self.session_id[:8] if self.session_id else "new")
        factory = self.transport_factory or (lambda a, cwd, env: StdioTransport(a, cwd, env, self.stderr_path))
        self.ready.clear()
        self.transport = factory(args, self.cwd, self.build_env())
        gen = self.generation
        threading.Thread(target=self._read, args=(self.transport, gen), daemon=True, name=f"codex-out-{self.line_id}").start()
        threading.Thread(target=self._handshake, args=(gen,), daemon=True, name=f"codex-hello-{self.line_id}").start()

    def _handshake(self, gen: int) -> None:
        try:
            self._request("initialize", {"clientInfo": {"name": "voice-claude", "title": "voice-claude", "version": __version__},
                                         "capabilities": {"experimentalApi": True}}, HANDSHAKE_TIMEOUT)
            if gen != self.generation:
                return  # this app-server was replaced meanwhile: never talk to the next one from here
            self._notify("initialized", {})
            params = {"cwd": self.cwd, "approvalPolicy": "on-request", "sandbox": "workspace-write",
                      "developerInstructions": self.system_prompt, "model": self.model}
            if self.session_id:
                try:
                    r = self._request("thread/resume", {**params, "threadId": self.session_id}, HANDSHAKE_TIMEOUT)
                except RuntimeError as e:
                    if gen != self.generation or isinstance(e, CodexSession.Gone):
                        return  # it failed because its app-server went away: not a reason for a new thread
                    log.warning("[%s] thread/resume failed (%s); starting a new thread", self.line_id, e)
                    self.session_id = ""
                    r = self._request("thread/start", params, HANDSHAKE_TIMEOUT)
            else:
                r = self._request("thread/start", params, HANDSHAKE_TIMEOUT)
            if gen != self.generation:
                return
            self.session_id = (r.get("thread") or {}).get("id") or self.session_id
            ready = {"session_id": self.session_id}
            turns = (r.get("thread") or {}).get("turns") or r.get("turns") or []
            if isinstance(turns, list) and turns and isinstance(turns[-1], dict) and turns[-1].get("id"):
                ready["base_turn"] = str(turns[-1]["id"])  # the last turn of a resumed thread: a rewind can fork after it
            self.base_turn = ready.get("base_turn")
            # the event before the flag: whoever waits for ready (a send, a rewind) finds the line already told, so the
            # base turn is in before any turn of ours, and a rewind's own save does not race this one
            self.on_event("ready", ready)
            self.ready.set()
        except Exception as e:
            if gen != self.generation:
                log.debug("[%s] the handshake of a replaced app-server ended: %s", self.line_id, e)
                return
            log.exception("[%s] codex handshake failed", self.line_id)
            self.last_error = str(e)
            if gen == self.generation:
                self.on_event("done", {"status": "error", "message": f"Codex 起不来：{e}", "subtype": "handshake"})
                self.on_event("exit", -1)

    def send(self, text: str) -> None:
        if self.transport is None:
            raise RuntimeError("Codex 正在重启，请稍后再说一遍")
        gen = self.generation
        waited = self._wait_ready(gen, READY_TIMEOUT)
        if waited == "gone":
            raise RuntimeError("Codex 进程已结束，这句没有发出去")
        if waited == "timeout":
            raise RuntimeError("Codex 还没准备好，请稍后再说一遍")
        if gen != self.generation or self.transport is None:
            # the app-server this sentence waited for went away and another one came up: the user was told it ended,
            # so it is not sent now behind their back (they may well have said it again)
            raise RuntimeError("Codex 重新启动过，这句没有发出去，请再说一遍")
        self.interrupted = False
        self.turn_active = True
        self.text, self.last_error, self.turn_id = "", "", None
        params = {"threadId": self.session_id, "input": [{"type": "text", "text": text}],
                  "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [self.cwd], "networkAccess": False}}
        if self.effort:
            params["effort"] = self.effort
        threading.Thread(target=self._start_turn, args=(params, self.generation), daemon=True).start()

    def _start_turn(self, params: dict, gen: int) -> None:
        try:
            r = self._request("turn/start", params, HANDSHAKE_TIMEOUT)
            self.turn_id = (r.get("turn") or {}).get("id")
            if self.turn_id and gen == self.generation:
                self.on_event("mark", {"codex_turn": self.turn_id})  # the point a rewind can fork after
        except Exception as e:
            log.exception("[%s] turn/start failed", self.line_id)
            if gen == self.generation:
                self.turn_active = False
                self.on_event("done", {"status": "error", "message": f"Codex 没接受这句话：{e}", "subtype": "turn_start"})

    def interrupt(self) -> None:
        if self.turn_active and self.turn_id and self.session_id:
            self.interrupted = True
            threading.Thread(target=self._request, args=("turn/interrupt", {"threadId": self.session_id, "turnId": self.turn_id}, 15.0),
                             daemon=True).start()

    def close(self) -> None:
        old = self.generation
        self.generation += 1  # retired first: the waiters woken below see it is stale
        self._fail_pending(old)
        t, self.transport = self.transport, None
        self.ready.clear()
        self.turn_active = False
        if t is not None:
            try:
                t.close()
            except Exception:
                log.debug("[%s] close failed", self.line_id, exc_info=True)

    def kill(self) -> None:
        old = self.generation
        self.generation += 1
        self._fail_pending(old)
        t, self.transport = self.transport, None
        self.ready.clear()
        self.turn_active = False
        if t is not None:
            try:
                t.kill()
            except Exception:
                log.debug("[%s] kill failed", self.line_id, exc_info=True)

    def rewind_to(self, keep: dict, drop: dict, single: bool = True) -> None:
        """Continue this conversation only up to the end of the turn `keep`: Codex forks the thread there
        (thread/fork, lastTurnId) and the line goes on in the new thread. Files are not touched (Codex cannot)."""
        last = keep.get("codex_turn")
        if not (last and self.session_id):
            raise RuntimeError("找不到撤回的位置（这几轮的记录不全）")
        waited = self._wait_ready(self.generation, READY_TIMEOUT)
        if waited != "ready":
            raise RuntimeError("Codex 进程已结束" if waited == "gone" else "Codex 还没准备好")
        r = self._request("thread/fork", {"threadId": self.session_id, "lastTurnId": last}, HANDSHAKE_TIMEOUT)
        new = (r.get("thread") or {}).get("id")
        if not new:
            raise RuntimeError("Codex 没有给出新的对话")
        old = self.session_id
        log.info("[%s] rewound: thread %s forked after turn %s -> %s", self.line_id, old[:8], last, new[:8])
        self.session_id = new
        self.restart()  # the new thread is resumed like any other (its id is reported on ready)
        if self._wait_ready(self.generation, READY_TIMEOUT) == "ready" and self.session_id == new:
            return
        # the forked thread did not come up (resume failed, so the handshake started an empty one, or nothing came up)
        log.warning("[%s] the forked thread %s did not come up; back to %s", self.line_id, new[:8], old[:8])
        self.close()
        self.session_id = old
        self.restart()
        raise RuntimeError("撤回后的对话没有接上")

    def _wait_ready(self, gen: int, timeout: float) -> str:
        """Wait for this app-server's thread: "ready", "gone" as soon as that app-server ended or was replaced (its
        generation retired: no waiting out the full timeout), or "timeout"."""
        deadline = time.monotonic() + timeout
        while True:
            if gen != self.generation or self.transport is None:
                return "gone"
            if self.ready.wait(timeout=min(0.2, max(0.0, deadline - time.monotonic()))):
                return "ready" if gen == self.generation else "gone"
            if time.monotonic() >= deadline:
                return "timeout"

    def restart(self, model: str | None = None, provider: str | None = None, fresh: bool = False) -> None:
        self.close()
        if model:
            self.model = model
        if fresh:
            self.session_id = ""
        self.start()

    @property
    def alive(self) -> bool:
        return self.transport is not None and self.transport.poll() is None

    # ---------- JSON-RPC ----------

    def _send(self, msg: dict) -> None:
        t = self.transport
        if t is None:
            raise RuntimeError("Codex 已关闭")
        with self.write_lock:
            t.send(json.dumps(msg, ensure_ascii=False))

    def _notify(self, method: str, params: dict) -> None:
        self._send({"method": method, "params": params})

    class Gone(RuntimeError):
        """A request whose app-server ended before it answered: not an answer, never a refused resume."""

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        with self.write_lock:
            self.next_id += 1
            rid = self.next_id
            slot = {"event": threading.Event(), "result": None, "error": None, "gen": self.generation}
            self.pending[rid] = slot
        try:
            self._send({"id": rid, "method": method, "params": params})
            if not slot["event"].wait(timeout):
                raise RuntimeError(f"{method} 超时（{int(timeout)} 秒）")
            if slot.get("gone"):
                raise CodexSession.Gone(f"{method}：Codex 进程已结束")
            if slot["error"] is not None:
                raise RuntimeError(f"{method} -> {json.dumps(slot['error'], ensure_ascii=False)[:200]}")
            return slot["result"] or {}
        finally:
            self.pending.pop(rid, None)

    def _fail_pending(self, gen: int) -> None:
        """Every request still waiting on that app-server gets its answer now: it is gone (no 60-s wait, and nothing
        that waited on it goes on with the next one)."""
        for slot in list(self.pending.values()):
            if slot.get("gen") == gen and not slot["event"].is_set():
                slot["gone"] = True
                slot["event"].set()

    def _read(self, transport, gen: int) -> None:
        while True:
            try:
                line = transport.readline()
            except Exception:
                line = None
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.debug("[%s] non-json from codex: %s", self.line_id, line[:200])
                continue
            if gen != self.generation:
                continue  # an old transport after close(): ignore
            try:
                self._dispatch(msg)
            except Exception:
                log.exception("[%s] codex message handling failed", self.line_id)
        if gen == self.generation and self.transport is not None:
            code = transport.poll()
            self.generation += 1  # retired before anyone waiting on it wakes: no handshake or turn acts on it
            self.transport = None
            self.turn_active = False
            self.ready.clear()
            self._fail_pending(gen)
            self.on_event("exit", code)  # once
        else:
            self._fail_pending(gen)

    def _dispatch(self, msg: dict) -> None:
        if "method" in msg and "id" in msg:  # a server-initiated request: answer on its own thread (it may ask the user)
            threading.Thread(target=self._serve, args=(msg,), daemon=True, name=f"codex-ask-{self.line_id}").start()
        elif "id" in msg:
            slot = self.pending.get(msg["id"])
            if slot is not None:
                if "error" in msg:
                    slot["error"] = msg["error"]
                else:
                    slot["result"] = msg.get("result")
                slot["event"].set()
        elif "method" in msg:
            self._notification(msg["method"], msg.get("params") or {})

    # ---------- notifications -> our events ----------

    def _notification(self, method: str, p: dict) -> None:
        if method == "item/agentMessage/delta":
            delta = str(p.get("delta", ""))
            self.text += delta
            self.on_event("text", delta)
        elif method == "item/started":
            item = p.get("item") or {}
            t = item.get("type")
            if t == "commandExecution":
                self.on_event("tool", {"name": "Bash", "input": {"command": str(item.get("command", ""))}})
            elif t == "mcpToolCall":
                self.on_event("tool", {"name": f"mcp__{item.get('server', '')}__{item.get('tool', '')}",
                                       "input": item.get("arguments") or {}})
            elif t == "fileChange":
                changes = [{"path": str(c.get("path", "")), "kind": str(((c.get("kind") or {}).get("type") if isinstance(c.get("kind"), dict) else c.get("kind")) or "")}
                           for c in (item.get("changes") or []) if isinstance(c, dict)]
                self.file_changes[str(item.get("id", ""))] = changes
                self.on_event("tool", {"name": "Edit", "input": {"file_path": "、".join(c["path"] for c in changes)[:120],
                                                                "files": [c["path"] for c in changes if c["path"]]}})
            elif t == "webSearch":
                self.on_event("tool", {"name": "WebSearch", "input": {"query": str(item.get("query", ""))}})
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            status = TURN_STATUS.get(str(turn.get("status")), "ok")
            if self.interrupted and status == "ok":
                status = "interrupted"
            err = turn.get("error") or {}
            message = self.text if status != "error" else (err.get("message") or self.last_error or self.text or "Codex 出错了")
            self.turn_active = False
            self.turn_id = None
            self.on_event("done", {"status": status, "message": message, "subtype": turn.get("status")})
        elif method == "error":
            self.last_error = str((p.get("error") or {}).get("message", ""))
            log.warning("[%s] codex error: %s (retry=%s)", self.line_id, self.last_error[:200], p.get("willRetry"))
        elif method == "mcpServer/startupStatus/updated":
            if p.get("status") == "failed" or p.get("error"):
                log.warning("[%s] mcp server %s: %s %s", self.line_id, p.get("name"), p.get("status"), p.get("error"))
        elif method == "account/updated":
            log.info("[%s] codex account: %s %s", self.line_id, p.get("authMode"), p.get("planType"))

    # ---------- server requests -> the spoken gate ----------

    def _serve(self, msg: dict) -> None:
        method, p, rid = msg["method"], msg.get("params") or {}, msg["id"]
        try:
            if method == "item/commandExecution/requestApproval":
                cmd = str(p.get("command") or "")
                reason = str(p.get("reason") or "")
                net = p.get("networkApprovalContext") or {}
                what = f"Codex 想在沙箱外运行命令：{cmd[:80]}" if not net else f"Codex 想联网访问 {net.get('host', '')}"
                ok, _ = self.ask("codex_shell", f"{what}，可以吗？", "\n".join(x for x in (cmd, reason) if x),
                                 {"kind": "shell", "command": cmd})
                self._send({"id": rid, "result": {"decision": "accept" if ok else "decline"}})
            elif method == "item/fileChange/requestApproval":
                where = str(p.get("grantRoot") or self.cwd)
                changes = self.file_changes.get(str(p.get("itemId") or ""), [])
                ok, _ = self.ask("codex_edit", f"Codex 想改 {Path(where).name or where} 下的文件，可以吗？", str(p.get("reason") or where),
                                 {"kind": "file_change", "changes": changes})
                self._send({"id": rid, "result": {"decision": "accept" if ok else "decline"}})
            elif method == "item/permissions/requestApproval":
                asked = p.get("permissions") or {}
                detail = json.dumps(asked, ensure_ascii=False)[:300]
                ok, _ = self.ask("codex_shell", f"Codex 申请更多权限（{'联网' if (asked.get('network') or {}).get('enabled') else '文件'}），可以吗？", detail,
                                 {"kind": "permissions"})
                self._send({"id": rid, "result": {"permissions": asked if ok else {}, "scope": "turn"}})
            elif method == "mcpServer/elicitation/request":
                ours = str(p.get("serverName", "")) in OUR_SERVERS
                if ours:  # our servers ask the user themselves, out loud
                    self._send({"id": rid, "result": {"action": "accept", "content": {}, "_meta": {"persist": "session"}}})
                else:
                    self._send({"id": rid, "result": {"action": "decline", "content": None}})
            elif method == "item/tool/requestUserInput":
                self._send({"id": rid, "result": {"answers": {}}})  # the model will ask in words; voice answers in words
            else:
                self._send({"id": rid, "error": {"code": -32601, "message": f"voice-claude does not handle {method}"}})
        except Exception:
            log.exception("[%s] could not answer %s", self.line_id, method)


# ---------- what the access layer asks of Codex ----------

def quick_probe(cfg: Config, timeout: float = 120.0) -> dict:
    """One real turn ("只回答一个字：好") through a CodexSession: test_provider for the ChatGPT-login method."""
    import tempfile
    if testing.under_test_runner():
        return {"ok": False, "message": testing.REFUSAL, "seconds": 0.0}
    events: list[tuple[str, Any]] = []
    done = threading.Event()

    def on_event(kind, data):
        events.append((kind, data))
        if kind in ("done", "exit"):
            done.set()
    s = CodexSession(cfg, "只用中文回答，一两句。", on_event, cwd=tempfile.gettempdir(), line_id="probe")
    t0 = time.time()
    try:
        s.start()
        s.send("只回答一个字：好")
        done.wait(timeout)
    except Exception as e:
        return {"ok": False, "message": f"Codex 起不来：{e}", "seconds": time.time() - t0}
    finally:
        s.close()
    secs = time.time() - t0
    text = "".join(d for k, d in events if k == "text")
    st = next((d for k, d in events if k == "done"), None)
    if st and st.get("status") == "ok" and text.strip():
        return {"ok": True, "message": f"接通了，Codex 回答“{text.strip()[:20]}”（{secs:.1f} 秒）。", "seconds": secs}
    return {"ok": False, "message": ("没接通：" + str((st or {}).get("message") or "没有回答"))[:200], "seconds": secs}


_login_lock = threading.Lock()  # one `codex login` at a time
_login_proc: Any = None


def login_log_path() -> Path:
    """Our own record of `codex login`'s output, in voice-claude's folder (Codex's own folder is never written)."""
    return paths.HOME_DIR / "codex-login.log"


def stop_login() -> bool:
    """Stop the `codex login` we started earlier, if it is still waiting: it holds its callback server on port 1455,
    so a second login cannot start while the first is alive. True when one was running."""
    global _login_proc
    proc, _login_proc = _login_proc, None
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            log.exception("could not stop codex login")
    log.info("stopped the previous codex login (pid %s)", getattr(proc, "pid", "?"))
    return True


def _pump_login(proc, args: list[str], lines: list[str], log_path: Path | None) -> None:
    """Collect the login's output for start_login() and keep it in ~/.voice-claude/codex-login.log, so a login that
    goes wrong can be looked at afterwards (the output has the sign-in URL and messages, no tokens)."""
    f = None
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            big = log_path.exists() and log_path.stat().st_size > 256 * 1024
            f = open(log_path, "w" if big else "a", encoding="utf-8")
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(['codex'] + list(args[1:]))} ===\n")
            f.flush()
        except OSError:
            log.warning("cannot write %s", log_path)
            f = None
    try:
        for raw in proc.stdout:
            line = str(raw).rstrip("\r\n")
            lines.append(line.strip())
            if f is not None:
                f.write(line + "\n")
                f.flush()
        code = proc.wait()
        if f is not None:
            f.write(f"=== exit {code} ===\n")
    except Exception:
        log.exception("codex login output")
    finally:
        if f is not None:
            f.close()


def start_login(exe: str | None = None, mode: str = "browser", *, popen: Callable = subprocess.Popen,
                log_path: Path | None = None, wait: float = 20.0) -> dict:
    """`codex login` (opens the browser) or `codex login --device-auth` (a code to type on any device). Returns as
    soon as the URL / code are known; the login itself finishes in the browser and Codex stores the result.
    A login still waiting from an earlier click is stopped first."""
    import re
    global _login_proc
    if testing.under_test_runner() and popen is subprocess.Popen:
        return {"ok": False, "message": testing.REFUSAL}
    exe = exe or find_codex()
    if not exe:
        return {"ok": False, "message": "没装 Codex：从 OpenAI 的 GitHub 发布页装官方 Windows 版，装好不用重启。"}
    args = [exe, "login"] + (["--device-auth"] if mode == "device" else [])
    with _login_lock:
        stop_login()
        try:
            proc = popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            return {"ok": False, "message": f"codex login 起不来：{e}"}
        _login_proc = proc
    lines: list[str] = []
    threading.Thread(target=_pump_login, args=(proc, args, lines, log_path or login_log_path()), daemon=True,
                     name="codex-login").start()
    url = code = local = ""
    deadline = time.time() + wait
    while time.time() < deadline:
        joined = "\n".join(lines)
        # Codex names its own callback server (http://localhost:1455) before the sign-in page: show the sign-in page
        found = [u.rstrip(".,)") for u in re.findall(r"https?://\S+", joined)]
        remote = [u for u in found if not re.match(r"https?://(localhost|127\.0\.0\.1)\b", u)]
        local = local or next((u for u in found if u not in remote), "")
        c = re.search(r"\b[A-Z0-9]{4,5}-[A-Z0-9]{4,6}\b", joined)
        url, code = (remote[0] if remote else url), (c.group(0) if c else code)
        if (mode == "device" and url and code) or (mode != "device" and url) or proc.poll() is not None:
            break
        time.sleep(0.2)
    url = url or local
    if proc.poll() is not None and proc.returncode != 0 and not url:
        return {"ok": False, "message": "codex login 没起来：" + (" ".join(lines)[-200:] or "没有输出")}
    if mode == "device":
        msg = f"在手机或浏览器打开 {url or '它给的网址'}，输入代码 {code or '（见窗口）'}；登录完成后点“重新检测”。"
    else:
        msg = "浏览器里登录 ChatGPT；完成后点“重新检测”。" + (f"（没弹出就打开 {url}）" if url else "")
    return {"ok": True, "message": msg, "url": url, "code": code, "pending": proc.poll() is None}


def logout(exe: str | None = None) -> tuple[bool, str]:
    if testing.under_test_runner():
        return False, testing.REFUSAL
    exe = exe or find_codex()
    if not exe:
        return False, "没装 Codex"
    try:
        r = subprocess.run([exe, "logout"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return False, f"codex logout 跑不起来：{e}"
    return r.returncode == 0, (r.stdout or r.stderr or "").strip()[:200] or "已退出 Codex 登录。"
