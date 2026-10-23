import os
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from voiceclaude import desktop
from voiceclaude.control import ControlServer

APPS = dict(desktop.DEFAULT_APPS)


class KeyTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(desktop.parse_keys("Ctrl+Shift+S"), ["ctrl", "shift", "s"])
        self.assertEqual(desktop.parse_keys("Enter"), ["enter"])
        self.assertEqual(desktop.parse_keys("windows + r"), ["win", "r"])
        self.assertEqual(desktop.parse_keys("Alt+F4"), ["alt", "f4"])

    def test_unknown_key(self):
        with self.assertRaises(ValueError):
            desktop.parse_keys("ctrl+banana")
        with self.assertRaises(ValueError):
            desktop.parse_keys("")

    def test_spoken(self):
        self.assertEqual(desktop.spoken_keys(["ctrl", "s"]), "Control 加 S")
        self.assertEqual(desktop.spoken_keys(["enter"]), "回车")
        self.assertEqual(desktop.spoken_keys(["win", "r"]), "Windows 键 加 R")


class TextAndAppTests(unittest.TestCase):
    def test_spoken_text(self):
        self.assertEqual(desktop.spoken_text("hello"), "输入文字：hello")
        self.assertEqual(desktop.spoken_text("第一行\n第二行"), "输入文字：第一行，换行，第二行")
        long = "a" * 100
        s = desktop.spoken_text(long)
        self.assertTrue(s.startswith("输入文字：" + "a" * 40))
        self.assertIn("一共 100 个字", s)

    def test_question(self):
        self.assertEqual(desktop.question("打开记事本"), "我想打开记事本，可以吗？")
        self.assertEqual(desktop.question("按回车", "为了保存文件。"), "为了保存文件，我想按回车，可以吗？")

    def test_resolve_app(self):
        self.assertEqual(desktop.resolve_app("记事本", APPS), ("记事本", "notepad"))
        self.assertEqual(desktop.resolve_app("Notepad.exe", APPS), ("记事本", "notepad"))
        self.assertEqual(desktop.resolve_app("VS Code", APPS), ("vs code", "code"))
        self.assertIsNone(desktop.resolve_app("Photoshop", APPS))
        self.assertIsNone(desktop.resolve_app("cmd /c del *", APPS))

    def test_user_entries_win(self):
        table = {**APPS, "记事本": "notepad++", "笔记": "C:\\Users\\you\\Documents\\notes.xlsx"}
        self.assertEqual(desktop.resolve_app("记事本", table), ("记事本", "notepad++"))
        self.assertEqual(desktop.resolve_app("笔记", table), ("笔记", "C:\\Users\\you\\Documents\\notes.xlsx"))

    def test_scale(self):
        with mock.patch.object(desktop.pyautogui, "size", return_value=(3840, 2160)):
            self.assertEqual(desktop.shot_scale(), 3.0)
            self.assertEqual(desktop.to_screen(100, 50), (300, 150))
        with mock.patch.object(desktop.pyautogui, "size", return_value=(1280, 720)):
            self.assertEqual(desktop.shot_scale(), 1.0)


