"""The mechanical risk check behind 通透模式 (transparent mode): does a request delete something, or would it
overwrite a file that already exists? Those two are still asked out loud when the mode is on, and so is a command
that starts another Claude Code (it could message the user's other sessions with no gate); everything else whose
permission is "ask" goes through. It is code, not the model's say-so, and it errs toward asking.

Inputs: the permissions-table action, the spoken question and the shown detail, plus `facts` when the tool that
asked could state them (desktop: kind/keys/text/target; shell: command; Codex: file changes). With no facts
the texts themselves are scanned."""
from __future__ import annotations

import base64
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
# A second Claude Code started by a command has none of voice-claude's gates: its SendMessage reaches the user's
# other sessions unasked. A command that starts one is asked like sending to another session, in 通透模式 too.
CLAUDE = "claude"
# The broad rule (the user's choice): the word claude on its own anywhere in a command line, or in text typed as a
# command, is asked like a message to another session. No shell parsing: a commit message or a search that mentions
# Claude may be asked too, and that is accepted. Not counted: a folder (claude\ or claude/), part of a longer name
# (voice-claude, claude-notes, .claude, claude.json, claude_tools, claude.ai). Also seen through: an encoded
# PowerShell command (decoded; one that does not decode is asked), and ^ / backtick / quote escapes (c^laude).
_CLAUDE_WORD = re.compile(r"(?<![\w.\-])claude(?:\.exe|\.cmd|\.ps1|\.bat|\.lnk)?\.*(?![\w\\/.\-])"  # the program on its own (@claude,
                          # claude. too: cmd drops trailing dots)
                          r"|(?<![\w.\-])claude[\\/]versions[\\/]\d"  # its versioned binary
                          r"|@anthropic-ai[\\/]claude-(?:code|agent-sdk)(?![\w.\-])"  # the npm packages
                          r"|\bclaude_(?:agent|code)_sdk\b", re.I)  # the Python SDKs
# a flag and its value; PowerShell takes -, /, and the en / em dashes and the horizontal bar for its flags
_DASHES = "-/\u2013\u2014\u2015"
_ENCODED_FLAG = re.compile(rf"(?:^|\s)([{_DASHES}][a-z]+)(?=(?:\s+|[:=])[\"']?([^\s\"'{_DASHES}][^\s\"']*))", re.I)
# Typed into something that is not a terminal or a launch box: only text that is itself an invocation, or a command
# line that starts with a launcher
TYPED_CLAUDE = re.compile(r"^\s*[\"']?(?:[^\s\"']*[\\/])?claude(?:(?:\.exe|\.cmd|\.ps1)[\"']?(?=$|\s)|[\"']?(?=\s+(?:--?\w|[\"'])|\s*$))", re.I)
TERMINAL = re.compile(r"powershell|pwsh|cmd\.exe|命令提示符|command prompt|terminal|终端|命令行|bash|wsl|ubuntu|mingw|msys|cygwin|"
                      r"visual studio code|vs code|cursor|pycharm|intellij|webstorm|windsurf|alacritty|wezterm|conemu|cmder|tabby"
                      r"|^[\w.-]+@[\w.-]+:", re.I)  # a shell prompt as the title: alex@PC: ~/proj
# boxes that run what is typed into them: Win+R's Run box (弹出的窗口 is what desktop.py calls a window a shortcut
# is about to bring up), the Explorer address bar, Start search
LAUNCH_BOX = re.compile(r"^(?:运行|run)$|弹出的窗口|文件资源管理器|file explorer|^(?:搜索|search)$", re.I)
TERMINAL_IMAGES = {"windowsterminal.exe", "windowsterminalpreview.exe", "openconsole.exe", "conhost.exe", "cmd.exe",
                   "powershell.exe", "pwsh.exe", "mintty.exe", "alacritty.exe", "wezterm-gui.exe", "wezterm.exe", "wsl.exe",
                   "wslhost.exe", "bash.exe", "ubuntu.exe", "hyper.exe", "tabby.exe", "conemu.exe", "conemu64.exe",
                   "cmder.exe"}
# editors with a terminal inside (what is typed may be a command; Claude Code may run in them)
EDITOR_IMAGES = {"code.exe", "code - insiders.exe", "cursor.exe", "windsurf.exe", "vscodium.exe", "pycharm.exe"}
_JETBRAINS = re.compile(r"^(?:idea|pycharm|webstorm|phpstorm|clion|goland|rider|rubymine|datagrip|studio)64\.exe$", re.I)


def hosts_commands(image: str) -> bool:
    """A terminal, or an editor with one: what is typed there may run as a command, and Claude Code may run inside."""
    image = (image or "").lower()
    return image in TERMINAL_IMAGES or image in EDITOR_IMAGES or bool(_JETBRAINS.match(image))
