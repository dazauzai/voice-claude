import unittest
from unittest import mock

from voiceclaude import manager_server
from voiceclaude.control import ControlServer


class ManagerServerTests(unittest.TestCase):
    def test_tools_call_the_daemon_with_the_line(self):
        seen = []

        def handler(path, data):
            seen.append((path, data))
            if path == "switch_to" and data["project"] == "Atlas":
                return {"ok": False, "message": "有几个都像", "candidates": ["Atlas（E 盘）", "Atlas（D 盘）"]}
            return {"ok": True, "message": f"did {path}"}
        srv = ControlServer(handler)
        try:
            with mock.patch.object(manager_server, "PORT", str(srv.port)), mock.patch.object(manager_server, "LINE", "manager"):
                self.assertEqual(manager_server.list_projects(), "did list_projects")
                self.assertEqual(manager_server.switch_to("Atlas"), "有几个都像（候选：Atlas（E 盘）、Atlas（D 盘））")
                self.assertEqual(manager_server.new_project("联合分析", read_from=["Atlas", "Beacon"]), "did new_project")
                self.assertEqual(manager_server.background_status(), "did background_status")
                self.assertEqual(manager_server.remember_alias("服务器", "Atlas"), "did remember_alias")
        finally:
            srv.close()
        self.assertEqual(seen[0], ("list_projects", {"line": "manager"}))
        self.assertEqual(seen[2][1], {"name": "联合分析", "dir": "", "read_from": ["Atlas", "Beacon"], "line": "manager"})

    def test_no_daemon(self):
        with mock.patch.object(manager_server, "PORT", ""):
            self.assertIn("没有在运行", manager_server.list_projects())
        with mock.patch.object(manager_server, "PORT", "1"):
            self.assertIn("没有响应", manager_server.list_projects())


if __name__ == "__main__":
    unittest.main()
