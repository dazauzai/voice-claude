"""Desktop control MCP server ("desktop"): screenshot, click, type_text, press_keys, scroll, open_app, open_path, run_steps.

Every tool first asks the user out loud through voice-claude and does NOTHING unless the user says 可以.
That gate lives here, in the tool code, not in a prompt: Claude can only call the tool, it cannot skip the ask.
The app may answer from its permissions table instead (每次问 / 不用问 / 禁止), as the user configured.

Started by Claude Code as a separate process (see claude.py). It reaches the running app over the local
control endpoint whose port is in VOICECLAUDE_PORT (POST /ask). Run by hand for a quick look:
    .venv\\Scripts\\python -X utf8 voiceclaude\\desktop.py --list
"""
from __future__ import annotations

import ctypes
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pyautogui  # noqa: E402  (importing it makes this process DPI-aware: coordinates are physical pixels)
from mcp.server.mcpserver import Image, MCPServer  # noqa: E402

from voiceclaude import config, risk, testing  # noqa: E402

log = logging.getLogger("desktop")

PORT = os.environ.get("VOICECLAUDE_PORT", "")
LINE = os.environ.get("VOICECLAUDE_LINE", "")  # which line this server belongs to (the daemon routes by it)
ASK_TIMEOUT = 90  # the app itself gives up after 60 s; this is only a safety net
MAX_WIDTH = 1280  # screenshots are shrunk to this width; click coordinates are given in that shrunken image
SPOKEN_MAX = 60  # typed text longer than this is only partly read out
DEFAULT_APPS = {
    "记事本": "notepad", "notepad": "notepad",
    "计算器": "calc", "calculator": "calc",
    "画图": "mspaint", "paint": "mspaint",
    "文件管理器": "explorer", "资源管理器": "explorer", "explorer": "explorer",
    "浏览器": "chrome", "chrome": "chrome", "edge": "msedge",
    "终端": "wt", "terminal": "wt",
    "设置": "ms-settings:", "settings": "ms-settings:",
    "vs code": "code", "vscode": "code", "code": "code",
}
_CJK = re.compile(r"[一-鿿]")

pyautogui.FAILSAFE = True  # flinging the mouse into the top-left corner aborts any action
pyautogui.PAUSE = 0.05

server = MCPServer("desktop", instructions=(
    "桌面控制：截图、点击、输入文字、按键、滚动、打开程序。每个工具会先向用户口头申请，用户说“可以”才执行；"
    "你不用先问用户，直接调用，被拒绝就不要重试同一个动作。坐标按最近一次截图的像素给。"
    "输入文字前先确认前台窗口是对的（open_app 的结果和 screenshot 都会告诉你）。"))


# ---------- the gate ----------

def _post(payload: dict, path: str = "ask") -> dict:
    """One request to the app's control endpoint; a dict with at least ok/message comes back."""
    if not PORT:
        return {"ok": False, "message": "语音助手没有在运行（VOICECLAUDE_PORT 未设置），无法向用户申请，没有执行。"}
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/{path}", data=json.dumps({**payload, "line": LINE}).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=ASK_TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": f"向用户申请失败（{e}），没有执行。"}


def ask(action: str, question: str, detail: str = "", facts: dict | None = None) -> tuple[bool, str]:
    """Ask the user out loud via the running voice-claude. Returns (approved, message for Claude).
    `action` is the permissions-table key (screenshot / click / type / hotkey / scroll / open_app); `facts` are
    the plain facts of the step (keys, typed text, click target) for the service's own risk check."""
    payload = {"action": action, "question": question, "detail": detail}
    if facts:
        payload["facts"] = facts
    d = _post(payload)
    return bool(d.get("ok")), str(d.get("message", ""))


def question(what: str, purpose: str = "") -> str:
    p = purpose.strip().rstrip("。，,.")
    return (f"{p}，" if p else "") + f"我想{what}，可以吗？"


def approved(action: str, what: str, purpose: str = "", detail: str = "", facts: dict | None = None) -> tuple[bool, str]:
    """The gate every tool goes through: (True, '') only if the user (or their permissions table) said yes."""
    q = question(what, purpose)
    ok, msg = ask(action, q, detail, facts)
    log.info("ask %s %r -> %s %s", action, q, "yes" if ok else "no", msg)
    if ok:
        return True, ""
    return False, msg or "用户没有同意，没有执行。不要再试同一个动作，口头说一句就好。"


# ---------- pure helpers (unit-tested) ----------

KEY_ALIASES = {
    "control": "ctrl", "windows": "win", "cmd": "win", "super": "win", "meta": "win", "winleft": "win",
    "return": "enter", "escape": "esc", "del": "delete", "spacebar": "space",
    "pgup": "pageup", "pgdn": "pagedown", "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "ins": "insert", "prtsc": "printscreen", "printscr": "printscreen",
}
KEY_WORDS = {
    "ctrl": "Control", "alt": "Alt", "shift": "Shift", "win": "Windows 键", "enter": "回车", "esc": "Escape",
    "tab": "Tab", "space": "空格", "backspace": "退格", "delete": "Delete", "insert": "Insert",
    "up": "上箭头", "down": "下箭头", "left": "左箭头", "right": "右箭头", "home": "Home", "end": "End",
    "pageup": "Page Up", "pagedown": "Page Down", "printscreen": "截屏键", "capslock": "大写锁定",
}


