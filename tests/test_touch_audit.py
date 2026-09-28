"""Drawn-vs-live touch region assertion tests (no panel needed).

Contracts: per-view region sets (global + view_regions.<view> only while
that view shows), the RUNNING service's announced set compared against
DRAWN geometry as a per-view matrix, diffs naming exact rects/ids.
DaemonDriftTest stages the macbook incident (no mac-map scope ->
views.macbook MISMATCH) and the fix (scoped mac-map -> ok), plus the
acceptance pair: one screen point dispatches macbook_mouse while the
macbook view shows and the picker tile while the picker shows.

Run from the repo root:  python3 -m pytest tests/test_touch_audit.py -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
import touch
import touch_audit
from renderers import picker as pk

W, H = 1920, 1080
MAP = {"id": "mac-map", "rect": [0, 250, 1920, 490],
       "action": {"name": "macbook_mouse"}}
ZOOM = {"id": "mac-zoom", "rect": [0, 740, 1920, 340],
        "action": {"name": "macbook_click"}}
TILE = {"id": "view-clock", "rect": [179, 59, 508, 401],
        "action": {"name": "select_view", "view": "clock"}}
FOCUS = {"id": "talon-focus-left", "rect": [48, 250, 560, 782],
         "action": {"name": "talon_focus"}}
FOCUS_RIGHT = {"id": "talon-focus-right",
               "rect": [1312, 250, 560, 782],
               "action": {"name": "talon_focus"}}
HOME = {"id": "home", "rect": [0, 0, 160, 160],
        "action": {"name": "select_view", "view": "picker"}}


def _region(rid, rect, action=None):
    return {"id": rid, "rect": list(rect),
            "action": dict(action or {"name": "screen_on"})}


class CandidatesTest(unittest.TestCase):
    def test_scoped_first_then_global(self):
        out = touch_audit.candidates(
            [_region("g", [0, 0, 1, 1])],
            {"v": [_region("s", [0, 0, 2, 2])]}, "v")
        self.assertEqual([r["id"] for r in out], ["s", "g"])

    def test_other_view_gets_global_only(self):
        out = touch_audit.candidates(
            [_region("g", [0, 0, 1, 1])],
            {"v": [_region("s", [0, 0, 2, 2])]}, "other")
        self.assertEqual([r["id"] for r in out], ["g"])

    def test_unknown_view_is_global_only(self):
        out = touch_audit.candidates([_region("g", [0, 0, 1, 1])],
                                     {"v": [_region("s", [0, 0, 2, 2])]},
                                     None)
        self.assertEqual([r["id"] for r in out], ["g"])


class CompareTest(unittest.TestCase):
    def test_identical_is_ok(self):
        expected = [{"id": "view-clock", "rect": [1, 2, 3, 4],
                     "action": {"name": "select_view", "view": "clock"}}]
        report = touch_audit.compare_exact(
            expected, touch_audit.normalize_live(expected))
        self.assertTrue(report["ok"])
        self.assertEqual(report["unasserted"], [])

    def test_moved_rect_names_both(self):
        expected = [{"id": "view-clock", "rect": [1, 2, 3, 4],
                     "action": {"name": "select_view", "view": "clock"}}]
        live = touch_audit.normalize_live(
            [_region("view-clock", [9, 9, 9, 9],
                      {"name": "select_view", "view": "clock"})])
        report = touch_audit.compare_exact(expected, live)
        self.assertFalse(report["ok"])
        moved = report["moved"][0]
        self.assertEqual((moved["expected_rect"], moved["live_rect"]),
                         ([1, 2, 3, 4], [9, 9, 9, 9]))
        self.assertFalse(moved["action_changed"])

    def test_same_rect_new_action_is_drift(self):
        expected = [{"id": "view-clock", "rect": [1, 2, 3, 4],
                     "action": {"name": "select_view", "view": "clock"}}]
        live = touch_audit.normalize_live(
            [_region("view-clock", [1, 2, 3, 4],
                      {"name": "select_view", "view": "chat"})])
        report = touch_audit.compare_exact(expected, live)
        self.assertFalse(report["ok"])
        self.assertTrue(report["moved"][0]["action_changed"])

    def test_operator_wiring_never_fails(self):
        expected = [{"id": "home", "rect": [0, 0, 160, 160], "action": None}]
        live = touch_audit.normalize_live([
            _region("home", [0, 0, 160, 160]),
            _region("playlist-next", [1760, 0, 160, 1080]),
            _region("reload-confirm", [0, 0, 1920, 1080],
                    {"name": "reload_confirm"})])
        report = touch_audit.compare_exact(expected, live)
        self.assertTrue(report["ok"])
        self.assertEqual(report["unasserted"],
                         ["playlist-next", "reload-confirm"])

    def test_presence_only_checks_action(self):
        live = touch_audit.normalize_live([dict(MAP)])
        self.assertTrue(touch_audit.compare_presence(
            [{"action": "macbook_mouse"}], live)["ok"])
        self.assertEqual(touch_audit.compare_presence(
            [{"action": "macbook_mouse"}], {})["missing"],
            [{"action": "macbook_mouse"}])


class ValidateTest(unittest.TestCase):
    def test_scoped_roundtrip(self):
        cfg = {"regions": [_region("g", [0, 0, 1, 1])],
               "view_regions": {"macbook": [dict(MAP)]}}
        payload = touch_audit.announce_payload(cfg)
        self.assertEqual(payload["regions_sha"],
                         touch_audit.regions_sha(payload))
        cleaned = touch_audit.validate_announce(payload)
        self.assertEqual(cleaned["regions"], payload["regions"])
        self.assertEqual(cleaned["view_regions"], payload["view_regions"])

    def test_rejects(self):
        scoped = {"macbook": [dict(MAP)]}
        for body, _why in [
                ("nope", "non-object"), ({}, "nothing at all"),
                ({"regions": []}, "empty"),
                ({"regions": [{"id": "a", "rect": [0, 0, 1]}]},
                 "short rect"),
                ({"regions": [{"id": "a", "rect": [0, 0, 1, 1]}]},
                 "no action"),
                ({"regions": [_region("a", [0, 0, 1, 1])],
                  "view_regions": {"v": [_region("a", [1, 1, 1, 1])]}},
                 "dup id across scopes"),
                ({"regions": [_region("a", [0, 0, 1, 1])],
                  "view_regions": {"bad/view": [_region("b", [0, 0, 1, 1])]}},
                 "slash view"),
                ({"regions": [_region("a", [0, 0, 1, 1])],
                  "view_regions": {"v": []}}, "empty scope"),
                ({"regions": [_region("a", [0, 0, 1, 1])],
                  "view_regions": []}, "scope not an object")]:
            with self.assertRaises(ValueError, msg=_why):
                touch_audit.validate_announce(body)
        # Scoped-only announce is valid (no global regions required).
        cleaned = touch_audit.validate_announce(
            {"view_regions": scoped})
        self.assertEqual(cleaned["regions"], [])


class ExpectedTest(unittest.TestCase):
    def test_picker_default_six_match_drawn_contract(self):
        expected = touch_audit.expected_for_view("picker", {}, W, H)
        self.assertTrue(expected["checkable"])
        self.assertEqual([e["id"] for e in expected["exact"]],
                         ["view-%s" % v for v in pk.DEFAULT_VIEWS])
        self.assertEqual(expected["exact"][0]["rect"], [179, 59, 508, 401])
        self.assertNotIn("home", [e["id"] for e in expected["exact"]])

    def test_plain_view_draws_only_home(self):
        expected = touch_audit.expected_for_view("clock", {}, W, H)
        self.assertEqual([(e["id"], e["rect"]) for e in expected["exact"]],
                         [("home", [0, 0, 160, 160])])

    def test_macbook_requires_mouse_presence(self):
        expected = touch_audit.expected_for_view("macbook", {}, W, H)
        self.assertIn({"action": "macbook_mouse"}, expected["presence"])

    def test_talon_apps_requires_focus_presence(self):
        expected = touch_audit.expected_for_view("talon_apps", {}, W, H)
        self.assertIn({"action": "talon_focus"}, expected["presence"])
        self.assertIn("home", [e["id"] for e in expected["exact"]])

    def test_retro_cells_plus_badge(self):
        expected = touch_audit.expected_for_view("retro_grid", {}, W, H)
        ids = [e["id"] for e in expected["exact"]]
        self.assertEqual(len([i for i in ids if i.startswith("retro-cell-")]),
                         12)
        self.assertIn("home", ids)


class ScopeConfigTest(unittest.TestCase):
    def test_scoped_regions_validate(self):
        cfg = touch.load_config(None)
        cfg["view_regions"] = {"macbook": [dict(MAP)],
                               "picker": [dict(TILE)]}
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump(cfg, fh)
            path = fh.name
        try:
            loaded = touch.load_config(path)
        finally:
            os.unlink(path)
        self.assertEqual(loaded["view_regions"]["macbook"], [MAP])

    def test_duplicate_id_across_scopes_rejected(self):
        cfg = touch.load_config(None)
        cfg["regions"] = [dict(HOME)]
        cfg["view_regions"] = {"picker": [dict(HOME)]}
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump(cfg, fh)
            path = fh.name
        try:
            with self.assertRaises(ValueError):
                touch.load_config(path)
        finally:
            os.unlink(path)

    def test_scoped_tiles_join_check_views(self):
        cfg = touch.load_config(None)
        cfg["view_regions"] = {"picker": [dict(TILE)]}
        cfg["tap_options"] = {"enabled": True, "renderer": "picker",
                                "params": {}}
        report = touch.check_views(
            cfg, fetch=lambda _url: ["clock", "picker"])
        self.assertTrue(report["ok"])
        self.assertIn({"region": "view-clock", "view": "clock"},
                      report["selected"])
        report = touch.check_views(cfg, fetch=lambda _url: ["clock"])
        self.assertFalse(report["ok"])
        self.assertIn("picker", report["unknown"])


class ViewClient:
    """Fake DisplaydClient showing one view; records dispatches."""

    def __init__(self, view):
        self.view = view
        self.dispatched = []

    def state(self, timeout=None):
        return {"renderer": self.view}

    def tap_dismiss(self, dry_run=False):
        return {"action": "tap_dismiss", "response": {"dismissed": False},
                "dry_run": dry_run}

    def dispatch(self, action, dry_run=False, panel=None):
        self.dispatched.append(action)
        return {"action": action.get("name"), "dry_run": dry_run}


class AcceptancePairTest(unittest.TestCase):
    """Captain's criterion: one screen point, two views, two outcomes."""

    def _service(self, view):
        cfg = touch.load_config(None)
        cfg.update({"width": W, "height": H, "tap_options": {"enabled": False,
                                                             "renderer": "x",
                                                             "params": {}},
                    "calibration": {"x_min": 0, "x_max": W,
                                    "y_min": 0, "y_max": H},
                    "tap_max_seconds": 60, "debounce_seconds": 0,
                    "regions": [dict(HOME)],
                    "view_regions": {"picker": [dict(TILE)],
                                     "macbook": [dict(MAP)],
                                     "talon_apps": [dict(FOCUS)]}})
        return touch.TouchService(cfg, client=ViewClient(view))

    def _tap(self, svc, x, y):
        svc.handle_frame([touch.TouchEvent("down", 0, x, y)])
        return svc.handle_frame([touch.TouchEvent("up", 0, x, y)])

    def test_same_point_mouse_on_macbook_tile_on_picker(self):
        x, y = 300, 300  # inside both the map area and the clock tile
        mac = self._service("macbook")
        summary = self._tap(mac, x, y)
        self.assertEqual(summary["action"], "macbook_mouse")
        sent = mac.client.dispatched[0]
        self.assertEqual((sent["x"], sent["y"]), (x, y))
        picker = self._service("picker")
        summary = self._tap(picker, x, y)
        self.assertEqual(summary["action"], "select_view")
        self.assertEqual(picker.client.dispatched[0]["view"], "clock")

    def test_picker_tiles_not_live_on_macbook(self):
        svc = self._service("macbook")
        self._tap(svc, 960, 540)
        self.assertNotIn("select_view",
                         [a.get("name") for a in svc.client.dispatched])

    def test_talon_tap_focuses_row_on_its_view(self):
        # (200, 500) sits on a left-column side button; the centre gap
        # (960, 500) dispatches nothing by design.
        svc = self._service("talon_apps")
        summary = self._tap(svc, 200, 500)
        self.assertEqual(summary["action"], "talon_focus")
        sent = svc.client.dispatched[0]
        self.assertEqual((sent["x"], sent["y"]), (200, 500))
        other = self._service("picker")
        self._tap(other, 200, 500)
        self.assertNotIn("talon_focus",
                         [a.get("name")
                          for a in other.client.dispatched])

    def test_unknown_view_is_global_only(self):
        svc = self._service(None)
        summary = self._tap(svc, 960, 540)
        self.assertIsNone(summary)  # dead zone: fallback disabled
        home = self._service(None)
        self._tap(home, 80, 80)
        self.assertEqual(home.client.dispatched[0]["view"], "picker")


