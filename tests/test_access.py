import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voiceclaude import access


class KeyFileTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "auth.json"
        mock.patch.object(access, "_restrict_to_me", lambda p: None).start()

    def tearDown(self):
        mock.patch.stopall()

    def test_shape(self):
        self.assertTrue(access.valid_key_shape("sk-ant-api03-abcdefghijklmnop"))
        for bad in ("", "abc", "sk-ant-x y", "sk-live-xxxxxxxxxxxxxxxxxxxx"):
            self.assertFalse(access.valid_key_shape(bad), bad)

    def test_acl_after_every_write_and_off_thread(self):
        """Each save replaces the file with one that has the folder's inherited permissions: restrict it every time."""
        calls = []
        mock.patch.object(access, "_restrict_to_me", lambda p: calls.append(p)).start()
        access.save_auth({"a": 1}, self.path)
        access.save_auth({"a": 2}, self.path)
        import time
        t0 = time.time()
        while len(calls) < 2 and time.time() - t0 < 2:
            time.sleep(0.01)
        self.assertEqual(calls, [self.path, self.path])
        self.assertEqual(access.load_auth(self.path), {"a": 2})

    def test_save_and_clear(self):
        with mock.patch.object(access, "detect", lambda *a, **k: access.AccessStatus(True, "api_key", "ok")):
            st = access.set_api_key("  sk-ant-api03-abcdefghijklmnop  ", self.path)
            self.assertTrue(st.ok)
            self.assertEqual(json.loads(self.path.read_text("utf-8")), {"anthropic_api_key": "sk-ant-api03-abcdefghijklmnop"})
            self.assertFalse(access.set_api_key("nope", self.path).ok)
            access.clear_api_key(self.path)
            self.assertEqual(access.load_auth(self.path), {})
        self.assertEqual(access.mask("sk-ant-api03-abcdefghijklmnop"), "…mnop")


class DetectTests(unittest.TestCase):
    def test_api_key_wins(self):
        st = access.detect(find=lambda c: r"C:\claude.exe", status=lambda c: None, key="sk-ant-api03-abcdefghijklmnop")
        self.assertEqual((st.ok, st.method, st.claude), (True, "api_key", r"C:\claude.exe"))
        self.assertIn("…mnop", st.message)
        self.assertNotIn("abcdefghijklmnop", st.message)  # never spoken in full

    def test_api_key_without_claude(self):
        st = access.detect(find=lambda c: None, status=lambda c: None, key="sk-ant-api03-abcdefghijklmnop")
        self.assertFalse(st.ok)
        self.assertIn("install.bat", st.message)  # this environment's installer, never a bare `pip install`
        self.assertNotIn("pip install", st.message)

    def test_subscription(self):
        st = access.detect(find=lambda c: r"C:\claude.exe", key="",
                           status=lambda c: {"loggedIn": True, "email": "a@b.c", "subscriptionType": "max"})
        self.assertEqual((st.ok, st.method), (True, "subscription"))
        self.assertIn("a@b.c", st.account)

    def test_not_logged_in_and_nothing(self):
        st = access.detect(find=lambda c: r"C:\claude.exe", key="", status=lambda c: {"loggedIn": False})
        self.assertEqual((st.ok, st.method), (False, "none"))
        self.assertIn("AI 服务商", st.message)
        st = access.detect(find=lambda c: None, key="", status=lambda c: None)
        self.assertFalse(st.ok)
        self.assertIn("没装", st.message)
        self.assertIn("AI 服务商", st.message)  # the first-start message points at the page with its login button

    def test_env_overrides(self):
        with mock.patch.object(access, "api_key", lambda: "sk-ant-k"):
            env = access.env_overrides(access.AccessStatus(True, "api_key", ""))
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-k")
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"]), (None, None))  # nothing redirects Claude
        env = access.env_overrides(access.AccessStatus(True, "subscription", ""))
        self.assertTrue(all(v is None for v in env.values()))
        for slot in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            self.assertIn(slot, env)  # a Claude line inherits none of the user's own model slots

    def test_find_prefers_users_own_then_bundled(self):
        with mock.patch("shutil.which", lambda c: r"C:\mine\claude.exe"):
            self.assertEqual(access.find_claude_binary(), r"C:\mine\claude.exe")
        with mock.patch("shutil.which", lambda c: None), mock.patch.object(access, "bundled_claude", lambda: r"C:\sdk\claude.exe"), \
                mock.patch.object(Path, "exists", lambda self: False):
            self.assertEqual(access.find_claude_binary(), r"C:\sdk\claude.exe")