def parse_keys(keys: str) -> list[str]:
    """'Ctrl+Shift+S' -> ['ctrl', 'shift', 's']; raises ValueError for a key pyautogui does not know."""
    parts = [p.strip().lower() for p in re.split(r"[+\s]+", keys.strip()) if p.strip()]
    out = []
    for p in parts:
        p = KEY_ALIASES.get(p, p)
        if p not in pyautogui.KEYBOARD_KEYS:
            raise ValueError(f"不认识的键 {p!r}")
        out.append(p)
    if not out:
        raise ValueError("没有给出要按的键")
    return out


def spoken_keys(keys: list[str]) -> str:
    return " 加 ".join(KEY_WORDS.get(k, k.upper() if len(k) == 1 else k) for k in keys)


_PATH_LIKE = re.compile(r"^\s*\"?([A-Za-z]:[\\/]|\\\\|~[\\/]|/)")


def looks_like_path(text: str) -> bool:
    return bool(_PATH_LIKE.match(text)) and "\n" not in text.strip()


def spoken_text(text: str) -> str:
    """What the user hears for type_text. A path is spoken by its last name only (the full text is shown in the
    window's request card, which otherwise truncates a long path), ordinary text in full up to SPOKEN_MAX."""
    if looks_like_path(text):
        tail = Path(text.strip().strip('"')).name or text.strip()
        return f"输入路径……{tail}"
    t = text.replace("\r", "").replace("\n", "，换行，").replace("\t", "，Tab，")
    if len(text) <= SPOKEN_MAX:
        return f"输入文字：{t}"
    return f"输入文字：{t[:40]}……一共 {len(text)} 个字"


def short_title(title: str) -> str:
    """'收件箱 - Alex - Outlook - Google Chrome' -> 'Google Chrome（收件箱）': the app, then what is open in it."""
    parts = [p.strip() for p in re.split(r"\s+[-—–|]\s+", title or "") if p.strip()]
    if len(parts) >= 2:
        return f"{parts[-1][:24]}（{parts[0][:16]}）"
    return (title or "")[:40]


def in_window(what: str, title: str | None = None) -> str:
    """Prefix an action with the window it will land in, so the user hears WHERE the keys go."""
    title = short_title(active_window() if title is None else title)
    return f"在“{title}”里{what}" if title else what


_WINDOW_PREFIX = re.compile(r"^(在“[^”]+”里)(.*)$", re.S)


def join_steps(whats: list[str]) -> str:
    """'在“X”里输入 a，然后在“X”里按回车' -> '在“X”里：输入 a，然后按回车': a window is named once."""
    out, current = [], None
    for w in whats:
        m = _WINDOW_PREFIX.match(w)
        if m and m.group(1) == current:
            out.append(m.group(2))
        elif m:
            current = m.group(1)
            out.append(w)
        else:
            current = None
            out.append(w)
    if len(whats) >= 2:
        m = _WINDOW_PREFIX.match(whats[0])
        if m and all(_WINDOW_PREFIX.match(w) and _WINDOW_PREFIX.match(w).group(1) == m.group(1) for w in whats):
            return m.group(1) + "：" + "，然后".join(_WINDOW_PREFIX.match(w).group(2) for w in whats)
    return "，然后".join(out)


def apps() -> dict[str, str]:
    """The allow-list: built-in names plus [desktop.apps] from config.toml (user entries win)."""
    try:
        user = config.load().desktop_apps
    except Exception:
        log.exception("config.toml unreadable; using the built-in app list")
        user = {}
    return {**DEFAULT_APPS, **{k.strip().lower(): v for k, v in user.items()}}


def resolve_app(name: str, table: dict[str, str] | None = None) -> tuple[str, str] | None:
    """(spoken label, command) for a program name on the allow-list, else None."""
    table = table if table is not None else apps()
    key = name.strip().lower().removesuffix(".exe")
    cmd = table.get(key) or table.get(key.replace(" ", ""))
    if cmd is None:
        return None
    label = next((k for k, v in table.items() if v == cmd and _CJK.search(k)), key)
    return label, cmd


def shot_scale() -> float:
    """Physical pixels per screenshot pixel (screenshots are shrunk to MAX_WIDTH)."""
    w, _ = pyautogui.size()
    return max(1.0, w / MAX_WIDTH)


def to_screen(x: int, y: int) -> tuple[int, int]:
    s = shot_scale()
    return int(round(x * s)), int(round(y * s))


# ---------- doing things ----------

def active_window() -> str:
    """Title of the foreground window ('' if none)."""
    try:
        user32 = ctypes.windll.user32
        h = user32.GetForegroundWindow()
        n = user32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        return buf.value.strip()
    except Exception:
        return ""


@dataclass
class Window:
    """The window the user heard a typing / keys step is for. It is checked again right before the step acts: the
    approval itself (a click on 可以, a pause) may have moved the focus, and in run_steps an earlier step opens it."""
    hwnd: int = 0
    title: str = ""
    pending: bool = False  # opened by an earlier step of the same run; hwnd is filled in once that step has run
    menu: bool = False  # its thread was in a menu when the step was asked: the step acts inside that menu


@dataclass
class Run:
    """What the steps of one run_steps share. A typing / keys step right after a click or keys step goes to whatever
    that step brought up (a Save As dialog after Ctrl+S, a menu after a right click)."""
    windows: set[int] | None = None  # every top-level window just before the latest click / keys step acted
    batch: bool = True  # approved once for all steps (False: asked step by step, so the focus may move between them)


_USER32 = None


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]


_GUI_MENU_FLAGS = 0x04 | 0x08 | 0x10  # GUI_INMENUMODE, GUI_SYSTEMMENUMODE, GUI_POPUPMENUMODE


