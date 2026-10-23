# voice-claude 使用手册

简要说明见 [README](../README.md)。

用说话代替打字，和 Claude Code 一起工作。中英混说都行，语音识别和朗读全部在本机完成。也可以接 DeepSeek、Kimi、GPT（API key 或 ChatGPT 订阅）。

English: [guide.en.md](guide.en.md)

独立的开源项目，与 Anthropic、OpenAI 没有关联，也没有得到它们的认可。Claude、Claude Code 是 Anthropic 的商标；Codex、ChatGPT 是 OpenAI 的商标。

## 怎么用

程序开着的时候，窗口角落会显示当前状态。主要只用一个键（默认**右 Ctrl**），第二个键（默认**右 Alt**）管静音和"马上发"：

| 操作 | 效果 |
|---|---|
| 单按 | 开始录音；再单按一次 → 发出去，它边想边念 |
| 双按 | 进入**连续对话**：你说完它自动回答，回答完自动继续听，全程不碰键盘鼠标 |
| 连续对话中双按，或说"结束对话" | 退出连续对话 |
| 它说话时单按 | 打断它；如果你刚说的话还在排队，第一下只撤销排队（或说"撤销排队"），再按一下才打断 |
| 按住 0.8 秒 | 紧急停止：所有线路和工具 |
| 右 Alt 单按，或点"静音" | 麦克风静音 / 恢复 |
| 右 Alt 双按 | 不等它说完：叫停这一轮，排队的话马上发过去（`hotkey.send_key` 可以换键或留空关掉） |
| 有申请卡时 | 说"可以 / 不行"；或者右 Alt（"可以"按钮）= 可以，右 Ctrl（"不行"按钮）= 不行 |
| 连续对话中直接开口（戴耳机时） | 它停下朗读；手上的活继续，你的话排队 |

说话时停顿 2 秒以内不会被切断（可以在配置里调）；只说了"呃""嗯"的那一段不会发出去。代码块只显示在窗口里，不会念出来（提示词要求模型把表格也放进代码块）。

### 窗口

![在听](ui/preview-small-listening.png) ![等你回答](ui/preview-small-request.png)

窗口通常小而置顶，放在屏幕一角，瞄一眼就够。从上到下：

- **状态条**：一个状态球加一行大字。整条的底色只回答一个问题——**绿 = 麦克风开着，说话它听得见**（录音中、在听、连续对话里它在想或在说时你开口也能插话）；**深琥珀 = 等你决定**；灰 = 它在忙或闲着；粉 = 坏了。球的形状也不一样：竖条随音量起伏是在听（有框 = 录音中，要你再按一下才发），蓝色实心圆呼吸是在说，转圈是在想，白方块问号是等你回答。静音是叠在球角上的一个徽标，字后面加" · 已静音"。开着通透模式时，状态条上写"通透"。右上角的小芯片写着当前线路，角标是没听的条数（琥珀色 = 有别的线路在等你）；点开是线路和后台任务的面板。
- **对话卡**：**你** / 答话的那一家（Claude、DeepSeek、GPT…）/ **通知** 三种说话人，工具调用（"· Bash …"）和备注退成灰色小字。往上翻旧回答时不会被拽回底部，右下角会出现"↓ 回到最新"。
- **申请卡**：它想做一件要你批准的事时，一张黄卡停靠在按钮上方（翻旧记录、开着设置页也挡不住），顶边的细条是剩余时间，右上角写"N 秒后算不行"。答完它搬进对话记录变成白卡，你的回答写在卡片里（"你说：可以""你按了右 Alt，算可以"）。后台线路问的，标题写"后台的 Atlas 申请 · 点击"，状态条写"Atlas 在等你"。
- **三个按钮**：说一段 / 发送 / 打断 / 撤销排队 · N / 不行，● 开始对话 / ■ 结束对话，静音 / 取消静音 / 可以；右边是设置。按钮和快捷键是一回事。
- **提示行**：一行、随状态变的快捷键提示，行尾"隐藏"可以关掉（设置页里能再打开，记在 `[ui] show_hints`）。

![在说](ui/preview-small-speaking.png) ![大窗](ui/preview-large.png)

窗口拉到 640 逻辑像素以上宽时，左边多一栏常驻的线路和后台任务，对话列封顶在 38 个汉字宽、居中。

