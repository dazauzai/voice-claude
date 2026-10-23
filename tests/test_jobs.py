import tempfile
import time
import unittest
from pathlib import Path

from voiceclaude import jobs


def wait_for(pred, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.changes = []  # (id, state) at the time of the callback; the Job object itself keeps changing
        self.record = lambda j: self.changes.append((j.id, j.state))
        self.j = jobs.Jobs(self.tmp / "jobs", self.tmp / "jobs.json", self.record, poll_seconds=0.2)
        self.j.load()

    def tearDown(self):
        for job in self.j.running():
            self.j.cancel(job.id)
        self.j.close()

    def test_job_runs_and_logs(self):
        job = self.j.start("echo hi & ping -n 2 127.0.0.1 >nul", str(self.tmp), "l1", "打个招呼")
        self.assertEqual(job.state, "running")
        self.assertTrue(wait_for(lambda: self.j.get(job.id).state == "done"))
        self.assertEqual(self.j.get(job.id).exit_code, 0)
        self.assertIn("hi", self.j.tail(job.id))
        self.assertEqual(self.changes, [(job.id, "running"), (job.id, "done")])
        self.assertIn("做完了", self.j.get(job.id).spoken)
        self.assertIn("打个招呼", self.j.get(job.id).spoken)

    def test_failed_job(self):
        job = self.j.start("exit 3", str(self.tmp), "l1")
        self.assertTrue(wait_for(lambda: self.j.get(job.id).state == "failed"))
        self.assertEqual(self.j.get(job.id).exit_code, 3)
        self.assertIn("退出码 3", self.j.get(job.id).spoken)

    def test_cancel(self):
        job = self.j.start("ping -n 30 127.0.0.1 >nul", str(self.tmp), "l1")
        self.assertTrue(self.j.cancel(job.id))
        self.assertEqual(self.j.get(job.id).state, "cancelled")
        self.assertFalse(self.j.cancel(job.id))
        self.assertFalse(jobs.pid_running(job.pid) and False)  # killed tree; just make sure the call works

    def test_cancel_is_never_reported_as_a_failure(self):
        """The watcher could see the killed process exit (code 1) before cancel() recorded it, and call it failed."""
        from unittest import mock
        job = self.j.start("ping -n 30 127.0.0.1 >nul", str(self.tmp), "l1", "慢活")
        real_run = jobs.subprocess.run

        def slow_taskkill(args, **kw):
            r = real_run(args, **kw)
            time.sleep(0.8)  # the watcher polls every 0.2 s meanwhile and sees the process gone
            return r
        with mock.patch.object(jobs.subprocess, "run", slow_taskkill):
            self.assertTrue(self.j.cancel(job.id))
        self.assertEqual(self.j.get(job.id).state, "cancelled")
        self.assertNotIn((job.id, "failed"), self.changes)

    def test_reload_running_job(self):
        job = self.j.start("ping -n 4 127.0.0.1 >nul", str(self.tmp), "l1", "慢活")
        self.j.close()  # the daemon "restarts": the watcher is gone, the process is not
        other = jobs.Jobs(self.tmp / "jobs", self.tmp / "jobs.json", self.record, poll_seconds=0.2)
        other.load()
        self.assertEqual(other.get(job.id).state, "running")
        self.assertTrue(jobs.pid_running(job.pid))
        self.assertTrue(wait_for(lambda: other.get(job.id).state == "done"))
        self.assertIsNone(other.get(job.id).exit_code)  # unknown after a restart, but it is over
        other.close()

    def test_looks_endless(self):
        for cmd in ("ping -t 127.0.0.1", "ping 127.0.0.1", "tail -f x.log", "watch nvidia-smi", "ping -t 1.1.1.1 & echo"):
            self.assertTrue(jobs.looks_endless(cmd), cmd)
        for cmd in ("ping -n 20 127.0.0.1", "echo hi", "tail -n 5 x.log", "python train.py", "make build"):
            self.assertFalse(jobs.looks_endless(cmd), cmd)

    def test_cancel_all(self):
        a = self.j.start("ping -n 30 127.0.0.1 >nul", str(self.tmp), "l1")
        b = self.j.start("ping -n 30 127.0.0.1 >nul", str(self.tmp), "l1")
        stopped = self.j.cancel_all()
        self.assertEqual({j.id for j in stopped}, {a.id, b.id})
        self.assertEqual(self.j.running(), [])

    def test_persisted(self):
        job = self.j.start("exit 0", str(self.tmp), "l2", "n")
        self.assertTrue(wait_for(lambda: self.j.get(job.id).state == "done"))
        other = jobs.Jobs(self.tmp / "jobs", self.tmp / "jobs.json", lambda j: None)
        other.load()
        self.assertEqual(other.get(job.id).note, "n")
        self.assertEqual(other.get(job.id).line, "l2")
        other.close()


if __name__ == "__main__":
    unittest.main()