def _u32():
    """user32 with proper prototypes, kept apart from ctypes.windll (pyautogui's own calls stay as they are)."""
    global _USER32
    if _USER32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetForegroundWindow.restype = wintypes.HWND
        for name in ("IsWindow", "IsIconic", "SetForegroundWindow"):
            getattr(u, name).argtypes = [wintypes.HWND]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.WindowFromPoint.restype = wintypes.HWND
        _USER32 = u
    return _USER32


def _root(hwnd: int) -> int:
    """The top of the owner chain (GA_ROOTOWNER): a Save As dialog's root is the window that opened it."""
    try:
        return int(_u32().GetAncestor(hwnd, 3) or 0) if hwnd else 0
    except Exception:
        return 0


def _pid(hwnd: int) -> int:
    try:
        pid = wintypes.DWORD(0)
        _u32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _process_image(hwnd: int) -> str:
    """The program file of the window's process ('notepad.exe'), '' when it cannot be read."""
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k.OpenProcess(0x1000, False, _pid(hwnd))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            return Path(buf.value).name if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
        finally:
            k.CloseHandle(h)
    except Exception:
        return ""


def _window_at(x: int, y: int) -> int:
    """The top-level window under a screen point."""
    try:
        h = int(_u32().WindowFromPoint(wintypes.POINT(x, y)) or 0)
        return _root(h) or h
    except Exception:
        return 0


def _frontend_pid() -> int:
    """The voice-claude window's process (the service learns it when the window connects); 0 when unknown."""
    try:
        return int(_post({}, "frontend_pid").get("pid") or 0)
    except Exception:
        return 0


def is_frontend(hwnd: int, pid: int | None = None) -> bool:
    """Is it the voice-claude window? Keys typed there count as the user's own words (answers to request cards,
    fixed phrases, new sentences), so the desktop tools never type, press keys or click into it."""
    if not hwnd:
        return False
    pid = _frontend_pid() if pid is None else pid
    return bool(pid) and _pid(hwnd) == pid


FRONTEND_REFUSAL = "那是 voice-claude 自己的窗口：在那里输入、按键或点击等于替用户说话，没有执行。先让用户切到要操作的窗口。"


def _related(fg: int, hwnd: int, same_process: bool = False) -> bool:
    """Is the window in front the announced one, or a window it owns (its dialog)? With same_process, also any window
    of the same program."""
    if fg == hwnd:
        return True
    if not fg or not hwnd:
        return False
    r = _root(fg)
    if r and r == _root(hwnd):
        return True
    return same_process and _pid(fg) != 0 and _pid(fg) == _pid(hwnd)


def _fg_hwnd() -> int:
    try:
        return int(_u32().GetForegroundWindow() or 0)
    except Exception:
        return 0


def current_window() -> Window:
    h = _fg_hwnd()
    return Window(h, active_window(), menu=_in_menu_mode(h))


def _activate(hwnd: int) -> bool:
    """Bring the announced window back to the front (answering in the voice-claude window takes the focus)."""
    if testing.under_test_runner():
        return False  # a unit test never moves the real desktop
    try:
        u = _u32()
        if not u.IsWindow(hwnd):
            return False
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)  # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        return _fg_hwnd() == hwnd
    except Exception:
        return False


def _in_menu_mode(hwnd: int) -> bool:
    """Is that window's thread in a menu (a lone Alt, the approval key, opens a classic menu bar)? XAML key tips,
    as in the new Notepad, are not a menu loop and do not show up here."""
    try:
        u = _u32()
        tid = u.GetWindowThreadProcessId(hwnd, None)
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not tid or not u.GetGUIThreadInfo(tid, ctypes.byref(info)):
            return False
        return bool(info.flags & _GUI_MENU_FLAGS)
    except Exception:
        return False


def _press_esc() -> None:
    if testing.under_test_runner():
        return
    pyautogui.press("esc")
    time.sleep(0.05)


NOT_DONE = "没有做："  # how a step that refused to act begins; run_steps stops there


def replace_prompt_popped(announced: str) -> str:
    """'' unless a "replace it?" confirmation is in front that the step was not announced for: a later step of a
    batch must never answer it (that would overwrite a file nobody was asked about)."""
    now = active_window()
    if risk.in_replace_dialog(now) and not risk.in_replace_dialog(announced):
        return (f"{NOT_DONE}弹出了“{short_title(now)}”：会覆盖已有文件。要覆盖请单独按确认键（会先问用户），"
                "不要就按 Esc。")
    return ""


def dialog_folder(hwnd: int) -> str:
    """The folder a Save As dialog is showing, from its address bar ("地址: C:\\..." / "Address: ..."); '' when it
    cannot be read or is not a real folder."""
    found: list[str] = []
    if not hwnd:
        return ""  # EnumChildWindows(NULL) would walk every top-level window
    try:
        u = ctypes.windll.user32

        def each(h, _):
            cls = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(h, cls, 64)
            if cls.value == "ToolbarWindow32":
                n = u.GetWindowTextLengthW(h)
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, buf, n + 1)
                m = re.match(r"^\s*(?:地址|Address)\s*[:：]\s*(.+)$", buf.value)
                if m and Path(m.group(1).strip()).is_dir():
                    found.append(m.group(1).strip())
                    return False
            return True
        u.EnumChildWindows(hwnd, ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(each), 0)
    except Exception:
        log.debug("could not read the dialog's folder", exc_info=True)
    return found[0] if found else ""


def _settle() -> None:
    """Give a window the previous step brings up (a dialog after Ctrl+S, a menu after a right click) time to appear:
    at least 0.4 s, then until the foreground has not changed for 0.2 s (1.5 s at most)."""
    time.sleep(0.4)
    last, since, t0 = _fg_hwnd(), time.monotonic(), time.monotonic()
    while time.monotonic() - t0 < 1.5:
        time.sleep(0.05)
        h = _fg_hwnd()
        if h != last:
            last, since = h, time.monotonic()
        elif time.monotonic() - since >= 0.2:
            return


