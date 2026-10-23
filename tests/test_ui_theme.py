"""Pure helpers of the window theme (no Tk, no display): status classification, hint texts, titles parsed from
the spoken question, job notes, truncation."""
import unittest

from voiceclaude import ui_theme as t
from voiceclaude.ui_access import describe


class StatusTests(unittest.TestCase):
    def test_classify(self):
        cases = {"启动中…": "starting", "正在加载语音模型…": "starting", "正在连接后台服务…": "starting", "空闲": "idle",
                 "录音中…": "recording", "在听…": "listening", "识别中…": "transcribing", "思考中…": "thinking",
                 "正在跑命令…": "tool", "正在干活…": "tool", "在说…": "speaking", "你在说…": "you_talking",
                 "停了，在听你说": "you_stopped", "等你回答": "waiting", "Atlas 在等你": "waiting",
                 "后台服务断开": "disconnected", "启动失败": "failed", "后台服务已关掉": "closed", "已紧急停止": "estopped"}
        for text, key in cases.items():
            self.assertEqual(t.classify_status(text), key, text)
            self.assertIn(key, t.STATES)

    def test_band(self):
        self.assertEqual(t.band_for("listening", False, False), "green")
        self.assertEqual(t.band_for("listening", False, True), "gray")  # muted: the mic is not open
        self.assertEqual(t.band_for("thinking", True, False), "green")  # barge-in conversation
        self.assertEqual(t.band_for("thinking", False, False), "gray")
        self.assertEqual(t.band_for("waiting", False, True), "ask")  # amber stays even when muted
        self.assertEqual(t.band_for("disconnected", True, False), "error")

    def test_muted_words(self):
        self.assertEqual(t.status_display("在听…", True), "已静音")
        self.assertEqual(t.status_display("录音中…", True), "已静音")
        self.assertEqual(t.status_display("等你回答", True), "等你回答 · 已静音")
        self.assertEqual(t.status_display("思考中…", True), "思考中… · 已静音")
        self.assertEqual(t.status_display("后台服务断开", True), "后台服务断开")
        self.assertEqual(t.status_display("在听…", False), "在听…")

    def test_tool_words(self):
        self.assertEqual(t.tool_word("Bash"), "正在跑命令…")
        self.assertEqual(t.tool_word("Read"), "正在看文件…")
        self.assertEqual(t.tool_word("MultiEdit"), "正在改文件…")
        self.assertEqual(t.tool_word("Glob"), "正在找文件…")
        self.assertEqual(t.tool_word("WebSearch"), "正在查网页…")
        self.assertEqual(t.tool_word("Task"), "正在派活…")
        self.assertEqual(t.tool_word("mcp__voiceclaude-desktop__click"), "正在操作桌面…")
        self.assertEqual(t.tool_word("mcp__voiceclaude__set_font_size"), "正在改设置…")
        self.assertEqual(t.tool_word("SomethingElse"), "正在干活…")
        for name in ("Bash", "mcp__x__y", ""):
            self.assertNotIn(name or "x", t.tool_word(name).replace("…", "") if name else "")


class HintTests(unittest.TestCase):
    def test_texts(self):
        h = t.hint_for("ask", "右 Ctrl", "右 Alt", 0.8)
        self.assertEqual(h.text, "说「可以」或「不行」· 右 Alt 可以 · 右 Ctrl 不行")
        self.assertEqual(h.strong, "ask")
        self.assertEqual(t.hint_for("recording", "右 Ctrl", "右 Alt", 0.8).text, "右 Ctrl 再按一下 发送 · 按住 0.8 秒 紧急停止")
        self.assertEqual(t.hint_for("busy_conv", "右 Ctrl", "右 Alt", 0.8).text, "开口的话先排队 · 右 Ctrl 打断 · 按住 0.8 秒 紧急停止")
        self.assertEqual(t.hint_for("ask_muted", "右 Ctrl", "右 Alt", 0.8).text, "已静音，说话它听不到 · 右 Alt 可以 · 右 Ctrl 不行")
        self.assertEqual(t.hint_for("closed", "右 Ctrl", "", 0.8).text, "要再用，关掉这个窗口再打开")
        self.assertEqual(t.hint_for("ask", "右 Ctrl", "", 0.8).text, "说「可以」或「不行」· 右 Ctrl 不行")
        self.assertEqual(t.hint_for("idle", "右 Ctrl", "", 0.8).text, "右 Ctrl 单按 说一段 · 双按 开始对话 · 静音按钮 静音")
        for sit in ("starting", "idle", "recording", "listening", "busy_conv", "busy_queue", "busy_single", "ask", "question",
                    "ask_muted", "muted", "disconnected", "closed"):
            text = t.hint_for(sit, "右 Ctrl", "右 Alt", 0.8).text
            self.assertNotIn("\n", text)  # one line; fit_hint drops segments from the end when it does not fit
            self.assertNotIn("=", text)
            self.assertNotIn("叫停", text)
            self.assertNotIn("急停", text.replace("紧急停止", ""))  # one name per action

    def test_fit(self):
        measure = len
        self.assertEqual(t.fit_hint("a · bb · ccc", measure, 100), "a · bb · ccc")
        self.assertEqual(t.fit_hint("a · bb · ccc", measure, 7), "a · bb")
        self.assertEqual(t.fit_hint("a · bb · ccc", measure, 1), "a")