class GateTests(unittest.TestCase):
    """The hard rule: nothing happens unless the user said yes."""

    def setUp(self):
        self.asked = []
        self.actions = []

        def fake_ask(action, question, detail="", facts=None):
            self.actions.append(action)
            self.asked.append(question)
            return self.answer, "用户没有同意" if not self.answer else ""
        self.answer = False
        self.p_ask = mock.patch.object(desktop, "ask", fake_ask)
        self.p_apps = mock.patch.object(desktop, "apps", lambda: APPS)
        self.p_win = mock.patch.object(desktop, "active_window", lambda: "测试窗口")
        self.p_front = mock.patch.multiple(desktop, _fg_hwnd=lambda: 7, _in_menu_mode=lambda h: False,
                                           _window_handles=lambda: set(), _bring_front=lambda before: None, _root=lambda h: h, _pid=lambda h: h, _process_image=lambda h: "", _settle=lambda: None,
                                           _all_windows=lambda: {1, 2, 5, 7, 10, 11})
        self.p_launch = mock.patch.object(desktop, "_launch")
        self.p_type = mock.patch.object(desktop, "_send_unicode")
        # Only the functions that move or look at things are faked; key tables and exceptions stay real.
        self.p_gui = mock.patch.multiple(desktop.pyautogui, click=mock.DEFAULT, hotkey=mock.DEFAULT, press=mock.DEFAULT,
                                         scroll=mock.DEFAULT, moveTo=mock.DEFAULT, screenshot=mock.DEFAULT,
                                         size=mock.DEFAULT)
        self.launch = self.p_launch.start()
        self.typed = self.p_type.start()
        self.gui = mock.Mock(**self.p_gui.start())
        self.gui.size.return_value = (2560, 1440)
        self.p_ask.start()
        self.p_apps.start()
        self.p_win.start()
        self.p_front.start()

    def tearDown(self):
        for p in (self.p_ask, self.p_apps, self.p_win, self.p_front, self.p_launch, self.p_type, self.p_gui):
            p.stop()

    def test_denied_does_nothing(self):
        self.assertIn("没有同意", desktop.open_app("记事本"))
        self.assertIn("没有同意", desktop.type_text("hello"))
        self.assertIn("没有同意", desktop.click(10, 10, "按钮"))
        self.assertIn("没有同意", desktop.press_keys("ctrl+s"))
        self.assertIn("没有同意", desktop.scroll(-5, "页面"))
        self.assertIn("没有同意", desktop.screenshot())
        self.launch.assert_not_called()
        self.typed.assert_not_called()
        self.gui.click.assert_not_called()
        self.gui.hotkey.assert_not_called()
        self.gui.press.assert_not_called()
        self.gui.scroll.assert_not_called()
        self.gui.screenshot.assert_not_called()
        # typing and key presses say WHICH window they land in, so the user can refuse the wrong one
        self.assertEqual(self.asked, ["我想打开记事本，可以吗？", "我想在“测试窗口”里输入文字：hello，可以吗？", "我想点一下按钮，可以吗？",
                                      "我想在“测试窗口”里按Control 加 S，可以吗？", "我想在页面往下滚，可以吗？", "我想看一眼屏幕，可以吗？"])
        # the permissions-table keys the app looks up (must match config.PERMISSION_LABELS)
        self.assertEqual(self.actions, ["open_app", "type", "click", "hotkey", "scroll", "screenshot"])

    def test_approved_runs(self):
        self.answer = True
        self.assertIn("已打开记事本", desktop.open_app("notepad", purpose="为了记笔记"))
        self.launch.assert_called_once_with("notepad")
        self.assertEqual(self.asked[-1], "为了记笔记，我想打开记事本，可以吗？")
        desktop.type_text("hello")
        self.typed.assert_called_once_with("hello")
        desktop.click(100, 50, "保存按钮", double=True)
        self.gui.click.assert_called_once_with(200, 100, clicks=2, button="left")
        desktop.press_keys("ctrl+s")
        self.gui.hotkey.assert_called_once_with("ctrl", "s")
        desktop.press_keys("enter")
        self.gui.press.assert_called_once_with("enter")

    def test_unknown_app_is_refused_without_asking(self):
        self.answer = True
        out = desktop.open_app("Photoshop")
        self.assertIn("清单", out)
        self.assertEqual(self.asked, [])
        self.launch.assert_not_called()

    def test_bad_input_refused_without_asking(self):
        self.answer = True
        self.assertIn("没有执行", desktop.press_keys("ctrl+banana"))
        self.assertIn("没有执行", desktop.click(1, 1, "x", button="sideways"))
        self.assertEqual(self.asked, [])

    def test_failsafe_reported(self):
        self.answer = True
        self.gui.click.side_effect = desktop.pyautogui.FailSafeException("corner")
        self.assertIn("中止", desktop.click(1, 1, "按钮"))


