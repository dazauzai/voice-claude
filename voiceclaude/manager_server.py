"""MCP tool server for the manager line (the receptionist): find projects, hand the user over, start new ones,
report on the background. Started by Claude Code for the manager line; talks to the daemon's control endpoint."""
import json
import os
import urllib.error
import urllib.request

from mcp.server.mcpserver import MCPServer

PORT = os.environ.get("VOICECLAUDE_PORT", "")
LINE = os.environ.get("VOICECLAUDE_LINE", "manager")
server = MCPServer("manager", instructions="总机的工具：查项目清单、转接到项目、新建项目、报告后台情况、记别名。")


def call(endpoint: str, /, **data) -> str:  # positional-only: a tool argument may be called `path`
    if not PORT:
        return "后台服务没有在运行（VOICECLAUDE_PORT 未设置）。"
    body = json.dumps({**data, "line": LINE}).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/{endpoint}", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            d = json.load(e)
        except Exception:
            d = {"message": str(e)}
    except Exception as e:
        return f"后台服务没有响应（{e}）。"
    msg = d.get("message") or ("好了。" if d.get("ok") else "没成功。")
    if d.get("candidates"):
        msg += "（候选：" + "、".join(d["candidates"]) + "）"
    return msg


@server.tool()
def list_projects() -> str:
    """列出所有项目：名字、目录、最近活跃、别名、一句话主题。拿不准用户说的是哪个项目时先看这个。"""
    return call("list_projects")


@server.tool()
def switch_to(project: str) -> str:
    """把用户转接到一个项目的对话。project 用项目名或别名（比如 "Atlas"、"Beacon"、"服务器"）。
    程序会先口头问用户"要接到 XX 项目吗？"，用户说可以才转，成功后程序会念"接到 XX 项目"，你都不用再说；
    用户之后直接和那个项目对话，你退场。返回"用户没有同意"时，问一句用户想去哪里。
    返回"有几个都像"时，把候选念给用户选，不要自己猜。"""
    return call("switch_to", project=project)


@server.tool()
def new_project(name: str, dir: str = "", read_from: list[str] = []) -> str:
    """新建一个项目并转接过去。name 是项目名；dir 可选，默认放在用户的 Documents 下；read_from 可选，
    写要只读回看记忆的现有项目名（跨项目问题时用户选"新开一个"就这样建），它们的记忆会被复制一份快照进去。"""
    return call("new_project", name=name, dir=dir, read_from=list(read_from or []))


@server.tool()
def background_status() -> str:
    """报告后台情况：哪些项目线路在干活、闲着、等用户回答，有哪些后台任务跑完了或在跑。用户问"后台怎么样了"就调用它。"""
    return call("background_status")


@server.tool()
def remember_alias(alias: str, project: str = "") -> str:
    """记一个别名：用户说"以后叫它 服务器"就调用。project 是现有项目名；留空指当前接着的项目。"""
    return call("remember_alias", alias=alias, project=project)


if __name__ == "__main__":
    server.run("stdio")
