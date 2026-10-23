"""Rewind, window side: a sent sentence carries its turn id, right-click → 从这里重来 asks the service to rewind and
puts the sentence back into the text box, the 'rewound' event marks the transcript, and 撤回上一句 is a fixed
phrase. The window is built hidden; no daemon, microphone or model is used."""
import threading
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest import mock

from voiceclaude import app as appmod, config
from voiceclaude.text import fixed_phrase


class RewindTests(unittest.TestCase):
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
        self.turns = iter(["t1", "t2", "t3"])
        a.client.say.side_effect = lambda line, text: {"ok": True, "turn": next(self.turns)}
        a.client.rewind.return_value = {"ok": True, "message": "撤回到「打开记事本」之前。", "text": "打开记事本",
                                        "files_restored": 0}
        a.client.phrase.return_value = {"ok": True, "spoken": ""}
        a.speaker, a.player, a.answer_endpointer = mock.Mock(), mock.Mock(), mock.Mock()
        a.endpointer = mock.Mock(active=False)

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

    def send(self, text):
        box = self.app.typebox
        box._hint(False)
        box.text.insert("1.0", text)
        box.typed_at = time.monotonic()
        box._enter(SimpleNamespace(state=0))

    def tag_of(self, text):
        tb = self.app.textbox
        start = tb.search(text, "1.0", "end")
        return next((t for t in tb.tag_names(start) if t.startswith("turn:")), None)

    def test_a_sent_sentence_carries_its_turn(self):
        self.send("打开记事本")
        self.pump(until=lambda: self.app._turns)
        self.assertEqual(self.tag_of("打开记事本"), "turn:p-1:t1")
        self.assertEqual(self.app._turns["turn:p-1:t1"], ("p-1", "t1", "打开记事本"))

    def test_queued_sentences_sent_together_share_the_turn(self):
        a = self.app
        a.turn_open, a.state, a.splitter = True, appmod.BUSY, None
        self.send("第一件事")
        self.send("第二件事\n第二行")  # Shift+Enter inside one message
        self.pump()
        a._on_daemon("done", {"line": "p-1", "status": "ok"})  # the line finished: the queue goes out as one turn
        self.pump(until=lambda: a._turns)
        a.client.say.assert_called_with("p-1", "第一件事\n第二件事\n第二行")
        self.assertEqual(self.tag_of("第一件事"), "turn:p-1:t1")
        self.assertEqual(self.tag_of("第二件事"), "turn:p-1:t1")

    def test_a_sentence_still_in_the_queue_is_not_marked(self):
        a = self.app
        self.send("继续")
        self.pump(until=lambda: a._turns)
        a.turn_open, a.state = True, appmod.BUSY
        self.send("继续")  # the same words, waiting
        self.pump()
        a._mark_turn("p-1", "t9", ["继续"])
        tb = a.textbox
        last = tb.search("继续", "end", "1.0", backwards=True)
        self.assertNotIn("turn:p-1:t9", tb.tag_names(last))

    def test_from_here_rewinds_and_puts_the_sentence_back(self):
        self.send("打开记事本")
        self.pump(until=lambda: self.app._turns)
        self.app._rewind_to("turn:p-1:t1")
        self.pump(until=lambda: self.app.typebox.value())
        self.app.client.rewind.assert_called_with(line="p-1", turn="t1")
        self.assertEqual(self.app.typebox.value(), "打开记事本")
        self.app.speaker.say.assert_called_with("撤回到「打开记事本」之前。")

    def test_a_refused_rewind_leaves_the_box_alone(self):
        self.app.client.rewind.return_value = {"ok": False, "message": "正在回答，先打断再撤回。"}
        self.send("打开记事本")
        self.pump(until=lambda: self.app._turns)
        self.app._rewind_to("turn:p-1:t1")
        self.pump(until=lambda: "先打断再撤回" in self.app.textbox.get("1.0", "end"))
        self.assertIn("先打断再撤回", self.app.textbox.get("1.0", "end"))
        self.assertEqual(self.app.typebox.value(), "")

    def test_the_rewound_event_marks_the_transcript(self):
        self.app._on_daemon("rewound", {"line": "p-1", "turn": "t1", "text": "打开记事本"})
        self.pump(until=lambda: "撤回到" in self.app.textbox.get("1.0", "end"))
        self.assertIn("撤回到「打开记事本」之前", self.app.textbox.get("1.0", "end"))

    def test_saying_or_typing_rewind_goes_back_one_and_fills_the_box(self):
        for t in ["撤回上一句", "回到上一句", "退回上一句"]:
            self.assertEqual(fixed_phrase(t), "rewind_last", t)
        self.assertEqual(fixed_phrase("撤销排队"), "cancel_queue")
        self.send("撤回上一句")
        self.pump(until=lambda: self.app.typebox.value())
        self.app.client.rewind.assert_called_with(line="p-1", steps=1)
        self.assertFalse(self.app.client.phrase.called)
        self.assertFalse(self.app.client.say.called)
        self.assertEqual(self.app.typebox.value(), "打开记事本")

    def test_a_box_you_typed_in_is_kept(self):
        self.send("打开记事本")
        self.pump(until=lambda: self.app._turns)
        box = self.app.typebox
        box._hint(False)
        box.text.insert("1.0", "改成打开画图")
        self.app._rewind_to("turn:p-1:t1")
        self.pump(until=lambda: "没放进去" in self.app.textbox.get("1.0", "end"))
        self.assertEqual(box.value(), "改成打开画图")

    def test_one_rewind_at_a_time(self):
        gate = threading.Event()
        self.app.client.rewind.side_effect = lambda **k: (gate.wait(5), {"ok": True, "message": "", "text": "x"})[1]
        self.send("打开记事本")
        self.pump(until=lambda: self.app._turns)
        self.app._rewind_to("turn:p-1:t1")
        self.app._rewind_to("turn:p-1:t1")
        self.pump(until=lambda: "还没完成" in self.app.textbox.get("1.0", "end"))
        self.assertIn("上一次撤回还没完成", self.app.textbox.get("1.0", "end"))
        self.app.speaker.say.assert_any_call("上一次撤回还没完成。")
        gate.set()
        self.pump(until=lambda: not self.app._rewinding)
        self.assertEqual(self.app.client.rewind.call_count, 1)

    def test_a_second_rewind_replaces_the_sentence_the_first_put_back(self):
        a = self.app
        a.client.rewind.side_effect = [{"ok": True, "message": "", "text": "二"}, {"ok": True, "message": "", "text": "一"}]
        a._start_rewind("p-1", "", steps=1)
        self.pump(until=lambda: a.typebox.value() == "二")
        a._start_rewind("p-1", "", steps=1)
        self.pump(until=lambda: a.typebox.value() == "一")
        self.assertEqual(a.typebox.value(), "一")

    def test_another_lines_sentence_while_this_line_works_stays_put(self):
        a = self.app
        self.send("打开记事本")
        self.pump(until=lambda: a._turns)
        a.client.active = "manager"
        a.turn_open, a.state = True, appmod.BUSY  # the manager is answering
        a._rewind_to("turn:p-1:t1")
        self.pump(until=lambda: "另一条线上" in a.textbox.get("1.0", "end"))
        a.client.switch.assert_not_called()
        self.assertEqual(a.typebox.value(), "")

    def test_rewinding_another_line_makes_it_active(self):
        self.send("打开记事本")
        self.pump(until=lambda: self.app._turns)
        self.app.turn_open, self.app.state = False, appmod.IDLE  # that turn is over
        self.app.client.active = "manager"
        self.app._rewind_to("turn:p-1:t1")
        self.pump(until=lambda: self.app.typebox.value())
        self.app.client.switch.assert_called_with("p-1")


if __name__ == "__main__":
    unittest.main()
