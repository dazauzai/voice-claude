"""The whiteboard window process: the page's URL from daemon.json, its command line, and that nothing real starts
under the test runner. pywebview is never imported here."""
import unittest
from unittest import mock

from voiceclaude import board_window, standby


class BoardWindowTests(unittest.TestCase):
    def test_the_page_url_carries_the_project_and_the_token(self):
        url = board_window.page_url({"control_port": 50757, "board_token": "abc123"}, "p-4857fd7d", "日常")
        self.assertEqual(url, "http://127.0.0.1:50757/board/?project=p-4857fd7d&token=abc123&name=%E6%97%A5%E5%B8%B8")

    def test_the_window_is_its_own_process(self):
        cmd = board_window.command("p-1", "论文")
        self.assertEqual(cmd[1:], ["-X", "utf8", "-m", "voiceclaude.board_window", "--project", "p-1", "--name", "论文"])
        self.assertTrue(cmd[0].lower().endswith(("pythonw.exe", "python.exe")))

    def test_opening_spawns_that_command(self):
        with mock.patch.object(standby, "_spawn") as spawn:
            board_window.open_board("p-1", "论文")
        spawn.assert_called_once_with(board_window.command("p-1", "论文"))

    def test_a_board_already_open_comes_to_the_front_instead_of_a_second_window(self):
        with mock.patch.object(board_window, "bring_up", return_value=True) as up,                 mock.patch.object(standby, "_spawn") as spawn:
            board_window.open_board("p-1", "论文")
        up.assert_called_once_with("论文 白板 — voice-claude")
        spawn.assert_not_called()

    def test_the_window_title_is_what_the_desktop_tools_take_for_ours(self):
        from voiceclaude import desktop
        for name in ("论文", ""):
            self.assertRegex(board_window.title(name), desktop.VOICE_TITLE)

    def test_nothing_real_under_tests(self):
        with self.assertRaises(RuntimeError):
            board_window.main()
        with self.assertRaises(RuntimeError):
            board_window.open_board("p-1")  # _spawn refuses under the test runner


if __name__ == "__main__":
    unittest.main()
