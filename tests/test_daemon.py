import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from voiceclaude import access, config, daemon, paths
from voiceclaude.client import DaemonClient
from voiceclaude.projects import Project

from tests.test_switchboard import FakeSession


def wait_for(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class JobEscapeTests(unittest.TestCase):
    def test_in_job_is_a_bool(self):
        self.assertIn(daemon.in_job(), (True, False))

    def test_schtasks_command_and_sequence(self):
        """The scheduled task gets our launcher plus a file with every VOICECLAUDE_* variable (a scheduled task
        starts with the logon environment, so VOICECLAUDE_START_WITH etc. would otherwise be lost); never secrets."""
        calls = []

        class R:
            returncode, stdout, stderr = 0, "", ""

        kwargs = []

        def fake_run(argv, **kw):
            calls.append(argv)
            kwargs.append(kw)
            return R()
        tmp = Path(tempfile.mkdtemp())
        env = {"VOICECLAUDE_HOME": str(tmp), "VOICECLAUDE_START_WITH": "自检项目", "VOICECLAUDE_NO_HOTKEY": "1",
               "ANTHROPIC_API_KEY": "not-a-real-key", "CODEX_HOME": str(tmp / "codex-home"), "CLAUDE_CONFIG_DIR": str(tmp / "claude")}
        with mock.patch.dict("os.environ", env), mock.patch.object(paths, "LAUNCH_ENV_FILE", tmp / "launch-env.json"), \
                mock.patch.object(paths, "TASK_XML_FILE", tmp / "daemon-task.xml"), mock.patch.object(paths, "HOME_DIR", tmp):
            self.assertTrue(daemon.spawn_via_schtasks(["--no-relaunch"], run=fake_run))
        self.assertEqual([c[:2] for c in calls], [["schtasks", "/create"], ["schtasks", "/run"]])
        import locale
        self.assertEqual((kwargs[0]["encoding"], kwargs[0]["errors"]), (locale.getpreferredencoding(False), "replace"))  # GBK on a Chinese Windows
        create = calls[0]
        self.assertEqual(create[create.index("/xml") + 1], str(tmp / "daemon-task.xml"))
        self.assertIn("/f", create)
        xml = (tmp / "daemon-task.xml").read_text("utf-16")
        for needle in ("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", "<DisallowStartIfOnBatteries>false",
                       "<StopIfGoingOnBatteries>false", "<MultipleInstancesPolicy>IgnoreNew", "<Triggers />",
                       "run_daemon.py", "--env-file", "--no-relaunch</Arguments>"):
            self.assertIn(needle, xml)
        self.assertNotIn("not-a-real-key", xml)
        saved = json.loads((tmp / "launch-env.json").read_text("utf-8"))
        self.assertEqual(saved["VOICECLAUDE_START_WITH"], "自检项目")
        self.assertEqual(saved["VOICECLAUDE_HOME"], str(tmp))
        self.assertEqual(saved["VOICECLAUDE_NO_HOTKEY"], "1")
        self.assertEqual((saved["CODEX_HOME"], saved["CLAUDE_CONFIG_DIR"]), (str(tmp / "codex-home"), str(tmp / "claude")))
        self.assertNotIn("ANTHROPIC_API_KEY", saved)
        self.assertNotIn("PATH", saved)
        create = calls[0]
        name = create[create.index("/tn") + 1]
        self.assertTrue(name.startswith(daemon.TASK_NAME + "-") and len(name) == len(daemon.TASK_NAME) + 9, name)
        self.assertNotEqual(name, daemon.TASK_NAME)  # a throwaway home never touches the user's own task
        self.assertEqual(calls[1][calls[1].index("/tn") + 1], name)  # /run uses the same name
        with mock.patch.object(paths, "HOME_DIR", Path.home() / ".voice-claude"):
            self.assertEqual(daemon.task_name(), daemon.TASK_NAME)
        self.assertIn("/f", create)

    def test_remove_task_deletes_only_a_throwaway_homes_task(self):
        import subprocess
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(paths, "HOME_DIR", Path(tempfile.mkdtemp())):
            name = daemon.task_name()
            self.assertTrue(daemon.remove_task(run=run))
        self.assertTrue(name.startswith("voice-claude-daemon-"))
        self.assertEqual(calls, [["schtasks", "/query", "/tn", name], ["schtasks", "/delete", "/tn", name, "/f"]])
        calls.clear()
        with mock.patch.object(paths, "HOME_DIR", Path.home() / ".voice-claude"):
            self.assertFalse(daemon.remove_task(run=run))  # the user's own task: never
        self.assertEqual(calls, [])
        with mock.patch.object(paths, "HOME_DIR", Path(tempfile.mkdtemp())):
            self.assertFalse(daemon.remove_task())  # the real schtasks: refused under the test runner

    def test_schtasks_xml_refused_falls_back_to_sc_once(self):
        """Some machines may refuse /create /xml: then the old /sc once registration is used, with a warning."""
        calls = []

        class R:
            stdout, stderr = "", ""

            def __init__(self, rc):
                self.returncode = rc

        def fake_run(argv, **kw):
            calls.append(argv)
            return R(1 if "/xml" in argv else 0)
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict("os.environ", {"VOICECLAUDE_HOME": str(tmp)}), \
                mock.patch.object(paths, "LAUNCH_ENV_FILE", tmp / "launch-env.json"), \
                mock.patch.object(paths, "TASK_XML_FILE", tmp / "daemon-task.xml"), \
                self.assertLogs(daemon.log, level="WARNING") as logs:
            self.assertTrue(daemon.spawn_via_schtasks(["--no-relaunch"], run=fake_run))
        self.assertEqual([c[:2] for c in calls], [["schtasks", "/create"], ["schtasks", "/create"], ["schtasks", "/run"]])
        once = calls[1]
        self.assertEqual(once[once.index("/sc") + 1], "once")
        tr = once[once.index("/tr") + 1]
        self.assertIn("run_daemon.py", tr)
        self.assertIn("--env-file", tr)
        self.assertTrue(tr.endswith("--no-relaunch"))
        self.assertTrue(any("72 hours" in line for line in logs.output))

    def test_in_host_job_asks_about_the_venv_launcher(self):
        """Under the venv redirector we are always in the launcher's own job; what matters is the launcher's."""
        with mock.patch.object(daemon, "venv_launcher_pid", return_value=4242), \
                mock.patch.object(daemon, "_pid_in_job", return_value=False), \
                mock.patch.object(daemon, "in_job", return_value=True):
            self.assertFalse(daemon.in_host_job())
            self.assertEqual(daemon.describe_job(), "only in the venv launcher's own job (harmless)")
        with mock.patch.object(daemon, "venv_launcher_pid", return_value=4242), \
                mock.patch.object(daemon, "_pid_in_job", return_value=True), \
                mock.patch.object(daemon, "in_job", return_value=True):
            self.assertTrue(daemon.in_host_job())
            self.assertIn("Task Scheduler", daemon.describe_job(relaunched=True))
        with mock.patch.object(daemon, "venv_launcher_pid", return_value=None), \
                mock.patch.object(daemon, "in_job", return_value=False):
            self.assertFalse(daemon.in_host_job())
            self.assertEqual(daemon.describe_job(), "not in any job")

    def test_venv_launcher_is_our_parent(self):
        base = getattr(sys, "_base_executable", None)
        if not base or os.path.normcase(base) == os.path.normcase(sys.executable):
            self.skipTest("not running under the venv redirector")
        self.assertEqual(daemon.venv_launcher_pid(), os.getppid())
        self.assertTrue(daemon.in_job())  # the launcher's own job: silent breakaway, kill on close
        self.assertIn(daemon._pid_in_job(os.getppid()), (True, False))

    def test_launcher_restores_the_environment(self):
        """scripts/run_daemon.py --env-file puts VOICECLAUDE_HOME back before the daemon looks for daemon.json.
        --status only reads that file, so this starts nothing."""
        import subprocess
        tmp = Path(tempfile.mkdtemp())
        (tmp / "daemon.json").write_text(json.dumps({"pid": os.getpid(), "port": 1, "started": "t"}), "utf-8")
        (tmp / "launch-env.json").write_text(json.dumps({"VOICECLAUDE_HOME": str(tmp)}), "utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("VOICECLAUDE_")}
        r = subprocess.run([sys.executable, "-X", "utf8", str(daemon.LAUNCHER), "--env-file", str(tmp / "launch-env.json"),
                            "--status"], capture_output=True, text=True, encoding="utf-8", env=env,
                           creationflags=0x08000000, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"pid {os.getpid()}", r.stdout)

    def test_schtasks_failure_is_reported(self):
        class R:
            returncode, stdout, stderr = 1, "", "denied"
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(paths, "HOME_DIR", tmp), mock.patch.object(paths, "LAUNCH_ENV_FILE", tmp / "launch-env.json"),                 mock.patch.object(paths, "TASK_XML_FILE", tmp / "daemon-task.xml"):  # never the user's own files
            self.assertFalse(daemon.spawn_via_schtasks([], run=lambda *a, **k: R()))


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("HOME_DIR", "MANAGER_DIR", "MANAGER_LOG_DIR", "SCRATCH_DIR", "JOBS_DIR", "LINES_DIR"):
            mock.patch.object(paths, name, self.tmp / name.lower()).start()
        for name in ("LINES_FILE", "PROJECTS_FILE", "JOBS_FILE", "DAEMON_INFO", "DAEMON_LOG", "AUTH_FILE"):
            mock.patch.object(paths, name, self.tmp / name.lower()).start()
        mock.patch("voiceclaude.switchboard.config_save", lambda *a, **k: None).start()
        mock.patch.object(access, "check_reach", lambda pid, cfg=None: (True, "")).start()  # no network in tests
        (self.tmp / "atlas").mkdir()
        projects = [Project(key="atlas", name="Atlas", display_name="Atlas", cwd=str(self.tmp / "atlas"))]
        cfg = config.Config()
        self.server, self.sb, self.jobs, self.control = daemon.build(cfg, session_factory=FakeSession,
                                                                      detect_access=lambda cmd: access.AccessStatus(True, "subscription", "测试"),
                                                                      scan_projects=lambda: list(projects), ask_timeout=0.5)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sb.start()
        mock.patch.object(daemon, "read_info", lambda: {"port": self.server.port, "token": self.server.token, "pid": 1}).start()
        mock.patch.object(daemon, "spawn_detached", lambda: None).start()
        self.events = []

    def tearDown(self):
        self.server.shutdown()
        # a shutdown a test started runs on its own thread: let it finish while the temporary paths are still in
        # place, or its last writes and the daemon.json removal land in the user's real ~/.voice-claude
        self.assertTrue(self.server.finished.wait(15), "the service did not finish shutting down")
        mock.patch.stopall()

    def client(self):
        def on_event(k, d):
            self.events.append((k, d))
            if k == "ask" and d.get("action") == "switch_to":  # the manager asks before every hand-off
                threading.Thread(target=lambda: c.answer(d["request"], True), daemon=True).start()
        c = DaemonClient(on_event)
        c.connect()
        return c

    def test_hello_and_status(self):
        c = self.client()
        self.assertEqual(c.active, "manager")
        self.assertEqual(self.sb.frontend_pid, os.getpid())  # the window's process: the desktop tools never type into it
        self.assertEqual(self.sb.control_request("manager", "frontend_pid", {})["pid"], os.getpid())
        self.assertEqual({l["line"] for l in c.info["lines"]}, {"manager"})
        s = c.status()
        self.assertTrue(s["ok"])
        self.assertEqual(s["active"], "manager")
        c.close()

    def test_providers_round_trip(self):
        c = self.client()
        p = c.list_providers()
        self.assertTrue(p["ok"])
        self.assertEqual((p["default"], [x["id"] for x in p["providers"]]), ("claude", ["claude", "deepseek", "kimi", "openai"]))
        self.assertTrue(p["providers"][0]["ok"] and p["providers"][0]["is_default"])
        self.assertFalse(c.switch_provider("gemini")["ok"])
        self.assertFalse(c.set_provider_key("deepseek", "abc")["ok"])  # bad shape, nothing saved
        self.assertFalse(c.login_provider("kimi")["ok"])  # key-only vendor
        self.assertFalse(c.logout_provider("deepseek")["ok"])
        self.assertEqual(c.status()["access"]["provider"], "claude")
        c.close()

    def test_a_failing_command_never_logs_the_key(self):
        c = self.client()
        with mock.patch.object(self.sb, "set_provider_key", side_effect=OSError("disk full")), \
                self.assertLogs(daemon.log, level="ERROR") as logs:
            r = c.set_provider_key("deepseek", "sk-SECRETKEY-0123456789abcdef")
        self.assertFalse(r["ok"])
        self.assertIn("出错了", r["message"])
        self.assertNotIn("SECRETKEY", "\n".join(logs.output))
        self.assertIn("set_provider_key", "\n".join(logs.output))
        c.close()

    def test_bad_token_rejected(self):
        sock = socket.create_connection(("127.0.0.1", self.server.port))
        f = sock.makefile("rw", encoding="utf-8", newline="\n")
        f.write(json.dumps({"hello": "wrong"}) + "\n")
        f.flush()
        self.assertFalse(json.loads(f.readline())["ok"])
        self.assertEqual(f.readline(), "")  # closed
        sock.close()
        with self.assertRaises(ConnectionError):
            DaemonClient(lambda k, d: None).connect_to(self.server.port, "wrong")

    def test_events_and_switch(self):
        c = self.client()
        r = self.sb.control_request("manager", "switch_to", {"project": "Atlas"})  # what the manager's tool does
        self.assertTrue(r["ok"])
        self.assertTrue(wait_for(lambda: c.active == r["line"]))
        self.assertTrue(wait_for(lambda: any(k == "notice" and d["text"] == "接到Atlas项目" for k, d in list(self.events))))
        line = self.sb.lines[r["line"]]
        c.say(c.active, "你好")
        self.assertEqual(line.session.sent, ["你好"])
        line.on_session("text", "在呢。")
        line.on_session("done", {"status": "ok", "message": "在呢。"})
        self.assertTrue(wait_for(lambda: any(k == "done" and d["line"] == line.id for k, d in self.events)))
        self.assertTrue(any(k == "text" and d["text"] == "在呢。" for k, d in self.events))
        c.close()

    def test_request_reply_roundtrip(self):
        c = self.client()

        def handler(kind, data):
            self.events.append((kind, data))
            if kind == "request":
                c.reply(data["request"], {"ok": True, "message": f"字号已改成 {data['data']['size']}"})
        c.on_event = handler
        r = self.sb.control_request("manager", "set_font_size", {"size": 15})
        self.assertEqual(r, {"ok": True, "message": "字号已改成 15"})
        c.close()
        r = self.sb.control_request("manager", "set_font_size", {"size": 15})  # no front end any more
        self.assertFalse(r["ok"])

    def test_ask_roundtrip_and_timeout(self):
        c = self.client()

        def handler(kind, data):
            self.events.append((kind, data))
            if kind == "ask":
                c.answer(data["request"], yes="可以" in data["question"] or data.get("action") == "switch_to")
        c.on_event = handler
        r = self.sb.control_request("manager", "switch_to", {"project": "Atlas"})
        line = r["line"]
        self.assertTrue(wait_for(lambda: c.active == line))
        ok = self.sb.control_request(line, "ask", {"action": "open_app", "question": "我想打开记事本，可以吗？"})
        self.assertTrue(ok["ok"])
        c.close()
        self.assertTrue(wait_for(lambda: self.server.frontend is None))
        no = self.sb.control_request(line, "ask", {"action": "open_app", "question": "再开一个，可以吗？"})
        self.assertFalse(no["ok"])  # nobody there to answer -> refused after the (short test) timeout

    def test_reconnect_keeps_lines(self):
        c1 = self.client()
        r = self.sb.control_request("manager", "switch_to", {"project": "Atlas"})
        self.assertTrue(wait_for(lambda: c1.active == r["line"]))
        c1.say(c1.active, "开始干活")
        c1.close()
        line = self.sb.lines[r["line"]]
        self.assertEqual(line.state, "working")
        c2 = self.client()
        self.assertEqual(c2.active, r["line"])
        self.assertIn(r["line"], {l["line"] for l in c2.info["lines"]})
        self.assertEqual(line.session.sent, ["开始干活"])
        c2.close()

    def test_second_frontend_replaces_first(self):
        c1 = self.client()
        c2 = self.client()
        self.assertTrue(wait_for(lambda: any(k == "bye" for k, d in self.events)))
        self.assertTrue(c2.status()["ok"])
        with self.assertRaises((ConnectionError, TimeoutError)):
            c1.status()
        c2.close()

    def test_phrase_and_shutdown(self):
        c = self.client()
        self.assertEqual(c.phrase("background_status")["spoken"], "后台没有东西。")
        r = c.phrase("shutdown_daemon")
        self.assertTrue(r["ok"])
        self.assertTrue(wait_for(lambda: any(k == "bye" for k, d in self.events)))
        self.assertTrue(wait_for(lambda: self.server.stopped.is_set()))
        self.assertFalse(paths.DAEMON_INFO.exists())
        self.assertTrue(wait_for(lambda: not c.connected))
        time.sleep(0.3)
        self.assertEqual([d["reason"] for k, d in self.events if k == "bye"], ["后台服务已关闭"])  # one bye, the service's

    def test_main_waits_for_shutdown_to_finish(self):
        """What main() does after stopped: wait_finished() must not return before daemon.json is gone and the
        service said its own bye (the process used to exit the same millisecond `stopped` was set)."""
        c = self.client()
        daemon.write_info(self.server, self.control.port)
        self.assertTrue(paths.DAEMON_INFO.exists())
        threading.Thread(target=self.server.shutdown, daemon=True).start()
        self.assertTrue(daemon.wait_finished(self.server, 10))
        self.assertFalse(paths.DAEMON_INFO.exists())
        self.assertTrue(wait_for(lambda: any(k == "bye" and d.get("reason") == "后台服务已关闭" for k, d in self.events)))

    def test_shutdown_keeps_or_stops_jobs_as_told(self):
        c = self.client()
        self.assertFalse(c.shutdown(jobs="maybe")["ok"])
        job = self.jobs.start("ping -n 30 127.0.0.1 >nul", str(self.tmp), "manager", "慢活")
        try:
            r = c.shutdown(jobs="keep")
            self.assertTrue(r["ok"], r)
            self.assertIn("留着跑", r["message"])
            self.assertTrue(wait_for(lambda: self.server.stopped.is_set()))
            self.assertEqual(self.jobs.get(job.id).state, "running")  # not touched
        finally:
            self.jobs.cancel(job.id)

    def test_shutdown_stop_cancels_jobs_first(self):
        c = self.client()
        job = self.jobs.start("ping -n 30 127.0.0.1 >nul", str(self.tmp), "manager", "慢活")
        r = c.shutdown(jobs="stop")
        self.assertTrue(r["ok"], r)
        self.assertIn("停掉 1 个", r["message"])
        self.assertEqual(self.jobs.get(job.id).state, "cancelled")
        self.assertTrue(wait_for(lambda: self.server.stopped.is_set()))
        self.assertTrue(any(k == "notice" and "停掉" in d.get("text", "") for k, d in self.events))


if __name__ == "__main__":
    unittest.main()
