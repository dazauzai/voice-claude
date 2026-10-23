"""How voice-claude reaches Claude ("接入"): either the user's own, logged-in Claude Code (subscription) or an
Anthropic API key. Keys live in ~/.voice-claude/auth.json (this user only, never in the repo).

voice-claude never implements a claude.ai login itself (docs/architecture.md).
A subscription is used only by running the user's own Claude Code CLI; an API key is handed to Claude Code
through ANTHROPIC_API_KEY. Users without Claude Code installed get the copy bundled with the claude-agent-sdk
package."""
from __future__ import annotations

import getpass
import json
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import paths, testing

log = logging.getLogger(__name__)

API_KEY_ENV = "ANTHROPIC_API_KEY"
CREATE_NO_WINDOW = 0x08000000
METHODS = {"api_key": "API key", "subscription": "Claude 订阅（自己的 Claude Code）", "none": "还没接入",
           "deepseek_key": "DeepSeek API key", "openai_key": "OpenAI API key"}


@dataclass
class AccessStatus:
    ok: bool
    method: str  # "api_key" | "subscription" | "none"
    message: str  # spoken / shown to the user
    claude: str = ""  # path of the Claude Code program that will be used
    account: str = ""  # e-mail / subscription type for the subscription method; masked key for api_key
    provider: str = "claude"  # who answers: claude / deepseek / kimi / openai
    provider_name: str = ""
    model: str = ""  # the model id lines on this provider use

    def as_dict(self) -> dict:
        return asdict(self)


# ---------- the key file ----------

def load_auth(path: Path | None = None) -> dict:
    path = path or paths.AUTH_FILE
    try:
        d = json.loads(path.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _restrict_to_me(path: Path) -> None:
    """Windows: remove inherited permissions, grant only this user. Best effort (the home dir is private anyway)."""
    try:
        user = getpass.getuser()
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW, timeout=10)
    except Exception:
        log.debug("could not tighten permissions on %s", path, exc_info=True)


def save_auth(data: dict, path: Path | None = None) -> None:
    path = path or paths.AUTH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(path)
    # Every save: the replacement file comes with the folder's inherited permissions. icacls can take seconds, so
    # it runs off the caller's thread (the access page must not hang on "保存"); the folder is the user's own.
    threading.Thread(target=_restrict_to_me, args=(path,), daemon=True, name="auth-acl").start()


def mask(key: str) -> str:
    return f"…{key[-4:]}" if len(key) >= 8 else "…"


def valid_key_shape(key: str) -> bool:
    k = key.strip()
    return k.startswith("sk-ant-") and len(k) > 20 and " " not in k


def api_key() -> str:
    """The configured Anthropic key: auth.json first, then the environment (so {env:...} style use works too)."""
    return provider_key("claude")


# ---------- providers: who answers (Claude, DeepSeek, Kimi, GPT). Claude Code is the engine, except GPT through a ChatGPT plan (Codex). ----------

DEEPSEEK_ANTHROPIC_URL = "https://api.deepseek.com/anthropic"
PROVIDER_ORDER = ("claude", "deepseek", "kimi", "openai")
CLAUDE_MODELS = {"opus": "opus", "sonnet": "sonnet", "fable": "claude-fable-5-1"}  # spoken name -> Claude Code model
GATEWAY_MIN_VERSION = "1.106.2"  # LiteLLM; far above the 1.82.7 / 1.82.8 supply-chain incident of March 2026


