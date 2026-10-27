"""Token use and the latest quota snapshot, kept in ~/.voice-claude/usage.json.

Per day and per provider: turns, input / output / cache-read / cache-write tokens and, for Claude, the cost Claude
Code reports (list prices; with a subscription it is what the tokens would cost, not a bill). About 90 days are kept,
plus the latest rate-limit snapshot per provider. Written whole or not at all, under a lock (two threads saving at
once once left lines.json unreadable)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

KEEP_DAYS = 90
FIELDS = ("input", "output", "cache_read", "cache_write", "web_search")
KEEP_BASELINES = 300  # sessions whose last running totals are kept (a resumed one must not count its old spend again)


def empty() -> dict:
    return {"turns": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "web_search": 0, "cost_usd": 0.0}


def write_json_atomic(path: Path, data: dict) -> None:
    """A temp file in the same folder, then swapped in (retried while another program has the file open)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


class Ledger:
    def __init__(self, path: Path, now: Callable[[], float] = time.time):
        self.path, self.now = path, now
        self.lock = threading.Lock()
        self.data = self._load()
        self.session: dict[str, dict] = {}  # since this service started

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text("utf-8"))
            if isinstance(d, dict):
                return {"days": dict(d.get("days") or {}), "limits": dict(d.get("limits") or {}),
                        "baselines": dict(d.get("baselines") or {})}
        except (OSError, ValueError):
            pass
        return {"days": {}, "limits": {}, "baselines": {}}

    def _day(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.now()))

    def _save(self) -> None:
        try:
            write_json_atomic(self.path, self.data)
        except OSError:
            log.exception("could not save usage.json")

    def add(self, provider: str, tokens: dict, cost_usd: float | None) -> None:
        with self.lock:
            day = self.data["days"].setdefault(self._day(), {})
            for bucket in (day.setdefault(provider, empty()), self.session.setdefault(provider, empty())):
                bucket["turns"] += 1
                for k in FIELDS:
                    bucket[k] = int(bucket.get(k) or 0) + int(tokens.get(k) or 0)
                if cost_usd is not None:
                    bucket["cost_usd"] = round(float(bucket.get("cost_usd") or 0) + float(cost_usd), 6)
            cutoff = time.strftime("%Y-%m-%d", time.localtime(self.now() - KEEP_DAYS * 86400))
            for old in [d for d in self.data["days"] if d < cutoff]:
                del self.data["days"][old]
            self._save()

    def set_baseline(self, session_id: str, baseline: dict) -> None:
        """A session's last running totals (Claude Code's modelUsage / total_cost_usd), for when it is resumed."""
        if not session_id or not isinstance(baseline, dict):
            return
        with self.lock:
            b = self.data.setdefault("baselines", {})
            b[session_id] = {**baseline, "time": self.now()}
            for old in sorted(b, key=lambda s: b[s].get("time", 0))[:-KEEP_BASELINES]:
                del b[old]
            self._save()

    def baseline(self, session_id: str) -> dict | None:
        with self.lock:
            b = self.data.get("baselines", {}).get(session_id)
            return json.loads(json.dumps(b)) if b else None

    def set_limits(self, provider: str, snapshot: dict) -> None:
        with self.lock:
            self.data["limits"][provider] = snapshot
            self._save()

    def today(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data["days"].get(self._day(), {})))

    def since_start(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.session))

    def limits(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data["limits"]))


# ---------- words ----------

WINDOW_NAMES = {"five_hour": "5 小时", "seven_day": "这周", "seven_day_opus": "这周 Opus", "seven_day_sonnet": "这周 Sonnet",
                "overage": "超额"}


def fmt_tokens(n: int) -> str:
    return f"{n / 10000:.1f} 万".replace(".0 万", " 万") if n >= 10000 else str(n)


def fmt_reset(epoch: float | None, now: float) -> str:
    if not epoch:
        return ""
    t, today = time.localtime(epoch), time.localtime(now)
    if (t.tm_year, t.tm_yday) == (today.tm_year, today.tm_yday):
        return time.strftime("%H:%M", t)
    return f"{t.tm_mon}月{t.tm_mday}日 {time.strftime('%H:%M', t)}"


def seconds(v) -> float | None:
    """resetsAt as Unix seconds (milliseconds are folded)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v / 1000 if v > 1e12 else v


def snapshot(info: dict, now: float) -> dict:
    """Claude Code's rate_limit_info as the "limits" event: status, every window it sends (used fraction, reset
    time) and the overage state."""
    windows = {}
    for name, w in (info.get("unifiedWindows") or {}).items():
        if isinstance(w, dict):
            windows[str(name)] = {"used": w.get("utilization"), "resets_at": seconds(w.get("resetsAt"))}
    kind = info.get("rateLimitType")
    if kind and kind not in windows and info.get("utilization") is not None:
        windows[kind] = {"used": info.get("utilization"), "resets_at": seconds(info.get("resetsAt"))}
    return {"provider": "claude", "status": info.get("status") or "", "type": kind or "",
            "resets_at": seconds(info.get("resetsAt")), "windows": windows,
            "overage": {"status": info.get("overageStatus"), "disabled_reason": info.get("overageDisabledReason"),
                        "in_use": bool(info.get("isUsingOverage"))},
            "time": now}


def limit_notice(snap: dict, now: float) -> str:
    """Said out loud when the status turns to allowed_warning or rejected."""
    w = snap["windows"].get(snap["type"]) or {}
    when = fmt_reset(w.get("resets_at") or snap.get("resets_at"), now)
    if snap["status"] == "rejected":
        return f"Claude 的额度用完了{'，' + when + ' 重置' if when else ''}。"
    name = WINDOW_NAMES.get(snap["type"], "")
    used = f"已用 {round(float(w['used']) * 100)}%" if w.get("used") is not None else ""
    detail = "，".join(x for x in (used, f"{when} 重置" if when else "") if x)
    return f"Claude 的{' ' + name if name else ''}额度快用完了{'（' + detail + '）' if detail else ''}。"


def summary(today: dict, limits: dict, names: dict[str, str], now: float) -> str:
    """The spoken answer to 用量怎么样: Claude's quota windows if known, then today's tokens per provider."""
    parts = []
    snap = limits.get("claude")
    if snap and snap.get("windows"):
        order = sorted(snap["windows"], key=lambda k: (k != "seven_day", k != "five_hour", k))
        said = [f"{WINDOW_NAMES.get(k, k)}{'额度' if k == 'seven_day' else '窗口' if k == 'five_hour' else '额度'}用了 "
                f"{round(float(w['used']) * 100)}%" for k in order for w in [snap["windows"][k]] if w.get("used") is not None]
        if said:
            parts.append("Claude " + "，".join(said) + "。")
    used = [(p, t) for p, t in today.items() if t.get("turns")]
    for p, t in used:
        who = "" if len(used) == 1 else names.get(p, p)
        cache = int(t.get("cache_read") or 0) + int(t.get("cache_write") or 0)
        inp = int(t["input"]) + cache  # what went in, as the window shows it (the cache is most of it on long chats)
        mostly = "（多是缓存）" if cache * 2 > inp else ""
        parts.append(f"{who}今天用了 {t['turns']} 轮，输入 {fmt_tokens(inp)}{mostly}、输出 {fmt_tokens(int(t['output']))} token。")
    return "".join(parts) or "今天还没有用量记录。"
