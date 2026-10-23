"""The translation gateway for OpenAI-format providers (GPT): a LiteLLM proxy the daemon starts on 127.0.0.1. It
speaks the Anthropic Messages API to Claude Code and Chat Completions to the vendor.

LiteLLM (MIT) is used rather than our own Messages -> Chat translation because streaming tool calls, tool results
and count_tokens are already handled there. It is optional: install.bat --with-gpt, or
.venv\\Scripts\\python -m pip install "litellm[proxy]>=1.106.2". Rules it is run under:
- loopback only, a free port and a random master key chosen when the daemon starts;
- a scrubbed environment: the system basics plus only the vendor keys it serves, read from auth.json (the user's
  own *_API_KEY variables never reach it, nor do Anthropic keys);
- bound to the daemon's lifetime with a job object (kill on close): a crash or taskkill of the daemon takes it down;
- a fixed port and master key for the daemon's lifetime, so a restart of the proxy (a key changed, a vendor added)
  is invisible to the lines already pointed at it;
- its config file names no key (os.environ/...), telemetry off, the model price table is not fetched from the
  network (LITELLM_LOCAL_MODEL_COST_MAP), no admin UI, no docs page;
- its output goes to ~/.voice-claude/gateway.log; our own logs only ever mention the port and model names."""
from __future__ import annotations

import importlib.util
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from . import access, paths, testing

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
MIN_VERSION = access.GATEWAY_MIN_VERSION
# Environment the proxy may inherit (compared upper-case). Proxies stay: users in China may need one to reach OpenAI.
KEEP_ENV = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE",
            "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "NUMBER_OF_PROCESSORS", "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "LANG", "HOME"}
