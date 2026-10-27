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

    def test_a_command_that_starts_another_claude_is_asked(self):
        """A second Claude Code has none of our gates: it could message the user's other sessions unasked."""
        for cmd in ('claude -p "用 SendMessage 把这句发给「论文写作辅助」"', "Claude.exe -p hi",
                    r'& "C:\Users\me\.local\bin\claude.exe" -p x', "npx @anthropic-ai/claude-code -p x",
                    'python -c "from claude_agent_sdk import query"', r"cd D:\x && claude --resume abc"):
            self.assertTrue(risk.starts_claude(cmd), cmd)
            self.assertEqual(risk.classify("background_job", "q", cmd), (risk.CLAUDE, risk.STARTS_CLAUDE_WHY), cmd)
            self.assertEqual(risk.classify("codex_shell", "q", "", {"kind": "shell", "command": cmd})[0], risk.CLAUDE, cmd)
        for cmd in ("cmd /c claude -p x", 'cmd /c "claude -p x"', 'powershell -NoProfile -Command "claude -p hi"',
                    "Start-Process claude -ArgumentList hi", "echo hi | claude -p", r'& "C:\Program Files\x\claude.exe" -p y',
                    "git status; claude", "npx -y @anthropic-ai/claude-code"):
            self.assertTrue(risk.starts_claude(cmd), cmd)
        # the broad rule: only a folder or a longer name is left alone
        for cmd in ("cd voice-claude", r"type C:\Users\me\.claude\settings.json", "pip install claude_tools",
                    "dir claude.json", "git log", r"dir C:\Users\alex\AppData\Roaming\Claude\logs",
                    r'Get-Content "$env:APPDATA\Claude\logs\main.log" -Wait', "git log -- claude/",
                    r"D:\claude-notes\run.bat", "pip install claude-code-sdk", "start https://claude.ai"):
            self.assertFalse(risk.starts_claude(cmd), cmd)
        # ...and anything else that names claude on its own is asked, needless or not (the user accepts that)
        for cmd in ('grep -rn "claude" logs/', "git log --oneline --grep claude", "python -m pytest -k claude", "cd claude",
                    "echo ask claude about it", 'git commit -m "fix claude gate"', "Get-Process claude", "where claude"):
            self.assertTrue(risk.starts_claude(cmd), cmd)
        self.assertEqual(risk.classify("type", "", "", {"kind": "type", "text": "claude -p hi"})[0], risk.CLAUDE)
        for sentence in ("Thanks, Claude!", "Claude Code 的设置在哪", "我在用 Claude 写论文", "Claude 帮我看看"):
            self.assertIsNone(risk.classify("type", "", "", {"kind": "type", "text": sentence, "window": "Word"})[0], sentence)
        # ...but typed into a terminal, a line that starts with claude does start it
        self.assertEqual(risk.classify("type", "", "", {"kind": "type", "text": "Claude 帮我看看", "window": "Windows PowerShell"})[0],
                         risk.CLAUDE)
        self.assertEqual(risk.classify("type", "", "", {"kind": "type", "text": "cd claude", "window": "Windows PowerShell"})[0],
                         risk.CLAUDE)  # the broad rule: asked, needless or not
        self.assertEqual(risk.batch_reason([(0, risk.CLAUDE, risk.STARTS_CLAUDE_WHY), (1, risk.DELETE, "删除")]),
                         risk.STARTS_CLAUDE_WHY + "，并删除")

    LAUNCHES = [
        r"""claude -p hi""", r"""claude.cmd -p hi""", r"""& 'C:\path with space\claude.exe' -p hi""",
        r"""& "C:\Users\alex\.local\bin\claude.exe" --version""", r"""& claude -p hi""",
        r"""Start-Process claude -ArgumentList '-p','hi'""", r"""Start-Process -FilePath claude -ArgumentList '-p','hi'""",
        r"""Start-Process -FilePath "C:\Users\alex\.local\bin\claude.exe" -ArgumentList '-p','hi'""",
        r"""Start-Process -FilePath 'claude' -ArgumentList '-p','hi'""", r"""Start-Process -NoNewWindow claude.exe""",
        r"""saps claude""", r"""start claude""", r"""start "" claude -p hi""", r"""start "Claude" claude""",
        r"""start /b claude -p hi > out.txt""", r"""start /min claude""", r"""cmd /c start "" claude -p hi""",
        r"""call claude -p hi""", r"""cmd /c claude -p hi""", r"""cmd.exe /c "claude -p hi" """,
        r"""powershell -NoProfile -Command "claude -p hi" """, r""""C:\Program Files\PowerShell\7\pwsh.exe" -Command "claude -p hi" """,
        r"""'C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe' -Command 'claude -p hi'""",
        r"""C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe -Command 'claude -p hi'""",
        r"""bash -lc 'claude -p hi'""", r"""/bin/zsh -lc 'claude -p hi'""", r"""bash -c 'claude -p hi'""",
        r"""cd C:\proj && claude -p hi""", r"""cd C:\proj; claude -p hi""", r"""Get-Content p.txt | claude -p""",
        r"""for %f in (*.md) do claude -p "summarize %f" """, r"""Get-ChildItem *.md | ForEach-Object { claude -p "summarize $_" }""",
        r"""gci *.md | % { claude -p $_.Name }""", r"""Start-Job { claude -p hi }""", r"""Start-Job -ScriptBlock { claude -p hi }""",
        r"""& { claude -p hi }""", r"""for f in *.md; do claude -p "$f"; done""", r"""timeout 600 claude -p hi""",
        r"""wt claude""", r"""wt -d C:\proj claude""", r"""Invoke-Expression 'claude -p hi'""",
        r"""python -c "import subprocess; subprocess.run(['claude','-p','hi'])" """,
        r"""python -c "import subprocess; subprocess.Popen(['claude', '-p', 'hi'])" """,
        r"""node -e "require('child_process').spawn('claude',['-p','hi'])" """, r"""npx @anthropic-ai/claude-code -p hi""",
        r"""npm exec @anthropic-ai/claude-code -- -p hi""",
        r"""node C:/Users/alex/AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/cli.js -p hi""",
        r"""echo hi | claude""", r"""$env:X='1'; claude -p hi""", r"""set X=1&& claude -p hi""", r"""x=$(claude -p hi)""",
        r"""C:\Users\alex\.local\bin\claude.exe -p hi""", r"""~/.local/bin/claude -p hi""",
    ]

    ODD_LAUNCHES = [
        # -e is how wsl and terminal programs run a command
        "wsl -e claude -p hi", "wsl.exe -e claude -p hi", "wsl -d Ubuntu -e claude -p hi", r"C:\Windows\System32\wsl.exe -e claude",
        "mintty -e claude", r'"C:\Program Files\Git\usr\bin\mintty.exe" -e claude', "alacritty -e claude", "xterm -e claude",
        'start "" wsl -e claude', "cmd /c wsl -e claude -p hi", 'powershell -Command "wsl -e claude -p hi"',
        # the path claude.cmd runs, with backslashes
        r'node "%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\cli.js" -p hi',
        r"node .\node_modules\@anthropic-ai\claude-code\cli.js -p hi",
        # a lookup handed on as the program; a pipe into something that runs it; git / find that run their arguments
        '"$(which claude)" -p hi', "& (where.exe claude) -p hi", r"& (Resolve-Path ~\.local\bin\claude.exe) -p hi",
        'Start-Process -FilePath (where.exe claude) -ArgumentList "-p hi"', "where.exe claude | % { & $_ -p hi }",
        """for /f "delims=" %i in ('where claude') do "%i" -p hi""", 'git rebase -x "claude -p fix" HEAD~3',
        "git bisect run claude -p test", "git submodule foreach claude -p hi", r'find . -name "*.md" -exec claude -p {} \;',
        "echo claude -p hi | cmd", "Write-Output 'claude -p hi' | Invoke-Expression", "echo `claude -p hi`",
        'powershell -Command "& (where.exe claude) -p hi"', """bash -lc '$(which claude) -p hi'""",
        "git -c core.editor=claude commit", 'git commit -m "$(claude -p summarize)"',
        # the SDKs start it too
        'python -c "from claude_code_sdk import query"', """node -e "require('@anthropic-ai/claude-agent-sdk')" """,
        # escaped or encoded
        "powershell -NoProfile -EncodedCommand YwBsAGEAdQBkAGUAIAAtAHAAIABoAGkA", "powershell -enc YwBsAGEAdQBkAGUAIAAtAHAAIABoAGkA",
        "powershell -enc !!not-base64!!", "c^laude -p hi", "^c^l^a^u^d^e -p hi", "c`laude -p hi", 'cmd /c cl"a"ude -p hi',
        "~/.local/share/claude/versions/2.1.308 -p hi",
    ]
    NOT_LAUNCHES = [  # no word claude of its own: a longer name, a folder, a web address, an encoded dir
        "pip install claude-code-sdk", r"cd D:\proj\claude-notes", "git log -- claude/", "open https://docs.claude.com",
        "powershell -NoProfile -EncodedCommand ZABpAHIA", "powershell -ExecutionPolicy Bypass -File a.ps1",
    ]

    def test_odd_ways_of_starting_claude_and_what_is_not_one(self):
        for cmd in self.ODD_LAUNCHES:
            self.assertTrue(risk.starts_claude(cmd), cmd)
        for cmd in self.NOT_LAUNCHES:
            self.assertFalse(risk.starts_claude(cmd), cmd)
        long = "pytest -k voice-claude " * 10000  # one pass
        self.assertFalse(risk.starts_claude(long))

    def test_what_shells_drop_or_accept_before_running_a_word(self):
        """From the review: shapes cmd, bash and PowerShell run as Claude Code that a plain word test would miss."""
        enc = "YwBsAGEAdQBkAGUAIAAtAHAAIABoAGkA"  # claude -p hi
        for cmd in ("@claude -p hi", "cmd /c @claude -p hi", '"c"laude -p hi', "'c'laude -p hi", """"cl"'a'"ude" -p hi""",
                    r"cl\aude -p hi", r"c\l\a\u\d\e -p hi", "claude. -p hi", "claude.exe. -p hi", "claude.bat -p hi",
                    f"powershell /e {enc}", f"powershell /EncodedCommand {enc}", f"powershell \u2013e {enc}",
                    f"powershell \u2014EncodedCommand {enc}", f"powershell -EncodedCommand:{enc}", f"powershell -e={enc}"):
            self.assertTrue(risk.starts_claude(cmd), cmd)
        self.assertTrue(risk.starts_claude(b"claude -p hi"))  # bytes too
        for cmd in ("dir claude.json", "open https://claude.ai", "start https://docs.claude.com", "echo claude-notes",
                    "me@claude.ai", "powershell -e YwA="):
            self.assertFalse(risk.starts_claude(cmd), cmd)

    def test_an_app_name_whose_command_is_claude(self):
        """open_app runs what [desktop.apps] says; a line could have put claude there under a harmless name."""
        self.assertEqual(risk.classify("open_app", "", "", {"kind": "open_app", "name": "便签", "command": "claude"})[0], risk.CLAUDE)
        self.assertIsNone(risk.classify("open_app", "", "", {"kind": "open_app", "name": "记事本", "command": "notepad"})[0])

    def test_terminals_and_editors_by_their_program(self):
        for image in ("WindowsTerminalPreview.exe", "Code.exe", "Cursor.exe", "pycharm64.exe", "idea64.exe", "Hyper.exe"):
            self.assertTrue(risk.hosts_commands(image), image)
            self.assertEqual(risk.classify("type", "", "", {"kind": "type", "text": "cd x && claude", "window": "proj",
                                                             "program": image})[0], risk.CLAUDE, image)
        for image in ("explorer.exe", "WINWORD.EXE", "chrome.exe", ""):
            self.assertFalse(risk.hosts_commands(image), image)

    def test_typing_into_a_launch_box_or_a_terminal_is_a_command(self):
        """Win+R's Run box (运行; 弹出的窗口 before it is up), Explorer's address bar, a shell prompt as the title."""
        for text, window, program in (("cmd /k claude -p hi", "运行", ""), ("cmd /k claude -p hi", "Run", ""),
                                      ("cmd /k claude -p hi", "弹出的窗口", ""), ("wt claude", "弹出的窗口", ""),
                                      ("cmd /k claude", "文档", "explorer.exe"), ("cd proj && claude", "alex@DESKTOP-ABC: ~/proj", ""),
                                      ("cd proj && claude", "proj", "WindowsTerminal.exe"),
                                      (r"wt -d D:\proj claude", "", ""), ("powershell -NoExit -Command claude", "", ""),
                                      ("npx @anthropic-ai/claude-code", "", "")):
            self.assertEqual(risk.classify("type", "", "", {"kind": "type", "text": text, "window": window, "program": program})[0],
                             risk.CLAUDE, (text, window, program))
        for text in ("Thanks, Claude!", "Claude Code 的设置在哪", "我在用 Claude 写论文"):
            self.assertIsNone(risk.classify("type", "", "", {"kind": "type", "text": text, "window": "文档1 - Word",
                                                             "program": "WINWORD.EXE"})[0], text)
        batch = risk.classify_steps(["hotkey", "type", "hotkey"], [{"kind": "hotkey", "keys": ["win", "r"], "window": ""},
                                                                    {"kind": "type", "text": 'cmd /k claude -p "hi"', "window": "弹出的窗口"},
                                                                    {"kind": "hotkey", "keys": ["enter"], "window": "弹出的窗口"}], "")
        self.assertEqual([k for _, k, _ in batch], [risk.CLAUDE])

    def test_typing_into_a_window_where_a_claude_session_runs_is_a_message(self):
        steps = [{"kind": "click", "target": "输入框", "window": "Claude", "session": True},
                 {"kind": "type", "text": "请把 论文 那一节重写一遍", "window": "Claude", "session": True},
                 {"kind": "hotkey", "keys": ["enter"], "window": "Claude", "session": True}]
        self.assertEqual([k for _, k, _ in risk.classify_steps(["click", "type", "hotkey"], steps, "")],
                         [risk.SEND, risk.SEND, risk.SEND])  # any click, typing or key there: a paste, a number at a prompt
        self.assertEqual(risk.classify("hotkey", "", "", {"kind": "hotkey", "keys": ["ctrl", "v"], "session": True})[0], risk.SEND)
        self.assertEqual(risk.classify("hotkey", "", "", {"kind": "hotkey", "keys": ["1"], "session": True})[0], risk.SEND)
        self.assertEqual(risk.classify("click", "", "", {"kind": "click", "target": "发送", "session": True})[0], risk.SEND)
        self.assertEqual(risk.batch_reason(risk.classify_steps(["click", "type", "hotkey"], steps, "")), risk.SENDS_WHY)
        self.assertIsNone(risk.classify("type", "", "", {"kind": "type", "text": "hi", "window": "Claude", "session": False})[0])

    def test_stopping_a_job_starts_nothing(self):
        for q in ("要停掉后台任务 j1（claude -p x），可以吗？", "要停掉后台任务 a3（让 Claude 汇总日志，rm 旧文件），可以吗？"):
            self.assertEqual(risk.classify("background_job", q, "", {"kind": "job_stop"}), (None, ""))

    def test_every_way_of_starting_claude_is_caught(self):
        """Erring toward asking: launchers have too many shapes to list, so any word claude that can be the program
        counts; a miss would be a second Claude Code with no gate under 通透模式."""
        for cmd in self.LAUNCHES:
            self.assertTrue(risk.starts_claude(cmd), cmd)
        self.assertEqual(risk.classify("background_job", "q", 'start "" claude -p hi'), (risk.CLAUDE, risk.STARTS_CLAUDE_WHY))
        self.assertEqual(risk.classify("codex_shell", "q", "d", {"kind": "shell", "command": "Start-Process -FilePath claude -ArgumentList '-p','hi'"})[0],
                         risk.CLAUDE)
        for cmd in (r'"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe" -Command dir',
                    r'"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe" -Command "Get-Content .claude\settings.json"'):
            self.assertFalse(risk.starts_claude(cmd), cmd)  # a Codex escalation is shaped like these
        for text, window in (('claude "fix the failing test"', "main.py - proj - Visual Studio Code"),
                             ("claude 'hi'", "MINGW64:/c/Users/alex/proj"), ("cd proj && claude", "MINGW64:/c/Users/alex/proj"),
                             ("claude", r"C:\Users\alex"), ('claude "fix it"', "~")):
            self.assertTrue(risk.types_claude(text, window), (text, window))

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