class BatchTests(unittest.TestCase):
    """run_steps: one question for several steps when the app allows it, else step by step; never without a yes."""

    STEPS = [{"tool": "open_app", "args": {"name": "记事本"}}, {"tool": "type_text", "args": {"text": "hello"}}]

    def setUp(self):
        self.posts = []
        self.replies = []

        def fake_post(payload):
            self.posts.append(payload)
            return self.replies.pop(0) if self.replies else {"ok": False, "message": "no reply"}
        self.fg = [1]
        self.titles = {1: "W", 2: "无标题 - 记事本"}
        self.patches = [mock.patch.object(desktop, "_post", fake_post), mock.patch.object(desktop, "apps", lambda: APPS),
                        mock.patch.object(desktop, "active_window", lambda: self.titles.get(self.fg[0], "")),
                        mock.patch.object(desktop, "_launch", side_effect=lambda cmd: self.fg.__setitem__(0, 2)),
                        mock.patch.object(desktop, "_send_unicode"),
                        mock.patch.object(desktop.time, "sleep", lambda s: None),
                        mock.patch.multiple(desktop, _fg_hwnd=lambda: self.fg[0], _in_menu_mode=lambda h: False,
                                            _window_handles=lambda: {1}, _bring_front=lambda before: None, _root=lambda h: h, _pid=lambda h: h, _process_image=lambda h: "", _settle=lambda: None,
                                            _all_windows=lambda: set(self.existing))]
        self.existing = {1, 7}  # windows already there: W, and a terminal; anything else showing up is new
        started = [p.start() for p in self.patches]
        self.launch, self.typed = started[3], started[4]

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_one_question_for_all_steps(self):
        self.replies = [{"ok": True}]
        out = desktop.run_steps(self.STEPS, purpose="为了记笔记")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["action"], "batch")
        self.assertEqual(self.posts[0]["actions"], ["open_app", "type"])
        # the typing is announced for the window open_app brings up, not the one in front while asking
        self.assertEqual(self.posts[0]["question"], "为了记笔记，我想打开记事本，然后在“记事本”里输入文字：hello，可以吗？")
        self.launch.assert_called_once_with("notepad")
        self.typed.assert_called_once_with("hello")
        self.assertIn("1. 已打开记事本", out)
        self.assertIn("2. 已输入 5 个字", out)

    def test_per_step_when_user_wants_each_asked(self):
        self.replies = [{"ok": False, "per_step": True}, {"ok": True}, {"ok": False, "message": "用户没有同意"}]
        out = desktop.run_steps(self.STEPS)
        self.assertEqual([p["action"] for p in self.posts], ["batch", "open_app", "type"])
        self.launch.assert_called_once_with("notepad")
        self.typed.assert_not_called()
        self.assertIn("没有同意", out)

    def test_denied_runs_nothing(self):
        self.replies = [{"ok": False, "message": "用户没有同意"}]
        out = desktop.run_steps(self.STEPS)
        self.assertIn("没有同意", out)
        self.launch.assert_not_called()
        self.typed.assert_not_called()

    def test_typing_after_open_needs_the_opened_window_in_front(self):
        """open_app did not bring a new window up: the typing step refuses instead of typing into whatever is there."""
        self.replies = [{"ok": True}]
        with mock.patch.object(desktop, "_launch"):  # no new window appears
            out = desktop.run_steps(self.STEPS + [{"tool": "press_keys", "args": {"keys": "enter"}}])
        self.typed.assert_not_called()
        self.assertIn("2. 没有做：“记事本”没有到前台", out)
        self.assertIn("后面的步骤没有做", out)
        self.assertNotIn("3.", out)
        out = desktop.run_steps([{"tool": "type_text", "args": {"text": "x", "window": "evil"}}])  # an internal argument
        self.assertEqual(self.posts[-1]["facts"], [{"kind": "type", "text": "x", "window": "W"}])

    def test_save_as_dialog_after_ctrl_s_takes_the_typing(self):
        """[open 记事本, type, Ctrl+S, type a name, Enter]: the name goes into the Save As dialog Notepad opened."""
        self.replies = [{"ok": True}]
        self.titles[3] = "另存为"
        roots = {3: 2}  # the dialog is owned by Notepad
        hotkey = mock.Mock(side_effect=lambda *k: self.fg.__setitem__(0, 3))
        with mock.patch.object(desktop, "_root", lambda h: roots.get(h, h)), \
                mock.patch.multiple(desktop.pyautogui, hotkey=hotkey, press=mock.DEFAULT) as gui:
            out = desktop.run_steps([{"tool": "open_app", "args": {"name": "记事本"}}, {"tool": "type_text", "args": {"text": "hello"}},
                                     {"tool": "press_keys", "args": {"keys": "ctrl+s"}}, {"tool": "type_text", "args": {"text": "hello.txt"}},
                                     {"tool": "press_keys", "args": {"keys": "enter"}}])
        self.assertNotIn("没有做", out)
        self.assertEqual([c.args[0] for c in self.typed.call_args_list], ["hello", "hello.txt"])
        hotkey.assert_called_once_with("ctrl", "s")
        gui["press"].assert_called_once_with("enter")

    def test_a_dialog_from_any_click_or_key_step_takes_what_follows(self):
        """A window that came up after a click in the same run is where the next keys go (approved at once)."""
        self.replies = [{"ok": True}]
        self.titles[9] = "选项"
        with mock.patch.multiple(desktop.pyautogui, click=mock.Mock(side_effect=lambda *a, **k: self.fg.__setitem__(0, 9)),
                                 press=mock.DEFAULT) as gui:
            out = desktop.run_steps([{"tool": "click", "args": {"x": 1, "y": 2, "target": "设置按钮"}},
                                     {"tool": "press_keys", "args": {"keys": "tab"}}])
        self.assertNotIn("没有做", out)
        gui["press"].assert_called_once_with("tab")

    def test_open_reuses_a_running_window_and_ignores_a_stray_popup(self):
        """Windows 11 Notepad opens a tab in the window that is already there; a popup that shows up is not it."""
        self.replies = [{"ok": True}]
        self.existing = {1, 2, 7}  # Notepad is already open
        self.fg[0], self.titles[2] = 2, "a.txt - 记事本"

        def new_tab(cmd):
            self.titles[2] = "无标题 - 记事本"  # same window, new title
        with mock.patch.object(desktop, "_launch", side_effect=new_tab), \
                mock.patch.object(desktop, "_process_image", lambda h: "Notepad.exe"):
            out = desktop.run_steps(self.STEPS)
        self.assertIn("2. 已输入 5 个字", out)
        # the same existing window with a changed title, but another program (a terminal): not taken
        self.replies = [{"ok": True}]
        self.typed.reset_mock()
        self.fg[0], self.titles[2] = 2, "build - 记事本 notes - Windows Terminal"
        with mock.patch.object(desktop, "_launch", side_effect=lambda cmd: self.titles.__setitem__(2, "done 记事本 - Windows Terminal")), \
                mock.patch.object(desktop, "_process_image", lambda h: "WindowsTerminal.exe"):
            out = desktop.run_steps(self.STEPS)
        self.assertIn("没有到前台", out)
        self.typed.assert_not_called()
        self.replies = [{"ok": True}]
        self.typed.reset_mock()
        self.fg[0], self.titles[8] = 1, "有可用更新"

        def popup(cmd):
            self.fg[0] = 8  # someone else's window, neither named 记事本 nor notepad.exe
        with mock.patch.object(desktop, "_launch", side_effect=popup), \
                mock.patch.object(desktop, "_process_image", lambda h: "updater.exe"):
            out = desktop.run_steps(self.STEPS)
        self.assertIn("没有到前台", out)
        self.typed.assert_not_called()
        self.replies = [{"ok": True}]
        self.fg[0], self.titles[4] = 1, "Untitled - Notepad"  # an English Notepad: recognised by its program file

        def english(cmd):
            self.fg[0] = 4
        with mock.patch.object(desktop, "_launch", side_effect=english), \
                mock.patch.object(desktop, "_process_image", lambda h: "Notepad.exe"):
            out = desktop.run_steps(self.STEPS)
        self.assertIn("2. 已输入 5 个字", out)

    def test_a_later_step_never_answers_a_replace_prompt(self):
        """[type a name, Enter, type more] in a Save As: if the name exists, Windows asks "replace it?"; the next step
        stops there instead of typing into that prompt (a 'y' would overwrite the file)."""
        self.replies = [{"ok": True}]
        self.titles[1], self.titles[12] = "另存为", "确认另存为"
        with mock.patch.object(desktop, "dialog_folder", lambda h: ""), \
                mock.patch.multiple(desktop.pyautogui, press=mock.Mock(side_effect=lambda *k: self.fg.__setitem__(0, 12))):
            out = desktop.run_steps([{"tool": "type_text", "args": {"text": "test.txt"}}, {"tool": "press_keys", "args": {"keys": "enter"}},
                                     {"tool": "type_text", "args": {"text": "yes"}}])
        self.assertIn("3. 没有做：弹出了“确认另存为”：会覆盖已有文件", out)
        self.assertEqual([c.args[0] for c in self.typed.call_args_list], ["test.txt"])

    def test_typing_into_save_as_carries_its_folder(self):
        self.titles[1] = "另存为"
        with mock.patch.object(desktop, "dialog_folder", lambda h: r"C:\Users\you\Documents"):
            self.assertEqual(desktop.plan_type("test.txt").facts,
                             {"kind": "type", "text": "test.txt", "window": "另存为", "folder": r"C:\Users\you\Documents"})
        self.titles[1] = "笔记 - 记事本"
        with mock.patch.object(desktop, "dialog_folder", side_effect=AssertionError("not a save dialog")):
            self.assertNotIn("folder", desktop.plan_type("x").facts)

    def test_a_window_a_click_merely_activated_is_refused(self):
        """Stale coordinates: the click lands on a terminal that was already open. The typing the user heard was for
        Notepad, so it is not done there (and neither after Alt+Tab)."""
        self.titles[7] = "Windows Terminal"
        for first in ({"tool": "click", "args": {"x": 1, "y": 2, "target": "记事本的编辑区"}},
                      {"tool": "press_keys", "args": {"keys": "alt+tab"}}):
            self.replies = [{"ok": True}]
            self.fg[0] = 1
            with mock.patch.multiple(desktop.pyautogui, click=mock.Mock(side_effect=lambda *a, **k: self.fg.__setitem__(0, 7)),
                                     hotkey=mock.Mock(side_effect=lambda *k: self.fg.__setitem__(0, 7))):
                out = desktop.run_steps([first, {"tool": "type_text", "args": {"text": "dir\n"}}])
            self.assertIn("2. 没有做：前台窗口变了", out)
        self.typed.assert_not_called()

    def test_the_brake_stops_the_remaining_steps(self):
        """Approved at once (brake count 0 in the reply), then the brake is pulled: no further step runs."""
        brake = [0]

        def post(payload, path="ask"):
            self.posts.append((path, payload))
            if path == "brake":
                return {"ok": True, "brake": brake[0]}
            return {"ok": True, "brake": 0}

        def typed_then_brake(text):
            brake[0] = 1
        self.typed.side_effect = typed_then_brake
        with mock.patch.object(desktop, "_post", post), mock.patch.object(desktop.pyautogui, "press") as press:
            out = desktop.run_steps([{"tool": "type_text", "args": {"text": "a"}}, {"tool": "press_keys", "args": {"keys": "enter"}}])
        self.assertIn("1. 已输入 1 个字", out)
        self.assertIn("2. 没有做：用户按了紧急停止", out)
        press.assert_not_called()
        with mock.patch.object(desktop, "_post", lambda payload, path="ask": {"ok": False} if path == "brake" else {"ok": True, "brake": 0}):
            self.typed.reset_mock()
            out = desktop.run_steps([{"tool": "type_text", "args": {"text": "a"}}])
        self.assertIn("紧急停止", out)  # the service cannot be reached: counts as stopped
        self.typed.assert_not_called()

    def test_step_by_step_stops_at_the_first_no(self):
        self.replies = [{"ok": False, "per_step": True}, {"ok": False, "message": "用户没有同意"}, {"ok": True}]
        out = desktop.run_steps(self.STEPS)
        self.assertEqual([p["action"] for p in self.posts], ["batch", "open_app"])  # step 2 is never asked
        self.assertIn("后面的步骤没有做", out)
        self.launch.assert_not_called()
        self.typed.assert_not_called()

    def test_asked_step_by_step_the_focus_is_not_taken_from_whatever_came_up(self):
        """Asked step by step, an approval can move the focus (to the voice-claude window): the step after a keys step
        does not take that window as 'what the keys brought up'."""
        self.replies = [{"ok": False, "per_step": True}, {"ok": True}, {"ok": True}]
        self.titles[50] = "voice-claude"

        def answer_moves_focus(payload):
            self.posts.append(payload)
            if payload.get("action") == "type":
                self.fg[0] = 50
            return self.replies.pop(0) if self.replies else {"ok": False}
        with mock.patch.object(desktop, "_post", answer_moves_focus), \
                mock.patch.multiple(desktop.pyautogui, hotkey=mock.DEFAULT, press=mock.DEFAULT), \
                mock.patch.object(desktop, "_activate", lambda h: False):
            out = desktop.run_steps([{"tool": "press_keys", "args": {"keys": "ctrl+a"}}, {"tool": "type_text", "args": {"text": "x"}}])
        self.assertIn("2. 没有做：前台窗口变了", out)
        self.typed.assert_not_called()

    def test_bad_step_refused_without_asking(self):
        out = desktop.run_steps([{"tool": "screenshot", "args": {}}])
        self.assertIn("没有执行", out)
        out2 = desktop.run_steps([{"tool": "open_app", "args": {"name": "Photoshop"}}])
        self.assertIn("清单", out2)
        self.assertEqual(self.posts, [])


