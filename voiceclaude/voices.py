"""Named voices and a try-out: python -m voiceclaude.voices 3 18 30 50 — each one introduces itself."""
import sys
import time

# Spoken names for the voices the user can switch to by voice ("把声音换成小山"). Kokoro v1.1-zh: ids 3–102 are
# Chinese voices (3 = the default).
NAMED = {"小雨": 3, "小晨": 10, "小风": 25, "小云": 45, "小山": 60, "小海": 80, "小木": 100}


def name_of(sid: int) -> str:
    return next((n for n, s in NAMED.items() if s == sid), f"{sid} 号")


def resolve(name: str) -> int | None:
    """'小山' / '小山。' / '60' / '60 号' -> speaker id; None if unknown."""
    t = name.strip().replace("号", "").replace("。", "").replace(" ", "")
    if t in NAMED:
        return NAMED[t]
    for n, sid in NAMED.items():
        if n in t:
            return sid
    if t.isdigit() and 0 <= int(t) <= 102:
        return int(t)
    return None


def menu() -> str:
    return "、".join(f"{n}（{s} 号）" for n, s in NAMED.items())

from . import config
from .audio import Player
from .tts import Speaker


def main():
    sids = [int(a) for a in sys.argv[1:]] or [3, 18, 30, 50, 80, 100]
    cfg = config.load()
    done = []
    player = Player()
    speaker = Speaker(cfg.voice, cfg.speed, player, lambda: done.append(1))
    print(f"一共 {speaker.tts.num_speakers} 个声音（0–{speaker.tts.num_speakers - 1}）")
    for sid in sids:
        speaker.voice = sid
        done.clear()
        print(f"声音 {sid} …")
        speaker.say(f"我是 {sid} 号声音。帮我看一下这个 function 的 output，test 已经跑完了。")
        while not done:
            time.sleep(0.05)
        time.sleep(0.4)
    print("喜欢哪个，把 config.toml 里的 voice 改成那个号码。")


if __name__ == "__main__":
    main()
