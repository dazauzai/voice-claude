"""Probe of Codex app-server (JSON-RPC over stdio) for the voice-claude Codex engine. Spends a little of your
ChatGPT plan: one word, one `dir`, one call of our own MCP tool. It answers Codex's approval requests itself:
yes only to a plain `dir` command, no to every file change and every request for more permissions.

    .venv\\Scripts\\python -X utf8 scripts\\probe_codex.py [--steps word,dir,mcp] [--keep]

Reads nothing in ~/.codex itself: account facts come from account/read (e-mail masked), the Windows sandbox state
from windowsSandbox/readiness (never setupStart: that may need elevation). Raw protocol traffic goes to
<throwaway home>/probe-codex.log, truncated per line.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HOME = Path(tempfile.mkdtemp(prefix="vc-codex-"))
os.environ["VOICECLAUDE_HOME"] = str(HOME)

from voiceclaude import __version__  # noqa: E402
from voiceclaude.control import ControlServer  # noqa: E402

LOG = HOME / "probe-codex.log"
TURN_TIMEOUT = 150.0


def find_codex() -> str | None:
    """bin\\codex.exe from the official package first (never the .cmd shim: an extra cmd.exe would orphan the server)."""
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Codex" / "bin" / "codex.exe"
    if local.is_file():
        return str(local)
    return shutil.which("codex.exe") or shutil.which("codex")


def mask_email(s: str | None) -> str:
    if not s or "@" not in s:
        return s or ""
    u, d = s.split("@", 1)
    return f"{u[:2]}…@{d}"


class AppServer:
    """Minimal JSON-RPC client over the app-server's stdio."""

    def __init__(self, exe: str, cwd: Path, mcp: dict | None, env: dict):
        args = [exe, "app-server"]
        for k, v in (mcp or {}).items():
            args += ["-c", f"{k}={v}"]
        self.log = open(LOG, "a", encoding="utf-8")
        self.log.write("ARGS " + " ".join(args) + "\n")
        self.proc = subprocess.Popen(args, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=open(HOME / "codex-stderr.log", "ab"), text=True, encoding="utf-8",
                                     errors="replace", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.inbox: queue.Queue = queue.Queue()
        self.next_id = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            self.log.write("<< " + line[:600] + "\n")
            self.log.flush()
            try:
                self.inbox.put(json.loads(line))
            except json.JSONDecodeError:
                pass
        self.inbox.put(None)

    def send(self, msg: dict):
        line = json.dumps(msg, ensure_ascii=False)
        self.log.write(">> " + line[:600] + "\n")
        self.log.flush()
        with self.lock:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 60.0, on_event=None) -> dict:
        """Send a request and pump messages until its response arrives; other messages go to on_event."""
        self.next_id += 1
        rid = self.next_id
        self.send({"id": rid, "method": method, "params": params or {}})
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                m = self.inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            if m is None:
                raise RuntimeError("app-server closed")
            if m.get("id") == rid and ("result" in m or "error" in m):
                if "error" in m:
                    raise RuntimeError(f"{method} -> error {json.dumps(m['error'], ensure_ascii=False)[:300]}")
                return m.get("result") or {}
            self.dispatch(m, on_event)
        raise TimeoutError(f"{method}: no response in {timeout}s")

    def pump(self, until, timeout: float, on_event):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                m = self.inbox.get(timeout=0.5)
            except queue.Empty:
                if until():
                    return True
                continue
            if m is None:
                raise RuntimeError("app-server closed")
            self.dispatch(m, on_event)
            if until():
                return True
        return until()

    def dispatch(self, m: dict, on_event):
        if "method" in m and "id" in m:  # a server-initiated request: we must answer
            self.answer(m, on_event)
        elif "method" in m:
            if on_event:
                on_event(m["method"], m.get("params") or {})

    def answer(self, m: dict, on_event):
        method, params, rid = m["method"], m.get("params") or {}, m["id"]
        if on_event:
            on_event("SERVER-REQUEST " + method, params)
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            cmd = str(params.get("command") or "")
            decision = "accept" if method.endswith("commandExecution/requestApproval") and plain_dir(cmd) else "decline"
            print(f"   approval asked: {method} command={cmd!r} reason={params.get('reason')!r} -> {decision}")
            self.send({"id": rid, "result": {"decision": decision}})
        elif method == "item/permissions/requestApproval":
            print(f"   permissions asked: {json.dumps(params.get('permissions'), ensure_ascii=False)[:200]} -> none granted")
            self.send({"id": rid, "result": {"permissions": {}, "scope": "turn"}})
        elif method == "item/tool/requestUserInput":
            print("   requestUserInput:", json.dumps(params.get("questions"), ensure_ascii=False)[:200])
            self.send({"id": rid, "result": {"answers": {}}})
        elif method == "mcpServer/elicitation/request":
            # Codex asks the client before every MCP tool call ("Allow the X MCP server to run tool Y?"). Our own
            # servers gate themselves out loud, so they are accepted for the session; anything else is declined.
            meta = params.get("_meta") or {}
            ours = params.get("serverName") in ("voiceclaude", "desktop", "manager")
            print(f"   elicitation: server={params.get('serverName')} kind={meta.get('codex_approval_kind')} "
                  f"msg={params.get('message')!r} -> {'accept' if ours else 'decline'}")
            if ours:
                self.send({"id": rid, "result": {"action": "accept", "content": {}, "_meta": {"persist": "session"}}})
            else:
                self.send({"id": rid, "result": {"action": "decline", "content": None}})
        else:
            self.send({"id": rid, "error": {"code": -32601, "message": f"voice-claude probe does not handle {method}"}})

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        self.log.close()


