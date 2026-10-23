import os
import unittest

from voiceclaude import risk


def no(_):
    return False


def yes(_):
    return True


class RiskTests(unittest.TestCase):
    """What 通透模式 still asks about: deleting, and overwriting a file that already exists. Errs toward asking."""

    def test_desktop_keys_clicks_and_typing(self):
        self.assertEqual(risk.classify("hotkey", facts={"kind": "hotkey", "keys": ["delete"]})[0], risk.DELETE)
        self.assertEqual(risk.classify("hotkey", facts={"kind": "hotkey", "keys": ["shift", "delete"]})[0], risk.DELETE)
        self.assertEqual(risk.classify("hotkey", facts={"kind": "hotkey", "keys": ["ctrl", "d"]})[0], risk.DELETE)
        self.assertIsNone(risk.classify("hotkey", facts={"kind": "hotkey", "keys": ["ctrl", "s"]})[0])
        for keys in (["ctrlleft", "d"], ["d", "ctrlright"], ["shiftright", "del"]):  # left / right names, any order
            self.assertEqual(risk.classify("hotkey", facts={"kind": "hotkey", "keys": keys})[0], risk.DELETE, keys)
        self.assertIsNone(risk.classify("hotkey", facts={"kind": "hotkey", "keys": ["enter"]})[0])
        for target in ("删除按钮", "移到回收站", "Delete", "Remove item", "卸载程序", "清空列表"):
            self.assertEqual(risk.classify("click", facts={"kind": "click", "target": target})[0], risk.DELETE, target)
        for target in ("替换", "Replace", "覆盖原文件", "Overwrite"):
            self.assertEqual(risk.classify("click", facts={"kind": "click", "target": target})[0], risk.OVERWRITE, target)
        self.assertIsNone(risk.classify("click", facts={"kind": "click", "target": "保存按钮"})[0])
        self.assertIsNone(risk.classify("click", facts={"kind": "click", "target": "information panel"})[0])  # whole words only
        self.assertEqual(risk.classify("type", facts={"kind": "type", "text": "rm -rf build"})[0], risk.DELETE)
        self.assertEqual(risk.classify("type", facts={"kind": "type", "text": "Remove-Item x.txt"})[0], risk.DELETE)
        kind, why = risk.classify("type", facts={"kind": "type", "text": r"D:\x\a.docx"}, exists=yes)
        self.assertEqual((kind, why), (risk.OVERWRITE, "会覆盖已有文件 a.docx"))
        self.assertIsNone(risk.classify("type", facts={"kind": "type", "text": r"D:\x\new.docx"}, exists=no)[0])
        self.assertIsNone(risk.classify("type", facts={"kind": "type", "text": "hello world"}, exists=yes)[0])
        self.assertIsNone(risk.classify("screenshot", facts={"kind": "screenshot"})[0])
        self.assertIsNone(risk.classify("open_app", facts={"kind": "open_app", "name": "记事本"})[0])
        self.assertIsNone(risk.classify("open_app", facts={"kind": "open_path", "path": r"C:\x"}, exists=yes)[0])

    def test_a_plain_save_as_passes_a_replace_prompt_asks(self):
        """The user's rule: ask when something WOULD be overwritten, not whenever a Save As dialog is open."""
        def k(facts, exists=no):
            return risk.classify(facts["kind"], facts=facts, exists=exists)
        for f in ({"kind": "type", "text": "report.docx", "window": "另存为"}, {"kind": "type", "text": "report.docx", "window": "Save As"},
                  {"kind": "hotkey", "keys": ["enter"], "window": "另存为"}, {"kind": "click", "target": "文件名框", "window": "另存为"},
                  {"kind": "click", "target": "保存按钮", "window": "另存为"}, {"kind": "hotkey", "keys": ["ctrl", "s"], "window": "笔记 - 记事本"},
                  {"kind": "type", "text": "report.docx", "window": "笔记 - 记事本"}):
            self.assertIsNone(k(f)[0], f)
        for f in ({"kind": "hotkey", "keys": ["alt", "y"], "window": "确认另存为"}, {"kind": "hotkey", "keys": ["y"], "window": "Confirm Save As"},
                  {"kind": "hotkey", "keys": ["enter"], "window": "report.docx already exists"},
                  {"kind": "type", "text": "y", "window": "确认另存为"},
                  {"kind": "click", "target": "是", "window": "确认另存为"},
                  {"kind": "click", "target": "Replace the file", "window": "Replace or Skip Files"},
                  {"kind": "click", "target": "替换", "window": "W"}):
            self.assertEqual(k(f), (risk.OVERWRITE, "会覆盖已有文件"), f)
        for f in ({"kind": "click", "target": "否", "window": "确认另存为"}, {"kind": "hotkey", "keys": ["alt", "n"], "window": "确认另存为"},
                  {"kind": "hotkey", "keys": ["esc"], "window": "确认另存为"}, {"kind": "click", "target": "Cancel", "window": "Confirm Save As"}):
            self.assertIsNone(k(f)[0], f)  # saying no to the replace is fine
        # a bare name in a Save As whose folder the desktop tool could read: asked only when that file is there
        f = {"kind": "type", "text": "test.txt", "window": "另存为", "folder": r"C:\x"}
        self.assertEqual(k(f, exists=lambda p: p == os.path.join(r"C:\x", "test.txt")), (risk.OVERWRITE, "会覆盖已有文件 test.txt"))
        self.assertIsNone(k(f)[0])
        self.assertEqual(k({"kind": "type", "text": r"D:\x\a.docx", "window": "另存为"}, exists=yes)[0], risk.OVERWRITE)  # a full path

    def test_batches_ask_only_for_a_real_overwrite(self):
        save = [{"kind": "hotkey", "keys": ["ctrl", "shift", "s"], "window": "笔记 - 记事本"},
                {"kind": "type", "text": "test.txt", "window": "笔记 - 记事本"}, {"kind": "hotkey", "keys": ["enter"], "window": "笔记 - 记事本"}]
        self.assertEqual(risk.classify_steps(["hotkey", "type", "hotkey"], save, "", exists=no), [])
        in_dialog = [{"kind": "click", "target": "文件名框", "window": "另存为"}, {"kind": "type", "text": "test.txt", "window": "另存为"},
                     {"kind": "hotkey", "keys": ["enter"], "window": "另存为"}]
        self.assertEqual(risk.classify_steps(["click", "type", "hotkey"], in_dialog, "", exists=no), [])
        confirm = [dict(f, window="确认另存为") for f in in_dialog]
        risky = risk.classify_steps(["click", "type", "hotkey"], confirm, "", exists=no)
        self.assertEqual([i for i, kind, w in risky], [0, 1, 2])
        self.assertEqual(risk.batch_reason(risky), "会覆盖已有文件")  # said once

    def test_the_batch_reason_is_said_once(self):
        D, O = risk.DELETE, risk.OVERWRITE
        self.assertEqual(risk.batch_reason([(1, D, "删除"), (3, D, "删除文件")]), "删除")
        self.assertEqual(risk.batch_reason([(0, O, "会覆盖已有文件 test.txt"), (2, O, "会覆盖已有文件")]), "会覆盖已有文件 test.txt")
        self.assertEqual(risk.batch_reason([(0, O, "会覆盖已有文件 a.txt"), (1, O, "会覆盖已有文件 b.txt")]), "会覆盖已有文件")
        self.assertEqual(risk.batch_reason([(0, D, "删除"), (1, O, "会覆盖已有文件")]), "删除，并会覆盖已有文件")
        self.assertEqual(risk.batch_reason([(0, O, "可能覆盖已有文件 out.txt")]), "可能覆盖已有文件")

    def test_shell_delete(self):
        for cmd in ("rm -rf dist", "rmdir /s /q out", "rd out", "del *.tmp", "erase a.txt", "Remove-Item -Recurse x", "ri x",
                    "Clear-Content log.txt", "git clean -fdx", "git reset --hard HEAD~1", "git push -f origin main",
                    "git push origin main --force", "git branch -D old", "format D:", "diskpart", "DROP TABLE users",
                    "truncate table t", "DELETE FROM t WHERE 1=1", "python -c 'x' && del a"):
            self.assertEqual(risk.shell(cmd)[0], risk.DELETE, cmd)
        for cmd in ("ls -la", "git status", "git push origin main", "echo format-check", "pip install ridge", "dir /b",
                    "python delete_me.py", "ping -n 3 127.0.0.1", "git push --force-with-lease"):
            self.assertNotEqual(risk.shell(cmd, exists=no)[0], risk.DELETE, cmd)

    def test_shell_overwrite(self):
        kind, why = risk.shell(r"echo hi > D:\x\a.txt", exists=yes)
        self.assertEqual((kind, why), (risk.OVERWRITE, r"会覆盖已有文件 D:\x\a.txt"))
        self.assertIsNone(risk.shell(r"echo hi > D:\x\new.txt", exists=no)[0])
        self.assertIsNone(risk.shell(r"echo hi >> D:\x\a.txt", exists=yes)[0])  # appending is not overwriting
        self.assertIsNone(risk.shell("cmd 2> err.txt", exists=yes)[0])  # stderr redirect alone: not a file write we flag
        self.assertEqual(risk.shell("sort x > out.txt", exists=no)[0], risk.OVERWRITE)  # relative: cannot be sure, ask
        self.assertEqual(risk.shell(r"copy a.txt D:\x\b.txt", exists=yes)[0], risk.OVERWRITE)
        self.assertIsNone(risk.shell(r"copy a.txt D:\x\b.txt", exists=no)[0])
        self.assertEqual(risk.shell("cp a b", exists=no)[0], risk.OVERWRITE)  # relative names: ask
        self.assertEqual(risk.shell(r"Move-Item x D:\y", exists=yes)[0], risk.OVERWRITE)
        self.assertEqual(risk.shell(r"robocopy C:\src D:\dst /MIR", exists=yes)[0], risk.OVERWRITE)
        self.assertEqual(risk.shell(r"Set-Content D:\x\a.txt 'x'", exists=yes)[0], risk.OVERWRITE)
        self.assertIsNone(risk.shell("python train.py --epochs 3", exists=yes)[0])

    def test_codex_file_changes(self):
        f = {"kind": "file_change", "changes": [{"path": r"D:\p\a.py", "kind": "delete"}]}
        self.assertEqual(risk.classify("codex_edit", facts=f)[0], risk.DELETE)
        f = {"kind": "file_change", "changes": [{"path": r"D:\p\a.py", "kind": "update"}]}
        self.assertEqual(risk.classify("codex_edit", facts=f, exists=yes)[0], risk.OVERWRITE)
        f = {"kind": "file_change", "changes": [{"path": r"D:\p\new.py", "kind": "add"}]}
        self.assertIsNone(risk.classify("codex_edit", facts=f, exists=no)[0])
        self.assertEqual(risk.classify("codex_edit", facts=f, exists=yes)[0], risk.OVERWRITE)
        self.assertEqual(risk.classify("codex_edit", facts={"kind": "file_change"})[0], risk.OVERWRITE)  # unknown: ask
        self.assertEqual(risk.classify("codex_shell", facts={"kind": "shell", "command": "rm x"})[0], risk.DELETE)
        self.assertIsNone(risk.classify("codex_shell", facts={"kind": "shell", "command": "pytest -q"}, exists=no)[0])
        self.assertIsNone(risk.classify("codex_shell", facts={"kind": "permissions"})[0])

    def test_texts_without_facts(self):
        self.assertEqual(risk.classify("click", "我想点一下删除，可以吗？")[0], risk.DELETE)
        self.assertEqual(risk.classify("background_job", "我想在后台运行：rm -rf x，可以吗？", "rm -rf x")[0], risk.DELETE)
        self.assertEqual(risk.classify("click", "我想点一下替换，可以吗？")[0], risk.OVERWRITE)
        self.assertIsNone(risk.classify("screenshot", "我想看一眼屏幕，可以吗？")[0])
        self.assertIsNone(risk.classify("switch_model", "要把模型切换成 sonnet。可以吗？")[0])

    def test_batch(self):
        actions = ["open_app", "type", "hotkey"]
        facts = [{"kind": "open_app", "name": "记事本"}, {"kind": "type", "text": "hi"}, {"kind": "hotkey", "keys": ["delete"]}]
        self.assertEqual([(i, k) for i, k, w in risk.classify_steps(actions, facts, "")], [(2, risk.DELETE)])
        self.assertEqual(risk.classify_steps(["click", "click"], None, "1. 点一下保存\n2. 点一下删除"), [(1, risk.DELETE, "删除")])
        self.assertEqual(risk.classify_steps(["type"], [{"kind": "type", "text": "x"}], ""), [])
