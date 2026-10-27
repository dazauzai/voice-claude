"""The whiteboard window: Excalidraw in its own window (pywebview over the WebView2 runtime), showing the page the
background service serves at /board for one project. Started as its own process:

    pythonw -X utf8 -m voiceclaude.board_window --project p-xxxx --name 项目名

The project id and the run's board token (daemon.json) go into the page's URL; the page talks to the service from
there. The window's title ends with "— voice-claude", so the desktop tools treat it as one of ours (never clicked,
marked in screenshots)."""
from __future__ import annotations

import argparse
import logging
import sys
from urllib.parse import urlencode

from . import daemon, standby, testing

log = logging.getLogger(__name__)


def page_url(info: dict, project: str, name: str = "") -> str:
    """Where the page is for this run: the control server's /board with the project and the token."""
    q = {"project": project, "token": str(info.get("board_token") or "")}
    if name:
        q["name"] = name
    return f"http://127.0.0.1:{int(info['control_port'])}/board/?{urlencode(q)}"


def command(project: str, name: str = "") -> list[str]:
    argv = [standby._pythonw(), "-X", "utf8", "-m", "voiceclaude.board_window", "--project", project]
    if name:
        argv += ["--name", name]
    return argv


def title(name: str = "") -> str:
    return f"{name} 白板 — voice-claude" if name else "白板 — voice-claude"


def bring_up(window_title: str) -> bool:
    """Bring an open board window with this exact title to the front; False when there is none."""
    if testing.under_test_runner() or sys.platform != "win32":
        return False
    import ctypes
    u = ctypes.windll.user32
    hwnd = u.FindWindowW(None, window_title)
    if not hwnd:
        return False
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
    u.SetForegroundWindow(hwnd)
    return True


def open_board(project: str, name: str = "") -> None:
    """From the window (打开白板, or a line that wants to look): a separate process, so the Tk window stays as it is.
    A board already open for that project comes to the front instead of a second window."""
    if bring_up(title(name)):
        return
    standby._spawn(command(project, name))


def main() -> None:
    if testing.under_test_runner():
        raise RuntimeError(testing.REFUSAL)
    ap = argparse.ArgumentParser(prog="voiceclaude.board_window")
    ap.add_argument("--project", required=True)
    ap.add_argument("--name", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    info = daemon.read_info()
    if not info or not info.get("control_port"):
        log.error("the background service is not running: no board")
        sys.exit(1)
    import webview  # pywebview: a window over Edge's WebView2

    webview.create_window(title(args.name), page_url(info, args.project, args.name), width=1100, height=760, min_size=(600, 400))
    webview.start()


if __name__ == "__main__":
    main()