设置页（点"设置"或 Ctrl+,，[截图](ui/preview-settings.png)、[下半部分](ui/preview-settings-bottom.png)）在同一张白卡里：最上面是「AI 服务商」和「权限」两个子页的入口，下面是麦克风、扬声器、字号、始终置顶、快捷键提示、配置文件，最底下是"退出 voice-claude"。

- 「权限」子页（[截图](ui/preview-permissions.png)）：最上面是通透模式开关（见下面"桌面控制"）；下面每项"询问 / 允许 / 禁止"，点一个新值等于替你说一句"把点击的权限改成允许"，照常口头确认。
- 「AI 服务商」页（[截图](ui/preview-access.png)；说"打开 AI 服务商"也能打开）：选谁来答话、填各家的 key、用 Claude Code 或 Codex 登录；一家都连不上时会自动弹出。
- 退出：窗口右上角的 ×、设置页的"退出 voice-claude"、线路菜单（[截图](ui/preview-menu.png)）里的"退出"都一样，停掉后台服务并关窗口；有线路正在回答或后台任务在跑时先问一句。

窗内快捷键：Esc 关面板 / 设置页，End 回到最新，PageUp / PageDown 翻页，Ctrl+= / Ctrl+− 字号，Ctrl+, 设置，Ctrl+L 线路面板，Ctrl+T 置顶。字号 8–40 都能用，部件跟着字号一起变大。

设置也可以口头改：

- "把字号放大两个号" / "麦克风换成 USB 那个" / "扬声器换成显示器" —— 说了就改。
- "把模型切换成 sonnet" —— 它会先问你"要把模型切换成 sonnet，可以吗？"，你说"可以"才切，说"不行"就不切。这一步是程序层面的硬门槛（工具拿不到你的"可以"就不执行），不是靠提示词。切换后对话内容保留。

改过的设置会写回 `~/.voice-claude/config.toml`，下次启动还在。

## 安装

