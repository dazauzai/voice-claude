"""While a line works on the desktop, the window shrinks to a small bar in a corner so it does not cover what is being
clicked, and comes back when the turn ends or you use it. The window is built hidden; no daemon, microphone or model is
used."""
import time
import tkinter as tk
import unittest
from unittest import mock

from voiceclaude import app as appmod, config, ui_theme


class AsideTests(unittest.TestCase):
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
        a.speaker, a.player, a.answer_endpointer = mock.Mock(), mock.Mock(), mock.Mock()
        a.endpointer = mock.Mock(active=False)
        a.turn_line = "p-1"
        self.geometries = []
        self.real_geometry = a.root.geometry
        a.root.geometry = lambda *g: (self.geometries.append(g[0]) if g else None) or self.real_geometry(*g)

    def tearDown(self):
        try:
            self.app.root.destroy()
        except Exception:
            pass

    def desktop_step(self):
        a = self.app
        with a.lock:
            if not a.turn_open:
                a._begin_turn()
        a._on_daemon("tool", {"line": "p-1", "name": "mcp__desktop__click", "detail": "Submit"})
        a._drain_ui()

    def test_a_desktop_step_shrinks_the_window_to_a_corner(self):
        a = self.app
        self.desktop_step()
        self.assertTrue(a._aside)
        self.assertTrue(self.geometries and "+" in self.geometries[-1])  # a size and a place
        w, h = (int(v) for v in self.geometries[-1].split("+")[0].split("x"))
        self.assertLess(w, a.theme.min_w)
        self.assertLess(h, a.theme.min_h)
        self.assertLess(a.root.minsize()[1], a.theme.min_h)  # (Windows keeps its own minimum width)

    def test_a_file_step_does_not(self):
        a = self.app
        with a.lock:
            a._begin_turn()
        a._on_daemon("tool", {"line": "p-1", "name": "Read", "detail": "x.py"})
        a._drain_ui()
        self.assertFalse(a._aside)

    def test_the_window_comes_back_when_the_turn_ends(self):
        a = self.app
        before = a.root.geometry()
        self.desktop_step()
        a._on_daemon("done", {"line": "p-1", "status": "ok"})
        a.speaker.idle.return_value = True
        a._check_turn_finished()
        a._drain_ui()
        a._tick_ui()
        self.assertFalse(a._aside)
        self.assertEqual(self.geometries[-1], before)
        self.assertEqual(a.root.minsize(), (a.theme.min_w, a.theme.min_h))

    def test_a_request_during_desktop_work_keeps_it_aside(self):
        a = self.app
        self.desktop_step()
        a.state = appmod.APPROVING  # the desktop server asks before clicking: the turn is still open
        a._tick_ui()
        self.assertTrue(a._aside)  # the approved click must not land under the window
        a.turn_open = False
        a._tick_ui()
        self.assertFalse(a._aside)  # a question with no desktop work going on: the window is back

    def test_the_bar_fits_the_status_band(self):
        a = self.app
        self.desktop_step()
        h = int(self.geometries[-1].split("+")[0].split("x")[1])
        self.assertGreaterEqual(h, a.theme.bar_h)

    def test_a_font_change_while_aside_keeps_the_bar_small(self):
        a = self.app
        self.desktop_step()
        a.font_size += 1
        a._apply_font()
        a.root.update()
        self.assertLess(a.root.minsize()[1], a.theme.min_h)

    def test_using_the_window_brings_it_back_and_keeps_it_for_a_while(self):
        a = self.app
        self.desktop_step()
        a._touched()
        a._tick_ui()
        self.assertFalse(a._aside)
        a._on_daemon("tool", {"line": "p-1", "name": "mcp__desktop__type_text", "detail": "hi"})
        a._drain_ui()
        self.assertFalse(a._aside)  # you just took it back: not again for a while
        a._hands_off_until = time.monotonic() - 1
        a._on_daemon("tool", {"line": "p-1", "name": "mcp__desktop__type_text", "detail": "hi"})
        a._drain_ui()
        self.assertTrue(a._aside)

    def test_it_can_be_turned_off(self):
        a = self.app
        a._set_aside(False)
        self.desktop_step()
        self.assertFalse(a._aside)

    def test_desktop_tools_are_known(self):
        for name in ("mcp__desktop__click", "mcp__desktop__screenshot", "computer", "open_app"):
            self.assertTrue(ui_theme.is_desktop_tool(name), name)
        for name in ("Read", "Bash", "mcp__voiceclaude__set", ""):
            self.assertFalse(ui_theme.is_desktop_tool(name), name)


if __name__ == "__main__":
    unittest.main()
