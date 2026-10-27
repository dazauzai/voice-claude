"""The whiteboard page loads Excalidraw from voiceclaude/whiteboard/vendor/ (offline). Every module the vendor manifest
lists must exist, and no .gitignore rule may leave one out: one did (dist/), the repo lacked 78 of them, and every
copy made from git showed an empty board window."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "voiceclaude" / "whiteboard" / "vendor"


class VendorTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((VENDOR / "manifest.json").read_text("utf-8"))
        self.needed = [self.manifest["entry"], *self.manifest["modules"]]

    def test_every_module_exists(self):
        missing = [m for m in self.needed if not (VENDOR / m).is_file()]
        self.assertEqual(missing, [])

    def test_no_module_is_ignored_by_git(self):
        if not (ROOT / ".git").exists() or not shutil.which("git"):
            self.skipTest("not a git checkout")
        paths = ["voiceclaude/whiteboard/vendor/" + m for m in self.needed]
        r = subprocess.run(["git", "-c", "core.quotepath=off", "check-ignore", "--no-index", "--stdin"], cwd=ROOT,
                           input="\n".join(paths), capture_output=True, text=True, encoding="utf-8")
        ignored = [line for line in r.stdout.splitlines() if line.strip()]
        self.assertEqual(ignored, [], "ignored by .gitignore, so copies made from git lack them")


if __name__ == "__main__":
    unittest.main()
