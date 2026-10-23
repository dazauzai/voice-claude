# 前台 ↔ 后台服务 接口说明

整体结构见 [architecture.md](architecture.md)。这是两个进程之间的约定：**后台服务**（`voiceclaude/daemon.py`）和**前台**（`voiceclaude/app.py`）。前台只通过 `voiceclaude/client.py` 里的 `DaemonClient` 和后台服务说话，不直接碰线路、Claude 进程或工具服务器。

## 1. 怎么连

- 后台服务启动后把 `{"port", "pid", "token", "started"}` 写进 `~/.voice-claude/daemon.json`。
- `DaemonClient.connect(start_if_missing=True)`：读这个文件，连 `127.0.0.1:port`，第一行发 `{"hello": token, "role": "frontend"}`。文件不存在或连不上，就用脱离方式启动后台服务（`pythonw -m voiceclaude.daemon`，没有终端、独立进程组），等它写好文件再连。
- 同一时间只允许一个前台连着；新连接会把旧的挤掉（旧的收到 `bye`）。
- 连接是一条 TCP 长连接，UTF-8，一行一个 JSON。前台发**命令**（带 `id`，后台一定回一行同 `id` 的应答），后台随时推**事件**（带 `event`，没有 `id`）。

## 2. `DaemonClient`（前台用的全部接口）

```python
from voiceclaude.client import DaemonClient

client = DaemonClient(on_event)          # on_event(kind: str, data: dict)，在客户端自己的事件线程上按顺序调用；里面可以调 client 的命令（answer、reply、say…）
info = client.connect()                  # {"active": 当前线路 id, "lines": [...线路状态...], "daemon": {...}}
client.active                            # 当前线路 id，随 'line' 事件自动更新

client.switch(line_id)                   # 换线（前台一般不直接用；总机的工具会换线，前台只听 'line' 事件）
client.say(line_id, text)                # 把用户的一句话发给某条线（通常是 client.active）
client.interrupt(line_id)                # ②：叫停这条线的当前一轮
client.stop_all()                        # ③：所有线路的当前一轮和工具一起停（脱离运行的后台任务不停）
client.answer(request_id, yes, text="")  # 回答一个 'ask'：批准类给 yes=True/False；提问类给 text=用户那句话
client.phrase(key)                       # 前台听出的固定短语，key 见第 5 节；返回 {"ok", "spoken": 要念的话}
client.status()                          # {"active", "lines": [...], "jobs": [...], "unread": n}
client.reply(request_id, result)         # 回应一个 'request' 事件（见第 4 节），result 是 {"ok", "message"} 这样的 dict
client.shutdown(jobs="ask")              # 关掉后台服务（退出 voice-claude、窗口的 ×，或说"关掉后台服务"；见 6a）
client.close()                           # 只断开连接；后台服务和线路继续活着（退出时先 shutdown）
```

每个命令都阻塞到后台应答（超时 15 秒抛 `TimeoutError`；后台不在了抛 `ConnectionError`）。应答都是 `{"ok": bool, "message": str, ...}`。

## 3. 事件（后台 → 前台）

| kind | data | 前台该做什么 |
|---|---|---|
| `text` | `{"line", "text"}` | 这条线是 `client.active` 才朗读和显示（按句切分）；不是就只记到它的窗口页/忽略 |
| `tool` | `{"line", "name", "detail"}` | 当前线：显示"正在用 …"；别的线：忽略 |
| `done` | `{"line", "status": "ok"/"error"/"interrupted", "last": 最后一句}` | 当前线：这一轮结束（念完剩余、回到在听/空闲、把排队的话 `say` 出去）；别的线：忽略 |
| `ask` | `{"request", "line", "line_name", "action", "question", "detail", "kind": "approve"/"question"}` | 进入"等你回答"：念 `question`（不是当前线时念"后台的 XX 项目申请：…"），听答复，`client.answer(request, yes=…)`。`kind == "question"` 时把用户整句话作为 `text` 送回。前台会在用户正说话时等他说完再念。60 秒不答由后台当作拒绝/作罢 |
| `line` | `{"line", "name", "state", "active": bool, "unread": n}` | 更新窗口上的线路列表和顶栏；`active` 为真的那条就是新的当前线（`client.active` 已更新） |
| `notice` | `{"text"}` | 直接念出来并显示（"接到 Atlas 项目"、"你不在的时候 Atlas 做了…"、"后台任务 3 做完了"） |
| `job` | `{"job", "state": "running"/"done"/"failed"/"cancelled", "note", "tail"}` | 更新窗口上的任务列表；要不要念由后台决定（会另发 `notice`） |
| `request` | `{"request", "path", "data"}` | 后台转来的、只有前台能做的事（见第 4 节）；做完 `client.reply(request, {...})` |
| `bye` | `{"reason"}` | 后台服务关闭或被新前台挤掉：窗口显示原因，停止发命令 |