class TextTests(unittest.TestCase):
    def test_truncate(self):
        self.assertEqual(t.truncate_to_width("abcdef", len, 10), "abcdef")
        out = t.truncate_to_width("abcdefghij", len, 6)
        self.assertTrue(out.endswith("…") and len(out) <= 6, out)
        self.assertEqual(t.ellipsize("voice-claude-is-long", 12), "voice-claude…")
        self.assertEqual(t.ellipsize("Atlas"), "Atlas")

    def test_wrap_mode(self):
        col = 38 * 2  # a column of 38 Chinese characters when measure = len * 2 per character
        measure = lambda s: 2 * len(s)
        self.assertEqual(t.wrap_mode("ls -F /c/Users and some english", measure, col), "word")
        # English words inside a Chinese sentence must not break in half: every token is short -> word wrap
        self.assertEqual(t.wrap_mode("跑完了，一共 23 张图，其中 3 张有警告", measure, col), "word")
        # a Chinese sentence without spaces is one Tk word: character wrap, otherwise it moves whole to the next line
        self.assertEqual(t.wrap_mode("这是一句没有空格的很长很长很长的中文句子，它比半列还要宽一些才会按字折行", measure, col), "char")
        self.assertEqual(t.wrap_mode("", measure, col), "char")
        # an over-long command token (a path) still wraps by character so it does not leave a half-empty line
        self.assertEqual(t.wrap_mode("看 /c/Users/you/Documents/report/figures/all-figures.pdf", measure, 60), "char")
        self.assertTrue(t.is_ascii("python make_figures.py"))
        self.assertFalse(t.is_ascii("生成图表"))


class AskTitleTests(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(t.ask_title("我想在后台运行：python make_figures.py，输出记到日志。可以吗？"), ("申请 · 后台运行命令", "", "我想在后台运行：python make_figures.py，输出记到日志。可以吗？"))
        self.assertEqual(t.ask_title("要把模型切换成 sonnet。可以吗？")[0], "申请 · 切换模型")
        self.assertEqual(t.ask_title("要把“点击”的权限改成不用问，可以吗？")[0], "申请 · 改权限")
        self.assertEqual(t.ask_title("要接到 Atlas 项目吗？")[0], "申请 · 转接")
        self.assertEqual(t.ask_title("还有 2 个后台任务在跑（过滤），要一起停吗？")[0], "申请 · 停后台任务")
        self.assertEqual(t.ask_title("我想看一下屏幕，可以吗？")[0], "申请 · 看屏幕")
        self.assertEqual(t.ask_title("我想点一下“保存”，可以吗？")[0], "申请 · 点击")
        self.assertEqual(t.ask_title("我想输入文字 hello，可以吗？")[0], "申请 · 输入文字")
        self.assertEqual(t.ask_title("我想按快捷键 Ctrl+S，可以吗？")[0], "申请 · 按快捷键")
        self.assertEqual(t.ask_title("我想在记事本往下滚，可以吗？")[0], "申请 · 滚动")
        self.assertEqual(t.ask_title("我想打开记事本，可以吗？")[0], "申请 · 打开程序")
        self.assertEqual(t.ask_title("我想打开记事本，然后输入 hi，可以吗？")[0], "申请 · 一次批准一串步骤")
        self.assertEqual(t.ask_title("某个全新的问题？")[0], "申请")

    def test_background_and_question(self):
        title, line, body = t.ask_title("后台的Atlas申请：我想输入文字 hello，可以吗？")
        self.assertEqual((title, line, body), ("后台的 Atlas 申请 · 输入文字", "Atlas", "我想输入文字 hello，可以吗？"))
        self.assertEqual(t.ask_title("接到哪个项目？", kind="question"), ("问你 · 总机", "", "接到哪个项目？"))

    def test_heard(self):
        self.assertEqual(t.heard_suffix("可以。", "可以"), "")
        self.assertEqual(t.heard_suffix("行吧可以的", "可以"), "（听到：行吧可以的）")
        self.assertEqual(t.heard_suffix("", "不行"), "")


class JobTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(t.parse_job_note("后台任务 8a2f：running 生成图表 第 3 批"), ("8a2f", "running", "生成图表 第 3 批"))
        self.assertEqual(t.parse_job_note("后台任务 1c9e：done"), ("1c9e", "done", ""))
        self.assertIsNone(t.parse_job_note("后台任务做完了"))
        self.assertIsNone(t.parse_job_note("（没听清）"))

    def test_row_text(self):
        self.assertEqual(t.job_row_text({"id": "8a2f", "note": "过滤", "state": "running", "started": 1000}, 1000 + 200), "过滤 · 还在跑 3 分")
        self.assertEqual(t.job_row_text({"id": "8a2f", "note": "过滤", "state": "running", "started": 1000}, 1010), "过滤 · 还在跑")
        self.assertEqual(t.job_row_text({"id": "1c9e", "note": "", "command": "x" * 60, "state": "failed"}, 0), "x" * 40 + " · 失败了")
        self.assertEqual(t.permissions_summary({"a": "ask", "b": "ask", "c": "allow", "d": "deny"}), "询问 2 · 允许 1 · 禁止 1")
        self.assertEqual(set(t.PERMISSION_WORDS.values()), {"询问", "允许", "禁止"})


class AccessDescribeTests(unittest.TestCase):
    def test_never_shows_a_key(self):
        d = describe({"ok": True, "method": "api_key", "message": "用 API key（…mnop）", "account": "…mnop"})
        self.assertEqual(d, "Claude · API key")  # not even the last four characters on the settings row
        self.assertEqual(describe(None), "正在读取…")
        self.assertEqual(describe({"ok": False, "method": "none", "message": "没装 Claude Code"}), "未设置")


if __name__ == "__main__":
    unittest.main()
