"""Hand-drawn parts of the window (docs/architecture.md, "The window"): the state orb, the line chip and panel,
the docked request card, the answered / error card, the segmented permission button, the back-to-latest button
and the flat tk.Button styling. Pillow renders the round shapes at 4x and shrinks them (tk's Canvas has no
anti-aliasing); straight bars are plain Canvas rectangles."""
from __future__ import annotations

import ctypes
import math
import os
import time
import tkinter as tk
from typing import Callable

from PIL import Image, ImageDraw, ImageFont, ImageTk

from .ui_theme import COLORS, JOB_MARKS, STATE_WORDS, Theme, ellipsize, is_ascii, job_row_text, truncate_to_width, wrap_mode

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}


def _bold_font(size: int):
    if size not in _FONT_CACHE:
        try:
            _FONT_CACHE[size] = ImageFont.truetype("arialbd.ttf", size)
        except OSError:
            try:
                _FONT_CACHE[size] = ImageFont.load_default(size)
            except TypeError:
                _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _shrink(img: Image.Image, size: tuple[int, int], master=None) -> ImageTk.PhotoImage:
    return ImageTk.PhotoImage(img.resize(size, Image.LANCZOS), master=master)


# ---------- small PIL marks (panel dots, job icons, mute badge) ----------
_MARKS: dict[tuple, ImageTk.PhotoImage] = {}


