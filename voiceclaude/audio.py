"""Microphone capture, VAD-based end-of-utterance detection, and audio playback."""
from __future__ import annotations

import ctypes
import logging
import os
import queue
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd
import sherpa_onnx

from .config import VAD_MODEL

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK = 512  # samples per chunk = 32 ms; also the Silero VAD window size
TTS_RATE = 24000
DUCK_GAIN = 0.2  # volume while ducked


def _unbiased_query():
    if os.name != "nt":
        return None
    try:
        f = ctypes.windll.kernel32.QueryUnbiasedInterruptTime
        f.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
        f.restype = ctypes.c_int
        return f if f(ctypes.byref(ctypes.c_ulonglong())) else None
    except Exception:
        return None


_unbiased_ticks = _unbiased_query()


def awake_clock() -> float:
    """Seconds the PC has been awake. On Windows time.monotonic keeps counting while the PC sleeps, so speech paused
    by a sleep would look stuck the moment it wakes; QueryUnbiasedInterruptTime stops while it sleeps."""
    if _unbiased_ticks is None:
        return time.monotonic()
    t = ctypes.c_ulonglong()
    _unbiased_ticks(ctypes.byref(t))
    return t.value / 1e7


def find_device(name_part: str, output: bool, fallback: bool = False) -> int | None:
    """Index of the first device whose name contains `name_part` (MME host API); None = system default. A name that
    matches nothing raises, or with fallback (at startup: the headphones may simply not be plugged in) gives the
    system default."""
    if not name_part:
        return None
    key = "max_output_channels" if output else "max_input_channels"
    for i, d in enumerate(sd.query_devices()):
        if d[key] > 0 and d["hostapi"] == 0 and name_part.lower() in d["name"].lower():
            return i
    if fallback:
        log.warning("no %s named like %r: using the system default", "speaker" if output else "microphone", name_part)
        return None
    raise ValueError(f"找不到名字里有 {name_part!r} 的{'扬声器' if output else '麦克风'}")


def device_name(index: int | None, output: bool) -> str:
    if index is None:
        index = sd.default.device[1 if output else 0]
    return sd.query_devices(index)["name"]


def _names(devices, output: bool) -> list[str]:
    key = "max_output_channels" if output else "max_input_channels"
    names = [d["name"] for d in devices if d[key] > 0 and d["hostapi"] == 0 and "Microsoft" not in d["name"]]
    return list(dict.fromkeys(names))


def list_devices(output: bool) -> list[str]:
    """Names of the usable devices (MME host API), without the 'Microsoft 声音映射器' pseudo device, as this process's
    PortAudio saw them when it started (headphones connected since are missing: current_devices)."""
    return _names(sd.query_devices(), output)


_LIST_NOW = ("import json, sounddevice as sd; "
             "print(json.dumps([{k: d[k] for k in ('name', 'hostapi', 'max_input_channels', 'max_output_channels')} "
             "for d in sd.query_devices()]))")


def current_devices(output: bool) -> list[str]:
    """The devices there are now. PortAudio lists them once, when it starts, and restarting it here would cut the open
    microphone and speaker, so a short separate process lists them (about half a second). Falls back to this
    process's list when that fails."""
    import json
    import subprocess
    import sys
    try:
        exe = sys.executable.replace("pythonw.exe", "python.exe")
        out = subprocess.run([exe, "-I", "-c", _LIST_NOW], capture_output=True, text=True, encoding="utf-8", timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True).stdout
        return _names(json.loads(out), output)
    except Exception as e:
        log.warning("could not list the sound devices afresh (%s)", e)
        return list_devices(output)


def restart_portaudio() -> None:
    """Start PortAudio again so it sees the devices there are now. Every stream must be closed first: terminate would
    free them behind sounddevice's back."""
    sd.stop()
    if sd._initialized:
        sd._terminate()
    sd._initialize()


