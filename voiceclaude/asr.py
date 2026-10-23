"""Speech recognition with SenseVoice (sherpa-onnx, CPU, Mandarin + English in one sentence)."""
from __future__ import annotations

import logging
import re

import numpy as np
import sherpa_onnx

from .audio import SAMPLE_RATE
from .config import ASR_DIR

log = logging.getLogger(__name__)


class Recognizer:
    def __init__(self, replacements: dict[str, str], num_threads: int = 8):
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(ASR_DIR / "model.int8.onnx"), tokens=str(ASR_DIR / "tokens.txt"),
            num_threads=num_threads, use_itn=True, language="auto")
        self.replacements = [(re.compile(re.escape(k), re.I), v) for k, v in replacements.items()]

    def transcribe(self, samples: np.ndarray) -> str:
        stream = self.rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self.rec.decode_stream(stream)
        text = stream.result.text.strip()
        for pattern, repl in self.replacements:
            text = pattern.sub(repl, text)
        return text
