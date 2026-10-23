"""Render the window with sample content into docs/ui/preview-*.png and close it. No microphone, no daemon, no
hotkey; the window is on screen only while each picture is taken (about a second each).
Run: .venv\\Scripts\\python -X utf8 scripts\\preview_window.py [scenario ...]
Scenarios: small-listening, small-request, small-speaking, large, access, settings, permissions, menu (default: all)."""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["VOICECLAUDE_NO_HOTKEY"] = "1"
os.environ["VOICECLAUDE_HOME"] = tempfile.mkdtemp(prefix="vc-preview-")  # the example config, not yours

from PIL import ImageGrab  # noqa: E402

from voiceclaude import config  # noqa: E402
from voiceclaude.app import APPROVING, App, BUSY, IDLE, LISTENING  # noqa: E402

OUT = ROOT / "docs" / "ui"

LINES = {
    "manager": {"line": "manager", "name": "总机", "kind": "manager", "state": "idle", "active": False, "unread": 0},
    "p-1": {"line": "p-1", "name": "voice-claude", "kind": "project", "state": "working", "active": True, "unread": 2},
    "atlas": {"line": "atlas", "name": "Atlas", "kind": "project", "state": "waiting", "active": False, "unread": 0},
    "old": {"line": "old", "name": "旧项目-2024", "kind": "project", "state": "dead", "active": False, "unread": 0},
}


def fresh(width=None, height=None) -> App:
    cfg = config.load()
    cfg.font_size = 12
    cfg.show_hints = True
    app = App(cfg)
    app._build_window()
    px = app.theme.px
    app.root.geometry(f"{px(width or 420)}x{px(height or 560)}+{px(40)}+{px(40)}")
    app.root.attributes("-topmost", True)
    app.ready = True
    app.client.active = "p-1"
    app.client.info = {"ok": True, "access": {"ok": True, "method": "subscription", "message": "已登录", "account": "pro"}}
    app.ui_q.put(("lines", LINES, "p-1", "voice-claude"))
    app.ui_q.put(("devices", "耳机麦克风 (Realtek)", "扬声器 (Realtek)"))
    app._note("· 麦克风：耳机麦克风 (Realtek)")
    app._note("· 扬声器：扬声器 (Realtek)")
    app._note("—— 连续对话开始 ——")
    app._line("你", "帮我看一下 report 的图表，build 跑完了没有？")
    app._line("Claude", "")
    app._append("跑完了，一共 23 张图，其中 3 张有警告。要我把那 3 张列出来吗？")
    app._note("· Bash ls -F ~/projects/report/figures/")
    return app


