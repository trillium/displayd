"""Registered display surfaces over HTTP, against a headless daemon.

The pure model/registry/hub behaviour is in test_surfaces.py. This file
proves the wire shape, the auth gate, that /surfaces leaves every other
route alone, and the built-in ``panel`` surface: its self-description, and
that content for one region rebinds ONLY that region.

DISPLAYD_FAKE_FB=1 selects the in-memory 1920x1080 framebuffer double.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd  # noqa: E402


def sidecar(sid="phone", **over):
    spec = {"id": sid, "kind": "sidecar",
            "renderer": {"name": "web-canvas", "content_types": ["text"]},
            "viewport": {"width": 390, "height": 844},
            "touch": {"supported": True, "interactions": ["tap", "swipe"]},
            "ttl_seconds": 30}
    spec.update(over)
    return spec


class SurfaceHttpTestCase(unittest.TestCase):
    def setUp(self):
        # other suites restore the env on cleanup, so set it per test
        patcher = mock.patch.dict(os.environ, {"DISPLAYD_FAKE_FB": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_daemon = displayd.DAEMON
        self.addCleanup(setattr, displayd, "DAEMON", self._old_daemon)
        self.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            feedback_path=os.path.join(self.tmp.name, "feedback.jsonl"))
        displayd.DAEMON = self.daemon
        server = ThreadingHTTPServer(("127.0.0.1", 0), displayd.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(self.daemon.clear)
        self.base = "http://127.0.0.1:%d" % server.server_address[1]

    def call(self, method, path, body=None, token=None, timeout=10):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def ids(self, query=""):
        code, out = self.call("GET", "/surfaces" + query)
        self.assertEqual(code, 200, out)
        return [s["id"] for s in out["surfaces"]]


class TestWire(SurfaceHttpTestCase):
    def test_register_discover_get_update_deregister(self):
        code, rec = self.call("POST", "/surfaces", sidecar())
        self.assertEqual(code, 201, rec)
        code, _ = self.call("POST", "/surfaces", sidecar())
        self.assertEqual(code, 200)  # upsert
        self.assertEqual(self.ids(), ["panel", "phone"])
        code, one = self.call("GET", "/surfaces/phone")
        self.assertEqual(one["viewport"]["usable"]["h"], 844)
        code, upd = self.call("POST", "/surfaces/phone",
                              {"viewport": {"width": 844, "height": 390}})
        self.assertEqual((code, upd["viewport"]["orientation"]), (200, "landscape"))
        code, out = self.call("POST", "/surfaces/phone/heartbeat", {})
        self.assertEqual((code, out["id"]), (200, "phone"))
        code, _ = self.call("DELETE", "/surfaces/phone")
        self.assertEqual(code, 200)
        self.assertEqual(self.call("GET", "/surfaces/phone")[0], 404)
        self.assertEqual(self.call("POST", "/surfaces/phone/heartbeat", {})[0], 404)

    def test_discovery_filters_over_the_wire(self):
        self.call("POST", "/surfaces", sidecar("phone", labels={"room": "a"}))
        self.call("POST", "/surfaces", sidecar(
            "wall", kind="wall", viewport={"width": 1920, "height": 1080},
            touch={"supported": False}))
        self.assertEqual(self.ids("?kind=wall"), ["wall"])
        self.assertEqual(self.ids("?touch=swipe"), ["phone"])
        self.assertEqual(self.ids("?touch=none&kind=wall"), ["wall"])
        self.assertEqual(self.ids("?min_width=1000"), ["panel", "wall"])
        self.assertEqual(self.ids("?label.room=a"), ["phone"])
        self.assertEqual(self.call("GET", "/surfaces?colour=red")[0], 400)
        self.assertEqual(self.call("GET", "/surfaces?min_width=wide")[0], 400)

    def test_error_statuses(self):
        self.assertEqual(self.call("POST", "/surfaces", {"id": "x"})[0], 400)
        self.assertEqual(self.call("POST", "/surfaces", [1])[0], 400)
        self.assertEqual(self.call("POST", "/surfaces/none", {"name": "x"})[0], 404)
        self.assertEqual(self.call("GET", "/surfaces/a/b/c")[0], 404)
        self.assertEqual(self.call("DELETE", "/surfaces/panel")[0], 409)
        code, out = self.call("POST", "/surfaces", sidecar(id="panel"))
        self.assertEqual(code, 409, out)

    def test_expired_lease_is_410_over_the_wire(self):
        self.call("POST", "/surfaces", sidecar("short", ttl_seconds=1))
        time.sleep(1.2)
        self.assertEqual(self.call("GET", "/surfaces/short")[0], 410)
        self.assertEqual(self.ids(), ["panel"])
        self.assertEqual(self.call("POST", "/surfaces", sidecar("short"))[0], 201)

    def test_other_routes_are_untouched(self):
        self.assertEqual(self.call("GET", "/state")[0], 200)
        self.assertEqual(self.call("GET", "/renderers")[0], 200)
        self.assertEqual(self.call("GET", "/surfacesx")[0], 404)
        self.assertEqual(self.call("DELETE", "/layout")[0], 200)

    def test_token_gate_applies_to_every_verb(self):
        with mock.patch.object(displayd, "API_TOKEN", b"s3cret"):
            for method, path, body in (("GET", "/surfaces", None),
                                       ("POST", "/surfaces", sidecar()),
                                       ("DELETE", "/surfaces/x", None),
                                       ("GET", "/surfaces/x/events", None)):
                self.assertEqual(self.call(method, path, body)[0], 401, path)
                self.assertEqual(self.call(method, path, body, token="no")[0], 401)
            self.assertEqual(self.call("POST", "/surfaces", sidecar(),
                                       token="s3cret")[0], 201)
            self.assertEqual(self.call("GET", "/surfaces", token="s3cret")[0], 200)


class TestSidecarFlow(SurfaceHttpTestCase):
    """Two simultaneous Sidecars: send, pull, touch, read events."""

    def setUp(self):
        super().setUp()
        self.call("POST", "/surfaces", sidecar("phone"))
        self.call("POST", "/surfaces", sidecar(
            "ipad", viewport={"width": 1024, "height": 768},
            touch={"supported": True, "interactions": ["tap", "pinch"],
                   "max_points": 2}))

    def test_send_to_one_and_pull_it(self):
        code, out = self.call("POST", "/surfaces/phone/content",
                              {"content": {"type": "text", "text": "hello"}})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["delivered"][0]["delivery"], "pull")
        code, got = self.call("GET", "/surfaces/phone/content?since=0")
        self.assertEqual(got["contents"]["full"]["content"]["text"], "hello")
        code, other = self.call("GET", "/surfaces/ipad/content")
        self.assertEqual(other["contents"], {})  # surfaces do not leak

    def test_send_by_target_and_select(self):
        # the panel (1920 wide) matches too but takes only displayd-html:
        # first-match picks the first surface that can take THIS content.
        code, out = self.call("POST", "/surfaces/content", {
            "target": {"select": {"min_width": 1000}},
            "content": {"type": "text", "text": "wide"}})
        self.assertEqual([d["surface"] for d in out["delivered"]], ["ipad"])
        self.assertEqual(self.call("POST", "/surfaces/content", {
            "target": {"select": {"min_width": 1000}},
            "content": {"type": "hologram"}})[0], 404)
        code, out = self.call("POST", "/surfaces/content", {
            "target": {"select": {"kind": "sidecar"}, "all": True},
            "content": {"type": "text", "text": "everyone"}})
        self.assertEqual(sorted(d["surface"] for d in out["delivered"]),
                         ["ipad", "phone"])

    def test_content_errors(self):
        for body, status in (({"content": {"type": "video"}}, 415),
                             ({"content": {"text": "no type"}}, 400),
                             ({"content": "nope"}, 400),
                             ({"region": "nope", "content": {"type": "text"}}, 404)):
            self.assertEqual(self.call("POST", "/surfaces/phone/content", body)[0],
                             status, body)
        self.assertEqual(self.call("POST", "/surfaces/gone/content",
                                   {"content": {"type": "text"}})[0], 404)

    def test_long_poll_over_http(self):
        threading.Timer(0.2, lambda: self.call(
            "POST", "/surfaces/phone/content",
            {"content": {"type": "text", "text": "late"}})).start()
        started = time.time()
        code, got = self.call("GET", "/surfaces/phone/content?since=0&wait=5")
        self.assertEqual(got["contents"]["full"]["content"]["text"], "late")
        self.assertLess(time.time() - started, 4)

    def test_concurrent_long_polls_on_separate_surfaces_stay_isolated(self):
        for sid in ("phone", "tablet"):
            self.assertIn(self.call("POST", "/surfaces", sidecar(sid))[0], (200, 201))
        got, done = {}, {sid: threading.Event() for sid in ("phone", "tablet")}

        def poll(sid):
            got[sid] = self.call("GET", "/surfaces/%s/content?since=0&wait=10" % sid,
                                 timeout=15)
            done[sid].set()

        pollers = [threading.Thread(target=poll, args=(sid,), daemon=True)
                   for sid in done]
        for t in pollers:
            t.start()
        # content for phone only: its poller returns, the tablet's keeps holding
        self.call("POST", "/surfaces/phone/content",
                  {"content": {"type": "text", "text": "for-phone"}})
        self.assertTrue(done["phone"].wait(10))
        self.assertFalse(done["tablet"].is_set())
        code, out = got["phone"]
        self.assertEqual((code, out["contents"]["full"]["content"]["text"]),
                         (200, "for-phone"))
        self.call("POST", "/surfaces/tablet/content",
                  {"content": {"type": "text", "text": "for-tablet"}})
        self.assertTrue(done["tablet"].wait(10))
        for t in pollers:
            t.join(10)
        code, out = got["tablet"]
        self.assertEqual((code, out["contents"]["full"]["content"]["text"]),
                         (200, "for-tablet"))
        self.assertEqual(list(out["contents"]), ["full"])
        # neither surface's channel picked up the other's content
        for sid, text in (("phone", "for-phone"), ("tablet", "for-tablet")):
            code, out = self.call("GET", "/surfaces/%s/content?since=0" % sid)
            self.assertEqual([c["content"]["text"] for c in out["contents"].values()],
                             [text])

    def test_touch_roundtrip_each_surface_its_own_interactions(self):
        code, out = self.call("POST", "/surfaces/phone/touch",
                              {"interaction": "swipe", "x": 20, "y": 400,
                               "dx": 0, "dy": -150})
        self.assertEqual((code, out["event"]["region"]), (200, "full"))
        code, out = self.call("POST", "/surfaces/ipad/touch",
                              {"interaction": "pinch", "x": 500, "y": 300,
                               "scale": 2.0})
        self.assertEqual(code, 200, out)
        self.assertEqual(self.call("POST", "/surfaces/phone/touch",
                                   {"interaction": "pinch", "x": 5, "y": 5})[0], 409)
        self.assertEqual(self.call("POST", "/surfaces/ipad/touch",
                                   {"interaction": "swipe", "x": 5, "y": 5})[0], 409)
        code, ev = self.call("GET", "/surfaces/ipad/events")
        self.assertEqual([e["interaction"] for e in ev["events"]], ["pinch"])
        code, ev = self.call("GET", "/surfaces/phone/events?since=0")
        self.assertEqual([e["interaction"] for e in ev["events"]], ["swipe"])
        self.assertEqual(self.call("GET", "/surfaces/phone/events?since=x")[0], 400)

    def test_events_long_poll_over_http(self):
        threading.Timer(0.2, lambda: self.call(
            "POST", "/surfaces/phone/touch",
            {"interaction": "tap", "x": 10, "y": 10})).start()
        code, ev = self.call("GET", "/surfaces/phone/events?since=0&wait=5")
        self.assertEqual(len(ev["events"]), 1)


class TestPanelSurface(SurfaceHttpTestCase):
    """The panel is one registered surface; displayd-html is its renderer."""

    def test_panel_describes_the_live_framebuffer(self):
        code, panel = self.call("GET", "/surfaces/panel")
        self.assertEqual(code, 200)
        self.assertTrue(panel["builtin"])
        self.assertEqual(panel["kind"], "panel")
        self.assertEqual((panel["viewport"]["width"], panel["viewport"]["height"]),
                         (1920, 1080))
        self.assertEqual(panel["renderer"]["name"], "displayd-html")
        self.assertEqual(panel["renderer"]["content_types"], ["displayd-html"])
        self.assertEqual([r["name"] for r in panel["regions"]], ["full"])
        self.assertEqual(panel["touch"]["supported"], False)  # no touch heartbeat

    def test_panel_touch_follows_the_touch_service_heartbeat(self):
        self.daemon.touch_live = {"announced_at": time.time(), "announced": {},
                                  "regions_sha": "x"}
        code, panel = self.call("GET", "/surfaces/panel")
        self.assertEqual(panel["touch"], {"supported": True, "interactions": ["tap"],
                                          "max_points": 1})
        self.daemon.touch_live["announced_at"] -= 10 ** 6  # service went quiet
        self.assertFalse(self.call("GET", "/surfaces/panel")[1]["touch"]["supported"])
        # touch.py is the only toucher of the real panel; the API refuses.
        self.assertEqual(self.call("POST", "/surfaces/panel/touch",
                                   {"interaction": "tap", "x": 1, "y": 1})[0], 409)

    def test_panel_regions_follow_the_layout(self):
        code, out = self.call("POST", "/layout", {"regions": [
            {"name": "Top Bar!", "height": "25%", "renderer": "solid",
             "params": {"color": "#112233"}},
            {"name": "main", "height": "75%", "renderer": "solid",
             "params": {"color": "#445566"}}]})
        self.assertEqual(code, 200, out)
        code, panel = self.call("GET", "/surfaces/panel")
        regions = {r["name"]: r["rect"] for r in panel["regions"]}
        self.assertEqual(sorted(regions), ["main", "top-bar"])  # slugged
        self.assertEqual(regions["top-bar"]["h"], 270)
        self.assertEqual(regions["main"]["y"], 270)
        self.assertEqual(self.call("DELETE", "/layout")[0], 200)
        self.assertEqual([r["name"] for r in
                          self.call("GET", "/surfaces/panel")[1]["regions"]], ["full"])

    def test_panel_rejects_content_it_cannot_draw(self):
        for content, status in (({"type": "text", "text": "x"}, 415),
                                ({"type": "displayd-html", "template": "../x"}, 400),
                                ({"type": "displayd-html", "vars": {"a": [1]}}, 400)):
            code, out = self.call("POST", "/surfaces/panel/content",
                                  {"content": content})
            self.assertEqual(code, status, out)

    def test_panel_unknown_region_is_404(self):
        code, out = self.call("POST", "/surfaces/panel/content", {
            "region": "nope", "content": {"type": "displayd-html"}})
        self.assertEqual(code, 404, out)

    def test_html_content_without_layout_shows_the_html_view(self):
        code, out = self.call("POST", "/surfaces/panel/content", {"content": {
            "type": "displayd-html", "template": "status.html",
            "vars": {"title": "hi"}}})
        self.assertEqual(code, 200, out)
        self.assertTrue(out["delivered"][0]["rendered"])
        state = self.call("GET", "/state")[1]
        self.assertEqual(state["renderer"], "html")

    def test_html_content_rebinds_only_the_targeted_region(self):
        self.call("POST", "/layout", {"regions": [
            {"name": "left", "width": "50%", "renderer": "solid",
             "params": {"color": "#ff0000"}},
            {"name": "right", "width": "50%", "renderer": "solid",
             "params": {"color": "#0000ff"}}]})
        before = {n: self.daemon.region_threads[n] for n in ("left", "right")}
        old_right_thread = before["right"]["thread"]
        code, out = self.call("POST", "/surfaces/panel/content", {
            "region": "left", "content": {"type": "displayd-html",
                                          "template": "status.html",
                                          "vars": {"title": "L"}}})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["delivered"][0]["region"], "left")
        after = self.daemon.region_threads
        self.assertEqual(after["left"]["renderer"], "html")
        self.assertEqual(after["left"]["params"]["vars"], {"title": "L"})
        self.assertIsNot(after["left"]["thread"], before["left"]["thread"])
        self.assertTrue(before["left"]["stop"].is_set())  # old one told to stop
        # the right region was never touched: same entry, same thread, running
        self.assertIs(after["right"], before["right"])
        self.assertIs(after["right"]["thread"], old_right_thread)
        self.assertFalse(after["right"]["stop"].is_set())
        self.assertEqual(after["right"]["renderer"], "solid")
        layout = {r["name"]: r["renderer"] for r in
                  self.call("GET", "/state")[1]["layout"]["regions"]}
        self.assertEqual(layout, {"left": "html", "right": "solid"})

    def test_a_late_frame_from_a_replaced_region_is_dropped(self):
        from daemon_surfaces import _GuardedRegionScreen
        stop = threading.Event()
        screen = _GuardedRegionScreen(self.daemon, "ghost", 10, 10, stop)
        calls = []
        with mock.patch("daemon_surfaces.RegionScreen.present",
                        lambda self_, img: calls.append(img)):
            screen.present("live")
            stop.set()
            screen.present("late")
        self.assertEqual(calls, ["live"])

    def test_broadcast_reaches_a_sidecar_and_the_panel_with_their_own_renderers(self):
        self.call("POST", "/surfaces", sidecar("phone", renderer={
            "name": "displayd-html", "content_types": ["displayd-html"]}))
        self.call("POST", "/surfaces", sidecar("other"))
        code, out = self.call("POST", "/surfaces/content", {
            "target": {"select": {"content_type": "displayd-html"}, "all": True},
            "content": {"type": "displayd-html", "template": "status.html",
                        "vars": {"title": "all"}}})
        self.assertEqual(code, 200, out)
        by = {d["surface"]: d for d in out["delivered"]}
        self.assertEqual(sorted(by), ["panel", "phone"])
        self.assertEqual(by["panel"]["renderer"], "displayd-html")
        self.assertTrue(by["panel"]["rendered"])
        # 'phone' claims displayd-html too, but it is a registered Sidecar:
        # displayd draws nothing for it by itself -- see docs/SURFACES.md.
        self.assertIn("seq", by["phone"])


if __name__ == "__main__":
    unittest.main()
