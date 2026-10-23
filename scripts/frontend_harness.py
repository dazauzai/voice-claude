"""Front-end test against a FAKE daemon: no Claude, no hotkey, speech synthesised instead of a microphone.

Checks the front end's behaviour (docs/protocol.md): turns, level-1 ducking/cutting with queued speech,
spoken approvals and questions (also from a background line), fixed phrases, settings requests, tap/hold stops,
line switching and daemon loss. Run: .venv\\Scripts\\python -X utf8 scripts\\frontend_harness.py [--audible]
The hotkey is disabled, the settings live in a throwaway VOICECLAUDE_HOME (your ~/.voice-claude is not touched),
and everything the app says is played as silence of the same length; --audible plays it on the configured speaker.
"""
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if __name__ == "__main__":  # imported by selftest.py, which brings its own throwaway home
    os.environ["VOICECLAUDE_HOME"] = tempfile.mkdtemp(prefix="vc-harness-")  # before voiceclaude.paths is imported
os.environ["VOICECLAUDE_NO_HOTKEY"] = "1"

from voiceclaude import audio, config  # noqa: E402
from voiceclaude.app import APPROVING, App, BUSY, IDLE, LISTENING, RECORDING  # noqa: E402

log = logging.getLogger("harness")
results = []
TOKEN = "test-token"


def mute_output() -> None:
    """The app's speech is played as silence of the same length: the timing every check relies on (speaking,
    ducking, cutting, queued speech) stays, and no device in the room makes a sound."""
    play = audio.Player.play

    def silent(self, samples, gen, done):
        return play(self, np.zeros_like(samples), gen, done)
    audio.Player.play = silent


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    log.info("%s %s %s", "PASS" if ok else "FAIL", name, detail)


def wait_for(pred, timeout, every=0.02):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if pred():
            return time.perf_counter() - t0
        time.sleep(every)
    return None


class FakeDaemon(threading.Thread):
    """Speaks the daemon protocol; replies to `say` with a scripted reply."""

    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.lock = threading.Lock()
        self.received = []  # (cmd dict) in order
        self.replies = []  # reply commands (settings requests)
        self.active = "m1"
        self.lines = {"m1": {"line": "m1", "name": "总机", "state": "idle", "active": True, "unread": 0},
                      "l2": {"line": "l2", "name": "Atlas", "state": "idle", "active": False, "unread": 0}}
        self.plan = [(0.1, "好的。")]  # (delay before, text) pieces for the next reply
        self.stop_reply = threading.Event()
        self.wfile = None

    def send(self, obj):
        with self.lock:
            self.wfile.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.wfile.flush()

    def emit(self, _kind, **data):
        self.send({"event": _kind, **data})

    def got(self, cmd, after=0):
        return [c for c in self.received[after:] if c.get("cmd") == cmd]

    def run(self):
        sock, _ = self.srv.accept()
        rfile = sock.makefile("r", encoding="utf-8")
        self.wfile = sock.makefile("w", encoding="utf-8", newline="\n")
        hello = json.loads(rfile.readline())
        assert hello.get("hello") == TOKEN, hello
        self.send({"ok": True, "protocol": 1, "daemon": {"pid": 0, "port": self.port}, "active": self.active,
                   "lines": list(self.lines.values())})
        for line in rfile:
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.received.append(cmd)
            threading.Thread(target=self.handle, args=(cmd,), daemon=True).start()

    def handle(self, cmd):
        name, cid = cmd.get("cmd"), cmd.get("id")
        if name == "reply":
            self.replies.append(cmd)
            return
        result = {"ok": True, "message": ""}
        if name == "phrase":
            result["spoken"] = {"back_to_manager": "接到总机。", "scratch": "接到杂项。"}.get(cmd.get("key"), "好。")
        elif name == "switch":
            self._activate(cmd.get("line"))
        elif name == "status":
            result.update({"active": self.active, "lines": list(self.lines.values()), "jobs": [], "unread": 0})
        if cid is not None:
            self.send({"id": cid, **result})
        if name == "say":
            threading.Thread(target=self._reply, args=(cmd.get("line"),), daemon=True).start()
        elif name in ("interrupt", "stop_all"):
            self.stop_reply.set()

    def _activate(self, lid):
        for l in self.lines.values():
            l["active"] = l["line"] == lid
        self.active = lid
        for l in self.lines.values():
            self.emit("line", **l)

    def _reply(self, line):
        self.stop_reply.clear()
        last = ""
        for delay, text in self.plan:
            if self.stop_reply.wait(delay):
                self.emit("done", line=line, status="interrupted", last=last)
                return
            self.emit("text", line=line, text=text)
            last = text
        self.emit("done", line=line, status="ok", last=last)


