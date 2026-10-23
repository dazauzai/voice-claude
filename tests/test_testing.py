import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voiceclaude import access, config, daemon, gateway, paths, testing
from voiceclaude.claude import ClaudeSession


class TestRunnerGuardTests(unittest.TestCase):
    """Under the unit-test runner nothing real starts and the user's ~/.voice-claude is never the home."""

    def test_we_are_recognised(self):
        self.assertTrue(testing.under_test_runner())
        with mock.patch.dict("os.environ", {"VOICECLAUDE_TESTING": "1"}):
            self.assertTrue(testing.under_test_runner())

    def test_home_is_temporary_unless_told_otherwise(self):
        if os.environ.get("VOICECLAUDE_HOME"):
            self.skipTest("VOICECLAUDE_HOME set by the caller")
        self.assertNotEqual(paths.HOME_DIR.resolve(), (Path.home() / ".voice-claude").resolve())
        self.assertTrue(str(paths.HOME_DIR).startswith(tempfile.gettempdir()), paths.HOME_DIR)

    def test_no_real_claude_gateway_or_daemon(self):
        s = ClaudeSession(config.Config(), "P", lambda k, d: None, line_id="guard")
        with self.assertRaises(RuntimeError):
            s.start()
        st = access.AccessStatus(True, "subscription", "", claude=r"C:\claude.exe")
        with self.assertRaises(RuntimeError):
            access._probe(st, "haiku", None, 5.0, subprocess.run)
        gw = gateway.Gateway(config.Config())  # the real spawner
        with mock.patch.object(access, "gateway_problem", lambda: ""), \
                mock.patch.object(access, "provider_key", lambda pid, path=None: "sk-0123456789abcdef0123456789"):
            ok, why = gw.ensure("openai")
        self.assertFalse(ok)
        self.assertIn("单元测试", why)
        with self.assertRaises(RuntimeError):
            daemon.spawn_detached()
        self.assertFalse(daemon.spawn_via_schtasks(["--no-relaunch"]))  # the real schtasks is never run
