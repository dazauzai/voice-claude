"""Load the user's config.toml with sensible defaults.

The live copy is ~/.voice-claude/config.toml (see paths.py). On first run it is created from a config.toml placed
next to the program, if there is one, or else from config.example.toml, so the repository itself never has to
carry anyone's personal settings or keys."""
from __future__ import annotations

import logging
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"


def user_config_path() -> Path:
    """Where the user's settings live; created from the repo copy / example the first time."""
    target = paths.CONFIG_FILE
    if not target.exists():
        source = paths.REPO_CONFIG if paths.REPO_CONFIG.exists() else paths.EXAMPLE_CONFIG
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            log.info("config: created %s from %s", target, source.name)
    return target

ASR_DIR = MODELS / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
VAD_MODEL = MODELS / "silero_vad.onnx"
TTS_DIR = MODELS / "kokoro-multi-lang-v1_1"

# Every action that may need the user's spoken permission, with the words used when talking about it.
PERMISSION_LABELS = {
    "screenshot": "看屏幕", "click": "点击", "type": "输入文字", "hotkey": "按快捷键", "scroll": "滚动",
    "open_app": "打开程序", "switch_model": "切换模型", "batch": "一次批准一串步骤",
    "other_connectors": "账号里的其他连接器", "background_job": "后台运行命令", "switch_provider": "切换厂商",
    "codex_shell": "Codex 在沙箱外运行命令", "codex_edit": "Codex 改文件",
}
PERMISSION_VALUES = {"ask": "每次问", "allow": "不用问", "deny": "禁止"}
DEFAULT_PERMISSIONS = {name: "ask" for name in PERMISSION_LABELS} | {"batch": "allow", "other_connectors": "deny"}


def normalize_permission_value(value: str) -> str | None:
    v = str(value).strip().lower()
    aliases = {"ask": "ask", "问": "ask", "每次问": "ask", "询问": "ask", "要问": "ask",
               "allow": "allow", "不用问": "allow", "允许": "allow", "不问": "allow", "免问": "allow", "直接做": "allow",
               "deny": "deny", "禁止": "deny", "不允许": "deny", "关闭": "deny", "关掉": "deny"}
    return aliases.get(v)


@dataclass
class Config:
    hotkey: str = "right ctrl"
    send_key: str = "right alt"  # double tap = interrupt the line and send the queued words now ("" = off)
    double_tap_seconds: float = 0.4
    hold_seconds: float = 0.8  # hold the key this long = emergency stop
    microphone: str = ""
    speaker: str = ""
    silence_seconds: float = 2.0
    barge_in: bool = True
    duck_seconds: float = 0.3  # you talk this long over Claude -> its voice is lowered
    cut_seconds: float = 1.5  # ... this long -> its voice stops (the task keeps running)
    # [daemon] the background service that hosts the lines
    daemon_port: int = 0  # 0 = random
    max_lines: int = 3
    idle_minutes: int = 10
    start_with: str = "manager"
    # [manager] the switchboard line
    manager_model: str = "sonnet"
    manager_effort: str = "low"
    manager_engine: str = "auto"  # auto = Claude Code when reachable, else the plain rules; or "claude" / "rules"
    model: str = "opus"
    effort: str = "low"
    working_directory: str = "~"
    command: str = "claude"
    # [providers] who answers: claude / deepseek / kimi / openai (Claude Code is the engine, except GPT through a ChatGPT plan (Codex)); keys live in auth.json
    provider: str = "claude"
    providers: dict[str, dict] = field(default_factory=dict)  # [providers.<id>] overrides: model, models, thinking, ...
    voice: int = 3
    speed: float = 1.0
    font_size: int = 11
    show_hints: bool = True  # [ui] the one-line shortcut hint under the buttons ("隐藏" in the window turns it off)
    permissions: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PERMISSIONS))
    transparent: bool = False  # [permissions] transparent: every "ask" goes through, except deleting and overwriting
    desktop_enabled: bool = True  # start the desktop-control MCP server at all ([desktop] enabled)
    desktop_apps: dict[str, str] = field(default_factory=dict)  # [desktop.apps]: name -> command, added to the built-in list
    replacements: dict[str, str] = field(default_factory=dict)
    resume: str = ""  # a Claude Code session id to continue (command line --resume), not from config.toml


