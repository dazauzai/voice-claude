"""scripts/download_models.py without the network: source choice, the Hugging Face file-by-file path (skipped files,
sizes, the .part folder), the GitHub path, and the Silero VAD fallback when GitHub is unreachable."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("download_models", ROOT / "scripts" / "download_models.py")
dm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dm)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.dest = Path(tempfile.mkdtemp())
        self.fetched = []

        def fake_fetch(url, dest, size=None, show=True):
            self.fetched.append(url)
            if "unreachable" in getattr(self, "mode", "") and "github.com" in url:
                raise OSError("connection reset")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x" * (size or 3))
        trees = {
            dm.MODEL_SOURCES[dm.ASR][1]: [(".gitattributes", 1), ("model.onnx", 900), ("model.int8.onnx", 230), ("tokens.txt", 5)],
            dm.MODEL_SOURCES[dm.TTS][1]: [(".gitattributes", 1), ("model.onnx", 300), ("espeak-ng-data/en_dict", 7)],
            dm.MODEL_SOURCES[dm.VAD][1]: [("silero_vad.onnx", 11), ("Obama.mov", 999)],
        }
        for p in (mock.patch.object(dm, "fetch", fake_fetch),
                  mock.patch.object(dm, "hf_files", lambda host, repo: trees[repo])):
            p.start()
            self.addCleanup(p.stop)

    def test_hugging_face_mirror(self):
        rc = dm.main(["--source", "hf-mirror", "--yes", "--dest", str(self.dest)])
        self.assertEqual(rc, 0)
        asr = self.dest / dm.ASR
        self.assertTrue((asr / "model.int8.onnx").exists())
        self.assertFalse((asr / "model.onnx").exists())  # the 900 MB float model is not needed
        self.assertFalse((asr / ".gitattributes").exists())
        self.assertTrue((self.dest / dm.TTS / "espeak-ng-data" / "en_dict").exists())
        self.assertFalse((self.dest / (dm.TTS + ".part")).exists())  # moved into place when complete
        self.assertTrue(all(u.startswith("https://hf-mirror.com/") or "github.com" in u for u in self.fetched))
        self.assertTrue(any("github.com" in u and dm.VAD in u for u in self.fetched))  # the exact VAD comes from GitHub
        self.assertFalse(any("Obama" in u for u in self.fetched))

    def test_vad_falls_back_when_github_is_unreachable(self):
        self.mode = "unreachable"
        rc = dm.main(["--source", "hf-mirror", "--yes", "--dest", str(self.dest), "--only", dm.VAD])
        self.assertEqual(rc, 0)
        self.assertTrue((self.dest / dm.VAD).exists())
        self.assertTrue(any(u.startswith("https://hf-mirror.com/csukuangfj/vad/resolve/main/silero_vad.onnx") for u in self.fetched))

    def make_archive(self, top: str, bad: bool = False) -> bytes:
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
            for name in ([f"{top}/tokens.txt", f"{top}/model.int8.onnx"] if not bad else ["../escape.txt"]):
                data = b"x" * 5
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def test_github_archive_is_moved_into_place_only_when_complete(self):
        archive = self.make_archive(dm.ASR)
        with mock.patch.object(dm, "fetch", lambda url, dest, size=None, show=True: dest.write_bytes(archive)):
            dm.from_github(dm.ASR, self.dest)
        self.assertTrue((self.dest / dm.ASR / "tokens.txt").exists())
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), [dm.ASR])  # no archive, no .extract folder left

    def test_a_stopped_extraction_does_not_count_as_downloaded(self):
        archive = self.make_archive(dm.ASR)
        boom = mock.patch.object(dm, "safe_extract", side_effect=KeyboardInterrupt)
        with mock.patch.object(dm, "fetch", lambda url, dest, size=None, show=True: dest.write_bytes(archive)), boom:
            with self.assertRaises(KeyboardInterrupt):
                dm.from_github(dm.ASR, self.dest)
        self.assertFalse((self.dest / dm.ASR).exists())  # the next run downloads it again

    def test_unsafe_member_paths_are_refused_without_the_tarfile_filter(self):
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(self.make_archive("x", bad=True)), mode="r:bz2") as tar,                 mock.patch.object(dm.tarfile, "data_filter", None, create=True):
            delattr(dm.tarfile, "data_filter")
            with self.assertRaises(OSError):
                dm.safe_extract(tar, self.dest)

    def test_nothing_to_do(self):
        for t in dm.MODEL_SOURCES:
            (self.dest / t).mkdir()
        self.assertEqual(dm.main(["--yes", "--dest", str(self.dest)]), 0)
        self.assertEqual(self.fetched, [])

    def test_auto_source(self):
        with mock.patch.object(dm, "reachable", lambda url, timeout=8.0: 0.5):
            self.assertEqual(dm.pick_source(), "github")
        with mock.patch.object(dm, "reachable", lambda url, timeout=8.0: None if "github" in url else 0.3):
            self.assertEqual(dm.pick_source(), "hf-mirror")


class InstallerTests(unittest.TestCase):
    def test_index_choice(self):
        spec2 = importlib.util.spec_from_file_location("install", ROOT / "scripts" / "install.py")
        inst = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(inst)
        self.assertEqual(inst.pick_index("mirror"), inst.MIRROR)
        self.assertEqual(inst.pick_index("pypi"), inst.PYPI)
        with mock.patch.object(inst, "answers_in", lambda url, timeout=6.0: 0.4):
            self.assertEqual(inst.pick_index(None), inst.PYPI)
        with mock.patch.object(inst, "answers_in", lambda url, timeout=6.0: None if "pypi.org" in url else 0.4):
            self.assertEqual(inst.pick_index(None), inst.MIRROR)
        with mock.patch.object(inst, "answers_in", lambda url, timeout=6.0: 4.0 if "pypi.org" in url else 0.4):
            self.assertEqual(inst.pick_index(None), inst.MIRROR)  # PyPI answers, but too slowly


if __name__ == "__main__":
    unittest.main()
