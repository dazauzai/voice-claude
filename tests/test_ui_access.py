"""The AI 服务商 page: its pure helpers (no Tk) and the page against fake services of both shapes — a service without list_providers
(Claude alone, 7a commands) and a service that lists services (docs/protocol.md 7b) — and against the real
Switchboard. The Tk tests open a hidden root window and are skipped where no display exists."""
import time
import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

from voiceclaude import ui_access
from voiceclaude.ui_access import AccessPage, describe, describe_is_error, legacy_vendor, lines_text, short, state_of

OLD = {"ok": True, "method": "api_key", "message": "用 API key（…mnop）接入。", "account": "…mnop"}
NEW = {"ok": True, "method": "deepseek_key", "message": "用 DeepSeek（deepseek-flash，key …ab12）接入。", "account": "…ab12",
       "provider": "deepseek", "provider_name": "DeepSeek（深度求索）", "model": "deepseek-flash"}


def vendor(pid: str, name: str, **kw) -> dict:
    v = {"id": pid, "name": name, "configured": False, "account": "", "ok": False, "message": "没填 key", "model": "",
         "models": [], "installed": True, "install_hint": "", "is_default": False, "key_prefix": "sk-",
         "url": f"https://{pid}.example/keys", "subscription": pid == "claude",
         "fields": [{"key": "api_key", "label": f"{name} API key", "secret": True, "hint": ""}]}
    v.update(kw)
    return v


class LegacyClient:
    """A client / service with only the 7a commands."""

    def __init__(self):
        self.calls = []
        self.access = dict(OLD)

    def access_status(self):
        self.calls.append(("access_status",))
        return {"ok": True, "message": "检测了", "access": self.access}

    def set_api_key(self, key):
        self.calls.append(("set_api_key", key))
        return {"ok": True, "message": "存了", "access": self.access}

    def clear_api_key(self):
        self.calls.append(("clear_api_key",))
        self.access = {"ok": False, "method": "none", "message": "没有 key 了"}
        return {"ok": True, "message": "清了", "access": self.access}

    def test_access(self):
        self.calls.append(("test_access",))
        return {"ok": True, "message": "接通了", "seconds": 1.0, "access": self.access}


class VendorClient(LegacyClient):
    """A client / service that knows three services (7b)."""

    def __init__(self):
        super().__init__()
        self.default = "deepseek"
        self.state = {
            "claude": vendor("claude", "Claude（Anthropic）", key_prefix="sk-ant-"),
            "deepseek": vendor("deepseek", "DeepSeek（深度求索）", configured=True, account="…ab12", ok=True, message="能用",
                               model="deepseek-flash", models=["flash", "pro"]),
            "openai": vendor("openai", "GPT（OpenAI）", installed=False, install_hint="要先装翻译层：pip install litellm"),
        }
        self.access = dict(NEW)

    def list_providers(self):
        self.calls.append(("list_providers",))
        ps = [dict(v, is_default=(k == self.default)) for k, v in self.state.items()]
        return {"ok": True, "default": self.default, "providers": ps,
                "lines": [{"line": "manager", "name": "总机", "provider": self.default, "model": ""},
                          {"line": "p-1", "name": "voice-claude", "provider": "claude", "model": ""}]}

    def set_provider_key(self, pid, key, switch=False):
        self.calls.append(("set_provider_key", pid, key, switch))
        v = self.state[pid]
        v.update(configured=True, account="…" + key[-4:], ok=v["installed"])
        if switch and v["ok"]:
            self.default = pid
            self.access = dict(self.access, provider=pid)
        return {"ok": True, "message": "存了", "provider": v, "default": self.default, "access": self.access}

    def clear_provider_key(self, pid):
        self.calls.append(("clear_provider_key", pid))
        v = self.state[pid]
        v.update(configured=False, account="", ok=False)
        return {"ok": True, "message": "清了", "provider": v, "default": self.default, "access": self.access}

    def test_provider(self, pid):
        self.calls.append(("test_provider", pid))
        return {"ok": True, "message": f"{pid} 接通了", "seconds": 2.5, "provider": self.state[pid]}

    def switch_provider(self, pid, line=""):
        self.calls.append(("switch_provider", pid))
        if pid == "openai":
            return {"ok": False, "message": "无法切换到 GPT", "default": self.default, "access": self.access}
        self.default = pid
        self.access = dict(self.access, provider=pid)
        return {"ok": True, "message": "切了", "default": pid, "access": self.access}


