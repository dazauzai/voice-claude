"""MCP tool server that lets Claude adjust voice-claude's own settings and run long commands in the background.
Started by Claude Code for every line (see claude.py); runs as a plain script, so it uses nothing from the package.
It talks to the background service over the local control endpoint (VOICECLAUDE_PORT), naming its line
(VOICECLAUDE_LINE) so the service knows who is asking."""
import json
import os
import urllib.error
import urllib.request

from mcp.server.mcpserver import MCPServer

PORT = os.environ.get("VOICECLAUDE_PORT", "")
LINE = os.environ.get("VOICECLAUDE_LINE", "")
server = MCPServer("voiceclaude", instructions="语音助手 voice-claude 自身的设置（字号、麦克风、扬声器、模型、权限）和后台任务。")


def call(endpoint: str, /, **data) -> str:
    """POST the tool's arguments to the service. The endpoint is positional-only: a tool argument may be called
    `path` (board_show's image file) and must not collide with it."""
    if not PORT:
        return "语音助手没有在运行（VOICECLAUDE_PORT 未设置）。"
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/{endpoint}", data=json.dumps({**data, "line": LINE}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r).get("message", "好了。")
    except urllib.error.HTTPError as e:
        try:
            return json.load(e).get("message", str(e))
        except Exception:
            return str(e)
    except Exception as e:
        return f"语音助手没有响应（{e}）。"


def record(what: str, why: str, how: str, result: str = "", title: str = "") -> str:
    """记一条（voice-claude 的长期记忆）：一个话题结束时，把这个项目里做了什么记下来，让每个项目的每个动作以后都能追溯，
    换了模型也能接着做。what 是什么，why 为什么，how 怎么做，result 结果（没有就不填），title 可选（不给就用 what 的第一句）。
    编号、时间和格式由程序加，不要自己去写记录文件。写具体的东西：名字、数字、文件、决定。
    不记：密码、密钥、卡号、电话、邮箱、住址、证件号、账号 / 工单号 / 游戏 ID 这类标识、别人的全名、替用户发出去的消息原文，
    说“那个工单”“对方”“那封邮件”就好。一个话题记一条，不用每轮都记；每项最多两千字左右，再长会截掉。"""
    return call("record", what=what, why=why, how=how, result=result, title=title)


def board_show(kind: str, content: str = "", path: str = "", title: str = "", near_id: str = "", shape: str = "") -> str:
    """把东西放到这个项目的白板上（无边界的画布，用户也能在上面画、写、贴）。kind：
    text：一张文字卡片，content 是文字（几行，一张卡一个意思；Markdown 的标题和加粗记号会去掉），title 可选；
    image：一张图片，path 是你生成的 PNG 或 SVG 文件（最多 2 MB），title 可选，写在图下面；
    shape：一个简单图形，shape 是 rectangle、ellipse 或 arrow，content 是写在上面的字。
    会自动放在空的地方；near_id 给一个已有元素的 id（之前放东西时返回的），就挨着它放在右边。"""
    return call("board_show", kind=kind, content=content, path=path, title=title, near_id=near_id, shape=shape)


def board_look() -> str:
    """看白板现在的样子（用户说“让它看”“你看看白板”时用）：返回一张截图的文件路径（用 Read 打开看图），和白板上全部文字的原文
    （按位置从上到下）。然后根据用户画的、写的、贴的来回答。白板没开时会这么说，并列出白板上已有的文字。"""
    return call("board_look")


def board_clear() -> str:
    """清空这个项目的白板（会先口头问用户；原来的内容留一份备份）。"""
    return call("board_clear")


if LINE not in ("manager", "scratch"):  # only project lines keep records and have a whiteboard
    for _tool in (record, board_show, board_look, board_clear):
        server.tool()(_tool)


@server.tool()
def run_in_background(command: str, note: str = "") -> str:
    """把一条要跑很久的命令（下载、编译、跑测试、长脚本）放到后台独立运行：它会先向用户口头申请一次，用户说可以后，
    命令脱离你的进程运行，输出记到日志文件，你退出、语音程序重启都不影响它。note：一句话说明这是干什么的（会念给用户听）。
    返回任务编号；之后用 job_status 看进度。前台跑超过一两分钟的命令请改用这个。"""
    return call("run_in_background", command=command, note=note)


@server.tool()
def job_status(id: str = "") -> str:
    """看后台任务的状态和日志最后几行。id 留空就列出最近的任务。"""
    return call("job_status", id=id)


@server.tool()
def job_cancel(id: str) -> str:
    """停掉一个正在跑的后台任务（会先向用户口头申请）。"""
    return call("job_cancel", id=id)


