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

    def test_rewind_flags_follow_the_sdk(self):
        """--resume <id> --fork-session --resume-session-at=<last kept entry> --resume-drops-turn=<dropped prompt>,
        the prompts replayed for their uuids, and file checkpointing on."""
        s = ClaudeSession(config.Config(), "P", lambda k, d: None, line_id="l1")
        args = s.build_args()
        self.assertIn("--replay-user-messages", args)
        self.assertNotIn("--fork-session", args)
        self.assertEqual(s.build_env()["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"], "true")
        with self.assertRaises(RuntimeError):
            s.rewind_to({"last": None}, {"prompt": "p3"})  # points unknown: nothing happens
        s.session_id = "sess-1"
        starts = self._fake_starts(s)
        s.rewind_to({"last": "a-2"}, {"prompt": "p-3"})  # one turn dropped: guarded by --resume-drops-turn
        self.assertEqual(starts, [(("a-2", "p-3"), "sess-1")])
        args = s.build_args()
        i = args.index("--resume")
        self.assertEqual(args[i:i + 5], ["--resume", "sess-1", "--fork-session", "--resume-session-at=a-2", "--resume-drops-turn=p-3"])
        # two turns dropped: --resume-drops-turn checks that everything dropped is ONE turn, so it is left out
        s.fork_at = None
        s.rewind_to({"last": "a-1"}, {"prompt": "p-2"}, single=False)
        args = s.build_args()
        i = args.index("--resume")
        self.assertEqual(args[i:i + 4], ["--resume", "sess-1", "--fork-session", "--resume-session-at=a-1"])
        self.assertFalse(any(a.startswith("--resume-drops-turn") for a in args))
        mock.patch.stopall()
        mock.patch.object(paths, "LINES_DIR", self.tmp / "lines").start()
        mock.patch("voiceclaude.claude.find_claude", lambda cmd: "claude.exe").start()
        s._handle({"type": "system", "subtype": "init", "session_id": "sess-1"})
        self.assertIsNotNone(s.fork_at)  # not forked yet
        s._handle({"type": "system", "subtype": "init", "session_id": "sess-fork"})
        self.assertIsNone(s.fork_at)  # the fork exists: later restarts resume it plainly
        self.assertEqual(s.session_id, "sess-fork")
        s.fork_at = ("a", "p")
        with mock.patch.object(s, "start", lambda: None), mock.patch.object(s, "close", lambda: None):
            s.restart(fresh=True)
        self.assertIsNone(s.fork_at)

    def _fake_starts(self, s, refuse_first=False):
        """start() without a real process: a stand-in that stays up (or, refuse_first, is refused by Claude Code at
        once, the way --resume-drops-turn refuses). Records (fork_at, resume) at every start."""
        import threading
        starts = []

        class Proc:
            def __init__(self):
                self.stdin, self.stdout, self.code = mock.Mock(), iter([]), None

            def poll(self):
                return self.code

            def wait(self):
                return 1
        mock.patch.object(type(s), "REWIND_SETTLE", 0.05).start()
        mock.patch.object(type(s), "REWIND_TIMEOUT", 0.2).start()
        mock.patch.object(s, "close", lambda: setattr(s, "proc", None)).start()

        def start():
            starts.append((s.fork_at, s.resume))
            s.proc = proc = Proc()
            if refuse_first and len(starts) == 1:
                def refused():
                    s.fork_rejected = True  # "Resume rejected by --resume-drops-turn: ..."
                    proc.code = 1
                    s._read(proc)
                threading.Thread(target=refused, daemon=True).start()
        mock.patch.object(s, "start", start).start()
        return starts

    def test_a_second_rewind_before_the_first_is_made(self):
        """The first rewind's fork waits for the next message; a second one before that resumes the same base session,
        which still holds the turns the first dropped: never the one-turn guard, and a refusal goes back to the first."""
        s = ClaudeSession(config.Config(), "P", lambda k, d: None, line_id="l1")
        s.session_id = "S"
        starts = self._fake_starts(s)
        s.rewind_to({"last": "a2"}, {"prompt": "p3"})
        s.rewind_to({"last": "a1"}, {"prompt": "p2"})  # single by the record, but S has 二 and 三 after a1
        self.assertEqual(starts[-1], (("a1", None), "S"))
        mock.patch.stopall()
        mock.patch.object(paths, "LINES_DIR", self.tmp / "lines").start()
        mock.patch("voiceclaude.claude.find_claude", lambda cmd: "claude.exe").start()
        s2 = ClaudeSession(config.Config(), "P", lambda k, d: None, line_id="l2")
        s2.session_id, s2.fork_at = "S", ("a2", "p3")
        starts = self._fake_starts(s2, refuse_first=True)
        with self.assertRaises(RuntimeError):
            s2.rewind_to({"last": "a1"}, {"prompt": "p2"})
        self.assertEqual(starts[-1], (("a2", "p3"), "S"))  # back to the first rewind, not to all of S
        self.assertEqual(s2.fork_at, ("a2", "p3"))

    def test_a_refused_rewind_puts_the_line_back_on_its_conversation(self):
        """The end-to-end run: Claude Code refused the truncated resume. rewind_to must not report success, and the line
        must go on in the conversation as it was (resumed plainly), not be left broken."""
        events = []
        s = ClaudeSession(config.Config(), "P", lambda k, d: events.append((k, d)), line_id="l1")
        s.session_id = "sess-1"
        starts = self._fake_starts(s, refuse_first=True)
        with self.assertRaises(RuntimeError) as e:
            s.rewind_to({"last": "a-1"}, {"prompt": "p-2"})
        self.assertIn("不接受这次撤回", str(e.exception))
        self.assertEqual(starts, [(("a-1", "p-2"), "sess-1"), (None, "sess-1")])  # then plainly, on the same conversation
        self.assertEqual((s.fork_at, s.session_id), (None, "sess-1"))
        self.assertIsNotNone(s.proc)
        self.assertNotIn("exit", [k for k, d in events])  # the line is not dead

    def test_marks_file_rewind_and_a_refused_truncation(self):
        import threading
        events = []
        s = ClaudeSession(config.Config(), "P", lambda k, d: events.append((k, d)), line_id="l1")
        s._handle({"type": "user", "uuid": "p1", "message": {"role": "user", "content": "改一下 a.txt"}})
        s._handle({"type": "assistant", "uuid": "a1", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "a.txt"}}]}})
        s._handle({"type": "user", "uuid": "r1", "message": {"content": [{"type": "tool_result", "tool_use_id": "x"}]}})
        s._handle({"type": "user", "uuid": "sub", "parent_tool_use_id": "t", "message": {"content": "subagent"}})
        self.assertEqual([d for k, d in events if k == "mark"], [{"prompt": "p1", "last": "p1"}, {"last": "a1"}, {"last": "r1"}])

        class Pipe:
            def __init__(self):
                self.lines = []

            def write(self, t):
                self.lines.append(t)

            def flush(self):
                pass

        class Proc:
            stdin = Pipe()
            stdout = iter([])

            def poll(self):
                return None

            def wait(self):
                return 1
        s.proc = Proc()
        out = {}
        t = threading.Thread(target=lambda: out.update(r=s.rewind_files("p1", timeout=3)))
        t.start()
        for _ in range(100):
            if Proc.stdin.lines:
                break
            threading.Event().wait(0.01)
        req = json.loads(Proc.stdin.lines[-1])
        self.assertEqual((req["type"], req["request"]), ("control_request", {"subtype": "rewind_files", "user_message_id": "p1"}))
        s._handle({"type": "control_response", "response": {"subtype": "success", "request_id": req["request_id"]}})
        t.join(3)
        self.assertEqual(out["r"], {"ok": True, "error": "", "files": None, "skipped": 0})
        t = threading.Thread(target=lambda: out.update(r=s.rewind_files("p1", timeout=3)))
        t.start()
        for _ in range(100):
            if len(Proc.stdin.lines) >= 2:
                break
            threading.Event().wait(0.01)
        s._handle({"type": "control_response", "response": {"subtype": "error", "request_id": json.loads(Proc.stdin.lines[-1])["request_id"],
                                                            "error": "no checkpoint"}})
        t.join(3)
        self.assertEqual(out["r"], {"ok": False, "error": "no checkpoint", "files": None, "skipped": 0})
        self.assertEqual(s.rewind_files("p1", timeout=0.05)["error"], "Claude 没有回应")
        # a dry run: Claude Code's own list of what it would change, and links it would skip
        t = threading.Thread(target=lambda: out.update(r=s.rewind_files("p1", dry_run=True, timeout=3)))
        t.start()
        for _ in range(100):
            if json.loads(Proc.stdin.lines[-1])["request"].get("dry_run"):
                break
            threading.Event().wait(0.01)
        req = json.loads(Proc.stdin.lines[-1])
        self.assertEqual(req["request"], {"subtype": "rewind_files", "user_message_id": "p1", "dry_run": True})
        s._handle({"type": "control_response", "response": {"subtype": "success", "request_id": req["request_id"],
                                                            "response": {"canRewind": True, "filesChanged": ["C:/p/a.txt"], "skippedLinks": 1}}})
        t.join(3)
        self.assertEqual(out["r"], {"ok": True, "error": "", "files": ["C:/p/a.txt"], "skipped": 1})
        # Claude Code refused the truncating resume: resume as it was, say so, no 'exit'
        proc = Proc()
        s.proc, s.fork_at, s.fork_rejected = proc, ("a1", "p1"), True
        started = []
        with mock.patch.object(s, "start", lambda: started.append(1)):
            s._read(proc)
        self.assertEqual((started, s.fork_at), ([1], None))
        self.assertIn(("rewind_failed", {}), events)
        self.assertNotIn("exit", [k for k, d in events])

    @staticmethod
    def _mu(**models):
        """modelUsage as Claude Code sends it: running totals per model."""
        out = {}
        for name, (inp, outp, cr, cw, cost, canonical) in models.items():
            out[name] = {"inputTokens": inp, "outputTokens": outp, "cacheReadInputTokens": cr, "cacheCreationInputTokens": cw,
                         "webSearchRequests": 0, "costUSD": cost, "costBasis": "list", "canonicalModel": canonical}
        return out

    def test_usage_per_turn_and_rate_limits(self):
        """'tokens' = result.usage (the turn's main loop); 'all_tokens' and the cost = the change in modelUsage over every
        model (a subagent on another model counts too); a conversation reset starts the totals again."""
        events = []
        s = ClaudeSession(config.Config(), "P", lambda k, d: events.append((k, d)), line_id="l1")
        s._handle({"type": "result", "usage": {"input_tokens": 3, "output_tokens": 324, "cache_read_input_tokens": 15000,
                                               "cache_creation_input_tokens": 2000},
                   "modelUsage": self._mu(**{"claude-opus-5-5": (3, 324, 15000, 2000, 0.0123, "claude-opus-5-5")}),
                   "total_cost_usd": 0.0123})
        # turn 2: the main loop on Opus, plus a subagent on Haiku that result.usage leaves out
        s._handle({"type": "result", "usage": {"input_tokens": 5, "output_tokens": 65},
                   "modelUsage": self._mu(**{"claude-opus-5-5": (8, 389, 15000, 2000, 0.0150, "claude-opus-5-5"),
                                             "claude-haiku-4-5-20251001": (900, 120, 0, 0, 0.0010, "claude-haiku-4-5")}),
                   "total_cost_usd": 0.0160})
        u = [d for k, d in events if k == "usage"]
        self.assertEqual((u[0]["model"], u[0]["tokens"], u[0]["cost_usd"], u[0]["cost_basis"]),
                         ("claude-opus-5-5", {"input": 3, "output": 324, "cache_read": 15000, "cache_write": 2000}, 0.0123, "list"))
        self.assertEqual(u[1]["tokens"], {"input": 5, "output": 65, "cache_read": 0, "cache_write": 0})  # the line under the reply
        self.assertEqual(u[1]["all_tokens"], {"input": 905, "output": 185, "cache_read": 0, "cache_write": 0, "web_search": 0})
        self.assertEqual(u[1]["cost_usd"], 0.0037)  # Opus 0.0027 + the Haiku subagent 0.0010
        self.assertEqual(sorted(m["model"] for m in u[1]["by_model"]), ["claude-haiku-4-5", "claude-opus-5-5"])
        s._handle({"type": "conversation_reset"})
        s._handle({"type": "result", "usage": {"input_tokens": 1, "output_tokens": 2},
                   "modelUsage": self._mu(**{"claude-opus-5-5": (1, 2, 0, 0, 0.0004, "claude-opus-5-5")}), "total_cost_usd": 0.0004})
        self.assertEqual([d for k, d in events if k == "usage"][-1]["cost_usd"], 0.0004)  # counted from 0 again
        info = {"status": "allowed", "rateLimitType": "five_hour", "unifiedWindows": {"five_hour": {"utilization": 0.08}}}
        s._handle({"type": "rate_limit_event", "rate_limit_info": info})
        self.assertIn(("limits", info), events)

    def test_a_resumed_sessions_restored_spend_is_not_counted_again(self):
        """Since Claude Code 2.1.277 the totals of a resumed session carry its earlier spend. With the session's saved
        totals the first turn is the change from them; without them, only that turn's own tokens and no cost."""
        saved = {"S1": {"models": {"claude-opus-5-5": {"input": 500, "output": 9000, "cache_read": 300000, "cache_write": 20000,
                                                       "web_search": 1, "cost": 4.20}}, "cost": 4.20}}
        events = []
        s = ClaudeSession(config.Config(), "P", lambda k, d: events.append((k, d)), line_id="l1", resume="S1")
        s.baseline_lookup = saved.get
        with mock.patch.object(s, "build_args", lambda: []), mock.patch("subprocess.Popen"), \
                mock.patch("voiceclaude.claude.testing.under_test_runner", lambda: False), mock.patch("threading.Thread"):
            s.start()
        restored = self._mu(**{"claude-opus-5-5": (510, 9100, 310000, 20000, 4.25, "claude-opus-5-5")})
        restored["claude-opus-5-5"]["webSearchRequests"] = 1
        s._handle({"type": "result", "usage": {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 10000},
                   "modelUsage": restored, "total_cost_usd": 4.25})
        u = [d for k, d in events if k == "usage"][-1]
        self.assertEqual((u["all_tokens"]["output"], u["cost_usd"]), (100, 0.05))  # not the session's whole 4.25
        self.assertEqual((u["session"], u["baseline"]["cost"]), (s.session_id, 4.25))  # saved again for next time
        events.clear()
        s2 = ClaudeSession(config.Config(), "P", lambda k, d: events.append((k, d)), line_id="l2", resume="S-unknown")
        s2.baseline_lookup = saved.get
        with mock.patch.object(s2, "build_args", lambda: []), mock.patch("subprocess.Popen"), \
                mock.patch("voiceclaude.claude.testing.under_test_runner", lambda: False), mock.patch("threading.Thread"):
            s2.start()
        s2._handle({"type": "result", "usage": {"input_tokens": 10, "output_tokens": 100}, "modelUsage": restored,
                    "total_cost_usd": 4.25})
        u = [d for k, d in events if k == "usage"][-1]
        self.assertEqual((u["all_tokens"]["output"], u["cost_usd"]), (100, None))  # only its own tokens, cost not known
        more = self._mu(**{"claude-opus-5-5": (515, 9150, 310000, 20000, 4.27, "claude-opus-5-5")})
        more["claude-opus-5-5"]["webSearchRequests"] = 1
        s2._handle({"type": "result", "usage": {"input_tokens": 5, "output_tokens": 50}, "modelUsage": more, "total_cost_usd": 4.27})
        u = [d for k, d in events if k == "usage"][-1]
        self.assertEqual((u["all_tokens"]["output"], u["cost_usd"]), (50, 0.02))  # from the first result on, the change
        s.fork_at = ("u-3", None)  # a rewind restores only part of the spend: its first turn has no known cost
        with mock.patch.object(s, "build_args", lambda: []), mock.patch("subprocess.Popen"), \
                mock.patch("voiceclaude.claude.testing.under_test_runner", lambda: False), mock.patch("threading.Thread"):
            s.start()
        s._handle({"type": "result", "usage": {"input_tokens": 7, "output_tokens": 70}, "modelUsage": more, "total_cost_usd": 4.27})
        u = [d for k, d in events if k == "usage"][-1]
        self.assertEqual((u["all_tokens"]["output"], u["cost_usd"]), (70, None))

    def test_a_long_tool_call_being_written_is_shown(self):
        """A 2000-character type_text takes the model a minute to write, with nothing to show: 'writing' after 3 s,
        every 2 s after that, done when the call is complete. A quick call says nothing."""
        events, clock = [], [100.0]
        s = ClaudeSession(config.Config(), "P", lambda k, d: events.append((k, d)), line_id="l1")

        def ev(e):
            s._handle({"type": "stream_event", "event": e})

        def delta(text, at):
            clock[0] = at
            ev({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": text}})

        with mock.patch("voiceclaude.claude.time.monotonic", lambda: clock[0]):
            ev({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "name": "mcp__desktop__click"}})
            clock[0] = 101.0
            ev({"type": "content_block_stop", "index": 0})
            ev({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "name": "mcp__desktop__type_text"}})
            for i, at in enumerate((101.0, 102.0, 103.5, 104.0, 105.0, 105.6, 106.0, 108.0)):
                delta("x" * 10, at)
            clock[0] = 109.0
            ev({"type": "content_block_stop", "index": 1})
        w = [d for k, d in events if k == "writing"]
        self.assertEqual([(d["tool"], d["chars"], d["seconds"], d["done"]) for d in w],
                         [("mcp__desktop__type_text", 40, 3, False), ("mcp__desktop__type_text", 70, 5, False),
                          ("mcp__desktop__type_text", 80, 7, False), ("mcp__desktop__type_text", 80, 8, True)])
        events.clear()
        with mock.patch("voiceclaude.claude.time.monotonic", lambda: clock[0]):  # cut off by the end of the turn
            ev({"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "name": "mcp__desktop__type_text"}})
            delta("y", 115.0)
            s._handle({"type": "result", "subtype": "error_during_execution", "is_error": True})
        self.assertEqual([d["done"] for k, d in events if k == "writing"], [False, True])
        self.assertTrue(any(k == "text" for k, d in events) is False)  # nothing of it is read out

    def test_desktop_disabled_drops_server(self):
        cfg = config.Config()
        cfg.desktop_enabled = False
        s = ClaudeSession(cfg, "P", lambda k, d: None)
        args = s.build_args()
        self.assertEqual(args[args.index("--allowedTools") + 1], "mcp__voiceclaude")


if __name__ == "__main__":
    unittest.main()