def ready_window(w: Window, follows: Run | None = None) -> str:
    """'' when the step may act on the window in front, else why it is not done. Accepted: the announced window or a
    window it owns (its dialog). A step right after a click / keys step of the same run (`follows`) also accepts a
    window of the same program, and, when the run was approved at once, a window that did not exist before that step.
    An existing window that a click or Alt+Tab merely brought forward is refused (the user heard another one). Focus
    is brought back to the announced window only when nothing acceptable is in front and no earlier step of an
    approved-at-once run can have moved it there on purpose."""
    if follows is not None and follows.batch:
        _settle()
    placeholder = w.pending and not w.hwnd  # whatever the step before brings up (after Win+R, say)
    if placeholder and follows is None:
        return f"{NOT_DONE}“{short_title(w.title)}”没有到前台。停下来问用户。"
    fg = _fg_hwnd()
    ok = _related(fg, w.hwnd, same_process=follows is not None) if not placeholder else False
    if not ok and follows is not None and (follows.batch or placeholder) and fg and follows.windows \
            and fg not in follows.windows:
        ok = True  # a window the click / keys step before this one brought up (a window it merely activated is not)
        if placeholder:
            w.hwnd, w.title = fg, active_window() or w.title  # the steps after it go to the same window
    if not ok and w.hwnd and not (follows is not None and follows.batch) and _activate(w.hwnd):
        fg = _fg_hwnd()
        ok = _related(fg, w.hwnd, same_process=follows is not None)
    if not ok:
        now = short_title(active_window()) or "（无）"
        return f"{NOT_DONE}前台窗口变了（申请时是“{short_title(w.title) or '（无）'}”，现在是“{now}”）。停下来问用户。"
    if is_frontend(fg):
        return NOT_DONE + FRONTEND_REFUSAL
    if w.menu or follows is not None:
        return ""  # a menu that was open when asked, or that the step before opened: act inside it as announced
    for _ in range(2):
        if not _in_menu_mode(fg):
            return ""
        _press_esc()  # leave a menu that opened since the request (a lone Alt, the approval key, opens a menu bar)
    if not _in_menu_mode(fg):
        return ""
    return f"{NOT_DONE}“{short_title(w.title)}”停在菜单里，按 Esc 也没退出。停下来问用户。"


def _launch(cmd: str) -> None:
    subprocess.Popen(["cmd", "/c", "start", "", cmd], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _all_windows() -> set[int]:
    """Every top-level window handle, titled or not (EnumWindows); empty when they cannot be listed."""
    out: set[int] = set()
    try:
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(lambda h, _: out.add(int(h or 0)) or True)
        ctypes.windll.user32.EnumWindows(proc, 0)
    except Exception:
        log.debug("could not list the windows", exc_info=True)
    return out


def _window_handles() -> set[int]:
    try:
        import pygetwindow as gw
        return {w._hWnd for w in gw.getAllWindows() if w.title}
    except Exception:
        return set()


def _bring_front(before: set[int]) -> None:
    """Best effort: put a window that appeared since `before` in the foreground, so typing goes there and not
    into whatever the user had open. Windows may refuse (another app is active); the ask names the window anyway."""
    if testing.under_test_runner():
        return
    try:
        import pygetwindow as gw
        for w in gw.getAllWindows():
            if w.title and w._hWnd not in before and not w.isMinimized:
                w.activate()
                return
    except Exception:
        log.debug("could not bring the new window to the front", exc_info=True)


# SendInput with KEYEVENTF_UNICODE types any character (Chinese included) and bypasses the IME,
# so an active Chinese input method does not turn "hello" into pinyin candidates.
_ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_INPUT_KEYBOARD, _KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE = 1, 0x0002, 0x0004


def _send_input(arr) -> int:
    if testing.under_test_runner():
        raise RuntimeError(testing.REFUSAL)  # a unit test never types on the real desktop
    return ctypes.windll.user32.SendInput(2, arr, ctypes.sizeof(_INPUT))


def _fail_safe_check() -> None:
    """pyautogui checks its corner fail-safe inside its own functions only; typing goes through SendInput, so
    it is checked here before every character (FailSafeException, which _guard turns into a sentence)."""
    pyautogui.failSafeCheck()


class TypingStopped(Exception):
    def __init__(self, typed: int):
        super().__init__(typed)
        self.typed = typed


def _send_unicode(text: str, guard: Callable[[], bool] | None = None) -> None:
    """Type text; before every character the corner fail-safe, and `guard` (False: stop, TypingStopped)."""
    for n, ch in enumerate(text):
        _fail_safe_check()
        if guard is not None and not guard():
            raise TypingStopped(n)
        if ch == "\r":
            continue
        if ch in "\n\t":
            pyautogui.press("enter" if ch == "\n" else "tab")
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i:i + 2], "little")
            arr = (_INPUT * 2)()
            for j, flags in enumerate((_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)):
                arr[j].type = _INPUT_KEYBOARD
                arr[j].u.ki = _KEYBDINPUT(0, code, flags, 0, 0)
            if _send_input(arr) != 2:
                raise OSError(f"SendInput 失败（错误码 {ctypes.get_last_error()}）")
        time.sleep(0.01)


def _guard(fn: Callable[[], Any]):
    """Turn pyautogui's fail-safe and other errors into a sentence for Claude."""
    try:
        return fn()
    except pyautogui.FailSafeException:
        log.warning("fail-safe triggered")
        return "用户把鼠标甩到了屏幕角落，动作已中止。停下来问用户。"
    except Exception as e:
        log.exception("action failed")
        return f"动作失败：{e}"


