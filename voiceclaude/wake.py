"""The wake word ("hi chat" unless config.toml says otherwise): sherpa-onnx's keyword spotter on the small bilingual
zh-en model, fed from the microphone in 100 ms chunks, about 1 % of one CPU core. Used by the standby listener
(standby.py) and by the window while it is idle."""
from __future__ import annotations

import itertools
import logging
import re
import tempfile
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger(__name__)

KWS_DIR = config.MODELS / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
_FILES = {"tokens": "tokens.txt", "encoder": "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
          "decoder": "decoder-epoch-13-avg-2-chunk-16-left-64.onnx", "joiner": "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
          "lexicon": "en.phone"}
DEFAULT_KEYWORD = "hi chat"
DEFAULT_THRESHOLD = 0.15  # tried on synthetic speech: every English-voiced "hi chat" heard, one false alarm ("Hi Chad") in 64
# Ways people say the default that the lexicon does not list.
EXTRA = {"HI CHAT": ["HH AY1 CH AA1 T", "HH AY1 CH EH1 T", "HH AA1 Y CH AE1 T", "HH AY1 CH AE1 T AH0"]}
_CJK = re.compile(r"[一-鿿]+")


class WakeWordError(ValueError):
    """The wake word cannot be spotted (a word the model does not know, or Chinese without pypinyin)."""


def model_ready(root: Path | None = None) -> bool:
    d = root or KWS_DIR
    return all((d / f).exists() for f in _FILES.values())


def _lexicon(path: Path) -> dict[str, list[list[str]]]:
    table: dict[str, list[list[str]]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                word = re.sub(r"\(\d+\)$", "", parts[0]).upper()  # "WORD(2)": a second pronunciation
                table.setdefault(word, []).append(parts[1:])
    return table


def _pinyin_tokens(text: str) -> list[str]:
    """Chinese characters -> initials and toned finals, as the model's tokens.txt has them."""
    try:
        from pypinyin import pinyin
        from pypinyin.contrib.tone_convert import to_finals_tone, to_initials
    except ImportError as e:
        raise WakeWordError("中文唤醒词要先安装 pypinyin") from e
    out = []
    for (py,) in pinyin(text):
        initial, final = to_initials(py, strict=False), to_finals_tone(py, strict=False)
        out += [t for t in (initial, final) if t] or [py]
    return out


def keyword_lines(keyword: str, threshold: float = DEFAULT_THRESHOLD, root: Path | None = None) -> list[str]:
    """The spotter's keyword lines for one wake word: every pronunciation, 'TOKENS :score #threshold @NAME'."""
    d = root or KWS_DIR
    words = re.findall(r"[A-Za-z']+|[一-鿿]+", keyword)
    if not words:
        raise WakeWordError(f"唤醒词「{keyword}」里没有字")
    lexicon = _lexicon(d / _FILES["lexicon"]) if any(not _CJK.fullmatch(w) for w in words) else {}
    choices = []
    for w in words:
        if _CJK.fullmatch(w):
            choices.append([_pinyin_tokens(w)])
        elif w.upper() in lexicon:
            choices.append(lexicon[w.upper()][:3])
        else:
            raise WakeWordError(f"唤醒词里的「{w}」模型不认识")
    sequences = [" ".join(sum(combo, [])) for combo in itertools.islice(itertools.product(*choices), 8)]
    sequences += EXTRA.get(" ".join(w.upper() for w in words), [])
    known = {line.split()[0] for line in (d / _FILES["tokens"]).read_text("utf-8").splitlines() if line.strip()}
    sequences = [s for s in dict.fromkeys(sequences) if all(t in known for t in s.split())]
    if not sequences:
        raise WakeWordError(f"唤醒词「{keyword}」模型拼不出来")
    name = "_".join(w.upper() for w in words)
    return [f"{s} :1.0 #{threshold} @{name}" for s in sequences]


class Spotter:
    """feed(chunk) -> True when the wake word was just heard. 16 kHz float32 mono, any chunk size."""

    def __init__(self, keyword: str = DEFAULT_KEYWORD, threshold: float = DEFAULT_THRESHOLD, root: Path | None = None):
        import sherpa_onnx
        d = root or KWS_DIR
        self.keyword = keyword
        lines = keyword_lines(keyword, threshold, d)
        kw = Path(tempfile.gettempdir()) / f"voice-claude-wake-{abs(hash((keyword, threshold))) % 10**8}.txt"
        kw.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.ks = sherpa_onnx.KeywordSpotter(
            tokens=str(d / _FILES["tokens"]), encoder=str(d / _FILES["encoder"]), decoder=str(d / _FILES["decoder"]),
            joiner=str(d / _FILES["joiner"]), keywords_file=str(kw), num_threads=1, keywords_threshold=threshold)
        self.stream = self.ks.create_stream()

    def feed(self, chunk: np.ndarray, sample_rate: int = 16000) -> bool:
        self.stream.accept_waveform(sample_rate, np.asarray(chunk, dtype=np.float32))
        heard = False
        while self.ks.is_ready(self.stream):
            self.ks.decode_stream(self.stream)
            if self.ks.get_result(self.stream):
                heard = True
                self.ks.reset_stream(self.stream)
        return heard

    def reset(self) -> None:
        self.stream = self.ks.create_stream()