def make_mark(kind: str, size: int, bg: str, color: str, master=None) -> ImageTk.PhotoImage:
    """kind: dot / hollow / home / spin / check / bang / dash. Cached per Tk interpreter and (kind, size, bg, colour)."""
    key = (id(master.tk) if master is not None else 0, kind, size, bg, color)
    if key in _MARKS:
        return _MARKS[key]
    s = size * 4
    img = Image.new("RGB", (s, s), _hex(bg))
    d = ImageDraw.Draw(img)
    w = max(4, s // 8)
    pad = s // 6
    if kind == "dot":
        d.ellipse((pad, pad, s - pad, s - pad), fill=_hex(color))
    elif kind == "hollow":
        d.ellipse((pad, pad, s - pad, s - pad), outline=_hex(color), width=w)
    elif kind == "home":
        d.polygon([(s // 2, pad), (s - pad, s // 2), (pad, s // 2)], fill=_hex(color))
        d.rectangle((pad + w, s // 2, s - pad - w, s - pad), fill=_hex(color))
        d.rectangle((s // 2 - w, s - pad - 3 * w, s // 2 + w, s - pad), fill=_hex(bg))
    elif kind == "spin":
        d.arc((pad, pad, s - pad, s - pad), start=300, end=200, fill=_hex(color), width=w)
    elif kind == "check":
        d.line([(pad, s // 2), (s // 2 - w, s - pad - w), (s - pad, pad + w)], fill=_hex(color), width=w, joint="curve")
    elif kind == "bang":
        d.rectangle((s // 2 - w // 2, pad, s // 2 + w // 2, s - pad - 3 * w), fill=_hex(color))
        d.ellipse((s // 2 - w, s - pad - 2 * w, s // 2 + w, s - pad), fill=_hex(color))
    elif kind == "dash":
        d.rectangle((pad, s // 2 - w // 2, s - pad, s // 2 + w // 2), fill=_hex(color))
    _MARKS[key] = ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS), master=master)
    return _MARKS[key]


# ---------- the state orb ----------
class StateOrb(tk.Canvas):
    """One Canvas: a pre-rendered image for round shapes, rectangles for the bars. set() changes what it shows;
    set_level() feeds the microphone level; the animation loop runs by itself and sleeps while minimised."""

    SPIN_FRAMES, BREATH_FRAMES = 12, 16

    def __init__(self, parent, theme: Theme, bg: str):
        self.theme = theme
        self.d = theme.orb
        self.extra = theme.px(16)  # room for the barge-in bars to the right
        super().__init__(parent, width=self.d + self.extra, height=self.d, bg=bg, highlightthickness=0, bd=0)
        self.kind = "ring"
        self.bg_color = bg
        self.muted = False
        self.barge = False
        self.level = 0.0
        self.shown = 0.0
        self.frame = 0
        self._cache: dict[tuple, list[ImageTk.PhotoImage]] = {}
        self._keep = None
        self._draw()
        self._after_id = self.after(80, self._tick)
        self.bind("<Destroy>", self._stop, add="+")

    def _stop(self, event=None):
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    # public
    def set(self, kind: str, bg: str, muted: bool = False, barge: bool = False) -> None:
        if (kind, bg, muted, barge) == (self.kind, self.bg_color, self.muted, self.barge):
            return
        self.kind, self.bg_color, self.muted, self.barge = kind, bg, muted, barge
        self.configure(bg=bg)
        self._draw()

    def set_level(self, rms: float) -> None:
        self.level = rms

    def resize(self) -> None:
        self.d = self.theme.orb
        self.extra = self.theme.px(16)
        self.configure(width=self.d + self.extra, height=self.d)
        self._draw()

    # animation
    def _tick(self):
        try:
            iconic = self.winfo_toplevel().state() == "iconic"
        except tk.TclError:
            return
        if not iconic:
            animated = self.kind in ("spinner", "spinner_gray", "breath")
            bars = self.kind in ("bars", "bars_boxed", "bars_ring") or self.barge
            if animated:
                self.frame += 1
            if bars:
                self.shown = max(self.level, self.shown * 0.6)
            if animated or bars:
                self._draw()
        self._after_id = self.after(80, self._tick)

    # drawing
    def _draw(self):
        self.delete("all")
        d, px = self.d, self.theme.px
        kind = self.kind
        if kind in ("bars", "bars_boxed", "bars_ring"):
            if kind == "bars_ring":
                self.create_image(0, 0, anchor="nw", image=self._frames("ring_soft")[0])
            self._draw_bars(0, d, five=True)
            if kind == "bars_boxed":
                w = px(2)
                self.create_rectangle(w // 2, w // 2, d - w // 2, d - w // 2, outline=COLORS["state_listen"], width=w)
            if self.muted:
                self.create_image(0, 0, anchor="nw", image=self._frames("badge_only")[0])
        else:
            frames = self._frames(kind)
            n = len(frames)
            self.create_image(0, 0, anchor="nw", image=frames[self.frame % n])
        if self.barge and kind not in ("bars", "bars_boxed", "bars_ring"):
            self._draw_bars(d + px(4), d, five=False)

    def _tier(self) -> int:
        if self.muted:
            return 0
        lv = self.shown
        for i, t in enumerate((0.004, 0.012, 0.03, 0.07), start=1):
            if lv < t:
                return i - 1
        return 4

    def _draw_bars(self, x0: int, d: int, five: bool):
        px = self.theme.px
        tier = self._tier()
        color = COLORS["text_disabled"] if self.muted else COLORS["state_listen"]
        if five:
            bw, gap = px(3), px(2)
            heights = [4 + 2 * tier, 4 + 3 * tier, 4 + 4 * tier, 4 + 3 * tier, 4 + 2 * tier]
            total = 5 * bw + 4 * gap
            left = x0 + (d - total) // 2
        else:
            bw, gap = px(2), px(2)
            heights = [4 + 1.5 * tier, 4 + 1.75 * tier, 4 + 1.5 * tier]
            left = x0
        base = d - (d - px(20)) // 2 if five else d - px(4)
        for i, h in enumerate(heights):
            hp = px(h)
            x = left + i * (bw + gap)
            self.create_rectangle(x, base - hp, x + bw, base, fill=color, outline="")

    def _frames(self, kind: str) -> list[ImageTk.PhotoImage]:
        key = (kind, self.d, self.bg_color, self.muted)
        if key not in self._cache:
            n = self.SPIN_FRAMES if kind.startswith("spinner") else self.BREATH_FRAMES if kind == "breath" else 1
            self._cache[key] = [self._render(kind, i, n) for i in range(n)]
            if len(self._cache) > 40:
                for k in list(self._cache)[:-20]:
                    del self._cache[k]
        return self._cache[key]

    def _render(self, kind: str, i: int, n: int) -> ImageTk.PhotoImage:
        d = self.d
        S = d * 4
        unit = S / 28.0  # the numbers below are for a 28 px orb and scale with it
        img = Image.new("RGB", (S, S), _hex(self.bg_color))
        dr = ImageDraw.Draw(img)
        stroke = max(4, int(2 * unit))
        box = (stroke, stroke, S - stroke, S - stroke)
        gray = _hex(COLORS["text_tertiary"])
        if kind == "ring":
            dr.ellipse(box, outline=gray, width=stroke)
        elif kind == "ring_soft":
            dr.ellipse(box, outline=_hex(COLORS["stroke_control"]), width=stroke)
        elif kind == "ring_dash":
            dr.ellipse(box, outline=gray, width=stroke)
            dr.rectangle((S * 0.3, S / 2 - stroke / 2, S * 0.7, S / 2 + stroke / 2), fill=gray)
        elif kind in ("spinner", "spinner_gray"):
            dr.ellipse(box, outline=_hex(COLORS["stroke_card"] if self.bg_color == COLORS["bg_base"] else COLORS["stroke_control"]), width=stroke)
            color = _hex(COLORS["accent"]) if kind == "spinner" else gray
            start = (i * 360 / n) - 90
            dr.arc(box, start=start, end=start + 100, fill=color, width=stroke)
        elif kind == "breath":
            r = (11 + 2 * math.sin(2 * math.pi * i / n - math.pi / 2)) * unit
            c = S / 2
            dr.ellipse((c - r, c - r, c + r, c + r), fill=_hex(COLORS["accent"]))
        elif kind == "question":
            m = 1.5 * unit
            dr.rounded_rectangle((m, m, S - m, S - m), radius=5 * unit, fill=(255, 255, 255))
            dr.text((S / 2, S / 2 + unit * 0.5), "?", fill=_hex(COLORS["state_ask"]), font=_bold_font(int(18 * unit)), anchor="mm")
        elif kind == "bang":
            dr.ellipse(box, fill=_hex(COLORS["state_error"]))
            dr.text((S / 2, S / 2), "!", fill=(255, 255, 255), font=_bold_font(int(18 * unit)), anchor="mm")
        elif kind == "stopsq":
            dr.ellipse(box, fill=_hex(COLORS["state_error"]))
            q = 8 * unit
            dr.rectangle((S / 2 - q / 2, S / 2 - q / 2, S / 2 + q / 2, S / 2 + q / 2), fill=(255, 255, 255))
        elif kind == "badge_only":
            pass
        if self.muted or kind == "badge_only":
            br = 7 * unit
            cx = cy = S - br - unit * 0.5
            dr.ellipse((cx - br, cy - br, cx + br, cy + br), fill=_hex(COLORS["btn_mute_fill"]))
            k = br * 0.55
            dr.line((cx - k, cy + k, cx + k, cy - k), fill=(255, 255, 255), width=max(4, int(1.8 * unit)))
        if kind == "badge_only":
            # only the badge should be opaque; everything else is the band colour already (RGB), so nothing to mask
            pass
        return _shrink(img, (d, d), self)


# ---------- flat buttons (classic tk.Button: colours can change, focus and keys come for free) ----------
_BUTTON_KINDS = {
    "primary": (COLORS["accent"], "#FFFFFF", COLORS["accent"], COLORS["accent_hover"], COLORS["accent_pressed"]),
    "secondary": (COLORS["bg_card"], COLORS["text_primary"], COLORS["stroke_control"], COLORS["bg_card_hover"], COLORS["bg_card_pressed"]),
    "danger": (COLORS["bg_card"], COLORS["state_error"], COLORS["state_error"], "#FFF5F5", "#FFECEC"),
    "mute": (COLORS["btn_mute_fill"], "#FFFFFF", COLORS["btn_mute_fill"], "#4A4A4A", "#3A3A3A"),
    "disabled": (COLORS["bg_card"], COLORS["text_disabled"], COLORS["stroke_control"], COLORS["bg_card"], COLORS["bg_card"]),
    # ghost: the page header's back arrow (no visible border) and the bar's 设置 while the settings area is open
    "ghost": (COLORS["bg_card"], COLORS["text_primary"], COLORS["bg_card"], COLORS["bg_card_hover"], COLORS["bg_card_pressed"]),
    "selected": (COLORS["bg_selected"], COLORS["text_primary"], COLORS["accent"], "#DCEAF8", "#CCE0F5"),
}


def flat_button(parent, theme: Theme, text: str, command, kind: str = "secondary", padx: int = 14) -> tk.Button:
    b = tk.Button(parent, text=text, command=command, relief="flat", bd=0, cursor="hand2", font=theme.font("ctrl"),
                  padx=theme.px(padx), pady=0, highlightthickness=theme.px(2), takefocus=1)
    b._frame = tk.Frame(parent, bd=0)  # the 1 px border around white buttons (tk.Button cannot draw its own)
    style_button(b, theme, kind)
    b.bind("<Enter>", lambda e: _hover(b, True))
    b.bind("<Leave>", lambda e: _hover(b, False))
    b.bind("<ButtonPress-1>", lambda e: _press(b, True), add="+")
    b.bind("<ButtonRelease-1>", lambda e: _press(b, False), add="+")
    return b


def style_button(b: tk.Button, theme: Theme, kind: str) -> None:
    bg, fg, border, hover, pressed = _BUTTON_KINDS[kind]
    b._kind = kind
    b._colors = (bg, fg, border, hover, pressed)
    b.configure(bg=bg, fg=fg, activebackground=pressed, activeforeground=fg, disabledforeground=COLORS["text_disabled"],
                highlightbackground=border, highlightcolor=COLORS["accent"],
                state="disabled" if kind == "disabled" else "normal", height=1)
    b._frame.configure(bg=border)


def place_button(b: tk.Button, theme: Theme, **pack) -> None:
    """Pack the border frame and the button inside it (1 px border, control height)."""
    b._frame.pack(**pack)
    b.pack(in_=b._frame, padx=1, pady=1)
    tk.Misc.tkraise(b, b._frame)  # the frame was created after the button and would otherwise cover it


def _hover(b, on):
    if b._kind == "disabled":
        return
    bg, fg, border, hover, pressed = b._colors
    b.configure(bg=hover if on else bg)


def _press(b, on):
    if b._kind == "disabled":
        return
    bg, fg, border, hover, pressed = b._colors
    b.configure(bg=pressed if on else hover)


# ---------- the line chip ----------
class LineChip(tk.Canvas):
    """Capsule in the status bar: name ▾ [badge]. No status dot (the orb already says it)."""

    def __init__(self, parent, theme: Theme, bg: str, command: Callable[[], None]):
        self.theme = theme
        super().__init__(parent, height=theme.chip_h, width=theme.px(80), bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=1)
        self.command = command
        self.name, self.badge, self.badge_kind, self.dark = "", 0, "neutral", False
        self.bg_color = bg
        self.hover = self.pressed = self.focused = False
        self.max_width = 10 ** 6
        self.bind("<Enter>", lambda e: self._state(hover=True))
        self.bind("<Leave>", lambda e: self._state(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._state(pressed=True))
        self.bind("<ButtonRelease-1>", self._click)
        self.bind("<FocusIn>", lambda e: self._state(focused=True))
        self.bind("<FocusOut>", lambda e: self._state(focused=False))
        self.bind("<Return>", lambda e: self.command())
        self.bind("<space>", lambda e: self.command())
        self._draw()

    def set(self, name: str, badge: int = 0, badge_kind: str = "neutral", bg: str | None = None, dark: bool = False,
            max_width: int | None = None) -> None:
        if bg is not None:
            self.bg_color = bg
            self.configure(bg=bg)
        self.name, self.badge, self.badge_kind, self.dark = name, badge, badge_kind, dark
        if max_width is not None:
            self.max_width = max_width
        self._draw()

    def _state(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self._draw()

    def _click(self, event):
        was = self.pressed
        self.pressed = False
        self._draw()
        if was:
            self.command()

    def _draw(self):
        self.delete("all")
        t, px = self.theme, self.theme.px
        h = t.chip_h
        self.configure(height=h)
        f, fb = t.font("ctrl"), t.font("cap_bold")
        name = ellipsize(self.name, 12)
        pad, gap = px(8), px(4)
        badge_w = 0
        if self.badge:
            badge_w = max(px(18), fb.measure(str(self.badge)) + px(10))
        arrow_w = f.measure("▾")
        w = pad + f.measure(name) + gap + arrow_w + (gap + badge_w if self.badge else 0) + pad
        if w > self.max_width:  # not enough room: name goes first, the badge stays
            name = ""
            w = pad + arrow_w + (gap + badge_w if self.badge else 0) + pad
        self.configure(width=w)
        fill = self.bg_color
        if self.dark:
            fill = "#8A5100" if self.pressed else "#AD6E10" if self.hover else self.bg_color
        else:
            fill = COLORS["bg_base_pressed"] if self.pressed else COLORS["bg_base_hover"] if self.hover else self.bg_color
        r = h // 2
        if fill != self.bg_color:
            self.create_oval(0, 0, h, h, fill=fill, outline="")
            self.create_oval(w - h, 0, w, h, fill=fill, outline="")
            self.create_rectangle(r, 0, w - r, h, fill=fill, outline="")
        if self.focused:
            self.create_rectangle(1, 1, w - 2, h - 2, outline=COLORS["accent"], width=px(2))
        fg = COLORS["text_on_dark"] if self.dark else COLORS["text_primary"]
        tert = "#F2E3C8" if self.dark else COLORS["text_tertiary"]
        x = pad
        if name:
            self.create_text(x, h / 2, text=name, anchor="w", font=f, fill=fg)
            x += f.measure(name) + gap
        self.create_text(x, h / 2, text="▾", anchor="w", font=f, fill=tert)
        x += arrow_w + gap
        if self.badge:
            bh = px(18)
            y0 = (h - bh) / 2
            alert = self.badge_kind == "alert"
            if self.dark:
                bbg, bfg = "#FFFFFF", COLORS["state_ask"]
            elif alert:
                bbg, bfg = COLORS["state_ask"], "#FFFFFF"
            else:
                bbg, bfg = COLORS["badge_neutral"], COLORS["text_secondary"]
            self.create_oval(x, y0, x + bh, y0 + bh, fill=bbg, outline="")
            self.create_oval(x + badge_w - bh, y0, x + badge_w, y0 + bh, fill=bbg, outline="")
            self.create_rectangle(x + bh / 2, y0, x + badge_w - bh / 2, y0 + bh, fill=bbg, outline="")
            self.create_text(x + badge_w / 2, h / 2, text=str(self.badge), font=fb, fill=bfg)


# ---------- the line panel (overlay in the small window, left column in the large one) ----------
class LinePanel(tk.Frame):
    """Rows of lines and background jobs. Click a line = switch; the manager row = back to the manager."""

    def __init__(self, parent, theme: Theme, on_pick: Callable[[str], None], on_manager: Callable[[], None],
                 on_settings: Callable[[], None] | None = None, embedded: bool = False, width: int | None = None,
                 on_quit: Callable[[], None] | None = None):
        self.theme = theme
        self.embedded = embedded
        bg = COLORS["bg_base"] if embedded else COLORS["bg_card"]
        super().__init__(parent, bg=bg, bd=0, highlightthickness=0 if embedded else 1,
                         highlightbackground=COLORS["stroke_control"], highlightcolor=COLORS["stroke_control"])
        self.bg_color = bg
        self.width = width or theme.px(200 if embedded else 280)
        self.on_pick, self.on_manager, self.on_settings, self.on_quit = on_pick, on_manager, on_settings, on_quit
        self.rows: list[tuple[str, str, tk.Canvas]] = []  # (kind, id, canvas)
        self.selected = -1
        self.show_settings = False
        self.configure(width=self.width, takefocus=1)
        self.bind("<Up>", lambda e: self._move(-1))
        self.bind("<Down>", lambda e: self._move(1))
        self.bind("<Return>", lambda e: self._activate())
        self.bind("<FocusIn>", lambda e: self._paint())
        self.bind("<FocusOut>", lambda e: self._paint())
        self._data = ({}, "", {})

    def update_rows(self, lines: dict, active: str, jobs: dict, show_settings: bool = False) -> None:
        self._data = (dict(lines), active, dict(jobs))
        self.show_settings = show_settings
        for child in self.winfo_children():
            child.destroy()
        self.rows = []
        t, px = self.theme, self.theme.px
        bg = self.bg_color
        pad = px(12)
        if self.embedded:
            tk.Label(self, text="线路", font=t.font("cap_bold"), fg=COLORS["text_secondary"], bg=bg, anchor="w").pack(
                fill="x", padx=pad, pady=(px(8), px(4)))
        else:
            tk.Frame(self, bg=bg, height=px(8)).pack(fill="x")
        now = time.time()
        ordered = sorted(lines.values(), key=lambda l: 0 if _is_manager(l) else 1)
        for line in ordered:
            lid = line.get("line", "")
            self._row("line", lid, self._line_row, line, lid == active)
        if jobs:
            if not self.embedded:
                tk.Frame(self, bg=COLORS["stroke_divider"], height=1).pack(fill="x", padx=pad, pady=(px(4), px(4)))
            tk.Label(self, text="后台任务", font=t.font("cap_bold"), fg=COLORS["text_secondary"], bg=bg, anchor="w").pack(
                fill="x", padx=pad, pady=(px(8) if self.embedded else px(2), px(4)))
            for jid, job in sorted(jobs.items(), key=lambda kv: kv[1].get("started", 0), reverse=True):
                self._row("job", jid, self._job_row, job, False, now)
        if not self.embedded and ((show_settings and self.on_settings) or self.on_quit):
            tk.Frame(self, bg=COLORS["stroke_divider"], height=1).pack(fill="x", padx=pad, pady=(px(4), px(4)))
            if show_settings and self.on_settings:
                self._row("settings", "", self._settings_row, None, False)
            if self.on_quit:
                self._row("quit", "", self._quit_row, None, False)
        tk.Frame(self, bg=bg, height=px(8)).pack(fill="x")
        self._paint()

    def _row(self, kind, rid, painter, data, current, *extra):
        t = self.theme
        c = tk.Canvas(self, height=t.row_h, width=self.width, bg=COLORS["bg_selected"] if current else self.bg_color,
                      highlightthickness=0, bd=0, cursor="arrow" if kind == "job" else "hand2")
        c.pack(fill="x")
        c._painter, c._data, c._current, c._extra = painter, data, current, extra
        i = len(self.rows)
        self.rows.append((kind, rid, c))
        if kind != "job":
            hover = COLORS["bg_base_hover"] if self.embedded else COLORS["bg_card_hover"]
            c.bind("<Enter>", lambda e, c=c: c.configure(bg=hover) if not c._current else None)
            c.bind("<Leave>", lambda e, c=c: c.configure(bg=COLORS["bg_selected"] if c._current else self.bg_color))
            c.bind("<Button-1>", lambda e, i=i: self._pick(i))
        c.bind("<Configure>", lambda e, c=c: self._repaint(c))
        self._repaint(c)

    def _repaint(self, c):
        c.delete("all")
        c._painter(c, c._data, *c._extra)

    def _paint(self):
        for i, (kind, rid, c) in enumerate(self.rows):
            self._repaint(c)
            if i == self.selected and self.focus_get() is self:
                c.create_rectangle(1, 1, c.winfo_width() - 2, c.winfo_height() - 2, outline=COLORS["accent"], width=self.theme.px(2))

    def _line_row(self, c: tk.Canvas, line: dict):
        t, px = self.theme, self.theme.px
        h = t.row_h
        f, fc = t.font("ctrl"), t.font("cap")
        bg = c.cget("bg")
        manager = _is_manager(line)
        state = line.get("state", "idle")
        mark_kind, color = {"starting": ("hollow", COLORS["text_tertiary"]), "idle": ("hollow", COLORS["text_tertiary"]),
                            "working": ("dot", COLORS["accent"]), "waiting": ("dot", COLORS["state_ask"]),
                            "dead": ("hollow", COLORS["state_error"])}.get(state, ("hollow", COLORS["text_tertiary"]))
        if manager:
            mark_kind, color = "home", COLORS["text_secondary"]
        size = px(14)
        c.create_image(px(12), h / 2, anchor="w", image=make_mark(mark_kind, size, bg, color, c))
        x = px(12) + size + px(8)
        name = "总机" if manager else line.get("name", line.get("line", ""))
        words = STATE_WORDS.get(state, state)
        unread = int(line.get("unread") or 0)
        right = words + (f" · {unread} 条没听" if unread else "")
        avail = c.winfo_width() or self.width
        if unread and x + f.measure(name) + fc.measure(right) + px(24) > avail:
            right = f"{unread} 条没听"  # the narrow left column: the dot colour already says "working"
        right_w = fc.measure(right)
        name = truncate_to_width(name, f.measure, max(px(40), avail - x - right_w - px(24)))
        c.create_text(x, h / 2, text=name, anchor="w", font=f, fill=COLORS["text_primary"])
        c.create_text(avail - px(12), h / 2, text=right, anchor="e", font=fc,
                      fill=COLORS["state_ask"] if state == "waiting" else COLORS["text_secondary"])

    def _job_row(self, c: tk.Canvas, job: dict, now: float):
        t, px = self.theme, self.theme.px
        h = t.row_h
        f, fc = t.font("ctrl"), t.font("cap")
        bg = c.cget("bg")
        state = job.get("state", "running")
        color = {"running": COLORS["accent"], "done": COLORS["state_listen"], "failed": COLORS["state_error"],
                 "cancelled": COLORS["text_tertiary"]}.get(state, COLORS["text_secondary"])
        size = px(14)
        c.create_image(px(12), h / 2, anchor="w", image=make_mark(JOB_MARKS.get(state, "dash"), size, bg, color, c))
        x = px(12) + size + px(8)
        jid = job.get("id", "")
        avail = c.winfo_width() or self.width
        id_w = fc.measure(jid)
        text = truncate_to_width(job_row_text(job, now), f.measure, max(px(40), avail - x - id_w - px(24)))
        c.create_text(x, h / 2, text=text, anchor="w", font=f, fill=COLORS["text_secondary"])
        c.create_text(avail - px(12), h / 2, text=jid, anchor="e", font=fc, fill=COLORS["text_tertiary"])

    def _settings_row(self, c: tk.Canvas, _):
        t, px = self.theme, self.theme.px
        c.create_text(px(12), t.row_h / 2, text="设置…", anchor="w", font=t.font("ctrl"), fill=COLORS["text_primary"])

    def _quit_row(self, c: tk.Canvas, _):
        t, px = self.theme, self.theme.px
        c.create_text(px(12), t.row_h / 2, text="退出 voice-claude", anchor="w", font=t.font("ctrl"), fill=COLORS["text_primary"])

    def _pick(self, i: int):
        if not (0 <= i < len(self.rows)):
            return
        kind, rid, c = self.rows[i]
        if kind == "line":
            lines, active, _ = self._data
            if _is_manager(lines.get(rid, {})):
                self.on_manager()
            else:
                self.on_pick(rid)
        elif kind == "settings" and self.on_settings:
            self.on_settings()
        elif kind == "quit" and self.on_quit:
            self.on_quit()

    def _move(self, step: int):
        picks = [i for i, (k, _, _) in enumerate(self.rows) if k != "job"]
        if not picks:
            return
        if self.selected not in picks:
            self.selected = picks[0] if step > 0 else picks[-1]
        else:
            j = picks.index(self.selected) + step
            self.selected = picks[max(0, min(len(picks) - 1, j))]
        self._paint()

    def _activate(self):
        self._pick(self.selected)


def _is_manager(line: dict) -> bool:
    return line.get("kind") == "manager" or line.get("line") == "manager" or line.get("name") == "总机"


# ---------- the docked request card ----------
class DockedCard(tk.Frame):
    """The request / question that is waiting for you: between the transcript card and the action bar."""

    def __init__(self, parent, theme: Theme, kind: str, title: str, body: str, detail: str, footnote_var: tk.StringVar,
                 total_seconds: int = 60):
        self.theme = theme
        self.kind = kind
        soft = kind == "question"
        self.fill = COLORS["ask_soft_bg"] if soft else COLORS["ask_bg"]
        super().__init__(parent, bg=COLORS["ask_border"], bd=0)
        px = theme.px
        inner = tk.Frame(self, bg=self.fill, bd=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.inner = inner
        tk.Frame(inner, bg=COLORS["state_ask"], width=px(3)).pack(side="left", fill="y")
        col = tk.Frame(inner, bg=self.fill)
        col.pack(side="left", fill="both", expand=True)
        self.drain = tk.Canvas(col, height=px(3), bg=self.fill, highlightthickness=0, bd=0)
        self.drain.pack(fill="x")
        self.drain_bar = self.drain.create_rectangle(0, 0, 0, px(3), fill=COLORS["state_ask"], outline="")
        head = tk.Frame(col, bg=self.fill)
        head.pack(fill="x", padx=px(12), pady=(px(8), px(2)))
        self.title = tk.Label(head, text=title, font=theme.font("cap_bold"), fg=COLORS["state_ask"], bg=self.fill, anchor="w")
        self.title.pack(side="left")
        self.count = tk.Label(head, text="", font=theme.font("ctrl_bold"), fg=COLORS["state_ask"], bg=self.fill, anchor="e")
        self.count.pack(side="right")
        col_px = parent.winfo_width() - px(3 + 24 + 2) if parent.winfo_width() > 50 else theme.col_max
        self.text = tk.Text(col, wrap=wrap_mode(body + "\n" + detail, theme.font("body").measure, col_px), bd=0, highlightthickness=0,
                            bg=self.fill, fg=COLORS["text_primary"],
                            font=theme.font("body"), padx=0, pady=0, cursor="arrow", height=2, spacing2=px(3))
        self.text.pack(fill="x", padx=px(12))
        self.text.tag_config("detail", font=theme.font("code") if is_ascii(detail) else theme.font("ctrl"),
                             foreground=COLORS["text_primary"], spacing1=px(4))
        self.text.insert("end", body)
        if detail:
            self.text.insert("end", "\n" + detail, "detail")
        self.text.configure(state="disabled")
        self.foot = tk.Label(col, textvariable=footnote_var, font=theme.font("ctrl_bold"), fg=COLORS["state_ask"], bg=self.fill,
                             anchor="w", justify="left")
        self.foot.pack(fill="x", padx=px(12), pady=(px(6), px(10)))
        self.total = total_seconds
        self.started = time.monotonic()
        self.heard = ""
        self.max_px = 10 ** 6
        self._fit_width = 0
        self.bind("<Configure>", self._fit)
        self.text.bind("<Configure>", self._fit_text)

    def set_remaining(self, seconds: float) -> None:
        s = max(0, int(math.ceil(seconds)))
        self.count.configure(text=f"{s} 秒后算不行", fg=COLORS["state_error"] if s <= 5 else COLORS["state_ask"])
        w = self.drain.winfo_width()
        self.drain.coords(self.drain_bar, 0, 0, int(w * max(0.0, min(1.0, seconds / self.total))), self.theme.px(3))

    def fit_height(self, max_px: int) -> None:
        """Body text shows as many lines as fit under max_px (45 % of the window); beyond that it scrolls inside."""
        self.max_px = max_px
        self.text.update_idletasks()
        line_h = max(1, self.theme.body_line)  # tk.Text's height is in font lines; spacing comes on top
        try:
            need_px = int(self.text.count("1.0", "end", "ypixels")[0])
        except (TypeError, tk.TclError):
            need_px = 2 * line_h
        lines = max(1, math.ceil(need_px / line_h))
        chrome = self.theme.px(3 + 8 + 2 + 6 + 10) + self.count.winfo_reqheight() + self.foot.winfo_reqheight()
        room = max(1, (max_px - chrome) // line_h)
        self.text.configure(height=max(1, min(lines, room)))

    def _fit(self, event):
        if event.widget is self:
            self.after_idle(lambda: self.winfo_exists() and
                            self.set_remaining(max(0.0, self.total - (time.monotonic() - self.started))))

    def _fit_text(self, event):
        if event.widget is self.text and event.width != self._fit_width:  # the wrap width settled: recount lines
            self._fit_width = event.width
            self.after_idle(lambda: self.winfo_exists() and self.fit_height(self.max_px))


# ---------- answered request cards and error cards, embedded in the transcript ----------
class AnsweredCard(tk.Frame):
    """severity: ok (green bar) / no (grey bar) / question (amber bar, soft yellow) / error (InfoBar red)."""

    def __init__(self, parent, theme: Theme, severity: str, title: str, body: str, detail: str = "", footnote: str = ""):
        self.theme = theme
        px = theme.px
        error = severity == "error"
        fill = COLORS["band_error"] if error else COLORS["ask_soft_bg"] if severity == "question" else COLORS["bg_card"]
        border = COLORS["error_border"] if error else COLORS["stroke_control"]
        bar = {"ok": COLORS["state_listen"], "no": COLORS["text_tertiary"], "question": COLORS["state_ask"],
               "error": COLORS["state_error"]}.get(severity, COLORS["text_tertiary"])
        super().__init__(parent, bg=border, bd=0)
        inner = tk.Frame(self, bg=fill)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.inner = inner
        tk.Frame(inner, bg=bar, width=px(3)).pack(side="left", fill="y")
        col = tk.Frame(inner, bg=fill)
        col.pack(side="left", fill="both", expand=True, padx=px(12), pady=px(8))
        self.labels: list[tk.Label] = []
        if title:
            self.labels.append(tk.Label(col, text=title, font=theme.font("cap_bold"), bg=fill, anchor="w", justify="left",
                                        fg=COLORS["state_error"] if error else COLORS["text_tertiary"]))
        self.labels.append(tk.Label(col, text=body, font=theme.font("body"), fg=COLORS["text_primary"], bg=fill, anchor="w", justify="left"))
        if detail:
            self.labels.append(tk.Label(col, text=detail, font=theme.font("code") if is_ascii(detail) else theme.font("cap"),
                                        fg=COLORS["text_secondary"], bg=fill, anchor="w", justify="left"))
        if footnote:
            green = severity == "ok"  # 你说：可以 / 你按了右 Alt，算可以 / 你点了「可以」
            self.labels.append(tk.Label(col, text=footnote, font=theme.font("cap_bold") if green else theme.font("cap"),
                                        fg=COLORS["state_listen"] if green else COLORS["text_secondary"], bg=fill, anchor="w", justify="left"))
        for i, lab in enumerate(self.labels):
            lab.pack(fill="x", pady=(0 if i == 0 else px(2), 0))
        self.footnote = footnote
        self.width = 0

    def set_width(self, width: int) -> None:
        if width == self.width or width < 50:
            return
        self.width = width
        wrap = max(60, width - self.theme.px(3 + 24 + 2))
        for lab in self.labels:
            lab.configure(wraplength=wrap)
        self.update_idletasks()
        self.pack_propagate(False)
        self.configure(width=width, height=self.inner.winfo_reqheight() + 2)

    def as_text(self) -> str:
        return "\n".join(l.cget("text") for l in self.labels)


# ---------- segmented button for the permissions table ----------
class Segmented(tk.Canvas):
    def __init__(self, parent, theme: Theme, options: list[tuple[str, str]], value: str, on_change: Callable[[str], None], bg: str):
        self.theme = theme
        self.options, self.value, self.on_change = options, value, on_change
        self.cursor_i = next((i for i, (k, _) in enumerate(options) if k == value), 0)
        self.focused = False
        f = theme.font("ctrl")
        self.cell_w = max(f.measure(label) for _, label in options) + theme.px(20)
        super().__init__(parent, width=self.cell_w * len(options) + 2, height=theme.control_h, bg=bg, highlightthickness=0,
                         bd=0, cursor="hand2", takefocus=1)
        self.bind("<Button-1>", self._click)
        self.bind("<Left>", lambda e: self._move(-1))
        self.bind("<Right>", lambda e: self._move(1))
        self.bind("<Return>", lambda e: self._confirm())
        self.bind("<space>", lambda e: self._confirm())
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self._draw()

    def set_value(self, value: str) -> None:
        self.value = value
        self.cursor_i = next((i for i, (k, _) in enumerate(self.options) if k == value), 0)
        self._draw()

    def _set_focus(self, on):
        self.focused = on
        self._draw()

    def _draw(self):
        self.delete("all")
        t, px = self.theme, self.theme.px
        h = t.control_h
        f = t.font("ctrl")
        for i, (key, label) in enumerate(self.options):
            x0 = 1 + i * self.cell_w
            current = key == self.value
            self.create_rectangle(x0, 1, x0 + self.cell_w, h - 1, fill=COLORS["accent"] if current else COLORS["bg_card"],
                                  outline=COLORS["stroke_control"], width=1)
            self.create_text(x0 + self.cell_w / 2, h / 2, text=label, font=f, fill="#FFFFFF" if current else COLORS["text_primary"])
            if self.focused and i == self.cursor_i:
                self.create_rectangle(x0 + 2, 3, x0 + self.cell_w - 2, h - 3, outline=COLORS["accent"], width=px(2))

    def _click(self, event):
        i = int((event.x - 1) // self.cell_w)
        if 0 <= i < len(self.options):
            self.cursor_i = i
            self.focus_set()
            self._confirm()

    def _move(self, step):
        self.cursor_i = max(0, min(len(self.options) - 1, self.cursor_i + step))
        self._draw()

    def _confirm(self):
        key = self.options[self.cursor_i][0]
        if key != self.value:
            self.on_change(key)


# ---------- the text box ----------
_IMM = []


def _imm32():
    """imm32 with 64-bit-safe signatures, or None (not Windows, or not loadable)."""
    if not _IMM:
        imm = None
        if os.name == "nt":
            try:
                imm = ctypes.windll.imm32
                imm.ImmGetContext.restype = ctypes.c_void_p
                imm.ImmGetContext.argtypes = [ctypes.c_void_p]
                imm.ImmGetCompositionStringW.restype = ctypes.c_long
                imm.ImmGetCompositionStringW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong]
                imm.ImmReleaseContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            except Exception:
                imm = None
        _IMM.append(imm)
    return _IMM[0]


class TypeBox(tk.Frame):
    """The text box under the transcript. Enter sends, Shift+Enter starts a new line; a grey hint while it is empty
    and unfocused. on_submit(text) returns False when the text was not taken, and the box keeps it."""

    HINT = "输入文字，回车发送"

    def __init__(self, parent, theme: Theme, on_submit: Callable[[str], bool], bg: str):
        px = theme.px
        super().__init__(parent, bg=COLORS["stroke_card"])
        inner = tk.Frame(self, bg=bg)
        inner.pack(fill="both", expand=True, pady=(1, 0))  # the 1 px above is the rule between transcript and box
        self.text = tk.Text(inner, height=2, wrap="char", bd=0, highlightthickness=0, padx=px(12), pady=px(8), undo=True,
                            bg=bg, fg=COLORS["text_primary"], insertbackground=COLORS["text_primary"], font=theme.font("body"))
        self.text.pack(fill="both", expand=True)
        self.on_submit = on_submit
        self.hinting = False
        self.typed_at = 0.0  # time.monotonic() when you last changed the text (the hint and put() do not count)
        self.emptied_at = 0.0  # when it last became empty
        self.began_at = 0.0  # when you began writing into it after it was empty (an input method: when it began composing)
        self._empty = True
        self._composing_from = 0.0
        self._placed: str | None = None  # what put() put there, until you change it
        for seq in ("<Return>", "<KP_Enter>"):
            self.text.bind(seq, self._enter)
        self.text.bind("<<Modified>>", self._modified)  # typing, the input method, pasting: anything that edits
        self.text.bind("<FocusIn>", lambda e: self._hint(False))
        self.text.bind("<FocusOut>", lambda e: self._hint(True))
        self._hint(True)

    def value(self) -> str:
        return "" if self.hinting else self.text.get("1.0", "end-1c")

    def put(self, text: str) -> bool:
        """Text the window chose (a rewound sentence); False, and the box left alone, when it holds something you
        wrote. What an earlier put() left there, untouched, is replaced."""
        now = self.value()
        if now.strip() and now != self._placed:
            return False
        self._hint(False)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self._placed = text
        self.text.edit_reset()
        return True

    def _modified(self, _event=None):
        """<<Modified>> comes after the change, from the event queue: tell your edits from the window's by content."""
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self.hinting:
            return
        now, content = time.monotonic(), self.text.get("1.0", "end-1c")
        if not content.strip():
            self.emptied_at, self._placed, self._empty, self._composing_from = now, None, True, 0.0
        elif content != self._placed:
            if self._empty:
                self.began_at = self._composing_from or now
            self.typed_at, self._placed, self._empty, self._composing_from = now, None, False, 0.0
        else:
            self._empty = False

    def poll(self) -> None:
        """While the box is empty, note when an input method begins composing into it: the text only arrives when
        the composition is committed, but the writing began here."""
        if not self._empty:
            return
        if self.composing():
            if not self._composing_from:
                self._composing_from = time.monotonic()
        elif self._composing_from and not self.text.get("1.0", "end-1c").strip():
            # given up (Esc, or the pinyin backspaced away): nothing reached the box, which counts as clearing it
            self._composing_from, self.emptied_at = 0.0, time.monotonic()

    def composing(self) -> bool:
        """An input method (pinyin and the like) holds text it has not put into the box yet."""
        imm = _imm32()
        if imm is None:
            return False
        try:
            hwnd = self.text.winfo_id()
            himc = imm.ImmGetContext(hwnd)
            if not himc:
                return False
            try:
                return imm.ImmGetCompositionStringW(himc, 0x0008, None, 0) > 0  # GCS_COMPSTR: bytes being composed
            finally:
                imm.ImmReleaseContext(hwnd, himc)
        except Exception:
            return False

    def _hint(self, on: bool) -> None:
        # The hint goes in and out with undo off: it is not an edit of yours, so Ctrl+Z neither brings it back as
        # text nor loses your own history.
        if on and not self.hinting and not self.text.get("1.0", "end-1c").strip():
            self.text.configure(undo=False)
            self.text.delete("1.0", "end")
            self.text.insert("1.0", self.HINT)
            self.text.configure(undo=True, fg=COLORS["text_tertiary"])
            self.hinting = True
        elif not on and self.hinting:
            self.text.configure(undo=False)
            self.text.delete("1.0", "end")
            self.text.configure(undo=True, fg=COLORS["text_primary"])
            self.hinting = False

    def _enter(self, event):
        if event.state & 0x0001:  # Shift: the default binding inserts a new line
            return None
        text = self.value().strip()
        if text and self.on_submit(text):
            self.text.delete("1.0", "end")
        return "break"


# ---------- "back to latest" ----------
class BackToLatest(tk.Canvas):
    def __init__(self, parent, theme: Theme, command: Callable[[], None]):
        self.theme = theme
        f = theme.font("ctrl")
        text = "↓ 回到最新"
        w, h = f.measure(text) + theme.px(24), theme.control_h - theme.px(4)
        super().__init__(parent, width=w, height=h, bg=COLORS["bg_card"], highlightthickness=0, bd=0, cursor="hand2")
        self.create_rectangle(0, 0, w - 1, h - 1, fill=COLORS["bg_card"], outline=COLORS["stroke_control"])
        self.create_text(w / 2, h / 2, text=text, font=f, fill=COLORS["text_primary"])
        self.bind("<Button-1>", lambda e: command())


# ---------- settings parts ----------
# Page header, a bordered group of navigation rows, radio / check marks drawn with Pillow so they grow with the font
# (tk's own indicators stay a few pixels wide), and a text link for things that open outside the window.

def page_header(parent, theme: Theme, title: str, on_back: Callable[[], None], bg: str | None = None):
    """[←] 标题, left-aligned. The caller packs it at the top of the page, outside the scrolling body."""
    bg = bg or COLORS["bg_card"]
    px = theme.px
    head = tk.Frame(parent, bg=bg)
    back = flat_button(head, theme, "←", on_back, kind="ghost", padx=8)
    back.configure(font=theme.font("sub"))
    place_button(back, theme, side="left")
    title_label = tk.Label(head, text=title, font=theme.font("status"), fg=COLORS["text_primary"], bg=bg, anchor="w")
    title_label.pack(side="left", padx=(px(2), 0))
    head.back, head.title = back, title_label
    return head


def header_padx(theme: Theme, pad: int = 16) -> tuple[int, int]:
    """Pack padding for page_header so the arrow glyph (not its button box) lines up with the body's left edge at
    `pad`: the box adds its 1 px border, the 2 px focus ring and 8 px of inner padding before the glyph."""
    px = theme.px
    return max(0, px(pad) - (1 + px(2) + px(8))), px(12)


_CHOICES: dict[tuple, ImageTk.PhotoImage] = {}


def choice_mark(kind: str, on: bool, size: int, bg: str, enabled: bool = True, gap: int = 0, master=None) -> ImageTk.PhotoImage:
    """A radio (kind "radio") or check box ("check") mark, size x size px, with `gap` px of background on its right
    (the space before the label: a tk.Label has no image-to-text gap of its own). Cached per interpreter."""
    key = (id(master.tk) if master is not None else 0, kind, on, size, bg, enabled, gap)
    if key in _CHOICES:
        return _CHOICES[key]
    s = size * 4
    img = Image.new("RGB", ((size + gap) * 4, s), _hex(bg))
    d = ImageDraw.Draw(img)
    fill = _hex(COLORS["accent"] if enabled else COLORS["text_disabled"])
    line = _hex(COLORS["text_tertiary"] if enabled else COLORS["stroke_control"])
    w = max(4, round(s * 0.09))
    if kind == "radio":
        if on:
            d.ellipse((0, 0, s - 1, s - 1), fill=fill)
            r, c = s * 0.2, s / 2
            d.ellipse((c - r, c - r, c + r, c + r), fill=(255, 255, 255))
        else:
            d.ellipse((w / 2, w / 2, s - 1 - w / 2, s - 1 - w / 2), outline=line, width=w)
    else:
        radius = round(s * 0.18)
        if on:
            d.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=fill)
            d.line([(s * 0.24, s * 0.52), (s * 0.43, s * 0.70), (s * 0.77, s * 0.32)], fill=(255, 255, 255),
                   width=max(5, round(s * 0.11)), joint="curve")
        else:
            d.rounded_rectangle((w / 2, w / 2, s - 1 - w / 2, s - 1 - w / 2), radius=radius, outline=line, width=max(3, w * 2 // 3))
    _CHOICES[key] = ImageTk.PhotoImage(img.resize((size + gap, size), Image.LANCZOS), master=master)
    return _CHOICES[key]


def mark_size(theme: Theme) -> int:
    return theme.px(theme.size + 4)


class ChoiceRadio(tk.Label):
    """A radio: drawn mark + text. It does not select itself: a click (or Enter / space) calls on_pick(value) and
    the owner decides what is selected with set(checked=...). That keeps the dot on what is true, not on the click."""

    def __init__(self, parent, theme: Theme, text: str, value: str, on_pick: Callable[[str], None], bg: str, font: str = "body"):
        self.theme, self.value, self.on_pick, self.bg_color = theme, value, on_pick, bg
        self.checked, self.enabled = False, True
        super().__init__(parent, text=text, compound="left", font=theme.font(font), bg=bg, fg=COLORS["text_primary"],
                         anchor="w", cursor="hand2", takefocus=1, bd=0, padx=0, highlightthickness=theme.px(2),
                         highlightbackground=bg, highlightcolor=COLORS["accent"])
        self.bind("<Button-1>", lambda e: self._pick())
        self.bind("<Return>", lambda e: self._pick())
        self.bind("<space>", lambda e: self._pick())
        self._draw()

    def set(self, checked: bool | None = None, enabled: bool | None = None) -> None:
        if checked is not None:
            self.checked = bool(checked)
        if enabled is not None:
            self.enabled = bool(enabled)
        self._draw()

    def _draw(self):
        img = choice_mark("radio", self.checked, mark_size(self.theme), self.bg_color, self.enabled, self.theme.px(8), self)
        self._img = img
        self.configure(image=img, fg=COLORS["text_primary"] if self.enabled else COLORS["text_disabled"],
                       cursor="hand2" if self.enabled else "arrow")

    def _pick(self):
        if self.enabled:
            self.focus_set()
            self.on_pick(self.value)


class CheckRow(tk.Frame):
    """Name on the left, check mark on the right; the whole row toggles (click, Enter, space)."""

    def __init__(self, parent, theme: Theme, text: str, variable: tk.BooleanVar, on_change: Callable[[bool], None], bg: str):
        px = theme.px
        super().__init__(parent, bg=bg, takefocus=1, cursor="hand2", highlightthickness=px(2), highlightbackground=bg,
                         highlightcolor=COLORS["accent"])
        self.theme, self.var, self.on_change, self.bg_color = theme, variable, on_change, bg
        self.label = tk.Label(self, text=text, font=theme.font("ctrl"), bg=bg, fg=COLORS["text_primary"], anchor="w", cursor="hand2")
        self.label.pack(side="left", fill="x", expand=True, pady=px(6))
        self.mark = tk.Label(self, bg=bg, bd=0, cursor="hand2")
        self.mark.pack(side="right")
        for w in (self, self.label, self.mark):
            w.bind("<Button-1>", lambda e: self.toggle())
        self.bind("<Return>", lambda e: self.toggle())
        self.bind("<space>", lambda e: self.toggle())
        self._trace = variable.trace_add("write", lambda *a: self._draw())
        self.bind("<Destroy>", self._untrace, add="+")
        self._draw()

    def toggle(self):
        self.focus_set()
        self.var.set(not self.var.get())
        self.on_change(bool(self.var.get()))

    def _draw(self):
        try:
            img = choice_mark("check", bool(self.var.get()), mark_size(self.theme), self.bg_color, True, 0, self)
            self._img = img
            self.mark.configure(image=img)
        except tk.TclError:  # destroyed while the variable still had our trace
            pass

    def _untrace(self, event):
        if event.widget is self:
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass


class LinkLabel(tk.Label):
    """Accent text, underlined on hover; opens something outside the window (a web page)."""

    def __init__(self, parent, theme: Theme, text: str, command: Callable[[], None], bg: str):
        self.theme, self.command = theme, command
        super().__init__(parent, text=text, font=theme.font("ctrl"), fg=COLORS["accent"], bg=bg, cursor="hand2", takefocus=1,
                         bd=0, padx=0, highlightthickness=theme.px(2), highlightbackground=bg, highlightcolor=COLORS["accent"])
        self.bind("<Enter>", lambda e: self.configure(font=theme.font("link")))
        self.bind("<Leave>", lambda e: self.configure(font=theme.font("ctrl")))
        for seq in ("<Button-1>", "<Return>", "<space>"):
            self.bind(seq, lambda e: self.command())


class NavGroup(tk.Frame):
    """A bordered box of navigation rows, separated by hairlines (the grouped list of Windows 11 / macOS settings)."""

    def __init__(self, parent, theme: Theme, bg: str | None = None):
        bg = bg or COLORS["bg_card"]
        super().__init__(parent, bg=COLORS["stroke_control"], bd=0)  # the 1 px border shows around `inner`
        self.theme, self.bg_color = theme, bg
        self.inner = tk.Frame(self, bg=bg)
        self.inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.rows: list[NavRow] = []

    def add(self, label: str, on_open: Callable[[], None]) -> "NavRow":
        if self.rows:
            tk.Frame(self.inner, bg=COLORS["stroke_divider"], height=1).pack(fill="x", padx=self.theme.px(12))
        row = NavRow(self.inner, self.theme, label, on_open, self.bg_color)
        row.pack(fill="x")
        self.rows.append(row)
        return row


class NavRow(tk.Frame):
    """名字 ······ 状态 › — the whole row opens a sub-page (click, Enter, space)."""

    def __init__(self, parent, theme: Theme, label: str, on_open: Callable[[], None], bg: str):
        px = theme.px
        super().__init__(parent, bg=bg, takefocus=1, cursor="hand2", highlightthickness=px(2), highlightbackground=bg,
                         highlightcolor=COLORS["accent"])
        self.theme, self.on_open, self.bg_color = theme, on_open, bg
        self.full_value, self.error = "", False
        self.name = tk.Label(self, text=label, font=theme.font("ctrl"), bg=bg, fg=COLORS["text_primary"], anchor="w", cursor="hand2")
        self.arrow = tk.Label(self, text="›", font=theme.font("sub"), bg=bg, fg=COLORS["text_tertiary"], cursor="hand2")
        self.value = tk.Label(self, text="", font=theme.font("cap"), bg=bg, fg=COLORS["text_secondary"], anchor="e", cursor="hand2")
        self.name.pack(side="left", padx=(px(12), px(8)), pady=px(8))
        self.arrow.pack(side="right", padx=(px(8), px(12)))
        self.value.pack(side="right")
        self.parts = (self, self.name, self.value, self.arrow)
        for w in self.parts:
            w.bind("<Button-1>", lambda e: self.on_open())
            w.bind("<Enter>", lambda e: self._hover(True), add="+")
            w.bind("<Leave>", lambda e: self.after(15, self._check_leave), add="+")
        self.bind("<Return>", lambda e: self.on_open())
        self.bind("<space>", lambda e: self.on_open())
        self.bind("<Configure>", lambda e: self._fit(), add="+")

    def set_value(self, text: str, error: bool = False) -> None:
        self.full_value, self.error = text, error
        self.value.configure(fg=COLORS["state_error"] if error else COLORS["text_secondary"])
        self._fit()

    def _fit(self):
        px = self.theme.px
        text = self.full_value
        if self.winfo_width() > 1:
            room = self.winfo_width() - self.name.winfo_reqwidth() - self.arrow.winfo_reqwidth() - px(12 + 8 + 8 + 12 + 8)
            text = truncate_to_width(text, self.theme.font("cap").measure, max(px(40), room))
        if self.value.cget("text") != text:
            self.value.configure(text=text)

    def _hover(self, on: bool):
        color = COLORS["bg_card_hover"] if on else self.bg_color
        for w in self.parts:
            w.configure(bg=color)
        self.configure(highlightbackground=color)

    def _check_leave(self):
        """<Leave> also fires when the pointer moves onto one of the row's own labels: only un-hover when it left."""
        try:
            x, y = self.winfo_pointerxy()
            w = self.winfo_containing(x, y)
        except tk.TclError:
            return
        inside = w is not None and (w is self or str(w).startswith(str(self) + "."))
        if not inside:
            self._hover(False)
