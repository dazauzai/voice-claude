import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from voiceclaude import projects


def make_project(store: Path, slug: str, cwd: str, memory: str = "", age_days: float = 0, with_cwd_field: bool = True):
    d = store / slug
    d.mkdir(parents=True)
    rec = {"type": "user", "cwd": cwd} if with_cwd_field else {"type": "user"}
    f = d / "11111111-2222-3333-4444-555555555555.jsonl"
    f.write_text(json.dumps(rec) + "\n", "utf-8")
    t = time.time() - age_days * 86400
    os.utime(f, (t, t))
    if memory:
        (d / "memory").mkdir()
        (d / "memory" / "MEMORY.md").write_text(memory, "utf-8")
    return d


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = self.tmp / "projects"
        self.work = self.tmp / "work"
        (self.work / "c" / "foo").mkdir(parents=True)
        (self.work / "d" / "foo").mkdir(parents=True)
        (self.work / "c" / "bar-baz").mkdir(parents=True)
        self.foo_c = str(self.work / "c" / "foo")
        self.foo_d = str(self.work / "d" / "foo")
        self.bar = str(self.work / "c" / "bar-baz")
        make_project(self.store, "X--c-foo", self.foo_c, "- [Release notes](r.md) — draft for the next version\n", age_days=1)
        make_project(self.store, "X--d-foo", self.foo_d, "# Memory index\n\n- [Figures build](f.md) — weekly report charts\n", age_days=3)
        make_project(self.store, "X--c-bar-baz", self.bar, "", age_days=0)
        make_project(self.store, "C--Users-u-AppData-Local-Temp-claude-scratch-workspaces-abc", str(self.work), "")
        make_project(self.store, "X--gone", str(self.tmp / "gone"), "")
        (self.store / "empty-no-jsonl").mkdir()
        self.aliases = self.tmp / "projects.toml"
        self.lines = self.tmp / "lines.json"

    def scan(self):
        return projects.scan(self.store, self.aliases, self.lines)

    def test_saved_conversations_know_their_engine(self):
        """lines.json says whose conversation an id is; an older record without it is told by the id itself."""
        self.assertEqual(projects.engine_of_session("01a166ba-23fd-7603-bdf4-1df493071b07"), "codex")
        self.assertEqual(projects.engine_of_session("3f2b6c1e-8d4a-4b7e-9c21-5a6d7e8f9012"), "claude")
        self.assertEqual(projects.engine_of_session("old-atlas"), "")
        import json
        self.lines.write_text(json.dumps({projects.norm_key(self.foo_c): {"session_id": "01a166ba-23fd-7603-bdf4-1df493071b07"},
                                          projects.norm_key(self.foo_d): {"session_id": "3f2b6c1e-8d4a-4b7e-9c21-5a6d7e8f9012", "engine": "codex"}}), "utf-8")
        ps = {p.cwd: p for p in self.scan()}
        self.assertEqual((ps[self.foo_c].session_id, ps[self.foo_c].session_engine), ("01a166ba-23fd-7603-bdf4-1df493071b07", "codex"))
        self.assertEqual(ps[self.foo_d].session_engine, "codex")  # what was stored wins

    def test_scan_basic(self):
        ps = self.scan()
        self.assertEqual([p.name for p in ps], ["bar-baz", "foo", "foo"])  # newest first
        foo_c = next(p for p in ps if p.cwd == self.foo_c)
        self.assertEqual(foo_c.topic, "Release notes — draft for the next version")
        foo_d = next(p for p in ps if p.cwd == self.foo_d)
        self.assertEqual(foo_d.topic, "Figures build — weekly report charts")
        self.assertNotEqual(foo_c.display_name, foo_d.display_name)
        self.assertEqual(next(p for p in ps if p.name == "bar-baz").display_name, "bar-baz")

    def test_duplicate_names(self):
        ps = self.scan()
        self.assertEqual(len(projects.resolve("foo", ps)), 2)
        self.assertEqual(len(projects.resolve("Foo。", ps)), 2)

    def test_alias_wins(self):
        projects.remember_alias(self.aliases, "服务器", self.foo_c)
        ps = self.scan()
        hits = projects.resolve("服务器", ps)
        self.assertEqual([p.cwd for p in hits], [self.foo_c])
        self.assertEqual(hits[0].aliases, ["服务器"])
        self.assertEqual(projects.load_aliases(self.aliases), {"服务器": self.foo_c})

    def test_chinese_numbers(self):
        self.assertEqual(projects._norm("Beacon 十七"), "beacon17")
        self.assertEqual(projects._norm("Beacon-17"), "beacon17")
        self.assertEqual(projects._norm("二十三号"), "23号")
        self.assertEqual(projects._norm("一七"), "17")
        (self.work / "c" / "Beacon-17").mkdir()
        make_project(self.store, "X--c-Beacon-17", str(self.work / "c" / "Beacon-17"))
        self.assertEqual([p.name for p in projects.resolve("接我到 Beacon 十七", self.scan())], ["Beacon-17"])

    def test_resolve_by_fragment_and_topic(self):
        ps = self.scan()
        self.assertEqual([p.name for p in projects.resolve("接我到 bar baz 那个", ps)], ["bar-baz"])
        self.assertEqual([p.cwd for p in projects.resolve("release", ps)], [self.foo_c])
        self.assertEqual(projects.resolve("不存在的", ps), [])
        self.assertEqual(projects.resolve("", ps), [])

    def test_skips_scratch_and_missing(self):
        cwds = {p.cwd for p in self.scan()}
        self.assertNotIn(str(self.work), cwds)
        self.assertNotIn(str(self.tmp / "gone"), cwds)

    def test_session_id_from_lines_file(self):
        self.lines.write_text(json.dumps({projects.norm_key(self.bar): {"session_id": "s-1"}}), "utf-8")
        ps = self.scan()
        self.assertEqual(next(p for p in ps if p.name == "bar-baz").session_id, "s-1")
        self.assertEqual(next(p for p in ps if p.cwd == self.foo_c).session_id, "")

    def test_scratch_cwd_in_transcript_is_ignored(self):
        d = make_project(self.store, "X--c-bar-baz-2", "ignored")
        (d / "11111111-2222-3333-4444-555555555555.jsonl").write_text(
            '{"type":"user","cwd":"C:\\\\x\\\\scratch-workspaces\\\\scratch-1"}\n', "utf-8")
        cwds = {p.cwd for p in self.scan()}
        self.assertNotIn(r"C:\x\scratch-workspaces\scratch-1", cwds)

    def test_index_text(self):
        projects.remember_alias(self.aliases, "服务器", self.foo_c)
        text = projects.index_text(self.scan(), now=time.time())
        self.assertIn("bar-baz", text)
        self.assertIn("别名 服务器", text)
        self.assertIn("最近 1 天前", text)
        self.assertIn("Release notes", text)


