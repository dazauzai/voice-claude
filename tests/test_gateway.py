import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voiceclaude import access, config, gateway, paths


class FakeProc:
    def __init__(self):
        self.alive = True
        self.terminated = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


class GatewayTests(unittest.TestCase):
    """The LiteLLM translation gateway for OpenAI-format providers: loopback, scrubbed environment, one key."""

    KEY = "sk-openai-0123456789abcdef0123"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        mock.patch.object(paths, "GATEWAY_CONFIG", self.tmp / "gateway.yaml").start()
        mock.patch.object(paths, "GATEWAY_LOG", self.tmp / "gateway.log").start()
        mock.patch.object(paths, "AUTH_FILE", self.tmp / "auth.json").start()
        mock.patch.object(access, "_restrict_to_me", lambda p: None).start()
        mock.patch.object(access, "gateway_problem", lambda: "").start()
        self.spawned = []

        def spawn(args, **kw):
            self.spawned.append((args, kw))
            return FakeProc()
        self.gw = gateway.Gateway(config.Config(), spawn=spawn, probe=lambda url: True, sleep=lambda s: None)

    def tearDown(self):
        self.gw.stop()
        access.GATEWAY = None
        mock.patch.stopall()

    def test_starts_scrubbed_on_loopback_with_only_that_key(self):
        access.set_provider_key("openai", self.KEY)
        stray = {"DEEPSEEK_API_KEY": "sk-users-own-deepseek-key-00000", "ANTHROPIC_API_KEY": "sk-ant-stray-key-000000000",
                 "VOICECLAUDE_HOME": str(self.tmp), "PATH": os.environ.get("PATH", "")}
        with mock.patch.dict("os.environ", stray):
            ok, msg = self.gw.ensure("openai")
        self.assertTrue(ok, msg)
        args, kw = self.spawned[0]
        self.assertEqual(args[args.index("--host") + 1], "127.0.0.1")
        self.assertEqual(args[args.index("--port") + 1], str(self.gw.port))
        self.assertEqual(args[args.index("--config") + 1], str(self.tmp / "gateway.yaml"))
        env = kw["env"]
        self.assertEqual(env["OPENAI_API_KEY"], self.KEY)
        self.assertEqual(env["LITELLM_MASTER_KEY"], self.gw.token)
        for banned in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "VOICECLAUDE_HOME"):
            self.assertNotIn(banned, env)  # the user's own keys never reach the proxy
        self.assertEqual(env["LITELLM_LOCAL_MODEL_COST_MAP"], "True")
        yaml = (self.tmp / "gateway.yaml").read_text("utf-8")
        self.assertIn("model_name: gpt-6.1-sol", yaml)
        self.assertIn("model: openai/gpt-6.1-sol", yaml)
        self.assertIn("gpt-6-luna", yaml)
        self.assertIn("api_key: os.environ/OPENAI_API_KEY", yaml)
        self.assertNotIn(self.KEY, yaml)
        self.assertIn("master_key: os.environ/LITELLM_MASTER_KEY", yaml)
        self.assertEqual(self.gw.endpoint(), (f"http://127.0.0.1:{self.gw.port}", self.gw.token))
        self.assertEqual(self.gw.ensure("openai"), (True, ""))  # already serving: nothing restarted
        self.assertEqual(len(self.spawned), 1)
        # what Claude Code gets for an openai line
        st = access.detect_provider("openai", find=lambda c: "claude.exe")
        self.assertTrue(st.ok, st.message)
        env = access.env_overrides(st)
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"]), (f"http://127.0.0.1:{self.gw.port}", self.gw.token))
        self.assertEqual((env["ANTHROPIC_MODEL"], env["ANTHROPIC_DEFAULT_HAIKU_MODEL"]), ("gpt-6.1-sol", "gpt-6-luna"))
        self.assertIsNone(env["ANTHROPIC_API_KEY"])
        port, token = self.gw.port, self.gw.token
        self.gw.stop()
        self.assertIsNone(self.gw.endpoint())
        # a line restarting while the proxy is down brings it back, on the same port and token (lines keep working)
        env = access.env_overrides(st)
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"]), (f"http://127.0.0.1:{port}", token))
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual((self.gw.port, self.gw.token), (port, token))

    def test_deepseek_can_go_through_the_gateway_too(self):
        access.set_provider_key("deepseek", "sk-0123456789abcdef0123456789")
        access.set_provider_key("openai", self.KEY)
        cfg = config.Config()
        cfg.providers = {"deepseek": {"via": "litellm"}}
        self.gw.cfg = cfg
        ok, msg = self.gw.ensure("deepseek")
        self.assertTrue(ok, msg)
        env = self.spawned[0][1]["env"]
        self.assertEqual((env["DEEPSEEK_API_KEY"], env["OPENAI_API_KEY"]), ("sk-0123456789abcdef0123456789", self.KEY))
        yaml = (self.tmp / "gateway.yaml").read_text("utf-8")
        self.assertIn("model: deepseek/deepseek-flash", yaml)
        self.assertIn("api_key: os.environ/DEEPSEEK_API_KEY", yaml)
        self.assertEqual(self.gw.serving, {"openai", "deepseek"})
        self.assertEqual(self.gw.ensure("openai"), (True, ""))
        # the port and master key never change for the daemon's lifetime, so a restart is invisible to open lines
        port, token = self.gw.port, self.gw.token
        self.gw.stop()
        self.assertTrue(self.gw.ensure("openai")[0])
        self.assertEqual((self.gw.port, self.gw.token), (port, token))
        self.assertEqual(len(self.spawned), 2)

    def test_without_key_or_litellm(self):
        self.assertIn("还没填", self.gw.ensure("openai")[1])
        self.assertIn("不走翻译层", self.gw.ensure("deepseek")[1])
        with mock.patch.object(access, "gateway_problem", lambda: "还没装"):
            self.assertIn("pip install", self.gw.ensure("openai")[1])
            self.assertFalse(access.provider_for("openai").installed)
            self.assertIn("pip install", access.detect_provider("openai", find=lambda c: "claude.exe", key=self.KEY).message)
        self.assertEqual(self.spawned, [])
        with mock.patch.object(access, "GATEWAY", None):
            st = access.AccessStatus(True, "openai_key", "", claude="c", provider="openai", model="gpt-6.1-sol")
            with mock.patch.object(access, "provider_key", lambda pid, path=None: self.KEY):
                with self.assertRaises(RuntimeError):
                    access.env_overrides(st)  # no gateway at all: the line must not start at Anthropic by accident

    def test_gateway_problem_checks_version_and_proxy_extras(self):
        import importlib.metadata
        import importlib.util
        with mock.patch.object(access, "gateway_problem", wraps=None):
            pass
        real = access.gateway_problem.__wrapped__ if hasattr(access.gateway_problem, "__wrapped__") else None
        mock.patch.stopall()  # use the real function for this test
        with mock.patch.object(importlib.util, "find_spec", lambda name: None):
            self.assertIn("还没装", access.gateway_problem())
        with mock.patch.object(importlib.util, "find_spec", lambda name: object()), \
                mock.patch.object(importlib.metadata, "version", lambda name: "1.82.6"):
            self.assertIn("太旧", access.gateway_problem())
        with mock.patch.object(importlib.util, "find_spec", lambda name: object() if name == "litellm" else None), \
                mock.patch.object(importlib.metadata, "version", lambda name: "1.106.2"):
            self.assertIn("proxy", access.gateway_problem())
        with mock.patch.object(importlib.util, "find_spec", lambda name: object()), \
                mock.patch.object(importlib.metadata, "version", lambda name: "1.110.0"):
            self.assertEqual(access.gateway_problem(), "")
        self.assertIn("-m pip install", access.pip_install_hint())
        self.assertIn(access.GATEWAY_MIN_VERSION, access.pip_install_hint())
        # a full sentence, with the console python.exe next to the service's pythonw.exe
        hint = access.gateway_install_hint("还没装")
        self.assertTrue(hint.startswith("翻译层还没装：再运行一次 install.bat --with-gpt"), hint)
        hint = access.gateway_install_hint("装的版本太旧（1.0，要 1.106.2 以上）")
        self.assertNotIn("install.bat", hint)  # the installer skips a LiteLLM that is already there
        self.assertIn("-m pip install", hint)
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        (d / "python.exe").write_text("", "utf-8")
        with mock.patch.object(access.sys, "executable", str(d / "pythonw.exe")):
            self.assertEqual(access.venv_python(), str(d / "python.exe"))
            self.assertIn(str(d / "python.exe"), access.pip_install_hint())

    def test_instant_exit_is_reported_with_the_log_line(self):
        access.set_provider_key("openai", self.KEY)
        (self.tmp / "gateway.log").write_text("ImportError: litellm[proxy] extras are missing\n", "utf-8")

        class Dead(FakeProc):
            def poll(self):
                return 3
        gw = gateway.Gateway(config.Config(), spawn=lambda a, **k: Dead(), probe=lambda url: False, sleep=lambda s: None)
        ok, msg = gw.ensure("openai")
        self.assertFalse(ok)
        self.assertIn("刚启动就退出了", msg)
        self.assertIn("退出码 3", msg)
        self.assertIn("ImportError", msg)

    def test_not_ready_in_time_is_reported_and_stopped(self):
        access.set_provider_key("openai", self.KEY)
        clock = [0.0]

        def now():
            clock[0] += 0.5
            return clock[0]
        procs = []

        def spawn(args, **kw):
            procs.append(FakeProc())
            return procs[-1]
        gw = gateway.Gateway(config.Config(), spawn=spawn, probe=lambda url: False, sleep=lambda s: None, now=now,
                             ready_timeout=2.0)
        ok, msg = gw.ensure("openai")
        self.assertFalse(ok)
        self.assertIn("没起来", msg)
        self.assertIsNone(gw.endpoint())
        self.assertTrue(procs[0].terminated)

    def test_yaml_and_env_helpers(self):
        y = gateway.config_yaml([("a", "openai/a", "OPENAI_API_KEY")])
        self.assertTrue(y.startswith("model_list:\n  - model_name: a\n"))
        with mock.patch.dict("os.environ", {"SECRET_THING": "x", "SYSTEMROOT": r"C:\Windows"}):
            env = gateway.scrubbed_env({"K": "v"})
        self.assertNotIn("SECRET_THING", env)
        self.assertEqual((env.get("SYSTEMROOT"), env["K"], env["NO_DOCS"]), (r"C:\Windows", "v", "True"))
        self.assertTrue(gateway.free_port() > 0)
