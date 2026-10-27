"""How the window words token usage and Claude's remaining quota. The service sends the numbers: the "usage" event
after each turn, the "limits" event when Claude Code reports its rate limits, and the "usage" command for totals."""
from __future__ import annotations

import time

PROVIDER_NAMES = {"claude": "Claude", "deepseek": "DeepSeek", "kimi": "Kimi", "openai": "GPT"}
WINDOW_NAMES = {"five_hour": "5 小时额度", "seven_day": "本周额度", "seven_day_opus": "本周 Opus 额度",
                "seven_day_sonnet": "本周 Sonnet 额度"}
WARN_AT = 0.8  # from this share used a window is shown as running out
_WEEKDAYS = "一二三四五六日"


def tokens(n) -> str:
    """1234 -> "1234", 15456 -> "1.5万", 230000000 -> "2.3亿"."""
    n = int(n or 0)
    if n >= 100_000_000:
        return f"{n / 1e8:.1f}".rstrip("0").rstrip(".") + "亿"
    if n >= 10_000:
        return f"{n / 1e4:.1f}".rstrip("0").rstrip(".") + "万"
    return str(n)


def money(usd) -> str:
    if usd is None:
        return ""
    usd = float(usd)
    return f"${usd:.4f}" if 0 < usd < 0.01 else f"${usd:.2f}"


def _input_total(t: dict) -> tuple[int, int]:
    """(all input tokens, of which from the cache): Claude Code counts cached input apart from the rest."""
    cached = int(t.get("cache_read") or 0) + int(t.get("cache_write") or 0)
    return int(t.get("input") or 0) + cached, cached


def turn_line(ev: dict) -> str:
    """The small line under a reply: '· 本轮用量：输入 1.6万（缓存 1.5万） · 输出 65 · 约 $0.0026'."""
    t = ev.get("tokens") or {}
    total_in, cached = _input_total(t)
    parts = [f"输入 {tokens(total_in)}" + (f"（缓存 {tokens(cached)}）" if cached else ""), f"输出 {tokens(t.get('output'))}"]
    if ev.get("cost_usd") is not None:
        parts.append(f"约 {money(ev['cost_usd'])}")
    return "· 本轮用量：" + " · ".join(parts)


def totals_text(tot: dict | None) -> str:
    """'4 轮 · 输入 3.2万 · 输出 1200 · 约 $0.12'."""
    if not tot or not tot.get("turns"):
        return "还没有记录"
    total_in, _ = _input_total(tot)
    s = f"{int(tot['turns'])} 轮 · 输入 {tokens(total_in)} · 输出 {tokens(tot.get('output'))}"
    if tot.get("cost_usd"):
        s += f" · 约 {money(tot['cost_usd'])}"
    return s


def resets(epoch, now: float | None = None) -> str:
    """'今天 18:30' / '明天 09:00' / '周五 10:00'."""
    when, today = time.localtime(float(epoch)), time.localtime(now)
    day = time.mktime((when.tm_year, when.tm_mon, when.tm_mday, 0, 0, 0, 0, 0, -1))
    start = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
    days = round((day - start) / 86400)
    label = "今天" if days == 0 else "明天" if days == 1 else f"周{_WEEKDAYS[when.tm_wday]}"
    return f"{label} {when.tm_hour:02d}:{when.tm_min:02d}"


def limit_rows(snap: dict | None, now: float | None = None) -> list[tuple[str, str, bool]]:
    """(name, '已用 8%，今天 18:30 重置', running out) for each window Claude Code reported."""
    rows, snap = [], snap or {}
    flagged = snap.get("type") if snap.get("status") in ("allowed_warning", "rejected") else None  # Claude's own warning
    for key, w in (snap.get("windows") or {}).items():
        used = (w or {}).get("used")
        if used is None:
            continue
        text = f"已用 {round(float(used) * 100)}%"
        if w.get("resets_at"):
            text += f"，{resets(w['resets_at'], now)}重置"
        rows.append((WINDOW_NAMES.get(key, key), text, float(used) >= WARN_AT or key == flagged))
    return rows


def settings_rows(snapshot: dict | None, claude_method: str = "", now: float | None = None) -> list[tuple[str, str, bool]]:
    """The 用量 group on the settings page: (name, value, highlighted) rows from the "usage" command's reply."""
    snap = snapshot or {}
    rows: list[tuple[str, str, bool]] = []
    for scope, label in (("today", "今天"), ("since_start", "本次启动")):
        per = snap.get(scope) or {}
        if not per:
            rows.append((label, "还没有记录", False))
        for pid in sorted(per, key=lambda p: list(PROVIDER_NAMES).index(p) if p in PROVIDER_NAMES else 99):
            rows.append((f"{label} · {PROVIDER_NAMES.get(pid, pid)}", totals_text(per[pid]), False))
    claude = (snap.get("limits") or {}).get("claude")
    if claude:
        rows += [(f"Claude {name}", text, warn) for name, text, warn in limit_rows(claude, now)]
    elif claude_method == "api_key":
        rows.append(("Claude 余额", "在 console.anthropic.com 查看", False))
    return rows