def pump(app: App, seconds: float):
    """Let the queue drain and the orb render (the animation loop needs a few ticks)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        app.root.update()
        time.sleep(0.02)


def capture(app: App, name: str):
    app.root.update_idletasks()
    x, y = app.root.winfo_rootx(), app.root.winfo_rooty()
    w, h = app.root.winfo_width(), app.root.winfo_height()
    out = OUT / f"{name}.png"
    ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(out)
    print("saved", out)


def scenario_small_listening():
    app = fresh()
    app.state = LISTENING
    app.conversation = True
    app._status("在听…")
    app.ui_q.put(("level", 0.03))
    app.root.after(50, app._drain_ui)
    pump(app, 0.7)
    app.ui_q.put(("level", 0.04))
    pump(app, 0.3)
    capture(app, "preview-small-listening")
    app.root.destroy()


def scenario_small_request():
    app = fresh()
    app.state = APPROVING
    app.conversation = True
    app._status("等你回答")
    app._note("（申请）我想在后台运行：python make_figures.py --dpi 300 data.csv，输出记到日志，可以吗？\n　　python make_figures.py --dpi 300 data.csv > figures.log")
    app.root.after(50, app._drain_ui)
    pump(app, 0.7)
    app.card_info["started"] -= 12  # show the drain bar partly spent
    pump(app, 0.3)
    capture(app, "preview-small-request")
    app.root.destroy()


def scenario_small_speaking():
    app = fresh()
    app.state = BUSY
    app.conversation = True
    app.turn_open = True
    app._line("你", "那把那三张列出来，顺便看看 Atlas 那条线。")
    app._line("Claude", "")
    app._status("正在跑命令…")
    app._note("· Bash grep -c WARN figures.log")
    app._append("好，三张分别是图 4、图 5 和图 7。")
    app._status("在说…")
    app.queued.append("等一下，先看 Atlas 那边。")
    app._line("你", "（排队）等一下，先看 Atlas 那边。")
    app.root.after(50, app._drain_ui)
    pump(app, 1.0)
    capture(app, "preview-small-speaking")
    app.root.destroy()


def scenario_large():
    app = fresh(920, 640)
    app.state = BUSY
    app.conversation = True
    app.turn_open = True
    app._status("思考中…")
    app._note("后台任务 8a2f：running 生成图表")
    app._note("后台任务 1c9e：done 跑测试")
    app._line("你", "再把 Atlas 那边的进度也说一下。")
    app._line("Claude", "")
    app._append("Atlas 那条线在等你批准一次点击，我先把这边的结果整理好。")
    app.root.after(50, app._drain_ui)
    pump(app, 1.0)
    capture(app, "preview-large")
    app.root.destroy()


def scenario_access():
    app = fresh()
    app.state = IDLE
    app._status("空闲")
    app.root.after(50, app._drain_ui)
    pump(app, 0.3)
    # the shape of docs/protocol.md 7b / 7b-1: the vendors, the default on DeepSeek (list_providers is faked here)
    note = "只存在你电脑上的 ~/.voice-claude/auth.json，不会出现在日志和窗口里。"
    field = lambda label, hint: [{"key": "api_key", "label": label, "secret": True, "hint": hint}]  # noqa: E731
    app.client.list_providers = lambda: {"ok": True, "default": "deepseek", "providers": [
        {"id": "claude", "name": "Claude（Anthropic）", "configured": False, "account": "", "ok": False, "message": "没登录，也没填 key。",
         "model": "", "models": ["opus", "sonnet", "fable"], "installed": True, "install_hint": "", "is_default": False,
         "key_prefix": "sk-ant-", "url": "https://console.anthropic.com/settings/keys", "subscription": True,
         "methods": ["login", "key"], "login_state": "not_logged_in", "engine": "claude",
         "fields": field("Claude（Anthropic） API key", "sk-ant- 开头；有自己登录的 Claude Code 可以不填")},
        {"id": "deepseek", "name": "DeepSeek（深度求索）", "configured": True, "account": "…ab12", "ok": True, "message": "能用。",
         "model": "deepseek-flash", "models": ["flash", "pro"], "installed": True, "install_hint": "", "is_default": True,
         "key_prefix": "sk-", "url": "https://platform.deepseek.com/api_keys", "subscription": False,
         "methods": ["key"], "login_state": "", "engine": "claude",
         "fields": field("DeepSeek（深度求索） API key", "platform.deepseek.com 申请，sk- 开头")},
        {"id": "openai", "name": "GPT（OpenAI）", "configured": False, "account": "", "ok": False, "message": "没登录，也没填 key。",
         "model": "gpt-6.1-sol", "models": ["sol", "luna"], "installed": False, "install_hint": "翻译层还没装：再运行一次 install.bat --with-gpt。", "is_default": False,
         "key_prefix": "sk-", "url": "https://platform.openai.com/api-keys", "subscription": True,
         "methods": ["login", "key"], "login_state": "not_logged_in", "engine": "claude",
         "fields": field("GPT（OpenAI） API key", "platform.openai.com 申请，sk- 开头")}],
        "lines": [{"line": "manager", "name": "总机", "provider": "deepseek", "model": "deepseek-flash"},
                  {"line": "p-1", "name": "voice-claude", "provider": "claude", "model": "claude-sonnet-4-5"}]}
    app._open_access({"ok": True, "method": "api_key", "message": "用 DeepSeek API key（…ab12）接入。", "account": "…ab12",
                      "provider": "deepseek", "provider_name": "DeepSeek", "model": "deepseek-flash"})
    pump(app, 0.6)
    capture(app, "preview-access")
    app.root.destroy()


def _settings_app():
    app = fresh()
    app.state = IDLE
    app._status("空闲")
    app.root.after(50, app._drain_ui)
    pump(app, 0.3)
    app._on_access_info({"ok": True, "method": "subscription", "account": "pro", "provider": "claude"})
    return app


def scenario_settings():
    app = _settings_app()
    app._open_settings()
    pump(app, 0.5)
    capture(app, "preview-settings")
    app.settings_canvas.yview_moveto(1.0)
    pump(app, 0.3)
    capture(app, "preview-settings-bottom")
    app.root.destroy()


def scenario_permissions():
    app = _settings_app()
    app._open_settings()
    app._open_permissions()
    pump(app, 0.5)
    capture(app, "preview-permissions")
    app.root.destroy()


def scenario_menu():
    app = _settings_app()
    app._toggle_panel()
    pump(app, 0.5)
    capture(app, "preview-menu")
    app.root.destroy()


SCENARIOS = {"small-listening": scenario_small_listening, "small-request": scenario_small_request,
             "small-speaking": scenario_small_speaking, "large": scenario_large, "access": scenario_access,
             "settings": scenario_settings, "permissions": scenario_permissions, "menu": scenario_menu}

if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    for n in names:
        SCENARIOS[n]()