class HelperTests(unittest.TestCase):
    def test_describe_is_one_short_line(self):
        self.assertEqual(describe(NEW), "DeepSeek · API key")
        self.assertEqual(describe(OLD), "Claude · API key")
        self.assertEqual(describe({"ok": True, "method": "subscription", "account": "a@b.c max"}), "Claude · 已登录")
        self.assertEqual(describe({"ok": True, "method": "openai_key", "account": "…9999", "provider": "openai"}), "GPT · API key")
        self.assertEqual(describe({"ok": False, "method": "none", "message": "没装 Claude Code"}), "未设置")
        self.assertEqual(describe(None), "正在读取…")
        self.assertTrue(describe_is_error({"ok": False}))
        self.assertFalse(describe_is_error(None))
        self.assertNotIn("ab12", describe(NEW))  # the settings row never shows any part of a key

    def test_states_and_names(self):
        self.assertEqual(short("DeepSeek（深度求索）"), "DeepSeek")
        self.assertEqual(state_of(vendor("x", "X")), ("未设置", False))
        self.assertEqual(state_of(vendor("x", "X", configured=True, account="…1234", ok=True)), ("已保存 …1234", False))
        self.assertEqual(state_of(vendor("x", "X", configured=True, account="…1234", ok=False)), ("已保存 …1234 · 不可用", True))
        self.assertEqual(state_of(vendor("claude", "C", ok=True, account="a@b.c max")), ("已登录 · a@b.c", False))
        self.assertEqual(state_of(vendor("openai", "G", installed=False)), ("需要安装 LiteLLM", True))
        # GPT also signs in through Codex: without a key, the missing LiteLLM is not the problem
        gpt = dict(installed=False, methods=["login", "key"], login_state="not_logged_in")
        self.assertEqual(state_of(vendor("openai", "G", **gpt)), ("未设置", False))
        self.assertEqual(state_of(vendor("openai", "G", configured=True, account="…ab12", **gpt)), ("需要安装 LiteLLM", True))
        # signed in to Codex: GPT answers through Codex, so the missing LiteLLM (the key route) does not matter
        self.assertEqual(state_of(vendor("openai", "G", installed=False, ok=True, login_state="logged_in", engine="codex",
                                         account="ChatGPT（Codex 已登录）")), ("已登录 · ChatGPT", False))

    def test_legacy_vendor_and_lines(self):
        v = legacy_vendor(OLD)
        self.assertEqual((v["id"], v["configured"], v["account"], v["ok"]), ("claude", True, "…mnop", True))
        vendors = [vendor("claude", "Claude（Anthropic）"), vendor("deepseek", "DeepSeek（深度求索）")]
        lines = [{"line": "manager", "name": "总机", "provider": "deepseek"}, {"line": "p-1", "name": "Atlas", "provider": "claude"}]
        self.assertEqual(lines_text(lines, vendors, "deepseek"), "Atlas 线路使用 Claude")
        self.assertEqual(lines_text(lines[:1], vendors, "deepseek"), "")


class TkPage(unittest.TestCase):
    """A hidden Tk root, the theme, and the helpers; the test classes below add the cases."""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:  # no display
            self.skipTest(f"no display: {e}")
        self.root.withdraw()
        from voiceclaude.ui_theme import Theme
        style = ttk.Style(self.root)
        style.layout("Flat.Vertical.TScrollbar", style.layout("Vertical.TScrollbar"))  # the app defines the real one
        self.theme = Theme(self.root, 12)
        self.posted = []
        self.backs = []
        self.accesses = []
        self.confirms = []
        self.said = []
        self.answer = True
        p = mock.patch.object(ui_access, "confirm", lambda parent, title, text: (self.confirms.append(text), self.answer)[1])
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.root.destroy()

    def page(self, client) -> AccessPage:
        p = AccessPage(self.root, self.theme, client, self.posted.append, lambda: self.backs.append(True), self.accesses.append,
                       self.said.append)
        p.pack(fill="both", expand=True)
        self.root.update()
        return p

    def settle(self, page: AccessPage):
        """Run the workers' finish on this (the UI) thread, like the window's queue does, until the page is quiet."""
        for _ in range(1000):
            while self.posted:
                self.posted.pop(0)()
            if not page.busy and page.inflight == 0:
                self.root.update()
                return
            time.sleep(0.01)
        self.fail("the page stayed busy")

    @staticmethod
    def radios(page: AccessPage) -> list[str]:
        return [row["radio"].value for row in page.rows.values()]

    @staticmethod
    def checked(page: AccessPage) -> list[str]:
        return [pid for pid, row in page.rows.items() if row["radio"].checked]

    @staticmethod
    def state(page: AccessPage, pid: str) -> str:
        return page.rows[pid]["state"].cget("text")

    @staticmethod
    def result(page: AccessPage, pid: str) -> str:
        return page.rows[pid]["result"].cget("text")

    @staticmethod
    def texts(page: AccessPage) -> str:
        out = [page.lines_label.cget("text"), page.message.cget("text")]
        for row in page.rows.values():
            out += [row["state"].cget("text"), row["radio"].cget("text"), row["result"].cget("text")]
        return "\n".join(out)