@dataclass
class Provider:
    id: str
    name: str
    key_name: str  # field in auth.json
    key_prefix: str  # what a key of theirs starts with
    key_hint: str
    base_url: str = ""  # Anthropic-compatible endpoint; "" = Anthropic itself
    model: str = ""  # what new lines (and the manager) use
    deep_model: str = ""  # Claude Code's opus slot
    small_model: str = ""  # Claude Code's haiku slot (titles, summaries, subagents)
    models: dict[str, str] = field(default_factory=dict)  # spoken name -> model id
    via: str = "direct"  # "direct" | "litellm" (the translation gateway for OpenAI-format providers)
    thinking: bool = True  # False -> MAX_THINKING_TOKENS=0: faster answers, which voice likes
    installed: bool = True  # engine / translation layer present
    install_hint: str = ""
    url: str = ""  # where to get a key
    host: str = ""  # the vendor's API host, for the cheap reachability probe ("" = do not probe)
    methods: tuple = ("key",)  # how this vendor can be reached: "login" (the user's own CLI login) and/or "key"
    login_command: str = ""  # what the access page's "登录…" button runs

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "model": self.model, "models": list(self.models),
                "installed": self.installed, "install_hint": self.install_hint, "key_prefix": self.key_prefix,
                "url": self.url, "subscription": "login" in self.methods, "methods": list(self.methods),
                "login_command": self.login_command,
                "fields": [{"key": "api_key", "label": f"{self.name} API key", "secret": True, "hint": self.key_hint}]}


def _defaults() -> dict[str, Provider]:
    # Model ids per the vendors' own pages, October 2026.
    return {
        "claude": Provider("claude", "Claude（Anthropic）", "anthropic_api_key", "sk-ant-",
                           "sk-ant- 开头；有自己登录的 Claude Code 可以不填",
                           models=dict(CLAUDE_MODELS),
                           url="https://console.anthropic.com/settings/keys", methods=("login", "key"),
                           login_command="claude auth login"),
        "deepseek": Provider("deepseek", "DeepSeek（深度求索）", "deepseek_api_key", "sk-", "platform.deepseek.com 申请，sk- 开头",
                             base_url=DEEPSEEK_ANTHROPIC_URL, model="deepseek-flash", deep_model="deepseek-v4-pro",
                             small_model="deepseek-flash", models={"flash": "deepseek-flash", "pro": "deepseek-v4-pro"},
                             url="https://platform.deepseek.com/api_keys", host="api.deepseek.com"),
        # Per Moonshot's own Claude Code guide (platform.kimi.com/docs/guide/claude-code-kimi, 2026-10): base URL
        # api.moonshot.cn/anthropic, key as ANTHROPIC_AUTH_TOKEN, kimi-k3[1m] in every big slot, kimi-k2.7-code for
        # haiku (its thinking cannot be turned off, so [providers.kimi] thinking = false needs another small model).
        # Overseas accounts: base_url = https://api.moonshot.ai/anthropic.
        "kimi": Provider("kimi", "Kimi（月之暗面）", "kimi_api_key", "sk-", "platform.kimi.com（原 moonshot.cn）申请，sk- 开头",
                         base_url="https://api.moonshot.cn/anthropic", model="kimi-k3[1m]", deep_model="kimi-k3[1m]",
                         small_model="kimi-k2.7-code", models={"k3": "kimi-k3[1m]", "code": "kimi-k2.7-code", "k2.6": "kimi-k2.6"},
                         url="https://platform.kimi.com/console/api-keys", host="api.moonshot.cn"),
        "openai": Provider("openai", "GPT（OpenAI）", "openai_api_key", "sk-", "platform.openai.com 申请，sk- 开头",
                           model="gpt-6.1-sol", deep_model="gpt-6.1-sol", small_model="gpt-6-luna",
                           models={"sol": "gpt-6.1-sol", "luna": "gpt-6-luna", "astra": "gpt-6-astra"}, via="litellm",
                           url="https://platform.openai.com/api-keys", host="api.openai.com", methods=("login", "key"),
                           login_command="codex login"),  # login = the user's own Codex (ChatGPT plan) as the engine
    }


# The running translation gateway (voiceclaude/gateway.py) registers itself here so env_overrides can point
# Claude Code at it.
GATEWAY: Any = None