线路的 `state`：`starting` / `idle` / `working` / `waiting`（它问了你一个问题或在等批准）/ `dead`。

## 4. 转给前台做的事（`request` 事件）

工具服务器在后台服务那边，而这几件事只有前台能做，所以后台把它们原样转给前台：`get_settings`、`list_devices`、`set_font_size`、`set_microphone`、`set_speaker`、`list_voices`、`set_voice`、`set_speed`。`data` 是工具调用的参数，`reply` 返回 `{"ok", "message"}`。前台 15 秒不回，后台替它回"语音端没有响应"。

其余控制请求（`ask`、`switch_model`、`get_permissions`、`set_permission`、`run_in_background`、`job_status`、`job_cancel`、总机的五个工具）都在后台服务里处理，前台不用管。`set_permission` 和 `switch_model` 要口头确认时，后台会发 `ask` 事件，前台照常处理。

## 5. 固定短语（前台听，后台做）

前台用 `text.fixed_phrase(文字)` 在本地判定，命中就 **不发给线路**，而是 `client.phrase(key)`，然后念返回的 `spoken`：

| key | 用户说的 | 后台做的 |
|---|---|---|
| `back_to_manager` | 回到总机 / 回到 manager | 换到总机 |
| `scratch` | 接杂项 | 换到杂项线 |
| `background_mode` | 进入后台模式 | 当前线继续干，换到总机；当前线没在干活就只回一句 |
| `background_status` | 后台怎么样了 | 不换线；`spoken` 是信箱和任务的口头汇总 |
| `shutdown_daemon` | 关掉后台服务 | 关所有线路（后台任务不杀），然后退出；前台随后收到 `bye` |

"结束对话"仍由前台自己处理（退出连续对话），不经过后台。

## 5a. 通透模式

规则（程序判断，不靠模型）：开着时，权限表里所有"每次问"的动作（含一串步骤）直接执行；"禁止"的仍禁止；**删除**东西和**覆盖已有文件**仍然问，问题前面说明原因（"删除：…，可以吗？"、"会覆盖已有文件 a.docx：…"）。判断在 `voiceclaude/risk.py`（桌面按键 / 点击目标 / 输入的文字、后台命令和 Codex 的命令、Codex 的改文件），宁可多问。Claude Code 自己的 Bash / Write 在它的 auto 模式里，不经我们的门，通透与否都一样。

- 配置：`[permissions] transparent = false`，照权限表一样保存；`cfg.transparent`。
- 短语键：`transparent_on`（**总是先口头问**，原话："开启通透模式：除删除和覆盖已有文件外，所有操作直接执行，不再询问。设为禁止的仍然禁止。可以吗？"，`phrase` 立即返回 `spoken=""`，问题以 `ask` 事件到达，答可以后发 `notice` 和 `transparent` 事件）、`transparent_off`（立即生效，`spoken="通透模式已关闭。"`）。前台的短语：打开 / 开启通透模式 → `transparent_on`，关闭 / 退出通透模式 → `transparent_off`。
- 状态：握手回复和 `status()` 里有 `"transparent": bool`；变化时事件 `{"event": "transparent", "on": bool}`。
- 线路工具 `set_transparent(on)`（voiceclaude 工具服务器）：开 = 同样先口头确认，关 = 立即。
- 工具服务器在 `/ask` 里传 `facts`（单步 `{kind, keys|text|target|command|changes}`，一串步骤是列表），后台据此判断；没有 facts 时扫描问题和细节文字。

## 6. 三级打断在接口上的样子