class Microphone:
    def __init__(self, name_part: str, on_chunk: Callable[[np.ndarray], None], fallback: bool = False):
        self.device = find_device(name_part, output=False, fallback=fallback)
        self.missing = name_part if name_part and self.device is None else ""  # the configured one was not there
        self.name = device_name(self.device, output=False)
        self.on_chunk = on_chunk
        self.stream: sd.InputStream | None = None

    def start(self):
        def cb(indata, frames, time_info, status):
            if status:
                log.warning("mic status: %s", status)
            self.on_chunk(indata[:, 0].copy())

        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                     blocksize=BLOCK, device=self.device, callback=cb)
        self.stream.start()
        log.info("mic: %s", self.name)

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None


class Endpointer:
    """Collects one utterance: starts at the first speech, ends after `silence_seconds` without speech. An utterance
    starts only on sound clearly above the room's noise floor (`level_ratio` times it, and at least `min_level`):
    a fan or a keyboard the detector takes for speech does not send a message. Once started, every block the
    detector calls speech counts, so quiet syllables and the tail of a short word are kept."""

    def __init__(self, silence_seconds: float, pre_roll: float = 1.0, min_speech: float = 0.3,
                 min_level: float = 0.003, level_ratio: float = 2.0):
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(VAD_MODEL)
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.2
        cfg.silero_vad.min_speech_duration = 0.1
        cfg.silero_vad.max_speech_duration = 60
        cfg.silero_vad.window_size = BLOCK
        cfg.sample_rate = SAMPLE_RATE
        self.vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=120)
        self.silence_blocks = int(silence_seconds * SAMPLE_RATE / BLOCK)
        self.pre_roll_blocks = int(pre_roll * SAMPLE_RATE / BLOCK)
        self.min_speech_blocks = int(min_speech * SAMPLE_RATE / BLOCK)
        self.min_level, self.level_ratio = min_level, level_ratio
        self.floor = min_level  # the room's level when nobody speaks, followed slowly
        self.reset()

    def reset(self):
        self.vad.reset()
        self.pre: list[np.ndarray] = []
        self.buf: list[np.ndarray] = []
        self.active = False
        self.speech_blocks = 0
        self.silent_blocks = 0

    @property
    def speech_seconds(self) -> float:
        return self.speech_blocks * BLOCK / SAMPLE_RATE

    def _speech(self, chunk: np.ndarray) -> bool:
        self.vad.accept_waveform(chunk)
        while not self.vad.empty():  # we do our own segmentation; keep the VAD's queue empty
            self.vad.pop()
        return self.vad.is_speech_detected()

    def _learn(self, level: float) -> None:
        """A block with nobody speaking: the room's level, followed down fast and up slowly."""
        self.floor += (level - self.floor) * (0.05 if level < self.floor else 0.01)

    def observe(self, chunk: np.ndarray) -> None:
        """A block heard while nobody is listening (the window idle): the room's level, so a fan that was on all
        along is known before the first listen. A bang moves the floor by one step only."""
        self._learn(float(np.sqrt(np.mean(chunk * chunk))) if len(chunk) else 0.0)

    def feed(self, chunk: np.ndarray) -> np.ndarray | None:
        speaking = self._speech(chunk)
        level = float(np.sqrt(np.mean(chunk * chunk))) if len(chunk) else 0.0
        if not speaking:
            self._learn(level)
        elif not self.active and level < max(self.min_level, self.floor * self.level_ratio):
            speaking = False  # the detector heard something, but not clearly above the room: no start
        if not self.active:
            self.pre.append(chunk)
            del self.pre[:-self.pre_roll_blocks]
            if speaking:
                self.active = True
                self.buf = list(self.pre)
                self.speech_blocks = 1
                self.silent_blocks = 0
            return None
        self.buf.append(chunk)
        if speaking:
            self.speech_blocks += 1
            self.silent_blocks = 0
            return None
        self.silent_blocks += 1
        if self.silent_blocks >= self.silence_blocks:
            return self.finish()
        return None

    def finish(self) -> np.ndarray | None:
        """End the current utterance now (used when the user taps to send)."""
        if not self.active:
            return None
        utt = np.concatenate(self.buf) if self.speech_blocks >= self.min_speech_blocks else None
        self.reset()
        return utt


