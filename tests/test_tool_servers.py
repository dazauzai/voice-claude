import asyncio
import unittest
from unittest import mock

from voiceclaude import manager_server, mcp_server
from voiceclaude.control import ControlServer

SAMPLE = {"string": "x", "integer": 12, "number": 1.0, "boolean": True, "array": ["a"]}


class ToolArgumentsTests(unittest.TestCase):
    """Every tool of both tool servers, run the way the MCP library runs it, reaches the service with its arguments.
    A tool argument named like the helper's own parameter (board_show's `path`) made every call crash, and the model
    saw only "Error executing tool board_show"."""

    def test_every_tool_reaches_the_service_with_its_arguments(self):
        for mod in (mcp_server, manager_server):
            seen = []
            srv = ControlServer(lambda path, data: (seen.append((path, data)), {"ok": True, "message": "好"})[1])
            try:
                with mock.patch.object(mod, "PORT", str(srv.port)):
                    tools = mod.server._tool_manager.list_tools()
                    self.assertTrue(tools)
                    for tool in tools:
                        props = tool.parameters.get("properties", {})
                        args = {k: SAMPLE[p.get("type", "string")] for k, p in props.items()}
                        seen.clear()
                        out = asyncio.run(tool.run(args, context=None))
                        self.assertEqual(out, "好", tool.name)
                        self.assertEqual(seen[0][0], tool.name)
                        self.assertLessEqual(set(args), set(seen[0][1]), tool.name)
            finally:
                srv.close()
        self.assertIn("board_show", [t.name for t in mcp_server.server._tool_manager.list_tools()])


if __name__ == "__main__":
    unittest.main()
