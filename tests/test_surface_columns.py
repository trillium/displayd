"""Event streams, columns and the panel config block.

Streams (what happens), surfaces/regions (where it shows) and columns (the
binding of one to the other) are separate concepts; these tests keep them
separate: one stream in columns on several surfaces, several columns composed
on one surface, stream-specific follow-on actions, targeting by column /
stream / panel, and the panel id staying distinct from the surface id.

Pure tests use a fake clock; the wire tests run a headless daemon
(DISPLAYD_FAKE_FB=1) through the helpers in test_surfaces_http.py.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import surface_columns as columns
from surface_hub import SurfaceHub
from surface_model import SurfaceError
from surface_target import resolve
from test_surfaces import Clock, ipad, kiosk, phone
from test_surfaces_http import SurfaceHttpTestCase, sidecar

AGENT = {"id": "agent", "name": "Agent activity", "event_types": ["step", "note"],
         "views": ["log", "timeline"],
         "actions": [{"id": "approve", "label": "Approve"},
                     {"id": "stop", "label": "Stop"}]}
TOOLS = {"id": "tools", "event_types": ["call"], "views": ["calls"]}


def with_columns(spec, cols, panel_id=None):
    return dict(spec, panel={"id": panel_id, "config": {"columns": cols}})


def wide(**over):
    """A wide Sidecar with three side-by-side regions."""
    spec = ipad(id="wall", viewport={"width": 1800, "height": 600}, regions=[
        {"name": "c1", "rect": {"x": 0, "y": 0, "w": 600, "h": 600}},
        {"name": "c2", "rect": {"x": 600, "y": 0, "w": 600, "h": 600}},
        {"name": "c3", "rect": {"x": 1200, "y": 0, "w": 600, "h": 600}}])
    spec.update(over)
    return spec


class TestStreams(unittest.TestCase):
    def setUp(self):
        self.hub = SurfaceHub(clock=Clock())
        self.streams = self.hub.streams

    def test_declare_publish_read_with_seq(self):
        stream, created = self.streams.declare(AGENT)
        self.assertTrue(created)
        self.assertEqual(stream["views"], ["log", "timeline"])
        for n in range(3):
            self.streams.publish("agent", {"type": "step", "data": {"n": n}})
        got = self.streams.read("agent", since=1)
        self.assertEqual([e["data"]["n"] for e in got["events"]], [1, 2])
        self.assertEqual(self.streams.read("agent", since=3)["events"], [])
        self.assertEqual(self.streams.get("agent")["seq"], 3)

    def test_redeclare_keeps_events(self):
        self.streams.declare(AGENT)
        self.streams.publish("agent", {"type": "step", "data": 1})
        _, created = self.streams.declare(dict(AGENT, name="Renamed"))
        self.assertFalse(created)
        self.assertEqual(self.streams.read("agent")["events"][0]["data"], 1)

    def test_errors(self):
        self.streams.declare(AGENT)
        with self.assertRaises(SurfaceError) as ctx:
            self.streams.publish("agent", {"type": "weather"})
        self.assertEqual(ctx.exception.status, 415)
        with self.assertRaises(SurfaceError) as ctx:
            self.streams.publish("nope", {"type": "step"})
        self.assertEqual(ctx.exception.status, 404)
        for bad in ([], {"type": 3}, {"type": "step", "data": "x" * 70000}):
            with self.assertRaises(ValueError, msg=str(bad)[:20]):
                self.streams.publish("agent", bad)
        for bad in ({"id": "Bad Id"}, {"id": "a", "actions": [{"id": 1}]},
                    {"id": "a", "views": "log"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.streams.declare(bad)

    def test_ring_is_bounded(self):
        self.streams.declare(TOOLS)
        for n in range(300):
            self.streams.publish("tools", {"type": "call", "data": n})
        events = self.streams.read("tools")["events"]
        self.assertEqual((len(events), events[0]["seq"]), (256, 45))

    def test_remove(self):
        self.streams.declare(TOOLS)
        self.streams.remove("tools")
        self.assertEqual(self.streams.list(), [])
        with self.assertRaises(SurfaceError):
            self.streams.remove("tools")

    def test_actions_must_be_declared_and_are_read_by_seq(self):
        self.streams.declare(AGENT)
        self.streams.add_action("agent", {"action": "approve", "column": "c"})
        with self.assertRaises(SurfaceError) as ctx:
            self.streams.add_action("agent", {"action": "delete-all"})
        self.assertEqual(ctx.exception.status, 409)
        self.streams.declare(TOOLS)  # no actions at all
        with self.assertRaises(SurfaceError):
            self.streams.add_action("tools", {"action": "approve"})
        self.assertEqual(self.streams.read_actions("agent", since=1)["actions"], [])
        self.assertEqual(len(self.streams.read_actions("agent")["actions"]), 1)

    def test_long_poll_wakes_on_publish(self):
        self.streams.declare(AGENT)
        threading.Timer(0.15, lambda: self.streams.publish(
            "agent", {"type": "note", "data": "late"})).start()
        started = time.time()
        got = self.streams.read("agent", since=0, wait=5)
        self.assertEqual(got["events"][0]["data"], "late")
        self.assertLess(time.time() - started, 3)


class TestColumns(unittest.TestCase):
    def setUp(self):
        self.hub = SurfaceHub(clock=Clock())
        self.reg, self.streams = self.hub.registry, self.hub.streams
        self.streams.declare(AGENT)
        self.streams.declare(TOOLS)

    def cols(self, *pairs):
        return [{"id": cid, "stream": stream, "view": view, "region": region}
                for cid, stream, view, region in pairs]

    def test_several_columns_compose_on_one_surface(self):
        rec, _ = self.reg.register(with_columns(wide(), self.cols(
            ("agent", "agent", "log", "c1"), ("tools", "tools", "calls", "c2"),
            ("agent-tl", "agent", "timeline", "c3"))))
        self.assertEqual([c["region"] for c in rec["panel"]["config"]["columns"]],
                         ["c1", "c2", "c3"])
        view = columns.describe(rec, self.streams)
        self.assertEqual([c["view_supported"] for c in view["columns"]],
                         [True, True, True])
        self.assertEqual(view["columns"][0]["stream_info"]["actions"][0]["id"],
                         "approve")

    def test_one_stream_on_surfaces_of_different_shape(self):
        self.reg.register(with_columns(phone(), self.cols(
            ("agent", "agent", "log", "full"))))
        self.reg.register(with_columns(kiosk(), self.cols(
            ("agent", "agent", "timeline", "full"))))
        got = self.reg.list({"stream": "agent"})
        self.assertEqual([s["id"] for s in got], ["kiosk", "phone"])
        self.assertEqual(self.reg.list({"stream": "tools"}), [])

    def test_default_region_is_filled_and_view_support_is_reported(self):
        rec, _ = self.reg.register(with_columns(phone(), [
            {"id": "a", "stream": "agent", "view": "hologram"}]))
        self.assertEqual(rec["panel"]["config"]["columns"][0]["region"], "full")
        col = columns.describe(rec, self.streams)["columns"][0]
        self.assertFalse(col["view_supported"])  # stream does not offer it
        rec, _ = self.reg.register(with_columns(phone(id="p2"), [
            {"id": "a", "stream": "not-declared-yet", "view": "log"}]))
        col = columns.describe(rec, self.streams)["columns"][0]
        self.assertEqual((col["stream_info"], col["view_supported"]), (None, False))

    def test_bad_columns_are_refused_whole(self):
        regions_msg = "has no region"
        cases = [
            ([{"id": "a", "stream": "s", "view": "v", "region": "nope"}], regions_msg),
            ([{"id": "a", "stream": "s", "view": "v", "region": "c1"},
              {"id": "b", "stream": "s", "view": "v", "region": "c1"}], "duplicate"),
            ([{"id": "a", "stream": "s", "view": "v", "region": "c1"},
              {"id": "a", "stream": "s", "view": "v", "region": "c2"}], "duplicate"),
            ([{"id": "a", "stream": "s", "view": "v"}], "name one"),
            ([{"id": "a", "view": "v", "region": "c1"}], "stream"),
            ([{"id": "A b", "stream": "s", "view": "v", "region": "c1"}], "id"),
            ("nope", "list"),
            ([{"id": "a", "stream": "s", "view": "v", "region": "c1",
               "options": {"x": "y" * 3000}}], "options"),
        ]
        for cols, needle in cases:
            with self.assertRaises(ValueError, msg=str(cols)[:40]) as ctx:
                self.reg.register(with_columns(wide(), cols))
            self.assertIn(needle, str(ctx.exception))
        self.assertEqual(self.reg.list(), [])

    def test_update_merges_panel_config_and_is_atomic(self):
        self.reg.register(with_columns(
            wide(), self.cols(("agent", "agent", "log", "c1")), panel_id="studio-wall"))
        rec = self.reg.update("wall", {"panel": {"config": {"note": "keep"}}})
        self.assertEqual(rec["panel"]["config"]["note"], "keep")
        self.assertEqual(len(rec["panel"]["config"]["columns"]), 1)  # merged
        rec = self.reg.update("wall", {"panel": {"config": {"columns": self.cols(
            ("tools", "tools", "calls", "c2"))}}})
        self.assertEqual(rec["panel"]["config"]["columns"][0]["id"], "tools")
        self.assertEqual(rec["panel"]["id"], "studio-wall")
        before = self.reg.get("wall")
        with self.assertRaises(ValueError):  # region c2 would vanish
            self.reg.update("wall", {"regions": [
                {"name": "c1", "rect": {"x": 0, "y": 0, "w": 600, "h": 600}}]})
        self.assertEqual(self.reg.get("wall"), before)

    def test_panel_id_is_distinct_from_surface_id(self):
        self.reg.register(dict(phone(id="tab-1"), panel={"id": "kitchen"}))
        self.reg.register(dict(phone(id="tab-2"), panel={"id": "kitchen"}))
        self.reg.register(phone(id="tab-3"))
        self.assertEqual([s["id"] for s in self.reg.list({"panel": "kitchen"})],
                         ["tab-1", "tab-2"])
        anon = self.reg.get("tab-3")["panel"]
        self.assertEqual((anon["id"], anon["version"], anon["config"]),
                         (None, 1, {"columns": []}))
        with self.assertRaises(ValueError):
            self.reg.register(dict(phone(id="x"), panel={"id": "Not A Token"}))
        with self.assertRaises(ValueError):
            self.reg.register(dict(phone(id="x"), panel={
                "config": {"blob": "z" * 20000}}))

    def test_target_a_column(self):
        self.reg.register(with_columns(wide(), self.cols(
            ("agent", "agent", "log", "c1"), ("tools", "tools", "calls", "c3"))))
        (surface, region), = resolve(self.reg, {"surface": "wall", "column": "tools"})
        self.assertEqual(region["name"], "c3")
        with self.assertRaises(SurfaceError) as ctx:
            resolve(self.reg, {"surface": "wall", "column": "nope"})
        self.assertEqual(ctx.exception.status, 404)
        for bad in ({"surface": "wall", "column": "a", "region": "c1"},
                    {"select": {}, "column": "a"}):
            with self.assertRaises(ValueError):
                resolve(self.reg, bad)

    def test_content_to_a_column_lands_in_its_region_only(self):
        self.reg.register(with_columns(wide(), self.cols(
            ("agent", "agent", "log", "c1"), ("tools", "tools", "calls", "c3"))))
        out = self.hub.deliver({"surface": "wall", "column": "tools"},
                               {"type": "text", "text": "x"})
        self.assertEqual(out["delivered"][0]["region"], "c3")
        got = self.reg.channels.read_content("wall")["contents"]
        self.assertEqual(list(got), ["c3"])


class TestColumnActions(unittest.TestCase):
    def setUp(self):
        self.hub = SurfaceHub(clock=Clock())
        self.reg, self.streams = self.hub.registry, self.hub.streams
        self.streams.declare(AGENT)
        self.streams.declare(TOOLS)
        self.reg.register(with_columns(wide(), [
            {"id": "agent", "stream": "agent", "view": "log", "region": "c1"},
            {"id": "tools", "stream": "tools", "view": "calls", "region": "c2"}],
            panel_id="studio-wall"))

    def act(self, column, body):
        return columns.act(self.reg, self.streams, "wall", column, body)

    def test_follow_on_action_reaches_the_producer_with_its_context(self):
        ev = self.streams.publish("agent", {"type": "step", "data": "deploy?"})
        out = self.act("agent", {"action": "approve", "event_seq": ev["seq"],
                                 "value": {"why": "ok"}})
        self.assertTrue(out["ok"])
        got, = self.streams.read_actions("agent")["actions"]
        self.assertEqual((got["action"], got["column"], got["surface"],
                          got["panel"], got["region"], got["event_seq"]),
                         ("approve", "agent", "wall", "studio-wall", "c1", 1))
        self.assertEqual(got["value"], {"why": "ok"})

    def test_actions_are_stream_specific(self):
        with self.assertRaises(SurfaceError) as ctx:
            self.act("tools", {"action": "approve"})  # tools offers none
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(SurfaceError) as ctx:
            self.act("agent", {"action": "approve"} if False else {"action": "bogus"})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.streams.read_actions("agent")["actions"], [])

    def test_unknown_column_surface_and_bad_body(self):
        with self.assertRaises(SurfaceError) as ctx:
            self.act("nope", {"action": "approve"})
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(SurfaceError):
            columns.act(self.reg, self.streams, "gone", "agent", {"action": "x"})
        for body in ("x", {"action": 3}, {"action": "approve", "event_seq": "1"}):
            with self.assertRaises(ValueError, msg=str(body)):
                self.act("agent", body)

    def test_stream_removed_after_column_bound(self):
        self.streams.remove("agent")
        with self.assertRaises(SurfaceError) as ctx:
            self.act("agent", {"action": "approve"})
        self.assertEqual(ctx.exception.status, 404)


class TestColumnsHttp(SurfaceHttpTestCase):
    """Two Sidecars of different shape, one stream, stream-specific UI."""

    def setUp(self):
        super().setUp()
        self.call("POST", "/event-streams", AGENT)
        self.call("POST", "/event-streams", TOOLS)

    def register_both(self):
        self.call("POST", "/surfaces", with_columns(
            sidecar("phone"), [{"id": "agent", "stream": "agent", "view": "log"}],
            panel_id="my-phone"))
        wall = sidecar("wall", viewport={"width": 1800, "height": 600},
                       regions=[{"name": "c1", "rect": {"x": 0, "y": 0, "w": 900, "h": 600}},
                                {"name": "c2", "rect": {"x": 900, "y": 0, "w": 900, "h": 600}}])
        self.call("POST", "/surfaces", with_columns(wall, [
            {"id": "agent", "stream": "agent", "view": "timeline", "region": "c1"},
            {"id": "tools", "stream": "tools", "view": "calls", "region": "c2"}]))

    def test_stream_declare_publish_subscribe_over_http(self):
        self.assertEqual(self.call("POST", "/event-streams", AGENT)[0], 200)
        code, out = self.call("POST", "/event-streams/agent/events",
                              {"type": "step", "data": {"msg": "started"}})
        self.assertEqual((code, out["event"]["seq"]), (200, 1))
        code, got = self.call("GET", "/event-streams/agent/events?since=0")
        self.assertEqual(got["events"][0]["data"], {"msg": "started"})
        self.assertEqual([s["id"] for s in self.call("GET", "/event-streams")[1]["streams"]],
                         ["agent", "tools"])
        self.assertEqual(self.call("GET", "/event-streams/agent")[1]["seq"], 1)

    def test_stream_errors_over_http(self):
        for method, path, body, status in (
                ("POST", "/event-streams", {"id": "Bad Id"}, 400),
                ("POST", "/event-streams/nope/events", {"type": "step"}, 404),
                ("POST", "/event-streams/agent/events", {"type": "weather"}, 415),
                ("GET", "/event-streams/nope", None, 404),
                ("GET", "/event-streams/agent/events?since=x", None, 400),
                ("GET", "/event-streams/agent/nonsense", None, 404),
                ("DELETE", "/event-streams/nope", None, 404)):
            self.assertEqual(self.call(method, path, body)[0], status, path)
        self.assertEqual(self.call("DELETE", "/event-streams/tools")[0], 200)

    def test_event_stream_token_gate(self):
        from unittest import mock
        import displayd
        with mock.patch.object(displayd, "API_TOKEN", b"s3cret"):
            for method, path, body in (("GET", "/event-streams", None),
                                       ("POST", "/event-streams", AGENT),
                                       ("DELETE", "/event-streams/agent", None)):
                self.assertEqual(self.call(method, path, body)[0], 401, path)
            self.assertEqual(self.call("GET", "/event-streams", token="s3cret")[0], 200)

    def test_columns_view_and_actions_over_http(self):
        self.register_both()
        code, view = self.call("GET", "/surfaces/wall/columns")
        self.assertEqual(code, 200, view)
        self.assertEqual([(c["id"], c["region"], c["view_supported"])
                          for c in view["columns"]],
                         [("agent", "c1", True), ("tools", "c2", True)])
        code, phone_view = self.call("GET", "/surfaces/phone/columns")
        self.assertEqual(phone_view["panel"], "my-phone")
        self.assertEqual(phone_view["columns"][0]["region"], "full")
        # the same stream feeds both columns: one publish, both subscribers see it
        self.call("POST", "/event-streams/agent/events", {"type": "step", "data": 1})
        for sid in ("phone", "wall"):
            col = self.call("GET", "/surfaces/%s/columns" % sid)[1]["columns"][0]
            self.assertEqual(col["stream_info"]["seq"], 1, sid)
        # stream-specific follow-on UI: only declared actions, per stream
        code, out = self.call("POST", "/surfaces/wall/columns/agent/action",
                              {"action": "approve", "event_seq": 1})
        self.assertEqual(code, 200, out)
        self.assertEqual(self.call("POST", "/surfaces/wall/columns/tools/action",
                                   {"action": "approve"})[0], 409)
        self.assertEqual(self.call("POST", "/surfaces/wall/columns/zzz/action",
                                   {"action": "approve"})[0], 404)
        code, acts = self.call("GET", "/event-streams/agent/actions")
        self.assertEqual([(a["action"], a["surface"], a["column"])
                          for a in acts["actions"]], [("approve", "wall", "agent")])

    def test_replace_columns_and_target_by_column_and_stream(self):
        self.register_both()
        code, out = self.call("POST", "/surfaces/wall/columns", {"columns": [
            {"id": "only", "stream": "tools", "view": "calls", "region": "c2"}]})
        self.assertEqual(code, 200, out)
        self.assertEqual([c["id"] for c in out["columns"]], ["only"])
        self.assertEqual(self.call("POST", "/surfaces/wall/columns",
                                   {"columns": [{"id": "x"}]})[0], 400)
        code, out = self.call("POST", "/surfaces/content", {
            "target": {"surface": "wall", "column": "only"},
            "content": {"type": "text", "text": "hi"}})
        self.assertEqual(out["delivered"][0]["region"], "c2")
        self.assertEqual(self.call("GET", "/surfaces?stream=tools")[1]["surfaces"][0]["id"],
                         "wall")
        self.assertEqual([s["id"] for s in
                          self.call("GET", "/surfaces?panel=my-phone")[1]["surfaces"]],
                         ["phone"])
        code, out = self.call("POST", "/surfaces/content", {
            "target": {"surface": "wall", "column": "nope"},
            "content": {"type": "text"}})
        self.assertEqual(code, 404)

    def test_touch_selects_a_column_action_end_to_end(self):
        """A tap lands in a column's region; the viewer's follow-on action for
        that column goes to the stream's producer."""
        self.register_both()
        code, out = self.call("POST", "/surfaces/wall/touch",
                              {"interaction": "tap", "x": 1000, "y": 100})
        self.assertEqual(out["event"]["region"], "c2")
        region = out["event"]["region"]
        column = [c for c in self.call("GET", "/surfaces/wall/columns")[1]["columns"]
                  if c["region"] == region][0]
        self.assertEqual(column["id"], "tools")
        self.assertEqual(self.call("POST", "/surfaces/wall/columns/agent/action",
                                   {"action": "stop"})[0], 200)


if __name__ == "__main__":
    unittest.main()
