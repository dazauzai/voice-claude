import unittest

from voiceclaude import line as linemod
from voiceclaude.line import DEAD, IDLE, WAITING, WORKING, Line


class FakeSession:
    def __init__(self, line, resume):
        self.line, self.resume = line, resume
        self.sent, self.calls = [], []

    ready_on_start = True

    def start(self):
        self.calls.append("start")
        if self.ready_on_start:
            self.line.on_session("ready", {"session_id": "s-new"})

    def send(self, text):
        self.sent.append(text)

    def interrupt(self):
        self.calls.append("interrupt")

    def close(self):
        self.calls.append("close")

    def kill(self):
        self.calls.append("kill")

    def restart(self, model=None):
        self.calls.append("restart")


class LineTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.clock = [100.0]
        self.line = Line("l1", "Atlas", "E:/Atlas", "project", FakeSession, lambda i, k, d: self.events.append((i, k, d)),
                         now=lambda: self.clock[0])
        self.line.start(resume="s-9")

    def test_state_transitions(self):
        self.assertEqual(self.line.state, IDLE)
        self.assertEqual(self.line.session.resume, "s-9")
        self.line.say("hi")
        self.assertEqual(self.line.state, WORKING)
        self.assertTrue(self.line.busy and self.line.keep_alive)
        self.line.on_session("text", "好的，")
        self.line.on_session("text", "做完了。")
        self.line.on_session("done", {"status": "ok", "message": "好的，做完了。"})
        self.assertEqual(self.line.state, IDLE)
        self.assertEqual(self.events[-1][1:], ("done", {"status": "ok", "last": "好的，做完了", "kind": "report"}))
        self.assertEqual(self.line.mailbox[-1]["kind"], "report")
        self.line.on_session("exit", 1)
        self.assertEqual(self.line.state, DEAD)
        self.assertEqual(self.events[-1][1:], ("dead", {"code": 1, "busy": False}))  # it was idle

    def test_exit_mid_turn_says_so_and_remembers_the_words(self):
        self.assertEqual((self.line.started_resume, self.line.ready_seen), ("s-9", True))  # this fake reports ready at once
        self.line.say("打开记事本")
        self.line.on_session("exit", 1)
        self.assertEqual(self.events[-1][1:], ("dead", {"code": 1, "busy": True}))
        self.assertEqual(self.line.last_said, "打开记事本")

    def test_done_classification(self):
        self.assertEqual(linemod.classify_done("ok", "跑完了。"), "report")
        self.assertEqual(linemod.classify_done("ok", "要不要我继续？"), "needs_you")
        self.assertEqual(linemod.classify_done("ok", "Continue?"), "needs_you")
        self.assertEqual(linemod.classify_done("error", "x"), "error")
        self.assertIsNone(linemod.classify_done("interrupted", "x"))
        self.line.say("q")
        self.line.on_session("done", {"status": "ok", "message": "第一步好了。第二步要不要做？"})
        self.assertEqual(self.line.state, WAITING)
        self.assertEqual(self.line.unread()[0]["text"], "第二步要不要做")

    def test_return_summary_and_keep_alive(self):
        self.assertEqual(self.line.summary_for_return(), "")
        self.line.say("a")
        self.line.on_session("done", {"status": "ok", "message": "第一件做完了。"})
        self.assertEqual(self.line.summary_for_return(), "你不在的时候，Atlas说：第一件做完了")
        self.line.say("b")
        self.line.on_session("done", {"status": "ok", "message": "第二件也做完了。"})
        self.assertIn("有 2 条消息，最后一条：第二件也做完了", self.line.summary_for_return())
        self.assertTrue(self.line.keep_alive)  # unread mail protects the line from eviction
        self.line.mark_read()
        self.assertFalse(self.line.keep_alive)
        self.assertEqual(self.line.summary_for_return(), "")

    def test_usable_before_claude_reports_ready(self):
        """Claude Code's init event only comes after the first message, so a line must accept words at once."""
        FakeSession.ready_on_start = False
        try:
            line = Line("l2", "Atlas", "E:/Atlas", "project", FakeSession, lambda i, k, d: self.events.append((i, k, d)))
            line.start()
            self.assertEqual(line.state, IDLE)
            self.assertTrue(line.say("第一句"))
            self.assertEqual(line.session.sent, ["第一句"])
            self.assertEqual(line.state, WORKING)
            line.on_session("ready", {"session_id": "s"})
            self.assertEqual(self.events[-1], ("l2", "ready", {"session_id": "s", "first": True}))
            line.on_session("ready", {"session_id": "s"})
            self.assertEqual(self.events[-1][2]["first"], False)
        finally:
            FakeSession.ready_on_start = True

    def test_interrupted_turn_leaves_no_mail(self):
        self.line.say("a")
        self.line.on_session("done", {"status": "interrupted", "message": ""})
        self.assertEqual(self.line.mailbox, [])
        self.assertEqual(self.line.state, IDLE)

    def test_kill_and_resume_and_tool_detail(self):
        self.line.kill_and_resume()
        self.assertEqual(self.line.session.calls[-2:], ["kill", "restart"])
        self.line.on_session("tool", {"name": "mcp__desktop__open_app", "input": {"name": "记事本"}})
        self.assertEqual(self.events[-1][2], {"name": "mcp__desktop__open_app", "detail": "记事本"})
        self.clock[0] = 130.0
        self.line.say("x")
        self.clock[0] = 142.0
        self.assertIn("在干活（12 秒）", self.line.status_text())


if __name__ == "__main__":
    unittest.main()
