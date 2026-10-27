"""Window theme for voice-claude (docs/architecture.md, "The window"): colour tokens, the five-step font
ladder, sizes derived from the font size, the status table, hint texts and the little pure helpers the window
needs. Everything that needs no Tk lives at module level so the unit tests can import it without a display."""
from __future__ import annotations

import re
from dataclasses import dataclass

FONT = "Microsoft YaHei UI"
CODE_FONT = "Consolas"

# ---------- colours (light theme; a dark theme would redefine the same names) ----------
COLORS = {
    "bg_base": "#F3F3F3", "bg_card": "#FFFFFF",
    "bg_base_hover": "#E9E9E9", "bg_base_pressed": "#DEDEDE",
    "bg_card_hover": "#F5F5F5", "bg_card_pressed": "#EBEBEB",
    "bg_selected": "#EAF2FB",
    "stroke_card": "#E0E0E0", "stroke_control": "#D6D6D6", "stroke_divider": "#EBEBEB",
    "text_primary": "#1A1A1A", "text_secondary": "#5D5D5D", "text_tertiary": "#8C8C8C", "text_disabled": "#A3A3A3",
    "text_on_dark": "#FFFFFF",
    "accent": "#005FB8", "accent_hover": "#1A6FC4", "accent_pressed": "#004F99",
    "state_listen": "#0F7B0F", "band_listen": "#CDEFC8",
    "state_ask": "#9D5D00", "band_ask": "#9D5D00", "ask_bg": "#FFF4CE", "ask_border": "#E9C46A", "ask_soft_bg": "#FAF6E6",
    "state_error": "#C42B1C", "band_error": "#FDE7E9", "error_border": "#F1B8B8",
    "badge_neutral": "#E6E6E6", "btn_mute_fill": "#5D5D5D",
}

# ---------- tool names -> plain words for the status bar ----------
_TOOL_EXACT = {
    "bash": "正在跑命令…", "read": "正在看文件…",
    "edit": "正在改文件…", "write": "正在改文件…", "multiedit": "正在改文件…", "notebookedit": "正在改文件…",
    "grep": "正在找文件…", "glob": "正在找文件…", "ls": "正在找文件…",
    "webfetch": "正在查网页…", "websearch": "正在查网页…",
    "agent": "正在派活…", "task": "正在派活…",
}


_DESKTOP_WORDS = ("desktop", "computer", "screenshot", "click", "type", "hotkey", "scroll", "open_app")


def is_desktop_tool(name: str) -> bool:
    """A tool that looks at or works the screen (the window gets out of its way)."""
    low = (name or "").strip().lower()
    return low not in _TOOL_EXACT and any(k in low for k in _DESKTOP_WORDS)


def tool_word(name: str) -> str:
    """What the status bar says while Claude uses a tool (never the tool's English name)."""
    n = (name or "").strip()
    low = n.lower()
    if low in _TOOL_EXACT:
        return _TOOL_EXACT[low]
    if is_desktop_tool(low):
        return "正在操作桌面…"
    if "voiceclaude" in low or "manager" in low or "setting" in low or "permission" in low:
        return "正在改设置…"
    return "正在干活…"


# ---------- status words -> how the bar looks ----------
# key: (orb kind, text colour token, band kind). Band "mic" = green when the microphone is really open
# (listening / recording / barge-in busy), grey otherwise; the window decides that with conversation + muted.
STATES = {
    "starting": ("spinner_gray", "text_secondary", "gray"),
    "idle": ("ring", "text_secondary", "gray"),
    "recording": ("bars_boxed", "state_listen", "green"),
    "listening": ("bars", "state_listen", "green"),
    "transcribing": ("spinner", "text_primary", "mic"),
    "thinking": ("spinner", "text_primary", "mic"),
    "tool": ("spinner", "text_primary", "mic"),
    "speaking": ("breath", "accent", "mic"),
    "you_talking": ("bars_ring", "state_listen", "green"),
    "you_stopped": ("bars_ring", "state_listen", "green"),
    "waiting": ("question", "text_on_dark", "ask"),
    "disconnected": ("bang", "state_error", "error"),
    "failed": ("bang", "state_error", "error"),
    "closed": ("ring_dash", "text_secondary", "gray"),
    "estopped": ("stopsq", "state_error", "error"),
}

BAND_COLORS = {"gray": ("bg_base", None), "green": ("band_listen", "state_listen"),
               "ask": ("band_ask", None), "error": ("band_error", "state_error")}


