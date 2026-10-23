"""The project index the manager works from: generated from Claude Code's own ~/.claude/projects, plus the
user's aliases in ~/.voice-claude/projects.toml. Nothing here needs to be maintained by hand."""
from __future__ import annotations

import json
import logging
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

SKIP = ("scratch-workspaces", "Temp-claude")
_MEMORY_LINE = re.compile(r"^\s*-\s*\[([^\]]+)\]\([^)]*\)\s*(?:[—\-–:：]+\s*)?(.*)$")
_PUNCT = re.compile(r"[\s。！？!?，,、.　_\-]+")


@dataclass
class Project:
    key: str  # normalised cwd, the identity
    name: str  # directory name
    display_name: str  # name made unique among the projects ("Atlas（E 盘）")
    cwd: str
    topic: str = ""
    last_active: float = 0.0
    aliases: list[str] = field(default_factory=list)
    session_id: str = ""  # the session the voice app used last for this project ("" = start fresh)
    session_engine: str = ""  # whose conversation that is: "claude" (Claude Code) or "codex"; "" = unknown
    session_fork: list | None = None  # a rewind not made yet: [last entry kept, dropped prompt or None] (Claude Code)


def transcript_tail(session_id: str, projects_dir: Path | None = None) -> str:
    """The uuid of the last entry of a Claude Code conversation's transcript ('' when it cannot be read): where a
    resumed conversation stood before this process's first message, so a rewind can go back to it."""
    if not session_id:
        return ""
    base = projects_dir or paths.CLAUDE_PROJECTS_DIR
    try:
        f = next(base.glob(f"*/{session_id}.jsonl"), None)
        if f is None:
            return ""
        for line in reversed(f.read_text("utf-8", errors="replace").splitlines()[-400:]):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and d.get("uuid"):
                return str(d["uuid"])
    except OSError:
        pass
    return ""


def engine_of_session(session_id: str) -> str:
    """For a record saved before the engine was stored: Codex thread ids are UUID version 7, Claude Code session
    ids version 4. '' when it is neither."""
    import uuid
    try:
        v = uuid.UUID(str(session_id)).version
    except ValueError:
        return ""
    return {7: "codex", 4: "claude"}.get(v, "")


def norm_key(cwd: str) -> str:
    return str(Path(cwd)).replace("/", "\\").rstrip("\\").lower()


# ---------- reading Claude Code's project store ----------

def cwd_from_transcript(jsonl: Path, max_lines: int = 50) -> str:
    try:
        with jsonl.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict) and isinstance(d.get("cwd"), str) and d["cwd"]:
                    return d["cwd"]
    except OSError:
        pass
    return ""


_SEPARATORS = ("\\", "-", "_", " ")


def cwd_from_slug(slug: str, exists=lambda p: Path(p).exists()) -> str:
    """'C--Users-u-my-proj' -> 'C:\\Users\\u\\my-proj'. Claude Code turns every non-alphanumeric character into
    '-', so each '-' may have been '\\', '-', '_' or ' '. Walk the possibilities directory by directory, only going
    deeper where the prefix really exists on disk, and return the first full path that exists."""
    m = re.match(r"^([A-Za-z])--(.*)$", slug)
    if not m:
        return ""
    drive, rest = m.group(1).upper(), m.group(2)
    tokens = rest.split("-")
    budget = [20000]

    def walk(prefix: str, i: int, cur: str) -> str:
        budget[0] -= 1
        if budget[0] < 0:
            return ""
        if i == len(tokens):
            full = prefix + cur
            return full if exists(full) else ""
        for sep in _SEPARATORS:
            if sep == "\\":
                if not exists(prefix + cur):
                    continue
                found = walk(prefix + cur + "\\", i + 1, tokens[i])
            else:
                found = walk(prefix, i + 1, cur + sep + tokens[i])
            if found:
                return found
        return ""

    return walk(f"{drive}:\\", 1, tokens[0]) if tokens else ""