需要 Windows 10/11、Python 3.11 以上，和约 1.2 GB 磁盘空间（另装 LiteLLM 再加约 550 MB）。安装 Python 时勾选 "Add python.exe to PATH"（[官网](https://www.python.org/downloads/windows/)，国内可用[镜像](https://registry.npmmirror.com/binary.html?path=python/)）。

然后双击 `install.bat`（Windows 默认隐藏扩展名，它可能显示为 "install"，类型是"Windows 批处理文件"）。它依次：

1. 创建运行环境 `.venv`；
2. 安装依赖；
3. 没装官方 Claude Code 时，装 `claude-agent-sdk`（约 130 MB，自带一份 Claude Code 程序；用 DeepSeek、Kimi 等别家时也由它运行）；
4. 下载语音模型（约 640 MB：SenseVoice 识别、Silero VAD 断句、Kokoro 朗读；许可证见 [NOTICE.md](../NOTICE.md)）；
5. 问要不要装 LiteLLM（约 550 MB，默认不装）：只有用 OpenAI 的 API key 时需要，用 ChatGPT 订阅登录不需要；以后要装：`install.bat --with-gpt`。

下载源自动选：PyPI 或 GitHub 连不上、太慢时，改用清华 PyPI 镜像和 hf-mirror.com；`install.bat --mirror` 一律用国内镜像。中途失败了再双击一次，已完成的步骤会跳过。

装好后双击 `run.bat`。第一次启动会打开「AI 服务商」页，任选一家：

- **Claude**：用自己登录的 Claude Code（Pro / Max 订阅，点「登录…」），或填 Anthropic API key；
- **DeepSeek**、**Kimi（月之暗面）**：填 API key；
- **GPT（OpenAI）**：用 ChatGPT 订阅登录——先装 OpenAI 的 Codex 命令行：从[官方发布页](https://github.com/openai/codex/releases)下载 Windows 版，把里面的 `codex.exe` 放到 `%LOCALAPPDATA%\Programs\Codex\bin\` 或 PATH 里的任意文件夹（npm 装的 `codex.cmd` 包装不支持）；再点 GPT 的「登录…」，Codex 当引擎，我们不碰令牌；或者填 OpenAI API key（需要上面第 5 步的 LiteLLM）。

几家可以并存，口头"切到 DeepSeek"就换。

不用 `install.bat` 时，手动装：

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install claude-agent-sdk
.venv\Scripts\python scripts\download_models.py
```

## 运行

在资源管理器里双击 `run.bat`，或者：

```bash
.venv\Scripts\python -X utf8 -m voiceclaude
```

第一次启动会加载模型（约 3 秒）并启动"后台服务"（一个看不见的独立进程，总机、各项目的对话和后台任务都住在里面），然后你会听到"我是总机，要去哪个项目？"。每个项目的对话下次打开时自动接着上次的。窗口意外关掉时后台服务还在，重新打开会接上。

什么都没接入时（没登录的 Claude Code，也没有 API key）程序照样能启动：会打开「AI 服务商」页让你选一种方式，总机先只能报后台和换服务商，接入好了才能转接。

从嵌在别的程序里的终端（比如某些 IDE 的终端面板）启动时，后台服务会经 Windows 任务计划程序启动，这样那个程序关掉时它不会跟着停；这会留下一个名为 `voice-claude-daemon` 的计划任务（只在被调用时运行）。

有用的命令：`.venv\Scripts\python -m voiceclaude.daemon --status` 看后台服务在不在。日志：`voice-claude.log`（前台）、`~/.voice-claude/daemon.log`（后台）。

## 配置

你的设置在 `~/.voice-claude/config.toml`（第一次运行时从 [config.example.toml](../config.example.toml) 复制过去，每一项都有注释），改完重启程序。主要的几项：

- `hotkey.key`：主键，默认 `right ctrl`。可选 `left ctrl`、`pause`、`scroll lock`、`f13`–`f24`。Shift（输入法切中/英、粘滞键）、Caps Lock（切大写）、Insert（改写模式）单独按都有副作用，所以不提供。`hotkey.send_key`：第二个键（静音 / 马上发 / 申请时的"可以"），默认 `right alt`，留空就不用。`hotkey.hold_seconds`：按住多久算紧急停止，默认 0.8 秒。
- `audio.microphone` / `audio.speaker`：留空用 Windows 默认设备，或填名字的一部分（如 `"USB"`、`"Realtek"`）。启动时窗口会显示当前用的设备，并说一句"准备好了"；听不到就改这里。
- `audio.silence_seconds`：停顿多久算说完。`audio.barge_in`：用外放时改成 `false`。`audio.duck_seconds` / `cut_seconds`：你开口多久后它的声音压低、停掉。
- `claude.model` / `claude.effort`：项目线路用的模型和思考深度。示例默认 `opus` + `low`（开放性问题约 3 秒开始说；`medium` / `high` 更深但更慢）。可以写 `opus`、`sonnet`、`fable` 或完整型号名；也可以口头说"切到 sonnet"，它会先问你再切，对话保留。总机用 `[manager]` 里单独的模型（默认 `sonnet`；总机在别家上时用那家的模型），不受影响。
- `[manager] engine`：总机的脑子，`auto` = 能连上模型就用它，否则用不需要模型的规则总机。总机的提示词可以用 `~/.voice-claude/manager-prompt.md` 覆盖。
- `[providers]`：谁来答话（`claude` / `deepseek` / `kimi` / `openai`）和各家用的模型名；key 不在这里，在「AI 服务商」页填。口头"切到 DeepSeek / 换成 GPT / 用 Claude"会先问你；对总机说是换默认，对项目线说只换那一条。
- `[daemon]`：后台服务的端口、同时开几条线路、闲置多久关线、开机先接谁。
- `[permissions]`：每个动作"每次问 / 不用问 / 禁止"，`transparent = true` 是通透模式（见下面"桌面控制"）。
- `[desktop.apps]`：允许打开的程序清单。
- `tts.voice`：朗读的声音，运行 `.venv\Scripts\python -m voiceclaude.voices 3 18 30 50` 试听。`ui.font_size`：窗口字号。
- `[replacements]`：识别常错的词，比如 `"Cloud" = "Claude"`。

API key 不在这个文件里，在 `~/.voice-claude/auth.json`（只有你能读），由「AI 服务商」页写入。

## 自检

三种测试，详见 [CONTRIBUTING.md](../CONTRIBUTING.md)：

- 单元测试（不需要模型、麦克风、模型服务或密钥，约 30 秒）：`.venv\Scripts\python -X utf8 -m unittest discover -s tests`
- 前台 + 假后台（需要语音模型，不需要模型服务）：`.venv\Scripts\python -X utf8 scripts\frontend_harness.py`
- 整体自检（真的临时后台服务 + 真的模型，约 5 分钟，会花你的额度）：`.venv\Scripts\python -X utf8 scripts\selftest.py`。它用合成的语音代替你说话，不模拟按键，不碰你正在用的 voice-claude 和 `~/.voice-claude`。跑之前确认电脑是你自己在用。

## 隐私

- 语音识别和朗读都在本机 CPU 上运行，录音不保存、不上传。
- 识别出来的文字发给你选的那一家（经 Claude Code 或 Codex），和你平时在终端里用它们一样；对话保存在它们自己的会话记录里。
- 截图：模型调用"看屏幕"时，整个屏幕的截图（缩到 1280 像素宽）会发给那一家。"看屏幕"设成"允许"或开着通透模式时，截图不再先问你。
- 总机为了听懂"去哪个项目"，提示词里带着项目索引：`~/.claude/projects` 里每个项目的路径、最近使用时间、别名和 `MEMORY.md` 的前几行。这些会发给总机当前用的那一家。
- 日志里有你说的话、它的回答和要输入的文字，方便排查问题，都在本机、可以随时删除：程序文件夹里的 `voice-claude.log`、`desktop.log`；`~/.voice-claude/` 里的 `daemon.log`、`manager/log/`（和总机的每段对话）、`jobs/`（后台任务的输出）、`gateway.log`、`lines/*/codex-stderr.log`。密钥只在本机，不进日志和窗口。

## 桌面控制

它可以看屏幕、点鼠标、输入文字、按快捷键、滚动、打开程序。**每个动作做之前它都会口头问你**，比如"我想打开记事本，可以吗？"；你说"可以"（或按右 Alt）才做，说"不行"（或按右 Ctrl）就不做。输入文字和按键时它会念出目标窗口的名字和要输的内容，所以你听到什么就是会发生什么。这道门在工具的代码里，不是靠模型自觉。

- 打开程序只认一张清单（记事本、计算器、画图、文件管理器、浏览器、终端、设置……），在 `config.toml` 的 `[desktop.apps]` 里加自己的。
- 每一项动作在 `[permissions]` 里可以设成"每次问 / 不用问 / 禁止"，也可以口头说"把截图权限改成不用问"（改权限本身会先口头向你确认）；"现在的权限设置是什么"可以查。`batch` 一项决定"打开记事本并输入 hello"是问一次做两步，还是每步都问。
- **通透模式**：说"打开通透模式"或在「权限」页打开（会先口头确认一次）。开着时，所有"每次问"的动作直接执行，只有两类还会问：**删除**（Delete 键、点"删除 / 移到废纸篓"、`rm`、`del`、`Remove-Item` 这类命令）和**覆盖已有文件**（Windows 问"文件已存在，要替换吗"时确认、另存为一个已存在的文件名、命令把输出写到已存在的文件；存一个新文件不问），申请会先说原因，比如"会覆盖已有文件 report.docx：…"。判断看的是动作本身：按的键、输入的文字、命令、要改的文件；点击看的是模型给的目标名（程序读不到屏幕上真正的按钮名）。能认出的是常见的删除和覆盖操作，认不出的不会问。设成"禁止"的仍然禁止。说"关闭通透模式"马上关。这道门只管 voice-claude 自己的工具（桌面控制、后台命令、Codex 的申请）；Claude Code 自己的命令和改文件由它的 auto 模式检查，不经过这里。
- 鼠标甩到屏幕**左上角**，鼠标键盘动作立刻中止。
- 小心一点：Windows 11 的记事本会恢复你上次打开的文件，"输入文字"会输进当前前台的那个标签页——所以听清它念的窗口名再说"可以"。
- 不想要这个功能：`[desktop] enabled = false`。每个动作和你的答复都记在 `desktop.log` 和 `voice-claude.log` 里。

设计说明在 [docs/desktop-control.md](desktop-control.md)。

## 权限

Claude Code 以 `--permission-mode auto` 运行：普通操作不用批准，每个动作都由它自己的风险检查把关。另外按前缀禁止了常见的删除命令（`rm`、`del`、`Remove-Item`、`git reset --hard`、`git push --force` 等）和用命令行打开程序（`start`、`Start-Process` 等，打开程序请走上面的桌面控制）。这些前缀规则是减速带，不是保证（换个写法就绕过了），真正的检查是 auto 模式。用 ChatGPT 订阅时引擎是 Codex：它在工作区沙箱里运行，沙箱外的命令和改工作区外的文件走口头申请（`codex_shell` / `codex_edit`）。

需要你点头的事（切换模型、桌面控制的每个动作、改权限）走口头批准；其他拿不准的动作会被自动模式的检查直接拒绝，它会告诉你，你可以换个说法或自己做。`[permissions]` 里的 `other_connectors` 决定语音会话挂不挂你账号里的其他连接器（Docs、Drive、Chrome 插件），默认不挂，因为它们动起来不经过申请。

## 已知限制

- 中文句子里的英文术语偶尔识别错，模型一般能按意思理解；常错的词可以加进 `[replacements]`。
- Kokoro 朗读中文夹英文时，英文单词的发音一般。
- 开口打断需要戴耳机（按键打断不需要）。
- 当焦点在以管理员身份运行的窗口上时，快捷键收不到。
- 它干活（读文件、跑命令）可能要几十秒，窗口会显示它在做什么。

## 结构

```
voiceclaude/
  前台（一个窗口 + 话筒）
    app.py          状态机 + 窗口
    ui_access.py    「AI 服务商」页
    ui_theme.py / ui_widgets.py  颜色、字号、提示行 / 画出来的部件
    hotkey.py       单按 / 双按 / 按住检测（轮询 GetAsyncKeyState，不装键盘钩子）
    audio.py        麦克风、VAD 断句、播放、压低
    asr.py / tts.py SenseVoice 识别 / Kokoro 朗读
    text.py         按句切分、去 markdown、听懂"可以/不行"和固定短语
    client.py       前台连后台服务的那一小段
  后台服务（独立进程 daemon）
    daemon.py       服务本体：本机端口、脱离启动（Job 里时经任务计划程序）
    switchboard.py  总机逻辑：线路、转接、信箱、权限门、后台任务的申请
    risk.py         通透模式下判断"删除 / 覆盖已有文件"
    line.py         一条线 = 一个 Claude Code 或 Codex 对话
    claude.py       常驻的 claude -p 进程（stream-json），带 --mcp-config
    engine_codex.py Codex app-server 引擎（ChatGPT 订阅登录）
    gateway.py      LiteLLM 翻译层（OpenAI API key）
    managers.py     不用模型的"规则总机"
    projects.py     项目索引（从 ~/.claude/projects 自动生成）+ 别名
    jobs.py         脱离运行的后台任务
    access.py       接入方式：各家的登录 / API key
    control.py      本机 HTTP 小端点，工具服务器通过它递申请
    prompts.py      线路和总机的提示词
    paths.py        ~/.voice-claude 里有什么
  给模型用的工具（MCP 服务器，每条线一套）
    mcp_server.py      设置、权限、后台任务
    desktop.py         桌面控制（每个动作先口头申请）
    manager_server.py  总机的工具（转接、新建项目、报后台）
docs/        architecture.md（整体结构），protocol.md（前台和后台服务之间的协议），desktop-control.md，ui/（截图）
scripts/     安装、下载模型、自检、前台假后台、截图
```

这个项目在开发中大量使用了 Claude Code 辅助编写。

## 卸载

删掉程序文件夹（里面有 `.venv` 和 `models`）和 `~/.voice-claude`。如果后台服务曾经经任务计划程序启动，再运行 `schtasks /delete /tn voice-claude-daemon /f`。

## 许可证

MIT（见 [LICENSE](../LICENSE)）。语音模型不在仓库里，由 `scripts/download_models.py` 下载，各有自己的许可证（SenseVoice 模型许可、Silero VAD MIT、Kokoro Apache-2.0）；全部第三方组件和条款见 [NOTICE.md](../NOTICE.md)。Claude Code 是 Anthropic 的软件、Codex 是 OpenAI 的软件，都由你自己安装和登录，voice-claude 不替你登录，也不碰登录令牌。
