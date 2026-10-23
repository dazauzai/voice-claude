"""Download the speech models (about 640 MB) into models/.

Sources: the sherpa-onnx GitHub releases (archives), or the same files on Hugging Face, directly or through the
hf-mirror.com mirror that is fast in mainland China. The Hugging Face copies of the two big models were compared
file by file with the GitHub archives (2026-10-23: same 10 + 377 files, same sizes). The small Silero VAD file
(630 KB) only exists in this exact version on GitHub; when GitHub cannot be reached, an older Silero VAD from
Hugging Face is used instead (sherpa-onnx reads both).

    .venv\\Scripts\\python scripts\\download_models.py [--source auto|github|hf|hf-mirror] [--yes] [--dest DIR]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
GITHUB = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
HOSTS = {"hf": "https://huggingface.co", "hf-mirror": "https://hf-mirror.com"}

ASR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
TTS = "kokoro-multi-lang-v1_1"
VAD = "silero_vad.onnx"
# target in models/ -> (GitHub release path, Hugging Face repo, files to skip there)
MODEL_SOURCES = {
    ASR: ("asr-models/" + ASR + ".tar.bz2", "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
          {".gitattributes", "model.onnx"}),  # model.onnx is the 900 MB float version; we use model.int8.onnx
    TTS: ("tts-models/" + TTS + ".tar.bz2", "csukuangfj/kokoro-multi-lang-v1_1", {".gitattributes"}),
    VAD: ("asr-models/" + VAD, "csukuangfj/vad", None),  # None: only the one file below, from Hugging Face
}
TIMEOUT = 60
TRIES = 3

LICENSES = [
    ("SenseVoice-Small（语音识别，FunAudioLLM / FunASR）", "FunASR Model License v1.1：可用、可改、可分享，要求署名并保留模型名；写明“仅供参考学习”",
     "https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE"),
    ("Silero VAD（判断说完了）", "MIT", "https://github.com/snakers4/silero-vad/blob/master/LICENSE"),
    ("Kokoro-82M 多语言 v1.1（朗读，hexgrad；含 espeak-ng 数据和 cppjieba 词典）", "Apache-2.0（espeak-ng-data 为 GPL-3.0，cppjieba 词典为 MIT）",
     "https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh"),
]


def show_licenses(ask: bool = True) -> None:
    """These models are not ours: print their licenses and let the user agree before downloading."""
    print("要下载的语音模型不属于 voice-claude，各有自己的许可证：")
    for name, lic, url in LICENSES:
        print(f"  - {name}\n      {lic}\n      {url}")
    print("详见仓库里的 NOTICE.md。")
    if ask:
        try:
            input("按回车继续下载（Ctrl+C 取消）…")
        except EOFError:
            pass


# ---------- plumbing ----------

def reachable(url: str, timeout: float = 8.0) -> float | None:
    """Seconds to get any HTTP answer from url, or None when it cannot be reached."""
    t = time.monotonic()
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=timeout).close()
    except urllib.error.HTTPError:
        pass  # an HTTP error still means the host answered
    except Exception:
        return None
    return time.monotonic() - t


def pick_source() -> str:
    """auto: GitHub when it answers quickly, else the Hugging Face mirror, else Hugging Face itself."""
    gh = reachable("https://github.com/k2-fsa/sherpa-onnx/releases")
    if gh is not None and gh < 3:
        return "github"
    for name in ("hf-mirror", "hf"):
        if reachable(HOSTS[name] + "/api/models/csukuangfj/vad") is not None:
            return name
    return "github"


def fetch(url: str, dest: Path, size: int | None = None, show: bool = True) -> None:
    """Download url to dest (through dest.part), a few tries; checks the size when it is known."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last = None
    for attempt in range(1, TRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r, open(part, "wb") as f:
                total = size or int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if show and total:
                        sys.stdout.write(f"\r  {done * 100 // total:3d}%  {done >> 20} / {total >> 20} MB")
                        sys.stdout.flush()
            if show and total:
                print()
            if size is not None and part.stat().st_size != size:
                raise OSError(f"大小不对：应为 {size} 字节，收到 {part.stat().st_size}")
            part.replace(dest)
            return
        except Exception as e:
            last = e
            if show:
                print(f"\n  第 {attempt} 次没下完（{e}）" + ("，再试一次…" if attempt < TRIES else ""))
            time.sleep(2 * attempt)
    part.unlink(missing_ok=True)
    raise OSError(f"下载 {url} 失败：{last}")


def hf_files(host: str, repo: str) -> list[tuple[str, int]]:
    url = f"{host}/api/models/{repo}/tree/main?recursive=true"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        tree = json.load(r)
    return [(x["path"], int(x.get("size") or 0)) for x in tree if x.get("type") == "file"]


# ---------- the three models ----------

NAMES = {ASR: "语音识别", VAD: "断句", TTS: "朗读"}


def from_github(target: str, dest_root: Path) -> None:
    rel = MODEL_SOURCES[target][0]
    url = f"{GITHUB}/{rel}"
    print(f"  {NAMES.get(target, target)}")
    tmp = dest_root / Path(rel).name
    if tmp.suffix == ".bz2":
        fetch(url, tmp)
        part = dest_root / (target + ".extract")  # a stopped extraction must not look like a finished model
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir(parents=True)
        with tarfile.open(tmp, "r:bz2") as tar:
            safe_extract(tar, part)
        (part / target).replace(dest_root / target)  # the archive's top folder is the model's name
        shutil.rmtree(part, ignore_errors=True)
        tmp.unlink()
    else:
        fetch(url, dest_root / target)


def safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """extractall without leaving dest; the filter= argument only exists from Python 3.11.4."""
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, filter="data")
        return
    root = dest.resolve()
    for m in tar.getmembers():
        if m.issym() or m.islnk() or not (dest / m.name).resolve().is_relative_to(root):
            raise OSError(f"压缩包里有不安全的路径：{m.name}")
    tar.extractall(dest)


