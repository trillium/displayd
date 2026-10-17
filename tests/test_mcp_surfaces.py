"""MCP surface/stream/column/touch tools: routes pinned, then a real daemon.

``TestRoutes`` stubs the HTTP layer and pins the exact route each tool drives.
``TestAgainstDaemon`` points the real client at a headless daemon
(``DISPLAYD_FAKE_FB=1``) and walks a whole agent flow through ``call_tool``.
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.dirname(__file__))

import mcp_client  # noqa: E402
import mcp_server  # noqa: E402
import mcp_surface_tools as st  # noqa: E402
from test_surfaces_http import SurfaceHttpTestCase, sidecar  # noqa: E402


def body_of(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


class TestRoutes(unittest.TestCase):
    def run_tool(self, name, args):
        seen = []

        def get(path):
            seen.append(("GET", path))
            return {"ok": True}

        def post(path, body=None):
            seen.append(("POST", path, body))
            return {"ok": True}

        def delete(path):
            seen.append(("DELETE", path))
            return {"ok": True}

        found, _ = st.call(name, args, get, post, delete)
        self.assertTrue(found, name)
        return seen[0]

    def test_catalog_is_unique_and_listed_by_the_server(self):
        names = [t["name"] for t in st.tools()]
        self.assertEqual(len(names), len(set(names)))
        with mock.patch.object(mcp_server, "fetch_renderers", return_value=[]):
            listed = {t["name"] for t in mcp_server.list_tools()}
        self.assertTrue(set(names) <= listed)
        self.assertFalse([n for n in names if n.startswith("_")])
        for tool in st.tools():
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertNotIn("_route", tool)

    def test_every_tool_names_a_real_route(self):
        want = {
            "surface_get": ("GET", "/surfaces/phone"),
            "surface_heartbeat": ("POST", "/surfaces/phone/heartbeat", None),
            "surface_deregister": ("DELETE", "/surfaces/phone"),
            "surface_columns": ("GET", "/surfaces/phone/columns"),
            "stream_get": ("GET", "/event-streams/phone"),
            "stream_remove": ("DELETE", "/event-streams/phone"),
            "streams_list": ("GET", "/event-streams", ),
        }
        for name, route in want.items():
            self.assertEqual(self.run_tool(name, {"id": "phone"}), route, name)

    def test_bodies_and_queries(self):
        self.assertEqual(self.run_tool("surface_update", {"id": "p", "patch": {"a": 1}}),
                         ("POST", "/surfaces/p", {"a": 1}))
        self.assertEqual(self.run_tool("surface_register", {"spec": {"id": "p"}}),
                         ("POST", "/surfaces", {"id": "p"}))
        self.assertEqual(self.run_tool("surface_send", {"target": "p/full", "content": {"x": 1}}),
                         ("POST", "/surfaces/content",
                          {"target": "p/full", "content": {"x": 1}}))
        self.assertEqual(self.run_tool("surface_touch_report",
                                       {"id": "p", "event": {"interaction": "tap"}}),
                         ("POST", "/surfaces/p/touch", {"interaction": "tap"}))
        self.assertEqual(self.run_tool("surface_columns_set", {"id": "panel", "columns": []}),
                         ("POST", "/surfaces/panel/columns", {"columns": []}))
        self.assertEqual(self.run_tool("column_action",
                                       {"id": "s", "column": "c", "action": "approve",
                                        "event_seq": 3}),
                         ("POST", "/surfaces/s/columns/c/action",
                          {"action": "approve", "event_seq": 3}))
        self.assertEqual(self.run_tool("stream_publish", {"id": "a", "type": "t", "data": 5}),
                         ("POST", "/event-streams/a/events", {"type": "t", "data": 5}))
        self.assertEqual(self.run_tool("stream_declare", {"id": "a", "views": ["log"], "x": 1}),
                         ("POST", "/event-streams", {"id": "a", "views": ["log"]}))
        method, path = self.run_tool("surfaces_list",
                                     {"kind": "wall", "min_width": 1000,
                                      "labels": {"room": "a b"}})[:2]
        self.assertEqual(method, "GET")
        self.assertTrue(path.startswith("/surfaces?"))
        for part in ("kind=wall", "min_width=1000", "label.room=a+b"):
            self.assertIn(part, path)
        self.assertEqual(self.run_tool("surfaces_list", {})[1], "/surfaces")

    def test_ids_are_quoted_into_a_single_path_segment(self):
        self.assertEqual(self.run_tool("surface_get", {"id": "a/../b"})[1],
                         "/surfaces/a%2F..%2Fb")
        with self.assertRaises(ValueError):
            self.run_tool("surface_get", {})

    def test_long_poll_is_clamped_below_the_client_timeout(self):
        with mock.patch.object(st, "TIMEOUT", 10.0):
            path = self.run_tool("stream_events", {"id": "a", "since": 4, "wait": 600})[1]
        self.assertEqual(path, "/event-streams/a/events?since=4&wait=9.0")
        self.assertEqual(self.run_tool("surface_events", {"id": "a"})[1],
                         "/surfaces/a/events?since=0")

    def test_bad_arguments_are_a_tool_error_not_a_crash(self):
        result = mcp_server.call_tool("surface_update", {"id": "p", "patch": 5})
        self.assertTrue(result["isError"])
        self.assertIn("bad arguments", result["content"][0]["text"])

    def test_unknown_name_is_not_claimed(self):
        self.assertEqual(st.call("teleport", {}, None, None, None), (False, None))


class TestAgainstDaemon(SurfaceHttpTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(mcp_client, "DISPLAYD_URL", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tool(self, tool_name, **args):
        return mcp_server.call_tool(tool_name, args)

    def test_agent_flow_over_the_real_api(self):
        self.assertEqual(body_of(self.tool("surface_register", spec=sidecar()))["id"], "phone")
        ids = [s["id"] for s in body_of(self.tool("surfaces_list", touch="swipe"))["surfaces"]]
        self.assertEqual(ids, ["phone"])
        self.assertEqual(body_of(self.tool("surface_get", id="phone"))["kind"], "sidecar")

        sent = body_of(self.tool("surface_send", target="phone",
                                 content={"type": "text", "text": "hi"}))
        self.assertEqual(sent["delivered"][0]["surface"], "phone")

        body_of(self.tool("surface_touch_report", id="phone",
                          event={"interaction": "tap", "x": 5, "y": 5}))
        events = body_of(self.tool("surface_events", id="phone"))["events"]
        self.assertEqual([e["interaction"] for e in events], ["tap"])

        self.assertEqual(body_of(self.tool("surface_heartbeat", id="phone"))["id"], "phone")
        body_of(self.tool("surface_deregister", id="phone"))
        gone = self.tool("surface_get", id="phone")
        self.assertTrue(gone["isError"])
        self.assertIn("404", gone["content"][0]["text"])

    def test_streams_columns_and_actions(self):
        body_of(self.tool("stream_declare", id="agent", name="Agent", views=["log"],
                          event_types=["note"],
                          actions=[{"id": "approve", "label": "Approve"}]))
        self.assertEqual([s["id"] for s in body_of(self.tool("streams_list"))["streams"]],
                         ["agent"])
        body_of(self.tool("stream_publish", id="agent", type="note", data={"text": "one"}))
        refused = self.tool("stream_publish", id="agent", type="other")
        self.assertIn("415", refused["content"][0]["text"])
        read = body_of(self.tool("stream_events", id="agent", since=0))
        self.assertEqual([e["data"]["text"] for e in read["events"]], ["one"])

        body_of(self.tool("surface_register", spec=sidecar(
            "wall", regions=[{"name": "main", "rect": {"x": 0, "y": 0, "w": 390, "h": 844}}],
            panel={"config": {"columns": [
                {"id": "c1", "stream": "agent", "view": "log", "region": "main"}]}})))
        cols = body_of(self.tool("surface_columns", id="wall"))
        self.assertEqual(cols["columns"][0]["id"], "c1")
        body_of(self.tool("column_action", id="wall", column="c1", action="approve"))
        acts = body_of(self.tool("stream_actions", id="agent", since=0))["actions"]
        self.assertEqual([a["action"] for a in acts], ["approve"])

        body_of(self.tool("stream_remove", id="agent"))
        self.assertEqual(body_of(self.tool("streams_list"))["streams"], [])

    def test_panel_columns_are_settable_through_the_tool(self):
        body_of(self.tool("stream_declare", id="agent", views=["log"]))
        out = body_of(self.tool("surface_columns_set", id="panel", columns=[
            {"id": "c1", "stream": "agent", "view": "log", "region": "full"}]))
        self.assertEqual([c["id"] for c in out["columns"]], ["c1"])
        refused = self.tool("surface_update", id="panel", patch={"name": "x"})
        self.assertIn("409", refused["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
