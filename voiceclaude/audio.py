"""Microphone capture, VAD-based end-of-utterance detection, and audio playback."""
from __future__ import annotations

import logging
import queue
import threading
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


def find_device(name_part: str, output: bool) -> int | None:
    """Index of the first device whose name contains `name_part` (MME host API); None = system default."""
    if not name_part:
        return None
    key = "max_output_channels" if output else "max_input_channels"
    for i, d in enumerate(sd.query_devices()):
        if d[key] > 0 and d["hostapi"] == 0 and name_part.lower() in d["name"].lower():
            return i
    raise ValueError(f"找不到名字里有 {name_part!r} 的{'扬声器' if output else '麦克风'}")


def device_name(index: int | None, output: bool) -> str:
    if index is None:
        index = sd.default.device[1 if output else 0]
    return sd.query_devices(index)["name"]


def list_devices(output: bool) -> list[str]:
    """Names of the usable devices (MME host API), without the 'Microsoft 声音映射器' pseudo device."""
    key = "max_output_channels" if output else "max_input_channels"
    names = [d["name"] for d in sd.query_devices() if d[key] > 0 and d["hostapi"] == 0 and "Microsoft" not in d["name"]]
    return list(dict.fromkeys(names))


class Microphone:
    def __init__(self, name_part: str, on_chunk: Callable[[np.ndarray], None]):
        self.device = find_device(name_part, output=False)
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
    """Collects one utterance: starts at the first speech, ends after `silence_seconds` without speech."""

    def __init__(self, silence_seconds: float, pre_roll: float = 1.0, min_speech: float = 0.3):
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

    def feed(self, chunk: np.ndarray) -> np.ndarray | None:
        self.vad.accept_waveform(chunk)
        while not self.vad.empty():  # we do our own segmentation; keep the VAD's queue empty
            self.vad.pop()
        speaking = self.vad.is_speech_detected()
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

    def __init__(self, name_part: str = ""):
        self.device = find_device(name_part, output=True)
        self.name = device_name(self.device, output=True)
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.current_gen = 0
        self._reopen = False
        self.ducked = False  # True while the user has started talking: speech keeps playing at 20 % volume
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
            except Exception:
                log.exception("audio output failed; will reopen the device for the next sentence")
                try:
                    stream.close()
                except Exception:
                    pass
                stream = None
            finally:
                done()  # never lose the bookkeeping, or the app would wait for this sentence forever


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