class DaemonDriftTest(unittest.TestCase):
    """The macbook incident, staged: missing scope FAILS, fix PASSES."""

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

    def _announce(self, regions, view_regions=None):
        return self.daemon.announce_touch(
            {"regions": regions, "view_regions": view_regions or {}})

    def _live_tiles(self):
        views = self.daemon._expected_picker_views({})
        rect = pk.default_rect(W, H)
        return [{"id": "view-%s" % v, "rect": list(r),
                 "action": {"name": "select_view", "view": v}}
                for v, r in zip(
                    views, pk.grid_geometry(rect, len(views)))]

    def test_unknown_until_heartbeat(self):
        report = self.daemon.touch_check()
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "unknown")

    def test_missing_mac_scope_fails_then_fix_passes(self):
        views = self.daemon._expected_picker_views({})
        rect = pk.default_rect(W, H)
        tiles = [{"id": "view-%s" % v, "rect": list(r),
                  "action": {"name": "select_view", "view": v}}
                 for v, r in zip(
                     views, pk.grid_geometry(rect, len(views)))]
        # LIVE failing case: tiles global (as on the host today), no
        # mac-map scope. Map taps hit tiles or nothing -- never the mouse.
        self._announce([dict(HOME)] + tiles)
        failed = self.daemon.touch_check()
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["status"], "mismatch")
        mac = failed["views"]["macbook"]
        self.assertFalse(mac["ok"])
        self.assertEqual(mac["presence_missing"],
                         [{"action": "macbook_mouse"}])
        # FIX: tiles scoped to picker, map scoped to macbook.
        self._announce([dict(HOME)], {"picker": tiles,
                                       "macbook": [dict(MAP)],
                                       "talon_apps": [dict(FOCUS)]})
        passed = self.daemon.touch_check()
        self.assertTrue(passed["ok"], json.dumps(passed, indent=2))
        self.assertEqual(passed["status"], "ok")
        self.assertTrue(passed["views"]["picker"]["ok"])
        self.assertTrue(passed["views"]["macbook"]["ok"])

    def test_current_view_carries_params_and_detail(self):
        self.daemon.show("clock", {})
        self.assertEqual(self.daemon.state()["params"], {})
        self._announce([dict(HOME)], {"picker": self._live_tiles(),
                                        "macbook": [dict(MAP)],
                                        "talon_apps": [dict(FOCUS)]})
        report = self.daemon.touch_check()
        self.assertTrue(report["ok"])
        self.assertEqual(report["current"]["view"], "clock")
        self.assertEqual(report["current"]["missing"], [])

    def test_announce_rejects_garbage(self):
        with self.assertRaises(ValueError):
            self.daemon.announce_touch({"regions": []})
        with self.assertRaises(ValueError):
            self.daemon.announce_touch({"nope": 1})