def topic_from_memory(memory_md: Path) -> str:
    try:
        text = memory_md.read_text("utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        m = _MEMORY_LINE.match(line)
        if m:
            title, desc = m.group(1).strip(), m.group(2).strip()
            return f"{title} — {desc}" if desc else title
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s
    return ""


def load_aliases(aliases_file: Path) -> dict[str, str]:
    """alias -> cwd (user-editable TOML: [aliases] "服务器" = "E:\\Atlas")."""
    try:
        data = tomllib.loads(aliases_file.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.get("aliases", {}).items()}


def save_aliases(aliases_file: Path, aliases: dict[str, str]) -> None:
    def lit(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    lines = ["# voice-claude 的项目别名：左边是你叫它的名字，右边是项目目录。总机会自动往这里加；也可以手改。", "", "[aliases]"]
    lines += [f"{lit(k)} = {lit(v)}" for k, v in aliases.items()]
    aliases_file.parent.mkdir(parents=True, exist_ok=True)
    aliases_file.write_text("\n".join(lines) + "\n", "utf-8")


def remember_alias(aliases_file: Path, alias: str, cwd: str) -> None:
    aliases = load_aliases(aliases_file)
    aliases[alias.strip()] = cwd
    save_aliases(aliases_file, aliases)


def load_lines(lines_file: Path) -> dict[str, dict]:
    try:
        return json.loads(lines_file.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def scan(claude_projects_dir: Path = paths.CLAUDE_PROJECTS_DIR, aliases_file: Path = paths.PROJECTS_FILE,
         lines_file: Path = paths.LINES_FILE, exists=lambda p: Path(p).is_dir()) -> list[Project]:
    """Every project Claude Code knows about whose directory still exists, newest first."""
    found: dict[str, Project] = {}
    if claude_projects_dir.is_dir():
        for d in claude_projects_dir.iterdir():
            if not d.is_dir() or any(s in d.name for s in SKIP):
                continue
            jsonls = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not jsonls:
                continue
            cwd = ""
            for jsonl in jsonls[:3]:  # a session may have started in a scratch workspace and moved on
                c = cwd_from_transcript(jsonl)
                if c and not any(s in c for s in SKIP):
                    cwd = c
                    break
            cwd = cwd or cwd_from_slug(d.name, exists=exists)
            if not cwd or any(s in cwd for s in SKIP) or not exists(cwd):
                continue
            key = norm_key(cwd)
            p = Project(key=key, name=Path(cwd).name or cwd, display_name="", cwd=cwd,
                        topic=topic_from_memory(d / "memory" / "MEMORY.md"), last_active=jsonls[0].stat().st_mtime)
            if key not in found or found[key].last_active < p.last_active:
                found[key] = p
    # aliases may name projects Claude Code has not seen yet
    aliases = load_aliases(aliases_file)
    for alias, cwd in aliases.items():
        key = norm_key(cwd)
        if key not in found and exists(cwd):
            found[key] = Project(key=key, name=Path(cwd).name or cwd, display_name="", cwd=cwd)
        if key in found:
            found[key].aliases.append(alias)
    for key, rec in load_lines(lines_file).items():
        if key in found and isinstance(rec, dict):
            found[key].session_id = str(rec.get("session_id", ""))
            found[key].session_engine = str(rec.get("engine") or engine_of_session(found[key].session_id))
            fork = rec.get("fork_at")
            found[key].session_fork = list(fork) if isinstance(fork, list) and len(fork) == 2 and fork[0] else None
    projects = sorted(found.values(), key=lambda p: p.last_active, reverse=True)
    _disambiguate(projects)
    return projects


def _disambiguate(projects: list[Project]) -> None:
    by_name: dict[str, list[Project]] = {}
    for p in projects:
        by_name.setdefault(p.name.lower(), []).append(p)
    for group in by_name.values():
        if len(group) == 1:
            group[0].display_name = group[0].name
            continue
        drives = {Path(p.cwd).drive for p in group}
        for p in group:
            where = f"{Path(p.cwd).drive.rstrip(':')} 盘" if len(drives) == len(group) else (Path(p.cwd).parent.name or Path(p.cwd).drive)
            p.display_name = f"{p.name}（{where}）"


# ---------- finding a project from what the user said ----------

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM = re.compile(r"[零〇一二两三四五六七八九十]+")


def _cn_number(s: str) -> str:
    """'十七' -> '17', '二十三' -> '23', '一七' -> '17' (digit by digit when there is no 十)."""
    if "十" not in s:
        return "".join(str(_CN_DIGITS.get(ch, "")) for ch in s)
    tens, _, ones = s.partition("十")
    t = _CN_DIGITS.get(tens, 1) if tens else 1
    o = _CN_DIGITS.get(ones, 0) if ones else 0
    return str(t * 10 + o)


def _norm(s: str) -> str:
    s = _CN_NUM.sub(lambda m: _cn_number(m.group(0)), s)
    return _PUNCT.sub("", s).lower()


def resolve(text: str, projects: list[Project]) -> list[Project]:
    """Candidates for a spoken name: exact alias > exact name > display name/name contained > topic contained."""
    t = _norm(text)
    if not t:
        return []
    for p in projects:
        if any(_norm(a) == t for a in p.aliases):
            return [p]
    exact = [p for p in projects if _norm(p.name) == t or _norm(p.display_name) == t]
    if exact:
        return exact
    contained = [p for p in projects if _norm(p.name) in t or t in _norm(p.display_name)
                 or any(_norm(a) in t or t in _norm(a) for a in p.aliases)]
    if contained:
        return contained
    return [p for p in projects if t in _norm(p.topic)]


def index_text(projects: list[Project], now: float | None = None) -> str:
    """The list the manager reads: one line per project."""
    now = time.time() if now is None else now
    out = []
    for p in projects:
        age = ""
        if p.last_active:
            days = int((now - p.last_active) // 86400)
            age = "今天" if days == 0 else f"{days} 天前"
        bits = [p.display_name, p.cwd]
        if age:
            bits.append(f"最近 {age}")
        if p.aliases:
            bits.append("别名 " + "、".join(p.aliases))
        if p.topic:
            bits.append(p.topic[:60])
        out.append("- " + "｜".join(bits))
    return "\n".join(out) if out else "（还没有项目）"
