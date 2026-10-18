"""One evening in the studio: several Sidecars and the panel at once.

The per-feature suites prove each piece alone. This walks the whole product
through the real headless daemon (DISPLAYD_FAKE_FB=1): a phone, an iPad and a
wall display register with different viewports and touch capabilities, two
streams feed columns composed differently on each, the panel draws its own
columns at its own scale, touches pick a column, a follow-on action reaches
the right producer, and a panel that moves to another surface gets its
configuration back. Simulated HTTP clients stand in for the Sidecars (no real
device is involved), and the panel's drawing is checked as renderer params,
not pixels.

Run from the repo root:  python3 -m unittest tests.test_multi_surface_scenario -v
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.dirname(__file__))

import surface_column_view as view  # noqa: E402
from test_surfaces_http import SurfaceHttpTestCase  # noqa: E402

AGENT = {"id": "agent", "name": "Agent", "event_types": ["say", "ask"],
         "views": ["log", "status"],
         "actions": [{"id": "approve", "label": "Approve"},
                     {"id": "stop", "label": "Stop"}]}
TOOLS = {"id": "tools", "name": "Tools", "event_types": ["call"],
         "views": ["calls"], "actions": [{"id": "retry", "label": "Retry"}]}


def surface(sid, w, h, regions=None, touch=None, **over):
    spec = {"id": sid, "kind": "sidecar",
            "renderer": {"name": "web", "content_types": ["text", "scene"]},
            "viewport": {"width": w, "height": h},
            "touch": touch or {"supported": False}, "ttl_seconds": 60}
    if regions:
        spec["regions"] = [{"name": n, "rect": {"x": x, "y": 0, "w": rw, "h": h},
                            "touch": bool(spec["touch"].get("supported"))}
                           for n, x, rw in regions]
    spec.update(over)
    return spec


def until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


class TestStudio(SurfaceHttpTestCase):
    def setUp(self):
        super().setUp()
        for decl in (AGENT, TOOLS):
            self.assertEqual(self.call("POST", "/event-streams", decl)[0], 201)
        self.register("phone", surface(
            "phone", 390, 844, touch={"supported": True, "interactions": ["tap", "swipe"]},
            panel={"id": "pocket", "config": {"scale": 1.5, "columns": [
                {"id": "agent", "stream": "agent", "view": "status"}]}}))
        self.register("ipad", surface(
            "ipad", 1024, 768, regions=[("left", 0, 512), ("right", 512, 512)],
            touch={"supported": True, "interactions": ["tap", "pinch"], "max_points": 2},
            panel={"id": "studio-ipad", "config": {"columns": [
                {"id": "agent", "stream": "agent", "view": "log", "region": "left"},
                {"id": "tools", "stream": "tools", "view": "calls", "region": "right"}]}}))
        self.register("wall", surface(
            "wall", 1920, 1080, regions=[("a", 0, 640), ("b", 640, 640), ("c", 1280, 640)],
            panel={"id": "wall-1", "config": {"scale": 0.75, "columns": [
                {"id": "agent", "stream": "agent", "view": "log", "region": "a"},
                {"id": "tools", "stream": "tools", "view": "calls", "region": "b"},
                {"id": "status", "stream": "agent", "view": "status", "region": "c"}]}}))

    def register(self, sid, spec):
        code, out = self.call("POST", "/surfaces", spec)
        self.assertEqual(code, 201, out)
        return out

    def publish(self, stream, etype, data):
        code, out = self.call("POST", "/event-streams/%s/events" % stream,
                              {"type": etype, "data": data})
        self.assertEqual(code, 200, out)

    def test_surfaces_differ_and_are_discoverable_by_what_they_can_do(self):
        self.assertEqual(self.ids(), ["panel", "ipad", "phone", "wall"])
        self.assertEqual(self.ids("?touch=pinch"), ["ipad"])
        self.assertEqual(self.ids("?touch=any"), ["ipad", "phone"])
        self.assertEqual(self.ids("?touch=none"), ["panel", "wall"])  # no touch service here
        self.assertEqual(self.ids("?min_width=1000&touch=none&kind=sidecar"), ["wall"])
        self.assertEqual(self.ids("?stream=tools"), ["ipad", "wall"])
        sizes = {s["id"]: (s["viewport"]["usable"]["w"], s["viewport"]["usable"]["h"])
                 for s in self.call("GET", "/surfaces")[1]["surfaces"]}
        self.assertEqual((sizes["phone"], sizes["ipad"], sizes["wall"]),
                         ((390, 844), (1024, 768), (1920, 1080)))

    def test_one_stream_feeds_columns_composed_differently_on_each_surface(self):
        self.publish("agent", "say", {"text": "hello"})
        for sid, expect in (("phone", ["agent"]), ("ipad", ["agent", "tools"]),
                            ("wall", ["agent", "tools", "status"])):
            cols = self.call("GET", "/surfaces/%s/columns" % sid)[1]["columns"]
            self.assertEqual([c["id"] for c in cols], expect, sid)
            self.assertTrue(all(c["view_supported"] for c in cols), sid)
        agent_views = {sid: self.call("GET", "/surfaces/%s/columns" % sid)[1]["columns"][0]["view"]
                       for sid in ("phone", "ipad", "wall")}
        self.assertEqual(agent_views, {"phone": "status", "ipad": "log", "wall": "log"})
        code, events = self.call("GET", "/event-streams/agent/events?since=0")
        self.assertEqual([e["data"]["text"] for e in events["events"]], ["hello"])

    def test_targets_name_a_surface_region_column_or_capability(self):
        def send(target, text):
            return self.call("POST", "/surfaces/content", {
                "target": target, "content": {"type": "text", "text": text}})

        code, out = send({"surface": "ipad", "column": "tools"}, "to-tools")
        self.assertEqual((code, out["delivered"][0]["region"]), (200, "right"))
        code, out = send("wall/c", "to-c")
        self.assertEqual((code, out["delivered"][0]["region"]), (200, "c"))
        code, out = send({"select": {"touch": "pinch"}, "region": "left"}, "pinchers")
        self.assertEqual([d["surface"] for d in out["delivered"]], ["ipad"])
        self.assertEqual(send("wall", "ambiguous")[0], 409)  # three regions: name one
        code, out = send({"select": {"kind": "sidecar", "region": "left"},
                          "region": "left", "all": True}, "everyone with a left")
        self.assertEqual([d["surface"] for d in out["delivered"]], ["ipad"])
        code, got = self.call("GET", "/surfaces/ipad/content?since=0&region=right")
        self.assertEqual(got["contents"]["right"]["content"]["text"], "to-tools")

    def test_touch_picks_a_column_and_its_action_reaches_that_producer(self):
        code, ev = self.call("POST", "/surfaces/ipad/touch",
                             {"interaction": "tap", "x": 700, "y": 100})
        self.assertEqual((code, ev["event"]["region"]), (200, "right"))
        self.assertEqual(ev["event"]["local"], {"x": 188, "y": 100})
        picked = [c for c in self.call("GET", "/surfaces/ipad/columns")[1]["columns"]
                  if c["region"] == ev["event"]["region"]][0]
        self.assertEqual(picked["stream"], "tools")
        code, out = self.call("POST", "/surfaces/ipad/columns/%s/action" % picked["id"],
                              {"action": "retry", "value": "last"})
        self.assertEqual(code, 200, out)
        acts = self.call("GET", "/event-streams/tools/actions")[1]["actions"]
        self.assertEqual([(a["action"], a["surface"], a["column"], a["region"])
                          for a in acts], [("retry", "ipad", "tools", "right")])
        self.assertEqual(self.call("GET", "/event-streams/agent/actions")[1]["actions"], [])
        # another stream's action is refused, and so are gestures a screen lacks
        self.assertEqual(self.call("POST", "/surfaces/ipad/columns/tools/action",
                                   {"action": "approve"})[0], 409)
        self.assertEqual(self.call("POST", "/surfaces/phone/touch",
                                   {"interaction": "pinch", "x": 5, "y": 5})[0], 409)
        self.assertEqual(self.call("POST", "/surfaces/wall/touch",
                                   {"interaction": "tap", "x": 5, "y": 5})[0], 409)
        self.assertEqual(self.call("POST", "/surfaces/ipad/touch",
                                   {"interaction": "tap", "x": 4000, "y": 5})[0], 400)

    def test_the_panel_draws_its_columns_at_its_own_scale(self):
        code, out = self.call("POST", "/layout", {"regions": [
            {"name": n, "width": w, "renderer": "solid", "params": {"color": "#102030"}}
            for n, w in (("left", "30%"), ("main", "70%"))]})
        self.assertEqual(code, 200, out)
        code, out = self.call("POST", "/surfaces/panel", {"panel": {"config": {
            "scale": 2, "columns": [
                {"id": "agent", "stream": "agent", "view": "log", "region": "left"},
                {"id": "tools", "stream": "tools", "view": "calls", "region": "main"}]}}})
        self.assertEqual(code, 200, out)
        self.publish("agent", "say", {"text": "hi"})
        names = {"left": self.daemon.layout[0]["name"], "main": self.daemon.layout[1]["name"]}

        def vars_of(region):
            params = self.daemon.region_threads[names[region]]["params"]
            return params.get("vars", {})

        self.assertTrue(until(lambda: vars_of("left").get("px_font")))
        self.assertEqual(vars_of("left")["px_font"], str(2 * view.FONT_PX))
        self.assertEqual(vars_of("main")["px_row"], str(2 * view.ROW_PX))
        # back to 1: both regions redraw smaller, no layout change needed
        self.assertEqual(self.call("POST", "/surfaces/panel", {"panel": {"config": {
            "scale": 1}}})[0], 200)
        self.assertTrue(until(lambda: vars_of("left").get("px_font") == str(view.FONT_PX)))
        self.assertEqual(self.call("POST", "/surfaces/panel", {"panel": {"config": {
            "scale": 7}}})[0], 400)

    def test_a_panel_that_moves_to_another_surface_gets_its_config_back(self):
        self.assertEqual(self.call("DELETE", "/surfaces/ipad")[0], 200)
        code, out = self.call("POST", "/surfaces", surface(
            "ipad-pro", 1366, 1024, regions=[("left", 0, 683), ("right", 683, 683)],
            touch={"supported": True, "interactions": ["tap"]},
            panel={"id": "studio-ipad"}))
        self.assertEqual(code, 201, out)
        self.assertEqual(out["panel"]["identity"], "restored")
        self.assertEqual(out["panel"]["restored"]["from_surface"], "ipad")
        self.assertEqual([c["id"] for c in out["panel"]["config"]["columns"]],
                         ["agent", "tools"])
        code, out = self.call("POST", "/surfaces", surface("stranger", 800, 600,
                                                           panel={"id": "unknown-panel"}))
        self.assertEqual((out["panel"]["identity"], out["panel"]["config"]["columns"]),
                         ("new", []))
        self.assertEqual(self.ids("?panel=studio-ipad"), ["ipad-pro"])

    def test_a_bad_panel_scale_is_refused_whole(self):
        self.assertEqual(self.call("POST", "/surfaces/phone", {"panel": {"config": {
            "scale": 0}}})[0], 400)
        self.assertEqual(self.call("GET", "/surfaces/phone")[1]["panel"]["config"]["scale"], 1.5)


if __name__ == "__main__":
    unittest.main()
