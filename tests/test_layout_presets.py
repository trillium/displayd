"""Named layout styles and the capability vocabulary behind them.

A view declares how much of the panel it can render into
(``capability.py``: FULL / PARTIAL / PRIMARY); ``POST /layout`` refuses a
full-panel-only view in a reduced region, and the named styles
(``layout_presets.py``) are built on that same grammar rather than on a
second layout system. No framebuffer needed: DISPLAYD_FAKE_FB=1 selects the
in-memory double (1920x1080).

Run from the repo root:  python3 -m unittest tests.test_layout_presets -v
"""

import io
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import capability as caps
import displayd
import layout_presets as presets
from displayd import parse_layout
from PIL import Image

W, H = 1920, 1080


def _renderers():
    return displayd.load_renderers(displayd.RENDERER_DIR)


class TestVocabulary(unittest.TestCase):
    """The declared vocabulary is small and fails in the safe direction."""

    def test_case_and_space_tolerated(self):
        self.assertEqual(caps.coerce(" PARTIAL "), caps.PARTIAL)
        self.assertEqual(caps.coerce("Primary"), caps.PRIMARY)

    def test_an_undeclared_or_bad_declaration_is_full(self):
        for value in (None, "", "partiall", 1, True, [], "split"):
            self.assertEqual(caps.coerce(value), caps.FULL, value)

    def test_reduced_ok(self):
        self.assertTrue(caps.reduced_ok(caps.PARTIAL))
        self.assertTrue(caps.reduced_ok(caps.PRIMARY))
        self.assertFalse(caps.reduced_ok(caps.FULL))
        self.assertFalse(caps.reduced_ok(None))

    def test_offered_excludes_broken_param_gated_and_full_only(self):
        names = caps.offered(_renderers())
        self.assertIn("picker", names)
        self.assertNotIn("text", names)  # needs a required param
        self.assertNotIn("beads", names)  # full-panel only
        self.assertNotIn("nope", names)

    def test_offered_is_sorted_and_defensive(self):
        self.assertEqual(caps.offered({
            "y": {"module": object(), "capability": "partial"},
            "w": {"module": object(), "capability": "PARTIAL"},
            "z": {"module": object(), "capability": "full"},
            "broken": {"broken": "boom"},
            "junk": "not an entry",
        }), ["w", "y"])
        self.assertEqual(caps.offered(None), [])
        self.assertEqual(caps.offered({"a/b": {"module": object()}}), [])


