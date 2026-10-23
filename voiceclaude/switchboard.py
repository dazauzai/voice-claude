"""The switchboard: every line (manager, scratch, projects), which one the microphone is on, the mailboxes, the
permission gate, and the control requests coming from the lines' tool servers. Lives inside the daemon."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import access as accessmod, gateway, managers, paths, projects, prompts, risk
from .claude import DESKTOP_SERVER, MANAGER_SERVER, SETTINGS_SERVER, ClaudeSession, server_spec
from .engine_codex import CodexSession
from .config import PERMISSION_LABELS, PERMISSION_VALUES, Config, normalize_permission_value, save as config_save
from .jobs import Job, Jobs, looks_endless
from .line import CLOSED, DEAD, IDLE, STARTING, WAITING, WORKING, Line, file_hash
from .projects import Project

log = logging.getLogger(__name__)

MANAGER, SCRATCH = "manager", "scratch"
DESKTOP_ACTIONS = {"screenshot", "click", "type", "hotkey", "scroll", "open_app"}
# Spoken name -> what Claude Code is given. Fable is slow and expensive, so its approval question says so.
MODELS = dict(accessmod.CLAUDE_MODELS)  # one source; [providers.claude] models may override the ids
MODEL_ALIASES = {"opus": "opus", "sonnet": "sonnet", "fable": "fable", "fable5": "fable", "fable5.1": "fable",
                 "飞宝": "fable", "飞five": "fable", "飞五": "fable", "飞5": "fable", "菲宝": "fable",
                 "claude-fable-5-1": "fable", "claude-opus-5-5": "opus", "claude-sonnet-5-5": "sonnet",
                 "opus5.5": "opus", "sonnet5.5": "sonnet"}
MODEL_NOTES = {"fable": "Fable 回答慢、额度贵。"}
# Spoken name -> provider id (who answers). Keys are compared without spaces, lower-case.
PROVIDER_ALIASES = {"claude": "claude", "克劳德": "claude", "anthropic": "claude", "cloud": "claude",
                    "deepseek": "deepseek", "深度求索": "deepseek", "dipsik": "deepseek", "deepsick": "deepseek",
                    "kimi": "kimi", "月之暗面": "kimi", "moonshot": "kimi", "kimmy": "kimi", "基米": "kimi",
                    "gpt": "openai", "openai": "openai", "chatgpt": "openai", "open-ai": "openai"}


def normalize_provider(text: str) -> str | None:
    t = str(text).strip().lower().replace(" ", "").replace("　", "")
    return PROVIDER_ALIASES.get(t)


def normalize_model(text: str) -> str | None:
    """'飞 five' / 'Fable 5' / 'claude-fable-5-1' -> 'fable'; unknown -> None."""
    t = str(text).strip().lower().replace(" ", "").replace("　", "")
    return MODEL_ALIASES.get(t)
FRONTEND_PATHS = {"get_settings", "list_devices", "set_font_size", "set_microphone", "set_speaker",
                  "list_voices", "set_voice", "set_speed"}
PHRASES = {"back_to_manager", "scratch", "background_mode", "background_status", "shutdown_daemon", "transparent_on", "transparent_off",
           "rewind_last"}
TRANSPARENT_QUESTION = "开启通透模式：除删除和覆盖已有文件外，所有操作直接执行，不再询问。设为禁止的仍然禁止。可以吗？"
TRANSPARENT_PASSED = "通透模式：直接执行。"  # what the model hears: nobody was asked, so never "用户同意了"


class Switchboard:
    def __init__(self, cfg: Config, *, jobs: Jobs, emit: Callable[[str, dict], None],
                 forward: Callable[[str, dict], dict], session_factory: Callable[[Line, str], Any] | None = None,
                 scan_projects: Callable[[], list[Project]] = projects.scan, now: Callable[[], float] = time.monotonic,
                 ask_timeout: float = 60.0, control_port: int = 0,
                 detect_access: Callable[[str], accessmod.AccessStatus] = accessmod.detect,
                 detect_provider: Callable[..., accessmod.AccessStatus] = accessmod.detect_provider,
                 reach: Callable[..., tuple[bool, str]] | None = None):
        self.cfg = cfg
        self.jobs = jobs
        self.emit = emit  # events to the front end
        self.forward = forward  # requests only the front end can serve
        self.session_factory = session_factory or self._claude_session
        self.scan_projects = scan_projects
        self.now = now
        self.ask_timeout = ask_timeout
        self.control_port = control_port
        self.detect_access = detect_access  # Claude (subscription / key); tests patch it
        self.detect_provider = detect_provider  # the other providers
        self.reach = reach or accessmod.check_reach  # 3-second TLS probe of a vendor before Claude Code is started on it
        self.access = accessmod.AccessStatus(False, "none", "还没检测接入方式")
        # Who answers by default (new lines, and the manager): [providers] default. Each line keeps its own.
        self.provider = str(getattr(cfg, "provider", "claude") or "claude").lower()
        if not accessmod.known_provider(self.provider):
            log.warning("unknown provider %r in config; using claude", self.provider)
            self.provider = "claude"
        # The translation gateway (LiteLLM) for OpenAI-format providers; started on first use, stopped with us.
        self.gateway = gateway.Gateway(cfg)
        self.manager_fallback = False  # the model manager could not start (its gateway, say): rules until access changes
        # Which brain serves the manager: "auto" = Claude Code when it is reachable, else the plain rules;
        # "claude" / "rules" force one. The manager is never tied to one vendor.
        self.manager_engine = str(getattr(cfg, "manager_engine", "auto") or "auto").lower()
        self.lock = threading.RLock()
        self.lines: dict[str, Line] = {}
        self.active_id: str = ""
        self.index: list[Project] = []
        self.pending: dict[str, dict] = {}  # ask requests waiting for the user's answer
        self.ask_lock = threading.Lock()  # one spoken question at a time
        self.brake_gen = 0  # counts emergency stops: a request queued before one is never asked after it
        self.rewound_backup: dict[str, tuple[list[dict], int]] = {}  # line -> (turns its latest rewind dropped, files restored)
        self.transcript_tail = projects.transcript_tail  # tests replace it
        self.frontend_pid = 0  # the window's process: the desktop tools never type into it
        self.forgotten: set[str] = set()  # projects whose saved conversation was dropped in this run
        self.lines_lock = threading.Lock()  # every read-modify-write of lines.json, from any thread
        self.max_lines = int(getattr(cfg, "max_lines", 3))
        self.idle_seconds = float(getattr(cfg, "idle_minutes", 10)) * 60
        # VOICECLAUDE_START_WITH lets a self-test pick its own project without touching config.toml
        self.start_with = os.environ.get("VOICECLAUDE_START_WITH") or str(getattr(cfg, "start_with", MANAGER) or MANAGER)
        self.stopping = False
        self.shutdown_hook: Callable[[], None] | None = None  # set by the daemon: how to actually exit
        self.turn_started: dict[str, float] = {}  # line id -> when the current turn was sent (latency log)
        self.line_started: dict[str, float] = {}  # line id -> when its claude process was launched
        self._last_ask_timed_out = False

    # ---------- startup ----------

    GREETING = "我是总机，要去哪个项目？"

    def start(self) -> None:
        """Check how we reach Claude; with a working access method bring up the manager line. Without one the
        daemon still runs (settings, jobs, the access page) and tells the front end what is missing."""
        paths.ensure()
        self.refresh_index()
        self.access = self.provider_status(self.provider)
        log.info("access: %s %s; manager engine %s -> %s", self.access.method, self.access.message,
                 self.manager_engine, self.manager_brain())
        if not self.access.ok:
            self.emit("notice", {"text": "还没接入：" + self.access.message + " 总机先用基本功能。"})
        self.start_lines()

    def manager_brain(self) -> str:
        """'claude' or 'rules' for the manager line, given the config and what is reachable."""
        if self.manager_engine == "rules" or self.manager_fallback:
            return "rules"
        if self.manager_engine == "claude":
            return "claude" if self.access.ok else "rules"
        return "claude" if self.access.ok else "rules"

    def start_lines(self) -> None:
        """Only the manager line starts now; the scratch line and project lines come up when first needed
        (every Claude process plus its tool servers costs seconds of CPU at startup)."""
        if MANAGER in self.lines and self.lines[MANAGER].state not in (DEAD, CLOSED):
            return
        self.open_line(MANAGER, "总机", str(paths.MANAGER_DIR), MANAGER)
        if self.lines[MANAGER].state == DEAD and self.manager_brain() == "claude":
            # e.g. the default vendor's gateway would not start: a manager on rules beats no manager at all
            self.manager_fallback = True
            self.emit("notice", {"text": "总机的模型线起不来，先用规则总机（只能转接和报后台）。"})
            self.open_line(MANAGER, "总机", str(paths.MANAGER_DIR), MANAGER)
        if self.start_with.lower() in (MANAGER, "总机", ""):
            self.switch(MANAGER)  # announces the greeting; the line takes the user's words right away
        else:
            r = self.switch_to_project(self.start_with)
            if not r["ok"]:
                self.switch(MANAGER, announce=r["message"])

    # ---------- access (how we reach Claude) ----------

    def access_status(self) -> dict:
        return {"ok": True, "message": self.access.message, "access": self.access.as_dict()}

    def provider_status(self, pid: str) -> accessmod.AccessStatus:
        """Can this provider answer right now? (Claude through detect_access, the others through detect_provider.)"""
        pid = normalize_provider(pid) or pid
        if pid == "claude":
            st = self.detect_access(self.cfg.command)
            st.provider, st.provider_name = "claude", accessmod.provider_for("claude", self.cfg).name
            st.model = st.model or str(self.cfg.model or "")
            return st
        return self.detect_provider(pid, self.cfg.command, cfg=self.cfg)

    def _after_access_change(self, mark_lines: bool = True) -> dict:
        """Re-detect the default provider and fix the manager up; mark_lines=False when the caller already marked
        the lines that actually change (a provider switch), so nothing restarts twice or is announced twice."""
        before = self.manager_brain()
        self.manager_fallback = False  # the model manager gets another chance now that something changed
        self.access = self.provider_status(self.provider)
        log.info("access now: %s %s", self.access.method, self.access.message)
        if self.access.ok:
            manager = self.lines.get(MANAGER)
            if manager is None or manager.state in (DEAD, CLOSED):
                self.start_lines()
            elif before != self.manager_brain():  # the manager was running on rules: swap in the real brain
                self._close_line(manager, "接入好了，换成真正的总机")
                self.start_lines()
            elif mark_lines:  # lines are up: reconnect them so the new method takes effect after their turns
                for l in self.lines.values():
                    l.pending_restart = True
                self.emit("notice", {"text": "接入方式变了，各条线这一轮结束后重新连接。"})
        return {"ok": self.access.ok, "message": self.access.message, "access": self.access.as_dict()}

    def set_api_key(self, key: str) -> dict:
        st = accessmod.set_api_key(key)
        if not st.ok and st.method == "none" and "样子不对" in st.message:
            return {"ok": False, "message": st.message, "access": st.as_dict()}
        return self._after_access_change()

    def clear_api_key(self) -> dict:
        accessmod.clear_api_key()
        return self._after_access_change()

    def test_access(self) -> dict:
        """test_provider("claude") under its 7a name."""
        r = self.test_provider("claude")
        return {**r, "access": self.provider_status("claude").as_dict()}

    # ---------- providers: who answers (Claude / DeepSeek / Kimi / GPT); Claude Code is the engine, except GPT
    # through a ChatGPT plan (Codex) ----------

    @staticmethod
    def _own_route(st: accessmod.AccessStatus) -> bool:
        """GPT through the user's own Codex login: Codex reaches its own endpoints, so neither our reachability probe
        nor the LiteLLM gateway applies (a signed-in Codex must work on an install without LiteLLM)."""
        return st.method == "codex_login"

    def _ensure_gateway(self, pid: str) -> None:
        """OpenAI-format providers talk through the LiteLLM gateway: have it running before a line starts."""
        if accessmod.provider_for(pid, self.cfg).via != "litellm":
            return
        ok, msg = self.gateway.ensure(pid)
        if not ok:
            raise RuntimeError(f"{accessmod.provider_for(pid, self.cfg).name}：{msg}")

    def _key_changed(self, pid: str) -> None:
        """A key was saved or cleared: a proxy holding the old one stops (the next line start brings it back with
        the current keys), and lines on that provider reconnect after their turns."""
        if accessmod.provider_for(pid, self.cfg).via == "litellm":
            self.gateway.stop()
        for l in self.lines.values():
            if l.provider == pid and l.state not in (DEAD, CLOSED):
                l.pending_restart = True

    def _provider_info(self, pid: str) -> dict:
        p = accessmod.provider_for(pid, self.cfg)
        st = self.access if pid == self.provider else self.provider_status(pid)
        d = p.as_dict()
        if pid == "claude":
            login_state = "logged_in" if st.method == "subscription" else ("not_installed" if not st.claude else "not_logged_in")
        elif pid == "openai":
            login_state = "logged_in" if st.method == "codex_login" else accessmod.codex_login_state()[0]
        else:
            login_state = ""
        d.update(configured=bool(accessmod.provider_key(pid)), account=st.account, ok=st.ok, message=st.message,
                 is_default=pid == self.provider, model=st.model or d["model"], login_state=login_state,
                 engine="codex" if st.method == "codex_login" else "claude")
        return d

    def login_provider(self, pid: str, mode: str = "browser") -> dict:
        """The access page's "登录…" button: run that vendor's own login (we never handle the tokens)."""
        pid = normalize_provider(pid) or pid
        if pid == "openai":
            from . import engine_codex
            r = engine_codex.start_login(mode=mode)
        elif pid == "claude":
            r = accessmod.start_claude_login(self.cfg.command)
        elif accessmod.known_provider(pid):
            return {"ok": False, "message": f"{accessmod.provider_for(pid, self.cfg).name}没有登录这种方式，填 key 就行。", "default": self.provider}
        else:
            return {"ok": False, "message": f"没有叫 {pid} 的厂商", "default": self.provider}
        return {**r, "provider": self._provider_info(pid), "default": self.provider, "access": self.access.as_dict()}

    def logout_provider(self, pid: str) -> dict:
        pid = normalize_provider(pid) or pid
        if pid == "openai":
            from . import engine_codex
            ok, msg = engine_codex.logout()
        elif pid == "claude":
            ok, msg = False, "Claude Code 的登录请在终端里用 claude 自己退出（/logout）。"
        else:
            return {"ok": False, "message": "这家没有登录可退", "default": self.provider}
        r = self._after_access_change() if (ok and pid == self.provider) else {"access": self.access.as_dict()}
        return {"ok": ok, "message": msg, "provider": self._provider_info(pid), "default": self.provider, "access": r["access"]}

    def list_providers(self) -> dict:
        return {"ok": True, "message": "", "default": self.provider,
                "providers": [self._provider_info(pid) for pid in accessmod.PROVIDER_ORDER],
                "lines": [{"line": l.id, "name": l.name, "provider": l.provider, "model": getattr(l.session, "model", "") or ""}
                          for l in self.lines.values() if l.state not in (DEAD, CLOSED)]}

    def set_provider_key(self, pid: str, key: str, switch: bool = False) -> dict:
        """Save a key. switch=True (the access page's "save and use it") makes it the default when it can answer;
        otherwise the default only moves to it when the current default cannot answer at all."""
        pid = normalize_provider(pid) or pid
        ok, msg = accessmod.set_provider_key(pid, key)
        if not ok:
            return {"ok": False, "message": msg, "default": self.provider}
        self._key_changed(pid)
        if pid == self.provider:  # ok = stored; whether it can answer is in provider.ok / message (docs 7b)
            r = self._after_access_change()
            return {"ok": True, "message": f"{msg} {r['message']}", "provider": self._provider_info(pid),
                    "default": self.provider, "access": r["access"]}
        if (switch or not self.access.ok) and self.provider_status(pid).ok and self.reach(pid, self.cfg)[0]:
            self._set_default_provider(pid)
            msg += f" 默认改成了{accessmod.provider_for(pid, self.cfg).name}。"
        return {"ok": True, "message": msg, "provider": self._provider_info(pid), "default": self.provider,
                "access": self.access.as_dict()}

    def clear_provider_key(self, pid: str) -> dict:
        pid = normalize_provider(pid) or pid
        ok, msg = accessmod.clear_provider_key(pid)
        if not ok:
            return {"ok": False, "message": msg, "default": self.provider}
        self._key_changed(pid)
        if pid == self.provider:
            # The default lost its key: the first provider that can answer takes over, else the rules manager.
            fallback = next((q for q in accessmod.PROVIDER_ORDER
                             if q != pid and self.provider_status(q).ok and self.reach(q, self.cfg)[0]), None)
            if fallback:
                self._set_default_provider(fallback)
                msg += f" 默认改成了{accessmod.provider_for(fallback, self.cfg).name}。"
                return {"ok": True, "message": msg, "provider": self._provider_info(pid), "default": self.provider,
                        "access": self.access.as_dict()}
            r = self._after_access_change()
            return {"ok": True, "message": f"{msg} {r['message']}", "provider": self._provider_info(pid),
                    "default": self.provider, "access": r["access"]}
        return {"ok": True, "message": msg, "provider": self._provider_info(pid), "default": self.provider,
                "access": self.access.as_dict()}

    def test_provider(self, pid: str) -> dict:
        """Really talk to one provider once through Claude Code (starts a claude process, 5-40 s)."""
        pid = normalize_provider(pid) or pid
        if accessmod.known_provider(pid) and accessmod.provider_for(pid, self.cfg).via == "litellm" \
                and not self._own_route(self.provider_status(pid)):
            ok, msg = self.gateway.ensure(pid)
            if not ok:
                return {"ok": False, "message": msg, "seconds": 0.0, "provider": self._provider_info(pid)}
        r = accessmod.test_provider(pid, self.cfg.command, cfg=self.cfg)
        return {**r, "provider": self._provider_info(pid) if accessmod.known_provider(pid) else {"id": pid}}

    def switch_provider(self, pid: str, line_id: str = "") -> dict:
        """From the front end (the click is the approval) or the rules manager. line_id="" = the default plus
        every open line; a line id = that line only."""
        key = normalize_provider(pid) or pid
        if not accessmod.known_provider(key):
            return {"ok": False, "message": f"厂商只能是 Claude、DeepSeek、Kimi 或 GPT（OpenAI），不认识 {pid}", "default": self.provider}
        st = self.provider_status(key)
        if not st.ok:
            return {"ok": False, "message": st.message, "default": self.provider, "access": self.access.as_dict()}
        if not self._own_route(st):
            ok, why = self.reach(key, self.cfg)
            if not ok:
                return {"ok": False, "message": why, "default": self.provider, "access": self.access.as_dict()}
            try:
                self._ensure_gateway(key)  # a vendor behind the translation layer must prove the layer starts first
            except RuntimeError as e:
                return {"ok": False, "message": str(e), "default": self.provider, "access": self.access.as_dict()}
        if line_id:
            line = self.lines.get(line_id)
            if line is None or line.state in (DEAD, CLOSED):
                return {"ok": False, "message": "那条线不在了", "default": self.provider}
            if line.provider == key:
                return {"ok": True, "message": f"{line.name}现在就在{st.provider_name}上", "default": self.provider}
            line.pending_provider = key
            self._apply_pending(line)  # an idle line changes right away; a busy one after its turn
            return {"ok": True, "message": f"{line.name}换到{st.provider_name}（{st.model}）", "default": self.provider,
                    "access": self.access.as_dict()}
        if key == self.provider:
            return {"ok": True, "message": f"默认现在就是{st.provider_name}", "default": self.provider,
                    "access": self.access.as_dict()}
        self._set_default_provider(key)
        return {"ok": True, "message": f"默认改成{st.provider_name}（{st.model}）；开着的线路这一轮结束后换过去。",
                "default": self.provider, "access": self.access.as_dict()}

    def _set_default_provider(self, pid: str) -> None:
        """New lines use it; open lines (the manager too) follow after their turns."""
        before = self.manager_brain()
        self.provider = pid
        self.cfg.provider = pid
        config_save("providers", "default", pid)
        for l in self.lines.values():
            if l.state in (DEAD, CLOSED) or l.provider == pid:
                continue
            if l.kind == MANAGER and before == "rules":
                continue  # _after_access_change replaces the rules manager with a real one on the new provider
            l.pending_provider = pid
        self._after_access_change(mark_lines=False)  # the lines that change are marked above, once
        for l in list(self.lines.values()):
            if l.pending_provider:
                self._apply_pending(l)

    def _switch_provider_spoken(self, line_id: str, text: str) -> dict:
        """The switch_provider tool: "切到 DeepSeek" said to a line changes that line, said to the manager changes
        the default and every project line. Always through the spoken gate (permissions: switch_provider)."""
        key = normalize_provider(text)
        if key is None:
            return {"ok": False, "message": "厂商只能是 Claude、DeepSeek、Kimi 或 GPT（OpenAI）"}
        line = self.lines.get(line_id)
        if line is None or line.session is None:
            return {"ok": False, "message": "那条线不在了"}
        st = self.provider_status(key)
        if not st.ok:
            return {"ok": False, "message": f"{st.provider_name or key}现在用不了：{st.message}"}
        if not self._own_route(st):
            ok, why = self.reach(key, self.cfg)
            if not ok:
                return {"ok": False, "message": f"{st.provider_name}现在用不了：{why}"}
            try:
                self._ensure_gateway(key)
            except RuntimeError as e:
                return {"ok": False, "message": f"现在用不了：{e}"}
        if line.kind == MANAGER:
            targets = [l for l in self.lines.values() if l.kind != MANAGER and l.state not in (DEAD, CLOSED)]
            if key == self.provider and all(t.provider == key for t in targets):
                return {"ok": True, "message": f"现在默认就是{st.provider_name}"}
            ok, why = self.gate(line_id, "switch_provider", f"要把默认和所有项目线路换到{st.provider_name}（{st.model}）吗？")
            if not ok:
                return {"ok": False, "message": why}
            self._set_default_provider(key)
            return {"ok": True, "message": f"用户同意了。默认改成{st.provider_name}，开着的线路这一轮结束后换过去（总机自己也会）。"}
        if line.provider == key:
            return {"ok": True, "message": f"这条线现在就在{st.provider_name}上"}
        ok, why = self.gate(line_id, "switch_provider", f"要把这条线换到{st.provider_name}（{st.model}）吗？")
        if not ok:
            return {"ok": False, "message": why}
        line.pending_provider = key
        return {"ok": True, "message": f"用户同意了。这一轮结束后换到{st.provider_name}，对话尽量保留（接不上就重新开始）。"}

    def _ensure_scratch(self) -> Line:
        line = self.lines.get(SCRATCH)
        if line is None or line.state in (DEAD, CLOSED):
            line = self.open_line(SCRATCH, "杂项", str(paths.SCRATCH_DIR), SCRATCH)
        return line

    def refresh_index(self) -> list[Project]:
        try:
            self.index = self.scan_projects()
        except Exception:
            log.exception("project scan failed")
            self.index = self.index or []
        return self.index

    # ---------- lines ----------

    def _claude_session(self, line: Line, resume: str):
        if line.kind == MANAGER:
            prompt = prompts.manager_prompt(projects.index_text(self.index))
            servers = {MANAGER_SERVER: server_spec("manager_server.py"), SETTINGS_SERVER: server_spec("mcp_server.py")}
            # [manager] model is a Claude name; on another provider the manager uses that provider's model
            model = str(getattr(self.cfg, "manager_model", "sonnet") or "sonnet") if line.provider == "claude" else None
            effort = str(getattr(self.cfg, "manager_effort", "low") or "")
        else:
            prompt = prompts.SCRATCH_PROMPT if line.kind == SCRATCH else prompts.PROJECT_PROMPT
            servers = {SETTINGS_SERVER: server_spec("mcp_server.py")}
            if self.cfg.desktop_enabled:
                servers[DESKTOP_SERVER] = server_spec("desktop.py")
            model, effort = None, None
        st = self.provider_status(line.provider)
        if st.method == "codex_login":
            # The user's own Codex (ChatGPT plan) is the engine for this line; its approvals come through our gate.
            line.engine = "codex"
            (paths.LINES_DIR / line.id).mkdir(parents=True, exist_ok=True)
            s = CodexSession(self.cfg, prompt, line.on_session, cwd=line.cwd, line_id=line.id, resume=resume,
                             model=model if line.provider != "claude" else None, effort=effort, servers=servers,
                             ask=lambda action, question, detail, facts=None, _id=line.id: self.gate(_id, action, question, detail, facts),
                             exe=st.claude or None, stderr_path=paths.LINES_DIR / line.id / "codex-stderr.log")
            s.port = self.control_port
            return s
        line.engine = "claude"
        ok, why = self.reach(line.provider, self.cfg)
        if not ok:
            raise RuntimeError(why)
        self._ensure_gateway(line.provider)  # raises when the translation layer cannot serve this provider
        s = ClaudeSession(self.cfg, prompt, line.on_session, cwd=line.cwd, line_id=line.id, resume=resume,
                          model=model, effort=effort, servers=servers, provider=line.provider)
        if resume and getattr(line, "resume_fork", None):
            s.fork_at = tuple(line.resume_fork)  # the rewind made before the line was closed, still to be made real
        s.port = self.control_port
        return s

    def _engine_for(self, pid: str) -> str:
        return "codex" if self._own_route(self.provider_status(pid)) else "claude"

    def open_line(self, line_id: str, name: str, cwd: str, kind: str, resume: str = "", resume_engine: str = "",
                  resume_fork: list | None = None) -> Line:
        if resume and resume_engine and resume_engine != self._engine_for(self.provider):
            # a Codex thread cannot be continued by Claude Code, nor the other way round: a new conversation
            log.info("[%s] saved conversation %s is %s's; this line runs on the other engine: starting fresh",
                     line_id, resume[:8], resume_engine)
            resume = ""
        with self.lock:
            if line_id in self.lines and self.lines[line_id].state not in (DEAD, CLOSED):
                return self.lines[line_id]
            Path(cwd).mkdir(parents=True, exist_ok=True)
            factory = self.session_factory
            if kind == MANAGER and self.manager_brain() == "rules":  # no model needed; whatever the factory is
                factory = lambda ln, resume: managers.RulesSession(ln, self)  # noqa: E731
            line = Line(line_id, name, cwd, kind, factory, lambda i, k, d: self.on_line_event(i, k, d), now=self.now)
            line.provider = self.provider  # new lines answer through the default provider
            line.resume_fork = list(resume_fork) if resume and resume_fork else None
            self.lines[line_id] = line
        self.line_started[line_id] = time.time()
        try:
            line.start(resume)
            if resume and line.engine == "claude":
                # the point a rewind of this process's first sentence goes back to: the saved conversation's end (or
                # the pending rewind's point, when the line was closed before that rewind was made)
                at = line.resume_fork[0] if line.resume_fork else self.transcript_tail(resume)
                line.base_point = {"last": at} if at else None
        except Exception as e:
            log.exception("[%s] could not start", line_id)
            line.state = DEAD
            self.emit("notice", {"text": f"{name}这条线起不来：{e}"})
        self._emit_line(line)
        return line

    def line_for_project(self, p: Project) -> Line:
        line_id = "p-" + hashlib.sha1(p.key.encode("utf-8")).hexdigest()[:8]
        with self.lock:
            existing = self.lines.get(line_id)
            if existing and existing.state not in (DEAD, CLOSED):
                return existing
            self._make_room()
        rec = self._read_line_record(p.key)
        if isinstance(rec, dict) and rec.get("session_id"):  # what this run saved last, not the start-up index
            p.session_id = str(rec["session_id"])
            p.session_engine = str(rec.get("engine") or projects.engine_of_session(p.session_id))
            fork = rec.get("fork_at")
            p.session_fork = list(fork) if isinstance(fork, list) and len(fork) == 2 and fork[0] else None
        elif isinstance(rec, dict) or rec is None and p.session_id and self._forgotten(p.key):
            p.session_id, p.session_engine, p.session_fork = "", "", None
        line = self.open_line(line_id, p.display_name, p.cwd, "project", resume=p.session_id,
                              resume_engine=getattr(p, "session_engine", ""), resume_fork=getattr(p, "session_fork", None))
        if isinstance(rec, dict) and rec.get("note") and rec.get("session_id") == p.session_id:
            line.pending_note = str(rec["note"])  # the model still has to hear about files put back
        return line

    def _make_room(self) -> None:
        """Close the least recently used closable project line when the limit is reached (lock held)."""
        open_projects = [l for l in self.lines.values() if l.kind == "project" and l.state not in (DEAD, CLOSED)]
        while len(open_projects) >= self.max_lines:
            # the active line is about to be replaced by the new one, so it is a candidate too
            victims = [l for l in open_projects if not l.keep_alive]
            if not victims:
                return
            victim = min(victims, key=lambda l: l.last_used)
            self._close_line(victim, "太多线路了，先关掉最久没用的")
            open_projects.remove(victim)

    def _close_line(self, line: Line, why: str) -> None:
        log.info("[%s] closing: %s", line.id, why)
        line.close()
        self._emit_line(line)

    def _emit_line(self, line: Line) -> None:
        self.emit("line", {"line": line.id, "name": line.name, "kind": line.kind, "state": line.state,
                           "active": line.id == self.active_id, "unread": len(line.unread()), "provider": line.provider,
                           "engine": line.engine})

    def active(self) -> Line | None:
        return self.lines.get(self.active_id)

    def greeting(self) -> str:
        """Spoken when a front end connects: where the microphone is right now."""
        line = self.active()
        if line is None:
            return ("还没接入：" + self.access.message) if not self.access.ok else ""
        if line.kind == MANAGER:
            if not self.access.ok:
                return "还没接入：" + self.access.message + " 我是总机，先只能报后台和换服务商。"
            return self.GREETING
        return f"现在接着{line.name}。" + (line.summary_for_return() or "")

    def switch(self, line_id: str, announce: str | None = None) -> dict:
        with self.lock:
            line = self.lines.get(line_id)
            if line is None or line.state in (DEAD, CLOSED):
                return {"ok": False, "message": "那条线不在了"}
            self.active_id = line_id
            line.last_used = self.now()
            for l in self.lines.values():
                self._emit_line(l)
            summary = line.summary_for_return()
            line.mark_read()
            if announce is None and line.kind == MANAGER:
                announce = self.GREETING
        if announce:
            self.emit("notice", {"text": announce})
        if summary:
            self.emit("notice", {"text": summary})
        return {"ok": True, "message": announce or "", "line": line_id}

    def switch_to_project(self, text: str, confirm: bool = False) -> dict:
        """confirm=True (the manager's switch_to tool): ask the user out loud before handing over, so a
        misheard project name never lands them somewhere unexpected."""
        cands = projects.resolve(text, self.index)
        if not cands:
            self.refresh_index()
            cands = projects.resolve(text, self.index)
        if not cands:
            names = "、".join(p.display_name for p in self.index[:8])
            return {"ok": False, "message": f"没有找到叫“{text}”的项目。现有的项目：{names}"}
        if len(cands) > 1:
            names = "、".join(p.display_name for p in cands[:5])
            return {"ok": False, "message": f"有几个都像：{names}。你指哪个？", "candidates": [p.display_name for p in cands]}
        p = cands[0]
        label = p.display_name if p.display_name.endswith("项目") else f"{p.display_name}项目"
        if confirm and not self.ask_frontend(MANAGER, "switch_to", f"要接到{label}吗？"):
            return {"ok": False, "message": f"用户没有同意接到{label}。"}
        if not self.access.ok:
            return {"ok": False, "message": f"还没接入，{label}的线路起不来：{self.access.message}"}
        line = self.line_for_project(p)
        if line.state == DEAD:
            return {"ok": False, "message": f"{p.display_name}这条线起不来"}
        self.switch(line.id, announce=f"接到{label}")
        return {"ok": True, "message": f"已接到{label}", "line": line.id}

    # ---------- talking ----------

    def say(self, line_id: str, text: str) -> dict:
        line = self.lines.get(line_id)
        if line is not None and line.state == DEAD and line.session is not None and not self.stopping:
            why = self._reconnect(line)  # its process went away: the next words bring it back
            if why:
                return {"ok": False, "message": why}
        if line is None or line.state in (DEAD, CLOSED):
            if not self.access.ok:
                return {"ok": False, "message": "还没接入：" + self.access.message}
            return {"ok": False, "message": "那条线不在了"}
        if getattr(line, "rewinding", False):
            return {"ok": False, "message": "正在撤回，稍等。"}
        if line.kind == MANAGER:
            self._manager_log("你", text)
        turn = "t" + uuid.uuid4().hex[:10]  # this user message on this line, for rewinding to it
        try:
            line.say(text, turn)
        except RuntimeError as e:
            return {"ok": False, "message": str(e)}
        self.turn_started[line_id] = time.time()
        self._emit_line(line)
        return {"ok": True, "message": "", "turn": turn}

    # ---------- rewind: back to just before one of the user's sentences ----------

    def rewind(self, line_id: str = "", turn: str | None = None, steps: int = 1) -> dict:
        """Drop the user turn `turn` and everything after it (or, without `turn`, the last `steps` user turns). The
        conversation continues from just before it; files are put back after one spoken question (Claude Code lines
        only: Codex cannot). One rewind per line at a time. Returns the first dropped sentence for the text box."""
        line = self.lines.get(line_id or self.active_id or "")
        if line is None or line.session is None or line.state in (DEAD, CLOSED):
            return self._rewind_no("那条线不在了")
        with self.lock:
            if line.rewinding:
                return self._rewind_no("正在撤回，稍等。")
            line.rewinding = True
        try:
            return self._rewind(line, turn, steps)
        finally:
            line.rewinding = False

    @staticmethod
    def _rewind_no(message: str, restored: int = 0) -> dict:
        return {"ok": False, "message": message, "text": "", "files_restored": restored}

    @staticmethod
    def _busy(line: Line) -> bool:
        return line.state == WORKING or bool(getattr(line.session, "turn_active", False))

    def _rewind(self, line: Line, turn: str | None, steps: int) -> dict:
        if self._busy(line):
            return self._rewind_no("正在回答，先打断再撤回。")
        log_ = line.turn_log
        if turn:
            k = next((i for i, e in enumerate(log_) if e["turn"] == turn), None)
            if k is None:
                return self._rewind_no("找不到那一句（可能是这条线重新开始以前说的），没有撤回。")
        else:
            k = max(0, len(log_) - max(1, int(steps or 1))) if log_ else None
        if k is None:
            return self._rewind_no("没有可以撤回的话。")
        dropped = list(log_[k:])
        first = dropped[0]
        codex = line.engine == "codex"
        # ---- where the conversation goes back to, checked before anything is asked or changed ----
        if k > 0:
            keep = log_[k - 1]
        elif line.log_from_start:
            keep = None  # the first sentence of the conversation: a new conversation
        elif line.base_point and line.base_point.get("codex_turn" if codex else "last"):
            keep = line.base_point  # a resumed conversation: back to where it stood when this process took it up
        else:
            return self._rewind_no("这条线是接着以前的对话开的，撤回不到这次接上后的第一句之前。")
        if keep is not None and not (keep.get("codex_turn") if codex else keep.get("last") and first.get("prompt")):
            return self._rewind_no("找不到撤回的位置（这几轮的记录不全），没有撤回。")
        # ---- files ----
        restored, note, said = 0, "", ""
        ours = {self._norm_path(line, f) for e in dropped for f in e["files"]}
        created = {self._norm_path(line, f) for e in dropped for f in e.get("created", ())}
        written = {}  # how each file looked after the last dropped turn that wrote it
        for e in dropped:
            for f, h in (e.get("hashes") or {}).items():
                written[self._norm_path(line, f)] = h
        if codex or not hasattr(line.session, "rewind_files"):
            if ours:
                note = f"改过的 {len(ours)} 个文件没有改回去（这条线只能撤回对话）。"
        elif first.get("prompt"):
            dry = line.session.rewind_files(first["prompt"], dry_run=True)
            shown = {self._norm_path(line, f): os.path.join(line.cwd, f) for e in dropped for f in e["files"]}
            shown.update({self._norm_path(line, f): f for f in dry["files"] or []})  # as Claude Code spells them
            changed = sorted({self._norm_path(line, f) for f in dry["files"]}) if dry["files"] is not None else sorted(ours)
            if changed and not dry["ok"]:
                note = f"文件没法改回去：{dry['error']}。"
            elif changed:
                outside = [f for f in changed if f not in ours]
                deleted = [f for f in changed if f in created]
                edited = [f for f in changed if written.get(f) and file_hash(f) not in (None, written[f])]
                show = lambda fs: [shown.get(f, f) for f in fs]  # noqa: E731
                answer = self.ask_frontend(line.id, "rewind_files",
                                           self._files_question(show(changed), show(outside), show(deleted), show(edited)),
                                           "\n".join(shown.get(f, f) + ("（会被删掉）" if f in deleted else "")
                                                     + ("（之后又被改过）" if f in edited else "")
                                                     + ("（不只是这几轮改的）" if f in outside else "") for f in changed))
                if self._busy(line):  # it started answering while the question was up
                    return self._rewind_no("正在回答，先打断再撤回。")
                if answer is True:
                    res = line.session.rewind_files(first["prompt"])
                    if res["ok"]:
                        done = {self._norm_path(line, f) for f in (res["files"] if res["files"] is not None else changed)}
                        # what still differs afterwards was not put back (a link skipped, a missing backup, a locked file)
                        check = line.session.rewind_files(first["prompt"], dry_run=True)
                        still = {self._norm_path(line, f) for f in check["files"] or []} if check["ok"] else set()
                        back = sorted(done - still)
                        restored = len(back)
                        said = self._names(show(back)) or "文件"
                        missed = len(done & still) or res["skipped"]
                        note = (f"{said}已改回去。" if back else "文件没有改回去。") \
                            + (f"{missed} 个没能改回去" + ("（其中有为安全跳过的链接文件）" if res["skipped"] else "") + "。" if missed else "") \
                            + "用命令改的文件不在内。"
                    else:
                        note = f"文件没能改回去：{res['error']}。"
                else:
                    note = "文件保持现在的样子。"
        elif ours:
            note = "文件没法改回去（这一句的记录不全）。"
        # ---- the conversation ----
        try:
            if keep is None:
                line.session.restart(fresh=True)  # nothing before it to keep: a new conversation
                self._forget_session(line)
                line.session_id, line.started_resume, line.base_point, line.log_from_start = "", "", None, True
            else:
                line.session.rewind_to(keep, first, single=len(dropped) == 1)
            line.pending_note = ""  # whatever a half-done rewind left for the model is about turns now gone
        except Exception as e:
            # the line is back on its conversation as it was; the turn record stays as it is
            log.warning("[%s] rewind failed: %s", line.id, e)
            if restored:
                line.pending_note = f"（说明：{said}已改回到「{self._short(first['text'])}」之前的样子，但对话没有撤回。）"
                return self._rewind_no(f"文件已改回去，但对话没撤回：{e}。", restored)
            return self._rewind_no(f"撤回没成功：{e}。对话保持原样。")
        self.rewound_backup[line.id] = (dropped, restored)  # put back if the engine refuses the truncated conversation
        line.turn_log[:] = [e for e in line.turn_log if not any(e is d for d in dropped)]  # only these: not words said since
        if not self._busy(line):
            line.state, line.reply = IDLE, ""
        self._save_lines()  # the pending fork point on disk at once, not at the next tick
        self.emit("rewound", {"line": line.id, "turn": first["turn"], "text": first["text"]})
        self._emit_line(line)
        log.info("[%s] rewound %d turn(s) to before %s; %s", line.id, len(dropped), first["turn"], note or "no files")
        return {"ok": True, "message": f"撤回到「{self._short(first['text'])}」之前。" + note, "text": first["text"],
                "files_restored": restored}

    @staticmethod
    def _short(text: str) -> str:
        return text if len(text) <= 12 else text[:12] + "……"

    @staticmethod
    def _norm_path(line: Line, f: str) -> str:
        return os.path.normcase(os.path.abspath(os.path.join(line.cwd, f)))

    @staticmethod
    def _names(files: list[str]) -> str:
        """One or two files by name, more as a count."""
        return "、".join(Path(f).name for f in files) if 0 < len(files) <= 2 else (f"{len(files)} 个文件" if files else "")

    def _files_question(self, changed: list[str], outside: list[str], deleted: list[str] = (), edited: list[str] = ()) -> str:
        """Short enough for the card: what changes, and what the restore also does that the user may not expect."""
        also = []
        if deleted:
            also.append(f"{self._names(list(deleted))}是这几轮新建的，会被删掉")
        if edited:
            also.append(f"{self._names(list(edited))}之后又被改过，那些改动也会没了")
        if outside:
            also.append(f"其中 {len(outside)} 个不只是这几轮改的，会被改回那时的样子，那时还没有的会被删掉")
        if also:
            return f"要把 {self._names(changed)}改回去吗？" + "；".join(also) + "。"
        return f"这几轮改过 {self._names(changed)}，要一起改回去吗？"

    def interrupt(self, line_id: str) -> dict:
        line = self.lines.get(line_id)
        if line is None:
            return {"ok": False, "message": "那条线不在了"}
        line.interrupt()
        self._cancel_pending(line_id)
        return {"ok": True, "message": ""}

    def stop_all(self) -> dict:
        """The emergency brake: every line's turn and tool; the Claude process is killed if it does not stop.
        A line waiting for an approval is mid-turn too: its request is answered no and its turn is stopped."""
        def running(l: Line) -> bool:
            return l.state == WORKING or (l.state == WAITING and bool(getattr(l.session, "turn_active", False)))
        with self.lock:
            self.brake_gen += 1
        busy = [l for l in self.lines.values() if running(l)]
        for l in busy:
            l.interrupt()
        self._cancel_pending(None)
        deadline = time.time() + 0.5
        while time.time() < deadline and any(running(l) for l in busy):
            time.sleep(0.05)
        killed = []
        for l in busy:
            if running(l):
                l.kill_and_resume()
                killed.append(l.name)
            self._emit_line(l)
        self.emit("notice", {"text": "已紧急停止" + ("，" + "、".join(killed) + "被强制重启" if killed else "")})
        if self.jobs.running():
            # By design the brake does not kill detached jobs by itself; it asks (no answer = keep them).
            threading.Thread(target=self._ask_stop_jobs, args=(False,), daemon=True, name="ask-stop-jobs").start()
        return {"ok": True, "message": "", "killed": killed}

    def _ask_stop_jobs(self, kill_on_timeout: bool) -> list[Job]:
        """Ask the user whether the running background jobs should be stopped too; stop them on 可以."""
        running = self.jobs.running()
        if not running:
            return []
        what = "、".join((j.note or j.command[:30]) for j in running[:3])
        q = f"还有 {len(running)} 个后台任务在跑（{what}），要一起停吗？"
        answer = self.ask_frontend(self.active_id or MANAGER, "stop_jobs", q, service=True)
        if answer is True or (answer is False and kill_on_timeout and self._last_ask_timed_out):
            stopped = self.jobs.cancel_all()
            if stopped:
                self.emit("notice", {"text": f"已停掉 {len(stopped)} 个后台任务。"})
            return stopped
        return []

    def request_shutdown(self, jobs: str = "ask", lines_confirmed: bool = False) -> dict:
        """'关掉后台服务' or the window's quit button: close every line and exit. jobs: "ask" = running jobs are
        asked about out loud first (no answer = stop them, nobody will be around to look after an endless
        command); "stop" = stop them all, then exit (the window already asked); "keep" = leave them running and
        exit (they are detached processes; see docs on when that holds). lines_confirmed: the window already asked
        about lines that are mid-turn, so that question is not asked again (the one about jobs still is)."""
        if jobs not in ("ask", "stop", "keep"):
            return {"ok": False, "spoken": f"jobs 只能是 ask、stop 或 keep，不是 {jobs}", "shutdown": False}
        running = self.jobs.running()
        if jobs == "stop" and running:
            stopped = self.jobs.cancel_all()
            self.emit("notice", {"text": f"已停掉 {len(stopped)} 个后台任务。"})
            return {"ok": True, "spoken": f"好，停掉 {len(stopped)} 个后台任务，关掉后台服务。", "shutdown": True}
        working = [] if lines_confirmed else [l for l in self.lines.values() if l.state == WORKING
                                              or (l.state == WAITING and getattr(l.session, "turn_active", False))]
        if jobs == "ask" and (running or working):
            def go():
                if working:
                    who = "、".join(l.name for l in working[:2]) + (f"等 {len(working)} 条线" if len(working) > 2 else "")
                    if self.ask_frontend(self.active_id or MANAGER, "shutdown", f"{who}还在做事，要关掉后台服务吗？",
                                         service=True) is not True:
                        self.emit("notice", {"text": "没有关掉后台服务。"})  # no answer: nothing is cut off mid-turn
                        return
                if self.jobs.running():
                    self._ask_stop_jobs(True)
                if self.shutdown_hook:
                    self.shutdown_hook()
            threading.Thread(target=go, daemon=True, name="shutdown").start()
            return {"ok": True, "spoken": "", "shutdown": False}  # the question itself arrives as an 'ask'
        if running:
            return {"ok": True, "spoken": f"好，关掉后台服务，{len(running)} 个后台任务留着跑。", "shutdown": True}
        return {"ok": True, "spoken": "好，关掉后台服务。", "shutdown": True}

    def phrase(self, key: str) -> dict:
        if key == "rewind_last":
            r = self.rewind(self.active_id or MANAGER, steps=1)
            return {"ok": r["ok"], "spoken": r["message"], "text": r.get("text", ""), "files_restored": r.get("files_restored", 0)}
        if key == "back_to_manager":
            self.switch(MANAGER, announce="好，回到总机。")
            return {"ok": True, "spoken": ""}
        if key == "scratch":
            self._ensure_scratch()
            self.switch(SCRATCH, announce="接到杂项")
            return {"ok": True, "spoken": ""}
        if key == "background_mode":
            line = self.active()
            if line is None or line.kind in (MANAGER,) or line.state != WORKING:
                return {"ok": True, "spoken": "现在没有在干活的线路。"}
            self.switch(MANAGER)
            return {"ok": True, "spoken": f"好，{line.name}在后台继续。"}
        if key == "background_status":
            return {"ok": True, "spoken": self.background_status_text()}
        if key == "shutdown_daemon":
            return self.request_shutdown()
        if key == "transparent_on":
            if self.cfg.transparent:
                return {"ok": True, "spoken": "通透模式已经开着。"}
            threading.Thread(target=self._ask_transparent, daemon=True, name="transparent").start()
            return {"ok": True, "spoken": ""}  # the confirmation itself arrives as an 'ask'
        if key == "transparent_off":
            self._set_transparent(False)
            return {"ok": True, "spoken": "通透模式已关闭。"}
        return {"ok": False, "spoken": "", "message": f"不认识的短语 {key}"}

    # ---------- 通透模式: every "ask" goes through except deleting and overwriting (risk.py decides, not the model) ----------

    def _ask_transparent(self) -> bool:
        """Turning it on is always confirmed out loud, with the exact wording, whatever the table says."""
        if self.ask_frontend(self.active_id or MANAGER, "transparent", TRANSPARENT_QUESTION, service=True) is True:
            self._set_transparent(True)
            self.emit("notice", {"text": "通透模式已开启：删除和覆盖已有文件仍会问，其他直接做。"})
            return True
        self.emit("notice", {"text": "没有开启通透模式。"})
        return False

    def _set_transparent(self, on: bool) -> None:
        self.cfg.transparent = bool(on)
        config_save("permissions", "transparent", bool(on))
        log.info("transparent mode %s", "on" if on else "off")
        self.emit("transparent", {"on": bool(on)})

    def _set_transparent_from_line(self, on: bool) -> dict:
        if on:
            if self.cfg.transparent:
                return {"ok": True, "message": "通透模式已经开着。"}
            ok = self._ask_transparent()
            return {"ok": ok, "message": "用户同意了，通透模式已开启。" if ok else "用户没有同意，通透模式没开。不要再问。"}
        if not self.cfg.transparent:
            return {"ok": True, "message": "通透模式本来就是关的。"}
        self._set_transparent(False)
        return {"ok": True, "message": "通透模式已关闭。"}

    def background_status_text(self) -> str:
        parts = []
        for l in self.lines.values():
            if l.kind == MANAGER or l.state in (DEAD, CLOSED):
                continue
            if (l.id == self.active_id or l.state == IDLE) and not l.unread():
                continue  # nothing to tell about a quiet line
            s = l.status_text()
            if l.unread():
                s += "，最后说：" + l.unread()[-1]["text"]
            parts.append(s)
        for j in self.jobs.list()[:5]:
            if j.state == "running" or (j.ended and time.time() - j.ended < 3600):
                parts.append(j.spoken)
        return "；".join(parts) + "。" if parts else "后台没有东西。"

    # ---------- the permission gate ----------

    def gate(self, line_id: str, action: str, question: str, detail: str = "", facts: dict | None = None) -> tuple[bool, str]:
        policy = self.cfg.permissions.get(action, "ask") if action else "ask"
        label = PERMISSION_LABELS.get(action, action)
        if action in DESKTOP_ACTIONS and line_id != self.active_id:
            return False, "这条线不在前台，不能动桌面；等用户接回来再做。"
        if policy == "deny":
            log.info("gate %s/%s: denied by the permissions table", line_id, action)
            return False, f"用户把“{label}”设成了禁止，没有执行。不要再试，口头说一句就好。"
        if policy == "allow":
            log.info("gate %s/%s: allowed by the permissions table", line_id, action)
            return True, ""
        if getattr(self.cfg, "transparent", False):
            kind, why = risk.classify(action, question, detail, facts)
            if kind is None:
                log.info("gate %s/%s: transparent mode, passed", line_id, action)
                return True, TRANSPARENT_PASSED
            log.info("gate %s/%s: transparent mode, still asking (%s)", line_id, action, kind)
            question = f"{why}：{question}"
        ok = self.ask_frontend(line_id, action, question, detail)
        if ok is True:
            return True, ""
        return False, "用户没有同意，没有执行。不要再试同一个动作，口头说一句就好。"

    def gate_batch(self, line_id: str, question: str, actions: list[str], detail: str,
                   facts: list[dict] | None = None) -> dict:
        table = self.cfg.permissions
        if any(a in DESKTOP_ACTIONS for a in actions) and line_id != self.active_id:
            return {"ok": False, "message": "这条线不在前台，不能动桌面；等用户接回来再做。"}
        denied = [PERMISSION_LABELS.get(a, a) for a in actions if table.get(a, "ask") == "deny"]
        if denied:
            return {"ok": False, "message": f"这串步骤里有被用户禁止的动作（{'、'.join(denied)}），没有执行。"}
        if actions and all(table.get(a, "ask") == "allow" for a in actions):
            return {"ok": True, "message": "这些动作用户都设成了不用问，已放行。"}
        if getattr(self.cfg, "transparent", False):
            risky = risk.classify_steps(actions, facts, detail)
            if not risky:
                log.info("gate %s/batch: transparent mode, passed", line_id)
                return {"ok": True, "message": TRANSPARENT_PASSED}
            question = f"{risk.batch_reason(risky)}：{question}"  # the reason once; the steps are in the detail
        elif table.get("batch", "ask") != "allow":
            return {"ok": False, "per_step": True, "message": "用户要求一步一步问。"}
        ok = self.ask_frontend(line_id, "batch", question, detail) is True
        return {"ok": ok, "message": "用户同意了。" if ok else "用户没有同意，没有执行。不要再试同一串动作，口头说一句就好。"}

    def ask_frontend(self, line_id: str, action: str, question: str, detail: str = "", kind: str = "approve",
                     service: bool = False) -> bool | str:
        """Have the front end ask the user out loud. One question at a time. True/False for approvals; the user's
        words for kind='question'; False/'' after the timeout (also when no front end is connected). service: a
        question of the service's own (shutdown, 通透模式, stopping jobs), shown on `line_id`'s card but leaving that
        line's state and mailbox alone."""
        line = None if service else self.lines.get(line_id)
        gen = self.brake_gen
        with self.ask_lock:
            if self.stopping or self.brake_gen != gen:  # the brake was pulled while this request waited its turn
                return False if kind == "approve" else ""
            request = uuid.uuid4().hex[:8]
            slot = {"event": threading.Event(), "answer": None, "text": "", "line": line_id}
            with self.lock:
                self.pending[request] = slot
                was_working = bool(line) and line.state == WORKING
                if was_working:
                    line.state = WAITING
            shown = self.lines.get(line_id)
            self.emit("ask", {"request": request, "line": line_id, "line_name": shown.name if shown else "",
                              "action": action, "question": question, "detail": detail, "kind": kind,
                              "background": line_id != self.active_id})
            slot["event"].wait(self.ask_timeout)
            with self.lock:
                self.pending.pop(request, None)
                if was_working and line.state == WAITING and getattr(line.session, "turn_active", True):
                    line.state = WORKING  # only what this question changed, and only while its turn still runs
            answer, text = slot["answer"], slot["text"]
            self._last_ask_timed_out = answer is None
        if line:
            self._emit_line(line)
        if answer is None:
            log.info("ask %s/%s: no answer within %.0f s", line_id, action, self.ask_timeout)
            if line and line.kind != MANAGER:
                line.post("needs_you", f"申请没人回答：{question}")
            return False if kind == "approve" else ""
        log.info("ask %s/%s -> %s", line_id, action, answer if kind == "approve" else text[:60])
        return answer if kind == "approve" else text

    def answer(self, request: str, yes: bool, text: str = "") -> dict:
        with self.lock:
            slot = self.pending.get(request)
            if slot is None:
                return {"ok": False, "message": "这个申请已经过期了"}
            slot["answer"], slot["text"] = bool(yes), text
            slot["event"].set()
        return {"ok": True, "message": ""}

    def _cancel_pending(self, line_id: str | None) -> None:
        with self.lock:
            for slot in self.pending.values():
                if line_id is None or slot["line"] == line_id:
                    slot["answer"] = False
                    slot["event"].set()

    # ---------- control requests from the tool servers ----------

    def control_request(self, line_id: str, path: str, data: dict) -> dict:
        line_id = line_id or self.active_id
        if path in FRONTEND_PATHS:
            return self.forward(path, data)
        if path == "ask":
            q = str(data.get("question", "")).strip()
            if not q:
                return {"ok": False, "message": "没有给出要申请的内容"}
            action = str(data.get("action", "")).strip()
            detail = str(data.get("detail", ""))
            facts = data.get("facts")
            gen = self.brake_gen  # with every reply, so a tool doing several steps can see a brake pulled since
            if action == "batch":
                return {**self.gate_batch(line_id, q, [str(a) for a in data.get("actions", [])], detail,
                                          facts if isinstance(facts, list) else None), "brake": gen}
            ok, why = self.gate(line_id, action, q, detail, facts if isinstance(facts, dict) else None)
            return {"ok": ok, "message": why or "用户同意了。", "brake": gen}
        if path == "brake":
            return {"ok": True, "message": "", "brake": self.brake_gen}
        if path == "frontend_pid":
            return {"ok": True, "message": "", "pid": self.frontend_pid}
        if path == "set_transparent":
            return self._set_transparent_from_line(bool(data.get("on")))
        if path == "switch_model":
            return self._switch_model(line_id, str(data.get("model", "")))
        if path == "get_permissions":
            return {"ok": True, "message": "；".join(f"{PERMISSION_LABELS[k]}：{PERMISSION_VALUES[v]}"
                                                    for k, v in self.cfg.permissions.items() if k in PERMISSION_LABELS)}
        if path == "set_permission":
            return self._set_permission(line_id, str(data.get("name", "")).strip().lower(), data.get("value", ""))
        if path == "run_in_background":
            return self._run_in_background(line_id, str(data.get("command", "")).strip(), str(data.get("note", "")).strip())
        if path == "job_status":
            return self._job_status(str(data.get("id", "")).strip())
        if path == "job_cancel":
            return self._job_cancel(line_id, str(data.get("id", "")).strip())
        if path == "list_projects":
            self.refresh_index()
            return {"ok": True, "message": projects.index_text(self.index)}
        if path == "switch_to":
            return self.switch_to_project(str(data.get("project", "")), confirm=True)
        if path == "new_project":
            return self.new_project(str(data.get("name", "")), str(data.get("dir", "")),
                                    [str(x) for x in data.get("read_from", [])])
        if path == "background_status":
            return {"ok": True, "message": self.background_status_text()}
        if path == "remember_alias":
            return self.remember_alias(str(data.get("alias", "")), str(data.get("project", "")))
        if path == "switch_provider":
            return self._switch_provider_spoken(line_id, str(data.get("provider", "")))
        return {"ok": False, "message": f"不认识的操作 {path}"}

    def _switch_model(self, line_id: str, text: str) -> dict:
        line = self.lines.get(line_id)
        if line is None or line.session is None:
            return {"ok": False, "message": "那条线不在了"}
        if line.provider != "claude":
            return self._switch_model_on_provider(line, text)
        key = normalize_model(text)
        if key is None:
            return {"ok": False, "message": f"模型只能是 {'、'.join(MODELS)}（fable 也可以说“飞五”）"}
        model = accessmod.provider_for("claude", self.cfg).models.get(key, MODELS[key])
        note = MODEL_NOTES.get(key, "")
        if line.kind == MANAGER:
            # Asked on the manager: this sets the model for the project lines (and the default for new ones).
            # The manager itself always stays on its own small, fast model.
            targets = [l for l in self.lines.values() if l.kind != MANAGER and l.state not in (DEAD, CLOSED)]
            if normalize_model(self.cfg.model or "") == key and all(
                    normalize_model(getattr(t.session, "model", "") or "") == key for t in targets):
                return {"ok": True, "message": f"项目线路现在用的就是 {key}（总机自己一直是 {normalize_model(getattr(line.session, 'model', '')) or '自己的模型'}）"}
            ok, why = self.gate(line_id, "switch_model", f"要把项目线路的模型切换成 {key}。{note}可以吗？")
            if not ok:
                return {"ok": False, "message": why}
            self.cfg.model = model
            config_save("claude", "model", model)
            for t in targets:
                t.pending_model = model
            return {"ok": True, "message": f"用户同意了。项目线路会切换到 {key}（正在干活的线路这轮结束后切换，新接的项目直接用它）；总机自己不换。"}
        current = normalize_model(getattr(line.session, "model", self.cfg.model) or "")
        if key == current:
            return {"ok": True, "message": f"现在用的就是 {key}"}
        ok, why = self.gate(line_id, "switch_model", f"要把模型切换成 {key}。{note}可以吗？")
        if not ok:
            return {"ok": False, "message": why}
        line.pending_model = model  # applied once the turn is over
        self.cfg.model = model
        config_save("claude", "model", model)
        return {"ok": True, "message": f"用户同意了。这一轮回答结束后会切换到 {key}，对话内容会保留。"}

    def _switch_model_on_provider(self, line: Line, text: str) -> dict:
        """On a DeepSeek / GPT line the spoken names are that provider's ([providers.<id>] models)."""
        p = accessmod.provider_for(line.provider, self.cfg)
        t = str(text).strip().lower().replace(" ", "")
        model = p.models.get(t) or (t if t in p.models.values() else None)
        # A Claude name said here means "the big / middle / small one" of this vendor (Claude Code's own slot
        # mapping), so the user is asked once with both names instead of the model retrying with another name.
        claude_slot = normalize_model(t)
        if model is None and claude_slot:
            model = {"opus": p.deep_model or p.model, "fable": p.deep_model or p.model, "sonnet": p.model}.get(claude_slot, p.model)
            t = f"{claude_slot}（在{p.name}上就是 {model}）"
        names = "、".join(p.models) or p.model
        if model is None:
            return {"ok": False, "message": f"这条线在{p.name}上，模型只能是 {names}；把可选的名字告诉用户，不要自己换个名字再调一次。"}
        if getattr(line.session, "model", "") == model:
            return {"ok": True, "message": f"现在用的就是 {t}"}
        ok, why = self.gate(line.id, "switch_model", f"要把这条线的模型切换成 {t}。可以吗？")
        if not ok:
            return {"ok": False, "message": why}
        line.pending_model = model  # applied once the turn is over; Claude's default model is untouched
        return {"ok": True, "message": f"用户同意了。这一轮回答结束后会切换到 {t}，对话内容会保留。"}

    def _set_permission(self, line_id: str, name: str, value_text: Any) -> dict:
        value = normalize_permission_value(value_text)
        if name not in PERMISSION_LABELS:
            return {"ok": False, "message": f"没有叫 {name} 的权限项。可选：{', '.join(PERMISSION_LABELS)}"}
        if value is None:
            return {"ok": False, "message": "值只能是 ask（每次问）、allow（不用问）或 deny（禁止）"}
        label, word = PERMISSION_LABELS[name], PERMISSION_VALUES[value]
        if self.cfg.permissions.get(name) == value:
            return {"ok": True, "message": f"“{label}”现在就是{word}"}
        # Changing a permission is always confirmed out loud, whatever the table says.
        if self.ask_frontend(line_id, "set_permission", f"要把“{label}”的权限改成{word}，可以吗？") is not True:
            return {"ok": False, "message": f"用户没有同意把“{label}”改成{word}"}
        self.cfg.permissions[name] = value
        config_save("permissions", name, value)
        msg = f"用户同意了，“{label}”已改成{word}。"
        if name == "other_connectors":
            for l in self.lines.values():
                l.pending_restart = True
            msg += "各条线这一轮结束后会重新连接 Claude，对话会保留。"
        return {"ok": True, "message": msg}

    def _run_in_background(self, line_id: str, command: str, note: str) -> dict:
        if not command:
            return {"ok": False, "message": "没有给出命令"}
        line = self.lines.get(line_id)
        cwd = line.cwd if line else os.path.expanduser(self.cfg.working_directory)
        warn = "这个命令不会自己结束，要停得说停。" if looks_endless(command) else ""
        ok, why = self.gate(line_id, "background_job", f"我想在后台运行：{command[:80]}，输出记到日志。{warn}可以吗？", command)
        if not ok:
            return {"ok": False, "message": why}
        job = self.jobs.start(command, cwd, line_id, note)
        return {"ok": True, "message": f"后台任务 {job.id} 已启动（进程 {job.pid}），日志在 {job.log}。用 job_status 看进度；它独立于你运行，你退出也不影响。",
                "job": job.id}

    def _job_status(self, jid: str) -> dict:
        if not jid:
            jobs = self.jobs.list()[:5]
            return {"ok": True, "message": "；".join(j.spoken for j in jobs) if jobs else "没有后台任务。"}
        job = self.jobs.get(jid)
        if job is None:
            return {"ok": False, "message": f"没有编号 {jid} 的后台任务"}
        tail = self.jobs.tail(jid, 8)
        return {"ok": True, "message": job.spoken + (f"。日志最后几行：\n{tail}" if tail else "")}

    def _job_cancel(self, line_id: str, jid: str) -> dict:
        job = self.jobs.get(jid)
        if job is None or job.state != "running":
            return {"ok": False, "message": f"没有正在跑的后台任务 {jid}"}
        ok, why = self.gate(line_id, "background_job", f"要停掉后台任务 {jid}（{job.note or job.command[:40]}），可以吗？")
        if not ok:
            return {"ok": False, "message": why}
        self.jobs.cancel(jid)
        return {"ok": True, "message": f"后台任务 {jid} 已停掉。"}

    def new_project(self, name: str, directory: str, read_from: list[str]) -> dict:
        name = name.strip()
        if not name:
            return {"ok": False, "message": "新项目要有名字"}
        directory = os.path.expanduser(directory) if directory else str(Path.home() / "Documents" / name)
        refs, missing = [], []
        for text in read_from:
            cands = projects.resolve(text, self.index)
            if len(cands) == 1:
                refs.append(cands[0])
            else:
                missing.append(text)
        if missing:
            return {"ok": False, "message": f"这些参考项目没找到或不唯一：{'、'.join(missing)}"}
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        copied = []
        for p in refs:
            mem = self._memory_dir(p.cwd)
            if mem and mem.is_dir():
                dest = d / "参考" / p.name / "memory"
                shutil.copytree(mem, dest, dirs_exist_ok=True)
                copied.append(p.display_name)
        notes = d / "参考" / "说明.md"
        if refs:
            notes.parent.mkdir(parents=True, exist_ok=True)
            notes.write_text("这里是其他项目记忆的只读副本（快照）：" + "、".join(copied) + "。不要改它们，它们不会同步回去。\n", "utf-8")
        projects.remember_alias(paths.PROJECTS_FILE, name, str(d))
        self.refresh_index()
        return self.switch_to_project(name)

    @staticmethod
    def _memory_dir(cwd: str) -> Path | None:
        slug = "".join(ch if ch.isalnum() else "-" for ch in str(Path(cwd)))
        d = paths.CLAUDE_PROJECTS_DIR / slug / "memory"
        return d if d.is_dir() else None

    def remember_alias(self, alias: str, project_text: str) -> dict:
        alias = alias.strip()
        if not alias:
            return {"ok": False, "message": "别名是空的"}
        cands = projects.resolve(project_text, self.index) if project_text else ([self.active()] if self.active() and self.active().kind == "project" else [])
        if len(cands) != 1:
            return {"ok": False, "message": "不确定你说的是哪个项目"}
        p = cands[0]
        cwd = p.cwd if isinstance(p, Project) else p.cwd
        projects.remember_alias(paths.PROJECTS_FILE, alias, cwd)
        self.refresh_index()
        return {"ok": True, "message": f"记住了：{alias} 指 {Path(cwd).name}"}

    # ---------- events ----------

    def on_line_event(self, line_id: str, kind: str, data: dict) -> None:
        line = self.lines.get(line_id)
        if line is None:
            return
        if kind == "ready":
            started = self.line_started.pop(line_id, None)
            if started and data.get("first"):
                # Claude Code sends system/init only after accepting the first message, so this is NOT the
                # process start-up time; it is "launch -> first message accepted" (includes the user's own delay).
                log.info("[%s] first init %.2f s after launch (first message accepted)", line_id, time.time() - started)
            self._save_lines()
            return
        if kind in ("text", "tool"):
            started = self.turn_started.get(line_id)
            if kind == "text" and started is not None:
                log.info("[%s] first text %.2f s after say", line_id, time.time() - started)
                self.turn_started[line_id] = None  # log only the first delta of the turn
            self.emit(kind, {"line": line_id, **data})
            return
        if kind == "done":
            if line.kind == MANAGER:
                self._manager_log("总机", line.reply)
            self.emit("done", {"line": line_id, "status": data.get("status"), "last": data.get("last", "")})
            if line_id == self.active_id:
                line.mark_read()  # the user was listening
            elif data.get("kind") in ("needs_you", "error"):
                self.emit("notice", {"text": f"{line.name}{'出错了' if data.get('kind') == 'error' else '问你'}：{data.get('last', '')}"})
            first = getattr(line, "first_turn_after_switch", False)
            line.first_turn_after_switch = False
            if first and data.get("kind") == "error" and self._fresh_after_switch(line):
                return
            self._apply_pending(line)
            self._emit_line(line)
            return
        if kind == "rewind_failed":
            back, restored = self.rewound_backup.pop(line_id, ([], 0))
            line.turn_log.extend(back)
            if restored and back:
                line.pending_note = f"（说明：{restored} 个文件已改回到「{self._short(back[0]['text'])}」之前的样子，但对话没有撤回。）"
                self.emit("notice", {"text": f"{line.name}撤回没成功：文件已改回去，但对话没撤回。"})
            else:
                self.emit("notice", {"text": f"{line.name}撤回没成功（那几轮里有没看到的内容），对话保持原样。"})
            return
        if kind == "dead":
            if self.stopping:
                return
            if getattr(line, "first_turn_after_switch", False) and self._fresh_after_switch(line):
                return
            if self._fresh_after_failed_resume(line, bool(data.get("busy"))):
                return
            if data.get("busy"):
                # the turn the window is waiting on will never finish: end it, or it stays on 思考中
                self.emit("done", {"line": line_id, "status": "error", "last": "程序退出了"})
            self.emit("notice", {"text": f"{line.name}的程序退出了，下次说话时会重新连接。"})
            self._emit_line(line)

    def _apply_pending(self, line: Line) -> None:
        model = getattr(line, "pending_model", None)
        provider = getattr(line, "pending_provider", None)
        restart = getattr(line, "pending_restart", False)
        if not (model or provider or restart) or line.state not in (IDLE, WAITING) or line.session is None \
                or getattr(line.session, "turn_active", False):
            return  # WAITING also covers "mid-turn, a spoken approval is open": never restart under the user
        line.pending_model, line.pending_provider, line.pending_restart = None, None, False
        try:
            if provider:
                st = self.provider_status(provider)
                if not self._own_route(st):
                    ok, why = self.reach(provider, self.cfg)
                    if not ok:
                        raise RuntimeError(why)
                    self._ensure_gateway(provider)
                if provider == "claude" and line.kind == MANAGER and not model:
                    model = str(getattr(self.cfg, "manager_model", "sonnet") or "sonnet")  # its own small model, not cfg.model
                engine = "codex" if self._own_route(st) else "claude"
                if engine != line.engine:
                    # Claude Code and Codex cannot continue each other's conversations: a new session on the other
                    # engine, made the way every line's session is made (the line's own factory).
                    line.session.close()
                    line.provider = provider
                    line.start("")
                else:
                    line.session.restart(model, provider=provider)
                    line.provider = provider
                line.first_turn_after_switch = True
                self.emit("notice", {"text": f"{line.name}已换到{accessmod.provider_for(provider, self.cfg).name}"})
                self._emit_line(line)
                return
            line.session.restart(model)
            spoken = normalize_model(model or "") or model
            self.emit("notice", {"text": f"{line.name}已切换到 {spoken}" if model else f"{line.name}已重新连接"})
        except Exception as e:
            log.exception("[%s] restart failed", line.id)
            self.emit("notice", {"text": f"{line.name}重连失败：{e}"})

    def _fresh_after_switch(self, line: Line) -> bool:
        """The first turn after a provider switch may fail because the old conversation does not resume on the
        new side (Claude's signed thinking blocks, for one). Then start it afresh once and say so."""
        if line.session is None:
            return False
        line.first_turn_after_switch = False
        name = accessmod.provider_for(line.provider, self.cfg).name
        try:
            line.session.restart(fresh=True)
            line.state = IDLE
            line.turn_log.clear()
            line.log_from_start, line.base_point = True, None
            line.pending_note = ""
        except Exception:
            log.exception("[%s] fresh restart failed", line.id)
            return False
        self.emit("notice", {"text": f"{line.name}换到{name}后接不上上次的对话，已重新开始，请再说一遍。"})
        self._emit_line(line)
        return True

    def _fresh_after_failed_resume(self, line: Line, busy: bool) -> bool:
        """The process was started to continue a saved conversation and went away before accepting a message ("No
        conversation found"): the id is no good here. Forget it, start a new conversation, and hand the waiting words
        to it."""
        s = line.session
        if s is None or not line.started_resume or line.ready_seen or line.engine == "codex":
            return False  # Codex falls back from a failed thread/resume itself: an exit is not that, keep the thread
        if not getattr(s, "resume_failed", False) and line.now() - line.started_at > 10:
            return False  # it ran for a while: some other failure, keep the conversation for the next try
        log.info("[%s] could not continue conversation %s%s; starting a new one", line.id, line.started_resume[:8],
                 " (no such conversation)" if getattr(s, "resume_failed", False) else "")
        self._forget_session(line)
        line.started_resume, line.session_id = "", ""
        waiting = line.turn_log[-1]["turn"] if busy and line.turn_log else None
        in_flight = busy and line.sending  # a Codex send still waiting for its thread: it goes to the fresh one itself
        kept = [line.turn_log[-1]] if in_flight and line.turn_log else []
        try:
            s.restart(fresh=True)
            line.state = WORKING if in_flight else IDLE
            line.turn_log[:] = kept
            line.log_from_start, line.base_point = True, None
            line.pending_note = ""
        except Exception:
            log.exception("[%s] fresh start failed", line.id)
            return False
        self.emit("notice", {"text": f"{line.name}接不上上次的对话，对话重新开始。"})
        if busy and line.last_said and not in_flight:
            line.say(line.last_said, waiting)  # the words the old process never answered, under the same id
            self.turn_started[line.id] = time.time()
        self._emit_line(line)
        return True

    def _reconnect(self, line: Line) -> str:
        """Start a dead line's process again, continuing its own conversation; '' on success, else why not."""
        try:
            line.started_resume, line.ready_seen, line.started_at = line.session_id, False, line.now()
            line.session.restart()
            line.state = IDLE
        except Exception as e:
            log.exception("[%s] reconnect failed", line.id)
            return f"{line.name}重新连接不上：{e}"
        self.emit("notice", {"text": f"{line.name}已重新连接"})
        return ""

    def _forgotten(self, key: str) -> bool:
        return key in self.forgotten

    def _forget_session(self, line: Line) -> None:
        """Drop a project's saved conversation id that turned out not to resume."""
        if line.kind != "project":
            return
        with self.lines_lock:
            self.forgotten.add(projects.norm_key(line.cwd))
            data = projects.load_lines(paths.LINES_FILE)
            if data.pop(projects.norm_key(line.cwd), None) is not None:
                self._write_lines(data)
        for p in self.index:
            if p.key == projects.norm_key(line.cwd):
                p.session_id, p.session_engine = "", ""

    def on_job_change(self, job: Job) -> None:
        self.emit("job", {"job": job.id, "state": job.state, "note": job.note, "tail": self.jobs.tail(job.id, 3)})
        line = self.lines.get(job.line)
        if job.state == "failed":
            if line:
                line.post("error", job.spoken)
            self.emit("notice", {"text": job.spoken})
        elif job.state == "done":
            if line:
                line.post("job", job.spoken)
            if job.line == self.active_id:
                self.emit("notice", {"text": job.spoken})
                if line:
                    line.mark_read()  # announced to the user just now
        if line:
            self._emit_line(line)

    # ---------- housekeeping ----------

    def tick(self) -> None:
        with self.lock:
            for l in list(self.lines.values()):
                if l.kind != "project" or l.state in (DEAD, CLOSED) or l.id == self.active_id or l.keep_alive:
                    continue
                if self.now() - l.last_used > self.idle_seconds:
                    self._close_line(l, "闲置太久")
            pending = [l for l in self.lines.values() if l.pending_model or l.pending_provider or l.pending_restart]
        for l in pending:  # outside the lock: a switch may start the gateway, which takes seconds
            self._apply_pending(l)
        self._save_lines()

    def _write_lines(self, data: dict) -> None:
        """lines.json written whole or not at all (a temp file in the same folder, then replaced): a reader never sees
        half a file, and two writers never leave one's tail after the other's end."""
        try:
            paths.LINES_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = paths.LINES_FILE.with_name(f"{paths.LINES_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
            for attempt in range(5):
                try:
                    os.replace(tmp, paths.LINES_FILE)
                    return
                except PermissionError:  # another program has it open for a moment (an editor, a virus scanner)
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except OSError:
            log.exception("could not save lines.json")

    def _read_line_record(self, key: str):
        with self.lines_lock:
            return projects.load_lines(paths.LINES_FILE).get(key)

    def _save_lines(self) -> None:
        with self.lines_lock:
            self._save_lines_locked()

    def _save_lines_locked(self) -> None:
        data = projects.load_lines(paths.LINES_FILE)
        for l in self.lines.values():
            if l.kind == "project" and l.session_id:
                rec = {"session_id": l.session_id, "engine": l.engine, "provider": l.provider, "name": l.name,
                       "last_used": time.time()}
                fork = getattr(l.session, "fork_at", None)
                if fork and getattr(l.session, "session_id", "") == l.session_id:
                    rec["fork_at"] = list(fork)  # a rewind not made yet: reopening must not bring the dropped turns back
                if l.pending_note:
                    rec["note"] = l.pending_note
                data[projects.norm_key(l.cwd)] = rec
                self.forgotten.discard(projects.norm_key(l.cwd))
        self._write_lines(data)

    def _manager_log(self, who: str, text: str) -> None:
        try:
            paths.MANAGER_LOG_DIR.mkdir(parents=True, exist_ok=True)
            f = paths.MANAGER_LOG_DIR / (time.strftime("%Y-%m-%d") + ".txt")
            with f.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {who}：{text.strip()}\n")
        except OSError:
            log.exception("manager log failed")

    def status(self) -> dict:
        return {"active": self.active_id, "access": self.access.as_dict(), "transparent": bool(getattr(self.cfg, "transparent", False)),
                "lines": [{"line": l.id, "name": l.name, "kind": l.kind, "state": l.state, "active": l.id == self.active_id,
                           "unread": len(l.unread()), "provider": l.provider, "engine": l.engine,
                           "model": getattr(l.session, "model", "") or ""} for l in self.lines.values()],
                "jobs": [{"job": j.id, "state": j.state, "note": j.note} for j in self.jobs.list()[:10]],
                "unread": sum(len(l.unread()) for l in self.lines.values())}

    def shutdown(self) -> None:
        """Close every line and the translation gateway. Background jobs keep running."""
        self.stopping = True
        self._cancel_pending(None)
        try:
            self.gateway.stop()
        except Exception:
            log.exception("gateway stop failed")
        try:
            from . import engine_codex
            engine_codex.stop_login()
        except Exception:
            log.exception("codex login stop failed")
        with self.lock:
            for l in self.lines.values():
                if l.state not in (DEAD, CLOSED):
                    l.close()
        self._save_lines()