class Player:
    """Plays float32 24 kHz audio from a queue on its own thread; stop() drops everything queued."""

    def __init__(self, name_part: str = "", fallback: bool = False):
        self.device = find_device(name_part, output=True, fallback=fallback)
        self.missing = name_part if name_part and self.device is None else ""  # the configured one was not there
        self.name = device_name(self.device, output=True)
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.current_gen = 0
        self._reopen = False
        self.ducked = False  # True while the user has started talking: speech keeps playing at 20 % volume
        self.progress_at = 0.0  # awake_clock() when the sound card last took a chunk (Speaker.stuck)
        self._in_flight: Callable[[], None] | None = None  # the done of the sentence being written
        self.thread = threading.Thread(target=self._run, daemon=True, name="player")
        self.running = True
        self.thread.start()

    def play(self, samples: np.ndarray, gen: int, done: Callable[[], None]):
        self.q.put((gen, samples, done))

    def stop(self, new_gen: int):
        with self.lock:
            self.current_gen = new_gen
            dropped = []
            while True:
                try:
                    dropped.append(self.q.get_nowait())
                except queue.Empty:
                    break
        for _, _, done in dropped:
            done()

    def duck(self, on: bool):
        """Lower (or restore) the volume of whatever is playing, without stopping it."""
        self.ducked = on

    def set_device(self, name_part: str):
        """Switch output device; takes effect from the next sentence."""
        self.device = find_device(name_part, output=True)
        self.name = device_name(self.device, output=True)
        self._reopen = True

    def close(self):
        self.running = False
        self.q.put((-1, np.zeros(1, np.float32), lambda: None))

    def abandon(self):
        """Give up on this player: the sound card stopped taking audio (a write that never returns). Everything
        queued, and the sentence stuck in the write, counts as done now; the thread ends if the write ever returns."""
        with self.lock:
            stuck, self._in_flight = self._in_flight, None
        self.stop(self.current_gen + 1)
        self.close()
        if stuck is not None:
            stuck()

    def _open(self):
        try:
            stream = sd.OutputStream(samplerate=TTS_RATE, channels=1, dtype="float32", latency="low",
                                     device=self.device)
            stream.start()
            log.info("speaker: %s", self.name)
            return stream
        except Exception:
            log.exception("cannot open audio output; nothing will be spoken")
            return None

    def _run(self):
        stream = self._open()
        while self.running:
            gen, samples, done = self.q.get()
            with self.lock:
                self._in_flight = done
            try:
                if self._reopen and stream is not None:
                    stream.close()
                    stream = None
                self._reopen = False
                if stream is None:
                    stream = self._open()  # e.g. the headset was off at startup or got unplugged
                if stream is not None and gen >= self.current_gen:
                    for i in range(0, len(samples), 2400):
                        if gen < self.current_gen:
                            break
                        chunk = samples[i:i + 2400]
                        stream.write(chunk * DUCK_GAIN if self.ducked else chunk)
                        self.progress_at = awake_clock()
            except Exception:
                log.exception("audio output failed; will reopen the device for the next sentence")
                try:
                    stream.close()
                except Exception:
                    pass
                stream = None
            finally:
                with self.lock:  # abandon() may have counted it already
                    mine, self._in_flight = self._in_flight, None
                if mine is not None:
                    mine()  # never lose the bookkeeping, or the app would wait for this sentence forever
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


OUTPUT_DEVICE: int | None = None  # set by the app so the UI sounds use the same speaker as speech


def tone(kind: str):
    """Short UI sounds, played asynchronously."""
    seqs = {"listen": [(660, 0.07), (880, 0.09)], "think": [(523, 0.08)], "stop": [(880, 0.07), (660, 0.09)],
            "error": [(330, 0.15), (330, 0.15)]}
    parts = []
    for freq, dur in seqs.get(kind, []):
        t = np.arange(int(TTS_RATE * dur)) / TTS_RATE
        env = np.minimum(1, np.minimum(t, dur - t) * 200)  # 5 ms fade in/out
        parts.append(0.15 * env * np.sin(2 * np.pi * freq * t))
    if parts:
        try:
            sd.play(np.concatenate(parts).astype(np.float32), samplerate=TTS_RATE, device=OUTPUT_DEVICE)
        except Exception:
            log.exception("tone failed")
