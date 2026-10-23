import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from voiceclaude import access, config, jobs, paths, switchboard
from voiceclaude.managers import RuleManager, RulesSession
from voiceclaude.projects import Project
from voiceclaude.switchboard import MANAGER, Switchboard

from tests.test_switchboard import FakeSession


def wait_for(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class RulesManagerTests(unittest.TestCase):
    """The manager that needs no model at all: fixed phrases + fuzzy project names."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("HOME_DIR", "MANAGER_DIR", "MANAGER_LOG_DIR", "SCRATCH_DIR", "JOBS_DIR", "LINES_DIR"):
            mock.patch.object(paths, name, self.tmp / name.lower()).start()
        mock.patch.object(paths, "LINES_FILE", self.tmp / "lines.json").start()
        mock.patch.object(paths, "PROJECTS_FILE", self.tmp / "projects.toml").start()
        mock.patch.object(paths, "AUTH_FILE", self.tmp / "auth.json").start()
        mock.patch.object(access, "check_reach", lambda pid, cfg=None: (True, "")).start()  # no network in tests
        mock.patch.object(switchboard, "config_save", lambda *a, **k: None).start()
        (self.tmp / "atlas").mkdir()
        (self.tmp / "beacon").mkdir()
        self.projects = [Project(key="atlas", name="Atlas", display_name="Atlas", cwd=str(self.tmp / "atlas"), aliases=["服务器"]),
                         Project(key="beacon", name="Beacon", display_name="Beacon", cwd=str(self.tmp / "beacon"))]
        self.events = []
        self.jobs = jobs.Jobs(self.tmp / "jobs", self.tmp / "jobs.json", lambda j: None, poll_seconds=0.2)
        self.jobs.load()
        self.state = {"st": access.AccessStatus(False, "none", "没装 Claude Code，也没有 API key。")}
        cfg = config.Config()
        cfg.manager_engine = "auto"
        # FakeSession for every model line (the rules manager needs no factory at all): a unit test never
        # starts a real claude, and voiceclaude.testing would refuse it anyway
        self.sb = Switchboard(cfg, jobs=self.jobs, emit=lambda k, d: self.events.append((k, d)),
                              forward=lambda p, d: {"ok": True}, session_factory=FakeSession,
                              scan_projects=lambda: list(self.projects), ask_timeout=0.5,
                              detect_access=lambda cmd: self.state["st"])
        self.sb.start()

    def tearDown(self):
        self.jobs.close()
        mock.patch.stopall()

    def texts(self):
        return "".join(d["text"] for k, d in self.events if k == "text" and d["line"] == MANAGER)

    def test_no_access_gives_a_rules_manager_that_still_answers(self):
        self.assertIsInstance(self.sb.lines[MANAGER].session, RulesSession)
        self.assertIn("还没接入", self.sb.greeting())
        self.assertTrue(self.sb.say(MANAGER, "有哪些项目")["ok"])
        self.assertTrue(wait_for(lambda: "Atlas" in self.texts() and "Beacon" in self.texts()))
        self.assertTrue(wait_for(lambda: any(k == "done" and d["line"] == MANAGER for k, d in self.events)))

    def test_switch_to_a_provider_by_voice(self):
        self.assertTrue(self.sb.say(MANAGER, "切到 DeepSeek")["ok"])
        self.assertTrue(wait_for(lambda: "DeepSeek" in self.texts()))  # no key: it says what is missing
        self.assertIn("还没填", self.texts())
        self.assertEqual(self.sb.provider, "claude")
        self.sb.session_factory = FakeSession  # the real manager that follows must not be a real process here
        self.events.clear()
        def detect_provider(pid, cmd, cfg=None):
            return access.AccessStatus(pid == "deepseek", "deepseek_key" if pid == "deepseek" else "none", "ok",
                                       claude="c", provider=pid, provider_name="DeepSeek（深度求索）", model="deepseek-flash")
        self.sb.detect_provider = detect_provider
        # asked out loud like every provider switch: nobody answers -> nothing changes
        self.assertTrue(self.sb.say(MANAGER, "换成 deepseek")["ok"])
        self.assertTrue(wait_for(lambda: "不换" in self.texts()))
        self.assertTrue(any(k == "ask" and d["question"] == "要把默认换到DeepSeek（深度求索）（deepseek-flash）吗？" for k, d in self.events))
        self.assertEqual(self.sb.provider, "claude")

        def answer():
            self.assertTrue(wait_for(lambda: len([1 for k, d in self.events if k == "ask"]) >= 2))
            self.sb.answer([d for k, d in self.events if k == "ask"][-1]["request"], True)
        threading.Thread(target=answer, daemon=True).start()
        self.assertTrue(self.sb.say(MANAGER, "换成 deepseek")["ok"])
        self.assertTrue(wait_for(lambda: self.sb.provider == "deepseek" and isinstance(self.sb.lines[MANAGER].session, FakeSession)))
        self.assertIn("换到DeepSeek", self.texts())

    def test_switch_asks_then_explains_no_access(self):
        # "接我到 服务器" -> asks 要接到Atlas项目吗 -> yes -> but no access, so the line cannot start
        def answer():
            self.assertTrue(wait_for(lambda: any(k == "ask" for k, d in self.events)))
            ask = [d for k, d in self.events if k == "ask"][0]
            self.assertEqual(ask["question"], "要接到Atlas项目吗？")
            self.sb.answer(ask["request"], True)
        threading.Thread(target=answer, daemon=True).start()
        self.sb.say(MANAGER, "呃，接我到 服务器 那个")
        self.assertTrue(wait_for(lambda: "还没接入" in self.texts()))

    def test_unknown_and_ambiguous(self):
        self.sb.say(MANAGER, "接我到火星")
        self.assertTrue(wait_for(lambda: "没有找到" in self.texts()))
        self.events.clear()
        self.sb.say(MANAGER, "后台怎么样了")
        self.assertTrue(wait_for(lambda: "后台没有东西" in self.texts()))

    def test_real_brain_takes_over_when_access_arrives(self):
        self.state["st"] = access.AccessStatus(True, "api_key", "用 API key（…mnop）接入。", claude="c")
        with mock.patch.object(access, "set_api_key", lambda key: self.state["st"]):
            r = self.sb.set_api_key("sk-ant-api03-abcdefghijklmnop")
        self.assertTrue(r["ok"])
        self.assertTrue(wait_for(lambda: isinstance(self.sb.lines[MANAGER].session, FakeSession)))  # the real brain, faked
        self.assertEqual(self.sb.greeting(), "我是总机，要去哪个项目？")

    def test_rules_can_be_forced_even_with_access(self):
        self.state["st"] = access.AccessStatus(True, "subscription", "ok", claude="c")
        cfg = config.Config()
        cfg.manager_engine = "rules"
        sb = Switchboard(cfg, jobs=self.jobs, emit=lambda k, d: None, forward=lambda p, d: {"ok": True},
                         session_factory=FakeSession, scan_projects=lambda: list(self.projects),
                         detect_access=lambda cmd: self.state["st"])
        sb.start()
        self.assertIsInstance(sb.lines[MANAGER].session, RulesSession)
        self.assertEqual(sb.manager_brain(), "rules")


class RuleManagerUnitTests(unittest.TestCase):
    def test_intents(self):
        calls = []

        class SB:
            index = [Project(key="a", name="Atlas", display_name="Atlas", cwd="x")]

            def background_status_text(self):
                return "后台没有东西。"

            def remember_alias(self, alias, project):
                calls.append(("alias", alias))
                return {"ok": True, "message": f"记住了 {alias}"}

            def new_project(self, name, d, refs):
                calls.append(("new", name))
                return {"ok": True, "message": f"建了 {name}"}

            def switch_to_project(self, text, confirm=False):
                calls.append(("switch", text, confirm))
                return {"ok": False, "message": f"没有 {text}"}
        rm = RuleManager(SB())
        self.assertEqual(rm.handle("后台怎么样了？"), "后台没有东西。")
        self.assertIn("Atlas", rm.handle("有哪些项目"))
        self.assertEqual(rm.handle("以后叫它 服务器"), "记住了 服务器")
        self.assertEqual(rm.handle("新建一个叫联合分析的项目"), "建了 联合分析")
        self.assertEqual(rm.handle("接我到 Atlas 那个项目吧"), "没有 Atlas")
        self.assertEqual(calls[-1], ("switch", "Atlas", True))
        self.assertEqual(rm.handle("。"), "没听清，要去哪个项目？")


if __name__ == "__main__":
    unittest.main()
