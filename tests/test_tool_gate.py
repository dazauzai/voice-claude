"""The spoken gate for Claude Code's own SendMessage: an in-process PreToolUse hook over stream-json (claude.py)."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from voiceclaude import config, paths
from voiceclaude.claude import GATED_TOOLS, HOOK_ID, ClaudeSession


def hook(tool="SendMessage", tool_input=None, callback=HOOK_ID, rid="r1", tool_use_id="tu1"):
    return {"type": "control_request", "request_id": rid, "request": {
        "subtype": "hook_callback", "callback_id": callback, "tool_use_id": tool_use_id,
        "input": {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_use_id": tool_use_id,
                  "tool_input": tool_input if tool_input is not None else {"to": "论文写作辅助", "message": "用户口述：你好"}}}}


class ToolGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        mock.patch.object(paths, "LINES_DIR", self.tmp / "lines").start()
        mock.patch("voiceclaude.claude.find_claude", lambda cmd: "claude.exe").start()
        from voiceclaude import access
        mock.patch.object(access, "detect", lambda cmd: access.AccessStatus(True, "subscription", "测试")).start()
        self.events = []
        self.s = ClaudeSession(config.Config(), "P", lambda k, d: self.events.append((k, d)), line_id="l1")
        self.written = []
        self.s._write = self.written.append

    def tearDown(self):
        mock.patch.stopall()

    def decision(self, i=-1):
        r = self.written[i]["response"]
        self.assertEqual((r["subtype"], self.written[i]["type"]), ("success", "control_response"))
        out = r["response"]["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        return r["request_id"], out["permissionDecision"], out["permissionDecisionReason"]

    def test_lines_refuse_peer_messages_and_keep_send_message_for_the_gate(self):
        args = self.s.build_args()
        self.assertEqual(json.loads(Path(args[args.index("--settings") + 1]).read_text("utf-8")),
                         {"crossSessionInbound": "refuse"})  # another session's message never starts a turn here
        self.assertNotIn("SendMessage", args[args.index("--disallowedTools") + 1])
        self.s.hooks_unsupported = True  # a CLI that would not take the hook: the tool is off instead
        args = self.s.build_args()
        self.assertIn("SendMessage", args[args.index("--disallowedTools") + 1].split(","))

    def test_start_registers_the_hook_before_any_message(self):
        with mock.patch("subprocess.Popen"), mock.patch("voiceclaude.claude.testing.under_test_runner", lambda: False), \
                mock.patch("threading.Thread"):
            self.s.start()
        req = self.written[0]["request"]
        self.assertEqual(req["subtype"], "initialize")
        self.assertEqual(req["hooks"], {"PreToolUse": [{"matcher": "SendMessage", "hookCallbackIds": [HOOK_ID],
                                                        "timeout": ClaudeSession.HOOK_TIMEOUT}]})
        self.assertGreater(ClaudeSession.HOOK_TIMEOUT, ClaudeSession.GATE_DEADLINE)  # we say no before it gives up

    def test_allow_deny_and_every_failure_is_a_no(self):
        asked = []

        def gate(tool, data, tool_use_id):
            asked.append((tool, data, tool_use_id))
            return data["message"].endswith("好"), "" if data["message"].endswith("好") else "用户没有同意。"

        self.s.tool_gate = gate
        self.s._answer_control(hook(tool_input={"to": "论文写作辅助", "message": "用户口述：好"}))
        self.assertEqual(self.decision(), ("r1", "allow", "用户同意了。"))
        self.assertEqual(asked, [("SendMessage", {"to": "论文写作辅助", "message": "用户口述：好"}, "tu1")])
        self.s._answer_control(hook(rid="r2", tool_use_id="tu2", tool_input={"to": "论文写作辅助", "message": "用户口述：算了"}))
        self.assertEqual(self.decision(), ("r2", "deny", "用户没有同意。"))

        def broken(*a):
            raise RuntimeError("boom")

        self.s.tool_gate = broken
        self.s._answer_control(hook(rid="r3"))
        self.assertEqual(self.decision()[1], "deny")
        self.s.tool_gate = gate
        asked.clear()
        self.s._answer_control(hook(rid="r4", callback="hook_99"))  # a callback we never registered
        self.s._answer_control(hook(rid="r5", tool="Bash"))  # a tool that is not ours to gate
        self.assertEqual([self.decision(i)[1] for i in (-2, -1)], ["deny", "deny"])
        self.assertEqual(asked, [])
        self.s.tool_gate = None
        self.s._answer_control(hook(rid="r6"))
        self.assertEqual(self.decision()[1], "deny")
        self.s._answer_control({"type": "control_request", "request_id": "r7", "request": {"subtype": "can_use_tool"}})
        self.assertEqual(self.written[-1]["response"]["subtype"], "error")  # nothing else is answered with a yes

    def test_a_question_that_takes_too_long_is_a_no_before_claude_code_gives_up(self):
        self.s.GATE_DEADLINE = 0.1
        self.s.tool_gate = lambda *a: (time.sleep(1), (True, ""))[1]
        t0 = time.monotonic()
        self.s._answer_control(hook())
        self.assertLess(time.monotonic() - t0, 0.8)
        self.assertEqual(self.decision()[1:], ("deny", "等用户批准等得太久，没有执行。"))
        self.assertEqual(self.s._gated, {})

    def test_the_hook_answer_comes_off_the_reader_thread_and_the_result_is_reported(self):
        self.s.tool_gate = lambda *a: (True, "")
        self.s._handle(hook(tool_use_id="tu9"))
        for _ in range(100):
            if self.written:
                break
            time.sleep(0.02)
        self.assertEqual(self.decision()[1], "allow")
        self.s._handle({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu9", "content": [{"type": "text", "text": '{"success":true}'}]}]}})
        self.assertIn(("gated_result", {"tool": "SendMessage", "tool_use_id": "tu9", "ok": True, "text": '{"success":true}'}),
                      self.events)
        self.events.clear()
        self.s._handle({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu9", "content": "x"}]}})
        self.assertEqual([k for k, d in self.events if k == "gated_result"], [])  # reported once

    def test_a_cli_that_will_not_take_the_hook_runs_without_the_tool(self):
        restarted = []
        self.s.close = lambda: restarted.append("close")
        self.s.start = lambda: restarted.append("start")
        for response in ({"subtype": "error", "error": "unknown subtype"}, {"subtype": "success", "response": {"hooks_applied": False}}, None):
            self.s.hooks_unsupported, restarted[:] = False, []
            slot = {"event": threading.Event(), "response": response}
            slot["event"].set()
            self.s._init_slot = slot
            self.s.INIT_TIMEOUT = 0.05
            self.s._await_init()
            self.assertEqual((self.s.hooks_unsupported, restarted), (True, ["close", "start"]), response)
        self.s.hooks_unsupported, restarted[:] = False, []
        slot = {"event": threading.Event(), "response": {"subtype": "success", "response": {"hooks_applied": True}}}
        slot["event"].set()
        self.s._init_slot = slot
        self.s._await_init()
        self.assertEqual((self.s.hooks_unsupported, restarted), (False, []))
        self.assertEqual(GATED_TOOLS, ("SendMessage",))
        # no answer because the process died: not taken for a refusal, and no long wait (its exit is reported)
        self.s._init_slot = {"event": threading.Event(), "response": None}
        self.s.INIT_TIMEOUT = 30
        t0 = time.monotonic()
        self.s._await_init()
        self.assertLess(time.monotonic() - t0, 1)
        self.assertEqual((self.s.hooks_unsupported, restarted), (False, []))


if __name__ == "__main__":
    unittest.main()