# ---------- plans: what a tool would do, before anyone is asked ----------

@dataclass
class Plan:
    action: str  # permissions-table key
    what: str  # spoken: "打开记事本"
    detail: str  # shown in the window, not spoken
    go: Callable[[], Any]  # does it; only ever called after approval
    facts: dict = field(default_factory=dict)  # plain facts for the service's risk check (通透模式): keys, text, target
    opens: Window | None = None  # open_app / open_path: the window it brings up, for the steps after it
    window: Window | None = None  # type / keys: the window it was announced for, shared with the steps after it


def plan_screenshot() -> Plan:
    def go():
        img = pyautogui.screenshot()
        w, h = img.size
        s = shot_scale()
        if s > 1.0:
            img = img.resize((int(w / s), int(h / s)))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=70)
        log.info("screenshot %dx%d -> %dx%d, %d bytes", w, h, *img.size, buf.tell())
        return [f"屏幕 {w}x{h}，这张图缩小为 {img.size[0]}x{img.size[1]}；点击坐标请按图上的像素给。前台窗口：{active_window() or '（无）'}。",
                Image(data=buf.getvalue(), format="jpeg")]
    return Plan("screenshot", "看一眼屏幕", "", go, {"kind": "screenshot"})


def plan_click(x: int, y: int, target: str, button: str = "left", double: bool = False, run: Run | None = None) -> Plan | str:
    button = button.strip().lower()
    if button not in ("left", "right", "middle"):
        return "button 只能是 left、right 或 middle，没有执行。"
    what = ("双击" if double else "右键点一下" if button == "right" else "点一下") + target.strip()
    announced = active_window()
    if is_frontend(_window_at(*to_screen(x, y))):
        return FRONTEND_REFUSAL  # its 可以 button answers a request: never clicked for the user

    def go():
        why = replace_prompt_popped(announced)
        if why:
            return why
        if is_frontend(_window_at(*to_screen(x, y))):
            return NOT_DONE + FRONTEND_REFUSAL
        if run is not None:
            run.windows = _all_windows()
        sx, sy = to_screen(x, y)
        pyautogui.click(sx, sy, clicks=2 if double else 1, button=button)
        return f"已{what}（屏幕坐标 {sx}, {sy}）。"
    return Plan("click", what, f"坐标 ({x}, {y})", go, {"kind": "click", "target": target.strip(), "window": announced})


def plan_type(text: str, window: Window | None = None, run: Run | None = None, follows: bool = False) -> Plan | str:
    if not text:
        return "没有要输入的文字。"
    w = window or current_window()
    if not w.pending and is_frontend(w.hwnd):
        return FRONTEND_REFUSAL
    facts = {"kind": "type", "text": text, "window": w.title}
    if w.hwnd and risk.SAVE_DIALOG.search(w.title or "") and not risk.in_replace_dialog(w.title):
        folder = dialog_folder(w.hwnd)  # so a bare name that exists there is asked about in 通透模式
        if folder:
            facts["folder"] = folder

    def go():
        why = ready_window(w, run if follows else None) or replace_prompt_popped(w.title)
        if why:
            return why
        target, pid = _fg_hwnd(), _frontend_pid()

        def same_window() -> bool:  # checked before every character: the rest must not land anywhere else
            now = _fg_hwnd()
            return now == target and not (pid and _pid(now) == pid)
        try:
            _send_unicode(text, same_window)
        except TypingStopped as s:
            return (f"{NOT_DONE}输入到一半前台窗口变了（已输入 {s.typed} 个字，其余的没有输入）。"
                    "停下来问用户。")
        return f"已输入 {len(text)} 个字。"
    return Plan("type", in_window(spoken_text(text), w.title), text, go, facts, window=w)


WINDOW_KEYS = {"up", "down", "left", "right", "home"}  # Win+these maximise, minimise, snap or move the window in front


def shell_shortcut(keys: list[str]) -> bool:
    """A combination Windows handles itself (Win+…, Alt+Tab, Ctrl+Esc, Ctrl+Shift+Esc, Print Screen): it never reaches
    the window in front, so the voice-claude window being there does not matter. Not Win+arrows / Win+Home (they act
    on the window in front) and not Ctrl+Alt+Del (injected, it is no secure attention sequence: the window gets it)."""
    k = set(keys)
    if k in ({"win", "ctrl", "left"}, {"win", "ctrl", "right"}):
        return True  # switch virtual desktop: the shell's, not the window's
    if "win" in k:
        return not (k & WINDOW_KEYS)
    return "printscreen" in k or k in ({"alt", "tab"}, {"alt", "shift", "tab"}, {"ctrl", "esc"}, {"ctrl", "shift", "esc"})


# Shortcuts that bring up a window of their own: what follows them in a run goes there.
OPENERS = [{"win"}, {"ctrl", "esc"}, {"ctrl", "shift", "esc"}, {"win", "tab"}, {"win", "shift", "s"}, {"printscreen"}] \
    + [{"win", c} for c in "reixsanvkpucwg123456789"]


def _new_foreground(existing: set[int], timeout: float = 1.5) -> int:
    """The window a shortcut brought up, once it is in front (0 if none came up in time)."""
    t0 = time.monotonic()
    while True:
        h = _fg_hwnd()
        if h and existing and h not in existing:
            return h
        if time.monotonic() - t0 >= timeout:
            return 0
        time.sleep(0.05)