def venv_python() -> str:
    """This environment's console python.exe: the service runs under pythonw.exe, which returns at once with no output
    when pasted into a terminal."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def pip_install_hint() -> str:
    """The one install command, for THIS environment (a plain `pip install` in a terminal would install into the
    global Python and the page would keep saying 'install it')."""
    return f'"{venv_python()}" -m pip install "litellm[proxy]>={GATEWAY_MIN_VERSION}"'


def gateway_install_hint(problem: str) -> str:
    """A full sentence for the AI 服务商 page: the translation layer GPT's key route needs, and how to get it. The
    installer only adds a missing LiteLLM; an old or incomplete one needs the pip command."""
    if problem == "还没装":
        return f"翻译层{problem}：再运行一次 install.bat --with-gpt，或在命令行运行 {pip_install_hint()}（装好不用重启）。"
    return f"翻译层{problem}：在命令行运行 {pip_install_hint()} 更新（装好不用重启）。"


def _vtuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.+-]", v)[:3])


def gateway_problem() -> str:
    """Why the translation layer cannot run: "" when LiteLLM's proxy is installed and new enough."""
    import importlib
    import importlib.metadata
    import importlib.util
    importlib.invalidate_caches()  # a package installed a minute ago must count without a restart
    if importlib.util.find_spec("litellm") is None:
        return "还没装"
    try:
        v = importlib.metadata.version("litellm")
    except importlib.metadata.PackageNotFoundError:
        v = "0"
    if _vtuple(v) < _vtuple(GATEWAY_MIN_VERSION):
        return f"装的版本太旧（{v}，要 {GATEWAY_MIN_VERSION} 以上）"
    if importlib.util.find_spec("uvicorn") is None or importlib.util.find_spec("fastapi") is None:
        return "装的不是 proxy 版（缺 fastapi / uvicorn）"
    return ""


def gateway_installed() -> bool:
    return not gateway_problem()


def gateway_endpoint() -> tuple[str, str] | None:
    """(base_url, token) of the running translation gateway, or None."""
    return GATEWAY.endpoint() if GATEWAY is not None else None


def known_provider(pid: str) -> bool:
    return pid in _defaults()


PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")


def reachable(host: str, port: int = 443, timeout: float = 3.0) -> tuple[bool, str]:
    """A cheap TLS handshake with the vendor, so "this network blocks it" shows up in three seconds instead of
    after Claude Code's two minutes of retries (some networks reset the handshake to a vendor's host by SNI).
    Skipped when a proxy is configured, since the handshake here would go direct."""
    if not host:
        return True, ""
    if any(os.environ.get(k) for k in PROXY_VARS):
        return True, ""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                tls.getpeercert()
        return True, ""
    except socket.gaierror as e:
        return False, f"解析不了 {host}（{e}）：看看网络和 DNS。"
    except (OSError, ssl.SSLError) as e:  # ConnectionResetError and TimeoutError are OSErrors
        return False, f"连不上 {host}（{type(e).__name__}：TLS 握手被重置或超时，可能是所在网络屏蔽了它）。"


def check_reach(pid: str, cfg=None) -> tuple[bool, str]:
    """reachable() for a provider's host (Claude's own route is not probed: Claude Code handles its own endpoints)."""
    return reachable(provider_for(pid, cfg).host) if known_provider(pid) else (True, "")


def provider_for(pid: str, cfg=None) -> Provider:
    """The built-in description of a provider with the user's [providers.<id>] overrides applied."""
    p = _defaults()[pid]
    over = dict((getattr(cfg, "providers", None) or {}).get(pid, {})) if cfg is not None else {}
    for k in ("model", "deep_model", "small_model", "base_url", "via", "host"):
        if over.get(k):
            setattr(p, k, str(over[k]))
    if over.get("base_url") and not over.get("host"):
        from urllib.parse import urlparse
        p.host = urlparse(str(over["base_url"])).hostname or p.host
    if isinstance(over.get("models"), dict):
        p.models = {str(k): str(v) for k, v in over["models"].items()}
    if "thinking" in over:
        p.thinking = bool(over["thinking"])
    if p.via == "litellm":
        problem = gateway_problem()
        if problem:
            p.installed, p.install_hint = False, gateway_install_hint(problem)
    return p


