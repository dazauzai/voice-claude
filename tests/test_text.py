import unittest

from voiceclaude.text import (CODE_NOTE, SentenceSplitter, clean_for_speech, fixed_phrase, is_exit_phrase,
                              is_filler, split_complete, yes_or_no)


class CleanTests(unittest.TestCase):
    def test_markdown_removed(self):
        self.assertEqual(clean_for_speech("**结论**：用 `pytest` 看 [文档](http://x.y)。"), "结论：用 pytest 看 文档。")
        self.assertEqual(clean_for_speech("## 标题\n- 第一点\n1. 第二点"), "标题 第一点 第二点")
        self.assertEqual(clean_for_speech("详见 https://example.com/a?b=c 这里"), "详见 链接 这里")

    def test_underscores_in_identifiers_survive(self):
        self.assertEqual(clean_for_speech("变量 snake_case_name 没问题"), "变量 snake_case_name 没问题")

    def test_punctuation_only_dropped(self):
        self.assertEqual(clean_for_speech("---"), "")
        self.assertEqual(clean_for_speech("。"), "")


class SplitTests(unittest.TestCase):
    def test_terminal_punctuation(self):
        self.assertEqual(split_complete("好的。我看了一下！还没完"), (["好的。", "我看了一下！"], "还没完"))

    def test_comma_only_when_long(self):
        short = "好的，我看一下"
        self.assertEqual(split_complete(short), ([], short))
        long = "好的，我看了一下 README 的 install 和 CI 的 pipeline，一共有二十三个问题"
        sentences, rest = split_complete(long)
        self.assertEqual(sentences, ["好的，我看了一下 README 的 install 和 CI 的 pipeline，"])
        self.assertEqual(rest, "一共有二十三个问题")


class StreamingSplitterTests(unittest.TestCase):
    def feed_all(self, deltas):
        sp = SentenceSplitter()
        out = []
        for d in deltas:
            out += sp.feed(d)
        return out + sp.flush()

    def test_sentences_across_deltas(self):
        self.assertEqual(self.feed_all(["好的。我看", "了一下。", "还有"]), ["好的。", "我看了一下。", "还有"])

    def test_code_block_replaced_once(self):
        deltas = ["结果如下：\n```python\nprint(1)\n", "print(2)\n``", "`\n后面继续。"]
        self.assertEqual(self.feed_all(deltas), ["结果如下：", CODE_NOTE, "后面继续。"])

    def test_unterminated_code_block(self):
        self.assertEqual(self.feed_all(["看这个 ```\nls -la"]), ["看这个", CODE_NOTE])

    def test_markdown_list_streamed(self):
        self.assertEqual(self.feed_all(["**要点**：\n- 第一\n- 第二\n"]), ["要点：", "第一", "第二"])

    def test_first_sentence_may_be_short(self):
        sp = SentenceSplitter()
        # 12+ chars before the first comma: speak it right away instead of waiting for 24
        self.assertEqual(sp.feed("Python 是最常用的一种脚本语言，被称为胶水语言之一，"), ["Python 是最常用的一种脚本语言，"])
        self.assertEqual(sp.feed("诞生于一九九一年，"), [])  # later sentences wait for the normal length
        self.assertEqual(sp.flush(), ["被称为胶水语言之一，诞生于一九九一年，"])