def plan_press(keys: str, window: Window | None = None, run: Run | None = None, follows: bool = False) -> Plan | str:
    try:
        ks = parse_keys(keys)
    except ValueError as e:
        return f"{e}，没有执行。"
    if shell_shortcut(ks):
        opener = set(ks) in OPENERS
        comes_up = Window(title="弹出的窗口", pending=True) if opener else None  # Win+R's Run box: what follows goes there

        def go_shell():
            existing = _all_windows()
            if run is not None:
                run.windows = existing
            pyautogui.hotkey(*ks) if len(ks) > 1 else pyautogui.press(ks[0])
            if comes_up is not None:
                h = _new_foreground(existing)
                if h and not is_frontend(h):
                    comes_up.hwnd, comes_up.title = h, active_window() or comes_up.title  # so it can be brought back
            return f"已按 {'+'.join(ks)}。"
        # a shortcut that opens nothing leaves the steps after it on the window they were announced for
        return Plan("hotkey", f"按{spoken_keys(ks)}", "", go_shell, {"kind": "hotkey", "keys": list(ks), "window": ""},
                    opens=comes_up)
    w = window or current_window()
    if not w.pending and is_frontend(w.hwnd):
        return FRONTEND_REFUSAL

    def go():
        why = ready_window(w, run if follows else None) or replace_prompt_popped(w.title)
        if why:
            return why
        if run is not None:
            run.windows = _all_windows()
        if len(ks) == 1:
            pyautogui.press(ks[0])
        else:
            pyautogui.hotkey(*ks)
        return f"已按 {'+'.join(ks)}。"
    return Plan("hotkey", in_window(f"按{spoken_keys(ks)}", w.title), "", go,
                {"kind": "hotkey", "keys": list(ks), "window": w.title}, window=w)


def plan_scroll(amount: int, target: str, x: int | None = None, y: int | None = None) -> Plan | str:
    if amount == 0:
        return "amount 不能是 0。"
    what = f"在{target.strip()}往{'上' if amount > 0 else '下'}滚"

    def go():
        if x is not None and y is not None:
            pyautogui.moveTo(*to_screen(x, y))
        pyautogui.scroll(amount)
        return f"已{what} {abs(amount)} 格。"
    return Plan("scroll", what, "", go, {"kind": "scroll"})


def plan_open(name: str) -> Plan | str:
    table = apps()
    app = resolve_app(name, table)
    if app is None:
        names = "、".join(k for k in table if _CJK.search(k))
        return (f"“{name}”不在允许打开的程序清单里（清单：{names}），没有执行。"
                f"要加新程序请让用户在 config.toml 的 [desktop.apps] 里添加。")
    label, cmd = app
    opened = Window(title=label, pending=True)

    def go():
        before, existing, before_fg, before_title = _window_handles(), _all_windows(), _fg_hwnd(), active_window()
        _launch(cmd)
        time.sleep(1.0)
        _bring_front(before)
        time.sleep(0.2)
        _note_opened(opened, existing, before_fg, before_title, [label], cmd)
        return f"已打开{label}。现在的前台窗口：{active_window() or '（无）'}。输入文字前请确认这是对的窗口。"
    return Plan("open_app", f"打开{label}", f"命令：{cmd}", go, {"kind": "open_app", "name": label}, opened)


def _note_opened(opened: Window, existing: set[int], before_fg: int, before_title: str, names: list[str],
                 program: str = "") -> None:
    """The window an open step put in front, for the steps after it. A new window counts when its title has the full
    name of what was opened or its process is the program started (open_path: the file's open handler). A window
    that was there before counts only when the foreground changed to it or its title changed (a tab in a running
    Notepad, a reused VS Code window) AND its process is that program: a terminal whose title happens to change, or a
    stray popup, is not taken."""
    h, title = _fg_hwnd(), active_window()
    if not h or (h == before_fg and title == before_title):
        return
    stem = Path(program).stem.lower() if program and ":" not in program else ""
    same_program = bool(stem) and Path(_process_image(h)).stem.lower() == stem
    if existing and h in existing:
        ok = same_program
    else:
        t = title.lower()
        ok = same_program or any(n and n.lower() in t for n in names)
    if ok:
        opened.hwnd, opened.title = h, title or opened.title


# Programs never open through open_path: only the open_app allow-list starts programs. A shortcut counts, and so
# does a script whose type runs when opened (.py with the python.org installer); PATHEXT is added at call time.
EXECUTABLE_EXTS = {".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".ps1xml", ".psc1", ".vb", ".vbs", ".vbe",
                   ".js", ".jse", ".ws", ".wsf", ".wsh", ".wsc", ".sct", ".msi", ".msp", ".mst", ".lnk", ".scr", ".pif",
                   ".reg", ".inf", ".hta", ".chm", ".cpl", ".msc", ".url", ".scf", ".appx", ".appxbundle", ".msix",
                   ".msixbundle", ".appinstaller", ".application", ".appref-ms", ".gadget", ".settingcontent-ms",
                   ".jar", ".py", ".pyw", ".pyz", ".pyzw", ".pyc", ".pyo", ".xll", ".diagcab", ".sh", ".bash", ".zsh",
                   ".ahk", ".au3", ".pl", ".pm", ".rb", ".rbw", ".tcl", ".php", ".lua", ".vsto", ".xbap", ".rdp", ".mjs",
                   ".cjs", ".ps2", ".psc2", ".workflow", ".action", ".command", ".jnlp", ".msu"}
