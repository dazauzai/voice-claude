"""End-to-end check of the background service with the REAL Claude Code, no audio and no keyboard:
a detached daemon in a throwaway ~/.voice-claude (desktop tools off), a client that plays the front end (answers
可以 to the hand-off and the background job only, 不行 to anything else),
the manager hands us to a test project, a background job survives the front end disconnecting, back to the
manager, emergency stop, shutdown. Run: .venv\\Scripts\\python -X utf8 scripts\\verify_daemon.py"""
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOME = Path(tempfile.mkdtemp(prefix="vc-verify-"))
os.environ["VOICECLAUDE_HOME"] = str(HOME)

from voiceclaude import daemon, paths, projects  # noqa: E402
from voiceclaude.client import DaemonClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("verify")
results = []
events = []
ev_lock = threading.Lock()


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    log.info("%s %s %s", "PASS" if ok else "FAIL", name, detail)


def wait_for(pred, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


def find(kind, pred=lambda d: True):
    with ev_lock:
        return [d for k, d in events if k == kind and pred(d)]


client = None


def on_event(kind, data):
    with ev_lock:
        events.append((kind, data))
    if kind in ("notice", "ask", "line", "done", "job", "bye"):
        log.info("EVENT %s %s", kind, json.dumps(data, ensure_ascii=False)[:200])
    if kind == "ask" and client is not None:
        # Only what this run expects (the manager's hand-off and the background job); everything else is refused,
        # since a real Claude Code is choosing the actions.
        yes = data.get("action") in EXPECTED_ASKS
        threading.Thread(target=lambda: client.answer(data["request"], yes), daemon=True).start()


EXPECTED_ASKS = {"switch_to", "background_job"}


# a project Claude Code has never seen: registered through an alias, so the manager can find it
project_dir = HOME / "测试项目"
project_dir.mkdir(parents=True)
paths.ensure()
paths.CONFIG_FILE.write_text("[desktop]\nenabled = false\n", "utf-8")  # no desktop tools for a real model here
projects.remember_alias(paths.PROJECTS_FILE, "测试项目", str(project_dir))

try:
    daemon.spawn_detached()
    check("daemon spawned detached (daemon.json appears)", wait_for(lambda: daemon.read_info() is not None, 30))
    info = daemon.read_info() or {}
    client = DaemonClient(on_event)
    hello = client.connect(start_if_missing=False)
    check("front end connected; manager is active", hello.get("ok") and client.active == "manager", str(hello)[:200])
    check("manager greeted", wait_for(lambda: any("总机" in d["text"] for d in find("notice")), 10))
    check("manager line came up", any(l["line"] == "manager" and l["state"] == "idle" for l in hello.get("lines", [])),
          str(hello.get("lines")))

    # 1. hand-off through the real manager (sonnet) and its switch_to tool
    client.say("manager", "接我到测试项目。")
    ok = wait_for(lambda: client.active.startswith("p-"), 120)
    check("manager handed us to the test project", ok, f"active={client.active}")
    check("hand-off announced", any(d["text"].startswith("接到测试项目") for d in find("notice")), str([d["text"] for d in find("notice")]))
    wait_for(lambda: any(d["line"] == "manager" for d in find("done")), 60)
    line = client.active

    # 2. a background job through the gate (auto-approved by this fake front end)
    client.say(line, "用 run_in_background 工具跑这条命令：ping -n 45 127.0.0.1，说明写“测试任务”。启动后告诉我任务编号就行，不用等它。")
    check("asked before starting the job", wait_for(lambda: any(d["action"] == "background_job" for d in find("ask")), 120),
          str([d["question"] for d in find("ask")]))
    check("job reported running", wait_for(lambda: any(d["state"] == "running" for d in find("job")), 60))
    job_id = (find("job") or [{}])[0].get("job")

    # 3. the front end goes away (while the line is still answering) and comes back: lines and job untouched
    client.close()
    time.sleep(1.0)
    client = DaemonClient(on_event)
    hello = client.connect(start_if_missing=False)
    check("reconnect: same active line", client.active == line, f"{client.active} vs {line}")
    check("reconnect greeting names the line", wait_for(lambda: any(d["text"].startswith("现在接着") for d in find("notice")), 5))
    st = client.status()
    running = [j for j in st.get("jobs", []) if j["job"] == job_id and j["state"] == "running"]
    check("reconnect: job still running", bool(running), str(st.get("jobs")))
    wait_for(lambda: any(d["line"] == line for d in find("done")), 120)
    check("job finished and was announced", wait_for(lambda: any(d["state"] == "done" and d["job"] == job_id for d in find("job")), 90)
          and wait_for(lambda: any("做完了" in d["text"] for d in find("notice")), 10))

    # 4. phrases, emergency stop, shutdown
    r = client.phrase("background_status")
    check("background status spoken", r.get("ok") and bool(r.get("spoken")), r.get("spoken", "")[:120])
    r = client.phrase("back_to_manager")
    check("back to manager", r.get("ok") and client.active == "manager", r.get("spoken"))
    client.say("manager", "后台怎么样了？用一句话说。")
    check("manager answered via background_status", wait_for(lambda: any(d["line"] == "manager" for d in find("done")[1:]), 120))
    r = client.stop_all()
    check("stop_all", r.get("ok") and any("已紧急停止" in d["text"] for d in find("notice")), str(r))
    r = client.phrase("shutdown_daemon")
    check("shutdown accepted", r.get("ok") and r.get("spoken") == "好，关掉后台服务。", str(r))
    check("bye received and daemon gone", wait_for(lambda: bool(find("bye")), 15) and wait_for(lambda: daemon.read_info() is None, 15))
finally:
    info = daemon.read_info()
    if info:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(info["pid"])], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        log.info("daemon.log tail:\n%s", "\n".join((HOME / "daemon.log").read_text("utf-8", errors="replace").splitlines()[-40:]))
    except OSError:
        pass
    daemon.remove_task()  # the Task Scheduler route registers one per throwaway home
    import shutil
    shutil.rmtree(HOME, ignore_errors=True)  # so Claude Code's record of the test project points nowhere and is skipped

print("\n==== verify summary ====")
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
print(f"{sum(ok for _, ok, _ in results)}/{len(results)} passed")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