def classify_status(text: str) -> str:
    """Status text (the `_status` argument) -> a key of STATES."""
    t = text or ""
    if t.startswith("已紧急停止"):
        return "estopped"
    if t.startswith("后台服务已关掉"):
        return "closed"
    if t.startswith("后台服务断开"):
        return "disconnected"
    if t.startswith("启动失败"):
        return "failed"
    if "等你回答" in t or t.endswith("在等你"):
        return "waiting"
    if t.startswith("你在说"):
        return "you_talking"
    if t.startswith("停了"):
        return "you_stopped"
    if t.startswith("录音中"):
        return "recording"
    if t.startswith("在听"):
        return "listening"
    if t.startswith("在说"):
        return "speaking"
    if t.startswith("识别中"):
        return "transcribing"
    if t.startswith("思考中"):
        return "thinking"
    if t.startswith("空闲"):
        return "idle"
    if t.startswith("启动中") or t.startswith("正在加载") or t.startswith("正在连接"):
        return "starting"
    if t.startswith("正在"):
        return "tool"
    return "idle"


def band_for(key: str, mic_open: bool, muted: bool) -> str:
    """Which band colour a status gets. mic_open = continuous conversation with barge-in (the mic is watched
    while Claude works); muted turns every green band grey, the amber and pink ones stay."""
    orb, color, band = STATES.get(key, STATES["idle"])
    if band == "mic":
        band = "green" if mic_open else "gray"
    if band == "green" and muted:
        band = "gray"
    return band


def status_display(text: str, muted: bool) -> str:
    """The status words while muted: listening / recording become 已静音, the others get a suffix."""
    if not muted:
        return text
    key = classify_status(text)
    if key in ("listening", "recording", "you_talking", "you_stopped"):
        return "已静音"
    if key in ("starting", "disconnected", "failed", "closed", "estopped"):
        return text
    return f"{text} · 已静音"


# ---------- the hint row ----------
@dataclass(frozen=True)
class Hint:
    text: str
    strong: str = ""  # "" = small grey; "listen" / "ask" = bold, one step below the body size, in that colour


def hint_for(situation: str, key: str, send_key: str, hold: float) -> Hint:
    """situation: starting / idle / recording / listening / busy_conv / busy_queue / busy_single / ask / question /
    ask_muted / muted / disconnected / closed."""
    send = send_key or "静音按钮"
    hold_s = f"{hold:.1f}".rstrip("0").rstrip(".")
    table = {
        "starting": Hint("正在准备，请稍等"),
        "idle": Hint(f"{key} 单按 说一段 · 双按 开始对话 · {send} 静音"),
        "recording": Hint(f"{key} 再按一下 发送 · 按住 {hold_s} 秒 紧急停止", "listen"),
        "listening": Hint(f"说完停一下会自动发 · {key} 单按 马上发 · 双按 结束对话"),
        "busy_conv": Hint(f"开口的话先排队 · {key} 打断 · 按住 {hold_s} 秒 紧急停止"),
        "busy_queue": Hint(f"{key} 撤销排队 · {send} 双按 马上发 · 按住 {hold_s} 秒 紧急停止"),
        "busy_single": Hint(f"{key} 打断 · 按住 {hold_s} 秒 紧急停止"),
        "ask": Hint(f"说「可以」或「不行」· {send_key + ' 可以 · ' if send_key else ''}{key} 不行", "ask"),
        "question": Hint("直接说就行，整句话它都会听", "ask"),
        "ask_muted": Hint(f"已静音，说话它听不到 · {send_key + ' 可以 · ' if send_key else ''}{key} 不行", "ask"),
        "muted": Hint(f"{send} 单按 恢复麦克风"),
        "disconnected": Hint("请关掉程序再打开"),
        "closed": Hint("要再用，关掉这个窗口再打开"),
    }
    return table.get(situation, table["idle"])


def fit_hint(text: str, measure, max_px: int) -> str:
    """Drop segments from the end (split on ' · ') until the hint fits on one line; never wrap."""
    parts = text.split(" · ")
    while len(parts) > 1 and measure(" · ".join(parts)) > max_px:
        parts.pop()
    return " · ".join(parts)


# ---------- one-line tool rows, chip names ----------
def truncate_to_width(text: str, measure, max_px: int, ellipsis: str = "…") -> str:
    """Longest prefix of text (plus an ellipsis) whose measured width fits max_px."""
    if max_px <= 0 or measure(text) <= max_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid] + ellipsis) <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ellipsis


def ellipsize(name: str, limit: int = 12) -> str:
    return name if len(name) <= limit else name[:limit] + "…"


def is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