class WindowCheckTests(unittest.TestCase):
    """Right before typing or pressing keys: the window the user heard about is in front and not in a menu."""

    def setUp(self):
        self.fg = [10]
        self.esc = []
        self.menu = [False]
        mock.patch.multiple(desktop, _fg_hwnd=lambda: self.fg[0], _in_menu_mode=lambda h: self.menu[0],
                            _press_esc=self._esc, active_window=lambda: "笔记 - 记事本", _root=lambda h: h, _pid=lambda h: h, _process_image=lambda h: "", _settle=lambda: None,
                                           _all_windows=lambda: {1, 2, 5, 7, 10, 11}).start()
        mock.patch.object(desktop, "ask", lambda *a, **k: (True, "")).start()
        self.typed = mock.patch.object(desktop, "_send_unicode").start()
        self.gui = mock.patch.multiple(desktop.pyautogui, press=mock.DEFAULT, hotkey=mock.DEFAULT).start()

    def _esc(self):
        self.esc.append(1)
        if self.esc_closes:
            self.menu[0] = False

    esc_closes = True

    def tearDown(self):
        mock.patch.stopall()

    def test_focus_moved_away_and_cannot_come_back(self):
        plan = desktop.plan_type("hello")
        self.assertEqual(plan.what, "在“记事本（笔记）”里输入文字：hello")
        self.fg[0] = 11  # say the answer was a click in the voice-claude window
        with mock.patch.object(desktop, "_activate", lambda h: False):
            out = desktop._guard(plan.go)
        self.assertTrue(out.startswith("没有做：前台窗口变了"), out)
        self.typed.assert_not_called()
        self.fg[0] = 10
        plan = desktop.plan_press("enter")
        self.fg[0] = 11
        with mock.patch.object(desktop, "_activate", lambda h: False):
            self.assertTrue(desktop._guard(plan.go).startswith("没有做"))
        self.gui["press"].assert_not_called()

    def test_focus_brought_back_to_the_announced_window(self):
        plan = desktop.plan_type("hello")
        self.fg[0] = 11

        def back(h):
            self.fg[0] = h
            return True
        with mock.patch.object(desktop, "_activate", back):
            self.assertEqual(desktop._guard(plan.go), "已输入 5 个字。")
        self.typed.assert_called_once_with("hello")

    def test_a_menu_opened_since_the_request_is_left_first(self):
        plan = desktop.plan_type("hi")  # no menu while asking
        self.menu[0] = True  # the approval's Alt opened the menu bar
        self.assertEqual(desktop._guard(plan.go), "已输入 2 个字。")
        self.assertEqual(self.esc, [1])
        plan = desktop.plan_press("ctrl+s")
        self.menu[0], self.esc_closes = True, False  # stays in the menu
        self.assertIn("停在菜单里", desktop._guard(plan.go))
        self.gui["hotkey"].assert_not_called()

    def test_a_menu_open_while_asking_is_used_not_closed(self):
        """The user opened 编辑 (or the model right-clicked) and then asked for 'down': the keys go into that menu."""
        self.menu[0] = True
        plan = desktop.plan_press("down")
        self.assertEqual(desktop._guard(plan.go), "已按 down。")
        self.assertEqual(self.esc, [])
        self.gui["press"].assert_called_once_with("down")

    def test_keys_after_a_right_click_in_one_run_act_in_its_menu(self):
        posts = []
        with mock.patch.object(desktop, "_post", lambda p: (posts.append(p), {"ok": True})[1]), \
                mock.patch.object(desktop.pyautogui, "click", side_effect=lambda *a, **k: self.menu.__setitem__(0, True)):
            out = desktop.run_steps([{"tool": "click", "args": {"x": 1, "y": 2, "target": "文件", "button": "right"}},
                                     {"tool": "press_keys", "args": {"keys": "down"}}, {"tool": "press_keys", "args": {"keys": "enter"}}])
        self.assertNotIn("没有做", out)
        self.assertEqual(self.esc, [])  # the menu the click opened on purpose is not closed
        self.assertEqual([c.args[0] for c in self.gui["press"].call_args_list], ["down", "enter"])

    def test_typing_stops_at_the_corner(self):
        """Typing goes through SendInput, not pyautogui: the corner fail-safe is checked before every character."""
        mock.patch.stopall()
        sent, checks = [], []

        def check():
            checks.append(1)
            if len(checks) > 2:
                raise desktop.pyautogui.FailSafeException("corner")
        with mock.patch.object(desktop, "_send_input", lambda arr: (sent.append(arr[0].u.ki.wScan), 2)[1]), \
                mock.patch.object(desktop.pyautogui, "failSafeCheck", check), \
                mock.patch.object(desktop.time, "sleep", lambda s: None):
            out = desktop._guard(lambda: desktop._send_unicode("abcdef"))
        self.assertEqual(sent, [ord("a"), ord("b")])
        self.assertIn("屏幕角落", out)
        with self.assertRaises(RuntimeError):  # and the real SendInput is refused under the test runner
            desktop._send_input(None)

    def test_menu_probe_reads_a_real_thread_without_side_effects(self):
        mock.patch.stopall()
        self.assertEqual(desktop.ctypes.sizeof(desktop._GUITHREADINFO), 72 if desktop.ctypes.sizeof(desktop.ctypes.c_void_p) == 8 else 48)
        h = desktop._fg_hwnd()
        self.assertIsInstance(desktop._in_menu_mode(h), bool)
        self.assertFalse(desktop._activate(h))  # refused under the test runner
        self.assertIsInstance(desktop._process_image(h), str)  # read-only probes of the real desktop
        self.assertEqual(desktop._root(0), 0)
        self.assertFalse(desktop._related(0, 0x7FFF0001))
        self.assertIsInstance(desktop._handler(".txt"), str)
        self.assertEqual(desktop.dialog_folder(0), "")  # reading a window's address bar: nothing for no window