class SlugTests(unittest.TestCase):
    def test_cwd_from_slug(self):
        existing = {r"C:\Users\u\my-proj", r"E:\Atlas", r"F:\work\Beacon",
                    r"E:\2_my_notes old copy"}
        prefixes = {p[:i] for p in existing for i in range(len(p) + 1) if p[:i].endswith("\\") or p[:i] == p}
        prefixes |= {p.rsplit("\\", 1)[0] for p in existing} | {p[:3] for p in existing}

        def exists(path):
            return path in existing or path.rstrip("\\") in {q.rstrip("\\") for q in prefixes} or path in {r"F:\work", r"C:\Users", r"C:\Users\u"}
        self.assertEqual(projects.cwd_from_slug("C--Users-u-my-proj", exists=exists), r"C:\Users\u\my-proj")
        self.assertEqual(projects.cwd_from_slug("E--Atlas", exists=exists), r"E:\Atlas")
        self.assertEqual(projects.cwd_from_slug("F--work-Beacon", exists=exists), r"F:\work\Beacon")
        self.assertEqual(projects.cwd_from_slug("E--2-my-notes-old-copy", exists=exists),
                         r"E:\2_my_notes old copy")
        self.assertEqual(projects.cwd_from_slug("E--nope", exists=lambda p: False), "")
        self.assertEqual(projects.cwd_from_slug("weird", exists=lambda p: True), "")


    def test_cwd_from_transcript_fallback(self):
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "s.jsonl"
        f.write_text('not json\n{"type":"summary"}\n{"type":"user","cwd":"D:\\\\Atlas"}\n', "utf-8")
        self.assertEqual(projects.cwd_from_transcript(f), "D:\\Atlas")
        f.write_text('{"type":"summary"}\n', "utf-8")
        self.assertEqual(projects.cwd_from_transcript(f), "")


if __name__ == "__main__":
    unittest.main()
