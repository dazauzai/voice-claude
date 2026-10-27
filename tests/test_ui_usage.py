"""Token usage in the window: how the numbers are worded (usage_view), the line under a reply, the 用量 group on the
settings page and the 用量怎么样 phrase. The window is built hidden; no daemon, microphone or model is used."""
import time
import tkinter as tk
import unittest
from unittest import mock

from voiceclaude import app as appmod, config, usage_view as uv
from voiceclaude.text import fixed_phrase

TURN = {"line": "p-1", "provider": "claude", "model": "claude-haiku-4-5", "turn": "t1",
        "tokens": {"input": 10, "output": 65, "cache_read": 15456, "cache_write": 370}, "cost_usd": 0.0026}
SNAPSHOT = {"ok": True,
            "today": {"claude": {"turns": 4, "input": 40, "output": 1200, "cache_read": 30000, "cache_write": 2000,
                                 "cost_usd": 0.12}},
            "since_start": {"claude": {"turns": 2, "input": 20, "output": 389, "cache_read": 15456, "cache_write": 15826,
                                       "cost_usd": 0.035}},
            "limits": {"claude": {"provider": "claude", "status": "allowed",
                                  "windows": {"five_hour": {"used": 0.08, "resets_at": time.time() + 3600},
                                              "seven_day": {"used": 0.85, "resets_at": time.time() + 3 * 86400}}}},
            "last_turn": {}}


class WordingTests(unittest.TestCase):
    def test_tokens(self):
        self.assertEqual([uv.tokens(n) for n in (0, 950, 10000, 15456, 1_234_567, 230_000_000)],
                         ["0", "950", "1万", "1.5万", "123.5万", "2.3亿"])

    def test_the_line_under_a_reply(self):
        self.assertEqual(uv.turn_line(TURN), "· 本轮用量：输入 1.6万（缓存 1.6万） · 输出 65 · 约 $0.0026")
        plain = dict(TURN, tokens={"input": 300, "output": 20}, cost_usd=None)  # e.g. DeepSeek: tokens, no price
        self.assertEqual(uv.turn_line(plain), "· 本轮用量：输入 300 · 输出 20")

    def test_resets(self):
        now = time.mktime((2026, 10, 23, 12, 0, 0, 0, 0, -1))  # a Friday
        self.assertEqual(uv.resets(now + 6.5 * 3600, now), "今天 18:30")
        self.assertEqual(uv.resets(now + 22 * 3600, now), "明天 10:00")
        self.assertEqual(uv.resets(now + 3 * 86400, now), "周一 12:00")

    def test_settings_rows(self):
        rows = uv.settings_rows(SNAPSHOT)
        names = [r[0] for r in rows]
        self.assertEqual(names, ["今天 · Claude", "本次启动 · Claude", "Claude 5 小时额度", "Claude 本周额度"])
        self.assertEqual(rows[0][1], "4 轮 · 输入 3.2万 · 输出 1200 · 约 $0.12")
        self.assertTrue(rows[3][1].startswith("已用 85%，"))
        self.assertEqual([r[2] for r in rows], [False, False, False, True])  # 85% is running out
        warned = {"status": "allowed_warning", "type": "seven_day",
                  "windows": {"five_hour": {"used": 0.18}, "seven_day": {"used": 0.77}}}
        self.assertEqual([r[2] for r in uv.limit_rows(warned)], [False, True])  # Claude flagged the week at 77%
        empty = uv.settings_rows({"today": {}, "since_start": {}, "limits": {}}, "api_key")
        self.assertEqual(empty, [("今天", "还没有记录", False), ("本次启动", "还没有记录", False),
                                 ("Claude 余额", "在 console.anthropic.com 查看", False)])

    def test_the_phrase(self):
        for t in ["用量怎么样？", "还剩多少额度", "那个，查一下用量", "Token 用了多少"]:
            self.assertEqual(fixed_phrase(t), "usage_status", t)
        for t in ["这个函数用了多少内存", "把用量写进日志"]:
            self.assertIsNone(fixed_phrase(t), t)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(config, "save", lambda *a, **k: None),
                        mock.patch.object(appmod.audio, "tone", lambda *a, **k: None)]
        for p in self.patches:
            p.start()
        try:
            self.app = appmod.App(config.Config())
            self.app._build_window()
        except tk.TclError as e:  # no display
            self.skipTest(f"no display: {e}")
        self.app.root.withdraw()
        a = self.app
        a.ready = True
        a.client = mock.Mock(connected=True, active="p-1")
        a.client.usage.return_value = SNAPSHOT
        a.speaker, a.player = mock.Mock(), mock.Mock()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        try:
            self.app.root.destroy()
        except Exception:
            pass

    def pump(self, until=None, secs=3.0):
        t = time.time()
        while True:
            self.app._drain_ui()
            self.app.root.update()
            if until is None or until() or time.time() - t > secs:
                return
            time.sleep(0.01)

    def transcript(self):
        return self.app.textbox.get("1.0", "end")

    def test_a_turn_on_the_line_in_front_gets_its_usage_line(self):
        self.app._on_daemon("usage", TURN)
        self.pump(until=lambda: "本轮用量" in self.transcript())
        self.assertIn("本轮用量：输入 1.6万", self.transcript())

    def test_a_background_line_turn_is_not_shown(self):
        self.app._on_daemon("usage", dict(TURN, line="p-2"))
        self.pump()
        self.assertNotIn("本轮用量", self.transcript())

    def test_the_settings_page_shows_totals_and_quota(self):
        self.app._open_settings()
        self.pump(until=lambda: self.app.usage_snapshot is not None)
        self.pump()
        shown = [w.cget("text") for w in self.app.usage_box.winfo_children()]
        self.assertIn("Claude 本周额度", shown)
        self.assertTrue(any(s.startswith("已用 85%") for s in shown))
        self.assertIn("4 轮 · 输入 3.2万 · 输出 1200 · 约 $0.12", shown)

    def test_new_numbers_refresh_the_open_settings_page(self):
        self.app._open_settings()
        self.pump(until=lambda: self.app.usage_snapshot is not None)
        self.app.client.usage.reset_mock()
        self.app._on_daemon("limits", {"provider": "claude", "status": "allowed_warning", "windows": {}})
        self.pump(until=lambda: self.app.client.usage.called)
        self.assertTrue(self.app.client.usage.called)


if __name__ == "__main__":
    unittest.main()