def plain_dir(command: str) -> bool:
    """The probe's one shell step, exactly: `dir`, possibly wrapped in cmd /c or a PowerShell -Command."""
    import re
    exe = r"""(?:"[^"]*\\)?(?:\S*\\)?"""  # an optional (quoted) folder in front of the program
    return bool(re.fullmatch(exe + r"""(?:cmd(?:\.exe)?"?\s+/c\s+|(?:powershell|pwsh)(?:\.exe)?"?\s+(?:-\w+\s+)*?-Command\s+)?["']?dir["']?""",
                             command.strip(), re.I))


def via_engine(steps: list[str]) -> int:
    """The same turns through voiceclaude.engine_codex.CodexSession (what a line will really use)."""
    from voiceclaude import config, prompts
    from voiceclaude.claude import SETTINGS_SERVER, server_spec
    from voiceclaude.engine_codex import CodexSession
    work = HOME / "work"
    work.mkdir(exist_ok=True)
    (work / "a.txt").write_text("a", "utf-8")
    events: list[tuple[str, object]] = []
    asks: list[tuple[str, str, str]] = []
    calls: list[str] = []
    control = ControlServer(lambda path, data: (calls.append(path), {"ok": True, "message": "screenshot=ask（每次问）"})[1])
    cfg = config.Config()
    cfg.working_directory = str(work)
    s = CodexSession(cfg, prompts.VOICE_RULES, lambda k, d: events.append((k, d)), cwd=str(work), line_id="probe",
                     servers={SETTINGS_SERVER: server_spec("mcp_server.py")},
                     ask=lambda a, q, d, facts=None: (asks.append((a, q, d)), print(f"   gate: {a} {q!r}"),
                                                      (a == "codex_shell" and plain_dir(str((facts or {}).get("command") or "")),
                                                       ""))[2],
                     stderr_path=HOME / "codex-stderr.log")
    s.port = control.port
    print("engine args:", " ".join(s.build_args())[:400])
    results = {}
    try:
        t0 = time.time()
        s.start()
        ok = wait_for(lambda: any(k == "ready" for k, d in events), 90)
        print(f"ready: {ok} ({time.time() - t0:.1f}s) thread={s.session_id[:8]}…")
        prompts_ = {"word": "只回答一个字：好", "dir": "用 shell 运行 dir 看看当前目录，然后用一句话告诉我里面有几个文件。",
                    "mcp": "请调用 voiceclaude 工具里的 get_permissions，然后用一句话告诉我截图权限现在是什么。"}
        for step in steps:
            n = len([1 for k, d in events if k == "done"])
            events_before = len(events)
            t1 = time.time()
            s.send(prompts_[step])
            done = wait_for(lambda: len([1 for k, d in events if k == "done"]) > n, TURN_TIMEOUT)
            d = [d for k, d in events if k == "done"][-1] if done else {}
            text = "".join(d for k, d in events[events_before:] if k == "text")
            tools = [d for k, d in events[events_before:] if k == "tool"]
            okstep = done and d.get("status") == "ok" and bool(text.strip())
            if step == "mcp":
                okstep = okstep and "get_permissions" in calls
            print(f"[engine:{step}] {'OK ' if okstep else 'FAIL'} status={d.get('status')} {time.time() - t1:.1f}s answer={text.strip()[:90]!r} tools={tools}")
            results[step] = okstep
        print("   gate questions:", asks, "| control calls:", calls)
    finally:
        s.close()
        control.close()
    print("\nsummary:", results)
    return 0 if results and all(results.values()) else 1