- ①（开口）：全在前台：压低、停朗读、排队；**不发任何命令**。这一轮的 `done` 到了再把排队的话 `say` 出去。
- ②（右 Ctrl 单按）：`client.interrupt(client.active)`，本地清空排队和朗读。
- ③（按住右 Ctrl）：本地立刻停朗读，然后 `client.stop_all()`。后台对每条线发打断、0.5 秒还没停就杀 Claude 进程并 resume，然后发 `notice`"已紧急停止"；若有后台任务在跑，再发一条 `notice` 问要不要停（用户的回答走总机）。

## 6a. 关掉后台服务（窗口的退出按钮）

`client.shutdown(jobs="ask")`：`"ask"`（默认）= 有后台任务在跑就先口头问一次（随后来一个 `ask` 事件，答完才关）；`"stop"` = 先停掉所有运行中的任务再关；`"keep"` = 不问、不停任务直接关。返回 `{"ok", "message"}`。窗口用自己的对话框问过了就传 `"stop"` 或 `"keep"`。关闭时后台发一个 `bye`（"后台服务已关闭"）再断连接；客户端收到过 `bye` 就不再自己补"连接断了"的 `bye`。"keep" 的前提是任务进程真的脱离了后台：从 run.bat 起的后台是普通脱离进程，任务不随它停；经任务计划程序起的后台，任务可能随计划任务结束被一起停掉，不保证保留。

## 7. 前台启动顺序

1. 建窗口、加载语音模型。
2. `client.connect()`；拿到 `active` 和 `lines`，画线路列表。
3. 按配置 `start_with`：`"manager"`（默认）什么都不用做，后台已经把总机设成当前线；每次前台连上，后台都会发一条 `notice` 开场白（"我是总机，要去哪个项目？"，或者"现在接着 Atlas。你不在的时候…"），念出来即可。写项目名则后台启动时就接到那个项目。
4. 之后一切都是事件驱动。

## 7a. 接入：后台怎么连上 Claude

后台服务启动时判断接入方式（`voiceclaude/access.py`）：有 API key（`~/.voice-claude/auth.json` 或环境变量 `ANTHROPIC_API_KEY`）→ `api_key`；否则找到并已登录的 Claude Code → `subscription`；都没有 → `none`。`none` 时后台照常运行但**不起任何线路**：握手和 `status()` 里 `access.ok` 为假，`greeting()` 是"还没接入：原因"，`say()` 返回 `{"ok": false, "message": "还没接入：…"}`。前台据此打开「AI 服务商」页。

- `status()` / 握手回复里有 `access: {"ok", "method": "api_key"|"subscription"|"none", "message", "claude": 程序路径, "account": 账号或遮掩后的 key}`。
- `client.access_status()` → 同上。
- `client.set_api_key(key)` → 校验样子（sk-ant- 开头）、存进 auth.json（只有本用户可读）、重新检测；接入变可用时后台自动把总机线起来（随后有 `line`/`notice` 事件）；线路已经开着时各线这一轮结束后重连。返回 `{"ok", "message", "access"}`。
- `client.clear_api_key()` → 删 key，重新检测（有登录的 Claude Code 就回到订阅方式）。
- `client.test_access()` → 用当前方式真起一次 Claude 问一个字，5–20 秒，**在 UI 线程外调**；返回 `{"ok", "message", "seconds", "access"}`。
- Claude Code 程序的查找顺序：PATH 里的 `claude` → `~/.local/bin/claude.exe` → pip 包 `claude-agent-sdk` 自带的 `_bundled/claude.exe`。
- **总机不依赖接入**：`access.ok` 为假时总机仍然上线，只是换成不用模型的"规则总机"（`voiceclaude/managers.py`：认项目名、新建项目、后台怎么样了、有哪些项目、以后叫它 X、切到哪一家；没接入时转接不了），`greeting()` 是"还没接入：原因。我是总机，先只能报后台和换服务商。"；接入好了它自动换成真正的脑子。配置 `[manager] engine = auto | claude | rules`。前台不用区分，事件一样。
- 日志和事件里永远只出现遮掩后的 key（…后四位）。

## 7b. 几家并存（Claude / DeepSeek / Kimi / GPT）

provider（谁答话）是每条线路的属性，默认值在 `[providers] default`；引擎是 Claude Code（ChatGPT 订阅例外，见 7b-1）。「AI 服务商」页用下面这些命令列出每一家，每家一个 key 框，可保存 / 测试 / 清除、选默认，并显示每条线在用谁。

