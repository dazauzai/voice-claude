"""Background jobs that outlive everything: detached OS processes (no console, own process group), output to a
log file, tracked in ~/.voice-claude/jobs.json so a restarted daemon still knows about them."""
from __future__ import annotations

import ctypes
import json
import logging
import re
import secrets
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# CREATE_NO_WINDOW gives the job its own *hidden* console: it does not inherit ours (so a Ctrl-C in our console
# never reaches it) and it never pops a window. DETACHED_PROCESS would make console programs such as cmd/ping
# open a visible console of their own.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STATES = ("running", "done", "failed", "cancelled")


@dataclass
class Job:
    id: str
    command: str
    cwd: str
    line: str  # which line started it
    note: str  # what Claude said it is for
    pid: int
    log: str
    started: float
    ended: float | None = None
    exit_code: int | None = None
    state: str = "running"

    @property
    def spoken(self) -> str:
        what = self.note or self.command[:40]
        return {"running": f"后台任务 {self.id}（{what}）还在跑",
                "done": f"后台任务 {self.id}（{what}）做完了",
                "failed": f"后台任务 {self.id}（{what}）失败了，退出码 {self.exit_code}",
                "cancelled": f"后台任务 {self.id}（{what}）已取消"}[self.state]


_ENDLESS = (re.compile(r"\bping\b(?![^&|]*\s-n\b)", re.I),  # Windows ping without -n (and especially -t) runs forever
            re.compile(r"\btail\b[^&|]*\s(-f|--follow)\b", re.I),
            re.compile(r"\b(watch|top|htop)\b", re.I))


def looks_endless(command: str) -> bool:
    """A command that will not finish by itself (so the user is warned before approving it)."""
    return any(p.search(command) for p in _ENDLESS)


def pid_running(pid: int) -> bool:
    """Is a process we did not start (e.g. after a daemon restart) still alive?"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == _STILL_ACTIVE
    except Exception:
        return False


class Jobs:
    def __init__(self, dir: Path, file: Path, on_change: Callable[[Job], None], poll_seconds: float = 2.0):
        self.dir, self.file, self.on_change = dir, file, on_change
        self.poll_seconds = poll_seconds
        self.jobs: dict[str, Job] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancelling: set[str] = set()  # being killed by cancel(): its exit is a cancel, not a failure
        self.lock = threading.RLock()  # re-entrant: the done callback may read the job list
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._monitor, daemon=True, name="jobs")

    # ---------- persistence ----------

    def load(self) -> None:
        """Recover jobs.json; jobs whose process is gone are closed out (exit code unknown)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(self.file.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        with self.lock:
            for jid, rec in data.items():
                try:
                    job = Job(**rec)
                except TypeError:
                    continue
                if job.state == "running" and not pid_running(job.pid):
                    job.state, job.ended = "done", time.time()
                self.jobs[jid] = job
        self._save()
        if not self.thread.is_alive():
            self.thread.start()

    def _save(self) -> None:
        with self.lock:
            data = {jid: asdict(j) for jid, j in self.jobs.items()}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(self.file)

    # ---------- starting / stopping ----------

    def start(self, command: str, cwd: str, line: str, note: str = "") -> Job:
        self.dir.mkdir(parents=True, exist_ok=True)
        jid = secrets.token_hex(2)
        while jid in self.jobs:
            jid = secrets.token_hex(2)
        logfile = self.dir / f"{jid}.log"
        header = f"# voice-claude 后台任务 {jid}\n# {time.strftime('%Y-%m-%d %H:%M:%S')}  cwd={cwd}\n# {command}\n\n"
        logfile.write_text(header, "utf-8")
        out = open(logfile, "ab")
        flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        try:
            proc = subprocess.Popen(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, close_fds=True,
                                    creationflags=flags | CREATE_BREAKAWAY_FROM_JOB)
        except OSError:
            proc = subprocess.Popen(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, close_fds=True, creationflags=flags)
        finally:
            out.close()  # the child holds its own handle
        job = Job(id=jid, command=command, cwd=cwd, line=line, note=note, pid=proc.pid, log=str(logfile),
                  started=time.time())
        with self.lock:
            self.jobs[jid] = job
            self._procs[jid] = proc
        self._save()
        log.info("job %s started pid %d: %s", jid, proc.pid, command)
        if not self.thread.is_alive():
            self.thread.start()
        try:
            self.on_change(job)  # "running", so the window can show it right away
        except Exception:
            log.exception("job callback failed")
        return job

    def cancel(self, jid: str) -> bool:
        with self.lock:
            job = self.jobs.get(jid)
            if not job or job.state != "running" or jid in self._cancelling:
                return False
            self._cancelling.add(jid)
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(job.pid)], capture_output=True, creationflags=CREATE_NO_WINDOW)
            self._finish(job, "cancelled", None)
        finally:
            with self.lock:
                self._cancelling.discard(jid)
        return True

    def cancel_all(self) -> list[Job]:
        done = []
        for job in self.running():
            if self.cancel(job.id):
                done.append(job)
        return done

    def close(self) -> None:
        """Stop watching. Never kills the jobs."""
        self._stop.set()

    # ---------- reading ----------

    def get(self, jid: str) -> Job | None:
        with self.lock:  # a finished job becomes visible only once its "done" callback has been delivered
            return self.jobs.get(jid)

    def list(self) -> list[Job]:
        with self.lock:
            return sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)

    def running(self) -> list[Job]:
        return [j for j in self.list() if j.state == "running"]

    def tail(self, jid: str, lines: int = 10) -> str:
        job = self.jobs.get(jid)
        if not job:
            return ""
        try:
            text = Path(job.log).read_text("utf-8", errors="replace")
        except OSError:
            return ""
        body = [l for l in text.splitlines() if not l.startswith("# ")]
        return "\n".join(body[-lines:])

    # ---------- monitoring ----------

    def _finish(self, job: Job, state: str, code: int | None, watcher: bool = False) -> None:
        # State change, save and callback under one lock, so nobody who asks get() sees "done" before the
        # "done" callback went out (the switchboard's notice, the window's job list). The watcher leaves a job
        # that cancel() is killing alone: the killed process exits non-zero, and that is no failure.
        with self.lock:
            if job.state != "running" or (watcher and job.id in self._cancelling):
                return
            job.state, job.exit_code, job.ended = state, code, time.time()
            self._procs.pop(job.id, None)
            self._save()
            log.info("job %s %s (exit %s)", job.id, state, code)
            try:
                self.on_change(job)
            except Exception:
                log.exception("job callback failed")

    def _monitor(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            with self.lock:
                items = [(j, self._procs.get(j.id)) for j in self.jobs.values() if j.state == "running"]
            for job, proc in items:
                if proc is not None:
                    rc = proc.poll()
                    if rc is not None:
                        self._finish(job, "done" if rc == 0 else "failed", rc, watcher=True)
                elif not pid_running(job.pid):  # recovered after a daemon restart: exit code unknown
                    self._finish(job, "done", None, watcher=True)
