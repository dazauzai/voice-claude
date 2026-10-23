"""The front end: hotkey, microphone, recogniser, speaker and the window, talking to the background service
through DaemonClient (see docs/protocol.md). Lines, Claude processes and tools live in the daemon."""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import ctypes
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np

from . import audio, config, testing, voices, text as textmod
from .asr import Recognizer
from .client import DaemonClient
from .config import Config, missing_models
from .hotkey import HotkeyListener
from .tts import Speaker
from .ui_access import AccessPage, describe as describe_access, describe_is_error
from .ui_theme import (BAND_COLORS, COLORS, JOB_WORDS, PERMISSION_NAMES, PERMISSION_WORDS, STATES, Theme, ask_title, band_for,
                       classify_status, fit_hint, heard_suffix, hint_for, is_ascii, parse_job_note, permissions_summary,
                       status_display, tool_word, truncate_to_width, wrap_mode)
from .ui_widgets import (AnsweredCard, BackToLatest, CheckRow, DockedCard, LineChip, LinePanel, NavGroup, Segmented, StateOrb,
                         flat_button, header_padx, page_header, place_button, style_button)

log = logging.getLogger(__name__)


def foreground_outside(root) -> int:
    """The window in front, unless it is this one (then 0). Windows only; never under the test runner."""
    if os.name != "nt" or testing.under_test_runner():
        return 0
    try:
        u = ctypes.windll.user32
        fg = u.GetForegroundWindow()
        own = int(root.wm_frame(), 16)
        return 0 if not fg or fg == own or u.GetAncestor(fg, 2) == own else int(fg)  # 2 = GA_ROOT
    except Exception:
        return 0


def bring_back(hwnd: int) -> None:
    """Give the focus back to a window this one took it from (allowed: this window is in front right now)."""
    if hwnd and os.name == "nt" and not testing.under_test_runner():
        try:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            log.debug("could not bring the window back", exc_info=True)

