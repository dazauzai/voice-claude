"""The text box under the transcript: typed text goes where spoken text goes (a new turn, the queue, fixed phrases,
answers to a request card), Enter sends, Shift+Enter is a new line. The window is built hidden; no daemon,
microphone or model is used."""
import threading
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest import mock

from voiceclaude import app as appmod, config


class TypeBoxTests(unittest.TestCase):
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
        a.client.phrase.return_value = {"ok": True, "spoken": ""}
        a.speaker, a.player, a.answer_endpointer = mock.Mock(), mock.Mock(), mock.Mock()
        a.endpointer = mock.Mock(active=False)
        a.ANSWER_GRACE = 0.0  # the tests type at machine speed; test_an_answer_begun_at_once_is_not_one sets it back

    def tearDown(self):
        for p in self.patches:
            p.stop()
        try:
            self.app.root.destroy()
        except Exception:
            pass

    def pump(self):
        self.app._drain_ui()  # what the window does every 50 ms
        self.app.root.update()

    def wait_for(self, cond, secs=3.0):
        t = time.time()
        while time.time() - t < secs:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def type_and_enter(self, text, typed=True):
        box = self.app.typebox
        box._hint(False)
        box.text.insert("1.0", text)
        if typed:  # as the keys you press would
            box.typed_at = box.began_at = time.monotonic()
        return box._enter(SimpleNamespace(state=0))

    def transcript(self):
        return self.app.textbox.get("1.0", "end")

    def test_typed_text_starts_a_turn(self):
        self.assertEqual(self.type_and_enter("帮我看一下 README"), "break")
        self.assertTrue(self.wait_for(lambda: self.app.client.say.called))
        self.app.client.say.assert_called_with("p-1", "帮我看一下 README")
        self.assertEqual(self.app.typebox.value(), "")  # taken: the box is empty again
        self.assertTrue(self.app.turn_open)
        self.pump()
        self.assertIn("帮我看一下 README", self.transcript())

    def test_typed_text_waits_in_the_queue_while_it_answers(self):
        self.app.turn_open = True
        self.app.state = appmod.BUSY
        self.type_and_enter("还有一件事")
        self.assertEqual(self.app.queued, ["还有一件事"])
        self.assertFalse(self.app.client.say.called)

    def test_fixed_phrases_work_typed_too(self):
        self.type_and_enter("回到总机")
        self.assertTrue(self.wait_for(lambda: self.app.client.phrase.called))
        self.app.client.phrase.assert_called_with("back_to_manager")
        self.assertFalse(self.app.client.say.called)

    def ask(self, kind="approve"):
        out = {}
        t = threading.Thread(target=lambda: out.update(answer=self.app._ask("打开记事本，可以吗？", "", kind)), daemon=True)
        t.start()
        for _ in range(100):
            self.pump()
            if self.app.card is not None:
                break
        self.assertIsNotNone(self.app.card)
        return t, out

    def test_a_typed_yes_or_no_answers_the_request(self):
        t, out = self.ask()
        self.type_and_enter("可以")
        t.join(5)
        self.assertEqual(out["answer"], (True, ""))
        self.pump()
        self.assertEqual(self.app.answered[-1].footnote, "你输入：可以")
        t, out = self.ask()
        self.type_and_enter("嗯，不行")
        t.join(5)
        self.assertEqual(out["answer"], (False, ""))
        self.pump()
        self.assertEqual(self.app.answered[-1].footnote, "你输入：嗯，不行")

    def test_an_unclear_typed_answer_asks_again_and_stays_in_the_box(self):
        t, out = self.ask()
        self.type_and_enter("顺便把 README 也更新一下")
        self.assertFalse(out)  # still waiting
        self.assertIn("可以」或「不行", self.app.card_info["override"])
        self.assertEqual(self.app.typebox.value(), "顺便把 README 也更新一下")  # not lost
        self.pump()
        self.assertNotIn("顺便把 README", self.transcript())
        self.app._resolve_approval(False)
        t.join(5)

    def edit_box(self, text):
        """Change the box's text the way typing does, and let <<Modified>> arrive."""
        box = self.app.typebox
        box._hint(False)
        box.text.delete("1.0", "end")
        self.pump()
        if text:
            box.text.insert("1.0", text)
            self.pump()

    def test_text_typed_before_the_card_is_not_an_answer(self):
        box = self.app.typebox
        self.edit_box("好的")
        time.sleep(0.02)
        t, out = self.ask()
        box._enter(SimpleNamespace(state=0))
        self.assertFalse(out)
        self.assertEqual(box.value(), "好的")
        self.assertIn("先回答申请", self.app.card_info["override"])
        self.edit_box("可以")  # cleared and typed after the card came up: now it is the answer
        box._enter(SimpleNamespace(state=0))
        t.join(5)
        self.assertEqual(out["answer"], (True, ""))

    def test_a_message_begun_before_the_card_and_finished_after_is_not_an_answer(self):
        box = self.app.typebox
        self.edit_box("等它跑完再")
        time.sleep(0.02)
        t, out = self.ask()
        box.text.insert("end", "提交")
        self.pump()
        box._enter(SimpleNamespace(state=0))
        self.assertFalse(out)  # not taken as 不行
        self.assertEqual(box.value(), "等它跑完再提交")
        self.app._resolve_approval(False)
        t.join(5)

    def test_an_answer_begun_at_once_is_not_one(self):
        self.app.ANSWER_GRACE = 1.0
        t, out = self.ask()
        self.type_and_enter("可以")  # within a second of the card: it was on its way for something else
        self.assertFalse(out)
        self.assertEqual(self.app.typebox.value(), "可以")
        self.app._resolve_approval(False)
        t.join(5)

    def test_text_still_in_the_input_method_must_be_cleared_first(self):
        box = self.app.typebox
        self.assertIs(box.composing(), False)  # nothing is being composed in a hidden test window
        self.edit_box("")  # you are in the box; the input method has not put anything in yet
        with mock.patch.object(box, "composing", return_value=True):
            t, out = self.ask()
        box.text.insert("1.0", "可以")  # ... now it commits what was being composed when the card came up
        self.pump()
        box._enter(SimpleNamespace(state=0))
        self.assertFalse(out)
        self.edit_box("")
        self.edit_box("可以")  # cleared, then typed: an answer
        box._enter(SimpleNamespace(state=0))
        t.join(5)
        self.assertEqual(out["answer"], (True, ""))

    def test_the_grace_counts_from_when_the_input_method_began(self):
        self.app.ANSWER_GRACE = 1.0
        box = self.app.typebox
        self.edit_box("")
        t, out = self.ask()
        t0 = self.app.card_info["started"]
        with mock.patch("time.monotonic", return_value=t0 + 0.1), mock.patch.object(box, "composing", return_value=True):
            box.poll()  # what the card's tick does: composing began right after the card came up
        with mock.patch("time.monotonic", return_value=t0 + 1.5):  # ... and is committed well after the grace
            box.text.insert("1.0", "可以")
            self.pump()
        box._enter(SimpleNamespace(state=0))
        self.assertFalse(out)  # begun within the grace: refused
        self.app._resolve_approval(False)
        t.join(5)

    def test_a_composition_given_up_does_not_count_later(self):
        box = self.app.typebox
        self.edit_box("")
        with mock.patch("time.monotonic", return_value=100.0), mock.patch.object(box, "composing", return_value=True):
            box.poll()  # pinyin begun ...
        with mock.patch.object(box, "composing", return_value=False):
            box.poll()  # ... and given up with Esc: nothing reached the box
        with mock.patch("time.monotonic", return_value=200.0):
            box.text.insert("1.0", "可以")
            self.pump()
        self.assertEqual(box.began_at, 200.0)

    def test_a_late_failure_leaves_a_newer_turn_open(self):
        a = self.app
        with a.lock:
            a._begin_turn()
        old = a._turn_seq
        with a.lock:
            a._begin_turn()  # you said it again while the first send was still waiting
        a._say_failed("（Codex 重新启动过，这句没有发出去）", "这句没有发出去", old)
        self.assertTrue(a.turn_open)
        self.assertNotIn(mock.call("这句没有发出去"), a.speaker.say.call_args_list)  # what you said since did go out
        self.pump()
        self.assertIn("前面那句没有发出去", self.transcript())

    def test_a_composition_given_up_while_the_card_is_up_counts_as_clearing(self):
        box = self.app.typebox
        self.edit_box("")
        with mock.patch.object(box, "composing", return_value=True):
            t, out = self.ask()  # composing when the card came up
            box.poll()
        with mock.patch.object(box, "composing", return_value=False):
            box.poll()  # Esc: nothing reached the box
        self.edit_box("可以")
        box._enter(SimpleNamespace(state=0))
        t.join(5)
        self.assertEqual(out["answer"], (True, ""))

    def test_a_quit_the_service_turns_down_lets_the_window_go_on(self):
        a = self.app
        a._quit_requested = True
        a._quit_turned_down()
        self.assertFalse(a._quit_requested)
        self.pump()
        self.assertIn("没有退出", self.transcript())

    def test_a_line_switch_drops_what_was_queued_for_the_old_line(self):
        a = self.app
        a.turn_open, a.state, a.turn_line = True, appmod.BUSY, "p-1"
        self.type_and_enter("顺便把 README 也更新一下")
        self.assertEqual(a.queued, ["顺便把 README 也更新一下"])
        a._on_line({"line": "manager", "active": True})
        self.assertEqual(a.queued, [])
        self.pump()
        self.assertIn("撤销了排队的 1 句", self.transcript())

    def test_a_reply_meant_for_the_line_does_not_answer_the_card(self):
        t, out = self.ask()
        self.type_and_enter("好的")  # e.g. composed for Claude's question just as a background card came up
        self.assertFalse(out)
        self.assertEqual(self.app.typebox.value(), "好的")
        self.app._resolve_approval(False)
        t.join(5)

    def test_writing_over_old_text_after_the_card_answers_it(self):
        box = self.app.typebox
        self.edit_box("好的")
        time.sleep(0.02)
        t, out = self.ask()
        box.text.delete("1.0", "end")
        box.text.insert("1.0", "可以")  # select all, type over: never empty in between
        self.pump()
        box._enter(SimpleNamespace(state=0))
        t.join(5)
        self.assertEqual(out["answer"], (True, ""))

    def test_what_waited_for_a_rewind_goes_out_after_a_card(self):
        a = self.app
        a._rewinding = True
        self.type_and_enter("改成用迭代")
        self.assertEqual(a.queued, ["改成用迭代"])
        t, out = self.ask()
        a._rewound_here({"ok": True, "message": "", "text": "三"}, "三")  # the rewind ends while the card is up
        self.assertFalse(a.client.say.called)
        a._resolve_approval(True)
        t.join(5)
        self.assertTrue(self.wait_for(lambda: a.client.say.called))
        a.client.say.assert_called_with("p-1", "改成用迭代")

    def test_a_new_sentence_goes_after_what_still_waits(self):
        a = self.app
        a.queued = ["改成用迭代"]  # left over from a rewind
        self.type_and_enter("再加个测试")
        self.assertTrue(self.wait_for(lambda: a.client.say.called))
        a.client.say.assert_called_with("p-1", "改成用迭代\n再加个测试")

    def test_send_now_waits_for_a_rewind(self):
        a = self.app
        a._rewinding = True
        a.queued = ["改成用迭代"]
        a._on_send_now()
        self.assertEqual(a.queued, ["改成用迭代"])
        a.speaker.say.assert_called_with("撤回完成后马上发。")

    def test_typing_while_a_rewind_runs_waits_for_it(self):
        a = self.app
        a._rewinding = True
        self.type_and_enter("打开画图")
        self.assertEqual(a.queued, ["打开画图"])
        self.assertEqual(a.typebox.value(), "")  # taken: shown as queued
        self.assertFalse(a.client.say.called)
        a._rewound_here({"ok": True, "message": "", "text": "打开记事本"}, "打开记事本")
        self.assertTrue(self.wait_for(lambda: a.client.say.called))
        a.client.say.assert_called_with("p-1", "打开画图")
        self.assertEqual(a.typebox.value(), "")  # the old sentence does not come back over what you said meanwhile

    def test_a_typed_answer_to_a_question_is_the_whole_sentence(self):
        t, out = self.ask(kind="question")
        self.type_and_enter("叫它 测试二")
        t.join(5)
        self.assertEqual(out["answer"], (True, "叫它 测试二"))
        for _ in range(5):
            self.pump()
            self.app._tick_card()
        self.assertEqual(self.app.answered[-1].footnote, "你输入：叫它 测试二")

    def test_a_refused_send_ends_the_turn(self):
        self.app.client.say.return_value = {"ok": False, "message": "Claude 正在重启，请稍后再说一遍"}
        self.type_and_enter("继续")
        self.assertTrue(self.wait_for(lambda: not self.app.turn_open))
        self.app.speaker.say.assert_called_with("Claude 正在重启，请稍后再说一遍")

    def test_not_taken_while_not_connected(self):
        self.app.ready = False
        self.type_and_enter("你好")
        self.assertEqual(self.app.typebox.value(), "你好")  # kept for later
        self.assertFalse(self.app.client.say.called)

    def test_shift_enter_is_a_new_line_and_the_hint_shows_when_empty(self):
        box = self.app.typebox
        self.assertTrue(box.hinting)
        self.assertEqual(box.value(), "")
        self.assertEqual(box.text.get("1.0", "end-1c"), box.HINT)
        box._hint(False)
        self.assertIsNone(box._enter(SimpleNamespace(state=0x0001)))  # Shift held: Tk inserts the new line
        self.assertEqual(self.app.client.say.call_count, 0)

    def test_undo_does_not_bring_the_hint_back(self):
        box = self.app.typebox
        box._hint(False)
        try:
            box.text.edit_undo()
        except tk.TclError:
            pass  # nothing to undo
        self.assertEqual(box.text.get("1.0", "end-1c"), "")


if __name__ == "__main__":
    unittest.main()
