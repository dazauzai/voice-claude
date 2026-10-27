"""A persistent `claude -p` process speaking stream-json on stdin/stdout. One instance = one line."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import access, paths, testing
from .config import ROOT, Config

log = logging.getLogger(__name__)

# Speed bumps against the worst mistake (deleting data). Prefix patterns only; the auto-mode
# permission checker is the real guard.
DENY = [
    "Bash(rm *)", "Bash(rmdir *)", "Bash(del *)", "Bash(rd *)", "Bash(Remove-Item *)",
    "Bash(git clean *)", "Bash(git reset --hard *)", "Bash(git reset --hard)",
    "Bash(git push --force *)", "Bash(git push -f *)", "Bash(git branch -D *)",
    "PowerShell(Remove-Item *)", "PowerShell(rm *)", "PowerShell(del *)", "PowerShell(rd *)",
    "PowerShell(rmdir *)", "PowerShell(ri *)",
    # Opening programs / windows must go through the desktop tools, which ask the user out loud first.
    "Bash(start *)", "Bash(cmd /c start*)", "Bash(cmd //c start*)", "Bash(explorer*)", "Bash(notepad*)",
    "PowerShell(Start-Process *)", "PowerShell(start *)", "PowerShell(saps *)", "PowerShell(Invoke-Item *)",
    "PowerShell(ii *)", "PowerShell(explorer*)", "PowerShell(notepad*)",
    # A second Claude Code has none of our gates (its SendMessage reaches the user's other sessions unasked).
    "Bash(claude)", "Bash(claude *)", "Bash(claude.exe*)", "Bash(npx @anthropic-ai/claude-code*)",
    "Bash(cmd /c claude*)", "Bash(cmd //c claude*)",
    "PowerShell(claude)", "PowerShell(claude *)", "PowerShell(claude.exe*)", "PowerShell(npx @anthropic-ai/claude-code*)",
]

# Claude Code's own tools that reach outside this line with no permission prompt of their own (SendMessage puts text
# into the user's other Claude Code sessions, which then act on it): every call is put to the user out loud first, by
# an in-process PreToolUse hook (the SDK's initialize + hook_callback over stream-json, no hook script that could fail
# open). No answer, an error or an unknown callback = deny. A CLI that will not take the hook gets them switched off.
GATED_TOOLS = ("SendMessage",)
HOOK_ID = "vc_tool_gate"
# Peer messages from other sessions must not start turns in a line nobody is listening to.
LINE_SETTINGS = {"crossSessionInbound": "refuse"}

SETTINGS_SERVER = "voiceclaude"  # settings + background jobs (mcp_server.py)
PERMISSION_MODE = "auto"  # every line's: a prompting mode, which decides how other sessions take its messages (relay.py)
DESKTOP_SERVER = "desktop"  # desktop control (desktop.py)
MANAGER_SERVER = "manager"  # the manager line's tools (manager_server.py)


def find_claude(command: str) -> str:
    from . import access
    found = access.find_claude_binary(command)
    if found:
        return found
    raise FileNotFoundError("找不到 Claude Code 程序。装官方 Claude Code（并 claude auth login），或再双击 install.bat 装上自带的引擎。")


def server_spec(script: str) -> dict:
    """An MCP server entry for one of our tool scripts (env is filled in per line by ClaudeSession)."""
    return {"command": sys.executable, "args": ["-X", "utf8", str(ROOT / "voiceclaude" / script)], "env": {}}


class ClaudeSession:
    """Events: ('ready', dict) ('text', str) ('tool', {name, input}) ('done', {status, message}) ('exit', code).

    Keyword arguments describe the line this process serves; by default it is a single line in
    cfg.working_directory with our two tool servers and the model/effort from cfg."""

    def __init__(self, cfg: Config, system_prompt: str, on_event: Callable[[str, Any], None], *,
                 cwd: str | None = None, line_id: str = "app", resume: str = "", model: str | None = None,
                 effort: str | None = None, servers: dict[str, dict] | None = None,
                 allowed: list[str] | None = None, extra_env: dict[str, str] | None = None, provider: str = ""):
        self.cfg = cfg
        self.system_prompt = system_prompt
        self.on_event = on_event
        self.cwd = os.path.expanduser(cwd or cfg.working_directory)
        self.line_id = line_id
        self.resume = resume or cfg.resume
        # Who answers (claude / deepseek / kimi / openai): this process is Claude Code, and the provider decides
        # the endpoint, the key and the model names it is given (access.env_overrides). GPT through a ChatGPT plan
        # runs on Codex instead (engine_codex.py).
        self.provider = provider or str(getattr(cfg, "provider", "") or "claude")
        self.model = model or self._default_model()
        self.effort = cfg.effort if effort is None else effort
        self.servers = servers
        self.allowed = allowed
        self.extra_env = dict(extra_env or {})
        self.proc: subprocess.Popen | None = None
        self.port = 0  # the control endpoint, handed to the MCP tool servers
        self.session_id = ""
        self.turn_active = False
        self.interrupted = False
        self.turn_done = threading.Event()
        self.turn_done.set()
        self.write_lock = threading.Lock()
        self.fork_at: tuple[str, str | None] | None = None  # rewinding: (last entry to keep, the one dropped prompt or None)
        self.fork_rejected = False
        self._rewind_outcome: dict | None = None  # a rewind waiting to hear whether the truncated resume came up
        self._outcome_lock = threading.Lock()
        self._err_threads: dict[int, threading.Thread] = {}  # per process: its stderr reader, joined before deciding
        self.control_waits: dict[str, dict] = {}  # our control requests waiting for their control_response
        # total_cost_usd and modelUsage are running totals (since Claude Code 2.1.277 they also carry the spend restored
        # with a resumed session): a turn is the change. None = not known (a resumed session we never saw a total of).
        self._cost_seen: float | None = 0.0
        self._models_seen: dict[str, dict] | None = {}
        self._baseline_sid = ""  # the session those totals belong to
        self.baseline_lookup: Callable[[str], dict | None] | None = None  # last saved totals of a session (the ledger)
        # (tool, tool_input, tool_use_id) -> (allow, reason): the spoken gate for GATED_TOOLS; None = always deny
        self.tool_gate: Callable[[str, dict, str], tuple[bool, str]] | None = None
        self.hooks_unsupported = False  # the CLI refused the hook: GATED_TOOLS are disallowed instead
        self._init_slot: dict | None = None  # the initialize request of the current process, until send() checks it
        self._gated: dict[str, str] = {}  # tool_use_id -> tool: allowed by the gate, its result not seen yet
        self._writing: dict | None = None  # the tool call being written right now (its arguments still streaming)

    # ---------- arguments ----------

    def _default_model(self) -> str:
        return self.cfg.model if self.provider == "claude" else access.provider_for(self.provider, self.cfg).model

    def default_servers(self) -> dict[str, dict]:
        servers = {SETTINGS_SERVER: server_spec("mcp_server.py")}
        if self.cfg.desktop_enabled:  # desktop control; every tool asks the user out loud before acting
            servers[DESKTOP_SERVER] = server_spec("desktop.py")
        return servers

    def _mcp_config(self) -> str:
        """Write the MCP server description Claude Code needs (a JSON file is safer than quoting JSON in argv)."""
        servers = {}
        for name, spec in (self.servers if self.servers is not None else self.default_servers()).items():
            spec = {**spec, "env": {**spec.get("env", {}), "VOICECLAUDE_PORT": str(self.port),
                                    "VOICECLAUDE_LINE": self.line_id, **self.extra_env}}
            servers[name] = spec
        d = paths.LINES_DIR / self.line_id
        d.mkdir(parents=True, exist_ok=True)
        path = d / "mcp-config.json"
        path.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2), "utf-8")
        return str(path)

    def _settings_file(self) -> str:
        d = paths.LINES_DIR / self.line_id
        d.mkdir(parents=True, exist_ok=True)
        path = d / "settings.json"
        path.write_text(json.dumps(LINE_SETTINGS, indent=2), "utf-8")
        return str(path)

    def build_args(self) -> list[str]:
        # Our MCP servers gate their actions through the user's spoken approval, so they are allow-listed
        # (otherwise auto mode's checker could reject them before the user ever hears the request).
        servers = self.servers if self.servers is not None else self.default_servers()
        allowed = self.allowed if self.allowed is not None else [f"mcp__{n}" for n in servers]
        args = [find_claude(self.cfg.command), "-p",
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages",
                "--permission-mode", PERMISSION_MODE, "--model", self.model,
                "--append-system-prompt", " ".join(self.system_prompt.split()),
                "--mcp-config", self._mcp_config(), "--allowedTools", ",".join(allowed),
                "--disallowedTools", ",".join(DENY + (list(GATED_TOOLS) if self.hooks_unsupported else [])),
                "--settings", self._settings_file()]
        if self.effort:
            args += ["--effort", self.effort]
        if self.resume:
            args += ["--resume", self.resume]
            if self.fork_at:
                # rewinding: load the conversation up to the end of the kept turn, as a new session (the SDK's flags).
                # --resume-drops-turn checks that everything dropped belongs to ONE turn: only given for one turn.
                at, drops = self.fork_at
                args += ["--fork-session", f"--resume-session-at={at}"] + ([f"--resume-drops-turn={drops}"] if drops else [])
        args.append("--replay-user-messages")  # our prompts come back with their uuid: the rewind points
        if self.cfg.permissions.get("other_connectors") == "deny":
            # Only our own MCP servers and no Claude-in-Chrome: the account's other connectors act without asking.
            args += ["--strict-mcp-config", "--no-chrome"]
        return args

    # ---------- process ----------

    def build_env(self) -> dict[str, str]:
        # Never the variables a host Claude Code session plants in its children (access.without_host_session). The
        # control port and line id reach our MCP servers through mcp-config.json only: the model's own shell does not
        # get them handed over.
        env = {k: v for k, v in access.without_host_session(dict(os.environ)).items()
               if k not in ("VOICECLAUDE_PORT", "VOICECLAUDE_LINE")}
        env.update(self.extra_env)
        # Our tool set is small (two or three servers, ~20 tools). Claude Code's default "tool search" defers MCP tool
        # definitions and costs an extra model round trip (2-3 s) before the first tool use of a turn; loading them
        # upfront is faster (official docs: under ~10-20 tools, load everything). The user's own setting wins.
        env.setdefault("ENABLE_TOOL_SEARCH", "false")
        env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "true"  # so a rewind can put edited files back
        # Access method / provider: key, endpoint and model slots go in through the environment; a subscription
        # must not be shadowed by a stray key.
        st = access.detect_for(self.provider, self.cfg.command, cfg=self.cfg)
        for k, v in access.env_overrides(st, self.cfg).items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return env

    def start(self):
        if testing.under_test_runner():
            raise RuntimeError(testing.REFUSAL)
        args = self.build_args()
        log.info("[%s] starting claude in %s: %s", self.line_id, self.cwd, " ".join(args[:12]))
        env = self.build_env()
        self.proc = proc = subprocess.Popen(
            args, cwd=self.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.turn_active = False
        self.resume_failed = False
        self._seed_baseline()
        self.turn_done.set()
        threading.Thread(target=self._read, args=(proc,), daemon=True, name=f"claude-out-{self.line_id}").start()
        err = threading.Thread(target=self._read_err, args=(proc,), daemon=True, name=f"claude-err-{self.line_id}")
        self._err_threads = {id(proc): err}
        err.start()
        self._initialize()

    # ---------- the gate for Claude Code's own outward tools ----------

    HOOK_TIMEOUT = 600  # seconds Claude Code waits for a hook answer: well above the spoken question's own timeout
    GATE_DEADLINE = HOOK_TIMEOUT - 30  # we answer "no" by then ourselves (questions queue one at a time)
    INIT_TIMEOUT = 60.0

    def _initialize(self) -> None:
        """Before the first message: register the PreToolUse hook for GATED_TOOLS (answered in _answer_control)."""
        self._init_slot = None
        if self.hooks_unsupported:
            return
        rid = "init_" + uuid.uuid4().hex[:10]
        slot = {"event": threading.Event(), "response": None}
        self.control_waits[rid] = slot
        self._init_slot = slot
        self._write({"type": "control_request", "request_id": rid, "request": {
            "subtype": "initialize",
            "hooks": {"PreToolUse": [{"matcher": "|".join(GATED_TOOLS), "hookCallbackIds": [HOOK_ID],
                                      "timeout": self.HOOK_TIMEOUT}]}}})

    def _await_init(self) -> None:
        """The first message waits for the hook to be in place. Refused or no answer: start again with the gated
        tools switched off, so they can never run unasked."""
        slot, self._init_slot = self._init_slot, None
        if slot is None:
            return
        deadline = time.monotonic() + self.INIT_TIMEOUT
        while not slot["event"].wait(0.1):
            if not self.alive:
                return  # the process is gone; _read reports its exit
            if time.monotonic() > deadline:
                break
        r = slot["response"] or {}
        body = r.get("response") if isinstance(r.get("response"), dict) else {}
        if r.get("subtype") == "success" and body.get("hooks_applied", True) is not False:
            return
        log.warning("[%s] Claude Code did not take the tool gate (%s); %s switched off for this line", self.line_id,
                    r.get("error") or ("no answer" if not r else "hooks not applied"), ", ".join(GATED_TOOLS))
        self.hooks_unsupported = True
        self.close()
        self.start()

    def _answer_control(self, d: dict) -> None:
        """A control request from Claude Code. hook_callback for a gated tool: the spoken gate decides; anything
        else is refused. Runs on its own thread (the question can take a minute)."""
        rid = d.get("request_id")
        req = d.get("request") if isinstance(d.get("request"), dict) else {}
        if req.get("subtype") != "hook_callback":
            self._write({"type": "control_response", "response": {"subtype": "error", "request_id": rid,
                                                                   "error": "not supported"}})
            return
        inp = req.get("input") if isinstance(req.get("input"), dict) else {}
        tool = str(inp.get("tool_name") or "")
        tool_input = inp.get("tool_input") if isinstance(inp.get("tool_input"), dict) else {}
        tool_use_id = str(req.get("tool_use_id") or inp.get("tool_use_id") or "")
        allow, why = False, "没有经过用户同意，没有执行。"
        if req.get("callback_id") == HOOK_ID and tool in GATED_TOOLS and self.tool_gate is not None:
            out: dict = {}

            def ask():
                try:
                    out["v"] = self.tool_gate(tool, tool_input, tool_use_id)
                except Exception:
                    log.exception("[%s] tool gate failed", self.line_id)
                    out["v"] = (False, "批准出了错，没有执行。")

            t = threading.Thread(target=ask, daemon=True, name=f"claude-gate-{self.line_id}")
            t.start()
            t.join(self.GATE_DEADLINE)
            allow, why = out.get("v") or (False, "等用户批准等得太久，没有执行。")
        if allow:
            self._gated[tool_use_id] = tool
        log.info("[%s] gate %s: %s", self.line_id, tool or req.get("callback_id"), "allow" if allow else "deny")
        self._write({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": {
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow" if allow else "deny",
                                   "permissionDecisionReason": why or ("用户同意了。" if allow else "用户没有同意。")}}}})

    def _gated_results(self, d: dict) -> None:
        """The outcome of a gated tool the user allowed (a subagent's too): ('gated_result', {tool, tool_use_id, ok,
        text})."""
        content = (d.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in self._gated:
                tool = self._gated.pop(b["tool_use_id"])
                c = b.get("content")
                text = c if isinstance(c, str) else "".join(
                    x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else ""
                self.on_event("gated_result", {"tool": tool, "tool_use_id": b["tool_use_id"],
                                               "ok": not b.get("is_error"), "text": text})

    def restart(self, model: str | None = None, provider: str | None = None, fresh: bool = False):
        """Reconnect, optionally on another model and/or provider, continuing the same conversation (Claude Code
        --resume); fresh=True starts a new conversation instead (when the old one cannot be resumed elsewhere)."""
        self.close()
        if provider:
            self.provider = provider
            self.model = model or self._default_model()
        elif model:
            self.model = model
        if fresh:
            self.session_id, self.resume, self.fork_at = "", "", None
        elif self.session_id:
            self.resume = self.session_id
        self.start()

    REWIND_SETTLE = 1.5  # seconds a truncated resume must stay up (Claude Code refuses one within half a second)
    REWIND_TIMEOUT = 20.0

    def rewind_to(self, keep: dict, drop: dict, single: bool = True) -> None:
        """Continue this conversation only up to the end of the turn `keep` (Claude Code's truncating resume, as a
        forked session); the turn `drop` and everything after it are gone. `single`: exactly one turn is dropped, so
        --resume-drops-turn can guard it. Returns once the resumed process is up; if Claude Code refuses or it exits,
        the line goes back to the conversation as it was and this raises."""
        at, prompt = keep.get("last"), drop.get("prompt")
        if not (at and self.session_id) or (single and not prompt):
            raise RuntimeError("找不到撤回的位置（这几轮的记录不全）")
        old, before = self.session_id, self.fork_at
        if before is not None:
            single = False  # the first rewind's fork is not made yet: the base session still holds the turns it dropped
        self.close()
        self.fork_at, self.fork_rejected, self.resume = (at, prompt if single else None), False, old
        outcome = {"event": threading.Event(), "ok": None, "why": ""}
        self._rewind_outcome = outcome
        started = time.monotonic()
        try:
            self.start()
            threading.Thread(target=self._await_loaded, args=(outcome, started), daemon=True,
                             name=f"claude-rewind-{self.line_id}").start()
            outcome["event"].wait(self.REWIND_TIMEOUT + self.REWIND_SETTLE + 1)
        finally:
            self._rewind_outcome = None
        if outcome["ok"] is not True:
            log.warning("[%s] rewind refused: %s; back to conversation %s as it was", self.line_id, outcome["why"], old[:8])
            self.close()
            # back to where the line was: the conversation as it was, or the earlier rewind still waiting to be made
            self.fork_at, self.fork_rejected, self.resume, self.session_id = before, False, old, old
            self.start()
            raise RuntimeError(outcome["why"] or "续接没有起来")

    def _await_loaded(self, outcome: dict, started: float) -> None:
        """The truncated resume is up when the process answers a harmless control request (mcp_status) and is still
        running REWIND_SETTLE seconds after it started; its exit or refusal is reported by _read instead."""
        rid = "up_" + uuid.uuid4().hex[:10]
        slot = {"event": threading.Event(), "response": None}
        self.control_waits[rid] = slot
        self._write({"type": "control_request", "request_id": rid, "request": {"subtype": "mcp_status"}})
        slot["event"].wait(self.REWIND_TIMEOUT)
        self.control_waits.pop(rid, None)
        rest = self.REWIND_SETTLE - (time.monotonic() - started)
        if rest > 0:
            time.sleep(rest)
        with self._outcome_lock:
            if outcome["ok"] is None:
                alive = self.proc is not None and self.proc.poll() is None
                outcome["ok"], outcome["why"] = (True, "") if alive else (False, "续接的进程没有起来")
                outcome["event"].set()

    def rewind_files(self, user_message_id: str, dry_run: bool = False, timeout: float = 30.0) -> dict:
        """Put the files back the way they were at that user message (Claude Code's file checkpointing restores EVERY
        file it tracks in the session, not only the dropped turns' ones; a file made later is deleted). dry_run: only
        report. Returns ok, error, files (Claude Code's filesChanged; None when it does not say) and skipped (links
        left alone for safety)."""
        if self.proc is None or not user_message_id:
            return {"ok": False, "error": "Claude 不在运行" if self.proc is None else "不知道从哪一句开始改回", "files": None, "skipped": 0}
        rid = "rw_" + uuid.uuid4().hex[:10]
        slot = {"event": threading.Event(), "response": None}
        self.control_waits[rid] = slot
        request = {"subtype": "rewind_files", "user_message_id": user_message_id}
        if dry_run:
            request["dry_run"] = True
        self._write({"type": "control_request", "request_id": rid, "request": request})
        if not slot["event"].wait(timeout):
            self.control_waits.pop(rid, None)
            return {"ok": False, "error": "Claude 没有回应", "files": None, "skipped": 0}
        resp = slot["response"] or {}
        if resp.get("subtype") == "error":
            return {"ok": False, "error": str(resp.get("error") or "出错了"), "files": None, "skipped": 0}
        r = resp.get("response") if isinstance(resp.get("response"), dict) else {}
        files = r.get("filesChanged")
        ok = r.get("canRewind", True) is not False and not r.get("error")
        return {"ok": ok, "error": str(r.get("error") or ("改不回去" if not ok else "")),
                "files": [str(f) for f in files] if isinstance(files, list) else None, "skipped": int(r.get("skippedLinks") or 0)}

    def send(self, text: str):
        if self.proc is None:
            raise RuntimeError("Claude 正在重启，请稍后再说一遍")
        self._await_init()
        # Wait for the previous (possibly interrupted) turn's result so replies never get mixed up.
        if not self.turn_done.wait(timeout=30):
            log.warning("[%s] previous turn still running after 30 s; sending anyway", self.line_id)
        self.interrupted = False
        self.turn_done.clear()
        self.turn_active = True
        self._write({"type": "user", "message": {"role": "user", "content": text}})

    def interrupt(self):
        if self.turn_active:
            self.interrupted = True
            self._write({"type": "control_request", "request_id": str(uuid.uuid4()),
                         "request": {"subtype": "interrupt"}})

    def close(self):
        self._writing_done()  # a call half written goes with the process
        proc, self.proc = self.proc, None  # the reader sees proc is not self.proc and stays quiet
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(timeout=3)
            except Exception:
                proc.kill()

    def kill(self):
        """The emergency brake: the whole process tree (claude, its tool servers, any command it is running)."""
        self._writing_done()
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        self.turn_active = False
        self.turn_done.set()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _write(self, obj: dict):
        with self.write_lock:
            if self.proc is None:  # it has just exited (a refused rewind's check, say): nothing to write to
                log.debug("[%s] no claude process to write to", self.line_id)
                return
            try:
                self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError, AttributeError):
                log.exception("[%s] claude stdin closed", self.line_id)

    def _read_err(self, proc):
        for line in proc.stderr:
            text = line.rstrip()
            if "No conversation found" in text:
                self.resume_failed = True  # the id given to --resume is not a Claude Code conversation here
            if "Resume rejected by --resume-drops-turn" in text:
                self.fork_rejected = True  # the rewound range holds something we did not see: resume plainly
            if "unrecognized_model" in text:
                # Claude Code noting that the model name is not one of its own: expected on a DeepSeek / GPT line
                log.debug("[%s] claude stderr (expected on a non-Claude provider): %s", self.line_id, text)
                continue
            log.info("[%s] claude stderr: %s", self.line_id, text)

    def _read(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                log.debug("non-json from claude: %s", line[:200])
                continue
            try:
                self._handle(d)
            except Exception:
                log.exception("bad claude event: %s", line[:300])
        code = proc.wait()
        err = self._err_threads.get(id(proc))
        if err is not None:
            err.join(2)  # its last lines (a refusal, "No conversation found") must be read before deciding anything
        if proc is not self.proc:
            return  # closed on purpose (restart / quit / kill)
        self.turn_active = False
        self.turn_done.set()
        out = self._rewind_outcome
        if out is not None:
            with self._outcome_lock:
                if out["ok"] is None:  # a rewind is waiting: this truncated resume did not come up
                    out["ok"] = False
                    out["why"] = ("Claude Code 不接受这次撤回（那几轮里有没看到的内容）" if self.fork_rejected
                                  else f"续接的进程退出了（{code}）")
                    self.proc = None
                    out["event"].set()
                    return
        if self.fork_at and self.fork_rejected:
            # the SDK's advice: drop the fork target and resume plainly rather than retrying the same request
            log.warning("[%s] the rewind was refused by Claude Code; continuing the conversation as it was", self.line_id)
            self.fork_at, self.fork_rejected = None, False
            self.on_event("rewind_failed", {})
            self.start()
            return
        self.on_event("exit", code)

    WRITING_AFTER = 3.0  # seconds a tool call's arguments have been streaming before the window is told
    WRITING_EVERY = 2.0  # then at most this often

    def _watch_writing(self, ev: dict) -> None:
        """('writing', {tool, chars, seconds, done}) while a tool call's arguments are still being written (a long
        type_text takes the model a minute, with nothing to show or say): so the user can see work is going on and
        does not stop it half way. Only once it has gone on WRITING_AFTER seconds; done=True when the call is complete
        or the turn ends."""
        kind = ev.get("type")
        if kind == "content_block_start" and (ev.get("content_block") or {}).get("type") == "tool_use":
            self._writing_done()
            self._writing = {"index": ev.get("index"), "tool": str(ev["content_block"].get("name") or ""),
                             "started": time.monotonic(), "chars": 0, "told": 0.0}
        elif kind == "content_block_delta" and self._writing and (ev.get("delta") or {}).get("type") == "input_json_delta":
            w, now = self._writing, time.monotonic()
            w["chars"] += len(str(ev["delta"].get("partial_json") or ""))
            if now - w["started"] >= self.WRITING_AFTER and now - w["told"] >= self.WRITING_EVERY:
                w["told"] = now
                self.on_event("writing", {"tool": w["tool"], "chars": w["chars"], "seconds": round(now - w["started"]),
                                          "done": False})
        elif kind == "content_block_stop" and self._writing and ev.get("index") == self._writing["index"]:
            self._writing_done()

    def _writing_done(self) -> None:
        w, self._writing = self._writing, None
        if w and w["told"]:
            self.on_event("writing", {"tool": w["tool"], "chars": w["chars"],
                                      "seconds": round(time.monotonic() - w["started"]), "done": True})

    USAGE_FIELDS = ("input", "output", "cache_read", "cache_write", "web_search", "cost")
    _MODEL_USAGE_KEYS = {"input": "inputTokens", "output": "outputTokens", "cache_read": "cacheReadInputTokens",
                         "cache_write": "cacheCreationInputTokens", "web_search": "webSearchRequests", "cost": "costUSD"}

    def _seed_baseline(self) -> None:
        """The totals a new process starts from. A new conversation: zero. The same session resumed again: what this
        object last saw. Another resumed session: its last saved totals (the ledger), else not known. A rewind (a
        truncating resume) restores only part of the spend: not known either."""
        if not self.resume:
            self._cost_seen, self._models_seen = 0.0, {}
        elif self.fork_at:
            self._models_seen, self._cost_seen = None, None
        elif self.resume != self._baseline_sid:
            saved = self.baseline_lookup(self.resume) if self.baseline_lookup else None
            if isinstance(saved, dict) and isinstance(saved.get("models"), dict):
                self._models_seen, self._cost_seen = dict(saved["models"]), saved.get("cost")
            else:
                self._models_seen, self._cost_seen = None, None
        self._baseline_sid = self.resume or ""

    def _report_usage(self, d: dict) -> None:
        """'tokens': this turn's main loop (result.usage, for the line under the reply). 'all_tokens' and the cost: the
        change in modelUsage per model since the last result, which also counts subagents, compaction and helper models
        (the SDK docs: prefer modelUsage for accounting). When the totals before are not known (a resumed session never
        seen) or went down, the turn counts only its own tokens and no cost, and the totals restart from here."""
        u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
        tokens = {"input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
                  "cache_read": int(u.get("cache_read_input_tokens") or 0),
                  "cache_write": int(u.get("cache_creation_input_tokens") or 0)}
        now = {}
        for name, mu in (d.get("modelUsage") or {}).items():
            if isinstance(mu, dict):
                now[name] = {k: (float(mu.get(src) or 0) if k == "cost" else int(mu.get(src) or 0))
                             for k, src in self._MODEL_USAGE_KEYS.items()}
                now[name]["canonical"] = str(mu.get("canonicalModel") or name)
                now[name]["basis"] = str(mu.get("costBasis") or "list")
        total = d.get("total_cost_usd")
        deltas, known = {}, self._models_seen is not None
        if now and known:
            for name, cur in now.items():
                before = self._models_seen.get(name) or {}
                delta = {k: cur[k] - (before.get(k) or 0) for k in self.USAGE_FIELDS}
                if any(v < -1e-9 for v in delta.values()):
                    known = False  # the totals went down (a truncated rewind, another process): start over from here
                    break
                deltas[name] = delta
        if now and known:
            all_tokens = {k: sum(int(x[k]) for x in deltas.values()) for k in self.USAGE_FIELDS if k != "cost"}
            cost = round(sum(x["cost"] for x in deltas.values()), 6)
        elif not now and self._cost_seen is not None and isinstance(total, (int, float)) and total >= self._cost_seen:
            all_tokens, cost = dict(tokens), round(float(total) - self._cost_seen, 6)  # an engine without modelUsage
        else:
            all_tokens, cost = dict(tokens), None
        lead = max(deltas or now or {"": {}}, key=lambda n: (deltas.get(n) or now.get(n) or {}).get("output", 0))
        model = now[lead]["canonical"] if lead in now else self.model
        basis = now[lead]["basis"] if lead in now else "list"
        by_model = [{"model": now[n]["canonical"], **{k: v for k, v in x.items()}} for n, x in deltas.items()]
        if now:
            self._models_seen = {n: {k: cur[k] for k in self.USAGE_FIELDS} for n, cur in now.items()}
        if isinstance(total, (int, float)):
            self._cost_seen = float(total)
        if any(tokens.values()) or any(all_tokens.values()) or cost:
            self.on_event("usage", {"model": model, "tokens": tokens, "all_tokens": all_tokens, "cost_usd": cost,
                                    "cost_basis": basis, "by_model": by_model, "session": self.session_id,
                                    "baseline": {"models": self._models_seen, "cost": self._cost_seen}})

    def _handle(self, d: dict):
        t = d.get("type")
        if t == "control_request":
            threading.Thread(target=self._answer_control, args=(d,), daemon=True,
                             name=f"claude-control-{self.line_id}").start()
            return
        if t == "user" and self._gated:
            self._gated_results(d)
        if d.get("parent_tool_use_id"):
            return  # subagent chatter
        if t == "system" and d.get("subtype") == "init":
            if self.fork_at and d.get("session_id") and d.get("session_id") != self.resume:
                self.fork_at = None  # the forked session exists now; later restarts resume it plainly
            self.session_id = d.get("session_id") or self.session_id
            self._baseline_sid = self.session_id  # the totals from here on are this session's (a fork's new id too)
            self.on_event("ready", d)
        elif t == "conversation_reset":
            self._cost_seen, self._models_seen = 0.0, {}  # the running totals start again
        elif t == "rate_limit_event":
            info = d.get("rate_limit_info")
            if isinstance(info, dict):
                self.on_event("limits", info)
        elif t == "control_response":
            r = d.get("response") or {}
            slot = self.control_waits.pop(str(r.get("request_id", "")), None)
            if slot is not None:
                slot["response"] = r
                slot["event"].set()
        elif t == "user":
            # replayed prompts (--replay-user-messages) and tool results: the rewind points of the current turn
            u = d.get("uuid")
            content = (d.get("message") or {}).get("content")
            tool_result = isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            if u:
                self.on_event("mark", {"last": u} if tool_result else {"prompt": u, "last": u})
        elif t == "stream_event":
            ev = d.get("event", {})
            if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                self.on_event("text", ev["delta"]["text"])
            else:
                self._watch_writing(ev)
        elif t == "assistant":
            if d.get("uuid"):
                self.on_event("mark", {"last": d["uuid"]})
            for block in d.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    self.on_event("tool", {"name": block.get("name", ""), "input": block.get("input", {})})
        elif t == "result":
            self._writing_done()
            self._report_usage(d)
            status = "interrupted" if self.interrupted else ("error" if d.get("is_error") else "ok")
            message = d.get("result") if isinstance(d.get("result"), str) else ""
            self.turn_active = False
            # Deliver the result before letting a waiting send() write the next message.
            self.on_event("done", {"status": status, "message": message or "", "subtype": d.get("subtype")})
            self.turn_done.set()