def provider_key(pid: str, path: Path | None = None) -> str:
    """Only auth.json — and, for Claude alone, the ANTHROPIC_API_KEY variable, kept for older setups. The DEEPSEEK_API_KEY /
    OPENAI_API_KEY variables a user may have on their machine are deliberately NOT read: the keys this program uses
    are the ones the user gave this program."""
    p = _defaults().get(pid)
    if p is None:
        return ""
    key = str(load_auth(path).get(p.key_name) or "").strip()
    if not key and pid == "claude":
        key = os.environ.get(API_KEY_ENV, "").strip()
    return key


def valid_provider_key(pid: str, key: str) -> bool:
    k = key.strip()
    if pid == "claude":
        return valid_key_shape(k)
    return k.startswith("sk-") and len(k) >= 20 and " " not in k


def set_provider_key(pid: str, key: str, path: Path | None = None) -> tuple[bool, str]:
    p = _defaults().get(pid)
    if p is None:
        return False, f"没有叫 {pid} 的厂商（可选：claude、deepseek、kimi、openai）"
    key = key.strip()
    if not valid_provider_key(pid, key):
        return False, f"{p.name} 的 key 样子不对（应以 {p.key_prefix} 开头，中间没有空格）。"
    data = load_auth(path)
    data[p.key_name] = key
    save_auth(data, path)
    log.info("%s key saved (%s)", pid, mask(key))
    return True, f"{p.name} 的 key 存好了（{mask(key)}）。"


def clear_provider_key(pid: str, path: Path | None = None) -> tuple[bool, str]:
    p = _defaults().get(pid)
    if p is None:
        return False, f"没有叫 {pid} 的厂商"
    data = load_auth(path)
    data.pop(p.key_name, None)
    save_auth(data, path)
    return True, f"{p.name} 的 key 删掉了。"


# ---------- finding Claude Code ----------

def bundled_claude() -> str | None:
    """The Claude Code binary that ships inside the claude-agent-sdk pip package, if installed."""
    try:
        import claude_agent_sdk  # type: ignore
    except Exception:
        return None
    p = Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if os.name == "nt" else "claude")
    return str(p) if p.is_file() else None


def find_claude_binary(command: str = "claude") -> str | None:
    """The user's own Claude Code first (it carries their login), then the SDK's bundled copy."""
    found = shutil.which(command) if command else None
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude.exe"
    if local.exists():
        return str(local)
    return bundled_claude()


def auth_status(claude: str, timeout: float = 15.0) -> dict | None:
    """`claude auth status` as a dict (loggedIn, authMethod, email, subscriptionType), or None if unknown."""
    try:
        r = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=CREATE_NO_WINDOW)
        start = r.stdout.find("{")
        return json.loads(r.stdout[start:]) if start >= 0 else None
    except Exception:
        log.debug("claude auth status failed", exc_info=True)
        return None


# ---------- the decision ----------

def detect(command: str = "claude", *, find=find_claude_binary, status=auth_status, key: str | None = None) -> AccessStatus:
    key = api_key() if key is None else key
    claude = find(command)
    if key:
        if not valid_key_shape(key):
            return AccessStatus(False, "none", "API key 的样子不对（应以 sk-ant- 开头），请重新粘贴。", claude or "")
        if not claude:
            return AccessStatus(False, "none", "有 API key，但找不到 Claude Code 程序（它是引擎）。装官方 Claude Code，或再双击 install.bat 装上自带的引擎。")
        src = "，来自环境变量 ANTHROPIC_API_KEY，「AI 服务商」页清不掉它" if os.environ.get(API_KEY_ENV, "").strip() == key and not load_auth().get("anthropic_api_key") else ""
        return AccessStatus(True, "api_key", f"用 API key（{mask(key)}{src}）接入。", claude, mask(key))
    if not claude:
        return AccessStatus(False, "none", "没装 Claude Code，也没有 API key。有 Claude 订阅就装官方 Claude Code 并登录；"
                                           "没有的话再双击 install.bat 装上引擎，再在「AI 服务商」页填任一家的 API key。")
    st = status(claude)
    if st and st.get("loggedIn"):
        who = st.get("email") or ""
        kind = st.get("subscriptionType") or st.get("authMethod") or ""
        return AccessStatus(True, "subscription", f"用你自己的 Claude Code 登录接入（{who} {kind}）。".replace("  ", " "), claude, f"{who} {kind}".strip())
    return AccessStatus(False, "none", "Claude Code 装了但没登录：在「AI 服务商」页登录，或填任一家的 API key。", claude)