class ProviderTests(unittest.TestCase):
    """Who answers: Claude / DeepSeek / Kimi / GPT through the Claude Code engine (GPT's ChatGPT login: Codex)."""

    KEY = "sk-0123456789abcdef0123456789"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        mock.patch.object(access.paths, "AUTH_FILE", self.tmp / "auth.json").start()
        mock.patch.object(access, "_restrict_to_me", lambda p: None).start()

    def tearDown(self):
        mock.patch.stopall()

    def test_defaults_and_config_overrides(self):
        from voiceclaude import config
        self.assertEqual(tuple(access.PROVIDER_ORDER), ("claude", "deepseek", "kimi", "openai"))
        k = access.provider_for("kimi")
        self.assertEqual((k.base_url, k.model, k.small_model, k.host), ("https://api.moonshot.cn/anthropic", "kimi-k3[1m]", "kimi-k2.7-code", "api.moonshot.cn"))
        self.assertEqual(k.models["k3"], "kimi-k3[1m]")
        self.assertTrue(k.url.startswith("https://platform.kimi.com"))
        from voiceclaude import config as _cfg
        cfg = _cfg.Config()
        cfg.providers = {"kimi": {"base_url": "https://api.moonshot.ai/anthropic"}}
        self.assertEqual(access.provider_for("kimi", cfg).host, "api.moonshot.ai")  # host follows an overseas base_url
        p = access.provider_for("deepseek")
        self.assertEqual((p.model, p.deep_model, p.base_url), ("deepseek-flash", "deepseek-v4-pro", "https://api.deepseek.com/anthropic"))
        self.assertEqual(p.models, {"flash": "deepseek-flash", "pro": "deepseek-v4-pro"})
        self.assertTrue(p.thinking and p.installed)
        cfg = config.Config()
        cfg.providers = {"deepseek": {"model": "deepseek-v4-pro", "thinking": False, "models": {"x": "y"}}}
        p = access.provider_for("deepseek", cfg)
        self.assertEqual((p.model, p.thinking, p.models), ("deepseek-v4-pro", False, {"x": "y"}))
        with mock.patch.object(access, "gateway_problem", lambda: "还没装"):
            p = access.provider_for("openai")
            self.assertFalse(p.installed)
            self.assertIn("pip install", p.install_hint)
        with mock.patch.object(access, "gateway_problem", lambda: ""):
            self.assertTrue(access.provider_for("openai").installed)
        d = access.provider_for("deepseek").as_dict()
        self.assertEqual((d["id"], d["models"], d["fields"][0]["key"], d["fields"][0]["secret"]), ("deepseek", ["flash", "pro"], "api_key", True))
        self.assertEqual((d["key_prefix"], d["subscription"]), ("sk-", False))
        self.assertTrue(d["url"].startswith("https://platform.deepseek.com"))
        self.assertTrue(access.provider_for("claude").as_dict()["subscription"])

    def test_keys_come_from_auth_json_only(self):
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-from-the-users-environment-000"}):
            self.assertEqual(access.provider_key("deepseek"), "")  # the user's own variables are never used
            ok, msg = access.set_provider_key("deepseek", f"  {self.KEY} ")
            self.assertTrue(ok, msg)
            self.assertIn("…6789", msg)
            self.assertNotIn(self.KEY, msg)
            self.assertEqual(access.provider_key("deepseek"), self.KEY)
        self.assertEqual(json.loads((self.tmp / "auth.json").read_text("utf-8")), {"deepseek_api_key": self.KEY})
        ok, msg = access.set_provider_key("deepseek", "abc")
        self.assertFalse(ok)
        self.assertIn("样子不对", msg)
        self.assertFalse(access.set_provider_key("gemini", self.KEY)[0])
        self.assertTrue(access.clear_provider_key("deepseek")[0])
        self.assertEqual(access.provider_key("deepseek"), "")
        self.assertEqual(access.provider_key("claude"), "")
        access.set_provider_key("claude", "sk-ant-api03-abcdefghijklmnop")
        self.assertEqual(access.api_key(), "sk-ant-api03-abcdefghijklmnop")  # the older field name is kept

    def test_detect_provider(self):
        st = access.detect_provider("deepseek", find=lambda c: r"C:\claude.exe")
        self.assertEqual((st.ok, st.provider, st.model), (False, "deepseek", "deepseek-flash"))
        self.assertIn("还没填", st.message)
        st = access.detect_provider("deepseek", find=lambda c: None, key=self.KEY)
        self.assertFalse(st.ok)
        self.assertIn("Claude Code", st.message)
        st = access.detect_provider("deepseek", find=lambda c: r"C:\claude.exe", key="bad")
        self.assertIn("样子不对", st.message)
        st = access.detect_provider("deepseek", find=lambda c: r"C:\claude.exe", key=self.KEY)
        self.assertEqual((st.ok, st.method, st.claude, st.account), (True, "deepseek_key", r"C:\claude.exe", "…6789"))
        self.assertIn("deepseek-flash", st.message)
        self.assertNotIn(self.KEY, st.message)
        self.assertFalse(access.detect_provider("gemini", find=lambda c: r"C:\claude.exe").ok)
        st = access.detect_provider("claude", find=lambda c: r"C:\claude.exe", status=lambda c: None, key="sk-ant-api03-abcdefghijklmnop")
        self.assertEqual((st.ok, st.provider, st.provider_name), (True, "claude", "Claude（Anthropic）"))

    def test_env_for_deepseek_redirects_claude_code(self):
        from voiceclaude import config
        access.set_provider_key("deepseek", self.KEY)
        st = access.detect_provider("deepseek", find=lambda c: r"C:\claude.exe")
        env = access.env_overrides(st)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], self.KEY)
        self.assertIsNone(env["ANTHROPIC_API_KEY"])  # both set = Claude Code's conflict prompt
        self.assertEqual((env["ANTHROPIC_MODEL"], env["ANTHROPIC_DEFAULT_OPUS_MODEL"], env["ANTHROPIC_DEFAULT_HAIKU_MODEL"]),
                         ("deepseek-flash", "deepseek-v4-pro", "deepseek-flash"))
        self.assertEqual(env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
        self.assertEqual(env["ANTHROPIC_DEFAULT_FABLE_MODEL"], "deepseek-v4-pro")
        self.assertNotIn("MAX_THINKING_TOKENS", env)
        access.set_provider_key("kimi", self.KEY)
        kenv = access.env_overrides(access.detect_provider("kimi", find=lambda c: r"C:\claude.exe"))
        self.assertEqual((kenv["ANTHROPIC_BASE_URL"], kenv["ANTHROPIC_MODEL"], kenv["ANTHROPIC_DEFAULT_HAIKU_MODEL"]),
                         ("https://api.moonshot.cn/anthropic", "kimi-k3[1m]", "kimi-k2.7-code"))
        cfg = config.Config()
        cfg.providers = {"deepseek": {"thinking": False}}
        self.assertEqual(access.env_overrides(st, cfg)["MAX_THINKING_TOKENS"], "0")

    def test_openai_prefers_the_users_codex_login(self):
        with mock.patch.object(access, "codex_login_state", lambda: ("logged_in", r"C:\codex\codex.exe", "Logged in using ChatGPT")):
            st = access.detect_provider("openai", find=lambda c: r"C:\claude.exe")
            self.assertEqual((st.ok, st.method, st.claude, st.provider), (True, "codex_login", r"C:\codex\codex.exe", "openai"))
            self.assertIn("ChatGPT", st.account)
            with self.assertRaises(RuntimeError):
                access.env_overrides(st)  # a codex_login line is never a Claude Code process
        from voiceclaude import engine_codex
        with mock.patch.object(access, "codex_login_state", lambda: ("not_installed", "", "")), \
                mock.patch.object(engine_codex, "codex_shim", lambda: ""):
            st = access.detect_provider("openai", find=lambda c: r"C:\claude.exe")
            self.assertFalse(st.ok)
            self.assertIn("装 OpenAI 的官方 Windows 版 Codex（codex.exe）", st.message)
        with mock.patch.object(access, "codex_login_state", lambda: ("not_installed", "", "")), \
                mock.patch.object(engine_codex, "codex_shim", lambda: r"C:\Users\you\AppData\Roaming\npm\codex.CMD"):
            st = access.detect_provider("openai", find=lambda c: r"C:\claude.exe")
            self.assertIn("找到的 codex.CMD 是 npm 装的脚本版，用不了", st.message)  # the npm install is named, not run
        with mock.patch.object(access, "codex_login_state", lambda: ("not_logged_in", r"C:\codex.exe", "")):
            st = access.detect_provider("openai", find=lambda c: r"C:\claude.exe")
            self.assertIn("登录", st.message)
        d = access.provider_for("openai").as_dict()
        self.assertEqual((d["methods"], d["login_command"], d["subscription"]), (["login", "key"], "codex login", True))
        self.assertEqual(access.provider_for("deepseek").as_dict()["methods"], ["key"])
        self.assertEqual(access.provider_for("claude").as_dict()["login_command"], "claude auth login")
        self.assertFalse(access.start_claude_login()["ok"])  # never under the test runner

    def test_no_key_means_no_line(self):
        st = access.detect_provider("deepseek", find=lambda c: r"C:\claude.exe", key=self.KEY)  # checked with a key...
        with self.assertRaises(RuntimeError):
            access.env_overrides(st)  # ...but none stored: never start Claude Code pointed at the vendor with no token

    def test_probe_strips_a_host_sessions_variables_but_keeps_ours(self):
        calls = []

        class R:
            returncode, stdout, stderr = 0, "好\n", ""

        def run(argv, **kw):
            calls.append(kw["env"])
            return R()
        access.set_provider_key("deepseek", self.KEY)
        with mock.patch.dict("os.environ", {"CLAUDECODE": "1", "MCP_CONNECTION_NONBLOCKING": "1", "CLAUDE_CODE_SESSION_ID": "s"}), \
                mock.patch.object(access, "reachable", lambda host, port=443, timeout=3.0: (True, "")):
            self.assertTrue(access.test_provider("deepseek", run=run, find=lambda c: r"C:\claude.exe")["ok"])
        env = calls[0]
        for k in ("CLAUDECODE", "MCP_CONNECTION_NONBLOCKING", "CLAUDE_CODE_SESSION_ID"):
            self.assertNotIn(k, env)
        self.assertEqual(env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")  # what we set ourselves survives
        self.assertEqual(access.without_host_session({"claudecode": "1", "PATH": "p"}), {"PATH": "p"})
        # the user's own Windows shell settings for Claude Code stay
        shell = {"CLAUDE_CODE_GIT_BASH_PATH": r"D:\Git\bin\bash.exe", "CLAUDE_CODE_USE_POWERSHELL_TOOL": "1"}
        self.assertEqual(access.without_host_session({**shell, "CLAUDE_CODE_SESSION_ID": "x"}), shell)

    def test_test_provider_uses_the_providers_model_and_key(self):
        calls = []

        class R:
            returncode, stdout, stderr = 0, "好\n", ""

        def run(argv, **kw):
            calls.append((argv, kw["env"]))
            return R()
        access.set_provider_key("deepseek", self.KEY)
        with mock.patch.object(access, "reachable", lambda host, port=443, timeout=3.0: (True, "")):  # no network in tests
            r = access.test_provider("deepseek", run=run, find=lambda c: r"C:\claude.exe")
        self.assertTrue(r["ok"], r)
        self.assertIn("DeepSeek", r["message"])
        argv, env = calls[0]
        self.assertEqual(argv[argv.index("--model") + 1], "deepseek-flash")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], self.KEY)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertFalse(access.test_provider("openai", run=run, find=lambda c: r"C:\claude.exe")["ok"])
        self.assertEqual(len(calls), 1)  # nothing is started for a provider that cannot answer


