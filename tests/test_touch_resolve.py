"""Read-only POST /touch/resolve tests (no panel needed).

Contracts: a tap inside a region resolves to that region's semantic
action; a dead-space tap resolves to nothing; a tap the real path would
refuse on view/mode grounds reports refused (never an action); the
endpoint never dispatches, queues, or mutates anything observable.

Run from the repo root:  python3 -m pytest tests/test_touch_resolve.py -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
import touch
import touch_audit
from renderers import macbook_layout as lay
from renderers import picker as pk

W, H = 1920, 1080
RELOAD_SHA = "25e0e740074740b6b98896a6076bf2763fe598f1"

HOME = {"id": "home", "rect": [0, 0, 160, 160],
        "action": {"name": "select_view", "view": "picker"}}
SLEEP = {"id": "screen-off", "rect": [1760, 0, 160, 160],
         "action": {"name": "screen_off"}}
WAKE = {"id": "wake", "rect": [0, 0, 1920, 1080],
        "action": {"name": "screen_on"}}
RELOAD_OK = {"id": "reload-confirm", "rect": [0, 0, 1920, 1080],
             "action": {"name": "reload_confirm"}}


def macbook_scope():
    """Full announced macbook scope, generated (mode/tab/focus/map/click)."""
    return [{"id": e["id"], "rect": list(e["rect"]),
             "action": dict(e["action"])} for e in
            lay.touch_regions(W, H)]


def live_tiles(daemon):
    views = daemon._expected_picker_views({})
    rect = pk.default_rect(W, H)
    return [{"id": "view-%s" % v, "rect": list(r),
             "action": {"name": "select_view", "view": v}}
            for v, r in zip(views, pk.grid_geometry(rect, len(views)))]


def tile_center(rect):
    x, y, w, h = rect
    return x + w // 2, y + h // 2


class ResolveDaemonTestCase(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore_env)
        self.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            feedback_path=os.path.join(self.tmp.name, "feedback.jsonl"))
        self.addCleanup(self.daemon.clear)
        self.addCleanup(self.daemon.playlist.stop)
        self.addCleanup(self.daemon.stop_watchdog)

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = self._env

    def _announce_full(self):
        return self.daemon.announce_touch(
            {"regions": [dict(HOME), dict(SLEEP)],
             "view_regions": {
                 "picker": live_tiles(self.daemon),
                 "macbook": macbook_scope(),
                 "sleep": [dict(WAKE)]}})


class UnknownTest(ResolveDaemonTestCase):
    def test_no_heartbeat_is_unknown(self):
        report = self.daemon.resolve_touch(100, 100)
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "unknown")


class StaticGridTest(ResolveDaemonTestCase):
    def test_tile_hit_resolves_action(self):
        self._announce_full()
        self.daemon.show("picker", {})
        tiles = live_tiles(self.daemon)
        x, y = tile_center(tiles[0]["rect"])
        report = self.daemon.resolve_touch(x, y)
        self.assertTrue(report["ok"])
        self.assertTrue(report["hit"])
        self.assertEqual(report["region"], tiles[0]["id"])
        self.assertEqual(report["action"],
                         tiles[0]["action"])
        self.assertFalse(report["refused"])
        self.assertFalse(report["dispatched"])

    def test_dead_space_resolves_nothing(self):
        self._announce_full()
        self.daemon.show("clock", {})
        report = self.daemon.resolve_touch(960, 900)
        self.assertTrue(report["ok"])
        self.assertFalse(report["hit"])
        self.assertIsNone(report["region"])
        self.assertIsNone(report["action"])
        self.assertIsNone(report["consumed_by"])

    def test_home_badge_serves_plain_views(self):
        self._announce_full()
        self.daemon.show("clock", {})
        report = self.daemon.resolve_touch(80, 80)
        self.assertTrue(report["hit"])
        self.assertEqual(report["region"], "home")
        self.assertEqual(report["action"],
                         {"name": "select_view", "view": "picker"})

    def test_scoped_tiles_do_not_leak_across_views(self):
        self._announce_full()
        self.daemon.show("clock", {})
        tiles = live_tiles(self.daemon)
        x, y = tile_center(tiles[0]["rect"])
        report = self.daemon.resolve_touch(x, y)
        # Picker scope is not live on clock: the tile point hits
        # nothing (or global chrome), never the tile action.
        if report["hit"]:
            self.assertNotEqual(report["region"], tiles[0]["id"])

    def test_parity_with_touch_hit_test(self):
        # The daemon answer must match what the touch service resolves:
        # same mode-aware candidates, same first-hit order, over the same
        # announce. Macbook is checked in BOTH modes: in AIM the
        # GLANCE-only map/strip regions are not live, so taps fall through
        # to the click catcher; in GLANCE the click catcher is not live.
        self._announce_full()
        announced = self.daemon.touch_live["announced"]
        plans = [("picker", None), ("macbook", "glance"),
                 ("macbook", "aim"), ("clock", None),
                 ("sleep", None)]
        for view, mode in plans:
            if view == "macbook":
                self.daemon.show("macbook", {"mode": mode})
            elif view == "clock":
                self.daemon.show("clock", {})
            elif view == "picker":
                self.daemon.show("picker", {})
            elif view == "sleep":
                self.daemon.show("sleep", {})
            candidates = touch_audit.candidates_for_mode(
                announced["regions"],
                announced["view_regions"], view, mode)
            for x, y in ((80, 80), (960, 540), (1800, 100),
                         (5, 200), (100, 1000), (1919, 1079), (0, 0)):
                want = touch.hit_test(x, y, candidates)
                got = self.daemon.resolve_touch(x, y)
                self.assertEqual(
                    got["region"], want,
                    "view=%r mode=%r tap=%r: daemon=%r touch=%r"
                    % (view, mode, (x, y), got["region"], want))


class ViewModeGateTest(ResolveDaemonTestCase):
    def _map_point(self):
        scope = macbook_scope()
        mac = next(e for e in scope
                   if e["action"].get("name") == "macbook_mouse")
        return mac["rect"][0] + 5, mac["rect"][1] + 5

    def test_mouse_applies_in_glance(self):
        self._announce_full()
        self.daemon.show("macbook", {"mode": "glance"})
        report = self.daemon.resolve_touch(*self._map_point())
        self.assertEqual(report["region"], "mac-map")
        self.assertFalse(report["refused"])
        self.assertEqual(report["action"]["name"], "macbook_mouse")
        # Tap-positioned: the point is stamped into the answer.
        self.assertEqual((report["action"]["x"], report["action"]["y"]),
                         (report["x"], report["y"]))
        self.assertIn("revalidation", report)

    def test_second_tap_reaches_click_in_aim(self):
        # The tap-tap fix: in AIM the GLANCE-only map/strip regions are
        # not live, so a tap on the review image falls through to the
        # fullscreen click catcher -- it dispatches the click instead of
        # reporting the map refused.
        self._announce_full()
        self.daemon.show("macbook", {"mode": "aim"})
        report = self.daemon.resolve_touch(*self._map_point())
        self.assertTrue(report["hit"])
        self.assertEqual(report["region"], "mac-zoom")
        self.assertFalse(report["refused"])
        self.assertEqual(report["action"]["name"], "macbook_click")
        # Tap-positioned: the point is stamped into the answer.
        self.assertEqual((report["action"]["x"], report["action"]["y"]),
                         (report["x"], report["y"]))
        self.assertIn("revalidation", report)

    def test_click_catcher_not_live_in_glance(self):
        # In GLANCE the fullscreen click catcher is not live: a tap the
        # map does not cover (header dead area) falls through to shared
        # chrome instead of reporting a refused click.
        self._announce_full()
        self.daemon.show("macbook", {"mode": "glance"})
        report = self.daemon.resolve_touch(80, 80)
        self.assertTrue(report["hit"])
        self.assertEqual(report["region"], "home")
        self.assertFalse(report["refused"])

    def test_click_gate_still_refuses_in_glance(self):
        self._announce_full()
        self.daemon.show("macbook", {"mode": "glance"})
        zoom = next(e for e in macbook_scope()
                    if e["action"].get("name") == "macbook_click")
        # The click catcher is not live in GLANCE, so a tap inside the
        # map resolves to the map warp (never a refused click); the
        # click gate itself still refuses GLANCE directly -- the daemon
        # revalidates at dispatch, and the touch dispatcher never sends
        # it there.
        report = self.daemon.resolve_touch(
            zoom["rect"][0] + 960, zoom["rect"][1] + 800)
        self.assertTrue(report["hit"])
        self.assertEqual(report["region"], "mac-map")
        self.assertFalse(report["refused"])
        self.assertEqual(
            self.daemon._resolve_refusal("macbook_click", "macbook"),
            None if self.daemon._macbook_mode() == "aim"
            else self.daemon._resolve_refusal("macbook_click", "macbook"))
        self.assertIsNotNone(
            self.daemon._resolve_refusal("macbook_click", "macbook"))

    def test_gated_names_refuse_off_view(self):
        self._announce_full()
        self.daemon.show("clock", {})
        for name in ("macbook_mouse", "macbook_click", "talon_focus",
                     "talon_tab", "macbook_mode"):
            reason = self.daemon._resolve_refusal(name, "clock")
            self.assertIsNotNone(reason, name)
            self.assertIn("clock", reason)
        self.assertIsNone(
            self.daemon._resolve_refusal("macbook_mode", "macbook"))
        self.assertIsNone(
            self.daemon._resolve_refusal("select_view", "clock"))

    def test_reload_confirm_refused_when_none_active(self):
        self.daemon.announce_touch(
            {"regions": [dict(RELOAD_OK)]})
        self.daemon.show("clock", {})
        report = self.daemon.resolve_touch(960, 540)
        self.assertTrue(report["hit"])
        self.assertTrue(report["refused"])
        self.assertEqual(report["reason"], "no-reload-active")


class ReloadPrecedenceTest(ResolveDaemonTestCase):
    def test_dead_zone_consumed_while_reload_active(self):
        # Home badge only: (960, 360) is dead space, and the active
        # reload transient would consume that tap on dismissal.
        self.daemon.announce_touch({"regions": [dict(HOME)]})
        self.daemon.show("clock", {})
        self.daemon.reload(RELOAD_SHA)
        self.addCleanup(self.daemon.dismiss_reload)
        report = self.daemon.resolve_touch(960, 360)
        self.assertFalse(report["hit"])
        self.assertEqual(report["consumed_by"], "reload-dismiss")

    def test_reload_confirm_applies_while_active(self):
        self.daemon.announce_touch(
            {"regions": [dict(RELOAD_OK)]})
        self.daemon.show("clock", {})
        self.daemon.reload(RELOAD_SHA)
        self.addCleanup(self.daemon.dismiss_reload)
        report = self.daemon.resolve_touch(10, 10)
        self.assertTrue(report["hit"])
        self.assertEqual(report["region"], "reload-confirm")
        self.assertFalse(report["refused"])


class CoordParsingTest(ResolveDaemonTestCase):
    def test_normalized_edges(self):
        self.assertEqual(
            displayd._resolve_coords(
                {"x_norm": 0.0, "y_norm": 0.0}, W, H), (0, 0))
        self.assertEqual(
            displayd._resolve_coords(
                {"x_norm": 1.0, "y_norm": 1.0}, W, H), (W - 1, H - 1))

    def test_pixels_win_over_fractions(self):
        self.assertEqual(
            displayd._resolve_coords(
                {"x": 7, "y": 9, "x_norm": 1.0, "y_norm": 1.0},
                W, H), (7, 9))

    def test_rejects(self):
        for body in ({}, {"x": 1}, {"y": 1},
                     {"x_norm": 2.0, "y_norm": 0.5},
                     {"x_norm": -0.1, "y_norm": 0.5},
                     {"x_norm": True, "y_norm": 0.5}):
            with self.assertRaises(ValueError, msg=repr(body)):
                displayd._resolve_coords(body, W, H)

    def test_resolve_rejects_malformed(self):
        self._announce_full()
        for x, y in ((True, 5), ("5", 5), (5.0, 5),
                     (-1, 5), (W, 5), (5, H)):
            with self.assertRaises(ValueError, msg=repr((x, y))):
                self.daemon.resolve_touch(x, y)


class ReadOnlyTest(ResolveDaemonTestCase):
    def test_nothing_moves(self):
        self._announce_full()
        self.daemon.show("macbook", {"mode": "glance"})
        before = (self.daemon.current, dict(self.daemon.current_params),
                  self.daemon.touch_live["regions_sha"],
                  self.daemon.policy.active,
                  getattr(self.daemon, "mouse_pending", None),
                  getattr(self.daemon, "focus_pending", None),
                  getattr(self.daemon, "click_pending", None))
        scope = macbook_scope()
        mac = next(e for e in scope
                   if e["action"].get("name") == "macbook_mouse")
        self.daemon.resolve_touch(mac["rect"][0] + 5, mac["rect"][1] + 5)
        self.daemon.resolve_touch(960, 900)
        self.daemon.resolve_touch(80, 80)
        after = (self.daemon.current, dict(self.daemon.current_params),
                 self.daemon.touch_live["regions_sha"],
                 self.daemon.policy.active,
                 getattr(self.daemon, "mouse_pending", None),
                 getattr(self.daemon, "focus_pending", None),
                 getattr(self.daemon, "click_pending", None))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