def wait_for(pred, timeout: float) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steps", default="word,dir,mcp")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--via-engine", action="store_true", help="run the turns through voiceclaude.engine_codex instead of the raw probe")
    a = ap.parse_args()
    if a.via_engine:
        return via_engine([x.strip() for x in a.steps.split(",") if x.strip()])
    exe = find_codex()
    if not exe:
        print("codex.exe not found")
        return 2
    print("codex:", exe, "| home:", HOME)
    steps = [s.strip() for s in a.steps.split(",") if s.strip()]
    work = HOME / "work"
    work.mkdir()
    (work / "a.txt").write_text("a", "utf-8")
    (work / "b.txt").write_text("b", "utf-8")

    calls: list[str] = []
    control = ControlServer(lambda path, data: (calls.append(path), {"ok": True, "message": "screenshot=ask（每次问），click=ask（验证用的假数据）"})[1])
    py = Path(sys.executable).as_posix()
    server_py = (ROOT / "voiceclaude" / "mcp_server.py").as_posix()
    mcp = {
        "mcp_servers.voiceclaude.command": json.dumps(py),
        "mcp_servers.voiceclaude.args": json.dumps(["-X", "utf8", server_py]),
        "mcp_servers.voiceclaude.env": '{VOICECLAUDE_PORT = "%d", VOICECLAUDE_LINE = "probe"}' % control.port,
    }
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
    srv = AppServer(exe, work, mcp if "mcp" in steps else None, env)
    results = {}
    try:
        t0 = time.time()
        init = srv.request("initialize", {"clientInfo": {"name": "voice-claude-probe", "title": "voice-claude probe", "version": __version__},
                                          "capabilities": {"experimentalApi": True}})
        srv.send({"method": "initialized", "params": {}})
        print(f"initialize ok ({time.time() - t0:.1f}s): {json.dumps(init, ensure_ascii=False)[:300]}")
        acct = srv.request("account/read", {})
        acc = acct.get("account") or {}
        print("account:", {"type": acc.get("type"), "planType": acc.get("planType"), "email": mask_email(acc.get("email")),
                           "requiresOpenaiAuth": acct.get("requiresOpenaiAuth")})
        try:
            ready = srv.request("windowsSandbox/readiness", {})
            print("windows sandbox readiness:", ready)
        except Exception as e:
            print("windows sandbox readiness: ", e)
        try:
            models = srv.request("model/list", {})
            items = models.get("data") or models.get("models") or models.get("items") or []
            print("models:", [(m.get("id") or m.get("model"), m.get("isDefault")) for m in items][:12])
        except Exception as e:
            print("model/list:", e)

        events: list[tuple[str, dict]] = []
        state = {"turn_done": False, "turn_status": None, "text": "", "items": []}

        def on_event(method, params):
            events.append((method, params))
            if method == "item/agentMessage/delta":
                state["text"] += params.get("delta", "")
            elif method in ("item/started", "item/completed"):
                it = params.get("item") or {}
                state["items"].append((method.split("/")[1], it.get("type"), it.get("status"), it.get("command") or it.get("tool") or ""))
            elif method == "turn/completed":
                state["turn_done"] = True
                state["turn_status"] = (params.get("turn") or {}).get("status")
                state["turn_error"] = (params.get("turn") or {}).get("error")
            elif method == "error":
                print("   error event:", json.dumps(params, ensure_ascii=False)[:300])
            elif method.startswith("mcpServer/"):
                print("   ", method, json.dumps(params, ensure_ascii=False)[:200])

        thread = srv.request("thread/start", {"cwd": str(work), "approvalPolicy": "on-request", "sandbox": "workspace-write",
                                              "developerInstructions": "你正在和用户进行语音对话。用口语化的中文回答，两三句以内，不要用 markdown。"},
                             on_event=on_event)
        tid = thread["thread"]["id"]
        print(f"thread {tid[:8]}… model={thread.get('model')} provider={thread.get('modelProvider')} effort={thread.get('reasoningEffort')} "
              f"approval={thread.get('approvalPolicy')} sandbox={thread.get('sandbox')}")

        def turn(name: str, text: str):
            state.update(turn_done=False, turn_status=None, text="", items=[], turn_error=None)
            t1 = time.time()
            r = srv.request("turn/start", {"threadId": tid, "input": [{"type": "text", "text": text}],
                                           "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [str(work)], "networkAccess": False}},
                            on_event=on_event)
            turn_id = (r.get("turn") or {}).get("id")
            done = srv.pump(lambda: state["turn_done"], TURN_TIMEOUT, on_event)
            secs = time.time() - t1
            ok = done and state["turn_status"] == "completed" and bool(state["text"].strip())
            print(f"[{name}] {'OK ' if ok else 'FAIL'} status={state['turn_status']} {secs:.1f}s answer={state['text'].strip()[:100]!r}")
            print("   items:", state["items"][:12])
            if state.get("turn_error"):
                print("   turn error:", state["turn_error"])
            results[name] = ok
            return ok

        if "word" in steps:
            turn("word", "只回答一个字：好")
        if "dir" in steps:
            turn("dir", "用 shell 运行 dir 看看当前目录，然后用一句话告诉我里面有几个文件。")
            print("   command items:", [i for i in state["items"] if i[1] == "commandExecution"])
        if "mcp" in steps:
            turn("mcp", "请调用 voiceclaude 工具里的 get_permissions，然后用一句话告诉我截图权限现在是什么。")
            print("   control endpoint calls:", calls, "| mcp items:", [i for i in state["items"] if i[1] == "mcpToolCall"])
            results["mcp"] = results.get("mcp", False) and "get_permissions" in calls
    except Exception as e:
        print("PROBE FAILED:", e)
    finally:
        srv.close()
        control.close()
    print("\nsummary:", results, "| log:", LOG)
    if all(results.values()) and results and not a.keep:
        pass  # keep the log anyway: the protocol transcript is the point of a probe
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
