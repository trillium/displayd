"""The panel draws its own columns.

Unit tests drive ``ColumnPainter`` against a real ``SurfaceHub`` with a fake
region set (pure geometry, no framebuffer); the HTTP tests run the real
headless daemon (DISPLAYD_FAKE_FB=1, 1920x1080) end to end: set columns on
the built-in panel, publish events, and watch which region's renderer params
change -- and which regions are never touched. Params are asserted, not
pixels, so the suite needs no litehtml build.

Run from the repo root:  python3 -m unittest tests.test_column_painter -v
"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.dirname(__file__))

import displayd  # noqa: E402
import surface_column_view as view  # noqa: E402
from column_painter import ColumnPainter  # noqa: E402
from surface_hub import SurfaceHub  # noqa: E402
from surface_model import SurfaceError  # noqa: E402
from test_surfaces_http import SurfaceHttpTestCase  # noqa: E402

STREAM = {"id": "agent", "name": "Agent", "views": ["log", "calls"],
          "event_types": ["say", "call"]}


def until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class Fixture:
    """A hub whose ``panel`` has the regions of a fake layout."""

    def __init__(self):
        self.layout = [("left", "Left", (0, 0, 288, 1080)),
                       ("main", "Main", (288, 0, 1344, 1080)),
                       ("right", "Right", (1632, 0, 288, 1080))]
        self.epoch = 1
        self.drawn = []  # (layout name, params), in order
        self.hub = SurfaceHub()
        self.hub.registry.register_builtin("panel", self.provider)
        self.hub.streams.declare(STREAM)

    def provider(self):
        regions = [{"name": slug, "rect": dict(zip("xywh", rect)), "touch": False}
                   for slug, _, rect in self.layout] or None
        return {"name": "panel", "kind": "panel",
                "renderer": {"name": "displayd-html", "version": "t",
                             "content_types": ["displayd-html"]},
                "viewport": {"width": 1920, "height": 1080, "scale": 1},
                "touch": {"supported": False}, "regions": regions}

    def painter(self):
        painter = ColumnPainter(self.hub, lambda: list(self.layout),
                                lambda name, params: self.drawn.append((name, params)),
                                lambda: self.epoch, interval=0.01)
        return painter

    def columns(self, *cols):
        self.hub.registry.update("panel", {"panel": {"config": {"columns": list(cols)}}})


def col(cid, region, view_="log", stream="agent"):
    return {"id": cid, "stream": stream, "view": view_, "region": region}


class TestPaint(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.painter = self.fx.painter()
        self.addCleanup(self.painter.stop)

    def test_a_column_paints_into_its_region_sized_to_it(self):
        self.fx.columns(col("c-left", "left"), col("c-main", "main"))
        self.assertEqual(sorted(self.painter.paint()), ["left", "main"])
        by_region = dict(self.fx.drawn)
        for name, rect in (("Left", self.fx.layout[0][2]), ("Main", self.fx.layout[1][2])):
            params = by_region[name]
            self.assertEqual(params["template"], view.TEMPLATE)
            fitted = view.plan(rect)["chars"]
            self.assertTrue(all(len(v) <= fitted for v in params["vars"].values()), name)
        self.assertNotIn("Right", by_region)  # no column there: untouched

    def test_an_unchanged_column_is_not_redrawn(self):
        self.fx.columns(col("c-main", "main"))
        self.painter.paint()
        self.fx.drawn.clear()
        self.assertEqual(self.painter.paint(), [])
        self.assertEqual(self.fx.drawn, [])

    def test_an_event_redraws_only_columns_on_that_stream(self):
        self.fx.hub.streams.declare({"id": "other", "name": "Other"})
        self.fx.columns(col("a", "left"), col("b", "main", stream="other"))
        self.painter.paint()
        self.fx.drawn.clear()
        self.fx.hub.streams.publish("agent", {"type": "say", "data": {"text": "hello"}})
        self.assertEqual(self.painter.paint(), ["left"])
        self.assertEqual([n for n, _ in self.fx.drawn], ["Left"])
        self.assertIn("hello", self.fx.drawn[0][1]["vars"]["r0"])

    def test_no_layout_means_nothing_is_drawn(self):
        self.fx.columns(col("c-main", "main"))
        self.fx.layout = []
        self.assertEqual(self.painter.paint(), [])
        self.assertEqual(self.fx.drawn, [])
        self.assertEqual(self.painter.status()["regions"], {})

    def test_a_new_layout_repaints_everything(self):
        self.fx.columns(col("c-main", "main"))
        self.painter.paint()
        self.fx.drawn.clear()
        self.fx.epoch = 2  # the layout was replaced; its regions run fresh renderers
        self.assertEqual(self.painter.paint(), ["main"])

    def test_columns_for_missing_regions_are_dormant_and_come_back(self):
        self.fx.columns(col("c-left", "left"), col("c-main", "main"))
        self.fx.layout = self.fx.layout[1:]  # the left region is gone
        panel = self.fx.hub.registry.get("panel")  # discovery still works
        self.assertEqual(panel["panel"]["dormant"], ["c-left"])
        self.assertEqual([c["id"] for c in panel["panel"]["config"]["columns"]],
                         ["c-main"])
        self.assertEqual(self.painter.paint(), ["main"])
        self.fx.layout = [("left", "Left", (0, 0, 288, 1080))] + self.fx.layout
        self.fx.epoch = 2
        self.assertEqual(sorted(self.painter.paint()), ["left", "main"])
        self.assertEqual(self.fx.hub.registry.get("panel")["panel"]["dormant"], [])

    def test_one_failing_region_does_not_stop_the_others(self):
        self.fx.columns(col("a", "left"), col("b", "main"))
        real = self.painter._apply

        def flaky(name, params):
            if name == "Left":
                raise SurfaceError(409, "cannot render here")
            real(name, params)
        self.painter._apply = flaky
        self.assertEqual(self.painter.paint(), ["main"])
        self.assertIn("cannot render here", self.painter.status()["errors"]["left"])
        self.painter._apply = real  # recovers on the next pass
        self.assertEqual(self.painter.paint(), ["left"])
        self.assertEqual(self.painter.status()["errors"], {})

    def test_a_stream_declared_later_replaces_the_waiting_notice(self):
        self.fx.columns(col("a", "main", stream="late"))
        self.painter.paint()
        self.assertIn("not declared", self.fx.drawn[-1][1]["vars"]["r0"])
        self.fx.hub.streams.declare({"id": "late", "name": "Late", "views": ["log"]})
        self.assertEqual(self.painter.paint(), ["main"])
        self.assertEqual(self.fx.drawn[-1][1]["vars"]["title"], "Late")

    def test_a_burst_of_events_is_coalesced_by_the_worker(self):
        self.fx.columns(col("c-main", "main"))
        self.painter.paint()
        self.fx.drawn.clear()
        for i in range(200):
            self.fx.hub.streams.publish("agent", {"type": "say", "data": "m%d" % i})
        self.assertTrue(until(lambda: self.fx.drawn))
        time.sleep(0.2)
        self.assertLess(len(self.fx.drawn), 20)  # not one paint per event
        self.assertTrue(any("m199" in v for v in self.fx.drawn[-1][1]["vars"].values()))

    def test_a_broken_listener_cannot_fail_the_publisher(self):
        self.fx.hub.streams.subscribe(lambda: 1 / 0)
        self.fx.hub.streams.publish("agent", {"type": "say", "data": "x"})


class TestBuiltinPanelBlock(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.reg = self.fx.hub.registry

    def test_only_a_panel_update_is_accepted_on_a_builtin(self):
        for patch in ({"name": "x"}, {"viewport": {"width": 1}},
                      {"panel": {}, "ttl_seconds": 5}):
            with self.assertRaises(SurfaceError, msg=patch) as ctx:
                self.reg.update("panel", patch)
            self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ValueError):
            self.reg.update("panel", {"panel": 5})

    def test_a_bad_column_is_rejected_and_nothing_is_stored(self):
        self.fx.columns(col("ok", "main"))
        for bad in (col("x", "nowhere"), dict(col("y", "main"), view=7),
                    [col("p", "main"), col("q", "main")]):
            bad = bad if isinstance(bad, list) else [bad]
            with self.assertRaises(ValueError):
                self.reg.update("panel", {"panel": {"config": {"columns": bad}}})
        cols = self.reg.get("panel")["panel"]["config"]["columns"]
        self.assertEqual([c["id"] for c in cols], ["ok"])

    def test_setting_columns_replaces_the_list(self):
        self.fx.columns(col("a", "left"), col("b", "main"))
        self.fx.columns(col("c", "right"))
        cols = self.reg.get("panel")["panel"]["config"]["columns"]
        self.assertEqual([c["id"] for c in cols], ["c"])

    def test_listeners_hear_a_panel_change(self):
        heard = []
        self.fx.hub.on_columns_changed(lambda: heard.append(1))
        self.fx.columns(col("a", "left"))
        self.assertEqual(len(heard), 1)

    def test_the_panel_block_persists_across_a_hub_restart(self):
        from surface_store import SurfaceStore
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state", "surfaces.json")
            first = Fixture.__new__(Fixture)
            first.layout = self.fx.layout
            first.hub = SurfaceHub(store=SurfaceStore(path))
            first.hub.registry.register_builtin("panel", self.fx.provider)
            first.columns(col("a", "left"), col("b", "main"))
            second = SurfaceHub(store=SurfaceStore(path))
            self.assertEqual(second.restored["panels"], 1)
            second.registry.register_builtin("panel", self.fx.provider)
            cols = second.registry.get("panel")["panel"]["config"]["columns"]
            self.assertEqual([c["id"] for c in cols], ["a", "b"])

    def test_a_hand_edited_panel_block_is_dropped_not_loaded(self):
        hub = SurfaceHub()
        count, bad = hub.registry.panels.restore({
            "panel": {"config": {"columns": [{"id": "A B", "stream": "s"}]}},
            "ok": {"id": None, "config": {"columns": []}}, "junk": 5})
        self.assertEqual((count, bad), (1, 2))


class TestDaemonColumns(SurfaceHttpTestCase):
    """The real daemon: events in, region params out."""

    def setUp(self):
        SurfaceHttpTestCase.setUp(self)
        self.assertEqual(self.call("POST", "/event-streams", dict(STREAM))[0], 201)

    def layout(self, regions=("left", "main", "right")):
        widths = {"left": "15%", "main": "70%", "right": "15%"}
        code, out = self.call("POST", "/layout", {"regions": [
            {"name": n, "width": widths[n], "renderer": "solid",
             "params": {"color": "#102030"}} for n in regions]})
        self.assertEqual(code, 200, out)

    def columns(self, *cols):
        code, out = self.call("POST", "/surfaces/panel/columns", {"columns": list(cols)})
        self.assertEqual(code, 200, out)
        return out

    def params(self, name):
        """The region's params; ``vars`` is always present (empty until drawn)."""
        params = self.daemon.region_threads[name]["params"]
        return dict(params, vars=params.get("vars", {}))

    def drawn(self, name):
        return self.daemon.region_threads[name]["renderer"] == "html"

    def publish(self, text):
        code, out = self.call("POST", "/event-streams/agent/events",
                              {"type": "say", "data": {"text": text}})
        self.assertEqual(code, 200, out)

    def test_columns_draw_in_regions_of_different_widths(self):
        self.layout()
        self.columns(col("c-left", "left"), col("c-main", "main"))
        self.publish("x" * 300)
        self.assertTrue(until(lambda: self.drawn("left") and self.drawn("main")))
        self.assertTrue(until(lambda: self.params("main")["vars"].get("r0")
                              and self.params("left")["vars"].get("r0")))
        narrow, wide = self.params("left")["vars"], self.params("main")["vars"]
        left_w = self.daemon.layout[0]["rect"][2]
        main_w = self.daemon.layout[1]["rect"][2]
        self.assertLess(left_w, main_w)
        self.assertEqual(len(narrow["r0"]), view.plan((0, 0, left_w, 1080))["chars"])
        self.assertEqual(len(wide["r0"]), view.plan((0, 0, main_w, 1080))["chars"])
        self.assertGreater(len(wide["r0"]), len(narrow["r0"]) * 3)

    def test_only_the_columns_regions_are_rebound(self):
        self.layout()
        before = {n: self.daemon.region_threads[n] for n in ("left", "main", "right")}
        self.columns(col("c-main", "main"))
        self.assertTrue(until(lambda: self.drawn("main")))
        self.assertIs(self.daemon.region_threads["left"], before["left"])
        self.assertIs(self.daemon.region_threads["right"], before["right"])
        self.assertTrue(before["main"]["stop"].is_set())
        self.assertEqual(self.daemon.region_threads["right"]["renderer"], "solid")

    def test_a_published_event_reaches_the_screen_region(self):
        self.layout()
        self.columns(col("c-main", "main"))
        self.publish("first")
        self.assertTrue(until(lambda: "first" in self.params("main")["vars"].get("r0", "")))
        self.publish("second")
        self.assertTrue(until(lambda: "second" in self.params("main")["vars"].get("r1", "")))

    def test_with_no_layout_a_column_never_takes_over_the_panel(self):
        self.columns(col("c-full", "full"))
        self.publish("hello")
        time.sleep(0.5)
        state = self.call("GET", "/state")[1]
        self.assertIsNone(state["renderer"])
        self.assertIsNone(state["layout"])

    def test_a_layout_set_after_the_columns_paints_them(self):
        self.columns(col("c-full", "full"))
        self.layout()  # the column's region 'full' is not in this layout: dormant
        panel = self.call("GET", "/surfaces/panel")[1]
        self.assertEqual(panel["panel"]["dormant"], ["c-full"])
        self.columns(col("c-main", "main"))
        self.assertTrue(until(lambda: self.drawn("main")))
        self.assertEqual(self.call("DELETE", "/layout")[0], 200)
        self.layout()  # a fresh layout: the column is painted again
        self.assertTrue(until(lambda: self.drawn("main")))

    def test_leaving_the_layout_keeps_discovery_working(self):
        self.layout()
        self.columns(col("c-main", "main"))
        self.assertEqual(self.call("DELETE", "/layout")[0], 200)
        code, panel = self.call("GET", "/surfaces/panel")
        self.assertEqual(code, 200, panel)
        self.assertEqual(panel["panel"]["dormant"], ["c-main"])
        self.assertEqual(self.call("GET", "/surfaces")[0], 200)

    def test_columns_are_validated_against_the_live_regions(self):
        self.layout()
        code, out = self.call("POST", "/surfaces/panel/columns",
                              {"columns": [col("x", "nowhere")]})
        self.assertEqual(code, 400, out)

    def test_state_reports_what_the_panel_drew(self):
        self.layout()
        self.columns(col("c-main", "main"))
        self.assertTrue(until(lambda: self.drawn("main")))
        columns = self.call("GET", "/state")[1]["surfaces"]["columns"]
        self.assertEqual(columns["regions"], {"main": "c-main"})
        self.assertGreaterEqual(columns["painted"], 1)

    def test_painting_is_not_operator_activity(self):
        self.layout()
        self.columns(col("c-main", "main"))
        self.assertTrue(until(lambda: self.drawn("main")))
        with mock.patch.object(self.daemon.policy, "note_api") as note:
            self.daemon.surface_hub.streams.publish(
                "agent", {"type": "say", "data": "quiet"})
            self.assertTrue(until(lambda: any(
                "quiet" in v for v in self.params("main")["vars"].values())))
        note.assert_not_called()

    def test_columns_survive_a_daemon_restart(self):
        self.layout()
        self.columns(col("c-main", "main"))
        again = displayd.DisplayDaemon(
            policy_path=self.daemon.policy.path,
            feedback_path=os.path.join(self.tmp.name, "feedback.jsonl"))
        self.addCleanup(again.clear)
        panel = again.surface_hub.registry.get("panel")
        self.assertEqual(panel["panel"]["dormant"], ["c-main"])  # no layout yet
        self.assertEqual(again.surfaces_state()["restored"]["panels"], 1)


if __name__ == "__main__":
    unittest.main()
