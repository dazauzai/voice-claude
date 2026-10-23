"""The background service: a separate, detached process that owns the lines (Claude Code conversations), the
switchboard and the background jobs. The voice front end connects to it over a local TCP socket (JSON lines) and
can come and go without disturbing anything. Protocol: docs/protocol.md.

Run: pythonw -X utf8 -m voiceclaude.daemon        (the front end starts it automatically)
     python  -X utf8 -m voiceclaude.daemon --status
"""
from __future__ import annotations

import argparse
import json
import locale
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from . import config, paths, testing
from .control import ControlServer
from .jobs import Jobs
from .switchboard import Switchboard

log = logging.getLogger(__name__)

PROTOCOL = 1
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
FORWARD_TIMEOUT = 15.0


def read_info() -> dict | None:
    """daemon.json if a daemon with that pid is still running."""
    try:
        info = json.loads(paths.DAEMON_INFO.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    from .jobs import pid_running
    if not isinstance(info, dict) or not pid_running(int(info.get("pid", 0))):
        return None
    return info


TASK_NAME = "voice-claude-daemon"


def task_name() -> str:
    """The scheduled task's name: the plain one for the user's own ~/.voice-claude, a hashed suffix for any other
    VOICECLAUDE_HOME (a self-test's throwaway home must never overwrite or /run the user's task)."""
    import hashlib
    default = Path.home() / ".voice-claude"
    try:
        same = paths.HOME_DIR.resolve() == default.resolve()
    except OSError:
        same = paths.HOME_DIR == default
    if same:
        return TASK_NAME
    return f"{TASK_NAME}-{hashlib.sha1(str(paths.HOME_DIR).lower().encode('utf-8')).hexdigest()[:8]}"
LAUNCHER = config.ROOT / "scripts" / "run_daemon.py"


def _pythonw() -> str:
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pythonw if os.path.exists(pythonw) else sys.executable


def in_job(handle: int | None = None) -> bool:
    """Is this process (or the given process handle) inside a Windows job object? Terminals embedded in other
    apps (Claude Desktop's panel, some IDEs) put their children in a job that is killed when the app closes."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        h = handle if handle is not None else k32.GetCurrentProcess()
        inside = wintypes.BOOL()
        if not k32.IsProcessInJob(wintypes.HANDLE(h), None, ctypes.byref(inside)):
            return False
        return bool(inside.value)
    except Exception:
        return False


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _pid_in_job(pid: int) -> bool | None:
    """in_job() for another process we may only look at; None when it cannot be opened."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            return in_job(h)
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None


def venv_launcher_pid() -> int | None:
    """The venv redirector (.venv\\Scripts\\python.exe) that started this interpreter, if there is one. Python's
    launcher runs the real interpreter inside a job object of its own (silent breakaway, kill on close), so every
    process started through a venv answers "in a job" even when double-clicked from Explorer. That job is not one
    to escape from: it only ties us to the launcher, which lives as long as we do."""
    base = getattr(sys, "_base_executable", None)
    if not base or os.path.normcase(base) == os.path.normcase(sys.executable):
        return None
    return os.getppid()


def in_host_job() -> bool:
    """Are we in a job that belongs to whoever launched us: an embedded terminal's host app (which kills the job
    when it closes), or the Task Scheduler after a relaunch? Under the venv launcher the question is whether the
    launcher itself sits in such a job, since we are always in the launcher's own one."""
    launcher = venv_launcher_pid()
    if launcher is None:
        return in_job()
    inside = _pid_in_job(launcher)
    return in_job() if inside is None else inside


def describe_job(relaunched: bool = False) -> str:
    """For the log: which kind of job we are in, so nobody has to wonder about 'in job: True' again."""
    if not in_job():
        return "not in any job"
    if not in_host_job():
        return "only in the venv launcher's own job (harmless)"
    return "in the launching process's job (the Task Scheduler's, after a relaunch)" if relaunched else \
        "in the launching app's job"


FORWARDED_ENV_PREFIXES = ("VOICECLAUDE_",)
FORWARDED_ENV_NAMES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME")  # where Claude Code / Codex keep their logins and records
SCHTASKS_TR_LIMIT = 261  # Windows caps the /tr command line of a scheduled task at this many characters


def forwarded_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """What the Task Scheduler route has to carry over by hand, since a scheduled task starts with the user's
    logon environment, not ours: our own switches (VOICECLAUDE_HOME, VOICECLAUDE_START_WITH, VOICECLAUDE_NO_HOTKEY,
    ...) and where Claude Code and Codex keep their records (a "new user" test points both at empty folders).
    Never secrets: the daemon reads the API key from auth.json
    itself, and a key that only lives in the launching shell is deliberately not forwarded."""
    src = os.environ if environ is None else environ
    return {k: v for k, v in src.items() if k.startswith(FORWARDED_ENV_PREFIXES) or k in FORWARDED_ENV_NAMES}


def write_launch_env(path: Path | None = None) -> Path:
    path = path or paths.LAUNCH_ENV_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(forwarded_env(), ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def schtasks_action(extra_args: list[str], env_file: Path | None = None) -> tuple[str, str]:
    """What the scheduled task runs: pythonw, and its arguments (our launcher script + the file holding the
    environment to restore, since schtasks cannot pass environment variables)."""
    argv = ["-X", "utf8", str(LAUNCHER), "--env-file", str(env_file or paths.LAUNCH_ENV_FILE)] + list(extra_args)
    return _pythonw(), subprocess.list2cmdline(argv)


def schtasks_command(extra_args: list[str], env_file: Path | None = None) -> str:
    """The same as one /tr command line, for the fallback route (Windows caps /tr at 261 characters, which is why
    the environment travels in a file rather than as --env NAME=VALUE pairs)."""
    command, arguments = schtasks_action(extra_args, env_file)
    return subprocess.list2cmdline([command]) + " " + arguments


# The task definition. The schtasks defaults would kill the daemon: "stop the task if it runs longer than 72 hours"
# (the Task Scheduler then terminates the task's whole process tree), "do not start on battery" and "stop when
# switching to battery". No trigger: the task is only ever started by /run. Priority 5 = a normal process (the
# default 7 is below normal).
TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>voice-claude 后台服务的跳板：只由前台 /run 启动，自己不会触发；留着没事。</Description>
  </RegistrationInfo>
  <Triggers />
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def task_xml(command: str, arguments: str) -> str:
    from xml.sax.saxutils import escape
    return TASK_XML.format(command=escape(command), arguments=escape(arguments))


def write_task_xml(command: str, arguments: str, path: Path | None = None) -> Path:
    path = path or paths.TASK_XML_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(task_xml(command, arguments), encoding="utf-16")  # BOM + UTF-16, as the declaration says
    return path


def _schtasks(run, argv: list[str]):
    """One schtasks call, hidden and with no console input; None when it could not even be run. schtasks writes
    in the system code page (GBK on a Chinese Windows), so decode with it, never with UTF-8 (a Chinese line would otherwise raise
    in subprocess's reader thread and the output would be lost)."""
    try:
        return run(argv, capture_output=True, text=True, encoding=locale.getpreferredencoding(False), errors="replace",
                   creationflags=0x08000000, stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        log.error("%s failed to run: %s", " ".join(argv[:3]), e)
        return None


def spawn_via_schtasks(extra_args: list[str] | None = None, run=subprocess.run) -> bool:
    """Start the daemon through the Task Scheduler service, which is outside every job object. The task is
    registered from TASK_XML (no time limit, no battery rules, no trigger: only /run starts it) and overwritten
    next time (/f). Where /xml is refused we fall back to /sc once, whose defaults stop the task after 72 hours
    and on battery power, and say so in the log."""
    extra_args = list(extra_args or [])
    if run is subprocess.run and testing.under_test_runner():
        log.error("refused: %s", testing.REFUSAL)
        return False
    try:
        env_file = write_launch_env()
        command, arguments = schtasks_action(extra_args, env_file)
        xml_file = write_task_xml(command, arguments)
    except OSError as e:
        log.error("could not write the launch files under %s: %s", paths.HOME_DIR, e)
        return False
    name = task_name()
    r = _schtasks(run, ["schtasks", "/create", "/tn", name, "/xml", str(xml_file), "/f"])
    if r is None:
        return False
    if r.returncode != 0:
        log.warning("schtasks /create /xml refused (%s %s); falling back to /sc once, whose defaults stop the task "
                    "after 72 hours and on battery power", r.stdout.strip(), r.stderr.strip())
        tr = schtasks_command(extra_args, env_file)
        if len(tr) > SCHTASKS_TR_LIMIT:
            log.warning("schtasks /tr is %d characters, over the %d limit; a shorter install path would fix this",
                        len(tr), SCHTASKS_TR_LIMIT)
        r = _schtasks(run, ["schtasks", "/create", "/tn", name, "/tr", tr, "/sc", "once", "/st", "00:00", "/f"])
        if r is None or r.returncode != 0:
            if r is not None:
                log.error("schtasks /create failed: %s %s", r.stdout, r.stderr)
            return False
    r = _schtasks(run, ["schtasks", "/run", "/tn", name])
    if r is None or r.returncode != 0:
        if r is not None:
            log.error("schtasks /run failed: %s %s", r.stdout, r.stderr)
        return False
    return True


def remove_task(run=subprocess.run) -> bool:
    """Delete this home's scheduled task, so a self test's throwaway home does not leave one behind each run. Never
    the user's own task: only a hashed name (task_name() for a non-default VOICECLAUDE_HOME) is ever deleted."""
    name = task_name()
    if name == TASK_NAME:
        return False
    if run is subprocess.run and testing.under_test_runner():
        log.error("refused: %s", testing.REFUSAL)
        return False
    q = _schtasks(run, ["schtasks", "/query", "/tn", name])
    if q is None or q.returncode != 0:
        return False  # there is no such task (the plain detached route was used)
    r = _schtasks(run, ["schtasks", "/delete", "/tn", name, "/f"])
    return r is not None and r.returncode == 0


def spawn_detached() -> None:
    """Start the daemon as its own process: no console, own process group, outside any job object.

    Normal launch (run.bat from Explorer, a plain terminal): a detached child is enough (the venv launcher's own
    job does not count, see in_host_job). From inside a host app's job (embedded terminals), the child inherits
    the job and dies with the host app, so we try to break away and, if the job forbids that, go through the Task
    Scheduler instead."""
    if testing.under_test_runner():
        raise RuntimeError(testing.REFUSAL)
    args = [_pythonw(), "-X", "utf8", "-m", "voiceclaude.daemon"]
    kw = dict(cwd=str(config.ROOT), close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL)
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    if not in_host_job():
        subprocess.Popen(args, creationflags=flags, **kw)
        return
    try:
        proc = subprocess.Popen(args, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
        if not in_job(int(proc._handle)):  # type: ignore[attr-defined]
            log.info("daemon spawned with breakaway from our job")
            return
        log.warning("breakaway did not take; the daemon would die with this app")
        proc.kill()
    except OSError as e:
        log.warning("breakaway spawn refused (%s); using the Task Scheduler", e)
    if not spawn_via_schtasks(["--no-relaunch"]):
        log.error("could not start the daemon outside our job; starting it as a plain child")
        subprocess.Popen(args + ["--no-relaunch"], creationflags=flags, **kw)  # it would only fail the same way


class Frontend:
    """The one connected front end."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.wfile = sock.makefile("w", encoding="utf-8", newline="\n")
        self.lock = threading.Lock()
        self.alive = True

    def send(self, obj: dict) -> bool:
        with self.lock:
            if not self.alive:
                return False
            try:
                self.wfile.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self.wfile.flush()
                return True
            except (OSError, ValueError):
                self.alive = False
                return False

    def close(self) -> None:
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class DaemonServer:
    def __init__(self, port: int = 0, token: str | None = None):
        self.token = token or secrets.token_hex(16)
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(2)
        self.port = self.srv.getsockname()[1]
        self.sb: Switchboard | None = None
        self.frontend: Frontend | None = None
        self.lock = threading.Lock()
        self.replies: dict[str, dict] = {}  # forwarded requests waiting for the front end's reply
        self.stopped = threading.Event()
        self.finished = threading.Event()  # shutdown() ran to its end (lines closed, files written, hook done)
        self.on_shutdown: Callable[[], None] | None = None

    # ---------- what the switchboard needs ----------

    def emit(self, kind: str, data: dict) -> None:
        fe = self.frontend
        if fe is not None:
            fe.send({"event": kind, **data})

    def forward(self, path: str, data: dict) -> dict:
        fe = self.frontend
        if fe is None:
            return {"ok": False, "message": "语音端没有连接，这件事做不了"}
        request = uuid.uuid4().hex[:8]
        slot = {"event": threading.Event(), "result": None}
        with self.lock:
            self.replies[request] = slot
        if not fe.send({"event": "request", "request": request, "path": path, "data": data}):
            return {"ok": False, "message": "语音端没有连接，这件事做不了"}
        slot["event"].wait(FORWARD_TIMEOUT)
        with self.lock:
            self.replies.pop(request, None)
        return slot["result"] if isinstance(slot["result"], dict) else {"ok": False, "message": "语音端没有响应"}

    # ---------- serving ----------

    def serve_forever(self) -> None:
        while not self.stopped.is_set():
            try:
                sock, _ = self.srv.accept()
            except OSError:
                break
            threading.Thread(target=self._serve_connection, args=(sock,), daemon=True, name="frontend").start()

    def _serve_connection(self, sock: socket.socket) -> None:
        rfile = sock.makefile("r", encoding="utf-8")
        fe = Frontend(sock)
        try:
            hello = json.loads(rfile.readline() or "{}")
        except json.JSONDecodeError:
            hello = {}
        if hello.get("hello") != self.token:
            fe.send({"ok": False, "message": "口令不对"})
            fe.close()
            return
        with self.lock:
            old, self.frontend = self.frontend, fe
        if self.sb is not None:
            self.sb.frontend_pid = int(hello.get("pid") or 0)  # the desktop tools never type into the window's process
        if old is not None:
            old.send({"event": "bye", "reason": "另一个语音端连上来了"})
            old.close()
        status = self.sb.status() if self.sb else {}
        fe.send({"ok": True, "protocol": PROTOCOL, "daemon": {"pid": os.getpid(), "port": self.port}, **status})
        log.info("frontend connected")
        if self.sb:
            greeting = self.sb.greeting()
            if greeting:
                fe.send({"event": "notice", "text": greeting})
        try:
            for line in rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    cmd = json.loads(line)
                except json.JSONDecodeError:
                    continue
                threading.Thread(target=self._run_command, args=(fe, cmd), daemon=True).start()
        except OSError:
            pass
        finally:
            with self.lock:
                if self.frontend is fe:
                    self.frontend = None
            fe.close()
            log.info("frontend disconnected")

    def _run_command(self, fe: Frontend, cmd: dict) -> None:
        cid = cmd.get("id")
        try:
            result = self.handle(cmd)
        except Exception as e:
            log.exception("command %s (id %s) failed", cmd.get("cmd"), cid)  # never the payload: it may hold a key
            result = {"ok": False, "message": f"出错了：{e}"}
        if cid is not None:
            fe.send({"id": cid, **result})

    def handle(self, cmd: dict) -> dict:
        sb = self.sb
        name = cmd.get("cmd")
        if name == "reply":
            with self.lock:
                slot = self.replies.get(str(cmd.get("request")))
            if slot:
                slot["result"] = cmd.get("result")
                slot["event"].set()
            return {"ok": True, "message": ""}
        if sb is None:
            return {"ok": False, "message": "后台服务还没准备好"}
        if name == "switch":
            return sb.switch(str(cmd.get("line", "")))
        if name == "say":
            return sb.say(str(cmd.get("line", "")), str(cmd.get("text", "")))
        if name == "interrupt":
            return sb.interrupt(str(cmd.get("line", "")))
        if name == "rewind":
            return sb.rewind(str(cmd.get("line", "") or ""), cmd.get("turn") or None, int(cmd.get("steps") or 1))
        if name == "stop_all":
            return sb.stop_all()
        if name == "answer":
            return sb.answer(str(cmd.get("request", "")), bool(cmd.get("yes")), str(cmd.get("text", "")))
        if name == "status":
            return {"ok": True, "message": "", **sb.status()}
        if name == "access_status":
            return sb.access_status()
        if name == "set_api_key":
            return sb.set_api_key(str(cmd.get("key", "")))
        if name == "clear_api_key":
            return sb.clear_api_key()
        if name == "test_access":
            return sb.test_access()
        if name == "list_providers":
            return sb.list_providers()
        if name == "set_provider_key":
            return sb.set_provider_key(str(cmd.get("provider", "")), str(cmd.get("key", "")), bool(cmd.get("switch")))
        if name == "clear_provider_key":
            return sb.clear_provider_key(str(cmd.get("provider", "")))
        if name == "test_provider":
            return sb.test_provider(str(cmd.get("provider", "")))
        if name == "switch_provider":
            return sb.switch_provider(str(cmd.get("provider", "")), str(cmd.get("line", "") or ""))
        if name == "login_provider":
            return sb.login_provider(str(cmd.get("provider", "")), str(cmd.get("mode", "browser") or "browser"))
        if name == "logout_provider":
            return sb.logout_provider(str(cmd.get("provider", "")))
        if name == "phrase":
            r = sb.phrase(str(cmd.get("key", "")))
            if r.get("shutdown"):
                threading.Thread(target=self.shutdown, daemon=True).start()
            return r
        if name == "shutdown":
            r = sb.request_shutdown(str(cmd.get("jobs", "ask") or "ask"),  # ask / stop / keep the running jobs
                                    lines_confirmed=bool(cmd.get("lines_confirmed")))  # the window asked about busy lines
            if not r.get("ok"):
                return {"ok": False, "message": str(r.get("spoken", ""))}
            if r.get("shutdown"):
                threading.Thread(target=self.shutdown, daemon=True).start()
            return {"ok": True, "message": (r.get("spoken") or "后台服务正在关闭") if r.get("shutdown") else "先问一下后台任务要不要停"}
        return {"ok": False, "message": f"不认识的命令 {name}"}

    def shutdown(self) -> None:
        if self.stopped.is_set():
            return
        self.stopped.set()
        try:
            log.info("shutting down")
            if self.sb:
                self.sb.shutdown()
            fe = self.frontend
            if fe:
                fe.send({"event": "bye", "reason": "后台服务已关闭"})
                fe.close()
            try:
                self.srv.close()
            except OSError:
                pass
            if self.on_shutdown:
                self.on_shutdown()
        finally:
            self.finished.set()


def wait_finished(server: DaemonServer, timeout: float = 30.0) -> bool:
    """shutdown() runs on a daemon thread, so main() must wait for it: otherwise the interpreter exits the moment
    `stopped` is set, before the lines are closed, lines.json saved, the bye sent and daemon.json removed
    (otherwise "shutting down" and "daemon exited" land in the same millisecond and daemon.json is left behind)."""
    if server.finished.wait(timeout):
        return True
    log.warning("shutdown did not finish within %.0f s; exiting anyway", timeout)
    return False


def write_info(server: DaemonServer, control_port: int) -> None:
    paths.ensure()
    paths.DAEMON_INFO.write_text(json.dumps({"port": server.port, "pid": os.getpid(), "token": server.token,
                                             "control_port": control_port, "protocol": PROTOCOL,
                                             "started": time.strftime("%Y-%m-%d %H:%M:%S")}), "utf-8")


def build(cfg: config.Config, *, port: int = 0, session_factory=None, scan_projects=None, ask_timeout: float = 60.0,
          detect_access=None):
    """Wire control endpoint, jobs, switchboard and server together (used by main() and by tests)."""
    server = DaemonServer(port=port)
    jobs = Jobs(paths.JOBS_DIR, paths.JOBS_FILE, lambda j: None, poll_seconds=2.0)
    jobs.load()

    def control_handler(path: str, data: dict) -> dict:
        line_id = str(data.pop("line", "") or "")
        return server.sb.control_request(line_id, path, data) if server.sb else {"ok": False, "message": "还没准备好"}

    control = ControlServer(control_handler)
    kwargs = {"jobs": jobs, "emit": server.emit, "forward": server.forward, "control_port": control.port,
              "ask_timeout": ask_timeout}
    if session_factory:
        kwargs["session_factory"] = session_factory
    if scan_projects:
        kwargs["scan_projects"] = scan_projects
    if detect_access:
        kwargs["detect_access"] = detect_access
    sb = Switchboard(cfg, **kwargs)
    jobs.on_change = sb.on_job_change
    sb.shutdown_hook = server.shutdown
    server.sb = sb

    info_path = paths.DAEMON_INFO  # the file this service writes; resolved now, not when a late shutdown runs

    def on_shutdown():
        jobs.close()
        control.close()
        try:
            info_path.unlink()
        except OSError:
            pass
    server.on_shutdown = on_shutdown
    return server, sb, jobs, control


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voiceclaude.daemon", description="voice-claude 后台服务")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认随机或配置里的 daemon.port）")
    parser.add_argument("--status", action="store_true", help="看后台服务在不在")
    parser.add_argument("--no-relaunch", action="store_true", help="内部用：即使在 Job 里也不再自我重拉")
    args = parser.parse_args(argv)
    if args.status:
        info = read_info()
        print("后台服务没有在运行" if not info else f"后台服务在运行：pid {info['pid']}，端口 {info['port']}，启动于 {info.get('started')}")
        return 0 if info else 1
    paths.ensure()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(paths.DAEMON_LOG, encoding="utf-8")])
    logging.getLogger("sherpa_onnx").setLevel(logging.WARNING)
    if read_info():
        log.warning("another daemon is already running; exiting")
        return 1
    if in_host_job() and not args.no_relaunch:
        # Whoever started us lives in a job object that would take us down with it. Relaunch through the
        # Task Scheduler (outside every job) and let that copy be the real daemon.
        log.warning("started inside the launching app's job object; relaunching through the Task Scheduler")
        return 0 if spawn_via_schtasks(["--no-relaunch"] + (["--port", str(args.port)] if args.port is not None else [])) else 1
    log.info("daemon pid %d, %s", os.getpid(), describe_job(relaunched=args.no_relaunch))
    cfg = config.load()
    port = args.port if args.port is not None else int(getattr(cfg, "daemon_port", 0) or 0)
    server, sb, jobs, control = build(cfg, port=port)
    sb.start()  # lines up and the manager active before any front end can connect
    write_info(server, control.port)
    log.info("daemon pid %d listening on %d (control %d)", os.getpid(), server.port, control.port)
    threading.Thread(target=server.serve_forever, daemon=True, name="accept").start()

    def ticker():
        while not server.stopped.wait(30):
            try:
                sb.tick()
            except Exception:
                log.exception("tick failed")
    threading.Thread(target=ticker, daemon=True, name="tick").start()
    try:
        server.stopped.wait()
        wait_finished(server)
    finally:
        try:
            sb.gateway.stop()  # a crash or Ctrl-C must not leave the proxy (and its vendor key) behind
        except Exception:
            log.exception("gateway stop failed")
    log.info("daemon exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
