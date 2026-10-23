"""Real, short round trips for one provider (DeepSeek by default). It spends a few cents of your API credit and
sends text to the vendor.

    .venv\\Scripts\\python -X utf8 scripts\\verify_providers.py deepseek [--key-env VOICECLAUDE_VERIFY_KEY] [--steps probe,tools,bash,manager]

Where the key comes from: ~/.voice-claude/auth.json (saved on the AI 服务商 page, read only), or an environment variable you name with
--key-env and set for this one run (default name VOICECLAUDE_VERIFY_KEY: the app itself never reads any vendor
key from the environment, and neither does this script unless told to). It is used in memory only: never written
to any file, log, config or output (our logs only ever show the last four characters). Everything else runs in a
throwaway VOICECLAUDE_HOME; no daemon is started, the lines run inside this process; a running voice-claude and
your ~/.voice-claude are not touched.

Steps (each one or two model turns; the summary says how many were spent):
  probe    one `claude -p` call: "只回答一个字：好" on the provider's model.
  tools    a stream-json line with our settings tool server: the model must call get_permissions (MCP tool use
           and tool_result through the provider) and answer in one sentence.
  bash     the model must run `dir` through Claude Code's own Bash tool: Claude Code's automatic permission
           judgement (--permission-mode auto) has to work through this provider for that to happen.
  manager  the real switchboard with the manager on this provider: "有哪些项目" (list_projects tool), then
           "接我到 服务器" (switch_to asks, we say yes, a project line starts on the provider).
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
REAL_AUTH = Path(os.environ.get("VOICECLAUDE_HOME") or Path.home() / ".voice-claude") / "auth.json"  # read only
HOME = Path(tempfile.mkdtemp(prefix="vc-verify-"))
os.environ["VOICECLAUDE_HOME"] = str(HOME)  # before voiceclaude.paths is imported

from voiceclaude import access, config, jobs as jobsmod, paths, prompts, switchboard  # noqa: E402
from voiceclaude.claude import SETTINGS_SERVER, ClaudeSession, server_spec  # noqa: E402
from voiceclaude.control import ControlServer  # noqa: E402
from voiceclaude.line import IDLE  # noqa: E402
from voiceclaude.managers import RulesSession  # noqa: E402
from voiceclaude.projects import Project  # noqa: E402
from voiceclaude.switchboard import MANAGER  # noqa: E402

TURNS: list[tuple[str, float]] = []  # (step, seconds) per model turn, for the cost note


def wait_for(pred, timeout: float) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def say(step: str, ok: bool, text: str) -> None:
    print(f"[{step}] {'OK ' if ok else 'FAIL'} {text}", flush=True)


# ---------- steps ----------

def probe(pid: str, cfg: config.Config) -> bool:
    t0 = time.time()
    r = access.test_provider(pid, cfg.command, cfg=cfg)
    TURNS.append(("probe", time.time() - t0))
    say("probe", r["ok"], f"{r['message']} ({r.get('seconds', 0):.1f} s)")
    return bool(r["ok"])


def tools(pid: str, cfg: config.Config) -> bool:
    events: list[tuple[str, object]] = []
    calls: list[str] = []

    def handler(path: str, data: dict) -> dict:
        calls.append(path)
        if path == "get_permissions":
            return {"ok": True, "message": "screenshot=ask（每次问），click=ask，type=ask（验证用的假数据）"}
        return {"ok": True, "message": "验证用：好了"}
    control = ControlServer(handler)
    cwd = HOME / "tools"
    cwd.mkdir(exist_ok=True)
    s = ClaudeSession(cfg, prompts.VOICE_RULES, lambda k, d: events.append((k, d)), cwd=str(cwd), line_id="verify-tools",
                      servers={SETTINGS_SERVER: server_spec("mcp_server.py")}, provider=pid)
    s.port = control.port
    t0 = time.time()
    try:
        s.start()
        s.send("请调用 get_permissions 工具，然后用一句话告诉我截图权限现在是什么。")
        done = wait_for(lambda: any(k in ("done", "exit") for k, d in events), 150)
    finally:
        TURNS.append(("tools", time.time() - t0))
        s.close()
        control.close()
    status = next((d for k, d in events if k == "done"), {})
    tool_names = [d.get("name", "") for k, d in events if k == "tool"]
    text = "".join(d for k, d in events if k == "text" and isinstance(d, str))
    ok = done and "get_permissions" in calls and status.get("status") == "ok" and bool(text.strip())
    say("tools", ok, f"model={s.model} tool calls={tool_names} control calls={calls} status={status.get('status')!r} "
                     f"answer={text.strip()[:80]!r} ({TURNS[-1][1]:.1f} s)")
    if not ok:
        errs = [d for k, d in events if k == "done"]
        print("   events:", [(k, (d if isinstance(d, str) else {kk: vv for kk, vv in d.items() if kk != 'input'})) for k, d in events][-8:])
        print("   done:", errs)
    return ok


def bash(pid: str, cfg: config.Config) -> bool:
    """Claude Code's own Bash tool under --permission-mode auto: does its safety judgement work through this vendor?"""
    events: list[tuple[str, object]] = []
    control = ControlServer(lambda path, data: {"ok": True, "message": "验证用：好了"})
    cwd = HOME / "bash"
    cwd.mkdir(exist_ok=True)
    (cwd / "a.txt").write_text("a", "utf-8")
    (cwd / "b.txt").write_text("b", "utf-8")
    s = ClaudeSession(cfg, prompts.VOICE_RULES, lambda k, d: events.append((k, d)), cwd=str(cwd), line_id="verify-bash",
                      servers={SETTINGS_SERVER: server_spec("mcp_server.py")}, provider=pid)
    s.port = control.port
    t0 = time.time()
    try:
        s.start()
        s.send("用 Bash 工具运行 dir 看看当前目录，然后用一句话告诉我里面有几个文件。")
        done = wait_for(lambda: any(k in ("done", "exit") for k, d in events), 150)
    finally:
        TURNS.append(("bash", time.time() - t0))
        s.close()
        control.close()
    status = next((d for k, d in events if k == "done"), {})
    tool_names = [d.get("name", "") for k, d in events if k == "tool"]
    text = "".join(d for k, d in events if k == "text" and isinstance(d, str))
    ok = done and any(n.lower() in ("bash", "powershell") for n in tool_names) and status.get("status") == "ok" and bool(text.strip())
    say("bash", ok, f"tool calls={tool_names} status={status.get('status')!r} answer={text.strip()[:80]!r} ({TURNS[-1][1]:.1f} s)")
    if not ok:
        print("   done:", [d for k, d in events if k == "done"])
    return ok


