import tempfile
import tomllib
import unittest
from pathlib import Path

from voiceclaude import config

SAMPLE = """# comment stays
[audio]
# which mic
microphone = ""
silence_seconds = 2.0

[claude]
model = "opus"
"""


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "config.toml"
        self.path.write_text(SAMPLE, "utf-8")

    def parsed(self):
        return tomllib.loads(self.path.read_text("utf-8"))

    def test_replace_existing_value(self):
        config.save("claude", "model", "sonnet", self.path)
        self.assertEqual(self.parsed()["claude"]["model"], "sonnet")
        self.assertIn("# which mic", self.path.read_text("utf-8"))  # comments survive

    def test_add_missing_key_and_section(self):
        config.save("audio", "speaker", "Speakers", self.path)
        config.save("ui", "font_size", 14, self.path)
        d = self.parsed()
        self.assertEqual(d["audio"]["speaker"], "Speakers")
        self.assertEqual(d["audio"]["silence_seconds"], 2.0)
        self.assertEqual(d["ui"]["font_size"], 14)

    def test_permissions_load_and_normalize(self):
        self.path.write_text(SAMPLE + '\n[permissions]\nscreenshot = "allow"\nclick = "nonsense"\n', "utf-8")
        cfg = config.load(self.path)
        self.assertEqual(cfg.permissions["screenshot"], "allow")
        self.assertEqual(cfg.permissions["click"], "ask")  # bad value -> default
        self.assertEqual(cfg.permissions["other_connectors"], "deny")
        for word, value in [("不用问", "allow"), ("每次问", "ask"), ("禁止", "deny"), ("ALLOW", "allow"), ("随便", None)]:
            self.assertEqual(config.normalize_permission_value(word), value, word)

    def test_defaults_for_missing_sections(self):
        cfg = config.load(self.path)  # SAMPLE has none of these sections
        self.assertEqual((cfg.hold_seconds, cfg.duck_seconds, cfg.cut_seconds), (0.8, 0.3, 1.5))
        self.assertEqual((cfg.daemon_port, cfg.max_lines, cfg.idle_minutes, cfg.start_with), (0, 3, 10, "manager"))
        self.assertEqual((cfg.manager_model, cfg.manager_effort), ("sonnet", "low"))
        self.assertEqual(cfg.permissions["background_job"], "ask")
        self.path.write_text(SAMPLE + '\n[hotkey]\nhold_seconds = 1.2\n[daemon]\nstart_with = "Atlas"\n[manager]\nmodel = "opus"\n', "utf-8")
        cfg = config.load(self.path)
        self.assertEqual((cfg.hold_seconds, cfg.start_with, cfg.manager_model), (1.2, "Atlas", "opus"))

    def test_providers_section(self):
        from voiceclaude import access
        cfg = config.load(self.path)
        self.assertEqual((cfg.provider, cfg.providers), ("claude", {}))
        self.assertEqual(cfg.permissions["switch_provider"], "ask")
        self.assertEqual((cfg.permissions["codex_shell"], cfg.permissions["codex_edit"]), ("ask", "ask"))
        self.assertFalse(cfg.transparent)
        self.path.write_text(SAMPLE + '\n[permissions]\ntransparent = true\nscreenshot = "allow"\n', "utf-8")
        cfg = config.load(self.path)
        self.assertTrue(cfg.transparent)
        self.assertEqual(cfg.permissions["screenshot"], "allow")
        self.assertNotIn("transparent", cfg.permissions)
        config.save("permissions", "transparent", False, self.path)
        self.assertFalse(config.load(self.path).transparent)
        self.path.write_text(SAMPLE + '\n[providers]\ndefault = "DeepSeek"\n[providers.deepseek]\nmodel = "deepseek-v4-pro"\nthinking = false\n', "utf-8")
        cfg = config.load(self.path)
        self.assertEqual(cfg.provider, "deepseek")
        p = access.provider_for("deepseek", cfg)
        self.assertEqual((p.model, p.thinking), ("deepseek-v4-pro", False))
        config.save("providers", "default", "claude", self.path)
        self.assertEqual(config.load(self.path).provider, "claude")

    def test_first_run_copies_the_repo_config_to_the_user_dir(self):
        from unittest import mock
        from voiceclaude import paths
        tmp = Path(tempfile.mkdtemp())
        repo = tmp / "repo" / "config.toml"
        repo.parent.mkdir()
        repo.write_text('[claude]\nmodel = "sonnet"\n', "utf-8")
        with mock.patch.object(paths, "CONFIG_FILE", tmp / "home" / "config.toml"), \
                mock.patch.object(paths, "REPO_CONFIG", repo), mock.patch.object(paths, "EXAMPLE_CONFIG", tmp / "none.toml"):
            self.assertEqual(config.load().model, "sonnet")  # copied from the config.toml next to the program
            self.assertTrue((tmp / "home" / "config.toml").exists())
            config.save("claude", "model", "opus")  # writes the user copy, not the repo
            self.assertEqual(config.load().model, "opus")
            self.assertIn('model = "sonnet"', repo.read_text("utf-8"))

    def test_quotes_in_value(self):
        config.save("audio", "microphone", 'Mic "A" \\ B', self.path)
        self.assertEqual(self.parsed()["audio"]["microphone"], 'Mic "A" \\ B')


if __name__ == "__main__":
    unittest.main()