- `client.list_providers()` → `{"ok", "default": "claude", "providers": [每家一项], "lines": [{"line", "name", "provider", "model"}]}`。每家：`id`（`claude` / `deepseek` / `kimi` / `openai`，按这个顺序）、`name`（显示名）、`fields`（要填的框：`[{"key": "api_key", "label", "secret": true, "hint"}]`；Claude 的 hint 说"有自己登录的 Claude Code 可以不填"）、`configured`（填了 key）、`account`（遮掩后的 key，或 Claude 的登录账号）、`ok`（现在能用）、`message`（为什么能 / 不能）、`model`（这家现在用的模型 ID）、`models`（口头能说的名字，如 `["flash", "pro"]`）、`installed`（引擎或翻译层装了没）、`install_hint`（没装时的一句话）、`is_default`、`key_prefix`（前台校验样子用）、`url`（申请 key 的网页）、`subscription`（这家能用厂商自己的程序登录：claude 和 openai 为真；详见 7b-1 的 `methods`）。
- `client.set_provider_key(provider, key, switch=False)` → `{"ok", "message", "provider": {同上一家}, "default", "access"}`。校验样子（claude 要 `sk-ant-` 开头；deepseek / kimi / openai 要 `sk-` 开头、没空格、至少 20 个字符）、存 `~/.voice-claude/auth.json`、重新检测。`switch=true`（「AI 服务商」页的“保存并使用”）= 存好后若这家能用就切成默认；否则只在当时的默认那家不可用而这家刚变可用时自动切（message 里会说）；默认那家的 key 变了，各线这一轮结束后重连。`ok` 只表示 key 存没存成功（样子不对才 false）；这家现在能不能答看 `provider.ok` / `provider.message`。清掉默认那家的 key 时，默认退到 claude / deepseek / kimi / openai 里第一个能用的，一家都没有就规则总机。
- `client.clear_provider_key(provider)` → 同上形状。
- `client.test_provider(provider)` → `{"ok", "message", "seconds", "provider"}`：真起一次 Claude Code 用这家问一个字，5–40 秒（openai 第一次还要起翻译层，再加 5–10 秒），**UI 线程外调**，客户端最多等 240 秒。
- `client.switch_provider(provider, line="")` → `line=""` 改默认：新线用它，已开的项目线路和总机这一轮结束后重连到它；`line="p-…"` 只换那一条。从前台调不再口头问（点击即批准）。返回 `{"ok", "message", "default", "access"}`。口头说的"切到 DeepSeek / 换成 GPT / 用 Claude"由线路里的 `switch_provider` 工具发起 → 权限门 `switch_provider`（默认每次问）：对线路说只换这条线，对总机说换默认和所有项目线路。
- `status()` / 握手：`access` 里有 `provider`（默认那家的 id）、`provider_name`、`model`；`lines[*]` 里有 `provider`、`model`；`line` 事件里有 `provider`。`access.method` 对 Claude 仍是 `subscription` / `api_key` / `none`，对别家是 `deepseek_key` / `kimi_key` / `openai_key`（规律：`<id>_key`；以 `_key` 结尾 = 用 key 接入）。
- `set_api_key / clear_api_key / test_access` 是 `provider="claude"` 的简写。
- 没装翻译层时 openai 那家 `installed=false`：`set_provider_key` 仍可存 key，用 key 时 `test_provider` / `switch_provider` 返回 `ok=false` 和 `install_hint`；用 Codex 登录（`codex_login`）时不需要翻译层，照常可用。
- 没接入时的规则总机也认"切到 / 换成 / 用 DeepSeek|GPT|Claude"，照样先口头问再改默认。
- 换厂商后 `--resume` 不一定成（Claude 的 thinking 签名块别家不认）：失败时后台以新对话重起那条线，并发 `notice`"对话重新开始"。

### 7b-1. ChatGPT 订阅 / Codex 引擎