class ExitPhraseTests(unittest.TestCase):
    def test_phrases(self):
        for t in ["结束对话", "结束对话。", "好，结束对话", "退出对话吧"]:
            self.assertTrue(is_exit_phrase(t), t)
        for t in ["结束对话之前先帮我看一下", "我们讨论一下怎么结束这个实验"]:
            self.assertFalse(is_exit_phrase(t), t)

    def test_yes_or_no(self):
        for t in ["可以。", "可以的", "好的", "好", "行", "对，切吧", "OK", "Yes.", "没问题"]:
            self.assertIs(yes_or_no(t), True, t)
        for t in ["不行", "不要", "不可以", "算了", "取消", "No", "等一下"]:
            self.assertIs(yes_or_no(t), False, t)
        for t in ["", "呃", "你说什么", "换成哪个"]:
            self.assertIsNone(yes_or_no(t), t)

    def test_a_negation_anywhere_is_no(self):
        # an approval must never be read into an answer that says no somewhere (宁可错拒不错批)
        for t in ["嗯，不行", "好像不行", "行不行啊", "可以，不过先等等", "好，先别", "停", "停一下", "没事，算了", "No problem? no.",
                  "嗯不要", "OK wait", "对，不对"]:
            self.assertIs(yes_or_no(t), False, t)
        for t in ["做吧", "嗯，好的", "嗯，可以", "没问题，做吧", "行吧", "是的", "对的", "yes, do it", "sure"]:
            self.assertIs(yes_or_no(t), True, t)
        for t in ["可以吗", "是要删除吗？", "好吗", "嗯"]:  # a question back, or a bare filler, is not an answer
            self.assertIsNone(yes_or_no(t), t)

    def test_english_refusals_later_ways_to_wait_and_rising_tones(self):
        for t in ["Yeah, no.", "Okay, no.", "Sure, not now", "OK, never mind", "yes, don't", "OK hold on",
                  "好，等下", "行，稍等", "好的，等会儿", "好的，慢着", "可以，回头再说"]:
            self.assertIs(yes_or_no(t), False, t)
        for t in ["可以？", "OK?", "Yes?", "好？"]:  # a rising tone is written as a question mark
            self.assertIsNone(yes_or_no(t), t)
        for t in ["没错，删吧", "没有问题", "No problem", "不错，做吧", "没毛病"]:  # yes phrases with a negation in them
            self.assertIs(yes_or_no(t), True, t)

    def test_yes_only_when_every_word_is_a_yes(self):
        # a negation next to a yes phrase is a no, even once the comma is gone ("不，错了" is not "不错了")
        for t in ["不，错了", "没，错了", "没有，问题很大"]:
            self.assertIs(yes_or_no(t), False, t)
        # anything that is not entirely a yes asks again: later, skip, but, forget it
        for t in ["好，待会儿", "好的，以后再弄", "好，下次吧", "可以，但是先让我看看", "好，跳过", "OK, forget it",
                  "OK, skip it", "Okay, hang on", "OK, nevermind", "好的，那个文件", "可以，顺便把桌面也清一下"]:
            self.assertIsNot(yes_or_no(t), True, t)
        for t in ["确定", "允许", "继续", "当然", "执行吧", "alright", "proceed", "可以吧", "好的好的", "行行行", "OK, go ahead",
                  "yes please", "嗯，确定", "对对对", "好嘞", "好，开始吧", "请继续", "对，就这个", "Sure thing"]:
            self.assertIs(yes_or_no(t), True, t)

    def test_fixed_phrases(self):
        cases = {"回到总机。": "back_to_manager", "好，回到 manager": "back_to_manager", "接杂项": "scratch",
                 "进入后台模式。": "background_mode", "后台怎么样了？": "background_status",
                 "关掉后台服务": "shutdown_daemon", "撤销排队。": "cancel_queue", "刚才的别发了": "cancel_queue",
                 "打开接入页。": "open_access", "打开 AI 服务商。": "open_access", "A I 服务商": "open_access",
                 "打开通透模式。": "transparent_on", "开启通透模式": "transparent_on", "请关闭通透模式": "transparent_off",
                 "退出通透模式。": "transparent_off"}
        for text, key in cases.items():
            self.assertEqual(fixed_phrase(text), key, text)
        for text in ["", "总机是什么", "帮我看一下后台的日志文件"]:
            self.assertIsNone(fixed_phrase(text), text)

    def test_fillers(self):
        for t in ["呃.", "嗯嗯", "啊", ".", "", "呃，嗯。"]:
            self.assertTrue(is_filler(t), t)
        for t in ["嗯，好的", "呃 Python", "对", "H Bob."]:
            self.assertFalse(is_filler(t), t)


if __name__ == "__main__":
    unittest.main()
