"""voice-claude installer (run by install.bat; also: python scripts\\install.py [--mirror | --no-mirror] [--with-gpt] [--yes]).

Steps, each skipped when already done, so running it again after a failure picks up where it stopped:
  1. the environment (.venv)       2. the Python packages        3. the Claude Code engine (claude-agent-sdk)
  4. the speech models (~640 MB)   5. optional: the LiteLLM layer for GPT
Downloads pick a source that answers: PyPI or the Tsinghua mirror; GitHub or the Hugging Face mirror (hf-mirror.com).
Nothing here asks for or stores an API key; keys are entered in the app's AI 服务商 page."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
VENV_PY = VENV / "Scripts" / "python.exe"
PYPI = "https://pypi.org/simple/"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple/"
GPT_PACKAGE = "litellm[proxy]>=1.106.2"


class StepFailed(Exception):
    def __init__(self, what: str, fix: str):
        super().__init__(what)
        self.what, self.fix = what, fix


def say(text: str = "") -> None:
    print(text, flush=True)


def answers_in(url: str, timeout: float = 6.0) -> float | None:
    t = time.monotonic()
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=timeout).close()
    except urllib.error.HTTPError:
        pass
    except Exception:
        return None
    return time.monotonic() - t


def pick_index(force: str | None) -> str:
    if force == "mirror":
        return MIRROR
    if force == "pypi":
        return PYPI
    pypi = answers_in(PYPI + "pip/")
    if pypi is not None and pypi < 2.5:
        return PYPI
    return MIRROR if answers_in(MIRROR + "pip/") is not None else PYPI


def run(cmd: list[str], what: str, fix: str) -> None:
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise StepFailed(f"{what}（退出码 {r.returncode}）", fix)


def pip(args: list[str], index: str, what: str) -> None:
    run([str(VENV_PY), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-i", index] + args,
        what, "检查网络后再运行一次 install.bat；也可以加 --mirror 强制用国内镜像。")


def has_module(name: str) -> bool:
    r = subprocess.run([str(VENV_PY), "-c", f"import {name}"], capture_output=True)
    return r.returncode == 0


def ask_yes(question: str, default: bool, assume: bool) -> bool:
    if assume:
        return default
    try:
        a = input(f"{question} {'[Y/n]' if default else '[y/N]'} ").strip().lower()
    except EOFError:
        return default
    return default if not a else a in ("y", "yes", "是", "要")


# ---------- the steps ----------

def step_venv() -> None:
    if VENV_PY.exists():
        say("  已有，跳过。")
        return
    if sys.version_info < (3, 11):
        raise StepFailed(f"Python 版本是 {sys.version.split()[0]}，需要 3.11 以上", "装新版 Python 后再运行 install.bat。")
    run([sys.executable, "-m", "venv", str(VENV)], "无法创建运行环境", "确认 Python 安装完整（安装时勾选了 pip），再运行一次。")


def step_packages(index: str) -> None:
    say("  下载并安装中…")
    pip(["-r", str(ROOT / "requirements.txt")], index, "依赖没有装完")


def step_engine(index: str) -> None:
    own = shutil.which("claude") or (Path.home() / ".local" / "bin" / "claude.exe").exists()
    if own:
        say("  已有，跳过。")
        return
    if has_module("claude_agent_sdk"):
        say("  已有，跳过。")
        return
    say("  下载并安装中（约 130 MB）…")
    pip(["claude-agent-sdk"], index, "Claude Code 引擎没有装上")


def step_models(source: str) -> None:
    args = [str(ROOT / "scripts" / "download_models.py"), "--source", source, "--yes"]
    run([str(VENV_PY), "-X", "utf8"] + args, "语音模型没有下载完",
        "网络好一点时再运行一次 install.bat，已下好的模型不会重下；也可以加 --mirror 用国内镜像。")


def gateway_ready() -> bool:
    """LiteLLM new enough and with its proxy extras: the gateway's own check (access.gateway_problem)."""
    check = "from voiceclaude.access import gateway_problem; raise SystemExit(1 if gateway_problem() else 0)"
    return subprocess.run([str(VENV_PY), "-c", check], cwd=str(ROOT), capture_output=True).returncode == 0


def step_gpt(index: str, wanted: bool | None, assume: bool) -> None:
    if gateway_ready():
        say("  已有，跳过。")
        return
    if has_module("litellm"):  # installed, but too old or without the proxy extras: bring it up to what is needed
        wanted = True
    if wanted is None:
        wanted = ask_yes("  要用 OpenAI 的 API key 吗？（约 550 MB；用 ChatGPT 订阅登录不需要）", False, assume)
    if not wanted:
        say("  跳过。")
        return
    pip(["-U", GPT_PACKAGE], index, "LiteLLM 没有装上")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="安装 voice-claude")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--mirror", action="store_true", help="一律用国内镜像（清华 PyPI、hf-mirror.com）")
    g.add_argument("--no-mirror", action="store_true", help="一律用 PyPI 和 GitHub")
    ap.add_argument("--with-gpt", action="store_true", help="同时安装 GPT 用的 LiteLLM")
    ap.add_argument("--yes", action="store_true", help="不停下来提问（用默认答案）")
    a = ap.parse_args(argv)
    force = "mirror" if a.mirror else "pypi" if a.no_mirror else None
    say("安装 voice-claude")
    index = pick_index(force)
    models_source = "hf-mirror" if a.mirror else "github" if a.no_mirror else ("github" if index == PYPI else "hf-mirror")
    steps = [("创建运行环境", step_venv),
             ("安装依赖", lambda: step_packages(index)),
             ("安装 Claude Code", lambda: step_engine(index)),
             ("下载语音模型（约 640 MB）", lambda: step_models(models_source)),
             ("OpenAI API key 组件（可选）", lambda: step_gpt(index, True if a.with_gpt else None, a.yes))]
    for i, (name, fn) in enumerate(steps, 1):
        say()
        say(f"[{i}/{len(steps)}] {name}")
        try:
            fn()
        except StepFailed as e:
            say()
            say(f"第 {i} 步「{name}」没有完成：{e.what}")
            say(f"怎么办：{e.fix}")
            say("修好后再双击 install.bat，已完成的步骤会跳过。")
            return 1
        except KeyboardInterrupt:
            say()
            say("已取消。再双击 install.bat 会从这一步接着装。")
            return 1
    say()
    say("装好了。双击 run.bat 启动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