IDLE, RECORDING, LISTENING, BUSY, APPROVING = "idle", "recording", "listening", "busy", "approving"
FONT = "Microsoft YaHei UI"


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.state = IDLE
        self.conversation = False
        self.turn_open = False  # we said something to the active line and its `done` has not arrived yet
        self.splitter: textmod.SentenceSplitter | None = None
        self.reply = ""
        self.queued: list[str] = []  # what you said while the line was busy; sent after its `done`
        self.transcribing = 0  # utterances handed to ASR whose text has not been handled yet
        self.muted = False  # microphone chunks are dropped (you went to watch a video)
        self.record_buf: list[np.ndarray] = []
        self.record_started = 0.0
        self.cut = False  # Claude's voice was cut because you kept talking
        self.ready = False
        self.daemon_gone = ""
        self.font_size = cfg.font_size
        self.approval: dict | None = None  # the question being asked right now
        self.lines: dict[str, dict] = {}
        self.audio_q: queue.Queue = queue.Queue()
        self.ui_q: queue.Queue = queue.Queue()
        self.client = DaemonClient(self._on_daemon)
        self.recognizer: Recognizer | None = None
        self.speaker: Speaker | None = None
        self.player: audio.Player | None = None
        self.endpointer: audio.Endpointer | None = None
        self.answer_endpointer: audio.Endpointer | None = None
        self.mic: audio.Microphone | None = None
        self.hotkey: HotkeyListener | None = None
        self.send_hotkey: HotkeyListener | None = None
        self.done_event = threading.Event()  # set whenever a `done` for the active line arrives
        self._quit_requested = False  # 退出 voice-claude was chosen: the service's bye closes the window
        self.transparent = False  # 通透模式, as the service reports it (hello reply, 'transparent' events)
        self._fg_before_card = 0  # the window in front when a request card opened (a click on 可以 gives it back)
        self._closing = False  # quit() ran: the UI loop must not reschedule itself

    # ---------- startup / shutdown ----------

    def run(self):
        self._build_window()
        threading.Thread(target=self._load, daemon=True, name="load").start()
        self.root.after(50, self._drain_ui)
        self.root.mainloop()

    def _load(self):
        try:
            missing = missing_models()
            if missing:
                raise FileNotFoundError("缺少语音模型：" + "、".join(missing) + "。再双击 install.bat 下载。")
            self._status("正在加载语音模型…")
            self.player = audio.Player(self.cfg.speaker)
            audio.OUTPUT_DEVICE = self.player.device
            self.speaker = Speaker(self.cfg.voice, self.cfg.speed, self.player, self._on_speech_idle)
            self.recognizer = Recognizer(self.cfg.replacements)
            self.endpointer = audio.Endpointer(self.cfg.silence_seconds)
            self.answer_endpointer = audio.Endpointer(0.8)  # 可以 / 不行 are one or two words
            self.mic = audio.Microphone(self.cfg.microphone, self.audio_q.put)
            self.mic.start()
            threading.Thread(target=self._audio_worker, daemon=True, name="audio").start()
            self._status("正在连接后台服务…")
            info = self._connect()
            self.ui_q.put(("call", lambda: self._show_transparent(bool(info.get("transparent")))))
            for line in info.get("lines", []):
                self.lines[line["line"]] = line
            self._refresh_lines()
            if not os.environ.get("VOICECLAUDE_NO_HOTKEY"):
                self.hotkey = HotkeyListener(self.cfg.hotkey, self.cfg.double_tap_seconds, self._on_tap, self._on_double,
                                             self._on_hold, self.cfg.hold_seconds)
                self.hotkey.start()
                if self.cfg.send_key:
                    # single tap (reported after the double-tap window) = mute / unmute; double tap = send the queue now
                    self.send_hotkey = HotkeyListener(self.cfg.send_key, self.cfg.double_tap_seconds, self._toggle_mute,
                                                      self._on_send_now, delay_tap=True)
                    self.send_hotkey.start()
            self.ready = True
            self._status("空闲")
            self.ui_q.put(("devices", self.mic.name, self.player.name))
            self._note(f"· 麦克风：{self.mic.name}")
            self._note(f"· 扬声器：{self.player.name}")  # the port is in the log only
            self.speaker.say("准备好了。")  # if you can't hear this, check the speaker line above / config.toml
        except Exception as e:
            log.exception("startup failed")
            self._status("启动失败")
            self._note(f"启动失败：{e}")
            audio.tone("error")

    def _connect(self) -> dict:
        """Separate so a test harness can point the app at its own daemon."""
        return self.client.connect()

    def _close_window(self):
        """The window's × quits like 退出 voice-claude: the service and its lines stop too (asking first when a line
        is answering or a background job runs)."""
        log.info("window closed with ×")
        self._request_quit()

    def quit(self):
        self._closing = True
        try:
            if self.hotkey:
                self.hotkey.stop()
            if self.send_hotkey:
                self.send_hotkey.stop()
            if self.mic:
                self.mic.stop()
            if self.speaker:
                self.speaker.stop()
            self.client.close()  # the daemon and its lines keep running
        finally:
            self.root.destroy()

    # ---------- hotkey / buttons ----------

    def _on_tap(self):
        with self.lock:
            if not self.ready or self._gone():
                return
            if self.state == IDLE:
                self._start_recording()
            elif self.state == RECORDING:
                samples = np.concatenate(self.record_buf) if self.record_buf else np.zeros(0, np.float32)
                self.record_buf = []
                self.state = BUSY
                self._status("识别中…")
                audio.tone("think")
                self._start_utterance(samples)
            elif self.state == LISTENING:
                utt = self.endpointer.finish()
                if utt is not None:
                    self.state = BUSY
                    audio.tone("think")
                    self._start_utterance(utt)
            elif self.state == APPROVING:
                self._resolve_approval(False)
            elif self.state == BUSY:
                if self.queued:
                    self._cancel_queue()  # first tap: only drop what is waiting; the line keeps working
                    return
                self._stop_turn("打断")
                threading.Thread(target=self._safe, args=(self.client.interrupt, self.client.active), daemon=True).start()

    def _cancel_queue(self):
        """Lock held: forget what you said while the line was busy, without touching the line."""
        n = len(self.queued)
        self.queued.clear()
        self._note(f"（撤销了排队的 {n} 句）" if n else "（没有排队的话）")
        audio.tone("stop")

    def _on_double(self):
        with self.lock:
            if not self.ready or self._gone():
                return
            if self.conversation:
                self._exit_conversation()
                return
            if self.state == RECORDING and time.monotonic() - self.record_started < 0.8:
                self.record_buf = []  # the first tap of this double-tap started a recording: undo it
            elif self.state in (BUSY, APPROVING):
                self._stop_turn("打断")
                threading.Thread(target=self._safe, args=(self.client.interrupt, self.client.active), daemon=True).start()
            self.conversation = True
            self._note("—— 连续对话开始 ——")
            self._listen()

    def _on_send_now(self):
        """Send key double-tapped: stop this turn and send what is queued right away."""
        with self.lock:
            if not self.ready or self._gone():
                return
            if not self.queued:
                self._note("（没有排队的话）")
                return
            text = "\n".join(self.queued)
            self.queued.clear()
            had_turn = self.turn_open
            self.done_event.clear()
            self._stop_turn("马上发排队的话")
        threading.Thread(target=self._send_now, args=(text, had_turn), daemon=True).start()

    def _send_now(self, text: str, had_turn: bool):
        if had_turn:
            self._safe(self.client.interrupt, self.client.active)
            self.done_event.wait(3)  # let the interrupted turn's `done` land before opening the next one
        with self.lock:
            if self._gone():
                return
            self._line("你", text)
            self._begin_turn()
        self._say(text)

    def _toggle_mute(self, via: str = "key"):
        """The send key's single tap and the 静音 button. During a yes/no request both mean 可以 (the talk key and
        the left button mean 不行)."""
        with self.lock:
            if not self.ready:
                return
            if self.state == APPROVING and self.approval is not None and self.approval.get("kind") == "approve":
                if via == "button":  # the click moved the focus here; typing must go back into the window you heard
                    bring_back(self._fg_before_card)
                self._resolve_approval(True, via=via)
                return
            self.muted = not self.muted
            if self.muted:
                self._note(f"—— 麦克风已静音（再按一下{key_name(self.cfg.send_key) if self.cfg.send_key else '静音按钮'}或点按钮恢复）——")
                audio.tone("stop")
            else:
                self.endpointer.reset()
                self.answer_endpointer.reset()
                self._note("—— 麦克风恢复 ——")
                audio.tone("listen")

    def _on_hold(self):
        """Emergency stop: speech, the current turn and running tools on every line. Detached jobs survive."""
        with self.lock:
            if not self.ready:
                return
            self._stop_turn("紧急停止")
            audio.tone("error")
        threading.Thread(target=self._safe, args=(self.client.stop_all,), daemon=True).start()

    def _safe(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:
            log.warning("%s failed: %s", getattr(fn, "__name__", fn), e)
            self._note(f"（{e}）")

    # ---------- state changes (call with lock held) ----------

    def _gone(self) -> bool:
        if self.daemon_gone:
            self._note(f"后台服务断开了：{self.daemon_gone}。请重启程序。")
            audio.tone("error")
        return bool(self.daemon_gone)

    def _start_recording(self):
        self.record_buf = []
        self.record_started = time.monotonic()
        self.conversation = False
        self.state = RECORDING
        self._status("录音中…")
        audio.tone("listen")

    def _listen(self, keep_audio: bool = False):
        """Start listening. keep_audio=True keeps what the mic already caught (you started talking while
        Claude was finishing its last sentence) instead of throwing it away."""
        if not keep_audio:
            self.endpointer.reset()
        self.player.duck(False)
        self.cut = False
        self.state = LISTENING
        self._status("在听…")
        audio.tone("listen")

    def _exit_conversation(self):
        self._stop_turn("")
        self.conversation = False
        self.state = IDLE
        self._status("空闲")
        self._note("—— 连续对话结束 ——")
        audio.tone("stop")
        threading.Thread(target=self._safe, args=(self.client.interrupt, self.client.active), daemon=True).start()

    def _stop_turn(self, why: str):
        """Drop speech, the queue and the open turn locally; the caller tells the daemon."""
        self.splitter = None
        self.turn_open = False
        self.queued.clear()
        self._resolve_approval(False)
        self.speaker.stop()
        self.player.duck(False)
        self.cut = False
        if why:
            self._note(f"（{why}）")
        if self.conversation:
            self._listen()
        else:
            self.state = IDLE
            self._status("空闲")

    def _check_turn_finished(self):
        with self.lock:
            if self.state != BUSY or self.turn_open or not self.speaker.idle():
                return
            if self.conversation:
                # With barge-in on (headphones) the mic was watched the whole time Claude spoke, so anything
                # you already started saying is kept. On speakers it would be Claude's own voice.
                self._listen(keep_audio=self.cfg.barge_in)
            else:
                self.state = IDLE
                self._status("空闲")

    # ---------- audio ----------

    def _audio_worker(self):
        while True:
            chunk = self.audio_q.get()
            self.ui_q.put(("level", 0.0 if self.muted else float(np.sqrt(np.mean(chunk * chunk))) if len(chunk) else 0.0))
            if self.muted:
                continue
            with self.lock:
                state, conv = self.state, self.conversation
                if state == RECORDING:
                    self.record_buf.append(chunk)
                    continue
                if state == LISTENING:
                    utt = self.endpointer.feed(chunk)
                    if utt is not None:
                        self.state = BUSY
                        audio.tone("think")
                        self._start_utterance(utt)
                    continue
                if state == APPROVING:
                    if not self.cfg.barge_in and not self.speaker.idle():
                        self.answer_endpointer.reset()  # on speakers: don't take our own question for the answer
                        continue
                    utt = self.answer_endpointer.feed(chunk)
                    if utt is not None:
                        threading.Thread(target=self._answer, args=(utt,), daemon=True).start()
                    continue
                if state == BUSY and conv and self.cfg.barge_in:
                    # Level ①: your voice only lowers, then stops, Claude's voice. The line keeps working and
                    # what you say is queued for after its `done`.
                    utt = self.endpointer.feed(chunk)
                    spoken = self.endpointer.speech_seconds
                    if spoken >= self.cfg.duck_seconds and not self.player.ducked:
                        self.player.duck(True)
                    if spoken >= self.cfg.cut_seconds and not self.cut:
                        self.cut = True
                        log.info("voice cut (you kept talking)")
                        self.speaker.stop()
                    if utt is not None:
                        self.player.duck(False)
                        self.cut = False
                        self._start_utterance(utt)

    def _start_utterance(self, samples: np.ndarray):
        """Called with the lock held."""
        self.transcribing += 1
        threading.Thread(target=self._utterance, args=(samples,), daemon=True).start()

    def _utterance(self, samples: np.ndarray):
        try:
            self._handle_utterance(samples)
        finally:
            with self.lock:
                self.transcribing -= 1

    def _handle_utterance(self, samples: np.ndarray):
        try:
            text = self.recognizer.transcribe(samples) if len(samples) else ""
        except Exception:
            log.exception("asr failed")
            text = ""
        log.info("you: %s", text)
        send = None
        with self.lock:
            if self.state != BUSY:
                return
            if self._gone():
                self.conversation = False
                self.state = IDLE
                return
            if not text or textmod.is_filler(text):
                if self.turn_open:
                    return  # a thinking sound while Claude works
                if self.conversation:
                    self._listen(keep_audio=True)
                else:
                    self._note("（没听清）")
                    self.state = IDLE
                    self._status("空闲")
                return
            key = textmod.fixed_phrase(text)
            if key == "cancel_queue":
                self._line("你", text)
                self._cancel_queue()
                if not self.turn_open:
                    self._after_turn()
                return
            if key == "open_access":  # a window action (the 你 line opens the access page); nothing for the daemon
                self._line("你", text)
                if not self.turn_open:
                    self._after_turn()
                return
            if key:
                self._line("你", text)
                threading.Thread(target=self._phrase, args=(key,), daemon=True).start()
                if not self.turn_open:
                    self._after_turn()
                return
            if self.conversation and textmod.is_exit_phrase(text):
                self._line("你", text)
                self._exit_conversation()
                self.speaker.say("好的。")
                return
            if self.turn_open:
                self.queued.append(text)
                self._line("你", f"（排队）{text}")
                return
            self._line("你", text)
            send = self._begin_turn()
        if send:
            self._say(text)

    def _after_turn(self):
        """Lock held: nothing in flight, go back to listening / idle."""
        if self.conversation:
            self._listen(keep_audio=True)
        else:
            self.state = IDLE
            self._status("空闲")

    def _begin_turn(self) -> bool:
        """Lock held: open a turn on the active line."""
        self.splitter = textmod.SentenceSplitter()
        self.reply = ""
        self.turn_open = True
        self.state = BUSY
        self._status("思考中…")
        self._line("Claude", "")
        return True

    def _say(self, text: str):
        try:
            self.client.say(self.client.active, text)
        except Exception as e:
            log.warning("say failed: %s", e)
            with self.lock:
                self.turn_open = False
                self._note(f"（没发出去：{e}）")
                self.speaker.say("后台服务没有回应。")
                self._after_turn()

    def _phrase(self, key: str):
        try:
            r = self.client.phrase(key)
        except Exception as e:
            log.warning("phrase failed: %s", e)
            self._note(f"（{e}）")
            return
        spoken = r.get("spoken") or r.get("message") or ""
        if spoken:
            self._note(spoken)
            self.speaker.say(spoken)

    # ---------- approvals and questions from the daemon ----------

    def _handle_ask(self, data: dict):
        """A line (or the switchboard) wants a yes/no or an answer. Runs on its own thread."""
        for _ in range(200):  # let you finish the sentence you are saying (up to 10 s)
            with self.lock:
                talking = self.state == RECORDING or (self.endpointer and self.endpointer.active)
            if not talking:
                break
            time.sleep(0.05)
        question = data.get("question", "")
        if data.get("background") or (data.get("line") and data.get("line") != self.client.active):
            question = f"后台的{data.get('line_name', '')}申请：{question}"
        yes, text = self._ask(question, data.get("detail", ""), data.get("kind", "approve"))
        try:
            self.client.answer(data["request"], yes, text)
        except Exception as e:
            log.warning("answer failed: %s", e)

    def _ask(self, question: str, detail: str, kind: str) -> tuple[bool, str]:
        """Speak the question, listen for 可以 / 不行 (or a whole sentence when kind == "question"), up to 60 s."""
        self._wait_for_your_sentence()
        with self.lock:
            if self.state in (RECORDING,) or self.approval is not None or not self.ready:
                return False, ""
            self.approval = {"event": threading.Event(), "answer": None, "text": "", "unclear": 0,
                             "kind": kind, "prev": self.state}
            event = self.approval["event"]
            self.state = APPROVING
            self.answer_endpointer.reset()
            self.player.duck(False)
            self._status("等你回答")
            self._note(("（申请）" if kind == "approve" else "（问你）") + question + (f"\n　　{detail}" if detail else ""))
            self.speaker.say(question)
        event.wait(60)
        with self.lock:
            a = self.approval or {}
            answer, text, prev = a.get("answer"), a.get("text", ""), a.get("prev", IDLE)
            self.approval = None
            self.endpointer.reset()  # the answer must not count as the start of a barge-in
            if self.state == APPROVING:
                if prev == BUSY and self.turn_open:
                    self.state = BUSY
                    self._status("思考中…")
                elif self.conversation:
                    self._listen()
                else:
                    self.state = IDLE
                    self._status("空闲")
        log.info("ask %r -> %s %r", question, answer, text)
        self._check_turn_finished()
        return answer is True, text

    def _wait_for_your_sentence(self, limit: float = 30.0):
        """If you are mid-sentence (talking over a busy line, or still speaking in listening mode), let the
        sentence end and get queued before a question takes the microphone — otherwise the half you had
        already said would be thrown away and the rest taken for the answer."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit:
            with self.lock:
                mid_sentence = self.state in (BUSY, LISTENING) and self.endpointer.active
                if not mid_sentence and not self.transcribing:
                    return
            time.sleep(0.05)

    def _resolve_approval(self, answer: bool, text: str = "", via: str = ""):
        """via: "" = by voice (or 不行 by the talk key); "key" / "button" = 可以 by the send key / the button."""
        if self.approval:
            self.approval["answer"] = answer
            self.approval["text"] = text
            self.approval["event"].set()
            if self.approval["kind"] == "approve":
                if answer and via:
                    self._note("（按键：可以）" if via == "key" else "（点了：可以）")
                else:
                    self._note("（你说：可以）" if answer else "（你说：不行）")

    def _answer(self, samples: np.ndarray):
        """Your spoken reply to a question from the daemon."""
        try:
            text = self.recognizer.transcribe(samples)
        except Exception:
            log.exception("asr failed")
            return
        log.info("you (answer): %s", text)
        with self.lock:
            if self.state != APPROVING or not self.approval:
                return
            if textmod.is_filler(text):
                return
            self._line("你", text)
            if self.approval["kind"] == "question":
                self._resolve_approval(True, text)
                return
            answer = textmod.yes_or_no(text)
            if answer is None:
                self.approval["unclear"] += 1
                if self.approval["unclear"] >= 2:
                    self._note("（两次没听清，算不行）")
                    self.speaker.say("没听清，这步先不做。")
                    self._resolve_approval(False)
                    return
                self.speaker.say("可以，还是不行？")
                self.answer_endpointer.reset()
                return
            self._resolve_approval(answer)

    # ---------- events from the daemon (reader thread: keep it quick) ----------

    def _on_daemon(self, kind: str, data: dict):
        if kind == "ask":
            threading.Thread(target=self._handle_ask, args=(data,), daemon=True).start()
            return
        if kind == "request":
            threading.Thread(target=self._handle_request, args=(data,), daemon=True).start()
            return
        if kind == "notice":
            text = data.get("text", "")
            if text:
                self._note(text)
                self.speaker.say(text)
            return
        if kind == "job":
            self._note(f"后台任务 {data.get('job')}：{data.get('state')} {data.get('note', '')}".strip())
            return
        if kind == "line":
            self._on_line(data)
            return
        if kind == "transparent":
            self.ui_q.put(("call", lambda: self._show_transparent(bool(data.get("on")))))
            return
        if kind == "bye":
            with self.lock:
                if self.daemon_gone:  # the client adds its own bye after the service's: one is enough
                    log.info("second bye ignored: %s", data.get("reason", ""))
                    return
                self.daemon_gone = data.get("reason", "") or "后台服务的连接断了"
                self.turn_open = False
                self.splitter = None
                self.queued.clear()
                self._resolve_approval(False)
                self.conversation = False
                self.state = IDLE
            if self._quit_requested:  # 退出 voice-claude: the service is gone, so is the window
                self.ui_q.put(("call", self.quit))
                return
            self._status("后台服务已关掉" if "已关闭" in self.daemon_gone else "后台服务断开")
            self._note(f"后台服务断开了：{self.daemon_gone}。请重启程序。")
            audio.tone("error")
            return
        if data.get("line") and data.get("line") != self.client.active:
            return  # another line's chatter goes to its mailbox in the daemon
        send_queued = None
        with self.lock:
            if not self.turn_open:
                return  # stale: a turn we stopped or one we did not start
            if kind == "text":
                delta = data.get("text", "")
                self._append(delta)
                self.reply += delta
                for s in self.splitter.feed(delta):
                    self._status("在说…")
                    self.speaker.say(s)
            elif kind == "tool":
                log.info("tool: %s %s", data.get("name"), str(data.get("detail", ""))[:200])
                self._status(tool_word(str(data.get("name", ""))))
                self._note(f"· {data.get('name')} {str(data.get('detail', ''))[:80]}")
            elif kind == "done":
                self.turn_open = False
                self.done_event.set()
                status = data.get("status", "ok")
                log.info("claude (%s): %s", status, self.reply.replace("\n", " ")[:2000])
                if status == "ok" and self.splitter:
                    for s in self.splitter.flush():
                        self.speaker.say(s)
                elif status == "error":
                    self._note(f"出错：{data.get('last', '')}")
                    audio.tone("error")
                    self.speaker.say("这一轮出错了，看一下屏幕。")
                self.splitter = None
                if self.queued:
                    send_queued = "\n".join(self.queued)
                    self.queued.clear()
                    self._begin_turn()
        if send_queued:
            threading.Thread(target=self._say, args=(send_queued,), daemon=True).start()
        else:
            self._check_turn_finished()

    def _on_line(self, data: dict):
        self.lines[data["line"]] = data
        if data.get("active"):
            with self.lock:
                if self.turn_open and getattr(self, "turn_line", None) not in (None, data["line"]):
                    self.turn_open = False  # the switchboard moved us while the old line was still talking
                    self.splitter = None
                self.turn_line = data["line"]
            self._check_turn_finished()
        self._refresh_lines()

    def _refresh_lines(self):
        active = self.client.active
        name = self.lines.get(active, {}).get("name", active or "")
        self.ui_q.put(("lines", dict(self.lines), active, name))

    def _handle_request(self, data: dict):
        """Settings the daemon's tools can only change here (font, devices)."""
        path, args = data.get("path", ""), data.get("data", {}) or {}
        try:
            if path == "get_settings":
                result = {"ok": True, "message": f"字号 {self.font_size}；麦克风 {self.mic.name}；扬声器 {self.player.name}；"
                                                 f"声音 {voices.name_of(self.speaker.voice)}；语速 {self.speaker.speed}；"
                                                 f"停顿阈值 {self.cfg.silence_seconds} 秒"}
            elif path == "list_voices":
                result = {"ok": True, "message": "可选的声音：" + voices.menu() + f"。现在是{voices.name_of(self.speaker.voice)}。"}
            elif path == "set_voice":
                sid = voices.resolve(str(args.get("name", "")))
                if sid is None:
                    result = {"ok": False, "message": "没有这个声音。可选：" + voices.menu()}
                else:
                    self.speaker.voice = sid
                    config.save("tts", "voice", sid)
                    result = {"ok": True, "message": f"声音已换成{voices.name_of(sid)}"}
            elif path == "set_speed":
                speed = max(0.6, min(1.6, float(args.get("speed", 1.0))))
                self.speaker.speed = speed
                config.save("tts", "speed", speed)
                result = {"ok": True, "message": f"语速已改成 {speed:g}"}
            elif path == "list_devices":
                result = {"ok": True, "message": "麦克风：" + "；".join(audio.list_devices(False)) +
                                                 "\n扬声器：" + "；".join(audio.list_devices(True))}
            elif path == "set_font_size":
                self._set_font(int(args["size"]))
                result = {"ok": True, "message": f"字号已改成 {self.font_size}"}
            elif path == "set_microphone":
                self._set_mic(str(args["name"]))
                result = {"ok": True, "message": f"麦克风已切换到 {self.mic.name}"}
            elif path == "set_speaker":
                self._set_speaker(str(args["name"]))
                result = {"ok": True, "message": f"扬声器已切换到 {self.player.name}"}
            else:
                result = {"ok": False, "message": f"语音端不认识的操作 {path}"}
        except Exception as e:
            log.exception("request %s failed", path)
            result = {"ok": False, "message": f"出错了：{e}"}
        try:
            self.client.reply(data["request"], result)
        except Exception as e:
            log.warning("reply failed: %s", e)

    def _on_speech_idle(self):
        self._check_turn_finished()

    # ---------- settings ----------

    def _set_font(self, size: int):
        self.font_size = max(FONT_MIN, min(FONT_MAX, size))
        config.save("ui", "font_size", self.font_size)
        self.ui_q.put(("font", self.font_size))

    def _set_mic(self, name: str):
        with self.lock:
            new = audio.Microphone(name, self.audio_q.put)
            new.start()
            old, self.mic = self.mic, new
            if old:
                old.stop()
            self.endpointer.reset()
        config.save("audio", "microphone", new.name.strip())
        self._note(f"麦克风已切换到 {new.name}")
        self.ui_q.put(("devices", self.mic.name, self.player.name))

    def _set_speaker(self, name: str):
        self.player.set_device(name)
        audio.OUTPUT_DEVICE = self.player.device
        config.save("audio", "speaker", self.player.name.strip())
        self._note(f"扬声器已切换到 {self.player.name}")
        self.ui_q.put(("devices", self.mic.name, self.player.name))

    # ---------- window ----------
    # Everything below is the window side (docs/architecture.md, "The window"). It only reads the voice state machine's
    # attributes (state, conversation, queued, muted, player.ducked, cut, approval) and parses the text of
    # _status / _line / _note items: request cards, footnotes, queued re-tagging and the job list are all derived
    # from those prefixes, so the voice flow above does not depend on how the window looks.

    ASK_SECONDS = 60  # _ask waits this long; the card counts it down

    def _build_window(self):
        try:  # crisp text on high-DPI screens (150 % scaling is common)
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.title("voice-claude")
        self.theme = Theme(self.root, self.font_size)
        self.theme.apply_ttk_style()
        px = self.theme.px
        self.root.geometry(f"{px(420)}x{px(560)}")
        self.root.minsize(self.theme.min_w, self.theme.min_h)
        self.root.configure(bg=COLORS["bg_base"])
        # window-side state
        self._status_raw = "启动中…"
        self._estop_until = 0.0
        self._level = 0.0
        self._band_shown: tuple = ()
        self._hint_shown: tuple = ()
        self.show_hints = bool(getattr(self.cfg, "show_hints", True))
        self.jobs: dict[str, dict] = {}  # background jobs seen in this session (id -> state/note/started)
        self.card: DockedCard | None = None
        self.card_info: dict = {}
        self._card_gone_ticks = 0
        self.answered: list[AnsweredCard] = []
        self.page = "transcript"
        self.panel: LinePanel | None = None
        self.large = False
        self._lines_snapshot: tuple = ({}, "", "")
        self._access_checked = False
        self._access_info: dict | None = None
        self.access_return = "transcript"  # where the AI 服务商 page's back arrow goes: "settings" or "transcript"
        self._provider_sig: tuple = ()  # (line, provider) pairs: a change refreshes the AI 服务商 state
        self._devices = ("", "")  # the microphone and speaker names the window shows
        self._area_ready = False  # the settings area is rebuilt on font changes after the first layout
        self._last_note = ("", 0.0)
        self._last_entry = "other"  # what the transcript ends with: "claude" or "other"
        self._cont = False  # the current Claude reply continues on an indented line after a tool row
        self._bar_mode = 0  # 0 = all buttons, 1 = settings in the chip menu, 2 = mute as an icon
        self._col_extra = 0  # padding either side of the capped text column (large window)
        self._drain_after = None  # the pending _drain_ui tick; cancelled when the window goes away
        self.status_var = tk.StringVar(value="启动中…")
        self.hint_var = tk.StringVar(value="")  # shared by the hint row and the docked card's footnote
        self.topmost_var = tk.BooleanVar(value=True)
        self.hints_var = tk.BooleanVar(value=self.show_hints)
        self.root.attributes("-topmost", True)
        self._build_band()
        self._build_footer()
        self._build_main()
        self._build_settings()
        self._build_permissions()
        self._build_access()
        self._bind_keys()
        self._apply_font()
        self._area_ready = True
        self.root.bind("<Configure>", self._on_configure)
        self.root.bind_all("<MouseWheel>", self._wheel)
        self.root.bind("<Button-1>", self._on_root_click, add="+")
        self.root.bind("<Destroy>", self._on_destroy, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self._close_window)

    def _on_destroy(self, event):
        """root.destroy() (quit, or the preview script) must not leave the 50 ms tick pending: Tcl would report
        'invalid command name ..._drain_ui' the next time any event loop runs in this process."""
        if event.widget is self.root and self._drain_after is not None:
            try:
                self.root.after_cancel(self._drain_after)
            except tk.TclError:
                pass
            self._drain_after = None

    # ----- the status bar -----
    def _build_band(self):
        t, px = self.theme, self.theme.px
        bg = COLORS["bg_base"]
        self.band = tk.Frame(self.root, bg=bg, height=t.bar_h)
        self.band.pack(fill="x", side="top")
        self.band.pack_propagate(False)
        # The chip is measured against the band's width. The root's <Configure> (and an after_idle from it) still
        # see the old band width after a font change or a resize, so the chip must be refitted when the band itself
        # has been re-laid out.
        self.band.bind("<Configure>", lambda e: self._fit_chip(), add="+")
        self.edge = tk.Frame(self.band, bg=bg, width=px(6))
        self.edge.pack(side="left", fill="y")
        self.orb = StateOrb(self.band, t, bg)
        self.orb.pack(side="left", padx=(px(6), px(12)))
        self.chip = LineChip(self.band, t, bg, self._toggle_panel)
        self.chip.pack(side="right", padx=(px(8), px(12)))
        self.transparent_label = tk.Label(self.band, text="通透", font=t.font("cap_bold"), bg=bg, fg=COLORS["text_secondary"])
        self.status_label = tk.Label(self.band, textvariable=self.status_var, font=t.font("status"), bg=bg,
                                     fg=COLORS["text_secondary"], anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)

    # ----- action bar + hint row (packed at the bottom first: a small window squeezes the transcript) -----
    def _build_footer(self):
        t, px = self.theme, self.theme.px
        bg = COLORS["bg_base"]
        self.hint_row = tk.Frame(self.root, bg=bg, height=t.hint_h)
        self.hint_hide = tk.Label(self.hint_row, text="隐藏", font=t.font("cap"), fg=COLORS["text_secondary"], bg=bg, cursor="hand2")
        self.hint_hide.pack(side="right")
        self.hint_hide.bind("<Button-1>", lambda e: self._set_hints(False))
        self.hint_label = tk.Label(self.hint_row, textvariable=self.hint_var, font=t.font("cap"), fg=COLORS["text_secondary"],
                                   bg=bg, anchor="w")
        self.hint_label.pack(side="left", fill="x", expand=True)
        if self.show_hints:
            self.hint_row.pack(side="bottom", fill="x", padx=px(12), pady=(0, px(8)))
        self.bar = tk.Frame(self.root, bg=bg)
        self.bar.pack(side="bottom", fill="x", padx=px(12), pady=(px(8), px(8) if self.show_hints else px(12)))
        self.btn_tap = flat_button(self.bar, t, "说一段", self._on_tap, kind="disabled")
        place_button(self.btn_tap, t, side="left")
        self.btn_conv = flat_button(self.bar, t, "● 开始对话", self._on_double, kind="disabled")
        place_button(self.btn_conv, t, side="left", padx=(px(8), 0))
        self.btn_mute = flat_button(self.bar, t, "静音", lambda: self._toggle_mute("button"), kind="disabled")
        place_button(self.btn_mute, t, side="left", padx=(px(8), 0))
        self.btn_settings = flat_button(self.bar, t, "设置", self._toggle_settings)
        place_button(self.btn_settings, t, side="right")

    # ----- the middle: optional left column, the white card (transcript / settings / access), the docked card -----
    def _build_main(self):
        t, px = self.theme, self.theme.px
        self.main = tk.Frame(self.root, bg=COLORS["bg_base"])
        self.main.pack(fill="both", expand=True, padx=px(12), pady=(px(8), 0))
        self.left = LinePanel(self.main, t, self._pick_line, self._to_manager, embedded=True)
        self.left.pack_propagate(False)
        self.left.configure(width=self.theme.px(max(200, 12 * self.font_size)))
        self.center = tk.Frame(self.main, bg=COLORS["bg_base"])
        self.center.pack(side="left", fill="both", expand=True)
        self.dock = tk.Frame(self.center, bg=COLORS["bg_base"])
        self.card_frame = tk.Frame(self.center, bg=COLORS["bg_card"], highlightthickness=1,
                                   highlightbackground=COLORS["stroke_card"], highlightcolor=COLORS["stroke_card"])
        self.card_frame.pack(fill="both", expand=True)
        self.pages: dict[str, tk.Frame] = {}
        page = tk.Frame(self.card_frame, bg=COLORS["bg_card"])
        self.pages["transcript"] = page
        page.pack(fill="both", expand=True)
        self.textbox = tk.Text(page, wrap="char", state="disabled", bd=0, highlightthickness=0, padx=px(12), pady=px(12),
                               bg=COLORS["bg_card"], fg=COLORS["text_primary"], cursor="arrow", font=t.font("body"))
        self.scrollbar = ttk.Scrollbar(page, orient="vertical", command=self._user_yview, style="Flat.Vertical.TScrollbar")
        self.textbox.configure(yscrollcommand=self._on_scroll)
        self.scrollbar.pack(side="right", fill="y", pady=px(4))
        self.textbox.pack(side="left", fill="both", expand=True)
        self.textbox.bind("<Configure>", self._on_text_configure)
        self.back_btn = BackToLatest(page, t, self._to_latest)
        self.back_shown = False
        self.follow = True  # auto-scroll to the newest entry until you scroll up

    def _configure_tags(self):
        t, px = self.theme, self.theme.px
        S = self.font_size
        tb = self.textbox
        body, bold, cap, capb, code = (t.font(n) for n in ("body", "body_bold", "cap", "cap_bold", "code"))
        tb.configure(font=body, spacing2=px(0.25 * S), spacing3=px(0.2 * S))
        gap = px(0.6 * S)
        plain = px(2.2 * S)
        for label, tag in (("你", "you"), (max(SPEAKERS.values(), key=bold.measure), "claude"), ("你 · 排队", "queued"), ("通知", "notice")):
            indent = bold.measure(label + " ") + gap
            tb.tag_config(tag + "_label", font=bold, lmargin1=0, lmargin2=indent, spacing1=px(8))
            tb.tag_config(tag, font=body, lmargin1=0, lmargin2=indent, spacing1=px(8))
        tb.tag_config("you_label", foreground=COLORS["accent"])
        tb.tag_config("you", foreground=COLORS["text_primary"])
        tb.tag_config("claude_label", foreground=COLORS["text_primary"])
        tb.tag_config("claude", foreground=COLORS["text_primary"])
        tb.tag_config("queued_label", foreground=COLORS["text_secondary"])
        tb.tag_config("queued", foreground=COLORS["text_secondary"])
        tb.tag_config("queued_dropped", foreground=COLORS["text_tertiary"], font=body, lmargin1=0,
                      lmargin2=bold.measure("你 · 排队 ") + gap, spacing1=px(8))
        tb.tag_config("notice_label", foreground=COLORS["text_secondary"])
        tb.tag_config("notice", foreground=COLORS["text_secondary"])
        tb.tag_config("pending", foreground=COLORS["text_tertiary"], font=body)
        cont = bold.measure("Claude ") + gap
        tb.tag_config("claude_cont", font=body, foreground=COLORS["text_primary"], lmargin1=cont, lmargin2=cont, spacing1=px(4))
        tb.tag_config("tool", foreground=COLORS["text_secondary"], font=cap, lmargin1=plain, lmargin2=plain, spacing1=px(4))
        tb.tag_config("tool_arg", foreground=COLORS["text_secondary"], font=code)
        tb.tag_config("note", foreground=COLORS["text_secondary"], font=cap, lmargin1=plain, lmargin2=plain, spacing1=px(4))
        tb.tag_config("section", foreground=COLORS["text_secondary"], font=cap, justify="center", spacing1=px(16))
        tb.tag_config("card_line", spacing1=px(12))
        tb.tag_config("latin", wrap="word")  # paragraphs with short tokens wrap at words (see ui_theme.wrap_mode)
        tb.tag_config("frozen", foreground=COLORS["text_secondary"], font=cap, lmargin1=plain, lmargin2=plain, spacing1=px(8))

    # ----- the settings area: 设置, its 权限 sub-page, the AI 服务商 page -----
    def _page_frame(self, name: str, title: str, on_back) -> tuple[tk.Frame, tk.Frame, tk.Canvas]:
        """A page of the settings area: a fixed [←] 标题 header over a scrolling body. Returns (page, body, canvas)."""
        t, px = self.theme, self.theme.px
        bg = COLORS["bg_card"]
        page = tk.Frame(self.card_frame, bg=bg)
        self.pages[name] = page
        head = page_header(page, t, title, on_back, bg)
        head.pack(side="top", fill="x", padx=header_padx(t), pady=(px(12), px(8)))
        canvas = tk.Canvas(page, bg=bg, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(page, orient="vertical", command=canvas.yview, style="Flat.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", pady=px(4))
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=bg)
        item = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
        page.head = head
        return page, body, canvas

    def _build_settings(self):
        t, px = self.theme, self.theme.px
        bg = COLORS["bg_card"]
        page, body, self.settings_canvas = self._page_frame("settings", "设置", self._close_page)
        self.settings_body = body
        pad = px(16)

        def group(title, top=px(24)):
            tk.Label(body, text=title, font=t.font("sub"), fg=COLORS["text_primary"], bg=bg, anchor="w").pack(
                fill="x", padx=pad, pady=(top, px(8)))
            g = tk.Frame(body, bg=bg)
            g.pack(fill="x", padx=pad)
            return g

        def row(parent, name):
            r = tk.Frame(parent, bg=bg)
            r.pack(fill="x", pady=(0, px(4)))
            tk.Label(r, text=name, anchor="w", font=t.font("ctrl"), fg=COLORS["text_primary"], bg=bg).pack(side="left")
            return r

        nav = NavGroup(body, t, bg)
        nav.pack(fill="x", padx=pad, pady=(px(4), 0))
        self.access_nav = nav.add("AI 服务商", lambda: self._open_access())
        self.perm_nav = nav.add("权限", self._open_permissions)

        g = group("声音")
        r = row(g, "麦克风")
        self.mic_var = tk.StringVar(value=self._devices[0])
        self.mic_box = ttk.Combobox(r, textvariable=self.mic_var, values=audio.list_devices(False), state="readonly",
                                    style="Card.TCombobox", font=t.font("ctrl"))
        self.mic_box.bind("<<ComboboxSelected>>", lambda e: threading.Thread(target=self._set_mic, args=(self.mic_var.get(),), daemon=True).start())
        self.mic_box.pack(side="right", fill="x", expand=True, padx=(px(16), 0), ipady=px(3))
        r = row(g, "扬声器")
        self.spk_var = tk.StringVar(value=self._devices[1])
        self.spk_box = ttk.Combobox(r, textvariable=self.spk_var, values=audio.list_devices(True), state="readonly",
                                    style="Card.TCombobox", font=t.font("ctrl"))
        self.spk_box.bind("<<ComboboxSelected>>", lambda e: self._set_speaker(self.spk_var.get()))
        self.spk_box.pack(side="right", fill="x", expand=True, padx=(px(16), 0), ipady=px(3))

        g = group("显示")
        r = row(g, "字号")
        self.font_plus = flat_button(r, t, "A+", lambda: self._set_font(self.font_size + 1), padx=10)
        place_button(self.font_plus, t, side="right")
        self.font_label = tk.Label(r, text=str(self.font_size), width=3, font=t.font("ctrl"), fg=COLORS["text_primary"], bg=bg)
        self.font_label.pack(side="right")
        self.font_minus = flat_button(r, t, "A−", lambda: self._set_font(self.font_size - 1), padx=10)
        place_button(self.font_minus, t, side="right")
        for b, at_limit in ((self.font_minus, self.font_size <= FONT_MIN), (self.font_plus, self.font_size >= FONT_MAX)):
            if at_limit:
                style_button(b, t, "disabled")
        # in the body, not the group: their 2 px focus ring sits outside the column, so the text lines up with 字号
        ring = (pad - px(2), pad - px(2))
        CheckRow(body, t, "始终置顶", self.topmost_var, self._set_topmost, bg).pack(fill="x", padx=ring, pady=(px(2), 0))
        CheckRow(body, t, "显示快捷键提示", self.hints_var, self._set_hints, bg).pack(fill="x", padx=ring)

        g = group("高级")
        r = row(g, "配置文件")
        b = flat_button(r, t, "打开", self._open_config)
        place_button(b, t, side="right")
        self.config_caption = tk.Label(g, text=CONFIG_CAPTION, font=t.font("cap"), fg=COLORS["text_secondary"], bg=bg,
                                       anchor="w", justify="left")
        self.config_caption.pack(fill="x")
        self.config_caption.bind("<Configure>", lambda e: self.config_caption.configure(wraplength=max(100, e.width - 4)))

        tk.Frame(body, bg=COLORS["stroke_divider"], height=1).pack(fill="x", padx=pad, pady=(px(24), px(16)))
        b = flat_button(body, t, "退出 voice-claude", self._request_quit)
        place_button(b, t, anchor="w", padx=pad, pady=(0, pad))
        self._paint_access_summary()
        self._refresh_permissions()

    def _build_permissions(self):
        t, px = self.theme, self.theme.px
        bg = COLORS["bg_card"]
        page, body, self.perm_canvas = self._page_frame("permissions", "权限", lambda: self._show_page("settings"))
        pad = px(16)
        self.perm_message = tk.Label(body, text="", font=t.font("cap"), fg=COLORS["state_error"], bg=bg, anchor="w")
        self.transparent_var = tk.BooleanVar(value=self.transparent)
        self.transparent_row = CheckRow(body, t, "通透模式", self.transparent_var, self._set_transparent, bg)
        self.transparent_row.pack(fill="x", padx=(pad - px(2), pad), pady=(px(4), 0))
        self.perm_table = tk.Frame(body, bg=bg)
        self.perm_table.pack(fill="x", padx=pad, pady=(px(4), pad))
        self.perm_widgets: dict[str, Segmented] = {}
        self._perm_rows: list[tuple[tk.Frame, tk.Label, Segmented]] = []
        options = [(k, PERMISSION_WORDS[k]) for k in ("ask", "allow", "deny")]
        for name, spoken in config.PERMISSION_LABELS.items():
            r = tk.Frame(self.perm_table, bg=bg)
            lab = tk.Label(r, text=PERMISSION_NAMES.get(name, spoken), anchor="w", font=t.font("ctrl"), fg=COLORS["text_primary"], bg=bg)
            seg = Segmented(r, t, options, self.cfg.permissions.get(name, "ask"), lambda v, name=name: self._change_permission(name, v), bg)
            self.perm_widgets[name] = seg
            self._perm_rows.append((r, lab, seg))
        self._perm_stacked = None
        self.perm_table.bind("<Configure>", lambda e: self._layout_permissions(e.width))
        self._layout_permissions(0)

    def _layout_permissions(self, width: int):
        """Name left, switch right; when the widest pair does not fit, the whole table goes name-over-switch."""
        px = self.theme.px
        need = max(l.winfo_reqwidth() for _, l, _ in self._perm_rows) + px(8) + max(s.winfo_reqwidth() for _, _, s in self._perm_rows)
        stacked = width > 1 and need > width
        if stacked == self._perm_stacked:
            return
        self._perm_stacked = stacked
        for r, lab, seg in self._perm_rows:
            for w in (r, lab, seg):
                w.pack_forget()
            r.pack(fill="x", pady=(px(10) if stacked else px(4), 0))
            if stacked:
                lab.pack(anchor="w")
                seg.pack(anchor="w", pady=(px(4), 0))
            else:
                lab.pack(side="left")
                seg.pack(side="right")

    def _build_access(self):
        self.access_page = AccessPage(self.card_frame, self.theme, self.client, lambda fn: self.ui_q.put(("call", fn)),
                                      self._access_back, self._on_access_info,
                                      lambda text: self.speaker.say(text) if self.speaker is not None else None)
        self.pages["access"] = self.access_page

    def _rebuild_settings_area(self):
        """A font change: the three pages are built again at the new size (their marks, switches and gaps are sized
        when built). The page that was open stays open, the settings page keeps its scroll position."""
        current = self.page
        try:
            top = self.settings_canvas.yview()[0]
        except tk.TclError:
            top = 0.0
        for name in ("settings", "permissions", "access"):
            p = self.pages.pop(name, None)
            if p is not None:
                p.destroy()
        self.perm_widgets = {}  # the old switches went with their page
        self._build_settings()
        self._build_permissions()
        self._build_access()
        self.page = ""  # nothing is packed in the card now except, maybe, the transcript
        self._show_page(current)
        if current == "access":
            self.access_page.show(self._access_info)
        self.settings_canvas.update_idletasks()
        self.settings_canvas.yview_moveto(top)

    # ----- keys inside the window (the global hotkey is untouched) -----
    def _bind_keys(self):
        r = self.root
        r.bind("<Escape>", lambda e: self._escape())
        r.bind("<End>", lambda e: self._to_latest())
        r.bind("<Prior>", lambda e: self._user_yview("scroll", -1, "pages"))  # through _user_yview: stops following
        r.bind("<Next>", lambda e: self._user_yview("scroll", 1, "pages"))
        for k in ("<Control-equal>", "<Control-plus>"):
            r.bind(k, lambda e: self._set_font(self.font_size + 1))
        r.bind("<Control-minus>", lambda e: self._set_font(self.font_size - 1))
        r.bind("<Control-comma>", lambda e: self._toggle_settings())
        r.bind("<Control-l>", lambda e: self._toggle_panel())
        r.bind("<Control-t>", lambda e: self._set_topmost(not self.topmost_var.get()))

    def _escape(self):
        """One level back: the panel, then 权限 -> 设置, AI 服务商 -> where it was opened from, 设置 -> 对话."""
        if self.panel is not None:
            self._close_panel()
        elif self.page == "permissions":
            self._show_page("settings")
        elif self.page == "access":
            self._access_back()
        elif self.page != "transcript":
            self._close_page()

    def _set_topmost(self, on: bool):
        self.topmost_var.set(on)
        self.root.attributes("-topmost", on)

    def _set_hints(self, on: bool):
        self.show_hints = bool(on)
        self.hints_var.set(self.show_hints)
        px = self.theme.px
        if self.show_hints:
            self.hint_row.pack(side="bottom", fill="x", padx=px(12), pady=(0, px(8)), before=self.bar)
            self.bar.pack_configure(pady=(px(8), px(8)))
        else:
            self.hint_row.pack_forget()
            self.bar.pack_configure(pady=(px(8), px(12)))
        try:
            config.save("ui", "show_hints", self.show_hints)
        except OSError:
            log.warning("could not save show_hints", exc_info=True)

    # ----- pages -----
    def _show_page(self, name: str):
        if name == self.page:
            return
        self._close_panel()
        if self.page == "access":
            self.access_page.on_hide()
        for n, p in self.pages.items():
            if n == name:
                p.pack(fill="both", expand=True)
            else:
                p.pack_forget()
        self.page = name
        style_button(self.btn_settings, self.theme, "secondary" if name == "transcript" else "selected")
        if name != "transcript":
            self.pages[name].focus_set()  # Tab then starts at the page's ← (not at a control in the middle)

    def _close_page(self):
        self._show_page("transcript")

    def _toggle_settings(self):
        """The bar's 设置 and Ctrl+, open and close the whole settings area in one step."""
        if self.page != "transcript":
            self._close_page()
        else:
            self._open_settings()

    def _reload_permissions(self):
        try:  # the daemon may have changed the table since we started (spoken permission changes)
            self.cfg.permissions = config.load().permissions
        except Exception:
            log.debug("could not reload permissions", exc_info=True)
        self._refresh_permissions()

    def _open_settings(self):
        self._close_panel()
        self._reload_permissions()
        self.config_caption.configure(text=CONFIG_CAPTION, fg=COLORS["text_secondary"])
        self._paint_access_summary()
        self._show_page("settings")

    def _open_permissions(self):
        self._reload_permissions()
        self.perm_message.pack_forget()
        self._show_page("permissions")

    def _set_transparent(self, on: bool):
        """The 通透模式 row says the same fixed phrase as 打开 / 关闭通透模式. The service asks out loud before it
        turns the mode on, so the row only ever shows what the service reports."""
        self.transparent_var.set(self.transparent)
        if not self.ready or self.daemon_gone:
            self.perm_message.configure(text="后台服务未连接")
            self.perm_message.pack(fill="x", padx=self.theme.px(16), pady=(0, self.theme.px(8)), before=self.transparent_row)
            return
        threading.Thread(target=self._phrase, args=("transparent_on" if on else "transparent_off",), daemon=True).start()
        if on:
            self._close_page()  # the request comes as a card on the transcript

    def _show_transparent(self, on: bool):
        self.transparent = on
        if hasattr(self, "transparent_var"):
            self.transparent_var.set(on)
        if on:
            self.transparent_label.pack(side="right", before=self.status_label, padx=(self.theme.px(8), 0))
        else:
            self.transparent_label.pack_forget()
        if hasattr(self, "perm_nav"):
            self._refresh_permissions()

    def _refresh_permissions(self):
        self.perm_nav.set_value(permissions_summary(self.cfg.permissions, self.transparent))
        for name, seg in getattr(self, "perm_widgets", {}).items():
            seg.set_value(self.cfg.permissions.get(name, "ask"))

    def _change_permission(self, name: str, value: str):
        """Not a direct edit of config.toml: the sentence goes to the line as if you had said it, so Claude asks
        out loud ("要把点击改成允许，可以吗？") exactly like a spoken change would."""
        label = PERMISSION_NAMES.get(name, config.PERMISSION_LABELS.get(name, name))
        sent, why = self._send_text(f"把{label}的权限改成{PERMISSION_WORDS.get(value, value)}")
        if sent:
            self._close_page()
            return
        self.perm_message.configure(text=why)
        self.perm_message.pack(fill="x", padx=self.theme.px(16), pady=(0, self.theme.px(8)), before=self.perm_table)
        self.root.after(4000, lambda: self.perm_message.winfo_exists() and self.perm_message.pack_forget())

    def _open_config(self):
        try:
            os.startfile(str(config.user_config_path()))
        except Exception as e:
            self.config_caption.configure(text=f"无法打开：{e}", fg=COLORS["state_error"])

    def _open_access(self, access: dict | None = None):
        if access is not None:
            self._access_info = access
        if self.page != "access":
            self.access_return = "settings" if self.page in ("settings", "permissions") else "transcript"
        self.access_page.show(self._access_info)
        self._show_page("access")

    def _access_back(self):
        self._show_page(self.access_return or "transcript")

    def _on_access_info(self, access: dict):
        """The AI 服务商 page (or a status check) has a fresh `access`: the settings row shows it."""
        if isinstance(access, dict) and access:
            self._access_info = access
        self._paint_access_summary()

    def _paint_access_summary(self):
        nav = getattr(self, "access_nav", None)
        if nav is not None and nav.winfo_exists():
            nav.set_value(describe_access(self._access_info), describe_is_error(self._access_info))

    def _recheck_access(self):
        """A line changed service (a spoken switch): read the access state again, off the UI thread."""
        def work():
            try:
                r = self.client.access_status()
            except Exception:
                return
            if isinstance(r, dict) and isinstance(r.get("access"), dict):
                self.ui_q.put(("call", lambda: self._on_access_info(r["access"])))
        threading.Thread(target=work, daemon=True, name="access-recheck").start()

    def _send_text(self, text: str) -> tuple[bool, str]:
        """Send a sentence to the active line as if it had been recognised (the settings pages use it).
        Returns (sent, why not) — why in the words the page shows."""
        with self.lock:
            if not self.ready or self.daemon_gone:
                return False, "后台服务未连接"
            if self.state not in (IDLE, LISTENING) or self.turn_open:
                return False, "正在回答，请稍后再试"
            self._line("你", text)
            self._begin_turn()
        threading.Thread(target=self._say, args=(text,), daemon=True).start()
        return True, ""

    # ----- 退出 voice-claude: stop the background service, then close the window -----
    def _busy_lines(self) -> list[str]:
        """Names of the lines that are still working or waiting for you (the manager counts as 总机)."""
        lines, active, _ = self._lines_snapshot
        names = []
        for lid, l in lines.items():
            if l.get("state") in ("working", "waiting"):
                names.append("总机" if (l.get("kind") == "manager" or lid == "manager") else str(l.get("name") or lid))
        if (self.turn_open or self.card is not None) and active:
            current = lines.get(active, {})
            name = "总机" if (current.get("kind") == "manager" or active == "manager") else str(current.get("name") or active)
            if name not in names:
                names.insert(0, name)
        return names

    def _request_quit(self):
        self._close_panel()
        if self._quit_requested:
            return
        if not self.ready or self.daemon_gone or not getattr(self.client, "connected", False):
            log.info("quit without the service (ready=%s, gone=%r)", self.ready, self.daemon_gone)
            self.quit()
            return
        # One question for everything that quitting interrupts. Running jobs are stopped, not left behind: whether a
        # job outlives the service depends on how the service was started, and nobody would be watching it anyway.
        parts = []
        busy = self._busy_lines()
        if busy:
            parts.append(f"{busy[0]} 正在回答。" if len(busy) == 1 else f"{len(busy)} 条线路正在进行。")
        running = [j for j in self.jobs.values() if j.get("state") == "running"]
        if running:
            parts.append(f"有 {len(running)} 个后台任务正在运行，退出会停止{'它' if len(running) == 1 else '它们'}。")
        if parts and not ask_ok(self.root, "退出 voice-claude", "\n".join(parts) + "\n仍要退出吗？"):
            return
        jobs = "stop" if running else "ask"  # jobs this window has not seen: the service asks about them out loud
        self._quit_requested = True
        log.info("quit: asking the service to shut down (jobs=%s)", jobs)
        self._status("正在退出…")
        threading.Thread(target=self._shutdown_for_quit, args=(jobs, len(self._lines_snapshot[0])), daemon=True,
                         name="quit").start()

    def _shutdown_for_quit(self, jobs: str, n_lines: int):
        try:
            try:
                r = self.client.shutdown(jobs=jobs)
            except TypeError:  # a client without the jobs argument: the service asks about jobs by voice
                r = self.client.shutdown()
        except Exception as e:
            log.warning("shutdown for quit failed: %s", e)
            self.ui_q.put(("call", self._quit_unanswered))
            return
        if "先问" in str(r.get("message", "")):
            return  # the service asks about the running jobs by voice; its bye closes the window
        wait_ms = int((5 + 3 * max(1, n_lines)) * 1000)  # each line gets up to 3 s to close
        self.ui_q.put(("call", lambda: self.root.after(wait_ms, self._quit_anyway)))

    def _quit_anyway(self):
        if not self._closing:
            log.info("no bye after the shutdown; closing the window anyway")
            self.quit()

    def _quit_unanswered(self):
        if ask_ok(self.root, "退出 voice-claude", "后台服务未响应。\n关闭窗口？"):
            self.quit()
            return
        self._quit_requested = False
        self._status("空闲")

    # ----- the line panel (overlay) and the chip -----
    def _toggle_panel(self):
        if self.panel is not None:
            self._close_panel()
            return
        if self.large:
            self.left.focus_set()
            return
        lines, active, _ = self._lines_snapshot
        self.panel = LinePanel(self.card_frame, self.theme, self._pick_line, self._to_manager, self._open_settings,
                               on_quit=self._request_quit)
        self.panel.update_rows(lines, active, self.jobs, show_settings=self._bar_mode >= 1)
        self.panel.place(in_=self.card_frame, relx=1.0, x=-self.theme.px(8), y=self.theme.px(8), anchor="ne")
        self.panel.lift()
        self.panel.focus_set()

    def _close_panel(self):
        if self.panel is not None:
            self.panel.destroy()
            self.panel = None

    def _on_root_click(self, event):
        if self.panel is None:
            return
        w = event.widget
        while w is not None:
            if w is self.panel or w is self.chip:
                return
            w = getattr(w, "master", None)
        self._close_panel()

    def _pick_line(self, lid: str):
        self._close_panel()
        if lid and lid != self.client.active:
            threading.Thread(target=self._safe, args=(self.client.switch, lid), daemon=True).start()

    def _to_manager(self):
        self._close_panel()
        threading.Thread(target=self._phrase, args=("back_to_manager",), daemon=True).start()

    def _speaker(self) -> str:
        """The name over the model's words: the vendor of the active line (Claude / DeepSeek / GPT)."""
        lines, active, _ = self._lines_snapshot
        provider = str((lines.get(active) or {}).get("provider") or "claude")
        return SPEAKERS.get(provider, provider)

    def _update_lines(self, lines: dict, active: str, name: str):
        self._lines_snapshot = (lines, active, name)
        sig = tuple(sorted((str(k), str(l.get("provider") or "")) for k, l in lines.items()))
        if sig != self._provider_sig:
            changed = bool(self._provider_sig)
            self._provider_sig = sig
            if changed:  # a line moved to another service (a spoken switch): show the new state
                self._recheck_access()
                if self.page == "access":
                    self.access_page.refresh()
        current = lines.get(active, {})
        manager = current.get("kind") == "manager" or active == "manager" or current.get("name") == "总机"
        shown = "总机" if manager else (name or active or "")
        unread = sum(int(l.get("unread") or 0) for l in lines.values())
        waiting = sum(1 for lid, l in lines.items() if lid != active and l.get("state") == "waiting")
        badge, kind = (waiting, "alert") if waiting else (unread, "neutral")
        self.chip.set(shown, badge, kind)
        self.root.title(f"{shown} — voice-claude" if shown else "voice-claude")
        self.left.update_rows(lines, active, self.jobs)
        if self.panel is not None:
            self.panel.update_rows(lines, active, self.jobs, show_settings=self._bar_mode >= 1)

    # ----- layout: large / small, column width, action-bar fallbacks -----
    def _on_configure(self, event):
        if event.widget is not self.root:
            return
        large = self.root.winfo_width() >= self.theme.px(640)
        if large != self.large:
            self.large = large
            if large:
                self._close_panel()
                self.left.configure(width=self.theme.px(max(200, 12 * self.font_size)))
                self.left.pack(side="left", fill="y", padx=(0, self.theme.px(8)), before=self.center)
            else:
                self.left.pack_forget()
        self._fit_bar()
        self._fit_hint()
        self._fit_chip()
        if self.card is not None:
            self.card.fit_height(int(self.root.winfo_height() * 0.45))

    def _on_text_configure(self, event):
        if event.widget is not self.textbox:
            return
        self._fit_columns(event.width)
        wrap = event.width - 2 * int(self.textbox.cget("padx")) - self.theme.px(4)
        for card in self.answered:
            card.set_width(wrap)

    def _fit_columns(self, width: int):
        base = self.theme.px(16 if self.large else 12)
        extra = max(0, (width - 2 * base - self.theme.col_max) // 2)
        padx = base + extra
        if int(self.textbox.cget("padx")) != padx:
            self.textbox.configure(padx=padx)
        if extra != self._col_extra:  # the docked card follows the capped text column
            self._col_extra = extra
            if self.card is not None:
                self.dock.pack_configure(padx=extra)

    def _fit_bar(self):
        """When the four buttons do not fit: settings moves into the chip menu, then mute shrinks to an icon."""
        px = self.theme.px
        width = self.bar.winfo_width()
        if width <= 1:
            return
        need = sum(b._frame.winfo_reqwidth() for b in (self.btn_tap, self.btn_conv, self.btn_mute)) + 2 * px(8)
        settings_w = self.btn_settings._frame.winfo_reqwidth() + px(8)
        mode = 0 if need + settings_w <= width else 1 if need <= width else 2
        if mode == self._bar_mode:
            return
        self._bar_mode = mode
        if mode == 0:
            self.btn_settings._frame.pack(side="right")
        else:
            self.btn_settings._frame.pack_forget()
        self.btn_mute.configure(text=("静" if mode == 2 else ("取消静音" if self.muted else "静音")))
        if self.panel is not None:
            lines, active, _ = self._lines_snapshot
            self.panel.update_rows(lines, active, self.jobs, show_settings=mode >= 1)

    def _fit_chip(self):
        px = self.theme.px
        room = self.band.winfo_width() - (px(6) + px(6) + self.orb.winfo_reqwidth() + px(12) + self.status_label.winfo_reqwidth()
                                          + px(8) + px(12) + px(8))
        if room > 0:
            self.chip.set(self.chip.name, self.chip.badge, self.chip.badge_kind, dark=self.chip.dark, max_width=max(px(40), room))

    def _fit_hint(self):
        text, strong = self._hint_shown[:2] if self._hint_shown else ("", "")
        avail = self.hint_row.winfo_width() - self.hint_hide.winfo_reqwidth() - self.theme.px(12)
        if avail > 40 and text:
            font = self.theme.font("ctrl_bold" if strong else "cap")
            self.hint_var.set(fit_hint(text, font.measure, avail))

    # ----- scrolling -----
    def _on_scroll(self, first, last):
        self.scrollbar.set(first, last)
        if float(last) >= 0.999:
            self.follow = True
        self._show_back(not self.follow)

    def _user_yview(self, *args):
        """The scrollbar or the wheel moved the view: stop following until the bottom is reached again."""
        self.follow = False
        self.textbox.yview(*args)
        if self._at_bottom():
            self.follow = True
        self._show_back(not self.follow)

    def _at_bottom(self) -> bool:
        return self.textbox.yview()[1] >= 0.999

    def _show_back(self, show: bool):
        if show == self.back_shown:
            return
        self.back_shown = show
        if show:
            self.back_btn.place(relx=1.0, rely=1.0, x=-self.theme.px(20), y=-self.theme.px(12), anchor="se")
            tk.Misc.tkraise(self.back_btn)  # Canvas.lift is tag_raise; this raises the widget itself
        else:
            self.back_btn.place_forget()

    def _to_latest(self):
        self.follow = True
        self.textbox.see("end")
        self._show_back(False)

    def _wheel(self, event):
        w = self.root.winfo_containing(event.x_root, event.y_root)
        if w is not None and w.winfo_toplevel() is not self.root:
            return None  # a Combobox popdown: let it scroll its own list
        target = (self.settings_canvas if self.page == "settings" else self.access_page.canvas if self.page == "access"
                  else self.textbox)
        while w is not None:
            if self.card is not None and w is self.card:
                target = self.card.text
                break
            if w is self.panel and self.panel is not None:
                return "break"
            w = getattr(w, "master", None)
        if target is self.textbox:
            self._user_yview("scroll", int(-event.delta / 120) * 3, "units")
        else:
            target.yview_scroll(int(-event.delta / 120) * 3, "units")
        return "break"

    # ----- transcript insertion -----
    def _begin_insert(self, paragraph: bool = True) -> bool:
        self.textbox.configure(state="normal")
        if paragraph and self.textbox.compare("end-1c", "!=", "1.0"):
            self.textbox.insert("end", "\n")  # a new paragraph; the very first entry needs no blank line above
        return self.follow

    def _end_insert(self, follow: bool, entry: str = "other"):
        self._last_entry = entry
        self._trim_transcript()
        self.textbox.configure(state="disabled")
        if follow:
            self.textbox.see("end")

    def _insert_labelled(self, tag: str, label: str, body: str):
        follow = self._begin_insert()
        tb = self.textbox
        tb.insert("end", label + " ", tag + "_label")
        tb.insert("end", body, tag)
        self._set_wrap(body)
        self._end_insert(follow)

    def _column_px(self) -> int:
        """Width of the text column in pixels (the capped column until the Text has been laid out)."""
        w = self.textbox.winfo_width() - 2 * int(self.textbox.cget("padx"))
        return w if w > 50 else self.theme.col_max

    def _set_wrap(self, text: str):
        """Word wrap for the current (last) paragraph unless one of its tokens is too wide (see ui_theme.wrap_mode)."""
        tb = self.textbox
        start, end = tb.index("end-1c linestart"), tb.index("end-1c")
        tb.tag_remove("latin", start, end)
        if wrap_mode(text, self.theme.font("body").measure, self._column_px()) == "word":
            tb.tag_add("latin", start, end)

    def _insert_small(self, s: str, tag: str = "note"):
        follow = self._begin_insert()
        self.textbox.insert("end", s, tag)
        self._end_insert(follow)

    def _insert_tool(self, s: str):
        """'· Bash  ls -F …' on one line, cut to the width; '· 麦克风：…' (no ASCII tool name) is a plain small line."""
        rest = s[2:]
        name, _, arg = rest.partition(" ")
        arg = arg.strip().replace("\n", " ")
        if not is_ascii(name) or not arg:
            self._insert_small(s)
            return
        t = self.theme
        width = self.textbox.winfo_width() - 2 * int(self.textbox.cget("padx")) - t.px(2.2 * self.font_size) - t.px(8)
        if width < 80:
            width = t.px(300)
        head = f"· {name}  "
        font = t.font("code") if is_ascii(arg) else t.font("cap")
        arg = truncate_to_width(arg, font.measure, width - t.font("cap").measure(head))
        follow = self._begin_insert()
        self.textbox.insert("end", head, "tool")
        self.textbox.insert("end", arg, ("tool", "tool_arg") if is_ascii(arg) else "tool")
        self._end_insert(follow)

    def _insert_card(self, card: AnsweredCard):
        follow = self._begin_insert()
        tb = self.textbox
        tb.tag_add("card_line", "end-1c linestart")
        tb.window_create("end", window=card, padx=0, pady=0, stretch=0)
        self.answered.append(card)
        wrap = self.textbox.winfo_width() - 2 * int(self.textbox.cget("padx")) - self.theme.px(4)
        card.set_width(max(wrap, self.theme.px(200)))
        self._end_insert(follow)
        if len(self.answered) > 50:
            self._freeze_card(self.answered.pop(0))

    def _freeze_card(self, card: AnsweredCard):
        """An old answered card becomes plain text (embedded windows are the expensive part of a long transcript)."""
        try:
            idx = self.textbox.index(card)
        except tk.TclError:
            return
        self.textbox.delete(idx)
        self.textbox.insert(idx, card.as_text().replace("\n", " · "), "frozen")
        card.destroy()

    def _trim_transcript(self):
        tb = self.textbox
        if int(tb.index("end-1c").split(".")[0]) > 3000:
            doomed = [c for c in self.answered if tb.compare(tb.index(c), "<", "500.0")] if self.answered else []
            tb.delete("1.0", "500.0")
            for c in doomed:
                self.answered.remove(c)
                c.destroy()

    def _retag_queued(self, dropped: bool):
        tb = self.textbox
        for old, new in (("queued_label", "queued_dropped" if dropped else "you_label"),
                         ("queued", "queued_dropped" if dropped else "you")):
            ranges = tb.tag_ranges(old)
            for i in range(0, len(ranges), 2):
                tb.tag_remove(old, ranges[i], ranges[i + 1])
                tb.tag_add(new, ranges[i], ranges[i + 1])

    def _drop_pending(self):
        r = self.textbox.tag_ranges("pending")
        if r:
            self.textbox.delete(r[0], r[1])

    # ----- items from the voice side -----
    def _handle_line(self, who: str, s: str):
        if who == "你":
            if self.card is not None:
                self._card_heard(s)
                return
            if s.startswith("（排队）"):
                self._insert_labelled("queued", "你 · 排队", s[len("（排队）"):])
            else:
                self._insert_labelled("you", "你", s)
                if textmod.fixed_phrase(s) == "open_access":  # "打开接入页" is a window action, not a daemon one
                    self._open_access()
            return
        self.textbox.configure(state="normal")
        self._retag_queued(False)
        self.textbox.configure(state="disabled")
        self._cont = False
        who = self._speaker()
        if s == "":
            follow = self._begin_insert()
            self.textbox.insert("end", who + " ", "claude_label")
            self.textbox.insert("end", "…", "pending")
            self._end_insert(follow, "claude")
        else:
            self._insert_labelled("claude", who, s)
            self._last_entry = "claude"

    def _handle_append(self, s: str):
        cont = self._last_entry != "claude"  # a tool line came in between: continue on a new indented line
        follow = self._begin_insert(paragraph=cont)
        self._drop_pending()
        self.textbox.insert("end", s, "claude_cont" if cont or self._cont else "claude")
        self._cont = cont or self._cont
        self._set_wrap(self.textbox.get("end-1c linestart", "end-1c"))
        self._end_insert(follow, "claude")

    def _handle_note(self, s: str):
        now = time.monotonic()
        if s.startswith("后台服务断开了：") and s == self._last_note[0] and now - self._last_note[1] < 2.0:
            return  # _gone() repeats the same line on every tap after a disconnect; other repeats are real
        self._last_note = (s, now)
        if s == "不认识的短语 open_access":
            return  # the daemon does not know the phrase; the window opened the access page from the 你 line
        if s.startswith("（申请）") or s.startswith("（问你）"):
            kind = "question" if s.startswith("（问你）") else "approve"
            q, _, detail = s[4:].partition("\n　　")
            self._open_card(kind, q.strip(), detail.strip())
        elif s.startswith("（你说：可以）"):
            self._resolve_answer(True)
        elif s.startswith("（按键：可以）"):
            self._resolve_card(f"你按了{key_name(self.cfg.send_key)}，算可以", "ok")
        elif s.startswith("（点了：可以）"):
            self._resolve_card("你点了「可以」", "ok")
        elif s.startswith("（你说：不行）"):
            self._resolve_answer(False)
        elif s.startswith("（两次没听清"):
            heard = self.card_info.get("heard", "")
            self._resolve_card("没听清，这步先不做" + (f"（听到：{heard}）" if heard else ""), "no")
        elif s.startswith("（撤销了排队"):
            self.textbox.configure(state="normal")
            self._retag_queued(True)
            self.textbox.configure(state="disabled")
            self._insert_small(s)
        elif s.startswith("（紧急停止）"):
            self._estop_until = now + 3.0
            self._insert_small(s)
        elif s.startswith("——"):
            self._insert_small(s, "section")
        elif s.startswith("· "):
            self._insert_tool(s)
        elif parse_job_note(s):
            self._handle_job(*parse_job_note(s))
        elif s.startswith("出错："):
            self._insert_card(AnsweredCard(self.textbox, self.theme, "error", "出错了", "这一轮出错了，看一下屏幕。", s[3:].strip()))
        elif s.startswith("后台服务断开了："):
            reason = s[len("后台服务断开了："):].split("。")[0]
            if "已关闭" in reason:
                self._insert_labelled("notice", "通知", "后台服务已经关掉了。要再用，关掉这个窗口再打开。")
            else:
                self._insert_card(AnsweredCard(self.textbox, self.theme, "error", "出错了", "后台服务断开了，请关掉程序再打开。", reason))
        elif s.startswith("启动失败："):
            self._insert_card(AnsweredCard(self.textbox, self.theme, "error", "出错了", "启动失败", s[5:].strip()))
        elif s.startswith("（") or s.startswith("麦克风已切换到") or s.startswith("扬声器已切换到"):
            self._insert_small(s)
        else:
            self._insert_labelled("notice", "通知", s)

    def _handle_job(self, jid: str, state: str, note: str):
        job = self.jobs.setdefault(jid, {"id": jid, "started": time.time(), "note": ""})
        job["state"] = state
        if note:
            job["note"] = note
        if state != "running":
            job["ended"] = time.time()
        what = job.get("note") or jid
        self._insert_small(f"· 后台任务开始了：{what}" if state == "running"
                           else f"· 后台任务 {jid}（{what}）{JOB_WORDS.get(state, state)}")
        lines, active, name = self._lines_snapshot
        self._update_lines(lines, active, name)

    # ----- the docked request card -----
    def _open_card(self, kind: str, question: str, detail: str):
        if self.card is not None:
            self._resolve_card("没有回答，算不行", "no")
        self._fg_before_card = foreground_outside(self.root)
        title, line, body = ask_title(question, kind)
        self.card = DockedCard(self.dock, self.theme, kind, title, body, detail, self.hint_var, self.ASK_SECONDS)
        self.card_info = {"kind": kind, "title": title, "line": line, "body": body, "detail": detail, "heard": "",
                          "override": "", "started": time.monotonic()}
        self._card_gone_ticks = 0
        self.card.pack(fill="x")
        self.dock.pack(side="bottom", fill="x", padx=self._col_extra, pady=(self.theme.px(8), 0), before=self.card_frame)
        self.card.fit_height(int(self.root.winfo_height() * 0.45))
        self.card.set_remaining(self.ASK_SECONDS)
        if self.page != "transcript":
            self._show_page("transcript")

    def _card_heard(self, text: str):
        self.card_info["heard"] = text
        if self.card_info.get("kind") == "approve" and textmod.yes_or_no(text) is None:
            self.card_info["override"] = f"没听清「{text}」— 可以，还是不行？"
            self._hint_shown = ()

    def _resolve_answer(self, yes: bool):
        if self.card is None:
            return
        heard = self.card_info.get("heard", "")
        if yes:
            self._resolve_card("你说：可以" + heard_suffix(heard, "可以"), "ok")
        elif heard:
            self._resolve_card("你说：不行" + heard_suffix(heard, "不行"), "no")
        elif self.daemon_gone:
            self._resolve_card("后台服务断开了，算不行", "no")
        else:
            self._resolve_card(f"你按了{key_name(self.cfg.hotkey)}，算不行", "no")

    def _resolve_card(self, footnote: str, severity: str):
        card, info = self.card, self.card_info
        if card is None:
            return
        self.card = None
        self.card_info = {}
        card.destroy()
        self.dock.pack_forget()
        if info.get("kind") == "question":
            severity = "question"
        self._insert_card(AnsweredCard(self.textbox, self.theme, severity, info.get("title", ""), info.get("body", ""),
                                       info.get("detail", ""), footnote))
        self._hint_shown = ()

    def _tick_card(self):
        if self.card is None:
            return
        remaining = self.ASK_SECONDS - (time.monotonic() - self.card_info["started"])
        self.card.set_remaining(max(0.0, remaining))
        if not self.card_info.get("fitted") and self.card.text.winfo_width() > 1:
            self.card_info["fitted"] = True
            self.card.fit_height(int(self.root.winfo_height() * 0.45))
            if self.follow:
                self.textbox.see("end")
        if self.state != APPROVING:
            self._card_gone_ticks += 1
            if self._card_gone_ticks >= 3:  # the resolution note, if any, has had its turn in the queue
                heard = self.card_info.get("heard", "")
                if self.card_info.get("kind") == "question" and heard:
                    self._resolve_card(f"你说：{heard}", "question")
                else:
                    self._resolve_card("没有回答，算不行", "no")
        else:
            self._card_gone_ticks = 0

    # ----- fonts -----
    def _apply_font(self):
        t = self.theme
        t.set_size(self.font_size)
        t.apply_ttk_style()
        self._configure_tags()
        self.root.minsize(t.min_w, t.min_h)
        self.band.configure(height=t.bar_h)
        self.orb.resize()
        self.chip.set(self.chip.name, self.chip.badge, self.chip.badge_kind, dark=self.chip.dark)
        self.hint_row.configure(height=t.hint_h)
        for b in (self.btn_tap, self.btn_conv, self.btn_mute, self.btn_settings):
            b.configure(highlightthickness=t.px(2))
        if self._area_ready:
            self._rebuild_settings_area()
        lines, active, name = self._lines_snapshot
        self.left.update_rows(lines, active, self.jobs)
        self._band_shown = ()
        self._hint_shown = ()
        self.textbox.configure(padx=t.px(12))
        if self.textbox.winfo_width() > 1:
            self._fit_columns(self.textbox.winfo_width())

    # ----- queue plumbing (names used by the voice side and the test scripts) -----
    def _status(self, s: str):
        self.ui_q.put(("status", s))

    def _line(self, who: str, s: str):
        self.ui_q.put(("line", who, s))

    def _append(self, s: str):
        self.ui_q.put(("append", s))

    def _note(self, s: str):
        self.ui_q.put(("note", s))

    def _drain_ui(self):
        try:
            while True:
                item = self.ui_q.get_nowait()
                kind = item[0]
                if kind == "status":
                    self._status_raw = item[1]
                elif kind == "level":
                    self._level = float(item[1])
                elif kind == "font":
                    self._apply_font()
                elif kind == "devices":
                    self._devices = (item[1], item[2])
                    self.mic_var.set(item[1])
                    self.spk_var.set(item[2])
                elif kind == "lines":
                    self._update_lines(item[1], item[2], item[3])
                elif kind == "call":
                    item[1]()
                elif kind == "access":
                    self._open_access(item[1] if len(item) > 1 else None)
                elif kind == "line":
                    self._handle_line(item[1], item[2])
                elif kind == "append":
                    self._handle_append(item[1])
                elif kind == "note":
                    self._handle_note(item[1])
                if self._closing:
                    return  # quit() destroyed the window
        except queue.Empty:
            pass
        except Exception:
            log.exception("ui item failed")
            try:
                self.textbox.configure(state="disabled")  # an item that failed between _begin_insert and _end_insert
            except tk.TclError:
                pass
        if self._closing:
            return
        try:
            self._tick_ui()
        except Exception:
            log.exception("ui tick failed")
        self._drain_after = self.root.after(50, self._drain_ui)

    def _tick_ui(self):
        """Everything that is derived from the current state: band colour, orb, buttons, hint, countdown."""
        now = time.monotonic()
        state, conv, muted = self.state, self.conversation, self.muted
        raw = self._status_raw
        key = classify_status(raw)
        text = raw
        if now < self._estop_until:
            key, text = "estopped", "已紧急停止"
        elif state == BUSY and conv and self.player is not None and getattr(self.player, "ducked", False):
            key, text = ("you_stopped", "停了，在听你说") if self.cut else ("you_talking", "你在说…")
        elif key == "waiting" and self.card_info.get("line"):
            text = f"{self.card_info['line']} 在等你"
        text = status_display(text, muted)
        mic_open = conv and self.cfg.barge_in and state == BUSY
        band = band_for(key, mic_open, muted)
        bg_token, edge_token = BAND_COLORS[band]
        bg = COLORS[bg_token]
        orb_kind, fg_token, _ = STATES[key]
        fg = COLORS[fg_token]
        if muted and key in ("listening", "recording", "you_talking", "you_stopped"):
            fg = COLORS["text_secondary"]
        barge = key in ("thinking", "tool", "speaking", "transcribing") and mic_open and not muted
        shown = (text, key, bg, fg, muted, barge)
        if shown != self._band_shown:
            self._band_shown = shown
            self.status_var.set(text)
            self.root.after_idle(self._fit_chip)
            self.band.configure(bg=bg)
            self.edge.configure(bg=COLORS[edge_token] if edge_token else bg)
            self.status_label.configure(bg=bg, fg=fg)
            self.transparent_label.configure(bg=bg, fg=fg)
            self.orb.set(orb_kind, bg, muted, barge)
            self.chip.set(self.chip.name, self.chip.badge, self.chip.badge_kind, bg=bg, dark=(band == "ask"))
        self.orb.set_level(self._level)
        # buttons
        enabled = self.ready and not self.daemon_gone
        conv_text = "■ 结束对话" if conv else "● 开始对话"
        tap = {IDLE: "说一段", RECORDING: "发送", LISTENING: "发送", BUSY: "打断", APPROVING: "不行"}[state]
        if state == BUSY and self.queued:
            tap = f"撤销排队 · {len(self.queued)}"
        tap_kind = "disabled" if not enabled else "danger" if state == APPROVING else "primary"
        mute_text = "静" if self._bar_mode == 2 else ("取消静音" if muted else "静音")
        mute_kind = "disabled" if not enabled else "mute" if muted else "secondary"
        if state == APPROVING and self.card is not None and self.card_info.get("kind") == "approve":
            mute_text, mute_kind = "可以", ("disabled" if not enabled else "primary")
        for b, txt, kind in ((self.btn_tap, tap, tap_kind), (self.btn_conv, conv_text, "disabled" if not enabled else "secondary"),
                             (self.btn_mute, mute_text, mute_kind)):
            if b.cget("text") != txt:
                b.configure(text=txt)
            if b._kind != kind:
                style_button(b, self.theme, kind)
        # hint row and card footnote
        if not self.ready:
            situation = "disconnected" if key in ("failed", "disconnected") else "starting"
        elif self.daemon_gone:
            situation = "closed" if "已关闭" in self.daemon_gone else "disconnected"
        elif state == APPROVING or self.card is not None:
            situation = "ask_muted" if muted else "question" if self.card_info.get("kind") == "question" else "ask"
        elif muted:
            situation = "muted"
        elif state == RECORDING:
            situation = "recording"
        elif state == LISTENING:
            situation = "listening"
        elif state == BUSY:
            situation = ("busy_queue" if self.queued else "busy_conv") if conv else "busy_single"
        else:
            situation = "idle"
        hint = hint_for(situation, key_name(self.cfg.hotkey), key_name(self.cfg.send_key) if self.cfg.send_key else "", self.cfg.hold_seconds)
        text, strong = hint.text, hint.strong
        if self.card is not None and self.card_info.get("override"):
            text, strong = self.card_info["override"], "ask"
        if (text, strong) != self._hint_shown:
            self._hint_shown = (text, strong)
            self.hint_label.configure(font=self.theme.font("ctrl_bold" if strong else "cap"),
                                      fg=COLORS["state_listen"] if strong == "listen" else COLORS["state_ask"] if strong == "ask" else COLORS["text_secondary"])
            self.hint_var.set(text)
            self._fit_hint()
        self._tick_card()
        if self.follow and self.page == "transcript" and not self._at_bottom():
            self.textbox.see("end")
        # jobs disappear ten minutes after they ended
        stale = [jid for jid, j in self.jobs.items() if j.get("ended") and time.time() - j["ended"] > 600]
        if stale:
            for jid in stale:
                del self.jobs[jid]
            self._update_lines(*self._lines_snapshot)
        # the access page opens by itself when the service has no way to reach Claude
        if self.ready and not self._access_checked:
            self._access_checked = True
            acc = (self.client.info or {}).get("access")
            if isinstance(acc, dict):
                self._access_info = acc
                self._paint_access_summary()
                if not acc.get("ok"):
                    self._open_access(acc)


SPEAKERS = {"claude": "Claude", "deepseek": "DeepSeek", "openai": "GPT"}
FONT_MIN, FONT_MAX = 8, 40
CONFIG_CAPTION = "快捷键、停顿时长、朗读声音和服务参数"


def ask_ok(parent, title: str, text: str) -> bool:
    """A native OK / Cancel question, Cancel being the default. Tests replace it."""
    return bool(messagebox.askokcancel(title, text, parent=parent, icon="warning", default="cancel"))

KEY_NAMES = {"right ctrl": "右 Ctrl", "left ctrl": "左 Ctrl", "right alt": "右 Alt", "pause": "Pause", "scroll lock": "Scroll Lock"}


def key_name(key: str) -> str:
    return KEY_NAMES.get(key, key.upper() if key.startswith("f") else key)