- `providers[*]` 里还有：`methods`（这家支持的接入方式：claude `["login", "key"]`、openai `["login", "key"]`、deepseek / kimi `["key"]`）、`login_command`（给"登录…"按钮显示：`claude auth login` / `codex login`，没有就 `""`）、`login_state`（`"logged_in"` / `"not_logged_in"` / `"not_installed"` / `""`）、`engine`（这家当前接入方式对应的引擎：`"claude"` / `"codex"`）。GPT 这一家：装了 Codex 且已登录 → `method = "codex_login"`、`engine = "codex"`、`account = "ChatGPT（Codex 已登录）"`，优先于 key；否则 key + LiteLLM 照旧。
- `client.login_provider(provider, mode="browser")` → 后台起该家自己的登录命令，**不等它完成**就返回 `{"ok", "message", "url", "code", "pending", "provider", "default", "access"}`：openai 的 `"browser"` = `codex login`（弹浏览器，`url` 备用）、`"device"` = `codex login --device-auth`（`code` + `url` 给前台显示并朗读，用户在手机或浏览器输入）；claude = 在一个新控制台里起 `claude auth login`。登录在浏览器里完成、由厂商自己的程序保存；前台之后调 `access_status` / `list_providers` 重新检测（后台不轮询）。deepseek / kimi 返回 `ok=false`"填 key 就行"。
- `client.logout_provider(provider)`：openai = `codex logout`；claude 只回一句"在终端里用 claude 自己退出"。同形返回。
- `access.method` 可以是 `codex_login`；`lines[*]` 和 `line` 事件里有 `engine`（`"claude"` / `"codex"`）。
- 权限表里有 `codex_shell`（Codex 想在沙箱外运行命令或联网）、`codex_edit`（Codex 想改工作区外的文件），默认 ask；沙箱内的命令 Codex 自己放行（和 Claude Code 的 auto 模式一个层次），我们自己的工具照旧各自口头申请。
- `test_provider("openai")` 在 `codex_login` 下真跑一轮 Codex（约 5 秒，花 ChatGPT 额度）。

## 8. 后台服务不在时

`connect()` 会自己启动它。启动失败（端口被占、口令文件坏）会抛异常，前台把原因显示并念出来（"后台服务起不来：…"）。`python -m voiceclaude.daemon --status` 在终端里能看后台服务的状态。

**怎么保证它不随前台一起死**：从资源管理器双击 run.bat 起的前台，普通的脱离启动就够了。但嵌在别的程序里的终端（Claude Desktop 的终端面板、一些 IDE）会把它开的进程都放进一个"宿主关闭就全杀"的 Windows Job 对象里，子进程逃不掉，宿主程序一重启（比如自动更新）后台服务就跟着死。所以 `spawn_detached()` 先检测自己在不在 Job 里；在的话先试"脱离 Job"启动并核实子进程确实不在 Job 里，不行就改经 **Windows 任务计划程序**启动（任务名 `voice-claude-daemon`（VOICECLAUDE_HOME 不是默认目录时带 HOME 的哈希后缀，自检的临时后台不会覆盖或 /run 用户的任务），由系统服务拉起，不属于任何前台 Job；`schtasks /create … /sc once /st 00:00 /f` 再 `/run`，任务留在那里不会再自己触发）。后台服务自己启动时也检测：发现在 Job 里就经任务计划程序把自己重拉一次再退出（`--no-relaunch` 防止循环）。任务计划程序起的进程只带用户的登录环境，不带前台的环境变量，而 `/tr` 命令行又限 261 个字符，所以前台先把所有 `VOICECLAUDE_*`（和 `CLAUDE_CONFIG_DIR`、`CODEX_HOME`）写进 `~/.voice-claude/launch-env.json`，启动器 `scripts/run_daemon.py --env-file` 读回来再起服务；密钥不走这条路（后台自己读 auth.json）。两个容易疑惑的点：(a) venv 里的 python.exe / pythonw.exe 是 Python 自带的重定向启动器，它把真正的解释器放进自己建的 job（允许子进程静默脱离、启动器退出即杀），所以经 venv 起的进程 `IsProcessInJob` 永远为真，哪怕是从资源管理器双击的；判断“要不要逃”用 `in_host_job()`：经重定向时查启动器（父进程）在不在 job，日志里写明是哪一种。(b) `schtasks /create` 建的任务默认“运行超过 72 小时就停止”“用电池不启动、切到电池就停止”，停止 = 杀掉任务的整棵进程树，所以任务改由 XML 定义注册（`~/.voice-claude/daemon-task.xml`：不限时、不管电池、无触发器只由 /run 启动、普通优先级）；XML 方式被拒时退回 `/sc once` 并在 daemon.log 里警告。
