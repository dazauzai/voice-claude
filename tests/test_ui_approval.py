"""Answering a request without speaking: during a yes/no request the send key and the 静音 button mean 可以 (the
talk key and the left button mean 不行); otherwise they still mute. The window is built hidden; no daemon,
microphone or model is used."""
import threading
import tkinter as tk
import unittest
from unittest import mock

from voiceclaude import app as appmod, config


class ApprovalKeyTests(unittest.TestCase):
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
        a.speaker, a.player, a.answer_endpointer = mock.Mock(), mock.Mock(), mock.Mock()
        a.endpointer = mock.Mock(active=False)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        try:
            self.app.root.destroy()
        except Exception:
            pass

    def pump(self):
        for _ in range(200):
            try:
                item = self.app.ui_q.get_nowait()
            except Exception:
                break
            if item[0] == "note":
                self.app._handle_note(item[1])
            elif item[0] == "call":
                item[1]()
        self.app._tick_ui()
        self.app.root.update()

    def ask(self, kind="approve"):
        """Start a request the way the daemon's 'ask' does; returns the thread and where its answer lands."""
        out = {}
        t = threading.Thread(target=lambda: out.update(answer=self.app._ask("打开记事本，可以吗？", "", kind)), daemon=True)
        t.start()
        for _ in range(100):
            self.pump()
            if self.app.card is not None:
                break
        self.assertIsNotNone(self.app.card)
        return t, out

    def footnote(self):
        return self.app.answered[-1].footnote if self.app.answered else None

    def test_send_key_approves(self):
        t, out = self.ask()
        self.assertEqual(self.app.btn_mute.cget("text"), "可以")
        self.app._toggle_mute()  # what the send key's single tap calls
        t.join(5)
        self.assertEqual(out["answer"], (True, ""))
        self.pump()
        self.assertIsNone(self.app.card)
        self.assertEqual(self.footnote(), "你按了右 Alt，算可以")
        self.assertFalse(self.app.muted)
        self.assertEqual(self.app.btn_mute.cget("text"), "静音")

    def test_button_approves_then_mutes_again(self):
        t, out = self.ask()
        self.app.btn_mute.invoke()
        t.join(5)
        self.assertEqual(out["answer"], (True, ""))
        self.pump()
        self.assertEqual(self.footnote(), "你点了「可以」")
        self.app.btn_mute.invoke()
        self.assertTrue(self.app.muted)

    def test_a_click_on_yes_gives_the_focus_back_before_answering(self):
        back, order = [], []
        with mock.patch.object(appmod, "foreground_outside", lambda root: 4242),                 mock.patch.object(appmod, "bring_back", lambda h: (back.append(h), order.append("back"))):
            t, out = self.ask()
            real = self.app._resolve_approval
            with mock.patch.object(self.app, "_resolve_approval", lambda *a, **k: (order.append("answer"), real(*a, **k))):
                self.app.btn_mute.invoke()
            t.join(5)
        self.assertEqual(back, [4242])
        self.assertEqual(order, ["back", "answer"])

    def test_the_send_key_does_not_move_the_focus(self):
        back = []
        with mock.patch.object(appmod, "foreground_outside", lambda root: 4242),                 mock.patch.object(appmod, "bring_back", back.append):
            t, out = self.ask()
            self.app._toggle_mute()
            t.join(5)
        self.assertEqual(back, [])

    def test_talk_key_still_says_no(self):
        t, out = self.ask()
        self.app._on_tap()
        t.join(5)
        self.assertEqual(out["answer"], (False, ""))
        self.pump()
        self.assertEqual(self.footnote(), "你按了右 Ctrl，算不行")

    def test_a_free_text_question_keeps_the_mute_key(self):
        t, out = self.ask(kind="question")
        self.assertNotEqual(self.app.btn_mute.cget("text"), "可以")
        self.app._toggle_mute()
        self.assertTrue(self.app.muted)
        self.assertFalse(out)  # still waiting for your words
        self.app._resolve_approval(False)
        t.join(5)


if __name__ == "__main__":
    unittest.main()
