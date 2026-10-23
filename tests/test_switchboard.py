import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from voiceclaude import config, jobs, paths, switchboard
from voiceclaude.line import WORKING
from voiceclaude.projects import Project
from voiceclaude.switchboard import MANAGER, SCRATCH, Switchboard


class FakeSession:
    def __init__(self, line, resume):
        self.line, self.resume = line, resume
        self.model = "opus"
        self.provider = getattr(line, "provider", "claude")
        self.sent, self.calls = [], []

    def start(self):
        self.calls.append("start")
        self.line.on_session("ready", {"session_id": "sess-" + self.line.id})

    def send(self, text):
        self.sent.append(text)

    def interrupt(self):
        self.calls.append("interrupt")

    def close(self):
        self.calls.append("close")

    def kill(self):
        self.calls.append("kill")

    def restart(self, model=None, provider=None, fresh=False):
        self.calls.append(("restart", model) if provider is None and not fresh else ("restart", model, provider, fresh))
        if provider:
            self.provider = provider
        if model:
            self.model = model

    refuse_rewind = False

    def rewind_to(self, keep, drop, single=True):
        self.calls.append(("rewind_to", keep.get("last") or keep.get("codex_turn"), drop.get("prompt") or drop.get("codex_turn"), single))
        if self.refuse_rewind:
            raise RuntimeError("Claude Code 不接受这次撤回（那几轮里有没看到的内容）")

    dry_files = None  # what Claude Code's dry run reports as filesChanged (None: it does not say)

    still_after = None  # what a dry run right after a restore still reports (files that were not put back)

    def rewind_files(self, user_message_id, dry_run=False):
        self.calls.append(("rewind_files", user_message_id) + (("dry",) if dry_run else ()))
        if dry_run and getattr(self, "_restored", False):
            self._restored = False
            return {"ok": True, "error": "", "files": self.still_after or [], "skipped": 0}
        self._restored = not dry_run
        return {"ok": True, "error": "", "files": self.dry_files, "skipped": 0}


def project(name, cwd, key=None, aliases=(), session_id=""):
    return Project(key=key or cwd.lower(), name=name, display_name=name, cwd=cwd, aliases=list(aliases), session_id=session_id)


class SwitchboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("HOME_DIR", "MANAGER_DIR", "MANAGER_LOG_DIR", "SCRATCH_DIR", "JOBS_DIR", "LINES_DIR"):
            mock.patch.object(paths, name, self.tmp / name.lower()).start()
        mock.patch.object(paths, "LINES_FILE", self.tmp / "lines.json").start()
        mock.patch.object(paths, "PROJECTS_FILE", self.tmp / "projects.toml").start()
        mock.patch.object(paths, "AUTH_FILE", self.tmp / "auth.json").start()  # never the user's real key file
        from voiceclaude import access
        mock.patch.object(access, "check_reach", lambda pid, cfg=None: (True, "")).start()  # no network in tests
        mock.patch.object(switchboard, "config_save", lambda *a, **k: None).start()
        self.events = []
        self.forwarded = []
        self.clock = [1000.0]
        self.cfg = config.Config()
        self.cfg.max_lines = 2
        self.cfg.idle_minutes = 10
        (self.tmp / "atlas").mkdir()
        (self.tmp / "beacon").mkdir()
        (self.tmp / "bench").mkdir()
        self.projects = [project("Atlas", str(self.tmp / "atlas"), aliases=["服务器"], session_id="old-atlas"),
                         project("Beacon", str(self.tmp / "beacon")),
                         project("benchmarking", str(self.tmp / "bench"))]
        self.jobs = jobs.Jobs(self.tmp / "jobs", self.tmp / "jobs.json", lambda j: None, poll_seconds=0.2)
        self.jobs.load()
        self.sb = Switchboard(self.cfg, jobs=self.jobs, emit=lambda k, d: self.events.append((k, d)),
                              forward=lambda p, d: (self.forwarded.append((p, d)), {"ok": True, "message": "前台做了"})[1],
                              session_factory=FakeSession, scan_projects=lambda: list(self.projects),
                              detect_access=lambda cmd: __import__("voiceclaude.access", fromlist=["x"]).AccessStatus(True, "subscription", "测试"),
                              now=lambda: self.clock[0], ask_timeout=0.3)
        self.sb.start()

    def tearDown(self):
        self.jobs.close()
        mock.patch.stopall()

    def notices(self):
        return [d["text"] for k, d in self.events if k == "notice"]

    def test_start_with_manager_emits_notice(self):
        self.assertEqual(self.sb.active_id, MANAGER)
        self.assertEqual(self.notices().count("我是总机，要去哪个项目？"), 1)
        self.assertEqual(set(self.sb.lines), {MANAGER})  # the scratch line is started on first use
        self.assertTrue(any(k == "line" and d["line"] == MANAGER and d["active"] for k, d in self.events))
        self.assertEqual(self.sb.greeting(), "我是总机，要去哪个项目？")

    def test_greets_and_accepts_words_before_claude_reports_ready(self):
        """Claude Code's init only arrives after the first message: never wait for it."""
        slow = []

        class SlowSession(FakeSession):
            def start(self):
                self.calls.append("start")
                slow.append(self)  # no 'ready' yet

        from voiceclaude import access
        sb = Switchboard(self.cfg, jobs=self.jobs, emit=lambda k, d: self.events.append((k, d)),
                         forward=lambda p, d: {"ok": True}, session_factory=SlowSession,
                         scan_projects=lambda: list(self.projects), now=lambda: self.clock[0], ask_timeout=0.3,
                         detect_access=lambda cmd: access.AccessStatus(True, "subscription", "测试"))  # never the real machine's
        self.events.clear()
        sb.start()
        self.assertEqual(sb.greeting(), "我是总机，要去哪个项目？")
        self.assertEqual(self.notices().count("我是总机，要去哪个项目？"), 1)
        r = sb.say(MANAGER, "接我到 Atlas")
        self.assertTrue(r["ok"])
        self.assertEqual(slow[0].sent, ["接我到 Atlas"])  # written to stdin immediately
        slow[0].line.on_session("ready", {"session_id": "m"})
        self.assertEqual(self.notices().count("我是总机，要去哪个项目？"), 1)  # no second greeting on init

    def test_switch_to_project_resolves_and_announces(self):
        r = self.sb.switch_to_project("接我到 服务器")
        self.assertTrue(r["ok"], r)
        line = self.sb.active()
        self.assertEqual((line.kind, line.name), ("project", "Atlas"))
        self.assertEqual(line.session.resume, "old-atlas")  # resumes the voice app's own last session
        self.assertIn("接到Atlas项目", self.notices())
        self.assertEqual(self.sb.switch_to_project("服务器")["line"], line.id)  # same line again
        self.assertEqual(len([l for l in self.sb.lines.values() if l.kind == "project"]), 1)

    def test_manager_switch_to_asks_first(self):
        results = {}

        def worker():
            results["r"] = self.sb.control_request(MANAGER, "switch_to", {"project": "服务器"})
        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(self._wait(lambda: any(k == "ask" for k, d in self.events)))
        ask = [d for k, d in self.events if k == "ask"][0]
        self.assertEqual((ask["question"], ask["line"], ask["background"]), ("要接到Atlas项目吗？", MANAGER, False))
        self.assertEqual(self.sb.active_id, MANAGER)  # nothing moved yet
        self.sb.answer(ask["request"], False)
        t.join(2)
        self.assertFalse(results["r"]["ok"])
        self.assertIn("没有同意", results["r"]["message"])
        self.assertEqual(self.sb.active_id, MANAGER)
        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(self._wait(lambda: len([d for k, d in self.events if k == "ask"]) == 2))
        self.sb.answer([d for k, d in self.events if k == "ask"][1]["request"], True)
        t.join(2)
        self.assertTrue(results["r"]["ok"])
        self.assertEqual(self.sb.active().name, "Atlas")
        self.assertIn("接到Atlas项目", self.notices())

    def test_ambiguous_and_unknown_project(self):
        self.projects.append(project("Atlas", str(self.tmp / "beacon"), key="other"))
        self.projects[-1].display_name = "Atlas（D 盘）"
        self.projects[0].display_name = "Atlas（E 盘）"
        self.sb.refresh_index()
        r = self.sb.switch_to_project("Atlas")
        self.assertFalse(r["ok"])
        self.assertIn("你指哪个", r["message"])
        self.assertEqual(len(r["candidates"]), 2)
        r = self.sb.switch_to_project("火星")
        self.assertFalse(r["ok"])
        self.assertIn("没有找到", r["message"])
        self.assertEqual(self.sb.active_id, MANAGER)

    def test_phrases(self):
        self.sb.switch_to_project("Beacon")
        line = self.sb.active()
        self.assertEqual(self.sb.phrase("background_mode")["spoken"], "现在没有在干活的线路。")
        self.sb.say(line.id, "干活")
        self.assertEqual(line.state, WORKING)
        r = self.sb.phrase("background_mode")
        self.assertIn("在后台继续", r["spoken"])
        self.assertEqual(self.sb.active_id, MANAGER)
        self.assertEqual(line.state, WORKING)  # not interrupted
        self.assertEqual(self.sb.phrase("scratch")["ok"], True)
        self.assertEqual(self.sb.active_id, SCRATCH)
        self.assertEqual(self.sb.phrase("back_to_manager")["spoken"], "")  # announced through a notice instead
        self.assertIn("好，回到总机。", self.notices())
        r = self.sb.phrase("shutdown_daemon")  # Beacon is mid-turn: asked out loud, not cut off
        self.assertFalse(r.get("shutdown"))
        self.assertTrue(self._wait(lambda: any(k == "ask" and d["action"] == "shutdown"
                                               and d["question"] == "Beacon还在做事，要关掉后台服务吗？" for k, d in self.events)))
        self.assertTrue(self._wait(lambda: "没有关掉后台服务。" in self.notices()))  # nobody answered: it stays up
        self.assertIn("Beacon在干活", self.sb.phrase("background_status")["spoken"])

    def test_return_summary_after_background_work(self):
        self.sb.switch_to_project("Beacon")
        line = self.sb.active()
        self.sb.say(line.id, "干活")
        self.sb.phrase("back_to_manager")
        line.on_session("text", "都跑完了。")
        line.on_session("done", {"status": "ok", "message": "都跑完了。"})
        self.assertNotIn("Beacon问你", "".join(self.notices()))  # a plain report waits in the mailbox
        self.sb.switch_to_project("Beacon")
        self.assertIn("你不在的时候，Beacon说：都跑完了", self.notices())
        self.assertEqual(line.unread(), [])

    def test_background_line_question_interrupts(self):
        self.sb.switch_to_project("Beacon")
        line = self.sb.active()
        self.sb.say(line.id, "干活")
        self.sb.phrase("back_to_manager")
        line.on_session("done", {"status": "ok", "message": "要不要继续？"})
        self.assertIn("Beacon问你：要不要继续", self.notices())

    def test_eviction_exceptions(self):
        self.sb.switch_to_project("Atlas")
        atlas = self.sb.active()
        self.sb.say(atlas.id, "干活")  # busy: must survive
        self.sb.switch_to_project("Beacon")
        tp = self.sb.active()
        self.clock[0] += 5
        r = self.sb.switch_to_project("benchmarking")  # max_lines = 2: someone has to go
        self.assertTrue(r["ok"])
        self.assertNotEqual(atlas.state, "closed")
        self.assertEqual(tp.state, "closed")  # idle and not active -> evicted
        self.clock[0] += 11 * 60
        self.sb.tick()
        self.assertNotEqual(atlas.state, "closed")  # still working
        atlas.on_session("done", {"status": "ok", "message": "好了。"})
        self.sb.tick()
        self.assertNotEqual(atlas.state, "closed")  # unread mail protects it
        atlas.mark_read()
        self.sb.tick()
        self.assertEqual(atlas.state, "closed")

    def test_ask_queue_one_at_a_time_and_answer(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        results = {}

        def worker(name):
            results[name] = self.sb.control_request(line.id, "ask", {"action": "open_app", "question": f"{name}，可以吗？"})
        t1 = threading.Thread(target=worker, args=("甲",))
        t1.start()
        self.assertTrue(self._wait(lambda: any(k == "ask" for k, d in self.events)))
        t2 = threading.Thread(target=worker, args=("乙",))
        t2.start()
        asks = [d for k, d in self.events if k == "ask"]
        self.assertEqual(len(asks), 1)  # 乙 waits until 甲 is answered
        self.sb.answer(asks[0]["request"], True)
        t1.join(2)
        self.assertTrue(results["甲"]["ok"])
        self.assertTrue(self._wait(lambda: len([d for k, d in self.events if k == "ask"]) == 2))
        second = [d for k, d in self.events if k == "ask"][1]
        self.sb.answer(second["request"], False)
        t2.join(2)
        self.assertFalse(results["乙"]["ok"])
        self.assertIn("没有同意", results["乙"]["message"])

    def test_ask_without_frontend_times_out(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        r = self.sb.control_request(line.id, "ask", {"action": "click", "question": "点一下，可以吗？"})
        self.assertFalse(r["ok"])
        self.assertTrue(any(m["text"].startswith("申请没人回答") for m in line.mailbox))

    def test_desktop_action_from_background_line_refused(self):
        self.sb.switch_to_project("Atlas")
        atlas = self.sb.active()
        self.sb.phrase("back_to_manager")
        r = self.sb.control_request(atlas.id, "ask", {"action": "click", "question": "点一下，可以吗？"})
        self.assertFalse(r["ok"])
        self.assertIn("不在前台", r["message"])
        self.assertFalse(any(k == "ask" for k, d in self.events))
        r = self.sb.control_request(atlas.id, "ask", {"action": "batch", "actions": ["open_app", "type"], "question": "q"})
        self.assertIn("不在前台", r["message"])

    def test_table_allow_and_deny(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.cfg.permissions["scroll"] = "allow"
        self.cfg.permissions["screenshot"] = "deny"
        self.assertTrue(self.sb.control_request(line.id, "ask", {"action": "scroll", "question": "q"})["ok"])
        r = self.sb.control_request(line.id, "ask", {"action": "screenshot", "question": "q"})
        self.assertIn("禁止", r["message"])
        self.assertFalse(any(k == "ask" for k, d in self.events))

    def test_frontend_requests_are_forwarded(self):
        r = self.sb.control_request(MANAGER, "set_font_size", {"size": 14})
        self.assertEqual(r["message"], "前台做了")
        self.assertEqual(self.forwarded, [("set_font_size", {"size": 14})])

    def test_run_in_background_and_status(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.cfg.permissions["background_job"] = "allow"
        r = self.sb.control_request(line.id, "run_in_background", {"command": "echo ok", "note": "试一下"})
        self.assertTrue(r["ok"], r)
        jid = r["job"]
        self.assertTrue(self._wait(lambda: self.jobs.get(jid).state == "done", 10))
        self.sb.on_job_change(self.jobs.get(jid))
        self.assertIn("做完了", self.sb.control_request(line.id, "job_status", {"id": jid})["message"])
        self.assertIn("试一下", self.sb.background_status_text())
        self.assertFalse(self.sb.control_request(line.id, "run_in_background", {"command": ""})["ok"])

    def test_remember_alias_and_list(self):
        r = self.sb.control_request(MANAGER, "remember_alias", {"alias": "基准", "project": "benchmarking"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(Path(paths.PROJECTS_FILE).exists(), True)
        self.assertIn("Atlas", self.sb.control_request(MANAGER, "list_projects", {})["message"])

    def test_a_saved_codex_thread_is_not_given_to_claude_code(self):
        """The user's case: the project last ran on GPT (Codex); now the default is a Claude Code engine."""
        self.projects[0].session_id, self.projects[0].session_engine = "01a166ba-23fd-7603-bdf4-1df493071b07", "codex"
        self.sb.index = self.sb.scan_projects()
        self.sb.switch_to_project("Atlas")
        self.assertEqual(self.sb.active().session.resume, "")  # a new conversation, not claude --resume <codex id>
        self.sb.switch_to_project("Beacon")
        import json
        self.sb._close_line(self.sb.active(), "test")
        data = json.loads(paths.LINES_FILE.read_text("utf-8"))
        data[self.projects[1].key] = {"session_id": "3f2b6c1e-8d4a-4b7e-9c21-5a6d7e8f9012", "engine": "claude"}
        paths.LINES_FILE.write_text(json.dumps(data), "utf-8")
        self.sb.switch_to_project("Beacon")
        self.assertEqual(self.sb.active().session.resume, "3f2b6c1e-8d4a-4b7e-9c21-5a6d7e8f9012")  # its own engine's conversation is continued

    def test_saved_lines_record_the_engine_and_provider(self):
        self.sb.switch_to_project("Atlas")  # FakeSession reports ready with a session id
        import json
        rec = json.loads(paths.LINES_FILE.read_text("utf-8"))[self.projects[0].key]
        self.assertEqual((rec["engine"], rec["provider"]), ("claude", "claude"))

    def test_a_conversation_that_does_not_resume_starts_fresh_with_the_waiting_words(self):
        """claude --resume <id> says "No conversation found" and exits: forget the id, start a new conversation,
        give it the words the user said, and never leave the window on 思考中."""
        import json

        class NoResume(FakeSession):
            def start(self):
                self.calls.append("start")  # no "ready": a resumed process never accepted anything
        self.sb.session_factory = NoResume  # Atlas was saved with the conversation "old-atlas"
        paths.LINES_FILE.write_text(json.dumps({self.projects[0].key: {"session_id": "old-atlas"}}), "utf-8")
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.assertEqual(line.started_resume, "old-atlas")
        self.sb.say(line.id, "打开记事本")
        self.events.clear()
        line.session.resume_failed = True  # what claude's stderr said
        line.on_session("exit", 1)
        self.assertIn(("restart", None, None, True), line.session.calls)
        self.assertEqual(line.session.sent[-2:], ["打开记事本", "打开记事本"])  # sent again, to the new conversation
        self.assertIn("Atlas接不上上次的对话，对话重新开始。", self.notices())
        self.assertFalse(any(k == "done" for k, d in self.events))  # the turn goes on in the new conversation
        self.assertNotIn(self.projects[0].key, json.loads(paths.LINES_FILE.read_text("utf-8")))  # the id is forgotten
        self.assertEqual(line.started_resume, "")
        line.on_session("exit", 1)  # dies again: no second fresh start, the turn ends instead
        self.assertEqual(line.session.calls.count(("restart", None, None, True)), 1)
        self.assertTrue(any(k == "done" and d["status"] == "error" for k, d in self.events))

    def test_a_process_exit_mid_turn_ends_the_turn_and_the_next_words_reconnect(self):
        self.sb.switch_to_project("Beacon")  # no saved conversation
        line = self.sb.active()
        self.sb.say(line.id, "x")
        self.events.clear()
        line.on_session("exit", 1)
        self.assertEqual(line.state, switchboard.DEAD)
        self.assertIn({"line": line.id, "status": "error", "last": "程序退出了"}, [d for k, d in self.events if k == "done"])
        self.assertIn("Beacon的程序退出了，下次说话时会重新连接。", self.notices())
        self.assertNotIn(("restart", None, None, True), line.session.calls)  # nothing to resume: no fresh-start path
        r = self.sb.say(line.id, "y")
        self.assertTrue(r["ok"], r)
        self.assertIn(("restart", None), line.session.calls)  # continues its own conversation
        self.assertEqual(line.session.sent[-1], "y")
        self.assertIn("Beacon已重新连接", self.notices())
        self.sb.switch_to_project("Atlas")
        idle = self.sb.active()
        self.events.clear()
        idle.on_session("exit", 1)  # an idle line: no turn to end
        self.assertFalse(any(k == "done" for k, d in self.events))

    def _turn(self, line, text, n, files=()):
        """One user turn as the engine reports it: the prompt's uuid p<n>, the last entry a<n>, files written."""
        r = self.sb.say(line.id, text)
        line.on_session("mark", {"prompt": f"p{n}", "last": f"p{n}", "codex_turn": f"turn-{n}"})  # either engine's ids
        for f in files:
            line.on_session("tool", {"name": "Write", "input": {"file_path": f}})
        line.on_session("mark", {"last": f"a{n}"})
        line.on_session("done", {"status": "ok", "message": "好。"})
        return r["turn"]

    def test_rewind_by_steps_and_by_turn(self):
        self.projects[0].session_id = ""  # a new conversation: the record reaches its first sentence
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        t1, t2, t3 = self._turn(line, "一", 1), self._turn(line, "二", 2), self._turn(line, "打开记事本输入hello然后另存为", 3)
        self.assertTrue(t1.startswith("t") and len({t1, t2, t3}) == 3)  # say's reply carries a stable id per message
        self.events.clear()
        r = self.sb.rewind(line.id, steps=1)
        self.assertEqual(r, {"ok": True, "message": "撤回到「打开记事本输入hello……」之前。", "text": "打开记事本输入hello然后另存为",
                             "files_restored": 0})
        self.assertIn(("rewind_to", "a2", "p3", True), line.session.calls)  # keep turn 2's last entry, drop turn 3's prompt
        self.assertIn(("rewound", {"line": line.id, "turn": t3, "text": "打开记事本输入hello然后另存为"}), self.events)
        self.assertEqual([e["turn"] for e in line.turn_log], [t1, t2])
        r = self.sb.rewind("", turn=t1)  # "" = the active line; the first turn: nothing to keep, a new conversation
        self.assertEqual((r["ok"], r["text"]), (True, "一"))
        self.assertIn(("restart", None, None, True), line.session.calls)
        self.assertEqual(line.turn_log, [])
        self.assertEqual(self.sb.rewind(line.id)["message"], "没有可以撤回的话。")
        self.assertFalse(self.sb.rewind(line.id, turn="t-unknown")["ok"])
        t4, t5 = self._turn(line, "四", 4), self._turn(line, "五", 5)
        r = self.sb.rewind(line.id, steps=5)  # more steps than turns: back to the start
        self.assertEqual((r["ok"], r["text"]), (True, "四"))

    def test_rewind_over_two_turns_then_one_more(self):
        """The end-to-end case: 记住暗号：苹果 / write a file / 暗号改成：香蕉, rewound to before the second. Two turns go,
        so the guard that checks for ONE dropped turn is not asked for; afterwards another rewind still works."""
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        t1 = self._turn(line, "记住暗号：苹果", 1)
        t2 = self._turn(line, "用 Write 工具写 note.txt", 2)
        self._turn(line, "暗号改成：香蕉", 3)
        r = self.sb.rewind(line.id, turn=t2)
        self.assertTrue(r["ok"], r)
        self.assertIn(("rewind_to", "a1", "p2", False), line.session.calls)  # more than one turn: no drops-turn guard
        self.assertEqual([e["turn"] for e in line.turn_log], [t1])
        self._turn(line, "刚才的暗号是什么？", 4)
        r = self.sb.rewind(line.id, steps=1)  # the turn record is consistent: the next rewind finds its place
        self.assertTrue(r["ok"], r)
        self.assertIn(("rewind_to", "a1", "p4", True), line.session.calls)

    def test_a_refused_rewind_leaves_the_conversation_and_the_record_as_they_were(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self._turn(line, "一", 1)
        self._turn(line, "二", 2)
        before = [e["turn"] for e in line.turn_log]
        line.session.refuse_rewind = True
        self.events.clear()
        r = self.sb.rewind(line.id)
        self.assertEqual((r["ok"], r["message"]), (False, "撤回没成功：Claude Code 不接受这次撤回（那几轮里有没看到的内容）。对话保持原样。"))
        self.assertEqual([e["turn"] for e in line.turn_log], before)
        self.assertFalse(any(k == "rewound" for k, d in self.events))
        line.session.refuse_rewind = False
        self.assertTrue(self.sb.rewind(line.id)["ok"])  # nothing broken: the next one works

    def test_rewind_waits_while_a_line_is_answering(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self._turn(line, "一", 1)
        self.sb.say(line.id, "二")  # still answering
        r = self.sb.rewind(line.id)
        self.assertEqual((r["ok"], r["message"]), (False, "正在回答，先打断再撤回。"))
        self.assertFalse([c for c in line.session.calls if c[0:1] == ("rewind_to",)])

    def test_rewind_asks_once_about_changed_files(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        for n in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt"):
            (Path(line.cwd) / n).write_text("old", "utf-8")  # edited, not created, by the turns below
        self._turn(line, "一", 1)
        self._turn(line, "写两个文件", 2, files=["a.txt", "b.txt"])
        self._turn(line, "再改一个", 3, files=["a.txt"])

        def answer():
            self.assertTrue(self._wait(lambda: any(k == "ask" for k, d in self.events)))
            self.sb.answer([d for k, d in self.events if k == "ask"][-1]["request"], True)
        self.events.clear()
        threading.Thread(target=answer, daemon=True).start()
        r = self.sb.rewind(line.id, steps=2)
        asks = [d for k, d in self.events if k == "ask"]
        self.assertEqual([(a["action"], a["question"]) for a in asks], [("rewind_files", "这几轮改过 a.txt、b.txt，要一起改回去吗？")])
        a, b = (os.path.join(line.cwd, n) for n in ("a.txt", "b.txt"))  # as written, not lowercased
        self.assertEqual(asks[0]["detail"], f"{a}\n{b}")  # the card lists the files
        self.assertEqual(line.session.calls[-4:], [("rewind_files", "p2", "dry"), ("rewind_files", "p2"),  # dry run first
                                                   ("rewind_files", "p2", "dry"),  # and after: what still differs
                                                   ("rewind_to", "a1", "p2", False)])
        self.assertEqual((r["files_restored"], r["message"]), (2, "撤回到「写两个文件」之前。a.txt、b.txt已改回去。用命令改的文件不在内。"))
        self._turn(line, "再写", 4, files=["c.txt"])
        r = self.sb.rewind(line.id)  # nobody answers: the files stay
        self.assertEqual((r["files_restored"], r["message"]), (0, "撤回到「再写」之前。文件保持现在的样子。"))
        self.assertEqual(line.session.calls.count(("rewind_files", "p2")), 1)
        # Claude Code's own list is the authority: it restores every file it tracks, kept turns' edits included
        self._turn(line, "改 c", 6, files=["c.txt"])
        kept = os.path.join(line.cwd, "config.py")
        line.session.dry_files = [os.path.join(line.cwd, "c.txt"), kept, os.path.join(line.cwd, "x.txt")]
        self.events.clear()
        self.sb.rewind(line.id)
        q = [d for k, d in self.events if k == "ask"][0]
        self.assertEqual(q["question"], "要把 3 个文件改回去吗？其中 2 个不只是这几轮改的，会被改回那时的样子，那时还没有的会被删掉。")
        self.assertIn(kept + "（不只是这几轮改的）", q["detail"])
        line.session.dry_files = []
        self._turn(line, "没改文件的一句", 7, files=["e.txt"])
        self.events.clear()
        r = self.sb.rewind(line.id)  # Claude Code says nothing would change: no question
        self.assertFalse(any(k == "ask" for k, d in self.events))
        line.session.dry_files = None
        line.engine = "codex"  # Codex: the conversation only, no question
        self._turn(line, "改", 5, files=["d.txt"])
        self.events.clear()
        r = self.sb.rewind(line.id)
        self.assertEqual(r["message"], "撤回到「改」之前。改过的 1 个文件没有改回去（这条线只能撤回对话）。")
        self.assertFalse(any(k == "ask" for k, d in self.events))

    def test_a_resumed_line_never_loses_its_earlier_conversation(self):
        """Yesterday's conversation resumed, one sentence today, 撤回上一句: back to where yesterday ended, not a new
        conversation; without that point, refused."""
        self.sb.transcript_tail = lambda sid: "y-last" if sid == "old-atlas" else ""
        self.sb.switch_to_project("Atlas")  # resumes "old-atlas"
        line = self.sb.active()
        self.assertEqual((line.log_from_start, line.base_point), (False, {"last": "y-last"}))
        self._turn(line, "把 README 标题改一下", 1)
        r = self.sb.rewind(line.id)
        self.assertTrue(r["ok"], r)
        self.assertIn(("rewind_to", "y-last", "p1", True), line.session.calls)
        self.assertNotIn(("restart", None, None, True), line.session.calls)  # no fresh start, nothing forgotten
        self._turn(line, "又一句", 2)
        line.base_point = None
        r = self.sb.rewind(line.id)
        self.assertEqual((r["ok"], r["message"]), (False, "这条线是接着以前的对话开的，撤回不到这次接上后的第一句之前。"))
        self.assertEqual(len(line.turn_log), 1)

    def test_a_rewind_is_alone_on_its_line(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self._turn(line, "一", 1)
        self._turn(line, "二", 2, files=["a.txt"])
        said = {}

        def answer_after_speaking():
            self.assertTrue(self._wait(lambda: any(k == "ask" for k, d in self.events)))
            said["say"] = self.sb.say(line.id, "趁撤回时说的话")  # the window refuses too; the service must as well
            said["rewind"] = self.sb.rewind(line.id)
            self.sb.answer([d for k, d in self.events if k == "ask"][-1]["request"], False)
        self.events.clear()
        threading.Thread(target=answer_after_speaking, daemon=True).start()
        r = self.sb.rewind(line.id)
        self.assertTrue(r["ok"], r)
        self.assertEqual((said["say"]["message"], said["rewind"]["message"]), ("正在撤回，稍等。", "正在撤回，稍等。"))
        self.assertTrue(self.sb.say(line.id, "现在可以了")["ok"])

    def test_a_pending_rewind_survives_closing_the_line(self):
        """A Claude Code rewind is made real only by the next message; closing first must not bring the dropped turns
        back: lines.json keeps the fork point, and the line opened again goes on from it."""
        import json
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        line.session.session_id, line.session_id = "S", "S"
        line.session.fork_at = ("a1", "p2")
        self.sb._save_lines()
        rec = json.loads(paths.LINES_FILE.read_text("utf-8"))[self.projects[0].key]
        self.assertEqual((rec["session_id"], rec["fork_at"]), ("S", ["a1", "p2"]))
        from voiceclaude import projects as projmod
        self.assertEqual(projmod.scan(self.tmp / "none", paths.PROJECTS_FILE, paths.LINES_FILE, exists=lambda p: True), [])
        self.sb._close_line(line, "test")  # the start-up index still says old-atlas without a fork: lines.json wins
        self.sb.switch_to_project("Atlas")
        again = self.sb.active()
        self.assertEqual((again.resume_fork, again.base_point), (["a1", "p2"], {"last": "a1"}))

    def test_the_question_says_what_is_deleted_and_what_was_edited_since(self):
        """Claude created notes.md and the user then typed into it; Claude edited plan.md and the user edited it
        afterwards: the question says both, the card marks them."""
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        cwd = Path(line.cwd)
        (cwd / "plan.md").write_text("v1", "utf-8")
        self._turn(line, "一", 1)
        r = self.sb.say(line.id, "写笔记、改计划")
        line.on_session("mark", {"prompt": "p2", "last": "p2"})
        line.on_session("tool", {"name": "Write", "input": {"file_path": "notes.md"}})  # does not exist yet: created
        (cwd / "notes.md").write_text("Claude 写的", "utf-8")
        line.on_session("tool", {"name": "Edit", "input": {"file_path": "plan.md"}})
        (cwd / "plan.md").write_text("v2 by Claude", "utf-8")
        line.on_session("mark", {"last": "a2"})
        line.on_session("done", {"status": "ok", "message": "好。"})  # what the files look like after the turn
        (cwd / "plan.md").write_text("v3 by the user", "utf-8")  # the user's own edit afterwards
        self.events.clear()
        self.sb.rewind(line.id)
        q = [d for k, d in self.events if k == "ask"][0]
        self.assertEqual(q["question"], "要把 notes.md、plan.md改回去吗？notes.md是这几轮新建的，会被删掉；plan.md之后又被改过，那些改动也会没了。")
        self.assertIn("notes.md（会被删掉）", q["detail"])
        self.assertIn("plan.md（之后又被改过）", q["detail"])

    def test_shutting_down_while_a_line_works_needs_a_yes(self):
        closed = []
        self.sb.shutdown_hook = lambda: closed.append(1)
        self.assertTrue(self.sb.request_shutdown().get("shutdown"))  # nothing running: at once, as before
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.sb.say(line.id, "重构一下")

        def yes():
            self.assertTrue(self._wait(lambda: any(k == "ask" and d["action"] == "shutdown" for k, d in self.events)))
            self.sb.answer([d for k, d in self.events if k == "ask"][-1]["request"], True)
        threading.Thread(target=yes, daemon=True).start()
        self.assertFalse(self.sb.request_shutdown().get("shutdown"))
        self.assertTrue(self._wait(lambda: closed == [1]))
        self.assertTrue(self.sb.request_shutdown(jobs="keep").get("shutdown"))  # the window's own quit: it asked already

    def test_the_windows_own_quit_after_its_own_dialog(self):
        """× / 退出 voice-claude: the window already asked about the busy line; the service must not ask again (the
        window would wait for a bye that never comes)."""
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.sb.say(line.id, "重构一下")
        self.events.clear()
        self.assertTrue(self.sb.request_shutdown(jobs="ask", lines_confirmed=True).get("shutdown"))
        self.assertFalse(any(k == "ask" for k, d in self.events))
        from voiceclaude import client as clientmod
        c = clientmod.DaemonClient(lambda k, d: None)
        with mock.patch.object(c, "_call", lambda cmd, **kw: kw):
            self.assertEqual(c.shutdown("ask", lines_confirmed=True), {"jobs": "ask", "lines_confirmed": True})
            # say waits as long as the service may (a Codex thread up to 90 s): never "没发出去" while it still goes out
            from voiceclaude import engine_codex
            self.assertGreater(c.say("p-1", "hi")["timeout"], engine_codex.READY_TIMEOUT)

    def test_a_service_question_leaves_the_line_alone(self):
        """The shutdown question shows on the working line's card, but its turn ending meanwhile is kept, and no answer
        does not put '申请没人回答' into that project's mailbox."""
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.sb.say(line.id, "重构一下")

        def turn_ends_while_asked():
            self.assertTrue(self._wait(lambda: any(k == "ask" and d["action"] == "shutdown" for k, d in self.events)))
            self.assertEqual(line.state, switchboard.WORKING)  # not flipped to WAITING by the service's question
            line.on_session("done", {"status": "ok", "message": "重构完了。"})
        threading.Thread(target=turn_ends_while_asked, daemon=True).start()
        self.sb.request_shutdown()
        self.assertTrue(self._wait(lambda: "没有关掉后台服务。" in self.notices()))
        self.assertEqual(line.state, switchboard.IDLE)
        self.assertFalse(any("申请没人回答" in m["text"] for m in line.mailbox))

    def test_a_codex_exit_keeps_its_saved_thread(self):
        """Codex falls back from a failed thread/resume by itself; an early exit is not that, so the thread is kept."""
        import json
        paths.LINES_FILE.write_text(json.dumps({self.projects[0].key: {"session_id": "old-atlas", "engine": "codex"}}), "utf-8")
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        line.engine, line.ready_seen = "codex", False
        line.on_session("exit", 1)
        self.assertNotIn(("restart", None, None, True), line.session.calls)
        self.assertIn(self.projects[0].key, json.loads(paths.LINES_FILE.read_text("utf-8")))

    def test_no_double_send_when_the_fresh_start_comes_while_a_send_waits(self):
        """A Codex sentence waiting for its thread when the app-server dies: the fresh thread gets it once (the waiting
        send goes there itself), and its record entry is kept."""
        self.sb.switch_to_project("Atlas")  # resumed from old-atlas
        line = self.sb.active()
        self.assertEqual(line.started_resume, "old-atlas")
        sent_before = len(line.session.sent)
        line.say("第一句", "t1")
        line.sending = True  # still inside CodexSession.send, waiting on ready
        line.ready_seen = False  # the thread never came up
        line.session.resume_failed = True
        line.on_session("exit", 1)
        self.assertIn(("restart", None, None, True), line.session.calls)
        self.assertEqual(line.session.sent[sent_before:], ["第一句"])  # not sent a second time
        self.assertEqual(([e["turn"] for e in line.turn_log], line.state), (["t1"], switchboard.WORKING))

    def test_lines_json_survives_saves_from_many_threads(self):
        """A Codex rewind saved from two threads at once and left lines.json unparseable (every other project then lost
        its conversation). One lock and an atomic write: always whole, other projects' records kept."""
        import json
        paths.LINES_FILE.write_text(json.dumps({"c:\\other": {"session_id": "keep-me", "engine": "claude"}}), "utf-8")
        self.sb.switch_to_project("Atlas")
        self.sb.switch_to_project("Beacon")
        errors = []

        def hammer(n):
            try:
                for _ in range(40):
                    self.sb._save_lines()
                    if n == 0:
                        self.sb._forget_session(self.sb.lines[self.sb.active_id])
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=hammer, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        data = json.loads(paths.LINES_FILE.read_text("utf-8"))  # never half a file
        self.assertEqual((errors, data["c:\\other"]["session_id"]), ([], "keep-me"))
        self.assertFalse(list(paths.LINES_FILE.parent.glob("lines.json.*.tmp")))  # no temp files left behind

    def test_a_sentence_said_while_a_codex_thread_resumes_can_still_be_rewound(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        line.engine = "codex"
        line.log_from_start, line.base_point, line.turn_log = False, None, []
        self.sb.say(line.id, "改一下 README")  # said during thread/resume: no Codex turn for it yet
        line.on_session("ready", {"session_id": "thread-old", "base_turn": "turn-9"})
        self.assertEqual(line.base_point, {"codex_turn": "turn-9"})
        line.on_session("mark", {"prompt": "p1", "last": "p1", "codex_turn": "turn-10"})
        line.on_session("done", {"status": "ok", "message": "好。"})
        self.assertTrue(self.sb.rewind(line.id)["ok"])
        self.assertIn(("rewind_to", "turn-9", "p1", True), line.session.calls)

    def test_a_partly_failed_restore_says_what_was_not_put_back(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        for n in ("a.py", "b.py"):
            (Path(line.cwd) / n).write_text("x", "utf-8")
        self._turn(line, "一", 1)
        self._turn(line, "改两个", 2, files=["a.py", "B.py"])
        line.session.still_after = [os.path.join(line.cwd, "B.py")]  # its backup was missing: it still differs

        def yes():
            self.assertTrue(self._wait(lambda: any(k == "ask" for k, d in self.events)))
            self.sb.answer([d for k, d in self.events if k == "ask"][-1]["request"], True)
        self.events.clear()
        threading.Thread(target=yes, daemon=True).start()
        r = self.sb.rewind(line.id)
        self.assertEqual(r["files_restored"], 1)
        self.assertEqual(r["message"], "撤回到「改两个」之前。a.py已改回去。1 个没能改回去。用命令改的文件不在内。")
        q = [d for k, d in self.events if k == "ask"][0]
        self.assertIn("B.py", q["question"])  # its own spelling, not lowercased

    def test_a_line_waiting_for_the_user_can_be_rewound(self):
        """The last reply ended in a question (needs_you: WAITING). The files question must not leave it WORKING."""
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        (Path(line.cwd) / "README.md").write_text("x", "utf-8")
        self._turn(line, "一", 1)
        self.sb.say(line.id, "改 README")
        line.on_session("mark", {"prompt": "p2", "last": "p2"})
        line.on_session("tool", {"name": "Edit", "input": {"file_path": "README.md"}})
        line.on_session("mark", {"last": "a2"})
        line.on_session("done", {"status": "ok", "message": "改好了，要不要也改英文版？"})
        self.assertEqual(line.state, switchboard.WAITING)
        r = self.sb.rewind(line.id)  # nobody answers the files question
        self.assertTrue(r["ok"], r)
        self.assertNotEqual(line.state, switchboard.WORKING)

    def test_words_the_session_refuses_leave_nothing_behind(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self._turn(line, "一", 1)
        line.pending_note = "（说明：……）"
        before = (line.state, [e["turn"] for e in line.turn_log])
        with mock.patch.object(line.session, "send", side_effect=RuntimeError("Claude 正在重启，请稍后再说一遍")):
            r = self.sb.say(line.id, "二")
        self.assertFalse(r["ok"])
        self.assertEqual((line.state, [e["turn"] for e in line.turn_log]), before)  # no orphan entry, not WORKING
        self.assertEqual(line.pending_note, "（说明：……）")  # still to be said with the next words that do go through

    def test_a_stale_note_is_never_said_after_the_conversation_moved_on(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self._turn(line, "一", 1)
        self._turn(line, "二", 2)
        line.pending_note = "（说明：a.txt已改回到「二」之前的样子，但对话没有撤回。）"
        self.assertTrue(self.sb.rewind(line.id)["ok"])  # this time the rewind worked
        self.assertEqual(line.pending_note, "")
        self.sb.say(line.id, "继续")
        self.assertEqual(line.session.sent[-1], "继续")

    def test_reopening_a_project_in_the_same_run_follows_lines_json(self):
        """Rewind, switch away, the line is closed (idle or to make room), reopen: the pending fork is continued, not the
        whole conversation from the start-up index."""
        self.sb.transcript_tail = lambda sid: "tail"
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self._turn(line, "一", 1)
        self._turn(line, "二", 2)
        line.session.session_id = line.session_id
        line.session.fork_at = None

        def rewind_to(keep, drop, single=True):
            line.session.fork_at = (keep["last"], drop["prompt"] if single else None)
        with mock.patch.object(line.session, "rewind_to", rewind_to):
            self.assertTrue(self.sb.rewind(line.id)["ok"])  # saved right away, not at the next tick
        import json
        rec = json.loads(paths.LINES_FILE.read_text("utf-8"))[self.projects[0].key]
        self.assertEqual(rec["fork_at"], ["a1", "p2"])
        self.sb._close_line(line, "太多线路了")
        self.sb.switch_to_project("Atlas")
        again = self.sb.active()
        self.assertEqual((again.session.resume, again.resume_fork), (line.session_id, ["a1", "p2"]))

    def test_a_resumed_codex_line_rewinds_its_first_sentence_after_the_threads_last_turn(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        line.engine = "codex"
        line.log_from_start, line.base_point, line.turn_log = False, None, []
        line.on_session("ready", {"session_id": "thread-old", "base_turn": "turn-9"})  # what thread/resume said
        self.assertEqual(line.base_point, {"codex_turn": "turn-9"})
        self._turn(line, "改一下 README", 1)
        self.assertTrue(self.sb.rewind(line.id)["ok"])
        self.assertIn(("rewind_to", "turn-9", "p1", True), line.session.calls)

    def test_a_refused_rewind_after_restoring_files_says_so_and_tells_the_model(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self._turn(line, "一", 1)
        self._turn(line, "写文件", 2, files=["a.txt"])
        line.session.refuse_rewind = True

        def yes():
            self.assertTrue(self._wait(lambda: any(k == "ask" for k, d in self.events)))
            self.sb.answer([d for k, d in self.events if k == "ask"][-1]["request"], True)
        self.events.clear()
        threading.Thread(target=yes, daemon=True).start()
        r = self.sb.rewind(line.id)
        self.assertEqual((r["ok"], r["files_restored"]), (False, 1))
        self.assertTrue(r["message"].startswith("文件已改回去，但对话没撤回："), r["message"])
        self.sb.say(line.id, "继续")
        self.assertTrue(line.session.sent[-1].startswith("（说明：a.txt已改回到「写文件」之前的样子，但对话没有撤回。）\n继续"))
        self.assertEqual(line.turn_log[-1]["text"], "继续")  # the record keeps the user's own words

    def test_rewind_by_voice_and_a_refused_truncation(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self._turn(line, "一", 1)
        t2 = self._turn(line, "二", 2)
        r = self.sb.phrase("rewind_last")  # 撤回上一句
        self.assertEqual((r["ok"], r["spoken"], r["text"]), (True, "撤回到「二」之前。", "二"))
        self.assertEqual([e["text"] for e in line.turn_log], ["一"])
        line.on_session("rewind_failed", {})  # Claude Code refused the truncated conversation: as it was (no files)
        self.assertEqual([e["turn"] for e in line.turn_log][-1], t2)
        self.assertIn("Atlas撤回没成功（那几轮里有没看到的内容），对话保持原样。", self.notices())
        self.sb.switch(MANAGER)
        m = self.sb.lines[MANAGER]
        self._turn(m, "接我到 Atlas", 9)
        self.assertTrue(self.sb.phrase("rewind_last")["ok"])  # the manager line by the same rules

    def test_stop_all_kills_stuck_lines(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.sb.say(line.id, "干活")
        r = self.sb.stop_all()
        self.assertEqual(r["killed"], ["Atlas"])
        self.assertIn("kill", line.session.calls)
        self.assertIn("已紧急停止", "".join(self.notices()))

    def test_stop_all_also_stops_a_line_waiting_for_an_approval(self):
        """The brake is most likely pressed while a line is asking: its request is answered no AND its turn stops."""
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.sb.say(line.id, "干活")
        line.session.turn_active = True
        asked = threading.Thread(target=lambda: self.sb.control_request(line.id, "run_in_background", {"command": "echo x"}),
                                 daemon=True)
        asked.start()
        self.assertTrue(self._wait(lambda: line.state == switchboard.WAITING))
        self.sb.stop_all()
        asked.join(5)
        self.assertIn("interrupt", line.session.calls)
        self.assertFalse(self.jobs.running())  # the cancelled request ran nothing

    def test_a_request_queued_behind_the_brake_is_never_asked(self):
        """Another line's request waiting its turn when the brake is pulled: refused, never spoken afterwards."""
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        results = []
        first = threading.Thread(target=lambda: results.append(self.sb.ask_frontend(line.id, "background_job", "第一个？")), daemon=True)
        first.start()
        self.assertTrue(self._wait(lambda: any(k == "ask" for k, d in self.events)))
        second = threading.Thread(target=lambda: results.append(self.sb.ask_frontend(MANAGER, "switch_provider", "第二个？")), daemon=True)
        second.start()
        threading.Event().wait(0.05)  # the second one is now waiting for the first to finish
        self.sb.stop_all()
        first.join(5)
        second.join(5)
        self.assertEqual(results, [False, False])
        self.assertEqual([d["question"] for k, d in self.events if k == "ask"], ["第一个？"])
        self.assertEqual(self.sb.control_request(line.id, "brake", {})["brake"], 1)  # what run_steps checks between steps

    def test_voice_tools_reach_the_front_end(self):
        for path, data in (("list_voices", {}), ("set_voice", {"name": "小山"}), ("set_speed", {"speed": 1.1})):
            r = self.sb.control_request("", path, data)
            self.assertEqual(r["message"], "前台做了", path)
        self.assertEqual([p for p, d in self.forwarded][-3:], ["list_voices", "set_voice", "set_speed"])

    def test_endless_job_is_flagged_in_the_question(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        threading.Thread(target=lambda: self.sb.control_request(line.id, "run_in_background", {"command": "ping -t 127.0.0.1"}),
                         daemon=True).start()
        self.assertTrue(self._wait(lambda: any(k == "ask" for k, d in self.events)))
        q = [d for k, d in self.events if k == "ask"][0]["question"]
        self.assertIn("不会自己结束", q)
        self.sb.answer([d for k, d in self.events if k == "ask"][0]["request"], False)

    def test_shutdown_asks_about_running_jobs(self):
        hooked = []
        self.sb.shutdown_hook = lambda: hooked.append(1)
        self.assertEqual(self.sb.phrase("shutdown_daemon"), {"ok": True, "spoken": "好，关掉后台服务。", "shutdown": True})
        job = self.jobs.start("ping -n 30 127.0.0.1 >nul", str(self.tmp), "l1", "慢活")
        r = self.sb.phrase("shutdown_daemon")
        self.assertFalse(r["shutdown"])
        self.assertTrue(self._wait(lambda: any(k == "ask" and d["action"] == "stop_jobs" for k, d in self.events)))
        ask = [d for k, d in self.events if k == "ask" and d["action"] == "stop_jobs"][0]
        self.assertIn("慢活", ask["question"])
        self.sb.answer(ask["request"], True)
        self.assertTrue(self._wait(lambda: hooked == [1]))
        self.assertEqual(self.jobs.get(job.id).state, "cancelled")

    def test_shutdown_kills_jobs_when_nobody_answers(self):
        hooked = []
        self.sb.shutdown_hook = lambda: hooked.append(1)
        job = self.jobs.start("ping -n 30 127.0.0.1 >nul", str(self.tmp), "l1", "慢活")
        self.sb.phrase("shutdown_daemon")
        self.assertTrue(self._wait(lambda: hooked == [1], timeout=3))  # ask_timeout is 0.3 s in these tests
        self.assertEqual(self.jobs.get(job.id).state, "cancelled")

    def test_brake_keeps_jobs_when_nobody_answers(self):
        job = self.jobs.start("ping -n 30 127.0.0.1 >nul", str(self.tmp), "l1", "慢活")
        self.sb.stop_all()
        self.assertTrue(self._wait(lambda: any(k == "ask" and d["action"] == "stop_jobs" for k, d in self.events)))
        import time
        time.sleep(0.6)  # past the (test) ask timeout
        self.assertEqual(self.jobs.get(job.id).state, "running")
        self.jobs.cancel(job.id)

    def test_switch_to_fable_by_spoken_alias(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        for alias in ("飞 five", "Fable 5", "飞五", "claude-fable-5-1"):
            self.assertEqual(switchboard.normalize_model(alias), "fable", alias)
        self.assertIsNone(switchboard.normalize_model("gpt"))
        results = {}
        threading.Thread(target=lambda: results.update(r=self.sb.control_request(line.id, "switch_model", {"model": "飞 five"})),
                         daemon=True).start()
        self.assertTrue(self._wait(lambda: any(k == "ask" for k, d in self.events)))
        ask = [d for k, d in self.events if k == "ask"][0]
        self.assertIn("fable", ask["question"])
        self.assertIn("慢", ask["question"])
        self.sb.answer(ask["request"], True)
        self.assertTrue(self._wait(lambda: "r" in results))
        self.assertTrue(results["r"]["ok"], results["r"])
        self.assertEqual(line.pending_model, "claude-fable-5-1")
        self.assertEqual(self.cfg.model, "claude-fable-5-1")
        line.session.model = "claude-fable-5-1"  # as it is once the turn ends and the line restarts
        self.assertIn("现在用的就是 fable", self.sb.control_request(line.id, "switch_model", {"model": "飞宝"})["message"])
        self.assertIn("只能是", self.sb.control_request(line.id, "switch_model", {"model": "gpt"})["message"])

    def test_switch_model_on_the_manager_changes_project_lines_not_the_manager(self):
        self.sb.switch_to_project("Atlas")
        atlas = self.sb.active()
        self.sb.phrase("back_to_manager")
        self.cfg.permissions["switch_model"] = "allow"
        r = self.sb.control_request(MANAGER, "switch_model", {"model": "fable"})
        self.assertTrue(r["ok"], r)
        self.assertIn("总机自己不换", r["message"])
        self.assertEqual(self.cfg.model, "claude-fable-5-1")
        self.assertEqual(atlas.pending_model, "claude-fable-5-1")
        self.assertIsNone(getattr(self.sb.lines[MANAGER], "pending_model", None))
        self.sb.tick()  # idle project line restarts on the new model right away; the manager does not
        self.assertIn(("restart", "claude-fable-5-1"), atlas.session.calls)
        self.assertNotIn(("restart", "claude-fable-5-1"), self.sb.lines[MANAGER].session.calls)
        atlas.session.model = "claude-fable-5-1"
        self.assertIn("现在用的就是 fable", self.sb.control_request(MANAGER, "switch_model", {"model": "飞五"})["message"])

    def test_no_access_means_no_lines_until_a_key_arrives(self):
        from voiceclaude import access
        state = {"st": access.AccessStatus(False, "none", "没装 Claude Code，也没有 API key。")}
        sb = Switchboard(self.cfg, jobs=self.jobs, emit=lambda k, d: self.events.append((k, d)),
                         forward=lambda p, d: {"ok": True}, session_factory=FakeSession,
                         scan_projects=lambda: list(self.projects), now=lambda: self.clock[0], ask_timeout=0.3,
                         detect_access=lambda cmd: state["st"])
        self.events.clear()
        sb.start()
        from voiceclaude.managers import RulesSession
        self.assertEqual(set(sb.lines), {MANAGER})  # the manager is up on plain rules, no project lines
        self.assertIsInstance(sb.lines[MANAGER].session, RulesSession)
        self.assertTrue(sb.greeting().startswith("还没接入"))
        self.assertTrue(any(n.startswith("还没接入：没装 Claude Code") for n in self.notices()))
        self.assertFalse(sb.switch_to_project("Atlas")["ok"])  # no project line can start without access
        self.assertEqual(sb.status()["access"]["method"], "none")
        state["st"] = access.AccessStatus(True, "api_key", "用 API key（…mnop）接入。", claude="c", account="…mnop")
        with mock.patch.object(access, "set_api_key", lambda key: state["st"]):
            r = sb.set_api_key("sk-ant-api03-abcdefghijklmnop")
        self.assertTrue(r["ok"])
        self.assertNotIsInstance(sb.lines[MANAGER].session, RulesSession)  # the real manager took over
        self.assertEqual(sb.active_id, MANAGER)
        self.assertEqual(sb.greeting(), "我是总机，要去哪个项目？")
        self.assertTrue(sb.switch_to_project("Atlas")["ok"])

    def _deepseek_ok(self, sb=None):
        from voiceclaude import access

        def detect_provider(pid, cmd, cfg=None):
            if pid == "deepseek":
                return access.AccessStatus(True, "deepseek_key", "用 DeepSeek（deepseek-flash，key …1234）接入。", claude="c",
                                           account="…1234", provider="deepseek", provider_name="DeepSeek（深度求索）", model="deepseek-flash")
            return access.AccessStatus(False, "none", f"还没填 {pid} 的 API key。", provider=pid, provider_name=pid)
        (sb or self.sb).detect_provider = detect_provider

    def test_switch_provider_on_a_line_asks_then_switches_after_the_turn(self):
        self._deepseek_ok()
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.assertEqual(line.provider, "claude")
        self.assertIn("只能是", self.sb.control_request(line.id, "switch_provider", {"provider": "gemini"})["message"])
        self.assertIn("用不了", self.sb.control_request(line.id, "switch_provider", {"provider": "gpt"})["message"])
        self.cfg.permissions["switch_provider"] = "allow"
        r = self.sb.control_request(line.id, "switch_provider", {"provider": "DeepSeek"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(line.pending_provider, "deepseek")
        self.sb.say(line.id, "x")
        self.assertNotIn(("restart", None, "deepseek", False), line.session.calls)
        line.on_session("done", {"status": "ok", "message": "好。"})
        self.assertIn(("restart", None, "deepseek", False), line.session.calls)
        self.assertEqual(line.provider, "deepseek")
        self.assertEqual(self.sb.provider, "claude")  # the default is untouched
        self.assertTrue(any("已换到DeepSeek" in n for n in self.notices()))
        lines = {l["line"]: l for l in self.sb.status()["lines"]}
        self.assertEqual(lines[line.id]["provider"], "deepseek")
        self.assertIn("现在就在", self.sb.control_request(line.id, "switch_provider", {"provider": "deepseek"})["message"])
        # right after the switch an error ends the old conversation: the line starts afresh once and says so
        self.sb.say(line.id, "y")
        line.on_session("done", {"status": "error", "message": "400"})
        self.assertIn(("restart", None, None, True), line.session.calls)
        self.assertTrue(any("重新开始" in n for n in self.notices()))

    def test_switch_provider_default_from_the_access_page(self):
        self._deepseek_ok()
        self.sb.switch_to_project("Atlas")
        atlas = self.sb.active()
        self.assertFalse(self.sb.switch_provider("openai")["ok"])  # no key
        r = self.sb.switch_provider("deepseek")  # no spoken gate: the click is the approval
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.sb.provider, "deepseek")
        self.assertEqual(self.sb.status()["access"]["provider"], "deepseek")
        self.assertIn(("restart", None, "deepseek", False), atlas.session.calls)  # idle line switched at once
        self.assertEqual(self.sb.lines[MANAGER].provider, "deepseek")  # the manager follows the default
        p = self.sb.list_providers()
        self.assertEqual((p["default"], [x["id"] for x in p["providers"]]), ("deepseek", ["claude", "deepseek", "kimi", "openai"]))
        self.assertEqual(switchboard.normalize_provider("月之暗面"), "kimi")
        self.assertEqual(switchboard.normalize_provider("Kimi"), "kimi")
        self.assertTrue(p["providers"][1]["ok"] and p["providers"][1]["is_default"])
        self.assertEqual(p["providers"][1]["models"], ["flash", "pro"])
        self.assertEqual({l["line"]: l["provider"] for l in p["lines"]}[atlas.id], "deepseek")
        self.assertIn("现在就是", self.sb.switch_provider("deepseek")["message"])
        r = self.sb.switch_provider("claude", line_id=atlas.id)  # one line back to Claude; the default stays
        self.assertTrue(r["ok"], r)
        self.assertEqual((atlas.provider, self.sb.provider), ("claude", "deepseek"))

    def test_unreachable_vendor_is_refused_in_three_seconds(self):
        from voiceclaude import access
        self._deepseek_ok()
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.cfg.permissions["switch_provider"] = "allow"
        with mock.patch.object(access, "check_reach", lambda pid, cfg=None: (False, "连不上 api.deepseek.com（屏蔽）。")):
            self.sb.reach = access.check_reach
            r = self.sb.switch_provider("deepseek")
            self.assertFalse(r["ok"])
            self.assertIn("屏蔽", r["message"])
            self.assertEqual(self.sb.provider, "claude")
            r = self.sb.control_request(line.id, "switch_provider", {"provider": "deepseek"})
            self.assertFalse(r["ok"])
            self.assertIn("屏蔽", r["message"])
            self.assertIsNone(line.pending_provider)
            with mock.patch.object(access, "set_provider_key", lambda pid, key, path=None: (True, "存好了。")):
                r = self.sb.set_provider_key("deepseek", "sk-0123456789abcdef0123456789", switch=True)
            self.assertEqual((r["ok"], self.sb.provider), (True, "claude"))  # saved, but not switched onto a dead route

    def test_pending_waits_for_the_turn_and_a_spoken_approval(self):
        self._deepseek_ok()
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        line.session.turn_active = True  # mid-turn: Claude is asking the user something through the gate
        line.state = switchboard.WAITING
        line.pending_provider = "deepseek"
        self.sb._apply_pending(line)
        self.assertEqual(line.pending_provider, "deepseek")  # untouched
        self.assertNotIn(("restart", None, "deepseek", False), line.session.calls)
        line.session.turn_active = False
        line.state = switchboard.IDLE
        self.sb._apply_pending(line)
        self.assertEqual(line.provider, "deepseek")

    def test_fresh_restart_only_on_the_first_turn_after_a_switch(self):
        self._deepseek_ok()
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.sb.switch_provider("deepseek", line_id=line.id)
        self.assertTrue(line.first_turn_after_switch)
        self.clock[0] += 1000  # however long the user waits
        self.sb.say(line.id, "x")
        line.on_session("done", {"status": "ok", "message": "好。"})  # the first turn went fine: no more fallback
        self.assertFalse(line.first_turn_after_switch)
        self.sb.say(line.id, "y")
        line.on_session("done", {"status": "error", "message": "401"})
        self.assertNotIn(("restart", None, None, True), line.session.calls)  # a later error keeps the conversation

    def test_clearing_the_default_key_falls_back_to_a_working_vendor(self):
        from voiceclaude import access
        self._deepseek_ok()
        self.sb.switch_to_project("Atlas")
        atlas = self.sb.active()
        self.sb.switch_provider("deepseek")
        self.assertEqual(self.sb.provider, "deepseek")
        with mock.patch.object(access, "clear_provider_key", lambda pid, path=None: (True, "删掉了。")):
            r = self.sb.clear_provider_key("deepseek")
        self.assertTrue(r["ok"], r)
        self.assertEqual((self.sb.provider, r["default"]), ("claude", "claude"))  # Claude still answers: it takes over
        self.assertIn("默认改成了", r["message"])
        self.assertEqual(atlas.provider, "claude")
        m = self.sb.lines[MANAGER]
        self.assertEqual(m.provider, "claude")
        self.assertIn(("restart", "sonnet", "claude", False), m.session.calls)  # the manager is back on its own model

    def test_manager_line_that_cannot_start_falls_back_to_rules(self):
        from voiceclaude import access
        from voiceclaude.managers import RulesSession

        def factory(line, resume):
            if line.kind == MANAGER:
                raise RuntimeError("GPT（OpenAI）：翻译层刚启动就退出了")
            return FakeSession(line, resume)
        sb = Switchboard(self.cfg, jobs=self.jobs, emit=lambda k, d: self.events.append((k, d)),
                         forward=lambda p, d: {"ok": True}, session_factory=factory,
                         scan_projects=lambda: list(self.projects), now=lambda: self.clock[0], ask_timeout=0.3,
                         detect_access=lambda cmd: access.AccessStatus(True, "subscription", "测试"))
        sb.start()
        self.assertIsInstance(sb.lines[MANAGER].session, RulesSession)
        self.assertTrue(any("规则总机" in n for n in self.notices()))
        self.assertEqual(sb.active_id, MANAGER)

    def test_set_provider_key_ok_means_stored(self):
        from voiceclaude import access
        state = {"st": access.AccessStatus(False, "none", "有 API key，但找不到 Claude Code 程序。")}
        sb = Switchboard(self.cfg, jobs=self.jobs, emit=lambda k, d: self.events.append((k, d)),
                         forward=lambda p, d: {"ok": True}, session_factory=FakeSession,
                         scan_projects=lambda: list(self.projects), now=lambda: self.clock[0], ask_timeout=0.3,
                         detect_access=lambda cmd: state["st"])
        sb.start()
        with mock.patch.object(access, "set_provider_key", lambda pid, key, path=None: (True, "存好了。")):
            r = sb.set_provider_key("claude", "sk-ant-api03-abcdefghijklmnop")
        self.assertTrue(r["ok"])  # stored, even though Claude cannot answer yet
        self.assertFalse(r["provider"]["ok"])
        self.assertIn("找不到 Claude Code", r["message"])

    def test_switch_model_on_a_deepseek_line_uses_that_providers_names(self):
        self._deepseek_ok()
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.cfg.permissions["switch_model"] = "allow"
        self.sb.switch_provider("deepseek", line_id=line.id)
        line.session.model = "deepseek-flash"
        r = self.sb.control_request(line.id, "switch_model", {"model": "pro"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(line.pending_model, "deepseek-v4-pro")
        self.assertIn("flash", self.sb.control_request(line.id, "switch_model", {"model": "gpt"})["message"])  # unknown: the names
        line.session.model = "deepseek-v4-pro"  # as it is once the pro switch has been applied
        r = self.sb.control_request(line.id, "switch_model", {"model": "sonnet"})  # a Claude name = this vendor's middle slot
        self.assertTrue(r["ok"], r)
        self.assertEqual(line.pending_model, "deepseek-flash")
        line.session.model = "deepseek-flash"
        self.assertIn("现在用的就是", self.sb.control_request(line.id, "switch_model", {"model": "sonnet"})["message"])
        r = self.sb.control_request(line.id, "switch_model", {"model": "飞五"})  # fable = the deep one here
        self.assertEqual(line.pending_model, "deepseek-v4-pro")
        self.assertEqual(self.cfg.model, "opus")  # Claude's default model is untouched

    def test_filling_a_deepseek_key_without_claude_makes_it_the_default(self):
        from voiceclaude import access
        from voiceclaude.managers import RulesSession
        sb = Switchboard(self.cfg, jobs=self.jobs, emit=lambda k, d: self.events.append((k, d)),
                         forward=lambda p, d: {"ok": True}, session_factory=FakeSession,
                         scan_projects=lambda: list(self.projects), now=lambda: self.clock[0], ask_timeout=0.3,
                         detect_access=lambda cmd: access.AccessStatus(False, "none", "没装 Claude Code，也没有 API key。"))
        self._deepseek_ok(sb)
        sb.start()
        self.assertIsInstance(sb.lines[MANAGER].session, RulesSession)
        with mock.patch.object(access, "set_provider_key", lambda pid, key, path=None: (True, "DeepSeek 的 key 存好了。")):
            r = sb.set_provider_key("deepseek", "sk-0123456789abcdef0123456789")
        self.assertTrue(r["ok"], r)
        self.assertIn("默认改成了", r["message"])
        self.assertEqual((sb.provider, r["default"]), ("deepseek", "deepseek"))
        self.assertNotIsInstance(sb.lines[MANAGER].session, RulesSession)  # a real manager, on DeepSeek
        self.assertEqual(sb.lines[MANAGER].provider, "deepseek")
        self.assertTrue(sb.switch_to_project("Atlas")["ok"])
        self.assertEqual(sb.active().provider, "deepseek")

    def test_save_and_use(self):
        from voiceclaude import access
        self._deepseek_ok()
        with mock.patch.object(access, "set_provider_key", lambda pid, key, path=None: (True, "存好了。")):
            r = self.sb.set_provider_key("deepseek", "sk-0123456789abcdef0123456789")  # Claude answers: no switch
            self.assertEqual((r["ok"], self.sb.provider), (True, "claude"))
            r = self.sb.set_provider_key("deepseek", "sk-0123456789abcdef0123456789", switch=True)
        self.assertEqual((r["ok"], self.sb.provider, r["default"]), (True, "deepseek", "deepseek"))
        self.assertIn("默认改成了", r["message"])

    def test_openai_line_goes_through_the_gateway(self):
        from voiceclaude import access
        calls = []

        class FakeGateway:
            answer = (True, "")

            def ensure(self, pid):
                calls.append(pid)
                return self.answer

            def endpoint(self):
                return ("http://127.0.0.1:4242", "tok")

            def stop(self):
                calls.append("stop")
        self.sb.gateway = FakeGateway()

        def detect_provider(pid, cmd, cfg=None):
            return access.AccessStatus(pid == "openai", "openai_key" if pid == "openai" else "none", "ok", claude="c",
                                       provider=pid, provider_name="GPT（OpenAI）", model="gpt-6.1-sol")
        self.sb.detect_provider = detect_provider
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        FakeGateway.answer = (False, "翻译层还没装：pip install ...")
        r = self.sb.switch_provider("openai", line_id=line.id)
        self.assertFalse(r["ok"])  # the layer must prove it starts before anything changes
        self.assertIn("翻译层", r["message"])
        self.assertEqual((line.provider, self.sb.provider), ("claude", "claude"))
        self.cfg.permissions["switch_provider"] = "allow"
        self.assertFalse(self.sb.control_request(line.id, "switch_provider", {"provider": "gpt"})["ok"])
        FakeGateway.answer = (True, "")
        r = self.sb.switch_provider("openai", line_id=line.id)
        self.assertTrue(r["ok"], r)
        # ensured on each of the two refused attempts, then on the accepted click and again when the line restarts
        self.assertEqual((line.provider, calls), ("openai", ["openai"] * 4))
        self.sb.shutdown()
        self.assertIn("stop", calls)

    def test_codex_login_picks_the_codex_engine_and_wires_the_gate(self):
        from voiceclaude import access
        made = {}

        class FakeCodex:
            def __init__(self, cfg, prompt, on_event, **kw):
                made.update(kw, prompt=prompt)
                self.model = kw.get("model") or "gpt-6.1-sol"
                self.port = 0

            def start(self):
                pass
        self.sb.detect_provider = lambda pid, cmd, cfg=None: access.AccessStatus(
            pid == "openai", "codex_login" if pid == "openai" else "none", "ok", claude=r"C:\codex\codex.exe", provider=pid,
            provider_name="GPT（OpenAI）", model="gpt-6.1-sol", account="ChatGPT（Codex 已登录）")
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        line.provider = "openai"
        with mock.patch.object(switchboard, "CodexSession", FakeCodex):
            s = self.sb._claude_session(line, "thread-1")
        self.assertIsInstance(s, FakeCodex)
        self.assertEqual((line.engine, made["resume"], made["exe"]), ("codex", "thread-1", r"C:\codex\codex.exe"))
        self.assertIn("voiceclaude", made["servers"])
        self.cfg.permissions["codex_shell"] = "allow"
        self.assertEqual(made["ask"]("codex_shell", "Codex 想在沙箱外运行命令：rm，可以吗？", "rm"), (True, ""))
        self.cfg.permissions["codex_edit"] = "deny"
        ok, why = made["ask"]("codex_edit", "改文件？", "")
        self.assertFalse(ok)
        self.assertIn("禁止", why)
        info = self.sb._provider_info("openai")
        self.assertEqual((info["engine"], info["login_state"], info["methods"]), ("codex", "logged_in", ["login", "key"]))
        self.assertEqual(self.sb.status()["lines"][0]["engine"], "claude")  # the manager stayed on Claude Code
        self.assertFalse(self.sb.login_provider("deepseek")["ok"])
        self.assertFalse(self.sb.login_provider("openai")["ok"])  # refused under the test runner, never a real codex login

    def test_gpt_through_codex_needs_no_litellm(self):
        """A fresh install with Codex signed in and no LiteLLM could not pick GPT. The Codex
        route needs neither the gateway nor our probe of api.openai.com; changing engines makes a new session."""
        from voiceclaude import access
        ensured, reached, made = [], [], []

        class NoGateway:
            def ensure(self, pid):
                ensured.append(pid)
                return False, "翻译层还没装"

            def endpoint(self):
                return ("", "")

            def stop(self):
                pass

        class Fake(FakeSession):
            def __init__(self, cfg, prompt, on_event, **kw):
                self.kw, self.model, self.port, self.calls, self.sent = kw, kw.get("model") or "m", 0, [], []
                made.append(self)

            def start(self):
                self.calls.append("start")

        class FakeCodex(Fake):
            pass

        class FakeClaude(Fake):
            pass
        self.sb.gateway = NoGateway()
        mock.patch.object(access, "gateway_problem", lambda: "翻译层还没装").start()
        mock.patch.object(access, "test_provider", lambda pid, cmd, cfg=None: {"ok": True, "message": "通了", "seconds": 1.0}).start()
        mock.patch.object(switchboard, "CodexSession", FakeCodex).start()
        mock.patch.object(switchboard, "ClaudeSession", FakeClaude).start()
        self.sb.reach = lambda pid, cfg=None: (reached.append(pid), (False, "连不上 api.openai.com") if pid == "openai" else (True, ""))[1]
        self.sb.detect_provider = lambda pid, cmd, cfg=None: access.AccessStatus(
            pid == "openai", "codex_login" if pid == "openai" else "none", "ok", claude=r"C:\codex\codex.exe", provider=pid,
            provider_name="GPT（OpenAI）", model="gpt-6.1-sol", account="ChatGPT（Codex 已登录）")
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        old = line.session
        line.session_factory = self.sb._claude_session  # the real factory: it picks the engine from the provider's status
        r = self.sb.switch_provider("openai", line_id=line.id)
        self.assertTrue(r["ok"], r)
        self.assertEqual((line.provider, line.engine), ("openai", "codex"))
        self.assertIsInstance(line.session, FakeCodex)
        self.assertIn("close", old.calls)  # the Claude Code session was closed, not restarted on the wrong engine
        self.assertEqual(line.session.kw["resume"], "")  # a Claude Code conversation cannot continue in Codex
        self.assertTrue(self.sb.test_provider("openai")["ok"])
        r = self.sb.switch_provider("claude", line_id=line.id)  # and back: a new Claude Code session
        self.assertTrue(r["ok"], r)
        self.assertEqual((line.provider, line.engine), ("claude", "claude"))
        self.assertIsInstance(line.session, FakeClaude)
        self.assertIn("close", made[0].calls)
        self.cfg.permissions["switch_provider"] = "allow"
        r = self.sb.control_request(line.id, "switch_provider", {"provider": "gpt"})  # by voice
        self.assertTrue(r["ok"], r)
        self.assertEqual(line.pending_provider, "openai")
        r = self.sb.switch_provider("openai")  # the default
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.sb.provider, "openai")
        self.assertEqual((ensured, [p for p in reached if p == "openai"]), ([], []))

    def test_transparent_mode_passes_asks_but_still_asks_for_delete_and_overwrite(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.cfg.transparent = True
        passed = (True, switchboard.TRANSPARENT_PASSED)
        self.assertEqual(self.sb.gate(line.id, "screenshot", "我想看一眼屏幕，可以吗？", "", {"kind": "screenshot"}), passed)
        self.assertEqual(self.sb.gate(line.id, "type", "我想输入文字：hello，可以吗？", "hello", {"kind": "type", "text": "hello"}), passed)
        # through the control endpoint the model is told nobody was asked, never that the user agreed
        r = self.sb.control_request(line.id, "ask", {"action": "screenshot", "question": "我想看一眼屏幕，可以吗？",
                                                     "facts": {"kind": "screenshot"}})
        self.assertEqual(r, {"ok": True, "message": "通透模式：直接执行。", "brake": 0})  # brake: the count, for run_steps
        self.assertEqual(self.sb.gate(line.id, "switch_model", "要把模型切换成 sonnet。可以吗？")[0], True)
        self.cfg.permissions["click"] = "deny"
        self.assertFalse(self.sb.gate(line.id, "click", "我想点一下保存，可以吗？", "", {"kind": "click", "target": "保存"})[0])  # deny stays
        self.cfg.permissions["click"] = "ask"
        self.events.clear()
        ok, why = self.sb.gate(line.id, "hotkey", "我想按Delete，可以吗？", "", {"kind": "hotkey", "keys": ["delete"]})  # nobody answers
        self.assertFalse(ok)
        asks = [d for k, d in self.events if k == "ask"]
        self.assertEqual(asks[0]["question"], "删除：我想按Delete，可以吗？")
        self.events.clear()
        self.sb.gate(line.id, "background_job", "我想在后台运行：echo x > D:\\a.txt，可以吗？", "echo x > D:\\a.txt", {"kind": "shell", "command": "echo x > D:\\a.txt"})
        with mock.patch("os.path.exists", lambda p: True):
            self.sb.gate(line.id, "background_job", "我想在后台运行：echo x > D:\\a.txt，可以吗？", "echo x > D:\\a.txt", {"kind": "shell", "command": "echo x > D:\\a.txt"})
        asks = [d for k, d in self.events if k == "ask"]
        self.assertTrue(any(q["question"].startswith("会覆盖已有文件") for q in asks), [q["question"] for q in asks])
        # a batch: no risky step -> straight through, even with batch = ask; a risky step -> one question naming it
        self.cfg.permissions["batch"] = "ask"
        r = self.sb.gate_batch(line.id, "我想打开记事本，然后输入 hi，可以吗？", ["open_app", "type"], "1. 打开记事本\n2. 输入文字：hi",
                               [{"kind": "open_app", "name": "记事本"}, {"kind": "type", "text": "hi"}])
        self.assertTrue(r["ok"], r)
        self.events.clear()
        r = self.sb.gate_batch(line.id, "我想打开记事本，然后按Delete，可以吗？", ["open_app", "hotkey"], "1. 打开记事本\n2. 按Delete",
                               [{"kind": "open_app", "name": "记事本"}, {"kind": "hotkey", "keys": ["delete"]}])
        self.assertFalse(r["ok"])
        self.assertFalse(r.get("per_step"))
        self.assertEqual([d for k, d in self.events if k == "ask"][0]["question"], "删除：我想打开记事本，然后按Delete，可以吗？")
        # the user's case: open Notepad, type, Save As test.txt. A plain Save As passes; Windows' "replace it?" asks once
        self.events.clear()
        steps = [{"kind": "click", "target": "文件名框", "window": "另存为"}, {"kind": "type", "text": "test.txt", "window": "另存为"},
                 {"kind": "hotkey", "keys": ["enter"], "window": "另存为"}]
        r = self.sb.gate_batch(line.id, "我想在“另存为”里：点一下文件名框，然后输入文字：test.txt，然后按回车，可以吗？",
                               ["click", "type", "hotkey"], "", steps)
        self.assertTrue(r["ok"], r)
        self.assertEqual([d for k, d in self.events if k == "ask"], [])
        r = self.sb.gate_batch(line.id, "我想在“确认另存为”里：点一下是，然后按回车，可以吗？", ["click", "type", "hotkey"], "",
                               [dict(f, window="确认另存为") for f in steps])
        q = [d for k, d in self.events if k == "ask"][0]["question"]
        self.assertEqual(q, "会覆盖已有文件：我想在“确认另存为”里：点一下是，然后按回车，可以吗？")  # one reason, no list of steps

    def test_transparent_phrases_and_tool(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.assertIn("transparent", self.sb.status())
        self.assertFalse(self.sb.status()["transparent"])
        r = self.sb.phrase("transparent_on")
        self.assertEqual((r["ok"], r["spoken"]), (True, ""))
        self.assertTrue(self._wait(lambda: any(k == "ask" for k, d in self.events)))
        ask = [d for k, d in self.events if k == "ask"][0]
        self.assertEqual(ask["question"], switchboard.TRANSPARENT_QUESTION)
        self.assertEqual(ask["action"], "transparent")
        self.sb.answer(ask["request"], True)
        self.assertTrue(self._wait(lambda: self.cfg.transparent))
        self.assertIn(("transparent", {"on": True}), self.events)
        self.assertTrue(self.sb.status()["transparent"])
        self.assertEqual(self.sb.phrase("transparent_on")["spoken"], "通透模式已经开着。")
        r = self.sb.phrase("transparent_off")
        self.assertEqual((r["spoken"], self.cfg.transparent), ("通透模式已关闭。", False))
        self.assertIn(("transparent", {"on": False}), self.events)
        self.assertEqual(self.sb.control_request(line.id, "set_transparent", {"on": False})["message"], "通透模式本来就是关的。")
        self.events.clear()
        results = {}
        threading.Thread(target=lambda: results.update(r=self.sb.control_request(line.id, "set_transparent", {"on": True})), daemon=True).start()
        self.assertTrue(self._wait(lambda: any(k == "ask" for k, d in self.events)))
        self.sb.answer([d for k, d in self.events if k == "ask"][0]["request"], False)
        self.assertTrue(self._wait(lambda: "r" in results))
        self.assertFalse(results["r"]["ok"])
        self.assertFalse(self.cfg.transparent)

    def test_switch_model_after_turn(self):
        self.sb.switch_to_project("Atlas")
        line = self.sb.active()
        self.cfg.permissions["switch_model"] = "allow"
        r = self.sb.control_request(line.id, "switch_model", {"model": "sonnet"})
        self.assertTrue(r["ok"])
        self.sb.say(line.id, "x")
        self.assertNotIn(("restart", "sonnet"), line.session.calls)
        line.on_session("done", {"status": "ok", "message": "好。"})
        self.assertIn(("restart", "sonnet"), line.session.calls)

    def _wait(self, pred, timeout=2.0):
        import time
        t0 = time.time()
        while time.time() - t0 < timeout:
            if pred():
                return True
            time.sleep(0.02)
        return False


if __name__ == "__main__":
    unittest.main()
