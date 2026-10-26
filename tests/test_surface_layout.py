"""A surface's declarative ``layout``: derived regions, re-derivation, wire.

Pure model/registry tests plus one HTTP round trip. The geometry itself is
``layout_geometry`` (also exercised by test_layout.py through POST /layout).

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import surface_model as model
from surface_registry import SurfaceRegistry
from test_surfaces import Clock, ipad, kiosk, phone
from test_surfaces_http import SurfaceHttpTestCase, sidecar

IPAD_INSETS = {"top": 24, "bottom": 20}


def tablet(layout, **over):
    spec = ipad(layout=layout, viewport={"width": 1024, "height": 768,
                                         "insets": IPAD_INSETS})
    del spec["regions"]
    spec.update(over)
    return spec


def rects(rec):
    return {r["name"]: r["rect"] for r in rec["regions"]}


def overlap(a, b):
    return (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
            and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])


class TestDerive(unittest.TestCase):
    def assertTiles(self, rec):
        usable = rec["viewport"]["usable"]
        regions = rec["regions"]
        for r in regions:
            rc = r["rect"]
            self.assertGreaterEqual(rc["x"], usable["x"])
            self.assertGreaterEqual(rc["y"], usable["y"])
            self.assertLessEqual(rc["x"] + rc["w"], usable["x"] + usable["w"])
            self.assertLessEqual(rc["y"] + rc["h"], usable["y"] + usable["h"])
        for i, a in enumerate(regions):
            for b in regions[i + 1:]:
                self.assertFalse(overlap(a["rect"], b["rect"]), (a, b))

    def test_stack_columns_inside_usable_with_insets(self):
        rec = model.normalize(tablet({"regions": [
            {"name": "left", "width": "50%"}, {"name": "right"}]}))
        self.assertEqual(rects(rec), {
            "left": {"x": 0, "y": 24, "w": 512, "h": 724},
            "right": {"x": 512, "y": 24, "w": 512, "h": 724}})
        self.assertTiles(rec)

    def test_stack_rows_offset_by_usable_origin(self):
        rec = model.normalize(tablet({"regions": [
            {"name": "top", "height": 100}, {"name": "rest"}]}, viewport={
                "width": 1000, "height": 700,
                "insets": {"left": 40, "right": 10, "top": 30}}))
        self.assertEqual(rects(rec)["top"], {"x": 40, "y": 30, "w": 950, "h": 100})
        self.assertEqual(rects(rec)["rest"], {"x": 40, "y": 130, "w": 950, "h": 570})
        self.assertTiles(rec)

    def test_grid_with_spans(self):
        rec = model.normalize(tablet({"rows": 2, "cols": 2, "regions": [
            {"name": "wide", "row": 0, "col": 0, "col_span": 2},
            {"name": "a", "row": 1, "col": 0}, {"name": "b", "row": 1, "col": 1}]}))
        self.assertEqual(rects(rec)["wide"], {"x": 0, "y": 24, "w": 1024, "h": 362})
        self.assertEqual(rects(rec)["b"], {"x": 512, "y": 386, "w": 512, "h": 362})
        self.assertTiles(rec)

    def test_explicit_rect_is_relative_to_usable(self):
        rec = model.normalize(tablet({"regions": [
            {"name": "badge", "rect": {"x": 10, "y": 10, "w": 100, "h": 60}},
            {"name": "body", "rect": [0, "20%", "100%", "80%"]}]}))
        self.assertEqual(rects(rec)["badge"], {"x": 10, "y": 34, "w": 100, "h": 60})
        self.assertEqual(rects(rec)["body"]["w"], 1024)

    def test_matches_the_explicit_ipad_regions(self):
        explicit = ipad(viewport={"width": 1024, "height": 768, "insets": IPAD_INSETS},
                        regions=[
            {"name": "left", "rect": {"x": 0, "y": 24, "w": 512, "h": 724},
             "content_types": ["text"], "touch": True},
            {"name": "right", "rect": {"x": 512, "y": 24, "w": 512, "h": 724},
             "content_types": ["text"], "touch": True}])
        derived = tablet({"regions": [
            {"name": "left", "width": "50%", "content_types": ["text"]},
            {"name": "right", "content_types": ["text"], "touch": True}]})
        self.assertEqual(model.normalize(derived)["regions"],
                         model.normalize(explicit)["regions"])

    def test_region_shape_defaults_like_explicit_regions(self):
        rec = model.normalize(tablet({"regions": [{"name": "only"}]}))
        self.assertEqual(rec["regions"], [{
            "name": "only", "rect": {"x": 0, "y": 24, "w": 1024, "h": 724},
            "content_types": ["text", "scene"], "touch": True}])
        self.assertEqual(rec["default_region"], "only")
        self.assertEqual(rec["layout"], {"regions": [{"name": "only"}]})

    def test_without_layout_the_record_is_unchanged(self):
        self.assertNotIn("layout", model.normalize(ipad()))
        self.assertNotIn("layout", model.normalize(kiosk()))

    def test_stored_layout_is_a_copy(self):
        layout = {"regions": [{"name": "only"}]}
        rec = model.normalize(tablet(layout))
        layout["regions"][0]["name"] = "mutated"
        self.assertEqual(rec["layout"]["regions"][0]["name"], "only")

    def test_idempotent_through_a_stored_record_is_refused_as_both(self):
        # restore strips derived regions itself (surface_restore); normalize
        # on a record that still has both is the 400 below.
        rec = model.normalize(tablet({"regions": [{"name": "a"}]}))
        with self.assertRaisesRegex(ValueError, "layout"):
            model.normalize(rec)


class TestRefusals(unittest.TestCase):
    def bad(self, layout, pattern, **over):
        with self.assertRaisesRegex(ValueError, pattern):
            model.normalize(tablet(layout, **over))

    def test_layout_and_regions_together(self):
        self.bad({"regions": [{"name": "a"}]}, "layout: give either",
                 regions=[{"name": "a", "rect": {"x": 0, "y": 24, "w": 5, "h": 5}}])

    def test_too_many_regions(self):
        self.bad({"regions": [{"name": "r%d" % i} for i in range(33)]},
                 r"layout\.regions: at most 32")

    def test_shape_errors_name_the_field(self):
        self.bad("grid", "layout: expected an object")
        self.bad({"regions": []}, r"layout\.regions: expected a non-empty")
        self.bad({"regions": ["x"]}, r"layout\.regions\[0\]: expected an object")
        self.bad({"regions": [{"height": 5}]}, r"layout\.regions\[0\]\.name")
        self.bad({"regions": [{"name": "a", "renderer": "clock"}]},
                 r"layout\.regions\[0\]: a surface region names no renderer")

    def test_duplicate_names_content_types_and_touch(self):
        self.bad({"regions": [{"name": "a"}, {"name": "a"}]},
                 r"layout\.regions\[1\]: duplicate region name")
        self.bad({"regions": [{"name": "a", "content_types": ["video"]}]},
                 r"layout\.regions\[0\]\.content_types: video not accepted")
        with self.assertRaisesRegex(ValueError, r"layout\.regions\[0\]\.touch"):
            model.normalize(dict(kiosk(), layout={"regions": [
                {"name": "a", "touch": True}]}))

    def test_bad_geometry_names_the_field(self):
        self.bad({"regions": [{"name": "a", "height": "abc"}]},
                 r"layout\.regions\[0\]\.height")
        self.bad({"rows": 1, "cols": 1, "regions": [
            {"name": "a", "row": 3, "col": 0}]}, r"layout\.regions\[0\]: row/col")

    def test_overlap_is_refused(self):
        self.bad({"regions": [
            {"name": "a", "rect": {"x": 0, "y": 0, "w": 600, "h": 100}},
            {"name": "b", "rect": {"x": 500, "y": 50, "w": 100, "h": 100}}]},
            r"layout\.regions\[1\]: overlaps layout\.regions\[0\]")


class TestRederive(unittest.TestCase):
    def setUp(self):
        self.reg = SurfaceRegistry(clock=Clock())
        self.layout = {"regions": [{"name": "left", "width": "50%"}, {"name": "right"}]}
        self.reg.register(tablet(self.layout))

    def test_viewport_update_rederives_from_the_stored_layout(self):
        rec = self.reg.update("ipad", {"viewport": {
            "width": 800, "height": 600, "insets": {"top": 0, "bottom": 0}}})
        self.assertEqual(rects(rec), {"left": {"x": 0, "y": 0, "w": 400, "h": 600},
                                      "right": {"x": 400, "y": 0, "w": 400, "h": 600}})
        self.assertEqual(rec["layout"], self.layout)

    def test_new_layout_replaces_regions(self):
        rec = self.reg.update("ipad", {"layout": {"regions": [{"name": "solo"}]}})
        self.assertEqual([r["name"] for r in rec["regions"]], ["solo"])

    def test_explicit_regions_replace_the_layout(self):
        rec = self.reg.update("ipad", {"regions": [
            {"name": "x", "rect": {"x": 0, "y": 24, "w": 10, "h": 10}}]})
        self.assertNotIn("layout", rec)
        rec = self.reg.update("ipad", {"viewport": {"width": 900}})
        self.assertEqual([r["name"] for r in rec["regions"]], ["x"])

    def test_patch_with_both_is_refused_and_nothing_changes(self):
        before = self.reg.get("ipad")
        with self.assertRaisesRegex(ValueError, "layout: give either"):
            self.reg.update("ipad", {"layout": {"regions": [{"name": "z"}]},
                                     "regions": [{"name": "z", "rect": {
                                         "x": 0, "y": 24, "w": 5, "h": 5}}]})
        self.assertEqual(self.reg.get("ipad"), before)

    def test_null_layout_freezes_the_derived_regions(self):
        rec = self.reg.update("ipad", {"layout": None})
        self.assertNotIn("layout", rec)
        self.assertEqual(len(rec["regions"]), 2)
        rec = self.reg.update("ipad", {"viewport": {"width": 1200}})
        self.assertEqual(rects(rec)["right"]["w"], 512)  # no longer re-derived

    def test_bad_layout_update_leaves_the_record(self):
        before = self.reg.get("ipad")
        with self.assertRaises(ValueError):
            self.reg.update("ipad", {"layout": {"regions": [{"name": "A!"}]}})
        self.assertEqual(self.reg.get("ipad"), before)

    def test_explicit_regions_still_default_rederive(self):
        self.reg.register(phone())
        rec = self.reg.update("phone", {"viewport": {"width": 300, "height": 600}})
        self.assertEqual(rects(rec)["full"], rec["viewport"]["usable"])


class TestWire(SurfaceHttpTestCase):
    def test_register_get_and_viewport_update_over_http(self):
        layout = {"regions": [{"name": "a", "height": "30%"}, {"name": "b"}]}
        code, out = self.call("POST", "/surfaces", sidecar(layout=layout))
        self.assertEqual(code, 201, out)
        self.assertEqual(out["layout"], layout)
        code, out = self.call("GET", "/surfaces/phone")
        self.assertEqual([r["name"] for r in out["regions"]], ["a", "b"])
        self.assertEqual(out["layout"], layout)
        code, out = self.call("POST", "/surfaces/phone",
                              {"viewport": {"width": 100, "height": 1000}})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["regions"][0]["rect"], {"x": 0, "y": 0, "w": 100, "h": 300})

    def test_both_is_a_400_naming_the_field(self):
        code, out = self.call("POST", "/surfaces", sidecar(
            layout={"regions": [{"name": "a"}]},
            regions=[{"name": "a", "rect": {"x": 0, "y": 0, "w": 5, "h": 5}}]))
        self.assertEqual(code, 400)
        self.assertIn("layout", json_error(out))
        self.assertEqual(self.ids(), ["panel"])  # nothing registered


def json_error(out):
    return str(out.get("error", out))


if __name__ == "__main__":
    unittest.main()
