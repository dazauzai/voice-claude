import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voiceclaude import config, paths
from voiceclaude.claude import ClaudeSession


class ArgsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p = mock.patch.object(paths, "LINES_DIR", self.tmp / "lines")
        self.p.start()
        mock.patch("voiceclaude.claude.find_claude", lambda cmd: "claude.exe").start()
        from voiceclaude import access
        mock.patch.object(access, "detect", lambda cmd: access.AccessStatus(True, "subscription", "测试")).start()

    def tearDown(self):
        mock.patch.stopall()

    def test_args_include_resume_model_and_servers(self):
        cfg = config.Config()
        cfg.permissions["other_connectors"] = "deny"
        s = ClaudeSession(cfg, "P", lambda k, d: None, cwd="C:/x", line_id="l1", resume="abc", model="sonnet",
                          servers={"manager": {"command": "py", "args": []}}, allowed=["mcp__manager"])
        s.port = 4242
        args = s.build_args()
        self.assertEqual(args[args.index("--resume") + 1], "abc")
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("--no-chrome", args)
        cfg_json = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text("utf-8"))
        self.assertEqual(list(cfg_json["mcpServers"]), ["manager"])
        env = cfg_json["mcpServers"]["manager"]["env"]
        self.assertEqual((env["VOICECLAUDE_LINE"], env["VOICECLAUDE_PORT"]), ("l1", "4242"))
        self.assertEqual(args[args.index("--allowedTools") + 1], "mcp__manager")
        self.assertEqual(s.cwd, "C:/x")

    def test_defaults_are_a_single_line_with_our_two_servers(self):
        cfg = config.Config()
        cfg.permissions["other_connectors"] = "allow"
        s = ClaudeSession(cfg, "P", lambda k, d: None)
        args = s.build_args()
        self.assertEqual(args[args.index("--allowedTools") + 1], "mcp__voiceclaude,mcp__desktop")
        self.assertNotIn("--resume", args)
        self.assertNotIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--model") + 1], cfg.model)
        cfg_json = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text("utf-8"))
        self.assertEqual(set(cfg_json["mcpServers"]), {"voiceclaude", "desktop"})
        self.assertTrue(cfg_json["mcpServers"]["desktop"]["args"][-1].endswith("desktop.py"))

    def test_env_loads_tools_upfront_unless_user_says_otherwise(self):
        s = ClaudeSession(config.Config(), "P", lambda k, d: None, line_id="l9", extra_env={"X": "1"})
        s.port = 7
        with mock.patch.dict("os.environ", {}, clear=False):
            os_env = s.build_env()
        self.assertEqual(os_env["ENABLE_TOOL_SEARCH"], "false")
        self.assertEqual(os_env["X"], "1")
        self.assertNotIn("VOICECLAUDE_PORT", os_env)  # the port is for our MCP servers (mcp-config.json), not the model's shell
        self.assertNotIn("VOICECLAUDE_LINE", os_env)
        with mock.patch.dict("os.environ", {"VOICECLAUDE_PORT": "1", "VOICECLAUDE_LINE": "x"}):
            self.assertFalse({"VOICECLAUDE_PORT", "VOICECLAUDE_LINE"} & set(s.build_env()))  # nor inherited ones
        with mock.patch.dict("os.environ", {"ENABLE_TOOL_SEARCH": "auto"}):
            self.assertEqual(s.build_env()["ENABLE_TOOL_SEARCH"], "auto")

    def test_env_carries_the_access_method(self):
        from voiceclaude import access
        s = ClaudeSession(config.Config(), "P", lambda k, d: None)
        with mock.patch.object(access, "detect", lambda cmd: access.AccessStatus(True, "api_key", "")), \
                mock.patch.object(access, "api_key", lambda: "sk-ant-test-key"):
            self.assertEqual(s.build_env()["ANTHROPIC_API_KEY"], "sk-ant-test-key")
        with mock.patch.object(access, "detect", lambda cmd: access.AccessStatus(True, "subscription", "")), \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "stray"}):
            self.assertNotIn("ANTHROPIC_API_KEY", s.build_env())  # a stray key must not hijack the subscription

    def test_a_host_claude_sessions_variables_never_reach_our_claude(self):
        host = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "abc", "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH": "1",
                "CLAUDE_CODE_MESSAGING_SOCKET": r"\\.\pipe\x", "CLAUDE_AGENT_SDK_VERSION": "0.2", "MCP_CONNECTION_NONBLOCKING": "1",
                "CLAUDE_CONFIG_DIR": r"C:\cfg", "CLAUDE_CODE_EFFORT_LEVEL": "max"}
        s = ClaudeSession(config.Config(), "P", lambda k, d: None, line_id="l1")
        with mock.patch.dict("os.environ", host):
            env = s.build_env()
        for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "CLAUDE_CODE_MESSAGING_SOCKET",
                  "CLAUDE_AGENT_SDK_VERSION", "MCP_CONNECTION_NONBLOCKING"):
            self.assertNotIn(k, env, k)
        self.assertEqual((env["CLAUDE_CONFIG_DIR"], env["CLAUDE_CODE_EFFORT_LEVEL"]), (r"C:\cfg", "max"))  # user settings stay

    def test_deepseek_provider_redirects_claude_code(self):
        from voiceclaude import access
        key = "sk-0123456789abcdef0123456789"
        with mock.patch.object(access, "provider_key", lambda pid, path=None: key if pid == "deepseek" else ""), \
                mock.patch.object(access, "find_claude_binary", lambda c="claude": "claude.exe"):
            s = ClaudeSession(config.Config(), "P", lambda k, d: None, line_id="l1", provider="deepseek")
            args = s.build_args()
            self.assertEqual(args[args.index("--model") + 1], "deepseek-flash")
            with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "stray"}):
                env = s.build_env()
            s.restart = lambda *a, **k: None  # not here: start() would spawn a process
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], key)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "deepseek-flash")
        self.assertEqual(ClaudeSession(config.Config(), "P", lambda k, d: None).provider, "claude")

    def test_desktop_disabled_drops_server(self):
        cfg = config.Config()
        cfg.desktop_enabled = False
        s = ClaudeSession(cfg, "P", lambda k, d: None)
        args = s.build_args()
        self.assertEqual(args[args.index("--allowedTools") + 1], "mcp__voiceclaude")


if __name__ == "__main__":
    unittest.main()
