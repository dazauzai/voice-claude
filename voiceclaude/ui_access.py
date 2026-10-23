"""The AI 服务商 page (contract: docs/protocol.md 7a / 7b): which service
answers, and each service's API key.

Layout: [←] AI 服务商 (fixed header), then the group 默认服务商 with one block per service from
`client.list_providers()` — radio + name and a short state on the right; the key box with 保存 / 删除; 登录… (only
the service that also works through the user's own Claude Code) and the link 获取 API key; that service's result
line. Lines that do not use the default service are listed under the blocks. A fixed footer holds 测试连接 / 刷新.
A service that does not answer list_providers shows Claude alone, driven by the 7a commands.

Copy rule: label, state, action; results only right after an action, in Windows wording. A full key is never
logged or shown."""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from typing import Callable

from .ui_theme import COLORS, Theme
from .ui_widgets import ChoiceRadio, LinkLabel, flat_button, header_padx, mark_size, page_header, place_button, style_button

log = logging.getLogger(__name__)

CREATE_NEW_CONSOLE = 0x00000010
INSTALL_URL = "https://claude.com/claude-code"
SHORT_NAMES = {"claude": "Claude", "anthropic": "Claude", "deepseek": "DeepSeek", "openai": "GPT"}
PLACEHOLDER = "API key"
TITLE = "AI 服务商"


def short(name: str) -> str:
    """'DeepSeek（深度求索）' -> 'DeepSeek'."""
    return name.split("（")[0].split("(")[0].strip() or name


def vendor_name(access: dict | None, pid: str) -> str:
    if access and access.get("provider") == pid and access.get("provider_name"):
        return short(str(access["provider_name"]))
    return SHORT_NAMES.get(pid, pid)


def describe(access: dict | None) -> str:
    """The settings page's one-line state of the AI service. Never a key."""
    if not access:
        return "正在读取…"
    method = str(access.get("method", "none"))
    pid = str(access.get("provider", "") or "claude")
    if not access.get("ok"):
        return "未设置"
    if method == "subscription":
        return "Claude · 已登录"
    return f"{vendor_name(access, pid)} · API key"


def describe_is_error(access: dict | None) -> bool:
    return bool(access) and not access.get("ok")


def legacy_vendor(access: dict | None) -> dict:
    """Claude alone, in the shape of list_providers, for a service without that command."""
    access = access or {}
    method = str(access.get("method", "none"))
    return {"id": "claude", "name": "Claude", "configured": method == "api_key", "account": str(access.get("account") or ""),
            "ok": bool(access.get("ok")), "message": str(access.get("message") or ""), "model": "", "models": [],
            "installed": True, "install_hint": "", "is_default": True, "key_prefix": "sk-ant-", "url": "", "subscription": True,
            "fields": [{"key": "api_key", "label": "API key", "secret": True, "hint": ""}]}


def state_of(v: dict) -> tuple[str, bool]:
    """(short state, is it a problem) for the right side of a service's row."""
    account = str(v.get("account") or "")
    signed_in = bool(v.get("ok")) and v.get("login_state") == "logged_in"
    # "installed" is the key route's part (LiteLLM for GPT): it matters once a key is saved, or when there is no login
    if not v.get("installed", True) and not signed_in and (v.get("configured") or not can_log_in(v)):
        hint = str(v.get("install_hint") or "").lower()
        return ("需要安装 LiteLLM" if "litellm" in hint or v.get("id") == "openai" else "需要安装组件"), True
    if v.get("configured"):
        saved = f"已保存 {account}" if account else "已保存"
        return (saved, False) if v.get("ok") else (saved + " · 不可用", True)
    if v.get("ok"):
        # "a@b.c max" -> the e-mail; "ChatGPT（Codex 已登录）" -> "ChatGPT"
        who = (account.split()[0] if "@" in account else short(account)) if account else ""
        return ("已登录 · " + who) if who else "已登录", False
    return "未设置", False


