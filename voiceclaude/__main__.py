import argparse
import logging
import os
import sys
import threading

from . import config


def end_soon(seconds: float = 10.0, code: int = 0) -> threading.Timer:
    """The window has closed (code 0) or stopped on an error (code 1): end the process even if a sound-card call never
    returns. A stuck PortAudio write would keep it alive, holding the window mutex, and standby would never listen
    again."""
    def end():
        logging.warning("still running %.0f s after the window %s; ending the process", seconds,
                        "stopped on an error" if code else "closed")
        logging.shutdown()
        os._exit(code)
    t = threading.Timer(seconds, end)
    t.daemon = True
    t.start()
    return t


def main():
    ap = argparse.ArgumentParser(prog="voiceclaude")
    ap.add_argument("--woken", action="store_true", help="started by the wake word: listen as soon as it is ready")
    ap.add_argument("--standby", action="store_true", help="started by standby: go back to it after a few idle minutes")
    args, _ = ap.parse_known_args()
    from .standby import WINDOW_MUTEX, hold_mutex
    window_mutex = hold_mutex(WINDOW_MUTEX)  # noqa: F841  kept for the process's life: standby leaves the mic alone
    handlers: list[logging.Handler] = [logging.FileHandler(config.ROOT / "voice-claude.log", encoding="utf-8")]
    if sys.stdout is not None:  # pythonw (run.bat) has no console; the log file is enough
        handlers.insert(0, logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("sherpa_onnx").setLevel(logging.WARNING)
    try:
        from .app import App  # after the log is set up: a broken install must leave a trace (run.bat has no console)
    except Exception as e:
        logging.exception("startup failed")
        from tkinter import messagebox
        messagebox.showerror("voice-claude", f"启动失败：{e}\n再双击 install.bat 修复安装。")
        sys.exit(1)
    try:
        cfg = config.load()
    except Exception as e:  # a hand-edited config: tomllib errors carry the line and column
        msg = (f"配置文件有错误：{e}\n文件：{config.user_config_path()}\n"
               "请修正后重新启动。（文字值要加英文引号，例如 key = \"left ctrl\"）")
        logging.error(msg)
        from tkinter import messagebox
        messagebox.showerror("voice-claude", msg)
        sys.exit(1)
    failed = False
    try:
        App(cfg, woken=args.woken, from_standby=args.standby).run()
    except BaseException:  # pythonw has no stderr: the traceback goes to the log (Ctrl+C in a console included)
        failed = True
        logging.exception("the window stopped on an error")
        raise
    finally:  # also when the window never closed its sound streams
        end_soon(code=1 if failed else 0)


if __name__ == "__main__":
    main()