def load(path: Path | None = None) -> Config:
    path = user_config_path() if path is None else path
    data = tomllib.loads(path.read_text("utf-8")) if path.exists() else {}
    c = Config()
    hk, au, cl, tts, ui, dm, mg = (data.get(k, {}) for k in ("hotkey", "audio", "claude", "tts", "ui", "daemon", "manager"))
    c.font_size = int(ui.get("font_size", c.font_size))
    c.show_hints = bool(ui.get("show_hints", c.show_hints))
    c.hotkey = str(hk.get("key", c.hotkey)).lower()
    c.send_key = str(hk.get("send_key", c.send_key)).lower()
    c.double_tap_seconds = float(hk.get("double_tap_seconds", c.double_tap_seconds))
    c.hold_seconds = float(hk.get("hold_seconds", c.hold_seconds))
    c.microphone = str(au.get("microphone", c.microphone))
    c.speaker = str(au.get("speaker", c.speaker))
    c.silence_seconds = float(au.get("silence_seconds", c.silence_seconds))
    c.barge_in = bool(au.get("barge_in", c.barge_in))
    c.duck_seconds = float(au.get("duck_seconds", c.duck_seconds))
    c.cut_seconds = float(au.get("cut_seconds", c.cut_seconds))
    c.daemon_port = int(dm.get("port", c.daemon_port))
    c.max_lines = int(dm.get("max_lines", c.max_lines))
    c.idle_minutes = int(dm.get("idle_minutes", c.idle_minutes))
    c.start_with = str(dm.get("start_with", c.start_with))
    c.manager_model = str(mg.get("model", c.manager_model))
    c.manager_effort = str(mg.get("effort", c.manager_effort))
    c.manager_engine = str(mg.get("engine", c.manager_engine)).lower()
    c.model = str(cl.get("model", c.model))
    c.effort = str(cl.get("effort", c.effort))
    c.working_directory = str(cl.get("working_directory", c.working_directory))
    c.command = str(cl.get("command", c.command))
    pv = data.get("providers", {})
    c.provider = str(pv.get("default", c.provider)).strip().lower() or "claude"
    c.providers = {str(k): dict(v) for k, v in pv.items() if isinstance(v, dict)}
    c.voice = int(tts.get("voice", c.voice))
    c.speed = float(tts.get("speed", c.speed))
    dt = data.get("desktop", {})
    c.desktop_enabled = bool(dt.get("enabled", c.desktop_enabled))
    c.desktop_apps = {str(k): str(v) for k, v in dt.get("apps", {}).items()}
    c.replacements = {str(k): str(v) for k, v in data.get("replacements", {}).items()}
    c.transparent = bool(data.get("permissions", {}).get("transparent", False))
    for name, value in data.get("permissions", {}).items():
        if name in PERMISSION_LABELS and normalize_permission_value(value):
            c.permissions[name] = normalize_permission_value(value)
    return c


def _literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(section: str, key: str, value, path: Path | None = None) -> None:
    """Write one value back into config.toml, keeping everything else (comments included) as it is."""
    path = user_config_path() if path is None else path
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    new_line = f"{key} = {_literal(value)}"
    start = next((i for i, l in enumerate(lines) if l.strip() == f"[{section}]"), None)
    if start is None:
        lines += ["", f"[{section}]", new_line]
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
        for i in range(start + 1, end):
            if re.match(rf"\s*{re.escape(key)}\s*=", lines[i]):
                lines[i] = new_line
                break
        else:
            lines.insert(start + 1, new_line)
    path.write_text("\n".join(lines) + "\n", "utf-8")


def missing_models() -> list[str]:
    """Names of model files that are not downloaded yet."""
    return [str(p.relative_to(MODELS)) for p in (ASR_DIR, VAD_MODEL, TTS_DIR) if not p.exists()]