class PageTests(TkPage):
    def test_layout(self):
        client = VendorClient()
        page = self.page(client)
        page.show(NEW)
        self.settle(page)
        self.assertEqual(page.header.title.cget("text"), "AI 服务商")
        self.assertFalse(page.legacy)
        self.assertEqual(self.radios(page), ["claude", "deepseek", "openai"])
        self.assertEqual([r["radio"].cget("text") for r in page.rows.values()], ["Claude", "DeepSeek", "GPT"])
        self.assertEqual(self.checked(page), ["deepseek"])
        self.assertEqual((self.state(page, "claude"), self.state(page, "deepseek"), self.state(page, "openai")),
                         ("未设置", "已保存 …ab12", "需要安装 LiteLLM"))
        self.assertEqual(page.lines_label.cget("text"), "voice-claude 线路使用 Claude")
        self.assertIsNotNone(page.rows["claude"]["login"])
        self.assertIsNone(page.rows["deepseek"]["login"])
        # 保存 needs text; 删除 needs a saved key; no blue buttons on the page
        self.assertEqual(page.rows["claude"]["save"]._kind, "disabled")
        self.assertEqual(page.rows["claude"]["delete"]._kind, "disabled")
        self.assertEqual(page.rows["deepseek"]["delete"]._kind, "secondary")
        page.rows["claude"]["entry"].set_value("sk-ant-x")
        self.assertEqual(page.rows["claude"]["save"]._kind, "secondary")
        kinds = [r[b]._kind for r in page.rows.values() for b in ("save", "delete")] + [page.btn_test._kind, page.btn_detect._kind]
        self.assertNotIn("primary", kinds)
        self.assertEqual(self.accesses[-1].get("provider"), "deepseek")  # the window is told the access state

    def test_saving(self):
        client = VendorClient()
        page = self.page(client)
        page.show(NEW)
        self.settle(page)
        page._save("claude")  # empty: nothing happens
        page.rows["claude"]["entry"].set_value("abc")
        page._save("claude")
        self.assertEqual(self.result(page, "claude"), "API key 格式不正确，应以 sk-ant- 开头")
        self.assertEqual([c for c in client.calls if c[0] != "list_providers"], [])
        # a service that cannot answer yet: stored, the default stays
        page.rows["openai"]["entry"].set_value("sk-openai-test-0000")
        page._save("openai")
        self.settle(page)
        self.assertIn(("set_provider_key", "openai", "sk-openai-test-0000", True), client.calls)
        self.assertEqual(page.rows["openai"]["entry"].value(), "")
        self.assertEqual(self.result(page, "openai"), "已保存")
        self.assertEqual(self.checked(page), ["deepseek"])
        # one that can answer becomes the default, and says so under it
        page.rows["claude"]["entry"].set_value("sk-ant-test-00000000000000")
        self.assertIn("<Key-Return>", page.rows["claude"]["entry"].bind())  # Enter saves (a hidden window gets no keys)
        page._save("claude")
        self.settle(page)
        self.assertEqual((client.default, self.checked(page)), ("claude", ["claude"]))
        self.assertEqual(self.result(page, "claude"), "已保存，已切换到 Claude")
        self.assertEqual(self.state(page, "claude"), "已保存 …0000")
        self.assertNotIn("sk-ant-test", self.texts(page))
        self.assertNotIn("sk-openai-test", self.texts(page))

    def test_test_and_delete(self):
        client = VendorClient()
        page = self.page(client)
        page.show(NEW)
        self.settle(page)
        page._test()
        self.settle(page)
        self.assertIn(("test_provider", "deepseek"), client.calls)
        self.assertEqual(self.result(page, "deepseek"), "连接正常（2.5 秒）")
        self.answer = False
        page._delete("deepseek")
        self.assertEqual(self.confirms, ["删除 DeepSeek 的 API key？"])
        self.assertNotIn(("clear_provider_key", "deepseek"), client.calls)
        self.answer = True
        page._delete("deepseek")
        self.settle(page)
        self.assertIn(("clear_provider_key", "deepseek"), client.calls)
        self.assertEqual(self.state(page, "deepseek"), "未设置")
        page._delete("claude")  # nothing saved: no question, no call
        self.assertEqual(len(self.confirms), 2)

    def test_list_failures(self):
        client = VendorClient()
        page = self.page(client)
        page.show(NEW)
        self.settle(page)
        real = client.list_providers
        client.list_providers = lambda: (_ for _ in ()).throw(TimeoutError("后台服务 15 秒没有回应（list_providers）"))
        page.refresh()
        self.settle(page)
        self.assertEqual(self.radios(page), ["claude", "deepseek", "openai"])
        self.assertFalse(page.legacy)
        self.assertIn("无法读取服务商列表", page.message.cget("text"))
        client.list_providers = lambda: {"ok": False, "message": "不认识的命令 list_providers"}
        page.refresh()
        self.settle(page)
        self.assertTrue(page.legacy)
        self.assertEqual(list(page.rows), ["claude"])
        client.list_providers = real
        page.refresh()
        self.settle(page)
        self.assertEqual(self.radios(page), ["claude", "deepseek", "openai"])

    def test_picking(self):
        client = VendorClient()
        page = self.page(client)
        page.show(NEW)
        self.settle(page)
        # no key: the dot stays, the reason shows under that service, the cursor goes to its key box
        focused = []
        page.rows["claude"]["entry"].focus_set = lambda: focused.append("claude")  # a hidden window grants no focus
        page._on_pick("claude")
        self.assertEqual(self.checked(page), ["deepseek"])
        self.assertEqual(self.result(page, "claude"), "未登录，也未设置 API key")
        self.assertEqual(focused, ["claude"])
        page._on_pick("openai")
        self.assertIn("pip install", self.result(page, "openai"))
        self.assertNotIn(("switch_provider", "claude"), client.calls)
        # a usable one switches; the dot moves only when the service says it did
        client.state["claude"].update(ok=True, account="a@b.c max", message="已登录")
        page.refresh()
        self.settle(page)
        self.assertEqual(self.state(page, "claude"), "已登录 · a@b.c")
        page._on_pick("claude")
        self.assertEqual(self.checked(page), ["deepseek"])  # not yet
        self.settle(page)
        self.assertEqual(self.checked(page), ["claude"])
        self.assertEqual(self.result(page, "claude"), "已切换到 Claude")
        # a failed switch leaves the dot where it was
        client.state["openai"].update(ok=True, configured=True, installed=True, account="…9999")
        page.refresh()
        self.settle(page)
        page._on_pick("openai")
        self.settle(page)
        self.assertEqual(self.checked(page), ["claude"])
        self.assertEqual(self.result(page, "openai"), "无法切换到 GPT")
        # leaving the page clears the key boxes and the results
        page.rows["deepseek"]["entry"].set_value("sk-secret-0000000000000")
        page.on_hide()
        self.assertEqual(page.rows["deepseek"]["entry"].value(), "")
        self.assertEqual(self.result(page, "openai"), "")

    def test_login_button_per_service(self):
        """7b-1: services that also work through their own program's login (Claude Code, Codex) get 登录…. The
        service starts the login and returns at once (pending); the page watches for it to finish."""
        client = VendorClient()
        client.state["openai"].update(methods=["login", "key"], login_state="not_logged_in", installed=True)
        client.state["claude"].update(methods=["login", "key"])
        client.state["deepseek"].update(methods=["key"])
        calls = []
        reply = {"ok": True, "pending": True, "message": "已打开浏览器"}

        def login_provider(pid, mode="browser"):
            calls.append((pid, mode))
            return dict(reply, provider=client.state[pid], access=client.access)
        client.login_provider = login_provider
        page = self.page(client)
        page.show(NEW)
        self.settle(page)
        self.assertIsNotNone(page.rows["openai"]["login"])
        self.assertIsNotNone(page.rows["claude"]["login"])
        self.assertIsNone(page.rows["deepseek"]["login"])
        page._login("openai")
        self.settle(page)
        self.assertEqual(calls, [("openai", "browser")])
        self.assertEqual(self.result(page, "openai"), "请在打开的浏览器中完成登录")
        self.assertEqual(page.rows["openai"]["login"].cget("text"), "正在登录…")
        self.assertEqual(page.rows["openai"]["login"]._kind, "disabled")
        self.assertFalse(page.busy)  # the rest of the page still works while the browser is open
        self.assertEqual(page.rows["deepseek"]["delete"]._kind, "secondary")
        # the user finishes in the browser; the next look at the services sees it
        client.state["openai"].update(ok=True, account="ChatGPT（Codex 已登录）", login_state="logged_in")
        page.refresh()
        self.settle(page)
        page._watch_login("openai", 5)
        self.assertEqual(self.result(page, "openai"), "已登录")
        self.assertEqual(page.rows["openai"]["login"].cget("text"), "登录…")
        self.assertEqual(self.state(page, "openai"), "已登录 · ChatGPT")
        # the device-code way: the address and the code are shown and read aloud
        client.state["openai"].update(ok=False, account="", login_state="not_logged_in")
        page.refresh()
        self.settle(page)
        reply = {"ok": True, "pending": True, "code": "ABCD-1234", "url": "https://auth.openai.com/codex/device"}
        page._login("openai")
        self.settle(page)
        self.assertEqual(self.result(page, "openai"), "在 https://auth.openai.com/codex/device 输入 ABCD-1234")
        self.assertEqual(self.said, ["请在手机或浏览器上打开 https://auth.openai.com/codex/device，输入 ABCD-1234"])
        page._watch_login("openai", 0)  # nobody finished: the page says so and gives the button back
        self.assertIn("刷新", self.result(page, "openai"))
        self.assertEqual(page.rows["openai"]["login"].cget("text"), "登录…")
        # a refusal (a service without a login) is shown as it comes
        reply = {"ok": False, "message": "填 key 就行"}
        page._login("openai")
        self.settle(page)
        self.assertEqual(self.result(page, "openai"), "填 key 就行")

    def test_nothing_answers(self):
        client = VendorClient()
        client.state["deepseek"].update(ok=False, configured=False, account="")
        client.access = {"ok": False, "method": "none", "message": "还没接入", "provider": "deepseek"}
        page = self.page(client)
        page.show(client.access)
        self.settle(page)
        self.assertEqual(self.checked(page), [])  # no dot on a service that cannot answer

    def test_old_service_keeps_claude_alone(self):
        client = LegacyClient()
        page = self.page(client)
        page.show(OLD)
        self.settle(page)
        self.assertTrue(page.legacy)
        self.assertEqual(self.radios(page), ["claude"])
        self.assertEqual(self.state(page, "claude"), "已保存 …mnop")
        page.rows["claude"]["entry"].set_value("sk-ant-test-00000000000000")
        page._save("claude")
        self.settle(page)
        page._test()
        self.settle(page)
        self.assertEqual(client.calls, [("set_api_key", "sk-ant-test-00000000000000"), ("test_access",)])
        self.assertNotIn("sk-ant-test", self.texts(page))
        page._delete("claude")
        self.settle(page)
        self.assertEqual(client.calls[-1], ("clear_api_key",))
        self.assertEqual(self.state(page, "claude"), "未设置")
        self.assertEqual(self.checked(page), [])
        page.header.back.invoke()
        self.assertEqual(self.backs, [True])


