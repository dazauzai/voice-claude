"""Pure text helpers: streaming sentence splitting, markdown stripping, exit-phrase detection."""
from __future__ import annotations

import re

CODE_NOTE = "代码在屏幕上。"
TERMINALS = "。！？!?；;\n"
SOFT = "，,、："
SOFT_MIN = 24  # split at a comma only once the sentence is this long (chars)
FIRST_SOFT_MIN = 12  # the first sentence of a reply may be shorter, so speech starts sooner

_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_MARKER = re.compile(r"^\s{0,3}(#{1,6}\s*|[-*+]\s+|\d+[.)]\s+|>\s?)", re.M)
_EMPH = re.compile(r"(?<![\w*_])[*_]{1,2}(\S(?:[^*_\n]*?\S)?)[*_]{1,2}(?![\w*_])")
_RULE = re.compile(r"[-=*_]{3,}")
_WS = re.compile(r"\s+")


def clean_for_speech(s: str) -> str:
    s = _LINK.sub(r"\1", s)
    s = _URL.sub("链接", s)
    s = _MARKER.sub("", s)
    s = _EMPH.sub(r"\1", s)
    s = s.replace("**", "").replace("`", "").replace("|", " ")
    s = _RULE.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    # Drop fragments that are only punctuation.
    return s if re.search(r"[\w一-鿿]", s) else ""


def split_complete(text: str, soft_min: int = SOFT_MIN) -> tuple[list[str], str]:
    """Split off complete sentences; return (sentences, remainder)."""
    out, start = [], 0
    for i, ch in enumerate(text):
        if ch in TERMINALS or (ch in SOFT and i - start >= soft_min):
            out.append(text[start:i + 1])
            start = i + 1
            soft_min = SOFT_MIN
    return [s for s in out if s.strip()], text[start:]


class SentenceSplitter:
    """Feed streamed text deltas, get back speakable sentences. Code blocks are replaced by CODE_NOTE."""

    def __init__(self):
        self.buf = ""
        self.in_code = False
        self.started = False  # has anything been emitted yet?

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out: list[str] = []
        while True:
            fence = self.buf.find("```")
            if self.in_code:
                if fence < 0:
                    # keep a possible partial fence ("`" or "``") at the end, drop the rest
                    tail = len(self.buf) - len(self.buf.rstrip("`"))
                    self.buf = self.buf[-tail:] if tail else ""
                    break
                self.buf = self.buf[fence + 3:]
                self.in_code = False
                out.append(CODE_NOTE)
                continue
            text = self.buf if fence < 0 else self.buf[:fence]
            sentences, rest = split_complete(text, SOFT_MIN if self.started or out else FIRST_SOFT_MIN)
            out.extend(sentences)
            if fence < 0:
                self.buf = rest
                break
            if rest.strip():
                out.append(rest)
            self.buf = self.buf[fence + 3:]
            self.in_code = True
        cleaned = [c for c in (clean_for_speech(s) for s in out) if c]
        self.started = self.started or bool(cleaned)
        return cleaned

    def flush(self) -> list[str]:
        rest, self.buf = self.buf, ""
        if self.in_code:
            self.in_code = False
            return [CODE_NOTE]
        c = clean_for_speech(rest)
        return [c] if c else []


_PUNCT = re.compile(r"[\s。！？!?，,、.　]+")
_FILLER_CHARS = set("呃嗯啊哦额噢唔呀嘛哎诶欸")


def is_exit_phrase(text: str) -> bool:
    t = _PUNCT.sub("", text)
    return t in {"结束对话", "结束对话吧", "退出对话", "退出对话吧", "结束", "再见"} or t.endswith("结束对话")


# Answers to an approval question. A negation anywhere is a no; a yes needs every word of the answer to be a yes
# word (fillers and particles aside), so "好，跳过" or "OK, forget it" are asked again instead of approved.
_ZH_NO = ("不", "别", "停", "没", "算了", "取消", "等", "先不", "暂停", "慢着", "回头", "再说", "免了", "放弃", "拒绝", "否")
_EN_NO = {"no", "nope", "nah", "not", "never", "dont", "don't", "wait", "stop", "cancel", "later", "hold"}
_ZH_YES_WITH_NO = ("没问题", "没有问题", "没错", "没毛病", "不错")  # yes phrases that contain a negation character
_ZH_YES = ("可以", "好的", "好", "行", "确认", "确定", "同意", "允许", "继续", "当然", "对的", "对", "是的", "是",
           "没问题", "没有问题", "没错", "没毛病", "不错",
           "好嘞", "开始", "就这样", "就这个", "照做", "按你说的",
           "做吧", "执行吧", "切吧", "换吧", "删吧", "开吧", "去吧", "来吧", "装吧", "发吧", "输入吧", "打开吧", "点吧")
