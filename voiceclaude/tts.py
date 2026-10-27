"""Text to speech with Kokoro (sherpa-onnx, CPU). Sentences are synthesised one by one and queued for playback."""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

import numpy as np
import sherpa_onnx

from .audio import Player, awake_clock
from .config import TTS_DIR

log = logging.getLogger(__name__)


def load_tts(num_threads: int = 4) -> sherpa_onnx.OfflineTts:
    k = TTS_DIR
    return sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(k / "model.onnx"), voices=str(k / "voices.bin"), tokens=str(k / "tokens.txt"),
                lexicon=f"{k / 'lexicon-us-en.txt'},{k / 'lexicon-zh.txt'}",
                data_dir=str(k / "espeak-ng-data"), dict_dir=str(k / "dict")),
            num_threads=num_threads),
        rule_fsts=f"{k / 'phone-zh.fst'},{k / 'date-zh.fst'},{k / 'number-zh.fst'}",
        max_num_sentences=1))


class Speaker:
    def __init__(self, voice: int, speed: float, player: Player, on_idle: Callable[[], None]):
        self.tts = load_tts()
        self.voice, self.speed = voice, speed
        self.player = player
        self.on_idle = on_idle
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.gen = 0
        self.pending = 0
        self._moved_at = 0.0  # a sentence was asked for from silence, synthesised, or finished
        threading.Thread(target=self._run, daemon=True, name="tts").start()

    def say(self, text: str):
        with self.lock:
            if self.pending == 0:
                self._moved_at = awake_clock()
            self.pending += 1
            self.q.put((self.gen, text))

    def stop(self):
        """Drop everything not yet spoken and cut off the current sentence."""
        with self.lock:
            self.gen += 1
            gen = self.gen
            dropped = 0
            while True:
                try:
                    self.q.get_nowait()
                    dropped += 1
                except queue.Empty:
                    break
        for _ in range(dropped):
            self._done()
        self.player.stop(gen)

    def idle(self) -> bool:
        with self.lock:
            return self.pending == 0

    def stuck(self, after: float = 60.0) -> bool:
        """Something is waiting to be read out, but neither synthesis nor the sound card has moved for `after`
        awake seconds (time asleep does not count): PortAudio's MME write can wait forever (e.g. after the PC slept),
        and then idle() never comes."""
        with self.lock:
            if self.pending == 0:
                return False
            moved, player = self._moved_at, self.player
        return awake_clock() - max(moved, player.progress_at) > after

    def replace_player(self, player: Player):
        """Read out through `player` from now on. The old one is stuck in a write and is given up: what it held
        counts as done."""
        with self.lock:
            old, self.player = self.player, player
        old.abandon()

    def _done(self):
        with self.lock:
            self.pending -= 1
            self._moved_at = awake_clock()
            idle = self.pending == 0
        if idle:
            self.on_idle()

    def _run(self):
        while True:
            gen, text = self.q.get()
            if gen != self.gen:
                self._done()
                continue
            try:
                audio = self.tts.generate(text, sid=self.voice, speed=self.speed)
                samples = np.asarray(audio.samples, dtype=np.float32)
            except Exception:
                log.exception("tts failed for %r", text)
                self._done()
                continue
            if gen != self.gen or len(samples) == 0:
                self._done()
                continue
            with self.lock:  # chosen and queued under the lock: replace_player() sees it in one player or the other
                self._moved_at = awake_clock()
                self.player.play(samples, gen, self._done)
