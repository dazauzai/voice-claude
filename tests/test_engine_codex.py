import json
import queue
import threading
import time
import unittest
from unittest import mock

from voiceclaude import config, engine_codex
from voiceclaude.engine_codex import CodexSession


def wait_for(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


class FakeAppServer:
    """A scripted codex app-server over the engine's transport interface: answers requests, emits notifications,
    and asks for approvals / elicitations the way the real one did in scripts/probe_codex.py."""

    def __init__(self, args, cwd, env, script=None):
        self.args, self.cwd, self.env = args, cwd, env
        self.out: queue.Queue = queue.Queue()
        self.received: list[dict] = []
        self.closed = False
        self.killed = False
        self.script = script if script is not None else {}  # shared with the test (an empty dict is still the same dict)
        self.turns = 0
        self.resumed = None

    # --- transport interface ---
    def send(self, line):
        msg = json.loads(line)
        self.received.append(msg)
        m = msg.get("method")
        rid = msg.get("id")
        if m == "initialize":
            self._reply(rid, {"userAgent": "fake", "codexHome": "x"})
        elif m == "thread/start":
            self._reply(rid, {"thread": {"id": "thread-new"}, "model": msg["params"].get("model")})
            self._emit("thread/started", {"thread": {"id": "thread-new"}})
        elif m == "thread/resume":
            self.resumed = msg["params"]["threadId"]
            if self.script.get("resume_fails"):
                self.out.put(json.dumps({"id": rid, "error": {"code": 1, "message": "no such thread"}}))
            else:
                self._reply(rid, {"thread": {"id": msg["params"]["threadId"]}})
        elif m == "turn/start":
            self.turns += 1
            tid = f"turn-{self.turns}"
            self._reply(rid, {"turn": {"id": tid, "status": "inProgress"}})
            threading.Thread(target=self._play_turn, args=(tid, msg["params"]), daemon=True).start()
        elif m == "turn/interrupt":
            self._reply(rid, {})
            self._emit("turn/completed", {"threadId": "t", "turn": {"id": msg["params"]["turnId"], "status": "interrupted"}})
        elif rid is not None and m is None:  # our answer to a server request
            self.script.setdefault("answers", []).append(msg)
            ev = self.script.get("waiting")
            if ev:
                ev.set()

    def readline(self):
        line = self.out.get()
        return line

    def poll(self):
        return 0 if (self.closed or self.killed) else None

    def close(self):
        self.closed = True
        self.out.put(None)

    def kill(self):
        self.killed = True
        self.out.put(None)

    # --- scripting ---
    def _reply(self, rid, result):
        self.out.put(json.dumps({"id": rid, "result": result}))

    def _emit(self, method, params):
        self.out.put(json.dumps({"method": method, "params": params}))

    def _ask(self, method, params):
        """A server request; blocks until the engine answers."""
        ev = threading.Event()
        self.script["waiting"] = ev
        self.out.put(json.dumps({"id": 1000 + self.turns, "method": method, "params": params}))
        ev.wait(3)
        self.script["waiting"] = None
        return self.script.get("answers", [])[-1] if self.script.get("answers") else None

    def _play_turn(self, tid, params):
        text = params["input"][0]["text"]
        base = {"threadId": "t", "turnId": tid}
        if text == "slow":
            time.sleep(0.3)
            if self.script.get("interrupted"):
                return
        if text == "dir":
            self._emit("item/started", {**base, "item": {"type": "commandExecution", "id": "c1", "command": "dir", "status": "inProgress"}})
            ans = self._ask("item/commandExecution/requestApproval", {**base, "itemId": "c1", "command": "rmdir x", "reason": "cleanup"})
            self.script["decision"] = (ans or {}).get("result", {}).get("decision")
            self._emit("item/completed", {**base, "item": {"type": "commandExecution", "id": "c1", "command": "dir", "status": "completed", "exitCode": 0}})
        if text == "mcp":
            ans = self._ask("mcpServer/elicitation/request", {**base, "serverName": "voiceclaude", "mode": "form", "message": "Allow?",
                                                               "_meta": {"codex_approval_kind": "mcp_tool_call"}})
            self.script["elicitation"] = (ans or {}).get("result")
            ans2 = self._ask("mcpServer/elicitation/request", {**base, "serverName": "cua_repl", "mode": "form", "message": "Allow?"})
            self.script["elicitation_other"] = (ans2 or {}).get("result")
            self._emit("item/started", {**base, "item": {"type": "mcpToolCall", "id": "m1", "server": "voiceclaude", "tool": "get_permissions", "arguments": {}}})
        if text == "fail":
            self._emit("error", {**base, "error": {"message": "quota exhausted"}, "willRetry": False})
            self._emit("turn/completed", {"threadId": "t", "turn": {"id": tid, "status": "failed", "error": {"message": "quota exhausted"}}})
            return
        for piece in ("好", "的"):
            self._emit("item/agentMessage/delta", {**base, "itemId": "a1", "delta": piece})
        self._emit("item/completed", {**base, "item": {"type": "agentMessage", "id": "a1", "text": "好的"}})
        self._emit("turn/completed", {"threadId": "t", "turn": {"id": tid, "status": "completed"}})


class CodexSessionTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.asks = []
        self.fake = None
        self.script = {}
        self.answer = True

        def factory(args, cwd, env):
            self.fake = FakeAppServer(args, cwd, env, self.script)
            return self.fake

        def ask(action, question, detail, facts=None):
            self.asks.append((action, question, detail))
            self.facts.append(facts)
            return (self.answer, "" if self.answer else "用户没有同意")
        self.facts = []
        cfg = config.Config()
        cfg.working_directory = "."
        self.cfg = cfg
        self.s = CodexSession(cfg, "语音规则", lambda k, d: self.events.append((k, d)), line_id="l1",
                              servers={"voiceclaude": {"command": r"C:\py\python.exe", "args": ["-X", "utf8", r"C:\x\mcp_server.py"], "env": {}}},
                              ask=ask, exe=r"C:\codex\codex.exe", transport_factory=factory)
        self.s.port = 4242

    def tearDown(self):
        self.s.close()

    def kinds(self):
        return [k for k, d in self.events]

    def dones(self):
        return [d for k, d in self.events if k == "done"]

    def test_args_isolate_our_servers_and_keep_the_users_login(self):
        args = self.s.build_args()
        self.assertEqual(args[:2], [r"C:\codex\codex.exe", "app-server"])
        self.assertIn("mcp_servers={}", args)  # the user's own MCP servers (computer use among them) stay out
        for f in engine_codex.DISABLED_FEATURES:
            self.assertIn(f, args)
        joined = " ".join(args)
        self.assertIn('mcp_servers.voiceclaude.command="C:/py/python.exe"', joined)
        self.assertIn('mcp_servers.voiceclaude.args=["-X", "utf8", "C:/x/mcp_server.py"]', joined)
        self.assertIn('VOICECLAUDE_PORT = "4242"', joined)
        self.assertIn('VOICECLAUDE_LINE = "l1"', joined)
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-stray", "CODEX_API_KEY": "x"}):
            env = self.s.build_env()
        self.assertNotIn("OPENAI_API_KEY", env)  # the user's ChatGPT login, never a stray key
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertEqual(self.s.model, "gpt-6.1-sol")

    def test_handshake_turn_and_done(self):
        self.s.start()
        self.assertTrue(wait_for(lambda: "ready" in self.kinds()))
        self.assertEqual(self.s.session_id, "thread-new")
        methods = [m.get("method") for m in self.fake.received]
        self.assertEqual(methods[:3], ["initialize", "initialized", "thread/start"])
        start = next(m for m in self.fake.received if m.get("method") == "thread/start")["params"]
        self.assertEqual((start["approvalPolicy"], start["sandbox"], start["model"]), ("on-request", "workspace-write", "gpt-6.1-sol"))
        self.assertEqual(start["developerInstructions"], "语音规则")
        self.s.send("你好")
        self.assertTrue(wait_for(lambda: self.dones()))
        self.assertEqual("".join(d for k, d in self.events if k == "text"), "好的")
        self.assertEqual(self.dones()[0]["status"], "ok")
        self.assertEqual(self.dones()[0]["message"], "好的")
        self.assertFalse(self.s.turn_active)
        turn = next(m for m in self.fake.received if m.get("method") == "turn/start")["params"]
        self.assertEqual(turn["sandboxPolicy"]["type"], "workspaceWrite")
        self.assertEqual(turn["effort"], "low")

    def test_command_approval_goes_through_the_gate(self):
        self.s.start()
        self.assertTrue(wait_for(lambda: self.s.ready.is_set()))
        self.s.send("dir")
        self.assertTrue(wait_for(lambda: self.dones()))
        self.assertEqual(len(self.asks), 1)
        action, question, detail = self.asks[0]
        self.assertEqual(action, "codex_shell")
        self.assertIn("rmdir x", question)
        self.assertIn("cleanup", detail)
        self.assertEqual(self.facts[0], {"kind": "shell", "command": "rmdir x"})  # the risk check gets the command itself
        self.assertEqual(self.script["decision"], "accept")
        tools = [d for k, d in self.events if k == "tool"]
        self.assertEqual(tools[0], {"name": "Bash", "input": {"command": "dir"}})
        # and declined when the user says no
        self.events.clear()
        self.answer = False
        self.s.send("dir")
        self.assertTrue(wait_for(lambda: self.dones()))
        self.assertEqual(self.script["decision"], "decline")

    def test_mcp_elicitation_accepts_ours_and_declines_others(self):
        self.s.start()
        self.assertTrue(wait_for(lambda: self.s.ready.is_set()))
        self.s.send("mcp")
        self.assertTrue(wait_for(lambda: self.dones()))
        self.assertEqual(self.script["elicitation"]["action"], "accept")
        self.assertEqual(self.script["elicitation_other"]["action"], "decline")
        self.assertEqual(self.asks, [])  # our servers ask the user themselves; no extra question here
        self.assertIn({"name": "mcp__voiceclaude__get_permissions", "input": {}}, [d for k, d in self.events if k == "tool"])

    def test_failed_turn_and_interrupt(self):
        self.s.start()
        self.assertTrue(wait_for(lambda: self.s.ready.is_set()))
        self.s.send("fail")
        self.assertTrue(wait_for(lambda: self.dones()))
        self.assertEqual((self.dones()[0]["status"], self.dones()[0]["message"]), ("error", "quota exhausted"))
        self.events.clear()
        self.script["interrupted"] = True
        self.s.send("slow")
        self.assertTrue(wait_for(lambda: self.s.turn_id is not None))
        self.s.interrupt()
        self.assertTrue(wait_for(lambda: self.dones()))
        self.assertEqual(self.dones()[0]["status"], "interrupted")
        self.assertIn("turn/interrupt", [m.get("method") for m in self.fake.received])

    def test_restart_resumes_the_thread_and_fresh_starts_a_new_one(self):
        self.s.start()
        self.assertTrue(wait_for(lambda: self.s.ready.is_set()))
        first = self.fake
        self.s.restart()
        self.assertTrue(wait_for(lambda: self.fake is not first and self.fake.resumed == "thread-new"))
        self.assertTrue(first.closed)
        self.assertTrue(wait_for(lambda: self.s.ready.is_set()))
        self.s.restart(fresh=True)
        self.assertTrue(wait_for(lambda: self.s.ready.is_set() and self.fake.resumed is None))
        self.assertEqual(self.s.session_id, "thread-new")
        # a thread that cannot be resumed falls back to a new one
        self.script["resume_fails"] = True
        self.s.restart()
        self.assertTrue(wait_for(lambda: self.s.ready.is_set() and any(m.get("method") == "thread/start" for m in self.fake.received)))

    def test_exit_of_the_server_is_reported(self):
        self.s.start()
        self.assertTrue(wait_for(lambda: self.s.ready.is_set()))
        self.fake.closed = True
        self.fake.out.put(None)
        self.assertTrue(wait_for(lambda: "exit" in self.kinds()))
        self.assertFalse(self.s.alive)

    def test_login_stops_the_previous_one_and_keeps_its_output(self):
        """A second click on 登录 used to fail: the first `codex login` still held port 1455."""
        import subprocess
        import tempfile
        from pathlib import Path
        made = []

        class FakeLogin:
            def __init__(self, args, **kw):
                self.args, self.returncode, self.pid = args, None, 100 + len(made)
                self.done = threading.Event()
                self.stdout = iter(["Starting local login server on http://localhost:1455.\n",
                                    f"https://auth.openai.com/oauth/authorize?state=s{len(made)}\n"])
                made.append(self)

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 1
                self.done.set()
            kill = terminate

            def wait(self, timeout=None):
                if not self.done.wait(timeout):
                    raise subprocess.TimeoutExpired("codex", timeout)
                return self.returncode
        from voiceclaude import paths
        self.assertEqual(engine_codex.login_log_path(), paths.HOME_DIR / "codex-login.log")  # never inside ~/.codex
        log_path = Path(tempfile.mkdtemp()) / "codex-login.log"
        r1 = engine_codex.start_login(r"C:\fake\codex.exe", popen=FakeLogin, log_path=log_path)
        self.assertEqual((r1["ok"], r1["pending"], r1["url"]), (True, True, "https://auth.openai.com/oauth/authorize?state=s0"))
        r2 = engine_codex.start_login(r"C:\fake\codex.exe", popen=FakeLogin, log_path=log_path)
        self.assertTrue(r2["ok"])
        self.assertEqual([p.returncode for p in made], [1, None])  # the first one was stopped before the second began
        self.assertEqual(made[1].args[1:], ["login"])
        self.assertTrue(engine_codex.stop_login())
        self.assertFalse(engine_codex.stop_login())
        deadline = time.time() + 5
        while time.time() < deadline and log_path.read_text("utf-8").count("=== exit") < 2:
            time.sleep(0.05)
        text = log_path.read_text("utf-8")
        self.assertEqual(text.count("=== exit 1 ==="), 2)
        self.assertIn("codex login ===", text)
        self.assertIn("state=s0", text)
        self.assertIn("state=s1", text)
        self.assertFalse(engine_codex.start_login(r"C:\fake\codex.exe")["ok"])  # the real one: refused under the test runner

    def test_find_codex_never_returns_a_script_shim(self):
        import tempfile
        from pathlib import Path
        empty = tempfile.mkdtemp()
        shim = r"C:\Users\you\AppData\Roaming\npm\codex.CMD"
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": empty}), \
                mock.patch.object(engine_codex.shutil, "which", lambda name: shim if name == "codex" else None):
            self.assertIsNone(engine_codex.find_codex())
            self.assertEqual(engine_codex.codex_shim(), shim)
        exe = r"C:\tools\codex.exe"
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": empty}), \
                mock.patch.object(engine_codex.shutil, "which", lambda name: exe):
            self.assertEqual(engine_codex.find_codex(), exe)
            self.assertEqual(engine_codex.codex_shim(), "")
        official = Path(empty) / "Programs" / "Codex" / "bin" / "codex.exe"
        official.parent.mkdir(parents=True)
        official.write_text("", "utf-8")
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": empty}):
            self.assertEqual(engine_codex.find_codex(), str(official))

    def test_real_transport_is_refused_under_the_test_runner(self):
        s = CodexSession(self.cfg, "P", lambda k, d: None, exe=r"C:\codex\codex.exe")
        with self.assertRaises(RuntimeError):
            s.start()
        self.assertFalse(engine_codex.login_status(r"C:\codex\codex.exe")[0])
        self.assertFalse(engine_codex.quick_probe(self.cfg)["ok"])
        self.assertFalse(engine_codex.start_login(r"C:\codex\codex.exe")["ok"])
        self.assertFalse(engine_codex.logout(r"C:\codex\codex.exe")[0])