_ZH_FILLER = "嗯呃啊哦额吧的了呀呢啦嘛请那"
_ZH_YES_LONGEST_FIRST = sorted(set(_ZH_YES), key=len, reverse=True)  # 好嘞 before 好
_EN_YES = {"ok", "okay", "yes", "yeah", "yep", "yup", "sure", "alright", "fine", "please", "proceed", "go", "ahead",
           "do", "it", "noproblem", "absolutely", "of", "course", "thing", "right", "correct"}
_CLAUSE = re.compile(r"[\s。！？!?，,、.　;；:：…~～]+")


def _yes_words(clause: str) -> int | None:
    """How many yes words the clause is made of (0 = only fillers); None when anything else is in it."""
    if clause.isascii():
        return 1 if clause in _EN_YES else None
    i, n = 0, 0
    while i < len(clause):
        if clause[i] in _ZH_FILLER:
            i += 1
            continue
        word = next((w for w in _ZH_YES_LONGEST_FIRST if clause.startswith(w, i)), None)
        if word is None:
            return None
        i, n = i + len(word), n + 1
    return n


def yes_or_no(text: str) -> bool | None:
    """Classify a spoken answer to an approval question. None = could not tell (it asks again).
    False when any clause negates ("嗯，不行", "不，错了", "Yeah, no", "好，等下"): wrongly refusing costs one more
    question, wrongly approving runs the action. True only when every word is a yes word ("好的", "对，切吧",
    "OK, go ahead"). A question back ("可以吗", "可以？") and anything else ("好，跳过") ask again."""
    raw = text.lower().replace("no problem", "noproblem").replace("not a problem", "noproblem")
    clauses = [c for c in _CLAUSE.split(raw) if c]
    if not clauses:
        return None
    for c in clauses:
        if c.strip(_ZH_FILLER) in _ZH_YES_WITH_NO:
            continue
        if any(w in c for w in _ZH_NO) or c in _EN_NO:
            return False
    if "?" in text or "？" in text or any("吗" in c or c.endswith(("么", "呢")) for c in clauses):
        return None
    counts = [_yes_words(c) for c in clauses]
    if None not in counts and sum(counts) > 0:
        return True
    return None
    zh = t
    for phrase in _ZH_ALSO_YES:
        zh = zh.replace(phrase, "")
    en = raw
    for phrase in _EN_ALSO_YES:
        en = en.replace(phrase, " ")
    if any(w in zh for w in _ZH_NO) or any(w in _EN_NO for w in _EN_WORD.findall(en)):
        return False
    if "?" in text or "？" in text or "吗" in t or t.endswith(("么", "呢")):
        return None
    t = t.lstrip("嗯呃啊哦额")
    if t.startswith(_YES) or t.startswith(_ZH_ALSO_YES):
        return True
    return None
    core = t
    for phrase in _ALSO_YES:
        core = core.replace(phrase, "")
    if any(w in core for w in _NO_ANYWHERE) or core.startswith(_NO_START):
        return False
    if "吗" in t or t.endswith(("么", "呢")):
        return None
    t = t.lstrip("嗯呃啊哦额")
    if t.startswith(_YES):
        return True
    return None


# Fixed phrases the front end acts on itself (no Claude call): spoken text -> key.
_FIXED = [
    ("back_to_manager", ("回到总机", "返回总机", "切回总机", "回到manager", "切回manager", "回总机")),
    ("scratch", ("接杂项", "接到杂项", "去杂项", "杂项线")),
    ("background_status", ("后台怎么样了", "后台怎么样", "后台进度", "后台情况", "后台做完了吗")),
    ("background_mode", ("进入后台模式", "后台模式", "切到后台", "放到后台")),
    ("shutdown_daemon", ("关掉后台服务", "关闭后台服务", "停掉后台服务")),
    ("cancel_queue", ("撤销排队", "取消排队", "排队的别发", "排队的不要了", "刚才的别发了", "刚说的别发")),
    ("transparent_on", ("打开通透模式", "开启通透模式", "进入通透模式")),
    ("transparent_off", ("关闭通透模式", "关掉通透模式", "退出通透模式")),
    ("open_access", ("打开接入页", "打开接入", "接入设置", "打开登录页", "打开ai服务商", "打开服务商", "ai服务商")),  # handled by the front end itself
]


def fixed_phrase(text: str) -> str | None:
    """Key of the fixed phrase the text is (or ends with), else None."""
    t = _PUNCT.sub("", text).lower().replace(" ", "")
    if not t:
        return None
    for key, phrases in _FIXED:
        if any(t == p or t.endswith(p) for p in phrases):
            return key
    return None


def is_filler(text: str) -> bool:
    """True for noise or thinking sounds ('呃', '嗯嗯', '.') that should not be sent to Claude."""
    t = _PUNCT.sub("", text)
    return not re.search(r"[\w一-鿿]", t) or (len(t) <= 3 and set(t) <= _FILLER_CHARS)