# Programs a file's open handler may be that run its content: shells, interpreters, script hosts, installers.
RUNNER_PROGRAMS = {"bash", "sh", "zsh", "wsl", "git-bash", "mintty", "cmd", "powershell", "pwsh", "wscript", "cscript", "mshta",
                   "python", "pythonw", "py", "pyw", "perl", "wperl", "ruby", "rubyw", "tclsh", "wish", "php", "php-win",
                   "lua", "node", "deno", "bun", "java", "javaw", "autohotkey", "autohotkey32", "autohotkey64",
                   "autohotkeyu32", "autohotkeyu64", "autoit3", "autoit3_x64", "msiexec", "regedit", "mmc", "hh",
                   "presentationhost", "mstsc", "wt", "conhost", "appinstaller", "javaws", "wusa"}


def _handler(suffix: str) -> str:
    """The program the extension's open verb runs (AssocQueryStringW, ASSOCSTR_EXECUTABLE), '' when unknown."""
    try:
        f = ctypes.WinDLL("shlwapi").AssocQueryStringW
        f.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        return buf.value if f(0, 2, suffix, "open", buf, ctypes.byref(n)) == 0 else ""
    except Exception:
        return ""


def is_program(p: Path) -> bool:
    """Would opening this file run it? Its extension is a known runnable type or in PATHEXT, or its open handler is a
    shell, interpreter or installer (Windows ignores trailing dots and spaces in a name)."""
    suffix = Path(p.name.rstrip(". ")).suffix.lower()
    if not suffix:
        return False
    pathext = {e.strip().lower() for e in os.environ.get("PATHEXT", "").split(";") if e.strip()}
    if suffix in (EXECUTABLE_EXTS | pathext):
        return True
    runner = Path(_handler(suffix)).stem.lower()
    base = re.sub(r"[\d.]+[a-z]?$", "", runner)  # python3.12 -> python, tclsh86t -> tclsh, lua54 -> lua
    return bool(runner) and (runner in RUNNER_PROGRAMS or base in RUNNER_PROGRAMS or "install" in runner or "setup" in runner)


def plan_open_path(path: str) -> Plan | str:
    """A folder or a document, with its default program: one request, spoken by its last name ("打开文件夹
    figures"), the full path in the window. Nothing is asked when the path does not exist or is a program."""
    raw = str(path or "").strip().strip('"')
    if not raw:
        return "没有给出路径，没有执行。"
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.exists():
        return f"路径不存在：{p}，没有执行。请先确认路径（可以用命令行列一下再试）。"
    if p.is_file() and is_program(p):
        return f"{p.name} 是程序或脚本，open_path 不开程序；程序只能用 open_app 从清单里打开，没有执行。"
    kind = "文件夹" if p.is_dir() else "文件"
    name = p.name or str(p)
    opened = Window(title=name, pending=True)

    def go():
        before, existing, before_fg, before_title = _window_handles(), _all_windows(), _fg_hwnd(), active_window()
        os.startfile(str(p))
        time.sleep(1.0)
        _bring_front(before)
        time.sleep(0.2)
        program = "explorer" if p.is_dir() else _handler(p.suffix.lower())
        _note_opened(opened, existing, before_fg, before_title, [name], program)
        return f"已打开{kind} {name}。现在的前台窗口：{active_window() or '（无）'}。"
    return Plan("open_app", f"打开{kind} {name}", f"路径：{p}", go, {"kind": "open_path", "path": str(p)}, opened)


def _run(plan: Plan | str, purpose: str = ""):
    """Gate, then do. The only way a Plan's go() is ever called outside run_steps."""
    if isinstance(plan, str):
        return plan
    ok, why = approved(plan.action, plan.what, purpose, plan.detail, plan.facts)
    if not ok:
        return why
    return _guard(plan.go)


# ---------- the tools ----------

@server.tool(structured_output=False)
def screenshot(purpose: str = ""):
    """截一张整个屏幕的图（缩小到宽 1280 像素）。之后 click / scroll 的坐标按这张图上的像素给。
    purpose：一句话说明目的（会念给用户听，可省略）。执行前会向用户口头申请。"""
    return _run(plan_screenshot(), purpose)


@server.tool()
def click(x: int, y: int, target: str, button: str = "left", double: bool = False, purpose: str = "") -> str:
    """点击屏幕。x, y 是最近一次截图上的像素坐标（截图是缩小过的，直接用图上的坐标）。
    target：用一句话说清点的是什么，比如“记事本的保存按钮”，会念给用户听（必填，光给坐标用户听不懂）。
    button：left 或 right。double：是否双击。purpose：一句话说明目的（可省略）。执行前会向用户口头申请。"""
    return _run(plan_click(x, y, target, button, double), purpose)


@server.tool()
def type_text(text: str, purpose: str = "") -> str:
    """在当前前台窗口的光标位置输入文字（中英文都行；换行写 \\n）。申请时会念出目标窗口的名字和完整内容，
    用户同意后才输入。注意：如果焦点在终端里，输入加回车就等于运行命令。purpose：一句话说明目的（可省略）。"""
    return _run(plan_type(text), purpose)


@server.tool()
def press_keys(keys: str, purpose: str = "") -> str:
    """按一个键或快捷键，比如 "enter"、"ctrl+s"、"alt+f4"、"win+r"。申请时会念出目标窗口的名字。
    purpose：一句话说明目的（可省略）。执行前会向用户口头申请。"""
    return _run(plan_press(keys), purpose)


@server.tool()
def scroll(amount: int, target: str, x: int | None = None, y: int | None = None, purpose: str = "") -> str:
    """滚动鼠标滚轮。amount 正数向上、负数向下（一格约三行，一般 5 到 10）。target：说清在哪里滚，比如“浏览器页面”，
    会念给用户听。x, y 可选，是最近一次截图上的位置，不给就在鼠标当前位置滚。执行前会向用户口头申请。"""
    return _run(plan_scroll(amount, target, x, y), purpose)