def codex_login_state() -> tuple[str, str, str]:
    """(state, codex exe, the status line): state is logged_in / not_logged_in / not_installed. The ChatGPT
    subscription reaches us only as the user's own `codex login`; we never touch ~/.codex ourselves."""
    from . import engine_codex
    exe = engine_codex.find_codex()
    if not exe:
        return "not_installed", "", ""
    ok, line = engine_codex.login_status(exe)
    return ("logged_in" if ok else "not_logged_in"), exe, line


def start_claude_login(command: str = "claude") -> dict:
    """`claude auth login` in a console of its own (it talks to the user there and in the browser)."""
    if testing.under_test_runner():
        return {"ok": False, "message": testing.REFUSAL}
    claude = find_claude_binary(command)
    if not claude:
        return {"ok": False, "message": "没装 Claude Code。"}
    try:
        subprocess.Popen([claude, "auth", "login"], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    except OSError as e:
        return {"ok": False, "message": f"claude auth login 起不来：{e}"}
    return {"ok": True, "message": "打开了 Claude Code 的登录窗口，照它的提示在浏览器里登录；完成后点“重新检测”。", "pending": True}


def detect_provider(pid: str, command: str = "claude", *, cfg=None, find=find_claude_binary, status=auth_status,
                    key: str | None = None) -> AccessStatus:
    """Can this provider answer right now? Claude goes through detect() (subscription or key); the others need
    their key, the Claude Code engine and, for OpenAI-format ones, the translation gateway."""
    if pid in ("", "claude"):
        st = detect(command, find=find, status=status, key=key)
        st.provider, st.provider_name = "claude", _defaults()["claude"].name
        st.model = st.model or str(getattr(cfg, "model", "") or "")
        return st
    if not known_provider(pid):
        return AccessStatus(False, "none", f"没有叫 {pid} 的厂商（可选：claude、deepseek、kimi、openai）", provider=pid)
    p = provider_for(pid, cfg)
    key = provider_key(pid) if key is None else key
    claude = find(command) or ""
    base = dict(claude=claude, provider=pid, provider_name=p.name, model=p.model)
    codex_state = ""
    if pid == "openai":  # the user's own Codex login (ChatGPT plan) comes first; a key + the gateway is the other way
        codex_state, exe, _ = codex_login_state()
        if codex_state == "logged_in":
            return AccessStatus(True, "codex_login", "用你自己登录的 Codex（ChatGPT 订阅）接入，Codex 当引擎。",
                                account="ChatGPT（Codex 已登录）", **{**base, "claude": exe})
    if not key:
        if pid == "openai":
            if codex_state == "not_installed":
                from . import engine_codex
                shim = engine_codex.codex_shim()
                how = (f"（找到的 {Path(shim).name} 是 npm 装的脚本版，用不了）" if shim else "") + \
                    "装 OpenAI 的官方 Windows 版 Codex（codex.exe）后在「AI 服务商」页点“登录”"
            else:
                how = "在「AI 服务商」页点“登录”（codex login）"
            return AccessStatus(False, "none", f"GPT 还没接入：{how}用 ChatGPT 订阅，或者填一个 OpenAI API key。", **base)
        return AccessStatus(False, "none", f"还没填 {p.name} 的 API key。", **base)
    if not valid_provider_key(pid, key):
        return AccessStatus(False, "none", f"{p.name} 的 key 样子不对（应以 {p.key_prefix} 开头），请重新粘贴。", **base)
    if not claude:
        return AccessStatus(False, "none", f"有 {p.name} 的 key，但找不到 Claude Code 程序（它是引擎）。装官方 Claude Code，或再双击 install.bat 装上自带的引擎。", **base)
    if not p.installed:
        return AccessStatus(False, "none", f"{p.name}的{p.install_hint}", **base)
    return AccessStatus(True, f"{pid}_key", f"用 {p.name}（{p.model}，key {mask(key)}）接入。", account=mask(key), **base)


def detect_for(pid: str, command: str = "claude", *, cfg=None) -> AccessStatus:
    """What claude.py asks for one line: Claude through detect() (which tests patch), others through detect_provider."""
    return detect(command) if pid in ("", "claude") else detect_provider(pid, command, cfg=cfg)


# Everything a vendor route sets, cleared for every line first: a Claude line must not inherit a stray key, base
# URL, token or model slot from the user's own environment, and a vendor line must not inherit another vendor's.
_CLEAR: dict[str, str | None] = {API_KEY_ENV: None, "ANTHROPIC_BASE_URL": None, "ANTHROPIC_AUTH_TOKEN": None,
                                 "ANTHROPIC_MODEL": None, "ANTHROPIC_DEFAULT_OPUS_MODEL": None,
                                 "ANTHROPIC_DEFAULT_SONNET_MODEL": None, "ANTHROPIC_DEFAULT_HAIKU_MODEL": None,
                                 "ANTHROPIC_SMALL_FAST_MODEL": None, "CLAUDE_CODE_SUBAGENT_MODEL": None,
                                 "ANTHROPIC_DEFAULT_FABLE_MODEL": None}


def env_overrides(st: AccessStatus, cfg=None) -> dict[str, str | None]:
    """What to put in Claude Code's environment (None = make sure the variable is absent).

    Claude: the key for api_key mode; a subscription must not be hijacked by a stray key, base URL or token
    (Claude Code prefers the key whenever the variable is set). Other providers: their Anthropic-compatible
    endpoint, the key as the auth token (both ANTHROPIC_API_KEY and the token set = Claude Code's conflict prompt),
    and every model slot pointed at their models so no "claude-*" name leaks through to be mapped by the other
    side; CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC as in DeepSeek's own Claude Code guide."""
    if st.provider in ("", "claude"):
        return {**_CLEAR, API_KEY_ENV: api_key()} if st.method == "api_key" else dict(_CLEAR)
    if st.method == "codex_login":
        raise RuntimeError("这条线该由 Codex 引擎来跑，不是 Claude Code")  # the switchboard picks the engine; never here
    p = provider_for(st.provider, cfg)
    base, token = p.base_url, provider_key(st.provider)
    if not token:
        raise RuntimeError(f"还没填 {p.name} 的 key")  # never start a line that would talk to the vendor with no key
    if p.via == "litellm":
        # Every restart path (a pending switch, the emergency brake, a fresh start) comes through here, so the
        # gateway is brought up here when it is not running; without it the line must NOT start, or Claude Code
        # would send the conversation to Anthropic with the user's own login and a foreign model name.
        if GATEWAY is None:
            raise RuntimeError(f"{p.name}的翻译层没有接上")
        ep = gateway_endpoint()
        if not ep:
            ok, why = GATEWAY.ensure(st.provider)
            ep = gateway_endpoint() if ok else None
            if not ep:
                raise RuntimeError(f"{p.name}的翻译层没起来：{why}")
        base, token = ep
    small, deep = p.small_model or p.model, p.deep_model or p.model
    env: dict[str, str | None] = {**_CLEAR, "ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": token,
                                  "ANTHROPIC_MODEL": p.model, "ANTHROPIC_DEFAULT_OPUS_MODEL": deep,
                                  "ANTHROPIC_DEFAULT_FABLE_MODEL": deep,  # Claude Code's newest slot (Moonshot's guide sets it)
                                  "ANTHROPIC_DEFAULT_SONNET_MODEL": p.model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": small,
                                  "ANTHROPIC_SMALL_FAST_MODEL": small, "CLAUDE_CODE_SUBAGENT_MODEL": small,
                                  "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    if not p.thinking:
        env["MAX_THINKING_TOKENS"] = "0"
    return env


def set_api_key(key: str, path: Path | None = None) -> AccessStatus:
    ok, msg = set_provider_key("claude", key, path)
    if not ok:
        return AccessStatus(False, "none", "API key 的样子不对（应以 sk-ant- 开头，中间没有空格）。")
    return detect()


def clear_api_key(path: Path | None = None) -> AccessStatus:
    clear_provider_key("claude", path)
    return detect()


# A *running* Claude Code session plants these in every child it starts (CLAUDECODE, CLAUDE_CODE_SDK_*,
# CLAUDE_CODE_MESSAGING_SOCKET, CLAUDE_CODE_SESSION_ID, CLAUDE_AGENT_SDK_*, MCP_* ...). A claude we start from inside
# such a session (the daemon or a test launched from a Claude Code terminal) then behaves as that session's child:
# on a non-Anthropic vendor it hangs without a word (the whole batch has to go; no single variable is enough). So every claude we start gets them stripped; documented user settings stay.
HOST_SESSION_PREFIXES = ("CLAUDE", "MCP_")
KEEP_CLAUDE_VARS = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                    "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                    "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
                    "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_ENABLE_TELEMETRY"}


def without_host_session(env: dict[str, str]) -> dict[str, str]:
    """env minus a host Claude Code session's own variables (see HOST_SESSION_PREFIXES)."""
    return {k: v for k, v in env.items()
            if not (k.upper().startswith(HOST_SESSION_PREFIXES) and k.upper() not in KEEP_CLAUDE_VARS)}


def _probe(st: AccessStatus, model: str, cfg, timeout: float, run: Callable) -> dict:
    """Really talk to the model once (a one-word answer) through Claude Code with this provider's environment."""
    if run is subprocess.run and testing.under_test_runner():
        raise RuntimeError(testing.REFUSAL)
    env = without_host_session(dict(os.environ))
    for k, v in env_overrides(st, cfg).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    t0 = time.time()
    try:
        r = run([st.claude, "-p", "只回答一个字：好", "--model", model, "--output-format", "text"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env,
                creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"{int(timeout)} 秒没有回答，可能是网络或登录问题。", "seconds": time.time() - t0}
    except Exception as e:
        return {"ok": False, "message": f"起不来：{e}", "seconds": time.time() - t0}
    secs = time.time() - t0
    who = st.provider_name or "Claude"
    if r.returncode == 0 and r.stdout.strip():
        return {"ok": True, "message": f"接通了，{who}回答“{r.stdout.strip()[:20]}”（{secs:.1f} 秒）。", "seconds": secs}
    err = (r.stderr or r.stdout or "").strip()
    hint = "登录过期或 key 无效" if any(w in err.lower() for w in ("auth", "api key", "401", "invalid")) else "看一下错误信息"
    return {"ok": False, "message": f"没接通（{hint}）：{err[:200]}", "seconds": secs}


def test_access(st: AccessStatus, timeout: float = 90.0, run: Callable = subprocess.run, cfg=None) -> dict:
    """Really talk to Claude once with the chosen method. Starts a claude process."""
    if not st.ok or not st.claude:
        return {"ok": False, "message": st.message, "seconds": 0.0}
    return _probe(st, "haiku" if st.provider in ("", "claude") else st.model, cfg, timeout, run)


def test_provider(pid: str, command: str = "claude", *, cfg=None, timeout: float = 120.0, run: Callable = subprocess.run,
                  find=find_claude_binary) -> dict:
    """Really talk to one provider once through Claude Code (5-40 s)."""
    st = detect_provider(pid, command, cfg=cfg, find=find)
    if not st.ok or not st.claude:
        return {"ok": False, "message": st.message, "seconds": 0.0}
    if st.method == "codex_login":
        from . import engine_codex
        return engine_codex.quick_probe(cfg if cfg is not None else __import__("voiceclaude.config", fromlist=["x"]).Config(), timeout)
    t0 = time.time()
    ok, why = check_reach(pid, cfg)
    if not ok:
        return {"ok": False, "message": f"{why} 没有起 Claude Code。", "seconds": time.time() - t0}
    return _probe(st, "haiku" if pid in ("", "claude") else st.model, cfg, timeout, run)
