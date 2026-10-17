"""Registered display surfaces: model, registry, targeting, hub, touch.

Pure (no daemon, no HTTP): a fake clock drives expiry, and a recording
renderer implementation proves the layer is independent of any renderer.
The daemon wiring and the wire shape are in test_surfaces_http.py.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import surface_model as model
from surface_hub import SurfaceHub
from surface_model import SurfaceError
from surface_registry import SurfaceRegistry
from surface_renderers import SurfaceRenderer
from surface_target import match, parse_target, resolve


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def phone(**over):
    spec = {"id": "phone", "kind": "sidecar", "labels": {"room": "studio"},
            "renderer": {"name": "web-canvas", "content_types": ["text", "image"]},
            "viewport": {"width": 390, "height": 844, "scale": 3,
                         "insets": {"top": 47, "bottom": 34}},
            "touch": {"supported": True, "interactions": ["tap", "swipe", "long_press"]},
            "ttl_seconds": 10}
    spec.update(over)
    return spec


def ipad(**over):
    spec = {"id": "ipad", "kind": "sidecar",
            "renderer": {"name": "swiftui", "content_types": ["text", "scene"]},
            "viewport": {"width": 1024, "height": 768},
            "touch": {"supported": True, "interactions": ["tap", "pinch", "drag"],
                      "max_points": 2},
            "regions": [
                {"name": "left", "rect": {"x": 0, "y": 0, "w": 512, "h": 768}},
                {"name": "right", "rect": {"x": 512, "y": 0, "w": 512, "h": 768},
                 "content_types": ["text"]},
                {"name": "badge", "rect": {"x": 900, "y": 10, "w": 100, "h": 60},
                 "content_types": ["text"]}]}
    spec.update(over)
    return spec


def kiosk(**over):
    spec = {"id": "kiosk", "kind": "wall",
            "renderer": {"name": "swiftui", "content_types": ["text"]},
            "viewport": {"width": 1920, "height": 1080}}
    spec.update(over)
    return spec


class TestModel(unittest.TestCase):
    def test_defaults_and_derived_geometry(self):
        rec = model.normalize(phone())
        self.assertEqual(rec["viewport"]["usable"],
                         {"x": 0, "y": 47, "w": 390, "h": 763})
        self.assertEqual(rec["viewport"]["orientation"], "portrait")
        self.assertEqual([r["name"] for r in rec["regions"]], ["full"])
        self.assertEqual(rec["regions"][0]["rect"], rec["viewport"]["usable"])
        self.assertEqual(rec["default_region"], "full")
        self.assertEqual(rec["touch"]["interactions"], ["tap", "long_press", "swipe"])
        self.assertEqual(model.normalize(kiosk())["touch"],
                         {"supported": False, "interactions": [], "max_points": 0})

    def test_normalize_is_idempotent(self):
        for spec in (phone(), ipad(), kiosk()):
            once = model.normalize(spec)
            self.assertEqual(model.normalize(once), once)

    def test_rejections_name_the_field(self):
        cases = [
            (phone(id="Bad Id"), "id"),
            (phone(id="content"), "reserved"),
            (phone(viewport={"width": 0, "height": 10}), "viewport.width"),
            (phone(viewport={"width": 100, "height": 100,
                             "insets": {"left": 60, "right": 40}}), "usable"),
            (phone(renderer={"name": "x", "content_types": []}), "content_types"),
            (phone(touch={"supported": False, "interactions": ["tap"]}), "supported"),
            (phone(touch={"supported": True, "interactions": ["wiggle"]}), "wiggle"),
            (phone(touch={"supported": True, "interactions": ["pinch"]}), "max_points"),
            (phone(ttl_seconds=0), "ttl_seconds"),
            (phone(regions=[{"name": "a", "rect": {"x": 0, "y": 0, "w": 9999, "h": 5}}]),
             "outside"),
            (phone(regions=[{"name": "a", "rect": {"x": 0, "y": 60, "w": 5, "h": 5}},
                            {"name": "a", "rect": {"x": 0, "y": 60, "w": 5, "h": 5}}]),
             "duplicate"),
            (phone(regions=[{"name": "a", "content_types": ["video"],
                             "rect": {"x": 0, "y": 60, "w": 5, "h": 5}}]), "not accepted"),
            (kiosk(regions=[{"name": "a", "touch": True,
                             "rect": {"x": 0, "y": 0, "w": 5, "h": 5}}]), "no touch"),
        ]
        for spec, needle in cases:
            with self.assertRaises(ValueError, msg=str(spec)) as ctx:
                model.normalize(spec)
            self.assertIn(needle, str(ctx.exception))

    def test_region_outside_insets_is_rejected(self):
        # y=0 is inside the viewport but under the inset: not usable.
        with self.assertRaises(ValueError):
            model.normalize(phone(regions=[
                {"name": "a", "rect": {"x": 0, "y": 0, "w": 10, "h": 10}}]))


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.reg = SurfaceRegistry(clock=self.clock)

    def test_register_is_an_upsert_with_revisions(self):
        rec, created = self.reg.register(phone())
        self.assertTrue(created)
        self.assertEqual(rec["revision"], 1)
        again, created = self.reg.register(phone())
        self.assertFalse(created)
        self.assertEqual(again["revision"], 2)
        self.assertEqual(again["registered_at"], rec["registered_at"])

    def test_expiry_without_heartbeat_then_410_not_404(self):
        self.reg.register(phone())
        self.clock.t += 9
        self.assertEqual(self.reg.get("phone")["expires_in"], 1.0)
        self.clock.t += 2
        with self.assertRaises(SurfaceError) as ctx:
            self.reg.get("phone")
        self.assertEqual(ctx.exception.status, 410)
        self.assertEqual(self.reg.list(), [])
        with self.assertRaises(SurfaceError) as ctx:
            self.reg.get("never-was")
        self.assertEqual(ctx.exception.status, 404)

    def test_heartbeat_extends_and_expired_heartbeat_is_gone(self):
        self.reg.register(phone())
        for _ in range(5):
            self.clock.t += 8
            self.reg.heartbeat("phone")
        self.assertEqual(len(self.reg.list()), 1)
        self.clock.t += 11
        with self.assertRaises(SurfaceError) as ctx:
            self.reg.heartbeat("phone")
        self.assertEqual(ctx.exception.status, 410)
        rec, created = self.reg.register(phone())  # re-registering heals it
        self.assertTrue(created)
        self.assertEqual(rec["revision"], 1)

    def test_expiry_drops_content_and_events(self):
        self.reg.register(phone())
        self.reg.channels.put_content("phone", "full", {"type": "text"})
        self.clock.t += 11
        self.reg.list()
        self.reg.register(phone())
        self.assertEqual(self.reg.channels.read_content("phone")["contents"], {})

    def test_deregister_is_final_and_distinct_from_expiry(self):
        self.reg.register(phone())
        self.assertEqual(self.reg.deregister("phone"), {"ok": True, "surface": "phone"})
        with self.assertRaises(SurfaceError) as ctx:
            self.reg.get("phone")
        self.assertEqual(ctx.exception.status, 404)

    def test_update_merges_and_rederives_the_default_region(self):
        self.reg.register(phone())
        rec = self.reg.update("phone", {"viewport": {"width": 844, "height": 390},
                                        "name": "Phone, rotated"})
        self.assertEqual(rec["viewport"]["orientation"], "landscape")
        self.assertEqual(rec["viewport"]["insets"]["top"], 47)  # merged, not lost
        self.assertEqual(rec["regions"][0]["rect"], rec["viewport"]["usable"])
        self.assertEqual(rec["revision"], 2)
        self.assertEqual(rec["name"], "Phone, rotated")

    def test_update_is_atomic_and_cannot_rename(self):
        self.reg.register(ipad())
        before = self.reg.get("ipad")
        with self.assertRaises(ValueError):  # regions no longer fit
            self.reg.update("ipad", {"viewport": {"width": 600, "height": 400}})
        with self.assertRaises(ValueError):
            self.reg.update("ipad", {"id": "other"})
        self.assertEqual(self.reg.get("ipad"), before)

    def test_touch_can_be_switched_off_by_update(self):
        self.reg.register(phone())
        rec = self.reg.update("phone", {"touch": {"supported": False}})
        self.assertEqual(rec["touch"]["interactions"], [])
        self.assertFalse(rec["regions"][0]["touch"])

    def test_builtin_never_expires_and_is_not_editable(self):
        self.reg.register_builtin("panel", lambda: kiosk(kind="panel"))
        self.clock.t += 10 ** 6
        self.assertTrue(self.reg.get("panel")["builtin"])
        for call in (lambda: self.reg.heartbeat("panel"),
                     lambda: self.reg.deregister("panel"),
                     lambda: self.reg.update("panel", {"name": "x"}),
                     lambda: self.reg.register(kiosk(id="panel"))):
            with self.assertRaises(SurfaceError) as ctx:
                call()
            self.assertEqual(ctx.exception.status, 409)

    def test_a_broken_builtin_never_breaks_discovery(self):
        def boom():
            raise RuntimeError("fb gone")
        self.reg.register_builtin("panel", boom)
        self.reg.register(phone())
        self.assertEqual([s["id"] for s in self.reg.list()], ["phone"])
        with self.assertRaises(SurfaceError) as ctx:
            self.reg.get("panel")
        self.assertEqual(ctx.exception.status, 503)


class TestSimultaneousSurfaces(unittest.TestCase):
    """Three surfaces at once with different viewports and capabilities."""

    def setUp(self):
        self.reg = SurfaceRegistry(clock=Clock())
        for spec in (phone(), ipad(), kiosk()):
            self.reg.register(spec)

    def ids(self, query):
        return [s["id"] for s in self.reg.list(query)]

    def test_all_live_and_independent(self):
        self.assertEqual(self.ids(None), ["ipad", "kiosk", "phone"])
        usable = {s["id"]: (s["viewport"]["usable"]["w"], s["viewport"]["usable"]["h"])
                  for s in self.reg.list()}
        self.assertEqual(usable, {"ipad": (1024, 768), "kiosk": (1920, 1080),
                                  "phone": (390, 763)})

    def test_discovery_by_touch_capability(self):
        self.assertEqual(self.ids({"touch": "swipe"}), ["phone"])
        self.assertEqual(self.ids({"touch": "pinch"}), ["ipad"])
        self.assertEqual(self.ids({"touch": "tap"}), ["ipad", "phone"])
        self.assertEqual(self.ids({"touch": "any"}), ["ipad", "phone"])
        self.assertEqual(self.ids({"touch": "none"}), ["kiosk"])

    def test_discovery_by_viewport_renderer_content_labels(self):
        self.assertEqual(self.ids({"min_width": 1000}), ["ipad", "kiosk"])
        self.assertEqual(self.ids({"min_width": 1000, "min_height": 800}), ["kiosk"])
        self.assertEqual(self.ids({"renderer": "swiftui"}), ["ipad", "kiosk"])
        self.assertEqual(self.ids({"content_type": "image"}), ["phone"])
        self.assertEqual(self.ids({"labels": {"room": "studio"}}), ["phone"])
        self.assertEqual(self.ids({"kind": "wall"}), ["kiosk"])
        self.assertEqual(self.ids({"region": "badge"}), ["ipad"])
        self.assertEqual(self.ids({"touch": "tap", "min_width": 1000}), ["ipad"])

    def test_match_is_a_pure_predicate(self):
        self.assertTrue(match(self.reg.get("phone"), {}))
        self.assertFalse(match(self.reg.get("phone"), {"kind": "wall"}))


class TestTargeting(unittest.TestCase):
    def setUp(self):
        self.reg = SurfaceRegistry(clock=Clock())
        for spec in (phone(), ipad(), kiosk()):
            self.reg.register(spec)

    def pairs(self, target):
        return [(s["id"], r["name"]) for s, r in resolve(self.reg, target)]

    def test_string_forms(self):
        self.assertEqual(self.pairs("phone"), [("phone", "full")])
        self.assertEqual(self.pairs("ipad/right"), [("ipad", "right")])

    def test_structured_form(self):
        self.assertEqual(self.pairs({"surface": "ipad", "region": "left"}),
                         [("ipad", "left")])

    def test_select_first_and_all(self):
        self.assertEqual(self.pairs({"select": {"touch": "tap"}, "region": "left"}),
                         [("ipad", "left")])
        # ipad has three regions and no 'full': a broadcast skips it
        self.assertEqual(self.pairs({"select": {"min_width": 1000}, "all": True}),
                         [("kiosk", "full")])
        self.assertEqual(self.pairs({"select": {"min_width": 1000}, "all": True,
                                     "region": "left"}), [("ipad", "left")])

    def test_ambiguous_region_is_refused_not_guessed(self):
        with self.assertRaises(SurfaceError) as ctx:
            resolve(self.reg, "ipad")
        self.assertEqual(ctx.exception.status, 409)

    def test_errors(self):
        for target, status in (("nope", 404), ("phone/nope", 404),
                               ({"select": {"kind": "none-such"}}, 404)):
            with self.assertRaises(SurfaceError) as ctx:
                resolve(self.reg, target)
            self.assertEqual(ctx.exception.status, status, target)
        for bad in (7, {}, {"surface": "a", "select": {}}, "a/b/c",
                    {"select": {"colour": "red"}}, {"select": {"touch": "wiggle"}}):
            with self.assertRaises(ValueError, msg=str(bad)):
                parse_target(bad)

    def test_expired_target_is_410(self):
        clock = Clock()
        reg = SurfaceRegistry(clock=clock)
        reg.register(phone())
        clock.t += 60
        with self.assertRaises(SurfaceError) as ctx:
            resolve(reg, "phone")
        self.assertEqual(ctx.exception.status, 410)


class Recorder(SurfaceRenderer):
    """A renderer nothing in displayd knows: proves the API is agnostic."""
    name = "swiftui"

    def __init__(self):
        self.drawn = []

    def validate(self, content):
        if content.get("type") == "scene" and "nodes" not in content:
            raise ValueError("scene: nodes required")

    def render(self, surface, region, content):
        self.drawn.append((surface["id"], region["name"], content["type"]))
        return {"rendered": True, "delivery": "push"}


class TestHub(unittest.TestCase):
    def setUp(self):
        self.hub = SurfaceHub(clock=Clock())
        self.recorder = Recorder()
        self.hub.register_renderer(self.recorder)
        for spec in (phone(), ipad(), kiosk()):
            self.hub.registry.register(spec)

    def test_registered_implementation_draws_and_unknown_renderer_relays(self):
        out = self.hub.deliver("kiosk", {"type": "text", "text": "hi"})
        self.assertEqual(out["delivered"][0]["rendered"], True)
        self.assertEqual(self.recorder.drawn, [("kiosk", "full", "text")])
        out = self.hub.deliver("phone", {"type": "text", "text": "hi"})
        self.assertEqual(out["delivered"][0]["delivery"], "pull")  # web-canvas: relay
        self.assertEqual(self.recorder.drawn, [("kiosk", "full", "text")])

    def test_content_type_is_checked_per_region(self):
        self.hub.deliver("ipad/left", {"type": "scene", "nodes": []})
        with self.assertRaises(SurfaceError) as ctx:
            self.hub.deliver("ipad/right", {"type": "scene", "nodes": []})
        self.assertEqual(ctx.exception.status, 415)
        with self.assertRaises(ValueError):
            self.hub.deliver("ipad/left", {"type": "scene"})  # impl.validate
        for bad in (None, [], {}, {"type": 3}, {"type": "text", "pad": "x" * 300000}):
            with self.assertRaises(ValueError, msg=str(bad)[:30]):
                self.hub.deliver("kiosk", bad)

    def test_broadcast_reports_delivered_and_skipped(self):
        out = self.hub.deliver({"select": {"min_width": 300}, "region": "full",
                                "all": True}, {"type": "image"})
        self.assertEqual([d["surface"] for d in out["delivered"]], ["phone"])
        self.assertEqual([s["surface"] for s in out["skipped"]], ["kiosk"])  # text only
        out = self.hub.deliver({"select": {"kind": "sidecar"}, "region": "right",
                                "all": True}, {"type": "text"})
        self.assertEqual([d["surface"] for d in out["delivered"]], ["ipad"])
        with self.assertRaises(SurfaceError) as ctx:
            self.hub.deliver({"select": {"min_width": 300}, "all": True},
                             {"type": "hologram"})
        self.assertEqual(ctx.exception.status, 415)

    def test_broadcast_skips_a_surface_that_cannot_take_it(self):
        out = self.hub.deliver({"select": {"min_width": 300}, "all": True},
                               {"type": "text"})
        got = sorted(d["surface"] for d in out["delivered"])
        self.assertEqual(got, ["kiosk", "phone"])  # ipad is ambiguous: not hit

    def test_slots_are_per_region_and_pullable_with_since(self):
        self.hub.deliver("ipad/left", {"type": "text", "n": 1})
        self.hub.deliver("ipad/right", {"type": "text", "n": 2})
        self.hub.deliver("ipad/left", {"type": "text", "n": 3})
        ch = self.hub.registry.channels
        everything = ch.read_content("ipad")
        self.assertEqual(everything["seq"], 3)
        self.assertEqual(everything["contents"]["left"]["content"]["n"], 3)
        self.assertEqual(everything["contents"]["right"]["content"]["n"], 2)
        self.assertEqual(list(ch.read_content("ipad", since=3)["contents"]), [])
        self.assertEqual(list(ch.read_content("ipad", since=2)["contents"]), ["left"])
        self.assertEqual(list(ch.read_content("ipad", region="right")["contents"]),
                         ["right"])

    def test_long_poll_wakes_on_send(self):
        ch = self.hub.registry.channels
        threading.Timer(0.15, lambda: self.hub.deliver(
            "phone", {"type": "text", "text": "late"})).start()
        started = time.time()
        got = ch.read_content("phone", since=0, wait=5)
        self.assertLess(time.time() - started, 3)
        self.assertEqual(got["contents"]["full"]["content"]["text"], "late")
        started = time.time()
        quiet = ch.read_content("phone", since=got["seq"], wait=0.2)
        self.assertEqual(quiet["contents"], {})
        self.assertGreaterEqual(time.time() - started, 0.15)


class TestTouch(unittest.TestCase):
    def setUp(self):
        self.reg = SurfaceRegistry(clock=Clock())
        for spec in (phone(), ipad(), kiosk()):
            self.reg.register(spec)

    def refused(self, sid, body, status):
        with self.assertRaises(SurfaceError) as ctx:
            self.reg.touch(sid, body)
        self.assertEqual(ctx.exception.status, status, body)

    def test_supported_interaction_is_recorded_with_local_coordinates(self):
        ev = self.reg.touch("ipad", {"interaction": "tap", "x": 600, "y": 100})
        self.assertEqual((ev["region"], ev["seq"]), ("right", 1))
        self.assertEqual(ev["local"], {"x": 88, "y": 100})
        sw = self.reg.touch("phone", {"interaction": "swipe", "x": 10, "y": 100,
                                      "dx": 120, "dy": 0, "duration_ms": 80})
        self.assertEqual((sw["region"], sw["dx"]), ("full", 120))
        read = self.reg.channels.read_events("ipad")
        self.assertEqual([e["interaction"] for e in read["events"]], ["tap"])

    def test_overlap_resolves_to_the_smallest_region(self):
        ev = self.reg.touch("ipad", {"interaction": "tap", "x": 950, "y": 30})
        self.assertEqual(ev["region"], "badge")
        ev = self.reg.touch("ipad", {"interaction": "tap", "x": 950, "y": 400})
        self.assertEqual(ev["region"], "right")

    def test_unsupported_interaction_and_surfaces_are_refused(self):
        self.refused("phone", {"interaction": "pinch", "x": 10, "y": 100}, 409)
        self.refused("ipad", {"interaction": "swipe", "x": 10, "y": 100}, 409)
        self.refused("kiosk", {"interaction": "tap", "x": 10, "y": 10}, 409)
        self.refused("nope", {"interaction": "tap", "x": 1, "y": 1}, 404)
        self.assertEqual(self.reg.channels.read_events("kiosk")["events"], [])

    def test_geometry_and_points_are_checked(self):
        for body in ({"interaction": "tap", "x": 5, "y": 5},       # under the inset
                     {"interaction": "tap", "x": 9999, "y": 100},
                     {"interaction": "tap", "x": "a", "y": 1},
                     {"interaction": "wiggle", "x": 1, "y": 100},
                     {"interaction": "tap", "x": 10, "y": 100, "points": 0}):
            with self.assertRaises(ValueError, msg=str(body)):
                self.reg.touch("phone", body)
        pinch = {"interaction": "pinch", "x": 100, "y": 100, "scale": 1.5}
        self.assertEqual(self.reg.touch("ipad", pinch)["points"], 2)
        self.refused("ipad", dict(pinch, points=3), 409)
        with self.assertRaises(ValueError):
            self.reg.touch("ipad", dict(pinch, points=1))

    def test_named_region_must_contain_the_point(self):
        self.refused("ipad", {"interaction": "tap", "x": 600, "y": 100,
                              "region": "left"}, 409)
        self.refused("ipad", {"interaction": "tap", "x": 1, "y": 1,
                              "region": "nope"}, 404)

    def test_region_without_touch_is_not_a_target(self):
        self.reg.register(ipad(id="mixed", regions=[
            {"name": "ui", "rect": {"x": 0, "y": 0, "w": 100, "h": 100}},
            {"name": "ticker", "touch": False,
             "rect": {"x": 0, "y": 100, "w": 100, "h": 100}}]))
        self.assertEqual(self.reg.touch("mixed", {"interaction": "tap", "x": 5,
                                                  "y": 5})["region"], "ui")
        self.refused("mixed", {"interaction": "tap", "x": 5, "y": 150}, 409)

    def test_events_long_poll_and_since(self):
        ch = self.reg.channels
        threading.Timer(0.15, lambda: self.reg.touch(
            "phone", {"interaction": "tap", "x": 10, "y": 100})).start()
        got = ch.read_events("phone", since=0, wait=5)
        self.assertEqual(len(got["events"]), 1)
        self.assertEqual(ch.read_events("phone", since=got["seq"])["events"], [])


if __name__ == "__main__":
    unittest.main()