class WireTests(unittest.TestCase):
    """desktop.ask talks to the app's control endpoint the same way the app expects."""

    def test_ask_round_trip(self):
        seen = []

        def handler(path, data):
            seen.append((path, data))
            return {"ok": data["question"].startswith("我想打开"), "message": "答复"}
        srv = ControlServer(handler)
        try:
            with mock.patch.object(desktop, "PORT", str(srv.port)):
                self.assertEqual(desktop.ask("open_app", "我想打开记事本，可以吗？", "命令：notepad"), (True, "答复"))
                self.assertEqual(desktop.ask("type", "我想输入文字：hello，可以吗？")[0], False)
        finally:
            srv.close()
        self.assertEqual(seen[0], ("ask", {"action": "open_app", "question": "我想打开记事本，可以吗？", "detail": "命令：notepad",
                                           "line": desktop.LINE}))

    def test_action_keys_exist_in_permissions_table(self):
        from voiceclaude.config import PERMISSION_LABELS
        for key in ("screenshot", "click", "type", "hotkey", "scroll", "open_app"):
            self.assertIn(key, PERMISSION_LABELS)

    def test_no_app_running(self):
        with mock.patch.object(desktop, "PORT", ""):
            ok, msg = desktop.ask("click", "x")
        self.assertFalse(ok)
        self.assertIn("没有在运行", msg)
        with mock.patch.object(desktop, "PORT", "1"):  # nothing listens there
            ok, msg = desktop.ask("click", "x")
        self.assertFalse(ok)
        self.assertIn("没有执行", msg)