class TestPresetGeometry(unittest.TestCase):
    """Every style is geometry in the existing /layout grammar."""

    def _parse(self, payload):
        return parse_layout(payload, W, H, _renderers())

    def test_every_style_builds_and_parses(self):
        for name in presets.PRESET_NAMES:
            regions = self._parse(presets.build({"preset": name},
                                               _renderers()))
            self.assertTrue(regions, name)
            self.assertEqual(len(regions), len(presets.PRESETS[name]["regions"]))
            self.assertEqual([r["name"] for r in regions],
                             [s for s, _g in presets.PRESETS[name]["regions"]])

    def test_full_is_the_whole_panel(self):
        regions = self._parse(presets.build({"preset": "full"},
                                           _renderers()))
        self.assertEqual([r["rect"] for r in regions], [(0, 0, W, H)])

    def test_split_fifty_fifty_is_two_equal_rows(self):
        regions = self._parse(presets.build({"preset": "split-50-50"},
                                           _renderers()))
        self.assertEqual([r["rect"] for r in regions],
                         [(0, 0, W, 540), (0, 540, W, 540)])

    def test_split_fifty_fifty_columns_is_two_half_width_columns(self):
        regions = self._parse(presets.build({"preset": "split-50-50-columns"},
                                           _renderers()))
        self.assertEqual([r["rect"] for r in regions],
                         [(0, 0, 960, H), (960, 0, 960, H)])

    def test_bands_geometry_is_15_70_15(self):
        regions = self._parse(presets.build({"preset": "15-70-15"},
                                           _renderers()))
        self.assertEqual([r["rect"] for r in regions],
                         [(0, 0, 288, H), (288, 0, 1344, H), (1632, 0, 288, H)])

    def test_bands_roles(self):
        roles = presets.slot_roles("15-70-15")
        self.assertEqual(roles, {"left": presets.NAVIGATION,
                                 "center": presets.PRIMARY,
                                 "right": presets.NAVIGATION})

    def test_bands_default_to_the_row_centre_and_tappable_bands(self):
        renderers = _renderers()
        regions = self._parse(presets.build({"preset": "15-70-15"}, renderers))
        by_name = {r["name"]: r for r in regions}
        self.assertEqual(by_name["center"]["renderer"], "row")
        self.assertEqual(by_name["center"]["rect"][2], 1344)
        for band in ("left", "right"):
            self.assertEqual(by_name[band]["renderer"], "picker")
            self.assertEqual(by_name[band]["rect"][2], 288)

    def test_bands_carry_the_applicable_list(self):
        renderers = _renderers()
        regions = self._parse(presets.build({"preset": "15-70-15"}, renderers))
        band = [r for r in regions if r["name"] == "left"][0]
        views = band["params"]["views"]
        self.assertTrue(views)
        self.assertNotIn("beads", views)  # full-panel only
        self.assertNotIn("text", views)  # needs a param it has none of
        self.assertEqual(views, caps.offered(renderers))

    def test_explicit_views_win(self):
        regions = self._parse(presets.build(
            {"preset": "15-70-15",
             "views": {"center": "chat", "left": "options",
                       "right": "clock"}}, _renderers()))
        self.assertEqual([r["renderer"] for r in regions],
                         ["options", "chat", "clock"])

    def test_defaults_do_not_repeat_a_view_in_a_split(self):
        regions = self._parse(presets.build({"preset": "split-50-50"},
                                           _renderers()))
        self.assertNotEqual(regions[0]["renderer"], regions[1]["renderer"])

    def test_unknown_preset_or_slot_is_refused(self):
        with self.assertRaises(ValueError):
            presets.build(["preset"], _renderers())
        with self.assertRaises(ValueError) as ctx:
            presets.build({"preset": "nope"}, _renderers())
        self.assertIn("nope", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            presets.build({"preset": "15-70-15", "views": {"top": "row"}},
                          _renderers())
        self.assertIn("top", str(ctx.exception))
        with self.assertRaises(ValueError):
            presets.build({"preset": "15-70-15", "views": ["row"]},
                          _renderers())
        with self.assertRaises(ValueError):
            presets.build({"preset": "15-70-15",
                           "views": {"center": ""}}, _renderers())

    def test_a_slot_with_no_fitting_view_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            presets.build({"preset": "split-50-50"}, {})
        self.assertIn("no loaded view fits", str(ctx.exception))

    def test_capability_decides_which_views_are_offered(self):
        renderers = _renderers()
        for preset in presets.PRESET_NAMES:
            for slot in presets.doc(renderers)[presets.PRESET_NAMES.index(
                    preset)]["slots"]:
                for name in slot["views"]:
                    self.assertTrue(
                        caps.reduced_ok(renderers[name]["capability"]),
                        "%s offers %s" % (preset, name))

    def test_doc_lists_every_style_with_its_slots(self):
        doc = presets.doc(_renderers())
        self.assertEqual([d["name"] for d in doc],
                         list(presets.PRESET_NAMES))
        for entry in doc:
            self.assertEqual([s["name"] for s in entry["slots"]],
                             [s for s, _g in presets.PRESETS[entry["name"]]["regions"]])
            self.assertTrue(entry["label"])
            for slot in entry["slots"]:
                self.assertIn(slot["role"],
                              (presets.PRIMARY, presets.NAVIGATION,
                               presets.PLAIN))
                self.assertIn(slot["default"], slot["views"])


class PresetHttpTestCase(unittest.TestCase):
    """The styles over the real HTTP surface, in the in-memory double."""

    def setUp(self):
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore_env)
        self._old_daemon = displayd.DAEMON
        self.addCleanup(self._restore_daemon)
        self.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            feedback_path=os.path.join(self.tmp.name, "feedback.jsonl"))
        displayd.DAEMON = self.daemon
        server = ThreadingHTTPServer(("127.0.0.1", 0), displayd.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(self.daemon.clear)
        self.base = "http://127.0.0.1:%d" % server.server_address[1]

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = self._env

    def _restore_daemon(self):
        displayd.DAEMON = self._old_daemon

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                if "image/" in resp.headers.get("Content-Type", ""):
                    return resp.status, raw
                return resp.status, json.loads(raw.decode() or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def _wait_frame(self, timeout=8.0):
        """Wait for every region of the active layout to have drawn once,
        then return the composited frame -- a fresh snapshot can still be
        the first composite (black regions), which is not a failure."""
        end = time.time() + timeout
        while time.time() < end:
            code, layout = self.call("GET", "/layout")
            if (code == 200 and layout.get("layout")
                    and all(r.get("updated_at")
                            for r in layout["layout"]["regions"])):
                code, out = self.call("GET", "/snapshot")
                if code == 200 and out:
                    return Image.open(io.BytesIO(out)).convert("RGB")
            time.sleep(0.05)
        self.fail("the layout never painted")

    def test_every_style_renders_over_http(self):
        # Explicit views only: no style's default reaches for a remote feed.
        choices = {
            "full": {"view": "clock"},
            "split-50-50": {"top": "clock", "bottom": "picker"},
            "split-50-50-columns": {"left": "clock", "right": "options"},
            "15-70-15": {"left": "picker", "center": "clock",
                         "right": "options"},
        }
        for name, views in choices.items():
            code, out = self.call("POST", "/layout",
                                  {"preset": name, "views": views})
            self.assertEqual(code, 200, (name, out))
            self.assertTrue(self._wait_frame(), name)
            code, layout = self.call("GET", "/layout")
            self.assertEqual(code, 200)
            self.assertEqual(layout["layout"]["preset"], name)
            self.assertEqual(
                [r["renderer"] for r in layout["layout"]["regions"]],
                [views[s] for s, _g in presets.PRESETS[name]["regions"]])

    def test_bands_render_and_the_panel_is_not_black(self):
        code, out = self.call("POST", "/layout", {
            "preset": "15-70-15",
            "views": {"left": "picker", "center": "clock",
                      "right": "options"}})
        self.assertEqual(code, 200, out)
        frame = self._wait_frame()
        self.assertEqual(frame.size, (W, H))
        left = frame.crop((0, 0, 288, H))
        centre = frame.crop((288, 0, 1632, H))
        right = frame.crop((1632, 0, W, H))
        for name, band in (("left", left), ("centre", centre),
                           ("right", right)):
            self.assertGreater(len(set(band.getdata())), 4,
                               "%s band is blank" % name)
        # The bands and the centre are different surfaces, not one view
        # stretched across the panel.
        self.assertNotEqual(left.getpixel((40, 200)),
                            centre.getpixel((40, 200)))

    def test_presets_are_published(self):
        code, out = self.call("GET", "/layout/presets")
        self.assertEqual(code, 200)
        self.assertEqual([p["name"] for p in out["presets"]],
                         list(presets.PRESET_NAMES))
        bands = [p for p in out["presets"] if p["name"] == "15-70-15"][0]
        self.assertEqual(bands["primary"], "center")
        for slot in bands["slots"]:
            self.assertTrue(slot["views"])
            self.assertNotIn("beads", slot["views"])

    def test_renderers_publish_the_declared_capability(self):
        code, out = self.call("GET", "/renderers")
        self.assertEqual(code, 200)
        by_name = {r["name"]: r for r in out["renderers"]}
        self.assertEqual(by_name["picker"]["capability"], caps.PARTIAL)
        self.assertEqual(by_name["row"]["capability"], caps.PRIMARY)
        self.assertEqual(by_name["beads"]["capability"], caps.FULL)

    def test_a_preset_slot_refuses_a_full_panel_only_view(self):
        code, out = self.call("POST", "/layout", {
            "preset": "split-50-50",
            "views": {"top": "beads", "bottom": "clock"}})
        self.assertEqual(code, 400, out)
        self.assertIn("beads", json.dumps(out))
        code, state = self.call("GET", "/state")
        self.assertIsNone(state["layout"])

    def test_unknown_preset_is_a_400(self):
        code, out = self.call("POST", "/layout", {"preset": "nope"})
        self.assertEqual(code, 400, out)


if __name__ == "__main__":
    unittest.main()