@server.tool()
def get_settings() -> str:
    """查看语音助手当前的设置：字号、麦克风、扬声器、声音、语速、模型、停顿阈值。"""
    return call("get_settings")


@server.tool()
def list_devices() -> str:
    """列出这台电脑可用的麦克风和扬声器名字。"""
    return call("list_devices")


@server.tool()
def list_voices() -> str:
    """列出可以切换的朗读声音（每个有名字和编号）。用户问"有哪些声音"就调用它。"""
    return call("list_voices")


@server.tool()
def set_voice(name: str) -> str:
    """换朗读的声音。name 用声音的名字（比如 "小山"）或编号（0–102）。用户说"把声音换成小山"就直接换，不需要确认。"""
    return call("set_voice", name=name)


@server.tool()
def set_speed(speed: float) -> str:
    """改朗读语速：1.0 正常，1.2 快一点，0.8 慢一点（范围 0.6–1.6）。用户说"说快一点"就在当前语速上加 0.2。"""
    return call("set_speed", speed=speed)


@server.tool()
def set_font_size(size: int) -> str:
    """把语音助手窗口的字号改成 size（8 到 40）。用户说"放大两个号"就先用 get_settings 看当前字号再加 2。"""
    return call("set_font_size", size=size)


@server.tool()
def set_microphone(name: str) -> str:
    """切换麦克风。name 是设备名字的一部分，先用 list_devices 看有哪些。不需要向用户确认。"""
    return call("set_microphone", name=name)


@server.tool()
def set_speaker(name: str) -> str:
    """切换扬声器（朗读输出设备）。name 是设备名字的一部分，先用 list_devices 看有哪些。不需要向用户确认。"""
    return call("set_speaker", name=name)


@server.tool()
def get_permissions() -> str:
    """查看各项权限现在是"每次问"、"不用问"还是"禁止"：看屏幕、点击、输入文字、按快捷键、滚动、打开程序、
    切换模型、一次批准一串步骤、账号里的其他连接器。用户问"现在的权限设置是什么"就调用它。"""
    return call("get_permissions")


@server.tool()
def set_permission(name: str, value: str) -> str:
    """修改一项权限。name 用英文键：screenshot（看屏幕）、click（点击）、type（输入文字）、hotkey（按快捷键）、
    scroll（滚动）、open_app（打开程序）、switch_model（切换模型）、batch（一次批准一串步骤）、
    other_connectors（账号里的其他连接器）。value：ask（每次问）/ allow（不用问）/ deny（禁止）。
    这个工具会自己向用户口头确认，用户说"可以"才改；你不用先问，直接调用并转述结果。"""
    return call("set_permission", name=name, value=value)


@server.tool()
def switch_model(model: str) -> str:
    """切换这条线的模型。在 Claude 上 model 是 opus、sonnet 或 fable（用户说"飞五""飞 five""Fable 5""飞宝"都指 fable，
    直接传用户说的词也行）；在 DeepSeek 上是 flash 或 pro，在 Kimi 上是 k3、code 或 k2.6，在 GPT 上是 sol 或 luna。这个工具会自己向用户口头申请
    （fable 还会提醒它慢且贵），用户说"可以"才会切换，所以你不需要先问用户；直接调用，然后把结果告诉用户。
    切换在这一轮回答结束后生效，对话内容会保留。工具说"只能是…"时，把可选的名字告诉用户，不要自己换个名字再调一次。"""
    return call("switch_model", model=model)


@server.tool()
def set_transparent(on: bool) -> str:
    """开或关"通透模式"。开着时，除了删除东西和覆盖已有文件的操作，别的都不再向用户申请（设为禁止的仍禁止；是程序判断，不是你判断）。
    开启会先向用户口头确认，用户说"可以"才开；关闭立即生效。用户说"打开/开启通透模式""关闭通透模式"时调用，然后转述结果。"""
    return call("set_transparent", on=bool(on))


@server.tool()
def switch_provider(provider: str) -> str:
    """换一家模型厂商来答这条线：provider 是 claude、deepseek、kimi 或 openai（用户说"切到 DeepSeek""换成 Kimi""用 Claude"都算，
    直接传用户说的词也行）。这个工具会自己向用户口头申请，用户说"可以"才换，所以你不需要先问；直接调用，然后把结果告诉用户。
    换厂商在这一轮回答结束后生效，对话尽量保留；在总机上调用是换默认和所有项目线路。"""
    return call("switch_provider", provider=provider)


if __name__ == "__main__":
    server.run("stdio")
