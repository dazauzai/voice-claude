import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voiceclaude import paths, prompts


class PromptTests(unittest.TestCase):
    def test_manager_prompt_override_lives_in_the_voice_claude_home(self):
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "manager-prompt.md"
        self.assertEqual(paths.MANAGER_PROMPT_FILE.name, "manager-prompt.md")
        self.assertEqual(paths.MANAGER_PROMPT_FILE.parent, paths.HOME_DIR)  # not inside the repo
        with mock.patch.object(paths, "MANAGER_PROMPT_FILE", f):
            self.assertTrue(prompts.manager_prompt("- Atlas").startswith(prompts.MANAGER_PROMPT_DEFAULT))
            f.write_text("  我自己的总机提示  \n", "utf-8")
            self.assertEqual(prompts.manager_prompt("- Atlas"), "我自己的总机提示\n\n项目清单：\n- Atlas")
            f.write_text("   \n", "utf-8")  # an empty file keeps the built-in prompt
            self.assertTrue(prompts.manager_prompt("").startswith(prompts.MANAGER_PROMPT_DEFAULT))