def can_log_in(v: dict) -> bool:
    """Does this service also work through its own program's login? 7b-1 says so in `methods`; services without it only
    marked Claude (`subscription`)."""
    methods = v.get("methods")
    if isinstance(methods, list):
        return "login" in methods
    return bool(v.get("subscription", v.get("id") == "claude"))


def lines_text(lines: list, vendors: list[dict], default: str) -> str:
    """The lines that do NOT use the default service: 'Atlas 线路使用 Claude'; '' when there are none."""
    names = {str(v.get("id")): short(str(v.get("name") or v.get("id"))) for v in vendors}
    odd = [f"{ln.get('name') or ln.get('line')} 线路使用 {names.get(str(ln.get('provider')), str(ln.get('provider')))}"
           for ln in lines if isinstance(ln, dict) and ln.get("provider") and str(ln.get("provider")) != default]
    return "；".join(odd)


def confirm(parent, title: str, text: str) -> bool:
    """A native OK / Cancel question; Cancel is the default. Tests replace it."""
    return bool(messagebox.askokcancel(title, text, parent=parent, icon="warning", default="cancel"))


class KeyEntry(tk.Entry):
    """A masked entry with a grey 'API key' placeholder while empty and unfocused. on_edit() runs on every change."""

    def __init__(self, parent, theme: Theme, bg: str, on_edit: Callable[[], None], on_enter: Callable[[], None]):
        self.var = tk.StringVar()
        super().__init__(parent, textvariable=self.var, font=theme.font("code"), relief="flat", highlightthickness=1,
                         highlightbackground=COLORS["stroke_control"], highlightcolor=COLORS["accent"], bg=bg,
                         insertbackground=COLORS["text_primary"])
        self.placeholder = False
        self.on_edit = on_edit
        self.bind("<FocusIn>", self._in)
        self.bind("<FocusOut>", self._out)
        self.bind("<Return>", lambda e: on_enter())
        self._out()
        self.var.trace_add("write", lambda *a: self.on_edit())

    def value(self) -> str:
        return "" if self.placeholder else self.var.get().strip()

    def clear(self) -> None:
        self.placeholder = False
        self.var.set("")
        if self.focus_get() is not self:
            self._out()

    def set_value(self, text: str) -> None:  # tests and paste helpers
        self._in()
        self.var.set(text)

    def _in(self, event=None):
        if self.placeholder:
            self.placeholder = False
            self.var.set("")
            self.configure(show="•", fg=COLORS["text_primary"])

    def _out(self, event=None):
        if not self.var.get():
            self.placeholder = True
            self.configure(show="", fg=COLORS["text_tertiary"])
            self.var.set(PLACEHOLDER)


