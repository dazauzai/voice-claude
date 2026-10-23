"""The settings area of the window: page navigation and where back goes,
the permissions sub-page, the font-size rebuild, and 退出 voice-claude (confirmations, one bye, the window closing).
The window is built hidden; no daemon, microphone or model is used, and the user's config is never written."""
import threading
import time
import tkinter as tk
import unittest
from unittest import mock

from voiceclaude import app as appmod, config


class FakeClient:
    """Only what the settings area touches. shutdown() records how it was asked."""

    def __init__(self, shutdown_reply=None):
        self.connected = True
        self.active = "p-1"
        self.info = {}
        self.shutdowns = []
        self.confirmed = []
        self.shutdown_reply = shutdown_reply or {"ok": True, "message": "后台服务正在关闭"}
        self.said = []
        self.phrases = []

    def phrase(self, key):
        self.phrases.append(key)
        return {"ok": True, "spoken": ""}

    def shutdown(self, jobs="ask", lines_confirmed=False):
        self.shutdowns.append(jobs)
        self.confirmed.append(lines_confirmed)
        return self.shutdown_reply

    def say(self, line, text):
        self.said.append((line, text))
        return {"ok": True}

    def access_status(self):
        return {"ok": True, "access": {"ok": True, "method": "subscription", "account": "a@b.c max", "provider": "claude"}}

    def close(self):
        self.connected = False


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(config, "save", lambda *a, **k: None),
                        mock.patch.object(config, "load", lambda *a, **k: self.cfg),
                        mock.patch.object(appmod.audio, "tone", lambda *a, **k: self.tones.append(a))]
        self.tones = []
        self.cfg = config.Config()
        for p in self.patches:
            p.start()
        try:
            self.app = appmod.App(self.cfg)
            self.app._build_window()
        except tk.TclError as e:  # no display
            self.skipTest(f"no display: {e}")
        self.app.root.withdraw()
        self.app.root.update()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        try:
            self.app.root.destroy()
        except Exception:
            pass

    def pump(self):
        """Run the window's queued items (what _drain_ui does every 50 ms)."""
        for _ in range(200):
            try:
                item = self.app.ui_q.get_nowait()
            except Exception:
                break
            kind = item[0]
            if kind == "call":
                item[1]()
            elif kind == "font":
                self.app._apply_font()
            if self.app._closing:
                return
        self.app.root.update()

    # ---------- pages ----------
    def test_navigation_and_back(self):
        a = self.app
        self.assertEqual(a.page, "transcript")
        a._toggle_settings()
        self.assertEqual((a.page, a.btn_settings._kind), ("settings", "selected"))
        a._open_permissions()
        self.assertEqual(a.page, "permissions")
        a._escape()
        self.assertEqual(a.page, "settings")
        a._open_access()  # from settings: back goes to settings
        self.assertEqual((a.page, a.access_return), ("access", "settings"))
        a._escape()
        self.assertEqual(a.page, "settings")
        a._escape()
        self.assertEqual((a.page, a.btn_settings._kind), ("transcript", "secondary"))
        a._open_access()  # from the conversation (spoken, or nothing works yet): back goes to the conversation
        self.assertEqual(a.access_return, "transcript")
        a._open_access()  # said again while open: the way back does not change
        self.assertEqual(a.access_return, "transcript")
        a.access_page.header.back.invoke()
        self.assertEqual(a.page, "transcript")
        a._open_settings()
        a._open_access()
        a._toggle_settings()  # the bar's 设置 / Ctrl+, close the whole area in one step
        self.assertEqual(a.page, "transcript")

    def test_settings_rows(self):
        a = self.app
        a._toggle_settings()
        from voiceclaude.ui_theme import permissions_summary
        self.assertEqual(a.perm_nav.full_value, permissions_summary(config.DEFAULT_PERMISSIONS))  # not a fixed count
        self.assertTrue(a.perm_nav.full_value.startswith("询问 "))
        self.assertEqual(a.access_nav.full_value, "正在读取…")
        a._on_access_info({"ok": True, "method": "subscription", "account": "a@b.c max", "provider": "claude"})
        self.assertEqual((a.access_nav.full_value, a.access_nav.error), ("Claude · 已登录", False))
        a._on_access_info({"ok": False, "method": "none", "message": "没装"})
        self.assertEqual((a.access_nav.full_value, a.access_nav.error), ("未设置", True))
        self.assertEqual(a.config_caption.cget("text"), appmod.CONFIG_CAPTION)
        a.topmost_var.set(True)
        rows = [w for w in a.settings_body.winfo_children()]
        self.assertTrue(rows)

    def test_permission_change_goes_through_the_line(self):
        a = self.app
        a._open_permissions()
        self.assertEqual(len(a.perm_widgets), len(config.PERMISSION_LABELS))
        a._change_permission("click", "allow")  # not ready: nothing is sent, the reason shows on the page
        self.assertEqual(a.page, "permissions")
        self.assertEqual(a.perm_message.cget("text"), "后台服务未连接")
        a.client = FakeClient()
        a.ready = True
        a._change_permission("click", "allow")
        self.assertEqual(a.page, "transcript")
        for _ in range(100):
            if a.client.said:
                break
            time.sleep(0.01)
        self.assertEqual(a.client.said, [("p-1", "把点击的权限改成允许")])
        self.assertEqual(config.normalize_permission_value("允许"), "allow")  # the word Claude will hear is understood
        a.turn_open = True
        a._open_permissions()
        a._change_permission("screenshot", "deny")
        self.assertEqual(a.perm_message.cget("text"), "正在回答，请稍后再试")

    def test_transparent_row_asks_the_service_and_shows_what_it_reports(self):
        a = self.app
        a._open_permissions()
        self.assertFalse(a.transparent_var.get())
        a.transparent_row.toggle()  # not ready: nothing is sent
        self.assertEqual(a.perm_message.cget("text"), "后台服务未连接")
        self.assertFalse(a.transparent_var.get())
        a.client = FakeClient()
        a.ready = True
        a.transparent_row.toggle()
        self.assertEqual(a.page, "transcript")  # the service's question comes as a card there
        self.assertFalse(a.transparent_var.get())  # on only once the service says so
        for _ in range(100):
            if a.client.phrases:
                break
            time.sleep(0.01)
        self.assertEqual(a.client.phrases, ["transparent_on"])
        self.assertEqual(a.transparent_label.winfo_manager(), "")
        a._on_daemon("transparent", {"on": True})
        self.pump()
        self.assertTrue(a.transparent_var.get())
        self.assertEqual(a.transparent_label.winfo_manager(), "pack")
        a._toggle_settings()
        deny = sum(1 for v in a.cfg.permissions.values() if v == "deny")
        self.assertEqual(a.perm_nav.full_value, f"通透模式 · 禁止 {deny}" if deny else "通透模式")
        a._open_permissions()
        a.transparent_row.toggle()
        for _ in range(100):
            if len(a.client.phrases) > 1:
                break
            time.sleep(0.01)
        self.assertEqual(a.client.phrases[-1], "transparent_off")
        self.assertEqual(a.page, "permissions")  # turning it off needs no question
        a._on_daemon("transparent", {"on": False})
        self.pump()
        self.assertEqual(a.transparent_label.winfo_manager(), "")
        self.assertTrue(a.perm_nav.full_value.startswith("询问 "))

    def test_font_change_rebuilds_the_area_and_keeps_the_page(self):
        a = self.app
        a._open_permissions()
        old = a.perm_widgets["click"]
        a._set_font(a.font_size + 2)
        self.pump()
        self.assertEqual(a.page, "permissions")
        self.assertIsNot(a.perm_widgets["click"], old)
        self.assertFalse(old.winfo_exists())
        self.assertEqual(a.font_label.cget("text"), str(self.cfg.font_size + 2))
        a._open_access()
        a._set_font(a.font_size - 1)
        self.pump()
        self.assertEqual((a.page, a.access_return), ("access", "settings"))

    # ---------- 退出 voice-claude ----------
    def ready_with(self, client, lines=None):
        a = self.app
        a.client = client
        a.ready = True
        a._lines_snapshot = (lines or {}, "p-1", "Atlas")
        closed = []
        a.quit = lambda: (closed.append(True), setattr(a, "_closing", True))
        return closed

    def wait(self, pred, timeout=2.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if pred():
                return True
            time.sleep(0.01)
        return False

    def test_quit_asks_when_a_line_is_answering(self):
        client = FakeClient()
        closed = self.ready_with(client, {"p-1": {"line": "p-1", "name": "Atlas", "state": "working"}})
        with mock.patch.object(appmod, "ask_ok", lambda *a: False) as _:
            self.app._request_quit()
        self.assertEqual((client.shutdowns, closed, self.app._quit_requested), ([], [], False))
        asked = []
        with mock.patch.object(appmod, "ask_ok", lambda parent, title, text: (asked.append(text), True)[1]):
            self.app._request_quit()
        self.assertEqual(asked, ["Atlas 正在回答。\n仍要退出吗？"])
        self.assertTrue(self.wait(lambda: client.shutdowns))
        self.assertEqual(client.shutdowns, ["ask"])
        self.assertEqual(client.confirmed, [True])  # asked here already: the service must not ask again
        # the service's bye closes the window, without the error tone; the client's own second bye is ignored
        self.app._on_daemon("bye", {"reason": "后台服务已关闭"})
        self.app._on_daemon("bye", {"reason": "后台服务的连接断了"})
        self.pump()
        self.assertEqual(closed, [True])
        self.assertEqual(self.tones, [])
        self.assertEqual(self.app.daemon_gone, "后台服务已关闭")

    def test_quit_with_running_jobs(self):
        client = FakeClient()
        closed = self.ready_with(client, {"p-1": {"line": "p-1", "name": "Atlas", "state": "working"}})
        self.app.jobs = {"8a2f": {"id": "8a2f", "state": "running", "note": "过滤"}}
        asked = []
        with mock.patch.object(appmod, "ask_ok", lambda parent, title, text: (asked.append(text), False)[1]):
            self.app._request_quit()
        self.assertEqual(client.shutdowns, [])
        # one question covers both the answering line and the job
        self.assertEqual(asked, ["Atlas 正在回答。\n有 1 个后台任务正在运行，退出会停止它。\n仍要退出吗？"])
        with mock.patch.object(appmod, "ask_ok", lambda *a: True):
            self.app._request_quit()
        self.assertTrue(self.wait(lambda: client.shutdowns))
        self.assertEqual(client.shutdowns, ["stop"])  # stopped, never left running unattended
        self.assertEqual(closed, [])  # the window closes on the service's bye, not before

    def test_quit_without_anything_running_asks_nothing(self):
        client = FakeClient()
        self.ready_with(client, {"p-1": {"line": "p-1", "name": "Atlas", "state": "idle"}})
        with mock.patch.object(appmod, "ask_ok", lambda *a: self.fail("nothing to confirm")):
            self.app._request_quit()
        self.assertTrue(self.wait(lambda: client.shutdowns))
        self.assertEqual(client.shutdowns, ["ask"])  # jobs the window has not seen are asked about by the service

    def test_quit_when_the_service_is_already_gone_just_closes(self):
        closed = self.ready_with(FakeClient())
        self.app.daemon_gone = "后台服务已关闭"
        self.app._request_quit()
        self.assertEqual(closed, [True])

    def test_a_disconnect_without_quit_still_reports(self):
        self.ready_with(FakeClient())
        self.app._on_daemon("bye", {"reason": "后台服务的连接断了"})
        self.app._on_daemon("bye", {"reason": "后台服务已关闭"})  # a late second bye changes nothing
        self.assertEqual(self.app.daemon_gone, "后台服务的连接断了")
        self.assertEqual(len(self.tones), 1)

    def test_the_window_close_button_quits_like_the_quit_row(self):
        a = self.app
        a.client = FakeClient()
        a.ready = True
        a._close_window()  # what the window's × calls
        for _ in range(100):
            if a.client.shutdowns:
                break
            time.sleep(0.01)
        self.assertEqual(a.client.shutdowns, ["ask"])
        self.assertTrue(a._quit_requested)

    def test_line_menu_has_quit(self):
        a = self.app
        a._toggle_panel()
        self.assertIsNotNone(a.panel)
        kinds = [k for k, _, _ in a.panel.rows]
        self.assertEqual(kinds[-1], "quit")
        called = []
        a.panel.on_quit = lambda: called.append(True)
        a.panel._pick(len(a.panel.rows) - 1)
        self.assertEqual(called, [True])


if __name__ == "__main__":
    unittest.main()