class SwitchboardClient:
    """The daemon's command routing (daemon.py), in-process. The two calls that would start claude are refused."""

    def __init__(self, sb):
        self.sb = sb

    def access_status(self):
        return self.sb.access_status()

    def set_api_key(self, key):
        return self.sb.set_api_key(key)

    def clear_api_key(self):
        return self.sb.clear_api_key()

    def test_access(self):
        raise AssertionError("would start claude")

    def list_providers(self):
        return self.sb.list_providers()

    def set_provider_key(self, provider, key, switch=False):
        return self.sb.set_provider_key(provider, key, bool(switch))

    def clear_provider_key(self, provider):
        return self.sb.clear_provider_key(provider)

    def test_provider(self, provider):
        raise AssertionError("would start claude")

    def switch_provider(self, provider, line=""):
        return self.sb.switch_provider(provider, line or "")


class ContractTests(TkPage):
    """The page against the real switchboard (docs/protocol.md 7b). Everything lives in a temp directory."""

    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path
        try:
            from test_switchboard import FakeSession, project  # discovered with -s tests
        except ImportError:
            from tests.test_switchboard import FakeSession, project
        from voiceclaude import access, config, jobs, paths, switchboard
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("HOME_DIR", "MANAGER_DIR", "MANAGER_LOG_DIR", "SCRATCH_DIR", "JOBS_DIR", "LINES_DIR"):
            mock.patch.object(paths, name, self.tmp / name.lower()).start()
        mock.patch.object(paths, "LINES_FILE", self.tmp / "lines.json").start()
        mock.patch.object(paths, "PROJECTS_FILE", self.tmp / "projects.toml").start()
        mock.patch.object(paths, "AUTH_FILE", self.tmp / "auth.json").start()
        mock.patch.object(access, "_restrict_to_me", lambda p: None).start()
        mock.patch.object(access, "check_reach", lambda pid, cfg=None: (True, "")).start()  # no network from a unit test
        mock.patch.object(switchboard, "config_save", lambda *a, **k: None).start()
        cfg = config.Config()
        cfg.max_lines = 2
        (self.tmp / "atlas").mkdir()
        projects = [project("Atlas", str(self.tmp / "atlas"), aliases=["服务器"])]

        def detect_provider(pid, cmd, cfg=None):
            key = access.provider_key(pid)
            if pid == "deepseek" and key:
                return access.AccessStatus(True, "deepseek_key", "用 DeepSeek 接入。", claude="c", account=access.mask(key),
                                           provider="deepseek", provider_name="DeepSeek（深度求索）", model="deepseek-flash")
            return access.AccessStatus(False, "none", f"还没填 {pid} 的 API key。", provider=pid, provider_name=pid)
        self.jobs = jobs.Jobs(self.tmp / "jobs", self.tmp / "jobs.json", lambda j: None, poll_seconds=0.2)
        self.jobs.load()
        self.sb = switchboard.Switchboard(cfg, jobs=self.jobs, emit=lambda k, d: None, forward=lambda p, d: {"ok": True},
                                          session_factory=FakeSession, scan_projects=lambda: list(projects),
                                          detect_access=lambda cmd: access.AccessStatus(True, "subscription", "登录接入。", account="me@x.y pro"),
                                          now=lambda: 1000.0, ask_timeout=0.3)
        self.sb.detect_provider = detect_provider
        self.sb.start()

    def tearDown(self):
        self.jobs.close()
        mock.patch.stopall()
        super().tearDown()

    def test_page_against_the_real_switchboard(self):
        from voiceclaude import access
        page = self.page(SwitchboardClient(self.sb))
        page.show(self.sb.status()["access"])
        self.settle(page)
        self.assertFalse(page.legacy)
        self.assertEqual(self.radios(page), list(access.PROVIDER_ORDER))  # every service the switchboard knows, in its order
        self.assertTrue({"claude", "deepseek", "openai"} <= set(self.radios(page)))
        self.assertEqual(self.checked(page), ["claude"])
        self.assertEqual(page.rows["claude"]["radio"].cget("text"), "Claude")
        self.assertEqual(self.state(page, "claude"), "已登录 · me@x.y")
        self.assertEqual(self.state(page, "deepseek"), "未设置")
        self.assertEqual(page.lines_label.cget("text"), "")
        self.assertEqual(page.rows["deepseek"]["prefix"], "sk-")
        page.rows["deepseek"]["entry"].set_value("abc")
        page._save("deepseek")
        self.assertIn("sk-", self.result(page, "deepseek"))
        key = "sk-0123456789abcdef0123456789"
        page.rows["deepseek"]["entry"].set_value(key)
        page._save("deepseek")
        self.settle(page)
        self.assertEqual(access.provider_key("deepseek"), key)
        self.assertEqual(page.rows["deepseek"]["entry"].value(), "")
        self.assertEqual(self.state(page, "deepseek"), "已保存 …6789")
        self.assertNotIn(key, self.texts(page))
        self.assertEqual((self.sb.provider, self.checked(page)), ("deepseek", ["deepseek"]))
        self.assertEqual(self.result(page, "deepseek"), "已保存，已切换到 DeepSeek")
        self.assertEqual(describe(self.sb.status()["access"]), "DeepSeek · API key")
        page._on_pick("claude")
        self.settle(page)
        self.assertEqual((self.sb.provider, self.checked(page)), ("claude", ["claude"]))
        page._on_pick("openai")
        self.assertEqual(self.sb.provider, "claude")
        self.assertTrue(self.result(page, "openai"))
        page._delete("deepseek")
        self.settle(page)
        self.assertEqual(access.provider_key("deepseek"), "")
        self.assertEqual(self.state(page, "deepseek"), "未设置")
        self.assertEqual((self.sb.provider, self.checked(page)), ("claude", ["claude"]))


if __name__ == "__main__":
    unittest.main()
