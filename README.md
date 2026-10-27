**中文** | [English](README.en.md)

# voice-claude

在 Windows 上用语音和 Claude Code 对话，不用打字。

中英文可以在一句话里混着说。语音识别（SenseVoice）和朗读（Kokoro）都在本机 CPU 上跑，除了 Claude 也能用 DeepSeek、Kimi 和 GPT。

<img src="docs/ui/preview-small-speaking.png" width="315" alt="voice-claude 正在朗读回答">

## 安装

需要 Windows 10 或 11、Python 3.11 以上，以及约 1.2 GB 磁盘空间。Python 可以从 [python.org](https://www.python.org/downloads/windows/) 下载，国内可以用 [npmmirror 镜像](https://registry.npmmirror.com/binary.html?path=python/)。安装时勾选「Add python.exe to PATH」。

1. 下载或克隆仓库：

   ```
   git clone https://github.com/dazauzai/voice-claude
   ```

2. 双击 `install.bat`。Windows 默认隐藏扩展名，它可能显示成「install」，类型是「Windows 批处理文件」。

`install.bat` 会创建 `.venv`、安装依赖、下载语音模型（约 640 MB）。如果你没装 Claude Code，它还会装 `claude-agent-sdk`（约 130 MB，自带一份 Claude Code，DeepSeek 和 Kimi 也用它运行）。最后它会问要不要装 LiteLLM（约 550 MB）。只有用 OpenAI API key 才需要它，用 ChatGPT 订阅不需要。默认不装，以后要装就运行 `install.bat --with-gpt`。

下载源会自动选能连上的：PyPI 或清华镜像，GitHub 或 hf-mirror.com。`install.bat --mirror` 一律用国内镜像。中途失败了就再运行一次，已经完成的步骤会跳过。

也可以手动安装：

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install claude-agent-sdk
.venv\Scripts\python scripts\download_models.py
```

已经装了 Claude Code 的话，第三行可以跳过。

## 第一次启动

双击 `run.bat`，命令行窗口闪一下就关，程序本身没有命令行窗口。第一次启动要加载模型（约 3 秒）并启动后台服务，然后你会听到“我是总机，要去哪个项目？”。

总机和每个项目各是一条线路。说出项目名，总机就把你转到那个项目的线路。

还没接入任何模型时，「AI 服务商」页会自己打开，在这里选谁来回答你。可以同时配好几家，随时口头切换。

- **Claude**：点「登录…」用你自己的 Claude Code（Pro / Max），或者填 Anthropic API key。
- **DeepSeek**、**Kimi（月之暗面）**：填 API key。每家旁边都有「获取 API key」链接。
- **GPT（OpenAI）**：用 ChatGPT 订阅的话，先从 [Codex 发布页](https://github.com/openai/codex/releases) 下载 Windows 版，把 `codex.exe` 放进 `%LOCALAPPDATA%\Programs\Codex\bin\` 或 PATH 里的任意文件夹（不支持 npm 装的 `codex.cmd`），再点 GPT 的「登录…」。也可以填 OpenAI API key（需要 LiteLLM）。

API key 存在 `~/.voice-claude/auth.json` 里，只有你的账户能读。

## 怎么用

主键是右 Ctrl，第二个键是右 Alt。

| 操作 | 效果 |
|---|---|
| 右 Ctrl 单按 | 开始录音，再按一下发出去；它在干活或说话时按，就是打断它 |
| 右 Ctrl 双按 | 开 / 关连续对话：你说完它就回答，答完接着听，不用按键 |
| 右 Ctrl 按住 0.8 秒 | 紧急停止：所有线路正在做的事和工具都停（放到后台的任务不停） |
| 右 Alt 单按 | 麦克风静音 / 恢复 |
| 右 Alt 双按 | 叫停这一轮，马上把你刚才插的话发过去 |

连续对话时戴着耳机，直接开口它就停止朗读；它手上的活继续，你的话排队。说话中间停顿不到 2 秒不会被切断。代码块只显示，不念出来。

不方便说话时，在窗口下方的输入框里打字，回车发送（Shift+回车换行），效果和说话一样。

想不按键就叫它：双击 `standby.bat` 进入值守，说“hi chat”就会打开窗口开始听（唤醒词可以改，设置页里能设成开机自动进入值守）。

每轮回答下面会显示这一轮用了多少 token；设置页的「用量」能看到今天的合计，用 Claude 订阅时还有剩余额度（说“用量怎么样”也会念出来）。

要看图、看结构时说“打开白板”：每个项目有一块自己的白板（Excalidraw，鼠标、触屏和笔都能画），它会把卡片和图放上去，你画了或写了之后点「让它看」，它就看得到。

每个项目有一份记录：一个话题告一段落时，它在 `~/.voice-claude/records/` 里记下做了什么、为什么、结果如何，下次打开这个项目时会先读最近几条（设置 → 记录 → 打开）。

说错了或想换个说法：说“撤回上一句”（可以连着说），或者在窗口里右键点你说过的某一句，选「从这里重来」。对话回到那句之前，那句话放回输入框，改完再发。如果之后的几轮改过文件，会问你要不要一起改回去。

常用的几句话：

- “去 Atlas”、“接我到 Atlas 项目”：总机会先问“要接到 Atlas 吗？”
- “回到总机”、“后台怎么样了”、“撤回上一句”
- “切到 DeepSeek”、“把模型切换成 sonnet”：都会先问你
- “打开 AI 服务商”、“打开通透模式” / “关闭通透模式”、“结束对话”

“回到总机”“撤回上一句”这类固定说法要单独说（前面可以带“好”“能不能”“帮我”）；夹在一句话里时，当普通的话发给模型。

设置也能口头改，比如“把字号放大两个号”、“麦克风换成 USB 那个”。所有设置都在 `~/.voice-claude/config.toml`（从 `config.example.toml` 生成，每一项都有注释）。

要退出，就点窗口的 ×、设置里的「退出 voice-claude」或线路菜单里的「退出」。三种方式都会停掉后台服务、关掉窗口；有线路正在回答或后台任务在跑时，会先问你。

## 申请和桌面控制

凡是要动你电脑的事，它都先开口问你，同时按钮上方出现一张黄色申请卡。要打字时，它会说出输进哪个窗口：

> 我想打开记事本，然后在记事本里输入文字 hello，可以吗？

只有你答应了才执行。说“可以”或“不行”，或者按右 Alt 表示可以、右 Ctrl 表示不行。话里带否定（“嗯，不行”）就算不行，听不清会再问一次，60 秒没回答算不行。这个确认是工具代码强制的，不靠模型自觉。

每个动作都可以在「权限」页或口头设成询问、允许或禁止，改权限本身也会口头确认。把鼠标甩到屏幕左上角，鼠标和键盘动作立刻停止。

**通透模式**：嫌它问得太多，就口头或在「权限」页打开（确认一次，状态栏显示「通透」）。开着时，设成询问的动作直接执行，只有删除和覆盖已有文件还会先说原因再问；设成禁止的照样禁止。点击时它只能看模型给的按钮名，读不到屏幕上真正的按钮；它只认得常见的删除和覆盖操作，也只管 voice-claude 自己的工具。Claude Code 执行的命令和改的文件归 Claude Code 的 auto 模式管，见[使用手册](docs/guide.md)。

## 隐私

- 声音只在你的电脑上处理，录音不保存、不上传。
- 识别出的文字发给你选的那家服务商。
- 模型看屏幕时，整个屏幕的截图会发给那家服务商。会先问你，除非「看屏幕」设成了允许，或者开着通透模式。
- 为了听懂项目名，总机的提示词里带着项目索引：`~/.claude/projects` 里每个项目的路径、最近使用时间、别名和 `MEMORY.md` 的前几行。
- 日志里有你说过的话，都是本地文件，可以删：程序文件夹里的 `voice-claude.log`、`desktop.log`，`~/.voice-claude/` 里的 `daemon.log`、`manager/log/`、`jobs/`。API key 不会写进日志。

## 已知问题

- 中文句子里夹的英文词有时会识别错，可以在配置的 `[replacements]` 里加一条替换。
- Kokoro 念中文里夹的英文，发音一般。
- 当前窗口是以管理员身份运行的程序时，快捷键不起作用。

## 卸载

删掉程序文件夹（里面有 `.venv` 和 `models`）和 `~/.voice-claude`。如果你在别的程序的内置终端里启动过后台服务，系统里会留下一个计划任务，再运行 `schtasks /delete /tn voice-claude-daemon /f` 删掉它。

## 文档

- [使用手册](docs/guide.md)（[English](docs/guide.en.md)）：窗口说明、全部配置项、权限表、几种打断方式、测试
- 设计文档：[整体结构](docs/architecture.md)（英文）、[窗口和后台服务之间的协议](docs/protocol.md)、[桌面控制](docs/desktop-control.md)
- [第三方组件和许可证](NOTICE.md)
- [参与开发](CONTRIBUTING.md)

## 许可证

MIT，见 [LICENSE](LICENSE)。语音模型不在仓库里，是安装时下载的，各有自己的许可证，见 [NOTICE.md](NOTICE.md)。

这是独立项目，与 Anthropic、OpenAI 没有关联，也没有得到它们的认可。Claude、Claude Code 是 Anthropic 的商标，Codex、ChatGPT 是 OpenAI 的商标。Codex 要你自己装；Claude Code 和 Codex 都要你自己登录，voice-claude 不替你登录，也不碰登录令牌。

这个项目在开发中大量使用了 Claude Code。
