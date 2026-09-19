"""MCP server tests: protocol, dispatch, and discovery-from-/renderers.

The server is stdlib-only; tests drive handle_message()/call_tool()
directly with the HTTP layer stubbed, plus one subprocess smoke test over
real stdio.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import mcp_server

FAKE_RENDERERS = [
    {"name": "text", "description": "words", "static": True,
     "params": {"text": {"type": "string", "required": True,
                         "help": "message to show"}},
     "inputs": {}},
    {"name": "beads", "description": "parade", "static": False,
     "params": {}, "inputs": {
         "focus": {"type": "object", "required": ["bead"],
                   "properties": {"bead": {"type": "string"},
                                  "note": {"type": "string"}}}}},
    {"name": "broken-thing", "broken": "nope"},
]


class McpTestCase(unittest.TestCase):
    def setUp(self):
        self._get = mcp_server.api_get
        self._post = mcp_server.api_post
        self.calls = []
        self.renderers = FAKE_RENDERERS
        self.addCleanup(self._restore)

    def _restore(self):
        mcp_server.api_get = self._get
        mcp_server.api_post = self._post

    def stub(self, get=None, post=None, unreachable=False):
        calls = self.calls

        def fake_get(path, raw=False):
            calls.append(("GET", path))
            if unreachable:
                raise mcp_server.DisplayUnreachable("refused")
            if get is not None:
                return get(path, raw)
            if path == "/renderers":
                return {"renderers": self.renderers}
            return {"ok": True, "path": path}

        def fake_post(path, body=None):
            calls.append(("POST", path, body))
            if unreachable:
                raise mcp_server.DisplayUnreachable("refused")
            if post is not None:
                return post(path, body)
            return {"ok": True, "path": path}

        mcp_server.api_get = fake_get
        mcp_server.api_post = fake_post

    def test_initialize_and_ping(self):
        resp = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "displayd-mcp")
        self.assertIn("tools", resp["result"]["capabilities"])
        resp = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(resp["result"], {})

    def test_unknown_method_and_bad_message(self):
        resp = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(resp["error"]["code"], -32601)
        resp = mcp_server.handle_message({"nope": True})
        self.assertEqual(resp["error"]["code"], -32600)
        # notifications get no reply
        resp = mcp_server.handle_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIsNone(resp)

    def test_core_tools_present(self):
        self.stub()
        resp = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        for expected in ("show", "notify", "feed", "state", "snapshot",
                         "renderers", "policy_get", "feedback_record",
                         "feedback_list", "feedback_summary"):
            self.assertIn(expected, names)

    def test_new_view_appears_without_code_change(self):
        """The acceptance demo at unit level: a renderer the server never
        heard of shows up as a tool straight from /renderers."""
        self.renderers = FAKE_RENDERERS + [
            {"name": "beacon", "description": "Test beacon", "static": True,
             "params": {"note": {"type": "string", "help": "what to say"}},
             "inputs": {"ping": {"type": "string"}}}]
        self.stub()
        resp = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertIn("show_beacon", names)
        self.assertIn("feed_beacon_ping", names)
        # and the dynamic show tool dispatches to POST /show
        result = mcp_server.call_tool("show_beacon", {"note": "hi"})
        self.assertNotIn("isError", result)
        method, path, body = self.calls[-1]
        self.assertEqual((method, path), ("POST", "/show"))
        self.assertEqual(body, {"renderer": "beacon",
                                "params": {"note": "hi"}})

    def test_dynamic_feed_inline_object(self):
        self.stub()
        result = mcp_server.call_tool("feed_beads_focus",
                                      {"bead": "task-1", "note": "n"})
        self.assertNotIn("isError", result)
        method, path, body = self.calls[-1]
        self.assertEqual((method, path), ("POST", "/feed/beads/focus"))
        self.assertEqual(body, {"bead": "task-1", "note": "n"})

    def test_broken_renderer_yields_no_tool(self):
        self.stub()
        resp = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertFalse([n for n in names if "broken" in n])

    def test_unreachable_is_loud(self):
        self.stub(unreachable=True)
        for name, args in (("state", {}), ("show", {"renderer": "text"}),
                           ("feed_status", {"view": "a", "input": "b"})):
            result = mcp_server.call_tool(name, args)
            self.assertTrue(result.get("isError"), name)
            self.assertIn("unreachable", result["content"][0]["text"])
        # feedback especially: the note is NOT recorded, says so plainly
        result = mcp_server.call_tool("feedback_record",
                                      {"view": "text", "rating": 4})
        self.assertTrue(result.get("isError"))
        text = result["content"][0]["text"]
        self.assertIn("NOT", text)
        self.assertIn("recorded", text)

    def test_display_error_surfaces(self):
        def post(path, body):
            raise mcp_server.DisplayError(400, "title is required")
        self.stub(post=post)
        result = mcp_server.call_tool("notify", {"title": ""})
        self.assertTrue(result.get("isError"))
        self.assertIn("400", result["content"][0]["text"])

    def test_snapshot_image_block(self):
        def get(path, raw):
            if path == "/snapshot":
                return b"\x89PNG-data"
            return {"ok": True}
        self.stub(get=get)
        result = mcp_server.call_tool("snapshot", {})
        content = result["content"][0]
        self.assertEqual(content["type"], "image")
        self.assertEqual(content["mimeType"], "image/png")

        def get404(path, raw):
            raise mcp_server.DisplayError(404, "nothing has been drawn yet")
        self.stub(get=get404)
        result = mcp_server.call_tool("snapshot", {})
        self.assertTrue(result.get("isError"))

    def test_feedback_tools_dispatch(self):
        seen = {}

        def post(path, body):
            seen["post"] = (path, body)
            return {"id": "fb-1"}
        self.stub(post=post)
        result = mcp_server.call_tool(
            "feedback_record",
            {"view": "text", "rating": 5, "categories": ["readability"],
             "notes": "great", "params": {"text": "hi"}, "agent": "tester"})
        self.assertNotIn("isError", result)
        self.assertEqual(seen["post"][0], "/feedback")
        self.assertEqual(seen["post"][1]["rating"], 5)

        def get(path, raw):
            seen["get"] = path
            return {"feedback": [], "count": 0}
        self.stub(get=get)
        mcp_server.call_tool("feedback_list", {"view": "text", "limit": 10})
        self.assertEqual(seen["get"], "/feedback?view=text&limit=10")
        mcp_server.call_tool("feedback_summary", {})
        self.assertEqual(seen["get"], "/feedback/summary")

    def test_unknown_tool(self):
        self.stub()
        result = mcp_server.call_tool("teleport", {})
        self.assertTrue(result.get("isError"))

    def test_stdio_smoke(self):
        """The real binary over real stdio against a dead port: initialize
        works and tools/list still yields the static core tools."""
        env = dict(os.environ, DISPLAYD_URL="http://127.0.0.1:1",
                   DISPLAYD_TIMEOUT="2")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(__file__),
                                          os.pardir, "mcp_server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)
        try:
            msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
            out, _ = proc.communicate(
                "\n".join(json.dumps(m) for m in msgs) + "\n", timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        names = {t["name"] for t in lines[1]["result"]["tools"]}
        for expected in ("show", "notify", "feed", "snapshot",
                         "feedback_record", "feedback_summary"):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