if __name__ == "__main__":
    unittest.main()


class OpenPathTests(unittest.TestCase):
    """open_path: one request, the last name spoken, the full path shown; programs and missing paths never asked."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.posts = []
        mock.patch.object(desktop, "_post", lambda payload: (self.posts.append(payload), {"ok": True, "message": "用户同意了。"})[1]).start()
        mock.patch.object(desktop, "_window_handles", lambda: set()).start()
        mock.patch.object(desktop, "_bring_front", lambda before: None).start()
        mock.patch.object(desktop, "active_window", lambda: "figures").start()
        mock.patch.multiple(desktop, _fg_hwnd=lambda: 5, _in_menu_mode=lambda h: False, _root=lambda h: h, _pid=lambda h: h, _process_image=lambda h: "", _settle=lambda: None,
                                           _all_windows=lambda: {1, 2, 5, 7, 10, 11}).start()
        mock.patch.object(desktop.time, "sleep", lambda s: None).start()
        self.opened = []
        mock.patch.object(desktop.os, "startfile", lambda p: self.opened.append(p), create=True).start()

    def tearDown(self):
        mock.patch.stopall()

    def test_folder_is_one_request_spoken_by_its_name(self):
        folder = self.tmp / "OneDrive" / "Documents" / "report" / "figures"
        folder.mkdir(parents=True)
        out = desktop.open_path(str(folder), purpose="看看稿子")
        self.assertIn("已打开文件夹 figures", out)
        self.assertEqual(self.opened, [str(folder)])
        self.assertEqual(len(self.posts), 1)
        q = self.posts[0]
        self.assertEqual(q["action"], "open_app")
        self.assertEqual(q["question"], "看看稿子，我想打开文件夹 figures，可以吗？")  # the long path is not spoken
        self.assertIn(str(folder), q["detail"])
        doc = self.tmp / "draft.docx"
        doc.write_text("x", "utf-8")
        self.assertIn("已打开文件 draft.docx", desktop.open_path(f'"{doc}"'))

    def test_missing_path_and_programs_are_refused_without_asking(self):
        self.assertIn("路径不存在", desktop.open_path(str(self.tmp / "nope")))
        for name in ("evil.bat", "setup.exe", "run.ps1", "shortcut.lnk", "thing.msi", "tool.py", "Tool.PYW", "app.pyz",
                     "x.pyc", "console.msc", "help.chm", "page.hta", "fix.reg", "screen.scr"):
            f = self.tmp / name
            f.write_text("x", "utf-8")
            self.assertIn("不开程序", desktop.open_path(str(f)), name)
        for name in ("setup.sh", "run.bash", "hotkeys.ahk", "x.au3", "a.pl", "b.rb", "c.tcl", "d.php", "e.lua",
                     "addin.vsto", "app.xbap", "server.rdp"):
            f = self.tmp / name
            f.write_text("x", "utf-8")
            self.assertIn("不开程序", desktop.open_path(str(f)), name)
        f = self.tmp / "job.mytype"
        f.write_text("x", "utf-8")
        with mock.patch.dict(os.environ, {"PATHEXT": ".COM;.EXE;.MYTYPE"}):  # whatever this machine runs directly
            self.assertIn("不开程序", desktop.open_path(str(f)))
        handlers = {".xyz": r"C:\Program Files\Git\git-bash.exe", ".abc": r"C:\Tools\FooInstaller.exe",
                    ".doc2": r"C:\Program Files\Office\WINWORD.EXE", ".p3": r"C:\Store\python3.12.exe",
                    ".tk": r"C:\Tcl\bin\tclsh86t.exe", ".l5": r"C:\Lua\lua54.exe"}
        with mock.patch.object(desktop, "_handler", lambda s: handlers.get(s, "")):
            for name, refused in (("a.xyz", True), ("b.abc", True), ("c.doc2", False), ("noext", False), ("d.p3", True),
                                  ("e.tk", True), ("f.l5", True), ("g.jnlp", True), ("h.msu", True)):
                self.assertEqual(desktop.is_program(self.tmp / name), refused, name)
        self.assertIn("没有给出路径", desktop.open_path("  "))
        self.assertEqual((self.posts, self.opened), ([], []))

    def test_open_path_inside_run_steps(self):
        a, b = self.tmp / "a", self.tmp / "b"
        a.mkdir()
        b.mkdir()
        mock.patch.object(desktop, "_post", lambda payload: (self.posts.append(payload), {"ok": True})[1]).start()
        out = desktop.run_steps([{"tool": "open_path", "args": {"path": str(a)}}, {"tool": "open_path", "args": {"path": str(b)}}],
                                purpose="整理两个文件夹")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["actions"], ["open_app", "open_app"])
        self.assertEqual(self.posts[0]["question"], "整理两个文件夹，我想打开文件夹 a，然后打开文件夹 b，可以吗？")
        self.assertEqual(self.opened, [str(a), str(b)])
        self.assertIn("1. 已打开文件夹 a", out)

    def test_facts_travel_with_every_ask_and_a_window_is_named_once(self):
        self.assertEqual(desktop.short_title("收件箱 - Alex - Outlook - Google Chrome"), "Google Chrome（收件箱）")
        self.assertEqual(desktop.short_title("Notepad"), "Notepad")
        self.assertEqual(desktop.join_steps(["在“Chrome（邮件）”里输入文字：tract", "在“Chrome（邮件）”里按回车"]),
                         "在“Chrome（邮件）”里：输入文字：tract，然后按回车")
        self.assertEqual(desktop.join_steps(["打开记事本", "在“W”里输入文字：hello", "在“W”里按回车"]),
                         "打开记事本，然后在“W”里输入文字：hello，然后按回车")
        plan = desktop.plan_press("Ctrl+D")
        self.assertEqual(plan.facts, {"kind": "hotkey", "keys": ["ctrl", "d"], "window": "figures"})
        self.assertEqual(desktop.plan_type("rm x").facts, {"kind": "type", "text": "rm x", "window": "figures"})
        self.assertEqual(desktop.plan_click(1, 2, "删除").facts, {"kind": "click", "target": "删除", "window": "figures"})
        posts = []
        with mock.patch.object(desktop, "_post", lambda payload: (posts.append(payload), {"ok": False, "message": "用户没有同意"})[1]):
            desktop.press_keys("Delete")
        self.assertEqual(posts[0]["facts"], {"kind": "hotkey", "keys": ["delete"], "window": "figures"})

    def test_typed_paths_are_spoken_by_their_last_name(self):
        long = r"C:\Users\you\OneDrive\Documents\report\figures"
        self.assertEqual(desktop.spoken_text(long), "输入路径……figures")
        self.assertEqual(desktop.spoken_text(r"\\server\share\data"), "输入路径……data")
        self.assertEqual(desktop.spoken_text("hello"), "输入文字：hello")
        self.assertFalse(desktop.looks_like_path("C: drive is full"))
        plan = desktop.plan_type(long)
        self.assertEqual(plan.detail, long)  # the window shows the whole thing