class Driver(threading.Thread):
    def __init__(self, app: App, fake: FakeDaemon):
        super().__init__(daemon=False)
        self.app, self.fake = app, fake

    def speech(self, text):
        a = self.app.speaker.tts.generate(text, sid=50, speed=1.0)
        s24 = np.asarray(a.samples, dtype=np.float32)
        n16 = int(len(s24) * 16000 / a.sample_rate)
        return np.interp(np.linspace(0, len(s24) - 1, n16), np.arange(len(s24)), s24).astype(np.float32)

    def push(self, samples, pace=0.002):
        pad = (-len(samples)) % audio.BLOCK
        samples = np.concatenate([samples, np.zeros(pad, np.float32)])
        for i in range(0, len(samples), audio.BLOCK):
            self.app.audio_q.put(samples[i:i + audio.BLOCK])
            time.sleep(pace)

    def silence(self, seconds=None):
        self.push(np.zeros(int(((seconds or self.app.cfg.silence_seconds) + 0.4) * 16000), np.float32))

    def say(self, text):
        self.push(self.speech(text))
        self.silence()

    def run(self):
        app, fake = self.app, self.fake
        try:
            ok = wait_for(lambda: app.ready, 90)
            check("app ready (fake daemon)", ok is not None, f"{ok and round(ok, 1)}s")
            if ok is None:
                return
            app.mic.stop()
            time.sleep(0.3)
            check("lines listed", "m1" in app.lines and app.client.active == "m1", str(list(app.lines)))

            # 1. a normal turn
            app._on_double()
            check("double tap -> listening", app.state == LISTENING and app.conversation)
            fake.plan = [(0.1, "好的，我看了一下。"), (0.2, "一共有三个文件。")]
            n = len(fake.received)
            self.say("帮我看一下当前文件夹。")
            got = wait_for(lambda: fake.got("say", n), 10)
            check("speech -> say to active line", got is not None and fake.got("say", n)[0].get("line") == "m1",
                  str(fake.got("say", n)[:1]))
            done = wait_for(lambda: app.state == LISTENING and not app.turn_open and app.speaker.idle(), 30)
            check("turn finished -> listening", done is not None, f"{done and round(done, 1)}s")

            # 2. talking over a long reply: duck, cut, queue, send after done
            fake.plan = [(0.3, f"第{i}句，还在说。") for i in range(1, 9)]
            n = len(fake.received)
            self.say("给我讲讲这个项目。")
            wait_for(lambda: app.turn_open and not app.speaker.idle(), 10)
            time.sleep(0.4)
            long_speech = self.speech("等一下，我还有一个问题想先问你，就是关于那个下载的事情。")
            self.push(long_speech, pace=0.004)
            ducked = app.player.ducked or app.cut
            self.silence()
            check("level-1: voice ducked/cut, line not interrupted", ducked and not fake.got("interrupt", n),
                  f"ducked={app.player.ducked} cut={app.cut}")
            queued = wait_for(lambda: len(app.queued) == 1 or len(fake.got("say", n)) >= 2, 10)
            check("your words queued while the line is busy", queued is not None, str(app.queued))
            sent = wait_for(lambda: len(fake.got("say", n)) >= 2, 30)
            check("queued words sent after done", sent is not None and "下载" in fake.got("say", n)[1].get("text", ""),
                  str([c.get("text") for c in fake.got("say", n)]))
            fake.plan = [(0.1, "好的。")]
            wait_for(lambda: app.state == LISTENING and not app.turn_open and app.speaker.idle(), 30)

            # 3. an approval while listening
            n = len(fake.received)
            fake.emit("ask", request="r1", line="m1", line_name="总机", action="switch_model",
                      question="要把模型切换成 sonnet，可以吗？", detail="", kind="approve")
            asked = wait_for(lambda: app.state == APPROVING, 5)
            check("ask -> approving", asked is not None)
            self.push(self.speech("可以。"))
            self.silence(0.8)
            ans = wait_for(lambda: fake.got("answer", n), 15)
            check("spoken 可以 -> answer yes", ans is not None and fake.got("answer", n)[0].get("yes") is True
                  and fake.got("answer", n)[0].get("request") == "r1", str(fake.got("answer", n)[:1]))
            wait_for(lambda: app.state == LISTENING, 5)

            # 4. a question (whole sentence goes back)
            n = len(fake.received)
            fake.emit("ask", request="r2", line="m1", line_name="总机", action="", question="接到哪个项目？", detail="", kind="question")
            wait_for(lambda: app.state == APPROVING, 5)
            self.push(self.speech("接到报告那个项目。"))
            self.silence(0.8)
            ans = wait_for(lambda: fake.got("answer", n), 15)
            check("question -> whole sentence returned", ans is not None and "项目" in fake.got("answer", n)[0].get("text", ""),
                  str(fake.got("answer", n)[:1]))
            wait_for(lambda: app.state == LISTENING, 5)

            # 5. a background line asks while a turn is open; 不行 -> no; the turn goes on
            fake.plan = [(0.2, "我在算。"), (2.5, "算完了。")]
            n = len(fake.received)
            self.say("帮我算一下。")
            wait_for(lambda: app.turn_open, 5)
            fake.emit("ask", request="r3", line="l2", line_name="Atlas", action="type", question="我想输入文字 hello，可以吗？",
                      detail="", kind="approve", background=True)
            asked = wait_for(lambda: app.state == APPROVING, 5)
            check("background ask during a turn -> approving", asked is not None)
            self.push(self.speech("不行。"))
            self.silence(0.8)
            ans = wait_for(lambda: fake.got("answer", n), 15)
            check("spoken 不行 -> answer no", ans is not None and fake.got("answer", n)[0].get("yes") is False)
            done = wait_for(lambda: app.state == LISTENING and not app.turn_open and app.speaker.idle(), 30)
            check("the open turn finished after the background ask", done is not None)

            # 5b. an ask arrives while you are mid-sentence over a busy line: your sentence must finish and be
            #     queued first, then the question is asked; nothing of what you said is lost or taken as the answer
            fake.plan = [(0.3, f"第{i}句，还在说。") for i in range(1, 25)]
            n = len(fake.received)
            self.say("再讲讲。")
            wait_for(lambda: app.turn_open and not app.speaker.idle(), 10)
            time.sleep(0.4)
            long_speech = self.speech("等一下，我还想问一个关于下载的问题，你先别急着回答我。")
            half = len(long_speech) // 2
            self.push(long_speech[:half], pace=0.004)
            fake.emit("ask", request="r4", line="l2", line_name="Atlas", action="type", question="我想输入文字 hi，可以吗？",
                      detail="", kind="approve", background=True)
            time.sleep(0.3)
            check("ask mid-sentence waits (not approving yet)", app.state != APPROVING, app.state)
            self.push(long_speech[half:], pace=0.004)
            self.silence()
            queued = wait_for(lambda: len(app.queued) == 1, 10)
            check("whole sentence queued before the question", queued is not None and "下载" in "".join(app.queued),
                  str(app.queued))
            asked = wait_for(lambda: app.state == APPROVING, 5)
            check("then the question is asked", asked is not None)
            self.push(self.speech("可以。"))
            self.silence(0.8)
            ans = wait_for(lambda: fake.got("answer", n), 15)
            check("answer yes after the queued sentence", ans is not None and fake.got("answer", n)[0].get("yes") is True
                  and fake.got("answer", n)[0].get("request") == "r4", str(fake.got("answer", n)[:1]))
            sent = wait_for(lambda: len(fake.got("say", n)) >= 2, 30)
            check("queued sentence sent after done", sent is not None and "下载" in fake.got("say", n)[1].get("text", ""),
                  str([c.get("text") for c in fake.got("say", n)]))
            fake.plan = [(0.1, "好的。")]
            wait_for(lambda: app.state == LISTENING and not app.turn_open and app.speaker.idle(), 30)

            # 5c. a tap while words are queued drops only the queue; the line goes on. Then the same by voice.
            fake.plan = [(0.3, f"第{i}句，还在说。") for i in range(1, 25)]
            n = len(fake.received)
            self.say("继续讲。")
            wait_for(lambda: app.turn_open and not app.speaker.idle(), 10)
            time.sleep(0.4)
            self.say("这句先排着。")
            wait_for(lambda: len(app.queued) == 1, 10)
            app._on_tap()
            check("tap with a queue -> queue dropped, line not interrupted",
                  not app.queued and app.turn_open and not fake.got("interrupt", n), str(app.queued))
            self.say("这句也排着。")
            wait_for(lambda: len(app.queued) == 1, 10)
            self.say("撤销排队。")
            dropped = wait_for(lambda: not app.queued, 10)
            check("'撤销排队' -> queue dropped, nothing sent", dropped is not None and len(fake.got("say", n)) == 1,
                  str([c.get("text") for c in fake.got("say", n)]))
            app._on_tap()  # now really interrupt
            wait_for(lambda: fake.got("interrupt", n), 5)
            fake.plan = [(0.1, "好的。")]
            wait_for(lambda: app.state == LISTENING and not app.turn_open and app.speaker.idle(), 30)

            # 5c2. send key double-tapped: the turn is interrupted and the queued words go out at once
            fake.plan = [(0.3, f"第{i}句，还在说。") for i in range(1, 25)]
            n = len(fake.received)
            self.say("再来一段。")
            wait_for(lambda: app.turn_open and not app.speaker.idle(), 10)
            time.sleep(0.4)
            self.say("这句马上发。")
            wait_for(lambda: len(app.queued) == 1, 10)
            fake.plan = [(0.1, "好的。")]  # a short reply to the words sent now
            app._on_send_now()
            sent = wait_for(lambda: len(fake.got("say", n)) >= 2, 10)
            check("send now -> interrupt, then the queued words sent",
                  sent is not None and fake.got("interrupt", n) and "马上" in fake.got("say", n)[1].get("text", "")
                  and not app.queued, str([c.get("text") for c in fake.got("say", n)]))
            wait_for(lambda: app.state == LISTENING and not app.turn_open and app.speaker.idle(), 30)

            # 5d. mute: speech is ignored until unmuted
            n = len(fake.received)
            app._toggle_mute()
            check("mute on", app.muted)
            self.say("这句不该被听到。")
            time.sleep(1.0)
            check("speech while muted -> nothing sent", not fake.got("say", n) and app.state == LISTENING,
                  f"{app.state} {[c.get('text') for c in fake.got('say', n)]}")
            app._toggle_mute()
            check("mute off", not app.muted)
            self.say("这句应该听到。")
            got = wait_for(lambda: fake.got("say", n), 10)
            check("speech after unmute -> sent", got is not None and "听到" in fake.got("say", n)[0].get("text", ""),
                  str([c.get("text") for c in fake.got("say", n)]))
            wait_for(lambda: app.state == LISTENING and not app.turn_open and app.speaker.idle(), 30)

            # 6. fixed phrase
            n = len(fake.received)
            self.say("回到总机。")
            got = wait_for(lambda: fake.got("phrase", n), 10)
            check("'回到总机' -> phrase back_to_manager, not say", got is not None
                  and fake.got("phrase", n)[0].get("key") == "back_to_manager" and not fake.got("say", n))
            wait_for(lambda: app.state == LISTENING and app.speaker.idle(), 10)

            # 7. settings request
            before = len(fake.replies)
            fake.emit("request", request="q1", path="get_settings", data={})
            rep = wait_for(lambda: len(fake.replies) > before, 5)
            check("request get_settings -> reply", rep is not None and fake.replies[-1].get("result", {}).get("ok") is True,
                  str(fake.replies[-1:]))

            # 8. tap during a reply = level 2 interrupt
            fake.plan = [(0.3, f"第{i}句。") for i in range(1, 9)]
            n = len(fake.received)
            self.say("再讲一遍。")
            wait_for(lambda: app.turn_open and not app.speaker.idle(), 10)
            app._on_tap()
            got = wait_for(lambda: fake.got("interrupt", n), 5)
            check("tap while speaking -> interrupt + listening", got is not None and app.state == LISTENING and not app.turn_open)
            wait_for(lambda: app.speaker.idle(), 5)

            # 9. hold = level 3 stop_all
            fake.plan = [(0.3, f"第{i}句。") for i in range(1, 9)]
            n = len(fake.received)
            self.say("再讲一遍。")
            wait_for(lambda: app.turn_open and not app.speaker.idle(), 10)
            app._on_hold()
            got = wait_for(lambda: fake.got("stop_all", n), 5)
            check("hold -> stop_all + listening", got is not None and app.state == LISTENING and not app.turn_open)
            wait_for(lambda: app.speaker.idle(), 5)

            # 10. the daemon moves us to another line
            fake._activate("l2")
            moved = wait_for(lambda: app.client.active == "l2" and app.lines["l2"].get("active"), 5)
            check("line event -> active line changed", moved is not None)
            fake.plan = [(0.1, "Atlas 这边。")]
            n = len(fake.received)
            self.say("在吗？")
            got = wait_for(lambda: fake.got("say", n), 10)
            check("speech goes to the new active line", got is not None and fake.got("say", n)[0].get("line") == "l2")
            wait_for(lambda: app.state == LISTENING and not app.turn_open and app.speaker.idle(), 20)

            # 11. exit phrase stays local
            n = len(fake.received)
            self.say("结束对话。")
            left = wait_for(lambda: not app.conversation and app.state == IDLE, 10)
            check("'结束对话' exits conversation locally", left is not None and not fake.got("say", n))

            # 12. daemon goes away
            fake.emit("bye", reason="测试结束")
            gone = wait_for(lambda: app.daemon_gone, 5)
            check("bye -> daemon_gone", gone is not None)
        except Exception as e:
            log.exception("driver crashed")
            check("driver", False, repr(e))
        finally:
            time.sleep(0.5)
            try:
                app.root.after(0, app.quit)
            except Exception:
                pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(ROOT / "frontend_harness.log", encoding="utf-8", mode="w")])
    if "--audible" not in sys.argv[1:]:
        mute_output()
    cfg = config.load()  # from the throwaway home: the test changes the font size there, nowhere else
    fake = FakeDaemon()
    fake.start()
    app = App(cfg)
    app._connect = lambda: app.client.connect_to(fake.port, TOKEN)
    driver = Driver(app, fake)
    driver.start()
    app.run()
    driver.join(timeout=30)
    print("\n==== frontend harness summary ====")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    print(f"{sum(ok for _, ok, _ in results)}/{len(results)} passed")
    sys.exit(0 if all(ok for _, ok, _ in results) else 1)


if __name__ == "__main__":
    main()