def from_hf(target: str, dest_root: Path, host: str) -> None:
    _, repo, skip = MODEL_SOURCES[target]
    if skip is None:  # the VAD: one file
        files = [(p, s) for p, s in hf_files(host, repo) if p == VAD]
        print(f"  {NAMES[VAD]}（备用来源）")
        fetch(f"{host}/{repo}/resolve/main/{VAD}", dest_root / target, files[0][1] if files else None)
        return
    files = [(p, s) for p, s in hf_files(host, repo) if p not in skip]
    total = sum(s for _, s in files)
    print(f"  {NAMES.get(target, target)}")
    tmpdir = dest_root / (target + ".part")
    done = 0
    for i, (path, size) in enumerate(files, 1):
        out = tmpdir / path
        if not (out.exists() and out.stat().st_size == size):
            fetch(f"{host}/{repo}/resolve/main/{path}", out, size, show=size > (8 << 20))
        done += size
        sys.stdout.write(f"\r  {i}/{len(files)} 个文件，{done >> 20} / {total >> 20} MB")
        sys.stdout.flush()
    print()
    tmpdir.replace(dest_root / target)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="下载语音模型（约 640 MB）")
    ap.add_argument("--source", choices=["auto", "github", "hf", "hf-mirror"], default="auto")
    ap.add_argument("--yes", action="store_true", help="不显示许可证、不停下来（许可证见 NOTICE.md）")
    ap.add_argument("--dest", default=str(MODELS))
    ap.add_argument("--only", nargs="*", default=None, help=argparse.SUPPRESS)  # tests: some of the three models
    a = ap.parse_args(argv)
    dest_root = Path(a.dest)
    dest_root.mkdir(parents=True, exist_ok=True)
    todo = [t for t in MODEL_SOURCES if not (dest_root / t).exists() and (a.only is None or t in a.only)]
    if not todo:
        print("  已有，跳过。")
        return 0
    if not a.yes:  # run by hand: the licenses once (install.bat passes --yes; they are in NOTICE.md)
        show_licenses(ask=True)
    source = pick_source() if a.source == "auto" else a.source
    for target in todo:
        try:
            if source == "github":
                try:
                    from_github(target, dest_root)
                except OSError:
                    print("  GitHub 连不上，改用国内镜像")
                    from_hf(target, dest_root, HOSTS["hf-mirror"])
            elif target == VAD:  # the exact version lives only on GitHub; try it first, it is small
                try:
                    from_github(target, dest_root)
                except OSError:
                    from_hf(target, dest_root, HOSTS[source])
            else:
                from_hf(target, dest_root, HOSTS[source])
        except OSError as e:
            print(f"没下载成功：{target}\n  原因：{e}\n  网络好一点时再运行一次就行，已下好的不会重下。")
            return 1
    print("  完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