@server.tool()
def open_app(name: str, purpose: str = "") -> str:
    """打开一个程序。name 用程序的名字：记事本、计算器、画图、文件管理器、浏览器、终端、设置 等
    （清单在 config.toml 的 [desktop.apps] 里，用户可以加）。purpose：一句话说明目的（可省略）。执行前会向用户口头申请。"""
    return _run(plan_open(name), purpose)


@server.tool()
def open_path(path: str, purpose: str = "") -> str:
    """打开一个文件夹或文档（用系统默认程序），path 是完整路径。一次申请：念的是"打开文件夹 XX / 打开文件 XX"，完整路径显示在窗口里。
    打开文件夹、打开某个文档一律用这个，不要用 open_app 文件管理器 + 按键 + 输入路径那样分三步。
    程序和脚本（.exe .bat .cmd .ps1 .py .lnk 等）不能用它打开，程序请用 open_app；路径不存在会直接告诉你，不申请。
    purpose：一句话说明目的（可省略）。"""
    return _run(plan_open_path(path), purpose)


_PLANNERS = {"open_app": plan_open, "open_path": plan_open_path, "type_text": plan_type, "press_keys": plan_press,
             "click": plan_click, "scroll": plan_scroll}


@server.tool()
def run_steps(steps: list[dict], purpose: str = "") -> str:
    """把几步连起来做，只向用户申请一次（用户在权限表里允许“一次批准一串步骤”时；否则自动退回每步都问）。
    一件事要两步以上的桌面操作，就用它一次交上去，不要一步一步分别申请；只有中间必须截图看结果才拆开。
    steps：列表，每项 {"tool": 工具名, "args": {该工具的参数}}，工具名可以是 open_app、open_path、type_text、press_keys、click、scroll
    （screenshot 不能放在串里）。例：[{"tool":"open_app","args":{"name":"记事本"}},{"tool":"type_text","args":{"text":"hello"}}]。
    purpose：一句话说"要做的事"（如"打开 figures 文件夹"），申请时念的是它，按键细节显示在窗口里。"""
    if not steps:
        return "steps 是空的。"
    plans: list[Plan] = []
    run = Run()
    window: Window | None = None  # where the typing / keys steps go: what the latest open step brings up, or the first's
    prev = ""
    for i, step in enumerate(steps, 1):
        tool = str(step.get("tool", "")).strip()
        args = {k: v for k, v in dict(step.get("args") or {}).items() if k not in ("window", "run", "follows")}
        if tool not in _PLANNERS:
            return f"第 {i} 步的工具 {tool!r} 不能放在一串步骤里（可以用：{', '.join(_PLANNERS)}），没有执行。"
        if tool in ("type_text", "press_keys"):
            args.update(window=window, run=run, follows=prev in ("click", "press_keys"))
        elif tool == "click":
            args["run"] = run
        try:
            plan = _PLANNERS[tool](**args)
        except TypeError as e:
            return f"第 {i} 步（{tool}）的参数不对：{e}，没有执行。"
        if isinstance(plan, str):
            return f"第 {i} 步：{plan}"
        if plan.opens is not None:
            window = plan.opens
        elif plan.window is not None:
            window = plan.window
        prev = tool
        plans.append(plan)
    q = question(join_steps([p.what for p in plans]), purpose)
    detail = "\n".join(f"{i}. {p.what}{'：' + p.detail if p.detail else ''}" for i, p in enumerate(plans, 1))
    d = _post({"action": "batch", "actions": [p.action for p in plans], "question": q, "detail": detail,
               "facts": [p.facts for p in plans]})
    log.info("ask batch %r -> %s", q, json.dumps(d, ensure_ascii=False)[:200])
    if d.get("per_step"):  # the user wants every step asked separately
        run.batch = False  # an approval between steps may move the focus: no "whatever came up" for the next step
        out = []
        for i, p in enumerate(plans, 1):
            ok, why = approved(p.action, p.what, "", p.detail, p.facts)
            r = _guard(p.go) if ok else why
            out.append(f"{i}. {r}")
            if not ok or _stops(r):  # a no (the brake answers no too), a refusal, a failure or the fail-safe
                if i < len(plans):
                    out.append("后面的步骤没有做。")
                break
        return "\n".join(out)
    if not d.get("ok"):
        return str(d.get("message") or "用户没有同意，没有执行。不要再试同一串动作，口头说一句就好。")
    out = []
    for i, p in enumerate(plans, 1):
        if braked(d.get("brake")):
            out.append(f"{i}. {NOT_DONE}用户按了紧急停止，后面的步骤没有做。")
            break
        r = _guard(p.go)
        out.append(f"{i}. {r}")
        if _stops(r):
            out.append("后面的步骤没有做。")
            break
    return "\n".join(out)


def _stops(r) -> bool:
    return not isinstance(r, str) or r.startswith(("动作失败", "用户把鼠标", NOT_DONE))


def braked(gen) -> bool:
    """Was the emergency brake pulled since the run was approved (the brake count in the approval's reply)? Asked
    before every step; a service that cannot be reached counts as stopped."""
    if gen is None:
        return False  # the service does not report it
    d = _post({}, "brake")
    return not d.get("ok") or d.get("brake") != gen


# ---------- entry point ----------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr),
                                  logging.FileHandler(ROOT / "desktop.log", encoding="utf-8")])
    if "--list" in sys.argv:
        for k, v in apps().items():
            print(f"{k} -> {v}")
        return
    log.info("desktop MCP server starting; app port %r; screen %s; scale %.2f", PORT, pyautogui.size(), shot_scale())
    server.run("stdio")


if __name__ == "__main__":
    main()
