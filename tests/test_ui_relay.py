"""A reply relayed from another Claude session (the desktop app's Code tab): shown under the session's name and read
out. The window is built hidden; no daemon, microphone or model is used."""
import time
import tkinter as tk
import unittest
from unittest import mock

from voiceclaude import app as appmod, config


class RelayReplyTests(unittest.TestCase):
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
        a.speaker, a.player = mock.Mock(), mock.Mock()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        try:
            self.app.root.destroy()
        except Exception:
            pass

    def pump(self, until, secs=3.0):
        t = time.time()
        while not until() and time.time() - t < secs:
            self.app._drain_ui()
            self.app.root.update()
            time.sleep(0.01)

    def test_a_relayed_reply_is_shown_and_read_out(self):
        self.app._on_daemon("relay_reply", {"to": "论文写作辅助", "text": "第三段的引用已经改好了。还要我检查图注吗？",
                                            "session": "s-1"})
        self.pump(lambda: "论文写作辅助" in self.app.textbox.get("1.0", "end"))
        shown = self.app.textbox.get("1.0", "end")
        self.assertIn("论文写作辅助 第三段的引用已经改好了。", shown)
        said = [c.args[0] for c in self.app.speaker.say.call_args_list]
        self.assertTrue(said[0].startswith("论文写作辅助回复：第三段"), said)

    def test_an_unfinished_reply_says_so(self):
        self.app._on_daemon("relay_reply", {"to": "论文写作辅助", "text": "我先看了前两段。", "session": "s-1", "done": False})
        self.pump(lambda: "未完" in self.app.textbox.get("1.0", "end"))
        self.assertIn("论文写作辅助 · 未完 我先看了前两段。", self.app.textbox.get("1.0", "end"))
        said = [c.args[0] for c in self.app.speaker.say.call_args_list]
        self.assertTrue(said[0].startswith("论文写作辅助还没说完，到现在的回复："), said)

    def test_a_reply_ends_the_wait_for_it(self):
        self.app._on_daemon("relay_waiting", {"to": "论文写作辅助", "session": "s-1"})
        self.assertEqual(self.app._relays_waiting, {"s-1"})
        self.app._on_daemon("relay_reply", {"to": "论文写作辅助", "text": "好的。", "session": "s-1", "done": True})
        self.assertEqual(self.app._relays_waiting, set())
        self.app._on_daemon("relay_waiting", {"to": "论文写作辅助", "session": "s-2"})
        self.app._on_daemon("relay_done", {"to": "论文写作辅助", "session": "s-2", "ok": False, "why": "timeout"})
        self.assertEqual(self.app._relays_waiting, set())

    def test_two_messages_to_one_session_are_two_waits(self):
        a = self.app
        a._on_daemon("relay_waiting", {"to": "论文写作辅助", "session": "s-1", "relay": "tu-1"})
        a._on_daemon("relay_waiting", {"to": "论文写作辅助", "session": "s-1", "relay": "tu-2"})
        a._on_daemon("relay_reply", {"to": "论文写作辅助", "text": "看过了。", "session": "s-1", "relay": "tu-1", "done": True})
        self.assertEqual(a._relays_waiting, {"tu-2"})  # the second answer is still on its way
        a._on_daemon("relay_done", {"to": "论文写作辅助", "session": "s-1", "relay": "tu-2", "ok": True, "why": "superseded"})
        self.assertEqual(a._relays_waiting, set())

    def test_an_empty_reply_says_nothing(self):
        self.app._on_daemon("relay_reply", {"to": "论文写作辅助", "text": "  "})
        self.app._drain_ui()
        self.assertFalse(self.app.speaker.say.called)


if __name__ == "__main__":
    unittest.main()