class ReachTests(unittest.TestCase):
    def test_reachable(self):
        import socket
        self.assertEqual(access.reachable(""), (True, ""))
        with mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://127.0.0.1:7890"}):
            self.assertEqual(access.reachable("api.deepseek.com"), (True, ""))  # a proxy would carry it: not probed
        with mock.patch.dict("os.environ", {k: "" for k in access.PROXY_VARS}):
            with mock.patch("socket.create_connection", side_effect=ConnectionResetError(10054, "reset")):
                ok, why = access.reachable("api.deepseek.com")
            self.assertFalse(ok)
            self.assertIn("api.deepseek.com", why)
            self.assertIn("屏蔽", why)
            with mock.patch("socket.create_connection", side_effect=socket.gaierror("no such host")):
                self.assertIn("解析不了", access.reachable("nope.example")[1])

            class FakeTLS:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def getpeercert(self):
                    return {}
            with mock.patch("socket.create_connection", return_value=FakeTLS()), \
                    mock.patch("ssl.create_default_context", return_value=mock.Mock(wrap_socket=lambda s, server_hostname: FakeTLS())):
                self.assertEqual(access.reachable("api.deepseek.com"), (True, ""))
        self.assertEqual(access.check_reach("claude"), (True, ""))  # Claude's own route is not probed
        with mock.patch.object(access, "reachable", lambda host, port=443, timeout=3.0: (False, host)):
            self.assertEqual(access.check_reach("deepseek"), (False, "api.deepseek.com"))
            from voiceclaude import config
            cfg = config.Config()
            cfg.providers = {"deepseek": {"base_url": "https://mirror.example.cn/anthropic"}}
            self.assertEqual(access.check_reach("deepseek", cfg), (False, "mirror.example.cn"))

    def test_test_provider_does_not_start_claude_when_the_vendor_is_unreachable(self):
        calls = []
        with mock.patch.object(access, "provider_key", lambda pid, path=None: "sk-0123456789abcdef0123456789"), \
                mock.patch.object(access, "reachable", lambda host, port=443, timeout=3.0: (False, f"连不上 {host}（屏蔽）")):
            r = access.test_provider("deepseek", run=lambda *a, **k: calls.append(a), find=lambda c: r"C:\claude.exe")
        self.assertFalse(r["ok"])
        self.assertIn("没有起 Claude Code", r["message"])
        self.assertEqual(calls, [])


class TestAccessTests(unittest.TestCase):
    def test_runs_claude_once(self):
        calls = []

        class R:
            returncode, stdout, stderr = 0, "好\n", ""

        def run(argv, **kw):
            calls.append((argv, kw["env"].get("ANTHROPIC_API_KEY")))
            return R()
        with mock.patch.object(access, "api_key", lambda: "sk-ant-k"):
            r = access.test_access(access.AccessStatus(True, "api_key", "", claude=r"C:\claude.exe"), run=run)
        self.assertTrue(r["ok"])
        self.assertIn("接通了", r["message"])
        self.assertEqual(calls[0][0][:2], [r"C:\claude.exe", "-p"])
        self.assertEqual(calls[0][1], "sk-ant-k")

    def test_failure_is_explained(self):
        class R:
            returncode, stdout, stderr = 1, "", "Invalid API key"
        r = access.test_access(access.AccessStatus(True, "api_key", "", claude="c"), run=lambda *a, **k: R())
        self.assertFalse(r["ok"])
        self.assertIn("key 无效", r["message"])
        r = access.test_access(access.AccessStatus(False, "none", "还没接入"))
        self.assertEqual((r["ok"], r["message"]), (False, "还没接入"))


if __name__ == "__main__":
    unittest.main()
