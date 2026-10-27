import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from voiceclaude import usage

# what Claude Code sent on the user's login (the rate_limit_event of the first request)
INFO = {"status": "allowed", "resetsAt": 1792760400, "rateLimitType": "five_hour", "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled", "isUsingOverage": False,
        "unifiedWindows": {"five_hour": {"utilization": 0.08, "resetsAt": 1792760400},
                           "seven_day": {"utilization": 0.75, "resetsAt": 1793200000}}}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.clock = [time.mktime((2026, 10, 23, 15, 0, 0, 0, 0, -1))]
        self.path = self.tmp / "usage.json"
        self.ledger = usage.Ledger(self.path, now=lambda: self.clock[0])

    def test_per_day_per_provider_and_since_start(self):
        self.ledger.add("claude", {"input": 1000, "output": 200, "cache_read": 30000, "cache_write": 500}, 0.0123)
        self.ledger.add("claude", {"input": 10, "output": 5}, 0.0001)
        self.ledger.add("deepseek", {"input": 70, "output": 9}, None)
        today = self.ledger.today()
        self.assertEqual(today["claude"], {"turns": 2, "input": 1010, "output": 205, "cache_read": 30000, "cache_write": 500,
                                           "web_search": 0, "cost_usd": 0.0124})
        self.assertEqual((today["deepseek"]["turns"], today["deepseek"]["cost_usd"]), (1, 0.0))
        self.assertEqual(self.ledger.since_start()["claude"]["turns"], 2)
        again = usage.Ledger(self.path, now=lambda: self.clock[0])  # read back from disk
        self.assertEqual(again.today(), today)
        self.assertEqual(again.since_start(), {})  # "since start" is this service run
        self.clock[0] += 91 * 86400  # three months on: old days are dropped
        again.add("claude", {"input": 1}, 0.0)
        self.assertEqual(list(json.loads(self.path.read_text("utf-8"))["days"]), [time.strftime("%Y-%m-%d", time.localtime(self.clock[0]))])

    def test_session_baselines(self):
        self.assertIsNone(self.ledger.baseline("S1"))
        self.ledger.set_baseline("S1", {"models": {"m": {"cost": 2.0}}, "cost": 2.0})
        again = usage.Ledger(self.path, now=lambda: self.clock[0])
        self.assertEqual(again.baseline("S1")["cost"], 2.0)  # kept on disk with the day totals
        for i in range(usage.KEEP_BASELINES + 5):
            self.clock[0] += 1
            again.set_baseline(f"X{i}", {"models": {}, "cost": 0})
        self.assertIsNone(again.baseline("S1"))  # the oldest go first
        self.assertEqual(len(json.loads(self.path.read_text("utf-8"))["baselines"]), usage.KEEP_BASELINES)

    def test_limits_are_kept_and_written_whole(self):
        snap = usage.snapshot(INFO, self.clock[0])
        self.assertEqual(snap["windows"], {"five_hour": {"used": 0.08, "resets_at": 1792760400.0},
                                           "seven_day": {"used": 0.75, "resets_at": 1793200000.0}})
        self.assertEqual(snap["overage"], {"status": "rejected", "disabled_reason": "org_level_disabled", "in_use": False})
        self.assertEqual((snap["status"], snap["provider"]), ("allowed", "claude"))
        errors = []

        def hammer():
            try:
                for _ in range(50):
                    self.ledger.add("claude", {"input": 1}, 0.0)
                    self.ledger.set_limits("claude", snap)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        data = json.loads(self.path.read_text("utf-8"))  # never half a file
        self.assertEqual((errors, data["limits"]["claude"]["status"]), ([], "allowed"))
        self.assertEqual(self.ledger.today()["claude"]["turns"], 200)
        self.assertFalse(list(self.tmp.glob("*.tmp")))


class WordsTests(unittest.TestCase):
    def test_numbers_resets_and_notices(self):
        self.assertEqual((usage.fmt_tokens(1200), usage.fmt_tokens(32000), usage.fmt_tokens(30000)), ("1200", "3.2 万", "3 万"))
        now = time.mktime((2026, 10, 23, 15, 0, 0, 0, 0, -1))
        at = time.mktime((2026, 10, 23, 18, 30, 0, 0, 0, -1))
        self.assertEqual(usage.fmt_reset(at, now), "18:30")
        self.assertEqual(usage.fmt_reset(at + 86400, now), "10月24日 18:30")
        self.assertEqual(usage.seconds(at * 1000), at)  # milliseconds folded
        warn = usage.snapshot({**INFO, "status": "allowed_warning", "unifiedWindows": {"five_hour": {"utilization": 0.9, "resetsAt": at}}}, now)
        self.assertEqual(usage.limit_notice(warn, now), "Claude 的 5 小时额度快用完了（已用 90%，18:30 重置）。")
        gone = usage.snapshot({**INFO, "status": "rejected", "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": at}}}, now)
        self.assertEqual(usage.limit_notice(gone, now), "Claude 的额度用完了，18:30 重置。")

    def test_the_spoken_summary(self):
        now = time.time()
        names = {"claude": "Claude", "deepseek": "DeepSeek"}
        today = {"claude": {"turns": 4, "input": 32000, "output": 1200, "cache_read": 0, "cache_write": 0, "cost_usd": 0.1}}
        limits = {"claude": usage.snapshot(INFO, now)}
        self.assertEqual(usage.summary(today, limits, names, now),
                         "Claude 这周额度用了 75%，5 小时窗口用了 8%。今天用了 4 轮，输入 3.2 万、输出 1200 token。")
        both = {**today, "deepseek": {"turns": 1, "input": 500, "output": 80, "cache_read": 0, "cache_write": 0, "cost_usd": 0}}
        self.assertEqual(usage.summary(both, {}, names, now),
                         "Claude今天用了 4 轮，输入 3.2 万、输出 1200 token。DeepSeek今天用了 1 轮，输入 500、输出 80 token。")
        self.assertEqual(usage.summary({}, {}, names, now), "今天还没有用量记录。")
        cached = {"claude": {"turns": 2, "input": 20, "output": 131, "cache_read": 90000, "cache_write": 12000, "cost_usd": 0}}
        self.assertEqual(usage.summary(cached, {}, names, now), "今天用了 2 轮，输入 10.2 万（多是缓存）、输出 131 token。")