def manager(pid: str, cfg: config.Config) -> bool:
    events: list[tuple[str, dict]] = []
    for name in ("atlas", "beacon"):
        (HOME / "proj" / name).mkdir(parents=True, exist_ok=True)
    projects = [Project(key="atlas", name="Atlas", display_name="Atlas", cwd=str(HOME / "proj" / "atlas"), aliases=["服务器"]),
                Project(key="beacon", name="Beacon", display_name="Beacon", cwd=str(HOME / "proj" / "beacon"))]
    jb = jobsmod.Jobs(paths.JOBS_DIR, paths.JOBS_FILE, lambda j: None, poll_seconds=2.0)
    jb.load()
    holder: dict = {}
    control = ControlServer(lambda path, data: holder["sb"].control_request(str(data.pop("line", "") or ""), path, data))
    switchboard.config_save = lambda *a, **k: None  # nothing is written to any config here
    sb = switchboard.Switchboard(cfg, jobs=jb, emit=lambda k, d: events.append((k, d)),
                                 forward=lambda p, d: {"ok": True, "message": "前台做了"}, scan_projects=lambda: list(projects),
                                 control_port=control.port, ask_timeout=30)
    holder["sb"] = sb
    ok = False
    try:
        sb.start()
        m = sb.lines[MANAGER]
        if isinstance(m.session, RulesSession) or m.provider != pid or m.state != IDLE:
            say("manager", False, f"the manager is not a model line on {pid}: {type(m.session).__name__} provider={m.provider} state={m.state}; "
                                  f"access: {sb.access.message}")
            return False

        def manager_text() -> str:
            return "".join(d.get("text", "") for k, d in events if k == "text" and d.get("line") == MANAGER)

        def dones(line_id: str) -> int:
            return sum(1 for k, d in events if k == "done" and d.get("line") == line_id)
        t0 = time.time()
        sb.say(MANAGER, "有哪些项目")
        got = wait_for(lambda: dones(MANAGER) >= 1, 150)
        TURNS.append(("manager:list", time.time() - t0))
        tools_used = [d.get("name", "") for k, d in events if k == "tool" and d.get("line") == MANAGER]
        text = manager_text()
        ok1 = got and ("Atlas" in text and "Beacon" in text)
        say("manager:list", ok1, f"model={getattr(m.session, 'model', '')} tools={tools_used} answer={text.strip()[:100]!r} ({TURNS[-1][1]:.1f} s)")

        # hand-off: the manager asks "要接到Atlas项目吗？", we answer yes, a project line starts on the provider
        def answerer():
            if wait_for(lambda: any(k == "ask" for k, d in events), 120):
                ask = [d for k, d in events if k == "ask"][0]
                sb.answer(ask["request"], True)
        threading.Thread(target=answerer, daemon=True).start()
        t0 = time.time()
        sb.say(MANAGER, "接我到 服务器")
        moved = wait_for(lambda: sb.active_id != MANAGER and sb.active() is not None and sb.active().state == IDLE, 150)
        TURNS.append(("manager:handoff", time.time() - t0))
        asked = [d.get("question") for k, d in events if k == "ask"]
        line = sb.active()
        ok2 = moved and line is not None and line.provider == pid and line.kind == "project"
        say("manager:handoff", ok2, f"asked={asked} active={sb.active_id} provider={getattr(line, 'provider', None)} "
                                    f"model={getattr(getattr(line, 'session', None), 'model', '')} ({TURNS[-1][1]:.1f} s)")
        ok = ok1 and ok2
        if not ok:
            print("   notices:", [d.get("text") for k, d in events if k == "notice"][-5:])
    finally:
        try:
            sb.shutdown()
        except Exception as e:
            print("   shutdown:", e)
        jb.close()
        control.close()
    return ok


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("provider", nargs="?", default="deepseek")
    ap.add_argument("--key-env", default="VOICECLAUDE_VERIFY_KEY",
                    help="environment variable holding the key for this run only (default: none of the vendors' own names)")
    ap.add_argument("--steps", default="probe,tools,bash,manager")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway home (logs) afterwards")
    a = ap.parse_args()
    pid = a.provider
    if not access.known_provider(pid):
        print(f"不认识的厂商 {pid}")
        return 2
    # the key saved on the AI 服务商 page: read from the real file (this process's own home is the throwaway one)
    key = os.environ.get(a.key_env, "").strip() or access.provider_key(pid, REAL_AUTH)
    if not key:
        print(f"没有 key：环境变量 {a.key_env} 为空，{REAL_AUTH} 里也没有 {pid} 的 key。")
        return 2
    orig = access.provider_key
    access.provider_key = lambda p, path=None: key if p == pid else orig(p, path)  # in memory only, never saved
    paths.ensure()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(paths.DAEMON_LOG, encoding="utf-8")])
    cfg = config.load()  # the example config copied into the throwaway home
    cfg.provider = pid
    cfg.working_directory = str(HOME)
    cfg.desktop_enabled = False
    p = access.provider_for(pid, cfg)
    print(f"provider {pid} ({p.name}) model {p.model}; key {access.mask(key)} from "
          f"{'env:' + a.key_env if os.environ.get(a.key_env) else 'auth.json'}; home {HOME}", flush=True)
    st = access.detect_provider(pid, cfg.command, cfg=cfg)
    if not st.ok:
        print("cannot start:", st.message)
        return 2
    results = {}
    for step in [s.strip() for s in a.steps.split(",") if s.strip()]:
        fn = {"probe": probe, "tools": tools, "bash": bash, "manager": manager}.get(step)
        if fn is None:
            print("unknown step", step)
            return 2
        try:
            results[step] = fn(pid, cfg)
        except Exception as e:
            logging.getLogger("verify").exception("step %s crashed", step)
            say(step, False, f"crashed: {e}")
            results[step] = False
    n = len(TURNS)
    print(f"\nsummary: {results}; model turns spent: {n} ({', '.join(f'{s} {t:.0f}s' for s, t in TURNS)}); "
          f"rough cost on {p.model}: well under US$0.05 (each turn ~10-20K input tokens). logs: {paths.DAEMON_LOG}")
    ok = all(results.values()) and bool(results)
    if ok and not a.keep:
        shutil.rmtree(HOME, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
