"""Self test: the real front end + a REAL temporary background service + real Claude, without a microphone or
the hotkey. Speech is synthesised with the TTS voice and fed in as if from the mic; the hotkey is disabled and
taps are simulated by calling the app's handlers, so it never touches a voice-claude you are using. Everything
the app says is played as silence of the same length (--audible plays it on the configured speaker).

The daemon it starts lives in a throwaway VOICECLAUDE_HOME and is shut down at the end; your real
~/.voice-claude is never touched. Run: .venv\\Scripts\\python -X utf8 scripts\\selftest.py   (about 5 minutes)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
HOME = Path(tempfile.mkdtemp(prefix="vc-selftest-"))
os.environ["VOICECLAUDE_HOME"] = str(HOME)
os.environ["VOICECLAUDE_NO_HOTKEY"] = "1"

import logging  # noqa: E402

from frontend_harness import Driver as BaseDriver, check, mute_output, results, wait_for  # noqa: E402
from voiceclaude import config, daemon, paths, projects  # noqa: E402
from voiceclaude.jobs import pid_running  # noqa: E402
from voiceclaude.app import APPROVING, App, BUSY, IDLE, LISTENING, RECORDING  # noqa: E402

log = logging.getLogger("selftest")
PROJECT = "自检项目"
events: list[tuple[str, dict]] = []
ev_lock = threading.Lock()


def find(kind, pred=lambda d: True):
    with ev_lock:
        return [d for k, d in events if k == kind and pred(d)]


class Driver(BaseDriver):
    def __init__(self, app: App):
        threading.Thread.__init__(self, daemon=False)
        self.app, self.fake = app, None

    def run(self):
        app = self.app
        try:
            ok = wait_for(lambda: app.ready, 120)
            check("app ready with a temporary daemon", ok is not None, f"{ok and round(ok, 1)}s")
            if ok is None:
                return
            app.mic.stop()
            greeted = wait_for(lambda: find("notice"), 30)
            check("daemon greeted us", greeted is not None, str([d.get("text") for d in find("notice")][:2]))
            check("started on the test project line", app.client.active.startswith("p-"), app.client.active)
            wait_for(lambda: app.speaker.idle(), 20)

            # 1. a normal turn with real Claude
            app._on_double()
            check("double tap -> listening", app.state == LISTENING and app.conversation)
            self.say_and_wait("今天星期几？只回答星期几。", LISTENING)
            # the same question again: the first turn on a fresh line pays for Claude Code + MCP start-up,
            # the second shows the warm latency (about 3 s)
            self.say_and_wait("再说一次，今天星期几？只回答星期几。", LISTENING)

            # 2. talking over a long reply: duck / cut / queue; the line is NOT interrupted
            n_done = len(find("done"))
            self.say("帮我解释一下光合作用是怎么回事，说详细一点。")
            spoke = wait_for(lambda: app.turn_open and not app.speaker.idle(), 60)
            check("claude started speaking", spoke is not None, f"{spoke and round(spoke, 1)}s")
            time.sleep(0.5)
            self.push(self.speech("等一下，我还有一个问题想先问你，就是关于那个下载的事情。"), pace=0.004)
            ducked, cut = app.player.ducked, app.cut
            self.silence()
            check("level-1: voice ducked/cut", ducked or cut, f"ducked={ducked} cut={cut}")
            queued = wait_for(lambda: bool(app.queued), 15)
            check("your words queued, line still working", queued is not None and app.turn_open, str(app.queued))
            finished = wait_for(lambda: len(find("done", lambda d: d.get("status") == "ok")) >= n_done + 2, 180)
            check("queued words answered after the first reply", finished is not None)
            wait_for(lambda: app.state == LISTENING and not app.turn_open and app.speaker.idle(), 60)

            # 3. settings by voice (request event round trip)
            font0 = app.font_size
            self.say_and_wait("把窗口的字号放大两个号。", LISTENING, timeout=90)
            check("font size +2 by voice", app.font_size == font0 + 2, f"{font0} -> {app.font_size}")

            # 4. a permission change is confirmed out loud; after 'allow' the model switch does not ask
            n_ask = len(find("ask"))
            self.say("把切换模型的权限改成不用问。")
            asked = wait_for(lambda: app.state == APPROVING, 90)
            check("set_permission asks for approval", asked is not None)
            self.push(self.speech("可以。"))
            self.silence(0.8)
            wait_for(lambda: app.state == LISTENING and not app.turn_open and app.speaker.idle(), 90)
            self.say_and_wait("把模型切换成 sonnet。", LISTENING, timeout=120)
            later_asks = [d for d in find("ask")[n_ask:] if d.get("action") == "switch_model"]
            check("after 'allow', switch_model did not ask", not later_asks, str([d.get("question") for d in later_asks]))

            # 5. a background job: asked first, then runs detached and reports when done
            # Spoken in words: "-n 20" does not survive speech recognition ("pink and"), and a mis-heard command
            # once became `ping -t` (never ends). The approval below refuses anything without a bounded -n.
            self.say("用 run_in_background 工具，在后台 ping 本机二十次，也就是 ping 127.0.0.1 二十次，说明写“自检任务”。"
                     "启动后告诉我任务编号就行，不用等它。")
            asked = wait_for(lambda: app.state == APPROVING and find("ask", lambda d: d.get("action") == "background_job"), 120)
            question = (find("ask", lambda d: d.get("action") == "background_job") or [{}])[-1].get("question", "")
            check("background job asks first", asked is not None, question)
            bounded = "-n" in question and "-t" not in question
            check("the proposed command ends by itself (-n, no -t)", bounded, question)
            self.push(self.speech("可以。" if bounded else "不行。"))
            self.silence(0.8)
            running = wait_for(lambda: find("job", lambda d: d.get("state") == "running"), 90) if bounded else None
            check("job reported running", running is not None, "" if bounded else "refused an unbounded command instead")
            wait_for(lambda: app.state == LISTENING and not app.turn_open and app.speaker.idle(), 120)
            done = wait_for(lambda: find("job", lambda d: d.get("state") == "done"), 120)
            check("job finished and was announced", done is not None and wait_for(lambda: any("做完" in d.get("text", "") for d in find("notice")), 15) is not None)
            wait_for(lambda: app.speaker.idle(), 30)

            # 6. back to the manager and hand-off back to the project
            self.say("回到总机。")
            back = wait_for(lambda: app.client.active == "manager" and app.state == LISTENING and app.speaker.idle(), 30)
            check("'回到总机' -> manager line", back is not None, app.client.active)
            self.say(f"接我到{PROJECT}。")
            # the manager asks "要接到 X 项目吗？" before every hand-off; answer it
            asked = wait_for(lambda: app.state == APPROVING, 90)
            check("manager asks before handing over", asked is not None)
            self.push(self.speech("可以。"))
            self.silence(0.8)
            handed = wait_for(lambda: app.client.active.startswith("p-"), 120)
            check("manager handed us back to the project", handed is not None, app.client.active)
            wait_for(lambda: app.state == LISTENING and not app.turn_open and app.speaker.idle(), 90)

            # 7. tap = interrupt this turn; hold = emergency stop
            self.say("从一数到五十，每个数字单独一行，不要说别的。")
            wait_for(lambda: app.turn_open and not app.speaker.idle(), 60)
            app._on_tap()
            check("tap while speaking -> listening, turn closed", app.state == LISTENING and not app.turn_open)
            wait_for(lambda: app.speaker.idle(), 10)
            time.sleep(1.0)
            self.say("从一数到五十，每个数字单独一行，不要说别的。")
            wait_for(lambda: app.turn_open and not app.speaker.idle(), 60)
            app._on_hold()
            stopped = wait_for(lambda: any("紧急停止" in d.get("text", "") for d in find("notice")), 15)
            check("hold -> emergency stop announced", stopped is not None and app.state == LISTENING)
            wait_for(lambda: app.speaker.idle(), 15)

            # 8. exit phrase, then one-shot mode
            self.say("结束对话。")
            left = wait_for(lambda: not app.conversation and app.state == IDLE, 20)
            check("'结束对话' exits conversation", left is not None)
            app._on_tap()
            check("tap -> recording", app.state == RECORDING)
            self.push(self.speech("今天星期几？只回答星期几。"))
            app._on_tap()
            check("tap -> busy", app.state == BUSY)
            done = wait_for(lambda: app.state == IDLE and app.speaker.idle() and not app.turn_open, 90)
            check("one-shot turn finished -> idle", done is not None, f"{done and round(done, 1)}s")

            # Tool definitions are preloaded (ENABLE_TOOL_SEARCH=false), so no turn should spend a round trip on
            # ToolSearch first; the tool turns above (font size, model switch) should take about 3–7 s.
            searches = find("tool", lambda d: "ToolSearch" in str(d.get("name", "")))
            check("no ToolSearch round trips (tools preloaded)", not searches, f"{len(searches)} seen")

            # 9. shut the temporary daemon down. With jobs still running it first asks "要一起停吗？"; say 可以.
            def _shutdown():
                try:
                    app.client.shutdown()
                except Exception as e:
                    log.warning("shutdown: %s", e)
            threading.Thread(target=_shutdown, daemon=True).start()
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 30 and not app.daemon_gone:
                if app.state == APPROVING:
                    self.push(self.speech("可以。"))
                    self.silence(0.8)
                time.sleep(0.2)
            bye = wait_for(lambda: app.daemon_gone, 5)
            check("daemon shut down (bye)", bye is not None and wait_for(lambda: daemon.read_info() is None, 20) is not None)
        except Exception as e:
            log.exception("driver crashed")
            check("driver", False, repr(e))
        finally:
            time.sleep(0.5)
            try:
                app.root.after(0, app.quit)
            except Exception:
                pass

    def say_and_wait(self, text, want_state_after, timeout=120):
        app = self.app
        n = len(find("done"))
        t_say = []
        orig_say = app.speaker.say
        app.speaker.say = lambda s: (t_say.append(time.perf_counter()), orig_say(s))
        self.say(text)
        t0 = time.perf_counter()
        done = wait_for(lambda: len(find("done")) > n and app.state == want_state_after and not app.turn_open
                        and app.speaker.idle(), timeout)
        check(f"'{text[:12]}…' answered -> {want_state_after}", done is not None, f"{done and round(done, 1)}s"
              + (f"; first sentence {t_say[0] - t0:.2f}s after end of speech" if t_say else ""))
        app.speaker.say = orig_say


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(ROOT / "selftest.log", encoding="utf-8", mode="w")])
    print("自检运行约 5 分钟。快捷键已禁用，不会碰你正在用的 voice-claude；说话放成静音。")
    if "--audible" not in sys.argv[1:]:
        mute_output()
    config_path = config.user_config_path()  # under the temporary VOICECLAUDE_HOME: copied from the repo on first load
    saved = config_path.read_text("utf-8") if config_path.exists() else None
    project_dir = HOME / PROJECT
    project_dir.mkdir(parents=True)
    paths.ensure()
    projects.remember_alias(paths.PROJECTS_FILE, PROJECT, str(project_dir))
    os.environ["VOICECLAUDE_START_WITH"] = PROJECT  # the daemon is our child and inherits this; config.toml stays untouched
    cfg = config.load()
    app = App(cfg)
    orig = app.client.on_event

    def record(kind, data):
        with ev_lock:
            events.append((kind, dict(data)))
        if kind in ("notice", "ask", "line", "done", "job", "bye"):
            log.info("EVENT %s %s", kind, str(data)[:200])
        orig(kind, data)

    app.client.on_event = record
    driver = Driver(app)
    driver.start()
    try:
        app.run()
        driver.join(timeout=60)
    finally:
        if saved is not None:
            config_path.write_text(saved, "utf-8")
        # Background jobs survive the daemon on purpose; a test must never leave one behind (an unbounded
        # ping once kept running hidden after the test). Kill every job this run started.
        try:
            jobs = json.loads(paths.JOBS_FILE.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            jobs = {}
        for jid, rec in jobs.items():
            pid = int(rec.get("pid") or 0)
            if pid and pid_running(pid):
                log.info("killing leftover job %s (pid %d): %s", jid, pid, rec.get("command", ""))
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        info = daemon.read_info()
        if info:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(info["pid"])], capture_output=True)
        timing = []
        try:
            lines = (HOME / "daemon.log").read_text("utf-8", errors="replace").splitlines()
            log.info("daemon.log tail:\n%s", "\n".join(lines[-30:]))
            timing = [l for l in lines if "claude ready" in l or "first text" in l]
        except OSError:
            pass
        daemon.remove_task()  # the Task Scheduler route registers one per throwaway home
        shutil.rmtree(HOME, ignore_errors=True)
    print("\n==== selftest summary ====")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    print(f"{sum(ok for _, ok, _ in results)}/{len(results)} passed")
    if timing:
        print("\n==== daemon timing (line start-up and first text per turn) ====")
        print("\n".join(timing))
    sys.exit(0 if all(ok for _, ok, _ in results) else 1)


if __name__ == "__main__":
    main()
