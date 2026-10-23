# 第三方组件与许可证（NOTICE）

voice-claude 本身的代码以 MIT 许可证发布（见 [LICENSE](LICENSE)）。仓库里**不包含**任何模型文件、Claude Code 或 Codex 程序、密钥；下面这些东西由用户自己安装或由 `scripts/download_models.py` 下载，各有各的许可证和条款。本文件只是摘要，以各项目的原文为准；我们不是律师。

voice-claude 是独立的开源项目，与 Anthropic、OpenAI 没有关联，也没有得到它们的认可。Claude、Claude Code 是 Anthropic 的商标；Codex、ChatGPT 是 OpenAI 的商标。

## 语音模型（下载到 `models/`，不在仓库里）

| 组件 | 用途 | 许可证 | 要注意的 |
|---|---|---|---|
| SenseVoice-Small（FunAudioLLM / FunASR，经 sherpa-onnx 导出的 int8 ONNX，2024-07-17） | 语音识别（中英混说） | FunASR Model License v1.1（[原文](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)） | 允许使用、复制、修改、分享，**要求署名并保留模型名**；条款写明"仅供参考学习"，商用条款模糊；许可方可以修改条款。voice-claude 不打包它，只在下载脚本里引用发布地址 |
| Silero VAD（snakers4/silero-vad） | 判断"说完了" | MIT | 无 |
| Kokoro-82M 多语言 v1.1-zh（hexgrad，[模型页](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh)，经 sherpa-onnx 导出） | 朗读 | Apache-2.0 | 它的英文发音用到 **espeak-ng 的数据文件**（`espeak-ng-data/`，**GPL-3.0**）；中文分词用 cppjieba 的词典（`dict/`，MIT）。voice-claude 不打包这些文件，用户自己下载 |

下载来源（`scripts/download_models.py` 自动选能连上的）：sherpa-onnx 的 GitHub 发布页（github.com/k2-fsa/sherpa-onnx/releases）；或者 Hugging Face 上 sherpa-onnx 维护者的同一批文件（huggingface.co/csukuangfj/…），直接下载或经第三方镜像 hf-mirror.com。Python 包来自 PyPI 或清华镜像（pypi.tuna.tsinghua.edu.cn）。`install.bat` 不逐个询问许可证；单独运行 `scripts/download_models.py`（不带 `--yes`）会先列出它们。

## Python 依赖（`pip install` 时由用户安装，不随仓库分发）

| 包 | 用途 | 许可证 |
|---|---|---|
| sherpa-onnx | 本机语音识别 / VAD / 朗读的运行库 | Apache-2.0。它的 Windows 安装包里**静态编译了 espeak-ng（GPL-3.0 或更高）**，并带着 ONNX Runtime（MIT）。它只是 pip 依赖、不随 voice-claude 分发；如果你要把 voice-claude 打成独立安装包（把依赖打进去），GPL 这部分需要单独考虑 |
| numpy | 音频数组 | BSD-3-Clause |
| sounddevice | 麦克风和扬声器 | MIT |
| mcp（Model Context Protocol Python SDK） | 我们的工具服务器 | MIT |
| pyautogui | 桌面控制（鼠标键盘、截图） | BSD-3-Clause |
| pyscreeze / pytweening / pygetwindow / pymsgbox（pyautogui 的依赖） | 同上 | MIT / MIT / BSD / BSD 风格 |
| **mouseinfo**（pyautogui 的依赖） | pyautogui 自带的小工具，voice-claude 不直接使用 | **GPL-3.0 或更高**。它只是 pip 依赖、不随 voice-claude 分发；如果你要把 voice-claude 打成独立安装包（把依赖打进去），这一项需要单独考虑 |
| Pillow | 截图编码 | MIT-CMU（HPND） |
| claude-agent-sdk（可选） | 没装官方 Claude Code 的人用它自带的 Claude Code 程序 | SDK 的代码是 MIT；**SDK 和它内含的 Claude Code 程序的使用按 Anthropic 的商业服务条款（Commercial Terms of Service）**，见 [Agent SDK 文档](https://code.claude.com/docs/en/agent-sdk/overview) |
| litellm[proxy]（可选） | 用 OpenAI API key 时的本机翻译层 | litellm 本身 MIT；`[proxy]` 会带上几十个依赖包，各有自己的许可证，其中 **litellm-enterprise 是 BerriAI 的私有许可证**（voice-claude 不用其中的企业功能），psycopg 是 LGPL-3.0 |

## 与 AI 服务相关的条款

| 项目 | 说明 |
|---|---|
| Claude Code 命令行（Anthropic） | 私有软件，不随 voice-claude 分发；由用户自己安装、自己登录或自己提供 API key。voice-claude 只是启动它，和用户在终端里自己运行它一样。**voice-claude 不实现、也不代替用户进行 claude.ai 订阅登录**："登录…"按钮打开的是 Claude Code 自己的登录 |
| Codex 命令行（OpenAI，[openai/codex](https://github.com/openai/codex)，Apache-2.0） | 不随 voice-claude 分发；用户自己安装。用 ChatGPT 订阅时，登录由 Codex 自己完成（"登录…"按钮运行 `codex login`），令牌由 Codex 保存，voice-claude 不读取、不经手；voice-claude 通过 Codex 的 app-server 接口和它对话。ChatGPT 订阅的使用按 OpenAI 的条款 |
| Anthropic、DeepSeek、Moonshot（Kimi）、OpenAI 的 API key | 用户自己的 key、自己的账单，按各家条款。只存在用户电脑的 `~/.voice-claude/auth.json`，voice-claude 永远不打包、不上传、不写进日志。选了哪一家，识别出的文字就发到哪一家的服务器（DeepSeek 和 Moonshot 的服务器在中国） |

## voice-claude 自己承诺的

- 声音只在本机处理，录音不保存、不上传。
- 识别出来的文字发给用户选定的那一家。总机的提示词里另外带着项目索引（`~/.claude/projects` 里各项目的路径、最近使用时间、别名、`MEMORY.md` 的前几行），同样发给总机当前用的那一家。
- 日志在本地，可随时删除。
- 仓库里没有密钥、没有个人设置（个人设置在 `~/.voice-claude/config.toml`，示例在 `config.example.toml`）。

## LiteLLM（可选，只在用 OpenAI API key 时）

- 项目：[BerriAI/litellm](https://github.com/BerriAI/litellm)，MIT 许可证（`[proxy]` 的依赖见上表）。不在仓库里，由 `install.bat --with-gpt` 或用户自己 `.venv\Scripts\python -m pip install "litellm[proxy]>=1.106.2"` 安装。
- 用法：后台服务在 127.0.0.1 上起一个 LiteLLM 代理，把 Claude Code 讲的 Anthropic Messages 格式翻成 OpenAI 的 Chat Completions；只拿到用户在「AI 服务商」页填的那一把 key，配置文件里不写 key，不开遥测，不联网下载模型价格表。
- 注意：2026-03-24 PyPI 上出现过 40 分钟的投毒版本 1.82.7 / 1.82.8（已下架），我们要求的最低版本远高于它。
