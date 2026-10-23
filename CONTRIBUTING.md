# 参与开发

voice-claude 是一个 Windows 上的语音助手：用说话代替打字，和 Claude Code（或 DeepSeek、Kimi、GPT）一起工作。设计原则：**实现越简单越好、交互手感第一、任何动手的动作都先口头申请（门槛在代码里，不靠模型自觉）**。改代码前先读 [docs/architecture.md](docs/architecture.md) 和 [docs/protocol.md](docs/protocol.md)。

English summary: run the unit tests before every commit (`python -X utf8 -m unittest discover -s tests`); never let a test inject keystrokes or touch the running app or `~/.voice-claude`; keep PRs small and against `main`.

## 跑起来

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python scripts\download_models.py      # 约 640 MB 语音模型，会先显示许可证；或者直接双击 install.bat
.venv\Scripts\python -X utf8 -m voiceclaude
```

个人设置在 `~/.voice-claude/config.toml`（第一次运行时从 `config.example.toml` 复制），密钥在 `~/.voice-claude/auth.json`。这两样永远不要提交到仓库。后台服务在不在：`.venv\Scripts\python -m voiceclaude.daemon --status`。

## 测试分三种

| 种类 | 命令 | 需要什么 | 什么时候跑 |
|---|---|---|---|
| **单元测试** | `.venv\Scripts\python -X utf8 -m unittest discover -s tests` | 不需要模型、麦克风、密钥、模型服务；约 30 秒 | 每次改动后、提交前。CI（`.github/workflows/tests.yml`）只跑这个 |
| **前台假后台** | `.venv\Scripts\python -X utf8 scripts\frontend_harness.py` | 语音模型；不需要模型服务；用一个只讲协议的假后台服务驱动窗口 | 改前台（app.py、audio.py、hotkey.py、ui_*.py）后 |
| **整体自检** | `.venv\Scripts\python -X utf8 scripts\selftest.py` | 语音模型 + 真的模型服务（会起一个临时后台服务、花你的额度），约 5 分钟 | 改后台或协议后、发布前 |

单元测试起不了真进程、也碰不到 `~/.voice-claude`，这是代码保证的（`voiceclaude/testing.py`）：在 `python -m unittest` 或 pytest 下，Claude Code、Codex、翻译层和后台服务都拒绝启动，默认的家目录换成临时目录。自己写的测试运行包装（不是这两种入口）要设 `VOICECLAUDE_TESTING=1` 才受保护。

整体自检和任何会启动后台服务、模型进程或后台任务的脚本（`scripts/verify_daemon.py` 等）会起真实进程、可能发声，跑之前确认这台电脑现在没有别人在用。规矩：

- 测试**永远不模拟按键**（模拟的按键可能卡在按下状态，整台电脑打不了字）。自动测试用 `VOICECLAUDE_NO_HOTKEY=1` 禁用快捷键。
- 测试**永远不碰正在用的东西**：用 `VOICECLAUDE_HOME=临时目录` 起自己的后台服务，用 `VOICECLAUDE_START_WITH=自检项目` 直连一个临时项目；结束时关掉自己起的服务和任务，删临时目录。
- 后台命令一律用 `CREATE_NO_WINDOW` 启动（不弹终端标签）；不要让测试起不会自己结束的命令（`ping -t` 之类）。
- 桌面控制的测试只批准"输入到自己打开的窗口"，别的窗口一律答"不行"。
- 绝不 `taskkill` 不是自己起的进程（比如 IDE 或 Claude 桌面端里正在用的 claude.exe 会话）。

## 提交和分支

- 开发在 `main` 上，或者从 `main` 拉分支提 PR。PR 小而专注，一个 PR 做一件事。
- 提交信息第一行说清改了什么、为什么（中文英文都行）。
- 不要提交 `config.toml`、`auth.json`、`*.log`、`models/`。
- 改了用户看得到的行为，同时更新 README.md 和 README.en.md。

## 界面文字

窗口里的文字只写功能本身：标签、状态、动作，不写解释和安慰的话。