# Which vendor key each LiteLLM-routed provider gets, and how LiteLLM names the vendor.
KEY_ENV = {"openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "kimi": "MOONSHOT_API_KEY"}
TARGET = {"openai": "openai/", "deepseek": "deepseek/", "kimi": "moonshot/"}


def installed() -> bool:
    return access.gateway_installed()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def scrubbed_env(secrets_: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in KEEP_ENV}
    env.update({"LITELLM_LOCAL_MODEL_COST_MAP": "True", "LITELLM_TELEMETRY": "False", "DISABLE_ADMIN_UI": "True",
                "NO_DOCS": "True", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    env.update(secrets_)
    return env


def config_yaml(models: list[tuple[str, str, str]]) -> str:
    """LiteLLM's config: (name Claude Code asks for, litellm target, key variable). No key literal ever goes in."""
    lines = ["model_list:"]
    for name, target, key_env in models:
        lines += [f"  - model_name: {name}", "    litellm_params:", f"      model: {target}", f"      api_key: os.environ/{key_env}"]
    lines += ["litellm_settings:", "  drop_params: true", "  telemetry: false",
              "general_settings:", "  master_key: os.environ/LITELLM_MASTER_KEY"]
    return "\n".join(lines) + "\n"


def launcher() -> list[str]:
    """How to start the proxy: the console script installed next to our interpreter, else the module entry point."""
    exe = Path(sys.executable).parent / ("litellm.exe" if os.name == "nt" else "litellm")
    if exe.exists():
        return [str(exe)]
    return [sys.executable, "-c", "import litellm; litellm.run_server()"]


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 127.0.0.1 must never go through a proxy


def _http_ok(url: str) -> bool:
    try:
        with _NO_PROXY.open(url, timeout=2) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _bind_to_us(proc) -> None:
    """Put the proxy in a job object that dies with this process (Windows): no orphan holding a vendor key."""
    handle = getattr(proc, "_handle", None)
    if handle is None or os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return

        class LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("a", "b", "c", "d", "e", "f")]

        class EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", LIMIT), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]
        info = EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(ctypes.c_void_p(job), 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(int(handle)))
        _JOBS.append(job)  # keep the handle alive: closing it is what kills the proxy
    except Exception:
        log.debug("could not bind the gateway to a job object", exc_info=True)


_JOBS: list = []


class Gateway:
    """One proxy for every LiteLLM-routed provider that has a key; restarted when a new provider joins."""

    def __init__(self, cfg, *, spawn: Callable = subprocess.Popen, probe: Callable[[str], bool] = _http_ok,
                 now: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 ready_timeout: float = 45.0):
        self.cfg = cfg
        self.spawn, self.probe, self.now, self.sleep = spawn, probe, now, sleep
        self.ready_timeout = ready_timeout
        self.proc = None
        self.port = free_port()  # chosen once: lines keep their ANTHROPIC_BASE_URL across proxy restarts
        self.token = secrets.token_urlsafe(24)
        self.serving: set[str] = set()
        self.lock = threading.Lock()
        self._log_file = None
        access.GATEWAY = self  # access.provider_for / env_overrides ask us whether and where the gateway runs

    # ---------- what the lines need ----------

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def endpoint(self) -> tuple[str, str] | None:
        """(base_url, token) for ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN, or None when not running."""
        return (f"http://127.0.0.1:{self.port}", self.token) if self.running() else None

    def ensure(self, pid: str) -> tuple[bool, str]:
        """Make sure the proxy runs and serves this provider. (ok, message); starting takes 5-15 s."""
        with self.lock:
            if self.running() and pid in self.serving:
                return True, ""
            if self.spawn is subprocess.Popen and testing.under_test_runner():
                return False, testing.REFUSAL
            problem = access.gateway_problem()
            if problem:
                return False, access.gateway_install_hint(problem)
            prov = access.provider_for(pid, self.cfg) if pid in KEY_ENV else None
            if prov is None or prov.via != "litellm":
                return False, "不走翻译层"
            wanted = [p for p in KEY_ENV if access.provider_for(p, self.cfg).via == "litellm" and access.provider_key(p)]
            if pid not in wanted:
                return False, "还没填 key"
            self._stop_locked()
            models, env_keys = [], {}
            for p in wanted:
                prov = access.provider_for(p, self.cfg)
                env_keys[KEY_ENV[p]] = access.provider_key(p)
                for m in dict.fromkeys([prov.model, prov.deep_model or prov.model, prov.small_model or prov.model]):
                    models.append((m, TARGET[p] + m, KEY_ENV[p]))
            path = paths.GATEWAY_CONFIG
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(config_yaml(models), "utf-8")
            env = scrubbed_env({**env_keys, "LITELLM_MASTER_KEY": self.token})
            args = launcher() + ["--config", str(path), "--host", "127.0.0.1", "--port", str(self.port), "--drop_params"]
            log.info("starting the translation gateway on 127.0.0.1:%d for %s (%s)", self.port, ", ".join(wanted),
                     ", ".join(m[0] for m in models))
            try:
                self._log_file = open(paths.GATEWAY_LOG, "ab")
                self.proc = self.spawn(args, env=env, stdin=subprocess.DEVNULL, stdout=self._log_file,
                                       stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
            except OSError as e:
                log.error("gateway failed to start: %s", e)
                self.proc = None
                return False, f"翻译层起不来：{e}"
            _bind_to_us(self.proc)
            self.serving = set(wanted)
            why = self._wait_ready()
            if why:
                self._stop_locked()
                return False, why
            return True, ""

    def _last_log_line(self) -> str:
        try:
            lines = [l.strip() for l in paths.GATEWAY_LOG.read_text("utf-8", errors="replace").splitlines() if l.strip()]
            return lines[-1][:150] if lines else ""
        except OSError:
            return ""

    def _wait_ready(self) -> str:
        """'' once the proxy answers, else why not (an instant exit says so, with the log's last line)."""
        url = f"http://127.0.0.1:{self.port}/health/liveliness"
        t0 = self.now()
        while self.now() - t0 < self.ready_timeout:
            if self.proc is None or self.proc.poll() is not None:
                code = self.proc.poll() if self.proc is not None else None
                tail = self._last_log_line()
                return f"翻译层刚启动就退出了（退出码 {code}）" + (f"：{tail}" if tail else f"，看 {paths.GATEWAY_LOG}")
            if self.probe(url):
                return ""
            self.sleep(0.5)
        return f"翻译层没起来（{int(self.ready_timeout)} 秒内没有响应），看 {paths.GATEWAY_LOG}"

    # ---------- stopping ----------

    def stop(self) -> None:
        with self.lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        proc, self.proc = self.proc, None
        self.serving = set()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
