"""Reading another Claude Code session's answer from Claude Code's own files (relay.py), with fakes only."""
import json
import tempfile
import unittest
from pathlib import Path

from voiceclaude import relay


def entry(kind, **kw):
    return json.dumps({"type": kind, **kw}, ensure_ascii=False) + "\n"


def envelope(text, sender="line-a"):
    return f'<cross-session-message from="{sender}">\n{text}\n</cross-session-message>'


def peer(text, stamp="2026-10-23T10:00:00.000Z", sender="line-a", msg_id=None):
    origin = {"kind": "peer", "from": sender, **({"msg_id": msg_id} if msg_id else {})}
    return entry("user", isMeta=True, timestamp=stamp, origin=origin, message={"role": "user", "content": envelope(text, sender)})


def queued(text, op="enqueue"):
    return entry("queue-operation", operation=op, content=envelope(text) if op == "enqueue" else "")


def batch(*texts, sender="line-a"):
    """Several messages the target held while busy, handed over in one entry (seen in real transcripts)."""
    return entry("user", isMeta=True, timestamp="2026-10-23T10:00:00.000Z", origin={"kind": "peer", "from": sender},
                 message={"role": "user", "content": "Another Claude session sent a message:\n"
                          + "\n".join(envelope(t, sender) for t in texts)})


def mid_turn(text):
    return entry("attachment", attachment={"type": "queued_command", "prompt": envelope(text)})


def said(text, stop="end_turn", sidechain=False, stamp=None):
    return entry("assistant", isSidechain=sidechain, **({"timestamp": stamp} if stamp else {}),
                 message={"role": "assistant", "stop_reason": stop, "content": [{"type": "text", "text": text}]})


def send(to, message, text=""):
    content = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "name": "SendMessage", "input": {"to": to, "message": message}}]
    return entry("assistant", message={"role": "assistant", "stop_reason": "tool_use", "content": content})


def tool_use(name="Read"):
    return entry("assistant", message={"stop_reason": "tool_use", "content": [{"type": "thinking", "thinking": "嗯"},
                                                                              {"type": "tool_use", "name": name, "input": {}}]})


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sessions = self.tmp / "sessions"
        self.sessions.mkdir()

    def add(self, pid, **d):
        (self.sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, **d}, ensure_ascii=False), "utf-8")

    def test_find_by_name_or_id_never_the_key_files(self):
        self.add(1, sessionId="A", name="论文写作辅助", cwd="D:\\thesis", updatedAt=5, hostSessionId="local_a")
        self.add(2, sessionId="B", name="Token 恢复", cwd="D:\\thesis", updatedAt=9)
        (self.sessions / "1.abcdef.key").write_text("secret", "utf-8")
        self.assertEqual(relay.find_target("论文写作辅助", self.sessions)["sessionId"], "A")  # by name, not by folder
        self.assertEqual(relay.find_target("论文写作辅助 [3fa9c1]", self.sessions)["sessionId"], "A")
        self.assertEqual(relay.find_target("B", self.sessions)["name"], "Token 恢复")
        self.assertEqual(relay.find_target("local_a", self.sessions)["sessionId"], "A")
        self.assertEqual(relay.find_target("token 恢复", self.sessions)["sessionId"], "B")
        self.assertIsNone(relay.find_target("别的", self.sessions))
        self.add(3, sessionId="C", name="论文写作辅助", cwd="D:\\thesis", updatedAt=7)
        self.assertEqual(relay.find_target("论文写作辅助", self.sessions)["sessionId"], "C")  # the newest
        self.assertTrue(all(not d["_file"].endswith(".key") for d in relay.live_sessions(self.sessions)))

    def test_the_sessions_permission_mode_from_its_transcript(self):
        """The latest mode the transcript noted; its class decides whether our message is delivered at once."""
        t = Path(tempfile.mkdtemp()) / "S.jsonl"
        self.assertEqual(relay.permission_mode(t), "")  # no transcript
        t.write_text(entry("user", permissionMode="auto") + entry("assistant") + entry("user", permissionMode="bypassPermissions")
                     + entry("assistant"), "utf-8")
        self.assertEqual(relay.permission_mode(t), "bypassPermissions")
        self.assertEqual([relay.mode_class(m) for m in ("bypassPermissions", "auto", "default", "acceptEdits", "plan", "")],
                         ["bypass", "prompting", "prompting", "prompting", "", ""])

    def test_transcript_path(self):
        projects = self.tmp / "projects"
        self.assertEqual(relay.transcript_path({"sessionId": "S", "cwd": "D:\\thesis"}, projects),
                         projects / "D--thesis" / "S.jsonl")
        (projects / "elsewhere").mkdir(parents=True)
        (projects / "elsewhere" / "T.jsonl").write_text("", "utf-8")
        self.assertEqual(relay.transcript_path({"sessionId": "T", "cwd": "C:\\x"}, projects), projects / "elsewhere" / "T.jsonl")


