"""Reading out: a new message of yours drops whatever of the old reply is still being read, so the reading starts
with the new reply instead of queueing behind the old one. The window is built hidden; no daemon, microphone or
model is used."""
import tkinter as tk
import unittest
from unittest import mock

from voiceclaude import app as appmod, config


class FakeSpeaker:
    """Like the real one for what matters here: stop() drops what was said but not yet read out."""

    def __init__(self):
        self.said, self.stops = [], 0

    def say(self, text):
        self.said.append(text)

    def stop(self):
        self.stops += 1
        self.said.clear()

    def idle(self):
        return not self.said

    def stuck(self, after=60.0):
        return False

    def replace_player(self, p):
        pass


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(config, "save", lambda *a, **k: None),
                        mock.patch.object(appmod.audio, "tone", lambda *a, **k: None)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        try:
            self.app = appmod.App(config.Config())
            self.app._build_window()
        except tk.TclError as e:  # no display
            self.skipTest(f"no display: {e}")
        self.app.root.withdraw()
        a = self.app
        a.ready = True
        a.client = mock.Mock(connected=True, active="p-1")
        a.client.say.return_value = {"ok": True, "turn": "t1"}
        a.speaker, a.player, a.answer_endpointer = FakeSpeaker(), mock.Mock(), mock.Mock()
        a.endpointer = mock.Mock(active=False)
        a.turn_line = "p-1"

    def tearDown(self):
        try:
            self.app.root.destroy()
        except Exception:
            pass

    def test_what_you_said_while_it_answered_drops_the_rest_of_that_answer(self):
        a = self.app
        with a.lock:
            a._begin_turn()
        a.speaker.stops = 0
        a._on_daemon("text", {"line": "p-1", "text": "第一句。第二句。第三句还没"})
        self.assertEqual(a.speaker.said, ["第一句。", "第二句。"])
        with a.lock:
            a._route("换个问题")  # said while the line was still answering: queued
        self.assertEqual(a.queued, ["换个问题"])
        a._on_daemon("done", {"line": "p-1", "status": "ok"})
        self.assertEqual(a.speaker.said, [])  # the unfinished rest is shown, not read; the rest dropped
        self.assertEqual(a.speaker.stops, 1)
        self.assertTrue(a.turn_open)  # the new turn is open
        self.assertEqual(a.queued, [])

    def test_a_new_question_stops_the_old_reply_being_read(self):
        a = self.app
        with a.lock:
            a._begin_turn()
        a._on_daemon("text", {"line": "p-1", "text": "很长的回答。还在念。"})
        a._on_daemon("done", {"line": "p-1", "status": "ok"})
        self.assertEqual(a.speaker.said, ["很长的回答。", "还在念。"])  # still reading it out
        with a.lock:
            parts = a._route("新的问题")
        self.assertEqual(parts, ["新的问题"])
        self.assertEqual(a.speaker.said, [])
        self.assertTrue(a.turn_open)

    def test_a_long_tool_call_being_written_is_shown(self):
        a = self.app
        with a.lock:
            a._begin_turn()
        a._on_daemon("writing", {"line": "p-1", "tool": "mcp__desktop__type_text", "chars": 1200, "seconds": 9, "done": False})
        a._drain_ui()
        self.assertEqual(a._status_raw, "正在写…（1200 字）")
        a._on_daemon("writing", {"line": "p-1", "tool": "mcp__desktop__type_text", "chars": 2093, "seconds": 40, "done": True})
        a._drain_ui()
        self.assertEqual(a._status_raw, "思考中…")

    def test_an_error_is_still_said(self):
        a = self.app
        with a.lock:
            a._begin_turn()
        a.queued.append("再来")
        a._on_daemon("done", {"line": "p-1", "status": "error", "last": "boom"})
        self.assertEqual(a.speaker.said, ["这一轮出错了，看一下屏幕。"])  # not dropped by the queued turn opening
        self.assertTrue(a.turn_open)

    def test_the_rewinds_result_is_heard_before_the_queued_words_go(self):
        a = self.app
        a.queued.append("再来一次")
        a.speaker.say("撤回了，这条线重新开始。")
        a._send_queued()
        self.assertEqual(a.speaker.said, ["撤回了，这条线重新开始。"])
        self.assertTrue(a.turn_open)

    def test_a_card_being_asked_is_not_cut_by_the_queued_turn(self):
        a = self.app
        with a.lock:
            a._begin_turn()
        a.queued.append("然后呢")
        a.state = appmod.APPROVING  # another line's question is up and being spoken
        a.approval = {"event": mock.Mock(), "answer": None, "text": "", "unclear": 0, "kind": "approve",
                      "prev": appmod.BUSY, "question": "要删文件吗？"}
        a.speaker.say("要删文件吗？")
        a._on_daemon("done", {"line": "p-1", "status": "ok"})
        self.assertEqual(a.speaker.said, ["要删文件吗？"])
        self.assertEqual(a.state, appmod.APPROVING)
        self.assertEqual(a.queued, ["然后呢"])  # goes out when the card is answered (_ask's _send_queued)
        self.assertFalse(a.turn_open)


if __name__ == "__main__":
    unittest.main()