def wrap_mode(s: str, measure, col_px: int) -> str:
    """'word' or 'char' for a paragraph. Tk (8.6) breaks word-wrapped lines only at spaces, so a run of Chinese
    characters is one word: a long run gets pushed whole onto the next line (a label left alone on its line), and
    a run wider than the line falls back to character breaks anyway. Character wrap, on the other hand, cuts the
    English words inside a Chinese sentence in half ('exo / n'). So: word wrap unless the widest space-delimited
    token is wider than half the column (a Chinese sentence without spaces), in which case character wrap."""
    tokens = s.split()
    if not tokens:
        return "char"
    limit = max(1, col_px // 2)
    widest = max(measure(tok) for tok in tokens)
    return "char" if widest > limit else "word"


# ---------- request-card titles, parsed from the spoken question (the card is built from the note the voice side
# writes, which carries the question but not the daemon's action name, so the category is read off the words) ----------
_BG_PREFIX = re.compile(r"^后台的(.*?)申请：")

_CATEGORY_RULES = (
    ("在后台运行", "后台运行命令"),
    ("后台任务", "停后台任务"),
    ("模型切换", "切换模型"),
    ("权限改成", "改权限"),
    ("要接到", "转接"),
    ("，然后", "一次批准一串步骤"),
    ("屏幕", "看屏幕"), ("截图", "看屏幕"), ("截屏", "看屏幕"),
    ("输入", "输入文字"),
    ("快捷键", "按快捷键"), ("按键", "按快捷键"), ("按下", "按快捷键"),
    ("滚", "滚动"),
    ("点一下", "点击"), ("双击", "点击"), ("点击", "点击"),
    ("打开", "打开程序"),
)


def split_background(question: str) -> tuple[str, str]:
    """'后台的Atlas申请：我想…' -> ('Atlas', '我想…'); other questions -> ('', question)."""
    m = _BG_PREFIX.match(question or "")
    if m:
        return m.group(1).strip(), question[m.end():].strip()
    return "", question or ""


def ask_category(question: str) -> str:
    q = question or ""
    for needle, word in _CATEGORY_RULES:
        if needle in q:
            return word
    return ""


def ask_title(question: str, kind: str = "approve") -> tuple[str, str, str]:
    """-> (title, line name of a background line or '', question without the background prefix)."""
    line, body = split_background(question)
    if kind == "question":
        return "问你 · 总机", line, body
    cat = ask_category(body)
    if line:
        return f"后台的 {line} 申请" + (f" · {cat}" if cat else ""), line, body
    return "申请" + (f" · {cat}" if cat else ""), line, body


def heard_suffix(heard: str, said: str) -> str:
    """'可以' heard as '行吧可以的' -> '（听到：行吧可以的）'; nothing when the words were the plain answer."""
    h = (heard or "").strip("。，,. ！!？?")
    if not h or h == said:
        return ""
    return f"（听到：{h}）"


# ---------- line and job words ----------
STATE_WORDS = {"starting": "启动中", "idle": "空闲", "working": "在干活", "waiting": "在等你", "dead": "已退出"}
JOB_WORDS = {"running": "还在跑", "done": "做完了", "failed": "失败了", "cancelled": "已取消"}
JOB_MARKS = {"running": "spin", "done": "check", "failed": "bang", "cancelled": "dash"}

_JOB_NOTE = re.compile(r"^后台任务\s+(\S+)：(\w+)\s*(.*)$", re.S)


def parse_job_note(note: str) -> tuple[str, str, str] | None:
    """'后台任务 8a2f：running 生成图表' -> ('8a2f', 'running', '生成图表')."""
    m = _JOB_NOTE.match(note or "")
    return (m.group(1), m.group(2), m.group(3).strip()) if m else None


def job_row_text(job: dict, now: float) -> str:
    what = job.get("note") or (job.get("command") or "")[:40] or job.get("id", "")
    state = job.get("state", "running")
    if state == "running":
        minutes = max(0, int((now - job.get("started", now)) // 60))
        return f"{what} · 还在跑 {minutes} 分" if minutes else f"{what} · 还在跑"
    return f"{what} · {JOB_WORDS.get(state, state)}"


def permissions_summary(table: dict, transparent: bool = False) -> str:
    ask = sum(1 for v in table.values() if v == "ask")
    allow = sum(1 for v in table.values() if v == "allow")
    deny = sum(1 for v in table.values() if v == "deny")
    if transparent:  # 询问 means 允许 then, except deleting and overwriting
        return f"通透模式 · 禁止 {deny}" if deny else "通透模式"
    return f"询问 {ask} · 允许 {allow} · 禁止 {deny}"


# The permission table as the window shows it. config.PERMISSION_LABELS /
# PERMISSION_VALUES stay the words the lines speak; these are display names only, and every one of them is a word
# the spoken permission change understands (config.normalize_permission_value accepts 询问 / 允许 / 禁止).
PERMISSION_NAMES = {"screenshot": "查看屏幕", "click": "点击", "type": "输入文字", "hotkey": "按快捷键", "scroll": "滚动",
                    "open_app": "打开应用", "switch_model": "切换模型", "batch": "批量批准", "other_connectors": "其他连接器",
                    "background_job": "后台运行命令", "switch_provider": "切换服务商",
                    "codex_shell": "Codex 沙箱外命令", "codex_edit": "Codex 改工作区外文件",
                    "send_to_session": "发给其他 Claude 会话"}
PERMISSION_WORDS = {"ask": "询问", "allow": "允许", "deny": "禁止"}


# ---------- Tk-side: fonts, sizes, ttk style ----------
class Theme:
    """Holds the scale, the nine fonts and every size derived from the font size S. Create after tk.Tk()."""

    def __init__(self, root, size: int):
        import tkinter.font as tkfont
        self.root = root
        self.scale = root.winfo_fpixels("1i") / 96.0  # Tk already scales point sizes; only pixel constants need this
        self.size = size
        self.fonts = {
            "status": tkfont.Font(root=root, family=FONT, size=size + 3, weight="bold"),
            "sub": tkfont.Font(root=root, family=FONT, size=size + 1, weight="bold"),
            "body": tkfont.Font(root=root, family=FONT, size=size),
            "body_bold": tkfont.Font(root=root, family=FONT, size=size, weight="bold"),
            "ctrl": tkfont.Font(root=root, family=FONT, size=size - 1),
            "ctrl_bold": tkfont.Font(root=root, family=FONT, size=size - 1, weight="bold"),
            "cap": tkfont.Font(root=root, family=FONT, size=max(8, size - 2)),
            "cap_bold": tkfont.Font(root=root, family=FONT, size=max(8, size - 2), weight="bold"),
            "code": tkfont.Font(root=root, family=CODE_FONT, size=size - 1),
            "link": tkfont.Font(root=root, family=FONT, size=size - 1, underline=1),  # ctrl, underlined (hovered link)
        }
        self.set_size(size)

    def px(self, n: float) -> int:
        """Logical pixels -> physical. 1 px lines stay 1 px."""
        if n == 0:
            return 0
        return max(1, round(n * self.scale)) if n > 0 else -max(1, round(-n * self.scale))

    def font(self, name: str):
        return self.fonts[name]

    def set_size(self, size: int) -> None:
        self.size = size
        f = self.fonts
        f["status"].configure(size=size + 3)
        f["sub"].configure(size=size + 1)
        f["body"].configure(size=size)
        f["body_bold"].configure(size=size)
        f["ctrl"].configure(size=size - 1)
        f["ctrl_bold"].configure(size=size - 1)
        f["cap"].configure(size=max(8, size - 2))
        f["cap_bold"].configure(size=max(8, size - 2))
        f["code"].configure(size=size - 1)
        f["link"].configure(size=size - 1)
        px = self.px
        ctrl_line = f["ctrl"].metrics("linespace")
        self.orb = px(round(2.3 * size))
        self.bar_h = max(px(56), f["status"].metrics("linespace") + px(24))
        self.control_h = max(px(32), ctrl_line + px(12))
        self.chip_h = self.control_h - px(4)
        self.row_h = self.control_h + px(4)
        self.hint_h = max(px(24), ctrl_line + px(4))
        self.col_max = 38 * f["body"].measure("汉")
        self.min_w = px(30 * size)
        self.min_h = px(35 * size)
        self.body_line = f["body"].metrics("linespace")

    def apply_ttk_style(self) -> None:
        """vista stays (native Combobox / Checkbutton); only the scrollbar borrows clam's flat parts."""
        from tkinter import ttk
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        try:
            style.element_create("Flat.Vertical.Scrollbar.trough", "from", "clam")
            style.element_create("Flat.Vertical.Scrollbar.thumb", "from", "clam")
        except Exception:
            pass  # already created (font change re-applies the style)
        style.layout("Flat.Vertical.TScrollbar",
                     [("Flat.Vertical.Scrollbar.trough",
                       {"children": [("Flat.Vertical.Scrollbar.thumb", {"expand": 1})], "sticky": "ns"})])
        style.configure("Flat.Vertical.TScrollbar", troughcolor=COLORS["bg_card"], background=COLORS["stroke_control"],
                        bordercolor=COLORS["bg_card"], lightcolor=COLORS["bg_card"], darkcolor=COLORS["bg_card"],
                        arrowsize=0, relief="flat", width=self.px(10))
        style.map("Flat.Vertical.TScrollbar", background=[("active", COLORS["text_tertiary"])])
        style.configure("Card.TCheckbutton", background=COLORS["bg_card"], font=self.fonts["ctrl"],
                        foreground=COLORS["text_primary"])
        style.configure("Card.TCombobox", font=self.fonts["ctrl"], arrowsize=self.px(14))
        self.root.option_add("*TCombobox*Listbox.font", self.fonts["ctrl"])
        self.root.option_add("*TCombobox*Listbox.background", COLORS["bg_card"])