LAUNCH_IMAGES = {"explorer.exe", "searchhost.exe", "searchapp.exe", "searchui.exe", "startmenuexperiencehost.exe"}
_LAUNCHERS = {"cmd", "powershell", "pwsh", "bash", "sh", "zsh", "wsl", "wt", "start", "start-process", "saps", "npx",
              "bunx", "pnpx", "node", "python", "py", "call", "conhost", "runas", "mintty", "alacritty", "xterm", "wezterm"}
STARTS_CLAUDE_WHY = "会另开一个 Claude，它给别的会话发消息不会再问你"
# Anything typed, pressed or clicked in a window where a Claude session runs reaches that session: a message to it.
SEND = "send"
SENDS_WHY = "这是另一个 Claude 会话的窗口，在里面打字、按键或点击都会交给它"


def _unescaped(command: str) -> str:
    """The line with what shells drop before they run a word: quotes ("c"laude, "cl"'a'"ude"), cmd's ^ and PowerShell's
    backtick escapes, and bash's backslash between letters (cl\\aude)."""
    t = re.sub(r"[\"'^`]", "", command)
    return re.sub(r"(?<=\w)\\(?=\w)", "", t)


def _decoded(command: str) -> list[str] | None:
    """What a PowerShell -EncodedCommand (-e, -ec, -en...) in the line says; None when one does not decode."""
    if not re.search(r"\b(?:powershell|pwsh)\b", command, re.I):
        return []
    out = []
    for m in _ENCODED_FLAG.finditer(command):
        flag, data = "-" + m.group(1)[1:].lower(), m.group(2)
        if flag in ("-e", "-ec") or (len(flag) >= 3 and "-encodedcommand".startswith(flag)):
            try:
                out.append(base64.b64decode(data + "=" * (-len(data) % 4), validate=True).decode("utf-16-le"))
            except ValueError:
                return None
    return out


def starts_claude(command: str, depth: int = 0) -> bool:
    """Could this command line start Claude Code (a second one, with none of voice-claude's gates)?"""
    cmd = command.decode("utf-8", "replace") if isinstance(command, (bytes, bytearray)) else str(command or "")
    if _CLAUDE_WORD.search(cmd) or _CLAUDE_WORD.search(_unescaped(cmd)):
        return True
    decoded = _decoded(cmd)
    if decoded is None:
        return True  # an encoded command that cannot be read: asked
    return depth < 4 and any(starts_claude(d, depth + 1) for d in decoded)


def _first_program(text: str) -> str:
    first = (text.strip().split() or [""])[0].strip("\"'")
    return re.sub(r"\.(?:exe|cmd|bat|ps1|com)$", "", re.split(r"[\\/]", first)[-1].lower())


def types_claude(text: str, window: str = "", program: str = "") -> bool:
    """Typing this would start Claude Code: a command line typed into a terminal, a launch box (Run, Explorer's address
    bar, Start search) or an editor that has a terminal; elsewhere the bare invocation, or a launcher's command line."""
    text, image, title = text or "", (program or "").lower(), (window or "").strip()
    if hosts_commands(image) or image in LAUNCH_IMAGES or TERMINAL.search(title) or LAUNCH_BOX.search(title):
        return starts_claude(text)
    if _first_program(text) in _LAUNCHERS:
        return starts_claude(text)
    return bool(TYPED_CLAUDE.match(text))


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
    if starts_claude(cmd):
        return CLAUDE, STARTS_CLAUDE_WHY
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
    if kind == "job_stop":  # stopping a background job starts nothing, whatever its command was
        return None, ""
    if kind in ("type", "hotkey", "click") and facts.get("session"):
        return SEND, SENDS_WHY
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
        if types_claude(text, str(facts.get("window") or ""), str(facts.get("program") or "")):
            return CLAUDE, f"{STARTS_CLAUDE_WHY}（输入的是启动 Claude 的命令）"
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
    if kind == "open_app" and starts_claude(str(facts.get("command") or "")):
        return CLAUDE, STARTS_CLAUDE_WHY  # a name in [desktop.apps] whose command is Claude Code
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
    for kind, why in ((SEND, SENDS_WHY), (CLAUDE, STARTS_CLAUDE_WHY)):
        if any(k == kind for _, k, _ in risky):
            rest = [r for r in risky if r[1] != kind]
            return why + (f"，并{batch_reason(rest)}" if rest else "")
    kinds = {k for _, k, _ in risky}
    names = sorted({w[len(OVERWRITES):].strip() for _, k, w in risky if k == OVERWRITE and w.startswith(OVERWRITES + " ")})
    sure = any(k == OVERWRITE and w.startswith(OVERWRITES) for _, k, w in risky)
    over = (OVERWRITES if sure else "可能覆盖已有文件") + (f" {names[0]}" if len(names) == 1 else "")
    if DELETE in kinds and OVERWRITE in kinds:
        return f"删除，并{over}"
    return "删除" if DELETE in kinds else over
