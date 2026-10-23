"""The mechanical risk check behind 通透模式 (transparent mode): does a request delete something, or would it
overwrite a file that already exists? Those two are still asked out loud when the mode is on; everything else
whose permission is "ask" goes through. It is code, not the model's say-so, and it errs toward asking.

Inputs: the permissions-table action, the spoken question and the shown detail, plus `facts` when the tool that
asked could state them (desktop: kind/keys/text/target; shell: command; Codex: file changes). With no facts
the texts themselves are scanned."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

DELETE = "delete"
OVERWRITE = "overwrite"

DELETE_WORDS_CJK = ("删除", "删掉", "移除", "清空", "废纸篓", "回收站", "卸载", "格式化")
DELETE_WORDS_EN = re.compile(r"\b(delete|remove|trash|discard|erase|uninstall|format)\b", re.I)
OVERWRITE_WORDS_CJK = ("替换", "覆盖")
OVERWRITE_WORDS_EN = re.compile(r"\b(replace|overwrite)\b", re.I)

SHELL_DELETE = re.compile(
    r"(?<![\w.-])(rm|rmdir|rd|del|erase|remove-item|ri|clear-content|format|diskpart)(?![\w.-])"
    r"|\bgit\s+clean\b|\bgit\s+reset\s+--hard\b|\bgit\s+push\b[^|&;\n]*?\s(-f|--force)(?![\w-])|\bgit\s+branch\s+-D\b"
    r"|\b(drop|truncate)\s+(table|database|schema|index|view)\b|\bdelete\s+from\b", re.I)
SHELL_OVERWRITE_VERBS = re.compile(r"(?<![\w.-])(set-content|out-file|cp|copy|copy-item|mv|move|move-item|xcopy|robocopy|ren|rename-item)(?![\w.-])", re.I)
REDIRECT = re.compile(r"(?<![>&\d])>(?!>)\s*(\"[^\"]+\"|'[^']+'|\S+)")
PATH_TOKEN = re.compile(r"\"([^\"]+)\"|'([^']+)'|(\S+)")
PATH_LIKE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/)")
# A plain Save As dialog: saving there overwrites nothing by itself (Windows asks before replacing a file), so it
# passes; only a bare name typed into it that exists in its folder counts. Matched on the window title.
SAVE_DIALOG = re.compile(r"另存为|保存为|save\s+as", re.I)
# Windows' "replace it?" confirmation: typing, keys and clicks there (other than cancelling) do overwrite.
REPLACE_DIALOG = re.compile(r"确认另存为|替换或跳过|文件替换|已存在|confirm\s+save|replace\s+or\s+skip|file\s+replace|"
                            r"already\s+exists", re.I)
CANCEL_WORDS = re.compile(r"否|取消|不要|跳过|\b(no|cancel|skip)\b", re.I)
OVERWRITES = "会覆盖已有文件"


def _words_delete(text: str) -> bool:
    return any(w in text for w in DELETE_WORDS_CJK) or bool(DELETE_WORDS_EN.search(text))


def _words_overwrite(text: str) -> bool:
    return any(w in text for w in OVERWRITE_WORDS_CJK) or bool(OVERWRITE_WORDS_EN.search(text))


def _tokens(command: str) -> list[str]:
    return [a or b or c for a, b, c in PATH_TOKEN.findall(command)]


def _clean(token: str) -> str:
    return token.strip().strip("\"'").rstrip(";,)")


def shell(command: str, exists: Callable[[str], bool] | None = None) -> tuple[str | None, str]:
    """A shell / PowerShell command: delete verbs, or writing onto a path that already exists."""
    exists = exists or os.path.exists  # looked up per call, so a test can patch os.path.exists
    cmd = command or ""
    if SHELL_DELETE.search(cmd):
        return DELETE, "删除"
    m = REDIRECT.search(cmd)
    if m:
        target = _clean(m.group(1))
        if PATH_LIKE.match(target) or os.sep in target or "/" in target or "." in target:
            if exists(os.path.expanduser(os.path.expandvars(target))):
                return OVERWRITE, f"会覆盖已有文件 {target}"
            if not PATH_LIKE.match(target):
                return OVERWRITE, f"可能覆盖已有文件 {target}"  # relative: cannot be sure from here
    if SHELL_OVERWRITE_VERBS.search(cmd):
        toks = [_clean(t) for t in _tokens(cmd)]
        paths = [t for t in toks if PATH_LIKE.match(t) and not t.startswith("-")]
        if paths:
            target = paths[-1]
            if exists(os.path.expanduser(os.path.expandvars(target))):
                return OVERWRITE, f"会覆盖已有文件 {target}"
            return None, ""
        return OVERWRITE, "可能覆盖已有文件"  # copy / move with relative names: cannot be sure from here
    return None, ""


MODIFIERS = {"ctrlleft": "ctrl", "ctrlright": "ctrl", "control": "ctrl", "shiftleft": "shift", "shiftright": "shift",
             "altleft": "alt", "altright": "alt", "option": "alt", "winleft": "win", "winright": "win", "command": "win",
             "del": "delete"}


def keys_of(facts: dict) -> list[str]:
    """The keys of a step with left / right modifier names folded ("ctrlleft+d" is Ctrl+D)."""
    return [MODIFIERS.get(str(k).lower(), str(k).lower()) for k in facts.get("keys") or []]


def in_replace_dialog(title: str) -> bool:
    return bool(REPLACE_DIALOG.search(title or ""))


def _typed_name_exists(facts: dict, text: str, exists: Callable[[str], bool]) -> str:
    """A bare file name typed into a Save As dialog whose folder the desktop tool could read: the name if that file
    is there already, else ''."""
    folder = str(facts.get("folder") or "")
    name = text.strip().strip('"')
    if not folder or not name or any(c in name for c in '\\/:\n') or not SAVE_DIALOG.search(str(facts.get("window") or "")):
        return ""
    return name if exists(os.path.join(folder, name)) else ""


def classify(action: str, question: str = "", detail: str = "", facts: dict | None = None,
             exists: Callable[[str], bool] | None = None) -> tuple[str | None, str]:
    """(DELETE | OVERWRITE | None, the reason to say first). Facts win; otherwise the texts are scanned."""
    exists = exists or os.path.exists
    facts = facts or {}
    kind = str(facts.get("kind") or "")
    if kind == "hotkey":
        keys = keys_of(facts)
        if "delete" in keys or set(keys) == {"ctrl", "d"}:
            return DELETE, "删除"
        if in_replace_dialog(str(facts.get("window") or "")) and set(keys) not in (
                {"esc"}, {"tab"}, {"shift", "tab"}, {"n"}, {"alt", "n"}, {"left"}, {"right"}):
            return OVERWRITE, OVERWRITES
        return None, ""
    if kind == "type":
        text = str(facts.get("text") or "")
        if SHELL_DELETE.search(text):
            return DELETE, "删除（输入的是删除命令）"
        t = text.strip().strip('"')
        if PATH_LIKE.match(t) and "\n" not in t and exists(os.path.expanduser(t)) and not Path(os.path.expanduser(t)).is_dir():
            return OVERWRITE, f"{OVERWRITES} {Path(t).name}"
        if in_replace_dialog(str(facts.get("window") or "")):
            return OVERWRITE, OVERWRITES
        name = _typed_name_exists(facts, text, exists)
        if name:
            return OVERWRITE, f"{OVERWRITES} {name}"
        return None, ""
    if kind == "click":
        target = str(facts.get("target") or "")
        if _words_delete(target):
            return DELETE, "删除"
        if _words_overwrite(target):
            return OVERWRITE, OVERWRITES
        if in_replace_dialog(str(facts.get("window") or "")) and not CANCEL_WORDS.search(target):
            return OVERWRITE, OVERWRITES  # 是 / Yes / 替换 in "replace it?"
        return None, ""
    if kind in ("scroll", "screenshot", "open_app", "open_path"):
        if kind == "open_app" and _words_delete(str(facts.get("name") or "")):
            return DELETE, "删除"
        return None, ""
    if kind == "shell" or action in ("background_job", "codex_shell"):
        command = str(facts.get("command") or detail or question)
        return shell(command, exists)
    if kind == "file_change" or action == "codex_edit":
        for c in facts.get("changes") or []:
            if str(c.get("kind") or "").lower() == "delete":
                return DELETE, "删除文件"
        for c in facts.get("changes") or []:
            k = str(c.get("kind") or "").lower()
            p = str(c.get("path") or "")
            if k in ("update", "move") or (k == "add" and p and exists(p)):
                return OVERWRITE, f"会覆盖已有文件 {Path(p).name or p}"
        if not facts.get("changes"):
            return OVERWRITE, "可能覆盖已有文件"  # a file change we know nothing about: ask
        return None, ""
    if kind == "permissions":
        return None, ""
    # no facts: the texts
    text = f"{question}\n{detail}"
    if _words_delete(text) or SHELL_DELETE.search(detail or ""):
        return DELETE, "删除"
    if _words_overwrite(text):
        return OVERWRITE, "可能覆盖已有文件"
    return None, ""


def classify_steps(actions: list[str], facts: list[dict] | None, detail: str,
                   exists: Callable[[str], bool] | None = None) -> list[tuple[int, str, str]]:
    """For a batch: every risky step as (index, kind, why). Steps without facts fall back to their detail line.
    Saving through a Save As dialog the batch itself opens is not asked: if the name exists, Windows asks "replace
    it?", and the desktop tool never lets a later step of the batch answer that prompt (it stops there)."""
    lines = (detail or "").split("\n")
    out = []
    for i, action in enumerate(actions):
        f = (facts or [])[i] if facts and i < len(facts) else None
        line = lines[i] if i < len(lines) else ""
        kind, why = classify(action, line, "", f, exists)
        if kind:
            out.append((i, kind, why))
    return out


def batch_reason(risky: list[tuple[int, str, str]]) -> str:
    """One short reason for a batch question, said once: "删除" / "会覆盖已有文件 test.txt" / "删除，并会覆盖已有文件"."""
    kinds = {k for _, k, _ in risky}
    names = sorted({w[len(OVERWRITES):].strip() for _, k, w in risky if k == OVERWRITE and w.startswith(OVERWRITES + " ")})
    sure = any(k == OVERWRITE and w.startswith(OVERWRITES) for _, k, w in risky)
    over = (OVERWRITES if sure else "可能覆盖已有文件") + (f" {names[0]}" if len(names) == 1 else "")
    if DELETE in kinds and OVERWRITE in kinds:
        return f"删除，并{over}"
    return "删除" if DELETE in kinds else over