class AccessPage(tk.Frame):
    """post(fn) runs fn on the UI thread (the window's ui_q). client is the DaemonClient: access_status /
    set_api_key / clear_api_key / test_access (7a) and, when present, list_providers / set_provider_key /
    clear_provider_key / test_provider / switch_provider (7b). on_back() leaves the page; on_access(access) is told
    every fresh `access` dict (the settings page shows it). The window forwards the wheel to `canvas`."""

    def __init__(self, parent, theme: Theme, client, post: Callable[[Callable[[], None]], None], on_back: Callable[[], None],
                 on_access: Callable[[dict], None] | None = None, on_say: Callable[[str], None] | None = None):
        self.theme, self.client, self.post, self.on_back = theme, client, post, on_back
        self.on_access = on_access or (lambda a: None)
        self.on_say = on_say or (lambda text: None)  # read aloud (a device code has to be heard, not only seen)
        bg = COLORS["bg_card"]
        super().__init__(parent, bg=bg)
        px = theme.px
        self.bg_color = bg
        self.pad = pad = px(16)

        self.header = page_header(self, theme, TITLE, self.on_back, bg)
        self.header.pack(side="top", fill="x", padx=header_padx(theme), pady=(px(12), px(8)))

        footer = tk.Frame(self, bg=bg)
        footer.pack(side="bottom", fill="x")
        tk.Frame(footer, bg=COLORS["stroke_divider"], height=1).pack(fill="x", padx=pad, pady=(0, px(8)))
        self.message = tk.Label(footer, text="", font=theme.font("cap"), fg=COLORS["text_secondary"], bg=bg, anchor="w", justify="left")
        self.buttons = tk.Frame(footer, bg=bg)
        self.buttons.pack(fill="x", padx=pad, pady=(0, pad))
        self.btn_test = flat_button(self.buttons, theme, "测试连接", self._test)
        place_button(self.btn_test, theme, side="left", padx=(0, px(8)))
        self.btn_detect = flat_button(self.buttons, theme, "刷新", self._detect)
        place_button(self.btn_detect, theme, side="left")

        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview, style="Flat.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", pady=px(4))
        self.canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(self.canvas, bg=bg)
        self.body = body
        self._item = self.canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_resize)
        tk.Label(body, text="默认服务商", font=theme.font("sub"), fg=COLORS["text_primary"], bg=bg, anchor="w").pack(
            fill="x", padx=pad, pady=(px(4), px(8)))
        self.blocks = tk.Frame(body, bg=bg)
        self.blocks.pack(fill="x")
        self.lines_label = tk.Label(body, text="", font=theme.font("cap"), fg=COLORS["text_secondary"], bg=bg, anchor="w", justify="left")
        self.bottom = tk.Frame(body, bg=bg, height=pad)
        self.bottom.pack(fill="x")

        self.access: dict = {}
        self.vendors: list[dict] = []
        self.lines: list = []
        self.default = ""
        self.selected = ""  # the service shown as answering: the default, when it can answer; "" otherwise
        self.legacy: bool | None = None  # None until the service has answered (or failed) list_providers once
        self.seq = 0  # the latest list_providers request; older answers are dropped
        self.inflight = 0
        self.block_ids: list[str] = []
        self.rows: dict[str, dict] = {}
        self.busy = False
        self._login_pending = False
        self._logging_in: set[str] = set()  # services whose login is running (it waits for the browser)
        self._build_blocks([legacy_vendor(None)])
        self._focus_bind = self.winfo_toplevel().bind("<FocusIn>", self._back_in_window, add="+")
        self.bind("<Destroy>", self._unbind_focus, add="+")

    # ---------- building blocks ----------
    def _build_blocks(self, vendors: list[dict]) -> None:
        theme, px, bg = self.theme, self.theme.px, self.bg_color
        for w in self.blocks.winfo_children():
            w.destroy()
        self.rows = {}
        self.block_ids = [str(v["id"]) for v in vendors]
        indent = self.pad + mark_size(theme) + px(8)  # under the name, past the radio mark
        for i, v in enumerate(vendors):
            pid = str(v["id"])
            if i:
                tk.Frame(self.blocks, bg=COLORS["stroke_divider"], height=1).pack(fill="x", padx=self.pad, pady=(px(8), px(8)))
            top = tk.Frame(self.blocks, bg=bg)
            top.pack(fill="x", padx=(self.pad - px(2), self.pad))
            radio = ChoiceRadio(top, theme, short(str(v.get("name") or pid)), pid, self._on_pick, bg)
            radio.pack(side="left")
            state = tk.Label(top, text="", font=theme.font("cap"), fg=COLORS["text_secondary"], bg=bg, anchor="e")
            state.pack(side="right")
            keyrow = tk.Frame(self.blocks, bg=bg)
            keyrow.pack(fill="x", padx=(indent, self.pad), pady=(px(6), 0))
            entry = KeyEntry(keyrow, theme, bg, lambda p=pid: self._paint_buttons(p), lambda p=pid: self._save(p))
            entry.pack(side="left", fill="x", expand=True, ipady=px(5), padx=(0, px(8)))
            save = flat_button(keyrow, theme, "保存", lambda p=pid: self._save(p))
            place_button(save, theme, side="left", padx=(0, px(8)))
            delete = flat_button(keyrow, theme, "删除", lambda p=pid: self._delete(p))
            place_button(delete, theme, side="left")
            extra = tk.Frame(self.blocks, bg=bg)
            login = None
            if can_log_in(v):  # this service also works through its own program's login (Claude Code, Codex)
                login = flat_button(extra, theme, "登录…", lambda p=pid: self._login(p))
                place_button(login, theme, side="left", padx=(0, px(12)))
            url = str(v.get("url") or "")
            if url:
                LinkLabel(extra, theme, "获取 API key", lambda u=url: webbrowser.open(u), bg).pack(side="left")
            if login is not None or url:
                extra.pack(fill="x", padx=(indent, self.pad), pady=(px(6), 0))
            result = tk.Label(self.blocks, text="", font=theme.font("cap"), fg=COLORS["text_secondary"], bg=bg, anchor="w", justify="left")
            after = tk.Frame(self.blocks, bg=bg, height=0)  # where the result line goes when it shows
            after.pack(fill="x")
            self.rows[pid] = {"radio": radio, "entry": entry, "var": entry.var, "save": save, "delete": delete, "login": login,
                              "state": state, "result": result, "anchor": after, "indent": indent,
                              "prefix": str(v.get("key_prefix") or "")}
            self._paint_buttons(pid)

    # ---------- showing state ----------
    def show(self, access: dict | None) -> None:
        """New `access` from the hello / status / a command's answer; then the service list is re-read."""
        self.access = dict(access or {})
        if access:
            self.on_access(self.access)
        if self.legacy:
            self._apply([legacy_vendor(self.access)], "claude", [])
        self.refresh()

    def on_hide(self) -> None:
        """The window left this page: no pasted key stays on screen, old results go."""
        for pid, row in self.rows.items():
            row["entry"].clear()
            self._result(pid, "")
        self.say("")

    def refresh(self) -> None:
        """Re-read list_providers on a worker thread; a service without it leaves Claude alone (from `access`)."""
        fn = getattr(self.client, "list_providers", None)
        if fn is None:
            self.legacy = True
            self._apply([legacy_vendor(self.access)], "claude", [])
            return
        self.seq += 1
        seq = self.seq
        self.inflight += 1
        err = ""

        def work():
            nonlocal err
            try:
                r = fn()
            except Exception as e:  # no connection, or the service did not answer in time
                log.debug("list_providers failed: %s", e)
                r, err = None, str(e)

            def finish():
                self.inflight -= 1
                if seq != self.seq:
                    return
                if isinstance(r, dict) and isinstance(r.get("providers"), list) and r["providers"]:
                    self.legacy = False
                    self._apply(r["providers"], str(r.get("default") or ""), r.get("lines") or [])
                elif isinstance(r, dict) and "不认识的命令" in str(r.get("message", "")):  # no list_providers
                    self.legacy = True
                    self._apply([legacy_vendor(self.access)], "claude", [])
                else:  # a passing failure (timeout, lost connection): keep what is shown, say what happened
                    why = str((r or {}).get("message", "") if isinstance(r, dict) else err) or "后台服务未响应"
                    self.say(f"无法读取服务商列表：{why}", False)
                    if self.legacy is None and not self.vendors:
                        self._apply([legacy_vendor(self.access)], "claude", [])
            self.post(finish)
        threading.Thread(target=work, daemon=True, name="access-list").start()

    def _apply(self, vendors: list[dict], default: str, lines: list) -> None:
        vendors = [v for v in vendors if isinstance(v, dict) and v.get("id")]
        if [str(v["id"]) for v in vendors] != self.block_ids:
            self._build_blocks(vendors)
        self.vendors, self.default, self.lines = vendors, default, list(lines)
        for v in vendors:
            row = self.rows.get(str(v["id"]))
            if row is not None:
                text, bad = state_of(v)
                row["state"].configure(text=text, fg=COLORS["state_error"] if bad else COLORS["text_secondary"])
        current = self._vendor(default)
        self.selected = default if current is not None and current.get("ok") else ""
        self._paint_choices()
        extra = lines_text(self.lines, self.vendors, self.default)
        self.lines_label.configure(text=extra)
        if extra:
            self.lines_label.pack(fill="x", padx=self.pad, pady=(self.theme.px(12), 0), before=self.bottom)
        else:
            self.lines_label.pack_forget()

    def _paint_choices(self) -> None:
        for pid, row in self.rows.items():
            row["radio"].set(checked=pid == self.selected, enabled=not self.busy)
            self._paint_buttons(pid)
        for b in (self.btn_test, self.btn_detect):
            style_button(b, self.theme, "disabled" if self.busy else "secondary")
        for pid, row in self.rows.items():
            if row.get("login") is not None:
                style_button(row["login"], self.theme, "disabled" if self.busy or pid in self._logging_in else "secondary")

    def _paint_buttons(self, pid: str) -> None:
        row = self.rows.get(pid)
        if row is None:
            return
        v = self._vendor(pid) or {}
        can_save = not self.busy and bool(row["entry"].value())
        can_delete = not self.busy and bool(v.get("configured"))
        for b, ok in ((row["save"], can_save), (row["delete"], can_delete)):
            kind = "secondary" if ok else "disabled"
            if getattr(b, "_kind", "") != kind:
                style_button(b, self.theme, kind)

    def _on_resize(self, event) -> None:
        """Wrap widths come from the page's width, never from a label's own width: a label that set its wrap from
        its own <Configure> could feed back through the body's layout and re-lay out forever (seen with the long
        LiteLLM install hint, whose path cannot be broken at a space)."""
        self.canvas.itemconfigure(self._item, width=event.width)
        self._wrap_all(event.width)

    def _wrap_all(self, width: int) -> None:
        px = self.theme.px
        if width <= 1:
            return
        for row in self.rows.values():
            row["result"].configure(wraplength=max(px(100), width - row["indent"] - self.pad - px(4)))
        self.lines_label.configure(wraplength=max(px(100), width - 2 * self.pad - px(4)))
        self.message.configure(wraplength=max(px(100), width - 2 * self.pad - px(4)))

    def _result(self, pid: str, text: str, ok: bool = True) -> None:
        """The line under one service: shown right after an action on it, gone when the page is left."""
        row = self.rows.get(pid)
        if row is None:
            return
        lab = row["result"]
        lab.configure(text=text, fg=COLORS["text_secondary"] if ok else COLORS["state_error"])
        self._wrap_all(self.canvas.winfo_width())
        if text:
            lab.pack(fill="x", padx=(row["indent"], self.pad), pady=(self.theme.px(6), 0), before=row["anchor"])
        else:
            lab.pack_forget()

    def say(self, message: str, ok: bool = True) -> None:
        """A page-level message (not about one service), above the footer buttons."""
        self.message.configure(text=message, fg=COLORS["text_secondary"] if ok else COLORS["state_error"])
        self._wrap_all(self.canvas.winfo_width())
        if message:
            self.message.pack(fill="x", padx=self.pad, pady=(0, self.theme.px(6)), before=self.buttons)
        else:
            self.message.pack_forget()

    def _vendor(self, pid: str) -> dict | None:
        return next((v for v in self.vendors if str(v.get("id")) == pid), None)

    def _vname(self, pid: str) -> str:
        v = self._vendor(pid)
        return short(str(v.get("name"))) if v and v.get("name") else SHORT_NAMES.get(pid, pid)

    # ---------- picking the service ----------
    def _on_pick(self, pid: str) -> None:
        if self.busy or pid == self.selected:
            return
        vendor = self._vendor(pid)
        if vendor is None:
            return
        if not vendor.get("ok"):
            if not vendor.get("installed", True) and (vendor.get("configured") or not can_log_in(vendor)):
                self._result(pid, str(vendor.get("install_hint") or vendor.get("message") or "需要安装组件"), False)
            elif not vendor.get("configured"):
                self._result(pid, "未登录，也未设置 API key" if can_log_in(vendor) else "未设置 API key", False)
                self.rows[pid]["entry"].focus_set()
            else:
                self._result(pid, str(vendor.get("message") or f"无法使用 {self._vname(pid)}"), False)
            return
        fn = getattr(self.client, "switch_provider", None)
        if fn is None or self.legacy:
            self._result(pid, "后台服务版本过旧，不支持此操作", False)
            return
        name = self._vname(pid)
        self._result(pid, f"正在切换到 {name}…")

        def done(r):
            self._result(pid, f"已切换到 {name}" if r.get("ok") else str(r.get("message") or "无法切换"), bool(r.get("ok")))
        self._call(lambda: fn(pid), done)

    # ---------- actions (each daemon call on a worker thread; the UI is updated through post) ----------
    def _call(self, fn, done: Callable[[dict], None], button: tk.Button | None = None, busy_text: str = "") -> bool:
        if self.busy:
            return False
        self.busy = True
        normal_text = button.cget("text") if button is not None else None
        self._paint_choices()
        if button is not None and busy_text:
            button.configure(text=busy_text)

        def work():
            try:
                r = fn()
                if not isinstance(r, dict):
                    r = {"ok": False, "message": str(r)}
            except Exception as e:  # a service without the command, or no connection
                log.warning("access action failed: %s", e)
                r = {"ok": False, "message": str(e) or "后台服务未响应"}

            def finish():
                self.busy = False
                if button is not None and normal_text:
                    button.configure(text=normal_text)
                if isinstance(r.get("access"), dict):
                    self.access = dict(r["access"])
                    self.on_access(self.access)
                self._paint_choices()
                done(r)
                self.refresh()
            self.post(finish)
        threading.Thread(target=work, daemon=True, name="access-action").start()
        return True

    def _detect(self):
        def done(r):
            self.say("" if r.get("ok", True) else str(r.get("message") or "无法刷新"), bool(r.get("ok", True)))
        self._call(self.client.access_status, done)

    def _test(self):
        pid = self.selected or self.default or "claude"
        fn = getattr(self.client, "test_provider", None)
        if fn is None or self.legacy:
            if pid != "claude":
                self._result(pid, "后台服务版本过旧，不支持此操作", False)
                return
            call = self.client.test_access
        else:
            call = lambda: fn(pid)  # noqa: E731

        def done(r):
            if r.get("ok"):
                secs = r.get("seconds")
                self._result(pid, f"连接正常（{float(secs):.1f} 秒）" if secs else "连接正常")
            else:
                self._result(pid, str(r.get("message") or "连接失败"), False)
        self._result(pid, "")
        self._call(call, done, self.btn_test, "正在测试…")

    def _save(self, pid: str):
        row = self.rows[pid]
        key = row["entry"].value()
        if not key or self.busy:
            return
        if row["prefix"] and not key.startswith(row["prefix"]):
            self._result(pid, f"API key 格式不正确，应以 {row['prefix']} 开头", False)
            return
        fn = getattr(self.client, "set_provider_key", None)
        if fn is None or self.legacy:
            if pid != "claude":
                self._result(pid, "后台服务版本过旧，不支持此操作", False)
                return
            call = lambda: self.client.set_api_key(key)  # noqa: E731
        else:
            call = lambda: fn(pid, key, switch=True)  # noqa: E731  # saving under a service also makes it answer
        before = self.default

        def done(r):
            stored = bool(r.get("ok") or (r.get("provider") or {}).get("configured"))
            if stored:
                row["entry"].clear()  # never keep it around once it is stored
                moved = str(r.get("default") or "") == pid and before != pid
                self._result(pid, f"已保存，已切换到 {self._vname(pid)}" if moved else "已保存")
            else:
                self._result(pid, str(r.get("message") or "保存失败"), False)
        self._call(call, done, row["save"])

    def _delete(self, pid: str):
        if self.busy or not (self._vendor(pid) or {}).get("configured"):
            return
        if not confirm(self.winfo_toplevel(), "删除 API key", f"删除 {self._vname(pid)} 的 API key？"):
            return
        fn = getattr(self.client, "clear_provider_key", None)
        if fn is None or self.legacy:
            if pid != "claude":
                self._result(pid, "后台服务版本过旧，不支持此操作", False)
                return
            call = self.client.clear_api_key
        else:
            call = lambda: fn(pid)  # noqa: E731

        def done(r):
            self._result(pid, "" if r.get("ok") else str(r.get("message") or "删除失败"), bool(r.get("ok")))
        self.rows[pid]["entry"].clear()
        self._call(call, done)

    def _login(self, pid: str):
        """登录…: the service's own program logs in (claude auth login / codex login, run by the background service,
        7b-1). It waits for the browser, so only this button is greyed meanwhile, not the whole page."""
        fn = getattr(self.client, "login_provider", None)
        if fn is None or self.legacy:
            if pid == "claude":
                self._open_login()  # a service without login_provider: open a terminal with claude auth login
            else:
                self._result(pid, "后台服务版本过旧，不支持此操作", False)
            return
        if pid in self._logging_in or self.busy:
            return
        self._logging_in.add(pid)
        login = self.rows[pid].get("login")
        if login is not None:
            style_button(login, self.theme, "disabled")
            login.configure(text="正在登录…")
        self._result(pid, "请在打开的浏览器中完成登录")

        def work():
            try:
                r = fn(pid, "browser")
                if not isinstance(r, dict):
                    r = {"ok": False, "message": str(r)}
            except Exception as e:
                log.warning("login %s failed: %s", pid, e)
                r = {"ok": False, "message": str(e) or "后台服务未响应"}

            def finish():
                if isinstance(r.get("access"), dict):
                    self.access = dict(r["access"])
                    self.on_access(self.access)
                if r.get("code") and r.get("url"):
                    self._result(pid, f"在 {r['url']} 输入 {r['code']}")
                    self.on_say(f"请在手机或浏览器上打开 {r['url']}，输入 {r['code']}")
                if r.get("ok") and r.get("pending"):  # the login goes on in the browser: watch for it to finish
                    self._login_pending = True  # coming back to the window re-reads the state at once
                    self._watch_login(pid, 100)  # every 3 s, for up to 5 minutes
                    if not r.get("code"):
                        self._result(pid, "请在打开的浏览器中完成登录")
                    return
                self._end_login(pid)
                if r.get("ok"):
                    self._result(pid, "已登录")
                elif not r.get("code"):
                    self._result(pid, str(r.get("message") or "登录失败"), False)
                self.refresh()
            self.post(finish)
        threading.Thread(target=work, daemon=True, name="access-login").start()

    def _end_login(self, pid: str) -> None:
        self._logging_in.discard(pid)
        if pid in self.rows and self.rows[pid].get("login") is not None:
            self.rows[pid]["login"].configure(text="登录…")
        self._paint_choices()

    def _watch_login(self, pid: str, tries: int) -> None:
        """Re-read the services until this one reports logged_in, the tries run out, or the page went away."""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        v = self._vendor(pid) or {}
        if v.get("login_state") == "logged_in":
            self._end_login(pid)
            self._result(pid, "已登录")
            return
        if tries <= 0:
            self._end_login(pid)
            self._result(pid, "没有等到登录完成；登录好了点“刷新”", False)
            return
        self.refresh()
        self.after(3000, lambda: self._watch_login(pid, tries - 1))

    def _open_login(self):
        exe = shutil.which("claude")
        if not exe:
            webbrowser.open(INSTALL_URL)
            self._result("claude", "未找到 Claude Code，已打开安装页面")
            return
        try:
            subprocess.Popen(["cmd", "/k", f'"{exe}" auth login'], creationflags=CREATE_NEW_CONSOLE)
            self._login_pending = True  # coming back to the window re-reads the state
            self._result("claude", "已打开登录终端")
        except Exception as e:
            self._result("claude", f"无法打开终端：{e}", False)

    def _unbind_focus(self, event):
        """This page is rebuilt on every font change: its window-wide focus hook goes with it."""
        if event.widget is self and self._focus_bind:
            try:
                self.winfo_toplevel().unbind("<FocusIn>", self._focus_bind)
            except tk.TclError:
                pass
            self._focus_bind = None

    def _back_in_window(self, event):
        try:
            if self._login_pending and self.winfo_exists() and event.widget is self.winfo_toplevel():
                self._login_pending = False
                self._detect()
        except tk.TclError:  # this page was rebuilt (font change) and the old one is gone
            pass
