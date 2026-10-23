"""Manager (总机) brains that do not need Claude.

The manager's whole job is small: read the project index, cope with mis-heard words, and pick one of a few
actions (switch_to / new_project / background_status / remember_alias / list_projects) or just answer. So it can
be served by (a) Claude Code with the manager MCP tools (the default) or (b) these plain rules, which need no model
at all and are also the fallback when nothing is connected. A RulesSession plugs into a Line exactly like a
ClaudeSession."""
from __future__ import annotations

import logging
import re
import threading
from typing import TYPE_CHECKING, Any

from . import projects

if TYPE_CHECKING:
    from .line import Line
    from .switchboard import Switchboard

log = logging.getLogger(__name__)

_STRIP = re.compile(r"(接我到|帮我接到|接到|转到|转接到|切到|切换到|去|进|接|到|那个|那边|项目|的|吧|呢|啊|呃|嗯|请|帮我|我想|我要|一下)")
_PUNCT = re.compile(r"[\s。！？!?，,、.　]+")
_NEW = re.compile(r"(?:新建|新开|建一个|建个|开一个|开个)(?:一个)?(?:叫|名字叫)?(.+?)(?:的)?(?:项目|对话)?$")
_ALIAS = re.compile(r"(?:以后|以后就)?(?:叫它|叫他|管它叫|别名叫|别名是|别名)(.+)$")
_STATUS = ("后台怎么样", "后台情况", "后台怎样", "后台状态", "后台有什么", "任务怎么样")
_LIST = ("有哪些项目", "项目清单", "列出项目", "有什么项目", "项目列表", "都有哪些")
_PROVIDER = re.compile(r"(?:切到|切换到|换成|换到|改用|用)(deepseek|深度求索|kimi|月之暗面|moonshot|gpt|openai|chatgpt|claude|克劳德|anthropic)", re.I)


def _clean(text: str) -> str:
    return _PUNCT.sub("", text).strip()


class RuleManager:
    """Turns one spoken sentence into an action on the switchboard and a spoken reply."""

    def __init__(self, sb: "Switchboard"):
        self.sb = sb
        self.after_turn: list = []  # actions that replace this very manager (a provider switch) run after "done"

    def handle(self, text: str) -> str:
        t = _clean(text)
        if not t:
            return "没听清，要去哪个项目？"
        if any(k in t for k in _STATUS):
            return self.sb.background_status_text()
        m = _PROVIDER.search(t)
        if m:
            # Through the same spoken gate as the model manager's switch_provider tool (permissions:
            # switch_provider); once this turn is over the real manager comes up on that provider.
            pid = m.group(1)
            st = self.sb.provider_status(pid)
            if not st.ok:
                return st.message
            ok, _ = self.sb.gate(self.sb.active_id or "manager", "switch_provider",
                                 f"要把默认换到{st.provider_name}（{st.model}）吗？")
            if not ok:
                return "好，不换。"
            self.after_turn.append(lambda: self.sb.switch_provider(pid))
            return f"好，换到{st.provider_name}。"
        if any(k in t for k in _LIST):
            names = "、".join(p.display_name for p in self.sb.index[:8]) or "还没有项目"
            return f"项目有：{names}。"
        m = _ALIAS.search(t)
        if m:
            r = self.sb.remember_alias(_STRIP.sub("", m.group(1)), "")
            return r["message"]
        m = _NEW.search(t)
        if m and m.group(1).strip():
            r = self.sb.new_project(m.group(1).strip(), "", [])
            return r["message"]
        name = _STRIP.sub("", t) or t
        r = self.sb.switch_to_project(name, confirm=True)
        if r["ok"]:
            return ""  # the switch itself announces "接到 X 项目"
        return r["message"]


class RulesSession:
    """A stand-in for ClaudeSession on the manager line: no process, answers through RuleManager on its own
    thread (actions may ask the user out loud, which blocks until they answer)."""

    model = "rules"

    def __init__(self, line: "Line", sb: "Switchboard"):
        self.line, self.sb = line, sb
        self.brain = RuleManager(sb)
        self.session_id = "rules"
        self.turn_active = False

    def start(self) -> None:
        self.line.on_session("ready", {"session_id": self.session_id})

    def send(self, text: str) -> None:
        self.turn_active = True
        threading.Thread(target=self._answer, args=(text,), daemon=True, name="rules-manager").start()

    def _answer(self, text: str) -> None:
        try:
            reply = self.brain.handle(text)
        except Exception as e:
            log.exception("rules manager failed")
            reply = f"出错了：{e}"
        if reply:
            self.line.on_session("text", reply)
        self.turn_active = False
        self.line.on_session("done", {"status": "ok", "message": reply})
        for action in self.brain.after_turn[:]:
            self.brain.after_turn.remove(action)
            try:
                action()
            except Exception:
                log.exception("rules manager follow-up failed")

    def interrupt(self) -> None:
        pass

    def close(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def restart(self, model: Any = None, provider: Any = None, fresh: bool = False) -> None:
        pass

    def rewind_to(self, keep: dict, drop: dict, single: bool = True) -> None:
        pass  # the rules keep no conversation
