import logging
import sys

from . import config


def main():
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
    App(cfg).run()


if __name__ == "__main__":
    main()