class WatchTests(unittest.TestCase):
    ASK = "用户口述：帮我看看引言"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "S.jsonl"
        self.path.write_text("", "utf-8")
        self.reg = self.tmp / "1.json"
        self.status("busy")
        self.t = [0.0]
        self.states = []
        self.target = {"sessionId": "S", "name": "论文写作辅助", "_file": str(self.reg)}

    def status(self, value, since=0):
        self.reg.write_text(json.dumps({"sessionId": "S", "status": value, "statusUpdatedAt": since}), "utf-8")

    def append(self, *lines):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("".join(lines))

    def watcher(self, offset=0, hook=None, message=None, **kw):
        def sleep(s):
            self.t[0] += s
            if hook:
                hook(self.t[0])
        kw.setdefault('poll', 5.0)  # fake seconds per look: keeps the long waits quick
        return relay.Watch(self.target, self.path, offset, message or self.ASK, clock=lambda: self.t[0], sleep=sleep,
                           on_state=self.states.append, **kw)

    def watch(self, offset=0, hook=None, **kw):
        return self.watcher(offset, hook, **kw).run()

    def test_the_answer_after_our_message(self):
        self.append(said("早先的回答，不算"), peer(self.ASK, "2026-10-23T09:00:00Z"))  # an older copy of the same words
        offset = self.path.stat().st_size
        self.append(peer(self.ASK), tool_use(), said("我先读一下。", "tool_use"), said("在侧链里", sidechain=True),
                    said("引言第二段太长，建议拆开。"))
        out = self.watch(offset)
        self.assertEqual(out, {"text": "我先读一下。\n\n引言第二段太长，建议拆开。", "done": True, "session": "S", "why": ""})
        self.assertEqual(self.states, [])

    def test_a_busy_targets_own_work_before_it_takes_our_message_is_not_the_answer(self):
        """Seen in real transcripts: the enqueue first, then the target's work in hand, then the hand-over."""
        self.append(queued(self.ASK), said("正在跑第 3 组测试，结果稍后汇总。", "stop_sequence"),
                    said("另一件事的总结：第二章的图都重画好了。"), queued("", "dequeue"), peer(self.ASK),
                    said("引言第二段太长。"))
        self.assertEqual(self.watch()["text"], "引言第二段太长。")

    def test_handed_over_mid_turn_as_a_queued_command(self):
        self.append(said("我在改第二章。", "tool_use"), mid_turn(self.ASK), said("收到。引言我看了：太长。"))
        self.assertEqual(self.watch()["text"], "收到。引言我看了：太长。")

    def test_an_answer_sent_back_by_send_message_is_the_answer(self):
        """Claude Code tells a receiver to answer with SendMessage to the from= address (our line refuses it, so it
        is read here); messages it sends anyone else are not ours."""
        self.append(peer(self.ASK, sender="line-a"), send("别的会话", "顺便告诉你一声"),
                    send("line-a [3fa9c1]", "改好了，换成了 2024 年的两篇。", text="我来回复一下。"), said("已回复对方会话。"))
        self.assertEqual(self.watch()["text"], "改好了，换成了 2024 年的两篇。")

    def test_two_messages_to_one_session_each_get_their_own_answer(self):
        """The second hand-over from us ends the first one's part, mid-turn or after the turn."""
        B = "用户口述：顺便看看结论"
        mid = [peer(self.ASK), said("我先看引言。", "tool_use"), mid_turn(B), send("line-a", "引言第二段太长。"),
               send("line-a", "结论缺一句总结。"), said("都回复了。")]
        after = [peer(self.ASK), said("引言第二段太长。"), peer(B), said("结论缺一句总结。")]
        for lines in (mid, after):
            self.path.write_text("".join(lines), "utf-8")
            first = self.watch()
            self.ASK, keep = B, self.ASK
            second = self.watch()
            self.ASK = keep
            self.assertNotIn("结论", first["text"])
            self.assertIn("结论缺一句总结。", second["text"])
            self.assertEqual(first["why"], "superseded")
        self.assertEqual(first["text"], "引言第二段太长。")
        # a message from someone else to that session does not end ours
        self.path.write_text("".join([peer(self.ASK), said("我先看引言。", "tool_use"), mid_turn("别人的话").replace("line-a", "line-b"),
                                      said("引言第二段太长。")]), "utf-8")
        self.assertEqual(self.watch()["text"], "我先看引言。\n\n引言第二段太长。")

    def test_messages_handed_over_together_are_answered_once(self):
        B = "用户口述：顺便看看结论"
        self.append(queued(self.ASK), queued(B), batch(self.ASK, B), send("line-a", "引言第二段太长。结论缺一句总结。"), said("已回复。"))
        first = self.watch()
        self.ASK, keep = B, self.ASK
        second = self.watch()
        self.ASK = keep
        self.assertEqual((first["text"], first["done"], first["why"]), ("", True, "superseded"))  # the last of ours answers
        self.assertEqual((second["text"], second["why"]), ("引言第二段太长。结论缺一句总结。", ""))

    def test_a_later_message_that_starts_with_ours_is_not_ours(self):
        B = "用户口述：帮我看看引言第二段的引用对不对"  # A's whole first line is in it
        self.append(peer(self.ASK), said("我先读引言。", "tool_use"), mid_turn(B), said("引用都对。"))
        first = self.watch()
        self.ASK, keep = B, self.ASK
        second = self.watch()
        self.ASK = keep
        self.assertEqual((first["text"], first["why"]), ("我先读引言。", "superseded"))
        self.assertEqual(second["text"], "引用都对。")

    def test_only_a_message_from_us_ends_ours(self):
        """A command the user queued there has no sender: it does not cut our answer short."""
        typed = entry("attachment", attachment={"type": "queued_command", "prompt": "顺便把标题也改了"})
        self.append(peer(self.ASK), said("我先读引言。", "tool_use"), typed, said("引言第二段太长。"))
        self.assertEqual(self.watch()["text"], "我先读引言。\n\n引言第二段太长。")

    def test_the_same_words_twice_are_two_messages(self):
        """The user says it again (after 还没收到这句话, say): each copy gets its own answer, mid-turn, in one batch, or
        both held and handed over one after the other."""
        shapes = {"mid-turn": [peer(self.ASK), said("ANSWER-1", "tool_use"), mid_turn(self.ASK), said("ANSWER-2")],
                  "batch": [peer("x", sender="line-b"), batch(self.ASK, self.ASK), said("ANSWER-2")],
                  "one after the other": [peer(self.ASK), said("ANSWER-1"), peer(self.ASK), said("ANSWER-2")]}
        for name, lines in shapes.items():
            self.path.write_text("".join(lines), "utf-8")
            first, second = self.watcher(), self.watcher()  # both waiting before either copy is handed over
            a, b = first.run(), second.run()
            self.assertEqual(b["text"], "ANSWER-2", name)
            self.assertNotIn("ANSWER-2", a["text"], name)
            self.assertEqual(a["why"], "superseded", name)
        # the first copy already handed over (its watch has not looked yet) when the user says yes to the second
        self.path.write_text(peer(self.ASK) + said("ANSWER-1", "tool_use"), "utf-8")
        first = self.watcher()
        second = self.watcher(self.path.stat().st_size)  # noted at the spoken yes, with the size then
        self.append(mid_turn(self.ASK), said("ANSWER-2"))
        a, b = first.run(), second.run()
        self.assertEqual((a["text"], b["text"]), ("ANSWER-1", "ANSWER-2"))

    def test_copies_are_matched_once_whichever_watch_looks_first(self):
        """The count is taken at the spoken yes, with the offset; what is whose is decided by position, once."""
        for second_first in (False, True):
            self.path.write_text("", "utf-8")
            first = self.watcher()
            self.append(peer(self.ASK), said("ANSWER-1", "tool_use"))  # copy 1 lands just before the second yes
            second = self.watcher(self.path.stat().st_size)
            self.append(mid_turn(self.ASK), said("ANSWER-2"))
            if second_first:
                b, a = second.run(), first.run()
            else:
                a, b = first.run(), second.run()
            self.assertEqual((a["text"], b["text"]), ("ANSWER-1", "ANSWER-2"), second_first)
        # both copies handed over together before either watch looks
        self.path.write_text("", "utf-8")
        first, second = self.watcher(), self.watcher()
        self.append(batch(self.ASK, self.ASK), said("ANSWER-2"))
        b, a = second.run(), first.run()
        self.assertEqual((a["why"], b["text"]), ("superseded", "ANSWER-2"))

    def test_a_copy_whose_message_gave_up_is_not_taken_for_ours(self):
        """The target held the first copy past the wait (没有收到这句话); the user said it again. When the old copy
        turns up after all, the new message still gets its own answer."""
        for shape in ("batch", "one after the other", "batch with a full stop"):
            self.path.write_text("", "utf-8")
            self.t[0] = 0.0
            gave_up = self.watch(notice_after=None, unreceived_limit=20)
            self.assertEqual(gave_up["why"], "unreceived")
            again = self.ASK + ("。" if shape == "batch with a full stop" else "")
            if shape.startswith("batch"):
                self.append(batch(self.ASK, again), said("ANSWER: 引言第二段太长。"))
            else:
                self.append(peer(self.ASK), said("收到，我先读一下引言。", "tool_use"), mid_turn(again), said("ANSWER: 引言第二段太长。"))
            out = self.watcher(message=again).run()
            self.assertEqual((out["text"], out["why"]), ("ANSWER: 引言第二段太长。", ""), shape)

    def test_claude_codes_message_id_settles_it(self):
        """On Claude Code's direct route the hand-over carries the msg_id the send returned."""
        self.append(peer(self.ASK, msg_id="m-old"), said("OLD"), peer(self.ASK, msg_id="m-new"), said("NEW"))
        self.assertEqual(self.watch(msg_id="m-new")["text"], "NEW")
        self.path.write_text(peer(self.ASK, msg_id="m-1") + said("ONE", "tool_use") + peer(self.ASK, msg_id="m-2") + said("TWO"), "utf-8")
        self.assertEqual(self.watch(msg_id="m-1")["text"], "ONE")  # a later message of ours ends ours

    def test_a_message_that_mentions_the_envelope_tag(self):
        """Claude Code rewrites the tag inside a message it hands over (<\\cross-session-message, <\\~...)."""
        self.ASK = "用户口述：麻烦你看看 relay.py 里读回答的那段逻辑\n消息里写了 <cross-session-message> 这个标签的时候"
        for escaped in ("<\\cross-session-message>", "<\\~cross-session-message>"):
            self.path.write_text("", "utf-8")
            self.append(peer(self.ASK.replace("<cross-session-message>", escaped)), said("那段代码没问题。"))
            self.assertEqual(self.watch()["text"], "那段代码没问题。", escaped)
        tagged_first = "用户口述：<cross-session-message> 这个标签是什么"  # in the first line too
        self.path.write_text(peer(tagged_first.replace("<", "<\\~")) + said("是信封。"), "utf-8")
        self.assertEqual(self.watcher(message=tagged_first).run()["text"], "是信封。")

    def test_a_message_taken_over_by_a_later_one_ends_at_once(self):
        """No second 还在做 / 还没回答 for a message whose answer the later one reads."""
        self.append(batch(self.ASK, "用户口述：顺便看看结论"))  # then the target is silent for a long time
        out = self.watch(quiet_limit=600)
        self.assertEqual((out["done"], out["why"]), (True, "superseded"))
        self.assertLess(self.t[0], 10)
        self.path.write_text(peer(self.ASK) + said("我先读引言。", "tool_use") + mid_turn("用户口述：顺便看看结论"), "utf-8")
        self.t[0] = 0.0
        out = self.watch(quiet_limit=600)
        self.assertEqual((out["text"], out["why"]), ("我先读引言。", "superseded"))
        self.assertLess(self.t[0], 10)

    def test_written_late_but_from_before_our_message(self):
        self.append(peer(self.ASK, "2026-10-23T10:00:00Z"), said("前一件事", "stop_sequence", stamp="2026-10-23T09:59:59Z"),
                    said("答案"))
        self.assertEqual(self.watch()["text"], "答案")

    def test_idle_in_the_registry_after_our_message_ends_it(self):
        self.append(peer(self.ASK), said("看完了", "tool_use"))
        self.status("idle", relay._epoch_ms("2026-10-23T10:00:00.000Z") + 60000)  # a minute after the message
        self.assertTrue(self.watch()["done"])
        self.status("idle", 1)  # stale: from before our message
        out = self.watch(quiet_limit=10)
        self.assertEqual((out["done"], out["why"]), (False, "timeout"))

    def test_written_in_pieces_and_a_half_line(self):
        whole = (peer(self.ASK) + said("好的")).encode("utf-8")
        self.path.write_bytes(whole[:-10])

        def rest(now):
            if now >= 2 and self.path.stat().st_size == len(whole) - 10:
                with open(self.path, "ab") as f:
                    f.write(whole[-10:])

        self.assertEqual(self.watch(hook=rest)["text"], "好的")

    def test_never_received_is_said_early_and_ends_the_wait(self):
        self.append(said("别的事"))
        out = self.watch(notice_after=45, unreceived_limit=100)
        self.assertEqual(out, {"text": "", "done": False, "session": "S", "why": "unreceived"})
        self.assertEqual(self.states, ["unreceived"])  # once
        self.assertLess(self.t[0], 101)

    def test_queued_but_never_handed_over(self):
        self.append(queued(self.ASK))
        self.status("idle")
        out = self.watch(notice_after=45, quiet_limit=100)
        self.assertEqual((out["why"], self.states), ("timeout", []))  # it did arrive: no "not received"

    def test_no_fixed_limit_while_it_keeps_working(self):
        self.append(peer(self.ASK), said("我先把第三章的引用逐条核对一遍。", "tool_use"))

        def work(now):
            if now % 100 == 0 and now < 1500:
                self.append(tool_use())
            elif now == 1500:
                self.append(said("核对完了：有 4 处引用年份不对，已改。"))

        out = self.watch(hook=work, quiet_limit=600)
        self.assertEqual((out["done"], out["text"]), (True, "我先把第三章的引用逐条核对一遍。\n\n核对完了：有 4 处引用年份不对，已改。"))
        self.assertGreater(self.t[0], 1500)

    def test_giving_up_while_it_is_still_busy_says_so(self):
        self.append(peer(self.ASK), said("我先核对一遍。", "tool_use"))  # then a long tool run: nothing new, still busy
        out = self.watch(quiet_limit=600)
        self.assertEqual((out["done"], out["why"], out["text"]), (False, "working", "我先核对一遍。"))

        def busy_writing(now):
            self.append(tool_use())

        self.t[0] = 0.0
        self.path.write_text(peer(self.ASK), "utf-8")
        out = self.watch(hook=busy_writing, quiet_limit=600, hard_limit=900)
        self.assertEqual(out["why"], "working")
        self.assertLess(self.t[0], 901)

    def test_the_session_closing_ends_the_wait(self):
        self.append(peer(self.ASK), said("读到一半", "tool_use"))

        def close(now):
            if now >= 1 and self.reg.exists():
                self.reg.unlink()
        out = self.watch(hook=close)
        self.assertEqual((out["done"], out["why"], out["text"]), (False, "gone", "读到一半"))
        self.assertLess(self.t[0], 20)  # not the whole wait

    def test_the_user_typing_there_next_closes_our_answer(self):
        self.append(peer(self.ASK), said("好的"), entry("user", message={"content": "再改一下标题"}), said("标题改好了"))
        self.assertEqual(self.watch()["text"], "好的")


if __name__ == "__main__":
    unittest.main()
