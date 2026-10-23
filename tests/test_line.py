import time
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


class SayRollbackTests(unittest.TestCase):
    def test_an_exit_during_turn_start_leaves_the_line_dead(self):
        """The app-server exits while starting the turn: the line is DEAD (so the next words reconnect it); the turn's
        late error must not make it IDLE with no process behind it."""
        import sys
        from pathlib import Path as P
        sys.path.insert(0, str(P(__file__).parent))
        from test_engine_codex import FakeAppServer, wait_for
        from voiceclaude import config
        from voiceclaude.engine_codex import CodexSession
        script = {}
        events = []

        def factory(ln, resume):
            return CodexSession(config.Config(), "x", ln.on_session, line_id=ln.id, exe=r"C:\codex\codex.exe",
                                transport_factory=lambda a, c, e: FakeAppServer(a, c, e, script))
        line = Line("l9", "Atlas", ".", "project", factory, lambda i, k, d: events.append((k, d)))
        line.start()
        self.assertTrue(wait_for(lambda: line.session.ready.is_set()))
        script["turn_dies"] = True
        line.say("把 build 目录删掉", "t1")
        self.assertTrue(wait_for(lambda: line.state == DEAD))
        time.sleep(0.3)
        self.assertEqual(line.state, DEAD)
        self.assertEqual([k for k, d in events].count("dead"), 1)
        line.on_session("done", {"status": "error", "message": "late"})  # however late it comes
        self.assertEqual(line.state, DEAD)
        line.session.close()

    def test_a_refused_send_never_undoes_a_later_say(self):
        """The first sentence's send fails only after the line said something else meanwhile (a reconnect resent it):
        the rollback leaves that later say's state and words alone."""
        line = Line("l1", "Atlas", "E:/Atlas", "project", FakeSession, lambda i, k, d: None)
        line.start()
        real_send = line.session.send

        def later_say_then_fail(text):
            line.session.send = real_send
            line.say("又说了一句", "t2")
            raise RuntimeError("Codex 重新启动过，这句没有发出去，请再说一遍")
        line.session.send = later_say_then_fail
        with self.assertRaises(RuntimeError):
            line.say("第一句", "t1")
        self.assertEqual((line.state, line.last_said, [e["turn"] for e in line.turn_log]), (WORKING, "又说了一句", ["t2"]))

    def test_a_refused_send_keeps_a_death_that_happened_meanwhile(self):
        """The Codex app-server died while the sentence waited for it: the line is DEAD (so the next words reconnect it),
        and the rollback must not put it back to IDLE."""
        events = []
        line = Line("l1", "Atlas", "E:/Atlas", "project", FakeSession, lambda i, k, d: events.append((k, d)))
        line.start()
        line.say("一", "t1")
        line.on_session("done", {"status": "ok", "message": "好。"})

        def dies_then_refuses(text):
            line.on_session("exit", 1)
            raise RuntimeError("Codex 已关闭")
        line.session.send = dies_then_refuses
        with self.assertRaises(RuntimeError):
            line.say("二", "t2")
        self.assertEqual(line.state, DEAD)
        self.assertEqual(([e["turn"] for e in line.turn_log], line.last_said), (["t1"], "一"))


class TurnLogTests(unittest.TestCase):
    def test_each_user_turn_keeps_the_engine_ids_and_the_files_it_changed(self):
        events = []
        line = Line("l1", "Atlas", "E:/Atlas", "project", FakeSession, lambda i, k, d: events.append((k, d)))
        line.start()
        line.say("改一下", "t1")
        line.on_session("mark", {"prompt": "p1", "last": "p1"})
        line.on_session("tool", {"name": "Write", "input": {"file_path": "a.txt"}})
        line.on_session("mark", {"last": "a1"})
        line.on_session("tool", {"name": "Edit", "input": {"file_path": "x、y", "files": ["x", "y"]}})  # Codex's file change
        line.on_session("tool", {"name": "Bash", "input": {"command": "ls"}})
        line.on_session("mark", {"prompt": "late", "codex_turn": "turn-1"})  # a prompt id is never replaced
        e = line.turn_log[-1]
        self.assertEqual((e["turn"], e["text"], e["prompt"], e["last"], e["codex_turn"]), ("t1", "改一下", "p1", "a1", "turn-1"))
        self.assertEqual(e["files"], {"a.txt", "x", "y"})
        self.assertEqual(e["created"], {"a.txt", "x", "y"})  # none of them existed when the tool was called
        line.say("二")
        self.assertTrue(line.turn_log[-1]["turn"].startswith("t"))
        line.start()  # a new process: the earlier turns are not known to it
        self.assertEqual((line.turn_log, line.log_from_start), ([], True))
        line.start("saved")  # a resumed conversation: the record does not reach its start
        self.assertFalse(line.log_from_start)
        line.start()
        for i in range(201):
            line.say(str(i))
        self.assertEqual((len(line.turn_log), line.log_from_start), (200, False))  # trimmed: no longer from the start


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
