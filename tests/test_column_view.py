"""Column views: region geometry in, template variables out (all pure).

``plan(rect)`` decides how many rows and characters a region can show; the
template's own pixel metrics are pinned against it so the two cannot drift.
The rendered check skips when the litehtml library is not built.

Run from the repo root:  python3 -m unittest tests.test_column_view -v
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "renderers"))

import _html_native  # noqa: E402
import _html_templates as templates  # noqa: E402
import surface_column_view as view  # noqa: E402

COL = {"id": "c1", "stream": "agent", "view": "log", "region": "main", "options": {}}
STREAM = {"id": "agent", "name": "Agent", "views": ["log", "calls", "status"]}


def ev(seq, etype="say", data=None, at=1000.0):
    return {"seq": seq, "type": etype, "data": data, "at": at}


def rows_of(out):
    return [out["r%d" % i] for i in range(view.ROW_SLOTS)]


class TestPlan(unittest.TestCase):
    def test_full_panel_and_narrow_columns_differ(self):
        full = view.plan((0, 0, 1920, 1080))
        narrow = view.plan((0, 0, 288, 1080))  # the 15% side of a 15-70-15 layout
        self.assertEqual(full["rows"], narrow["rows"])  # same height, same rows
        self.assertGreater(full["chars"], narrow["chars"] * 5)
        self.assertEqual(narrow["chars"], (288 - 2 * view.PAD_PX) // view.CHAR_PX)

    def test_differing_viewports_get_their_own_row_counts(self):
        hd = view.plan((0, 0, 1920, 1080))
        small = view.plan((0, 0, 1280, 720))
        short = view.plan((0, 0, 640, 360))
        self.assertGreater(hd["rows"], small["rows"])
        self.assertGreater(small["rows"], short["rows"])
        self.assertEqual(short["rows"], (360 - view.HEAD_PX - 2 * view.PAD_PX) // view.ROW_PX)

    def test_rows_are_capped_by_the_template(self):
        self.assertEqual(view.plan((0, 0, 400, 100000))["rows"], view.ROW_SLOTS)

    def test_sub_header_and_extreme_regions_never_claim_a_row(self):
        head = view.HEAD_PX + 2 * view.PAD_PX
        for rect in ((0, 0, 1, 1), (0, 0, 5, 40), (0, 0, 1920, 100),
                     (0, 0, 600, head - 1), (0, 0, 600, head),
                     (0, 0, 600, head + view.ROW_PX - 1),
                     (0, 0, 0, 0)):
            got = view.plan(rect)
            self.assertEqual(got["rows"], 0, rect)
            self.assertGreaterEqual(got["chars"], view.MIN_CHARS, rect)
        # a sliver narrower than the padding keeps its rows and floors chars
        sliver = view.plan((0, 0, 2 * view.PAD_PX - 1, 1080))
        self.assertEqual((sliver["rows"], sliver["chars"]),
                         (31, view.MIN_CHARS))
        # one row appears exactly when a row fits below the header band
        self.assertEqual(view.plan((0, 0, 600, head + view.ROW_PX))["rows"], 1)
        # a very wide, very short region keeps its chars but still gets no row
        wide = view.plan((0, 0, 4000, 50))
        self.assertEqual((wide["rows"], wide["chars"]),
                         (0, (4000 - 2 * view.PAD_PX) // view.CHAR_PX))
        # no rows planned means none drawn, however many events there are
        out = view.build_vars(COL, STREAM, [ev(i, data="x") for i in range(5)],
                              view.plan((0, 0, 600, 50)))
        self.assertEqual(set(rows_of(out)), {""})

    def test_origin_does_not_change_the_plan(self):
        self.assertEqual(view.plan((0, 0, 500, 500)), view.plan((700, 300, 500, 500)))

    def test_rows_always_fit_the_pixel_height(self):
        for h in range(1, 1200, 37):
            rows = view.plan((0, 0, 600, h))["rows"]
            if rows:
                used = view.HEAD_PX + 2 * view.PAD_PX + rows * view.ROW_PX
                self.assertLessEqual(used, h, h)


class TestTemplatePinned(unittest.TestCase):
    """column.html is where the plan's numbers come from."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(templates.default_root(), view.TEMPLATE),
                  encoding="utf-8") as fh:
            cls.text = fh.read()

    def css(self, selector, scale=1.0):
        """The rule for ``selector`` after the template is filled at ``scale``."""
        out = view.build_vars(COL, STREAM, [], view.plan((0, 0, 600, 600), scale))
        document, _root = templates.load(view.TEMPLATE, out)
        return re.search(r"%s\s*\{([^}]*)\}" % re.escape(selector), document).group(1)

    def test_pixel_metrics_match_the_plan_constants(self):
        for scale in (1.0, 0.5, 1.5, 3.0):
            m = view.metrics(scale)
            self.assertIn("height: %dpx" % m["head"], self.css(".head", scale))
            self.assertIn("height: %dpx" % m["row"], self.css(".row", scale))
            self.assertIn("font-size: %dpx" % m["font"], self.css(".row", scale))
            self.assertIn("padding: 0 %dpx" % m["pad"], self.css(".col", scale))
            self.assertEqual(self.css(".rows", scale).count("%dpx" % m["pad"]), 2)

    def test_scale_one_is_the_original_design(self):
        m = view.metrics()
        self.assertEqual((m["head"], m["row"], m["pad"], m["font"]),
                         (view.HEAD_PX, view.ROW_PX, view.PAD_PX, view.FONT_PX))

    def test_a_bigger_scale_means_bigger_type_and_fewer_shorter_rows(self):
        rect = (0, 0, 900, 700)
        small, normal, big = (view.plan(rect, s) for s in (0.5, 1.0, 2.0))
        self.assertGreater(small["rows"], normal["rows"])
        self.assertGreater(normal["rows"], big["rows"])
        self.assertGreater(normal["chars"], big["chars"])
        self.assertGreater(big["px"]["font"], normal["px"]["font"])

    def test_rows_fit_the_pixel_height_at_every_scale(self):
        for scale in (0.5, 1.0, 1.7, 3.0):
            for h in range(1, 1200, 53):
                got = view.plan((0, 0, 600, h), scale)
                if got["rows"] > 1:
                    m = got["px"]
                    self.assertLessEqual(m["head"] + 2 * m["pad"] + got["rows"] * m["row"], h)

    def test_slots_are_exactly_what_build_vars_fills(self):
        slots = set(re.findall(r"\{\{(\w+)\}\}", re.sub(r"<!--.*?-->", "", self.text,
                                                        flags=re.S)))
        out = view.build_vars(COL, STREAM, [], view.plan((0, 0, 600, 600)))
        self.assertEqual(slots, set(out))
        self.assertEqual(len([s for s in slots if re.match(r"r\d+$", s)]),
                         view.ROW_SLOTS)

    def test_the_trust_boundary_loads_it_with_hostile_values(self):
        out = view.build_vars(COL, STREAM, [ev(1, "say", "<b onclick=x>&</b>")],
                              view.plan((0, 0, 900, 600)))
        document, _root = templates.load(view.TEMPLATE, out)
        self.assertNotIn("<b onclick", document)
        self.assertIn("&lt;b", document)

    @unittest.skipUnless(
        any(os.path.exists(os.path.join(_html_native.NATIVE_DIR, n))
            for n in _html_native.LIB_NAMES), "native library not built")
    def test_renders_in_regions_of_different_sizes(self):
        for rect in ((0, 0, 288, 1080), (0, 0, 1920, 360), (0, 0, 640, 360)):
            out = view.build_vars(COL, STREAM, [ev(i, "say", "line %d" % i)
                                                for i in range(60)], view.plan(rect))
            document, root = templates.load(view.TEMPLATE, out)
            image, _ = _html_native.render(document, rect[2], rect[3], root=root)
            self.assertEqual(image.size, (rect[2], rect[3]))


class TestBuildVars(unittest.TestCase):
    def test_log_keeps_the_newest_rows_that_fit(self):
        events = [ev(i, "say", {"text": "line %d" % i}) for i in range(1, 41)]
        geometry = view.plan((0, 0, 900, 400))
        out = view.build_vars(COL, STREAM, events, geometry)
        shown = [r for r in rows_of(out) if r]
        self.assertEqual(len(shown), geometry["rows"])
        self.assertTrue(shown[-1].endswith("say line 40"))  # newest last
        self.assertTrue(shown[0].endswith("line %d" % (41 - geometry["rows"])))
        self.assertIn("40 events", out["sub"])

    def test_every_value_fits_the_regions_character_budget(self):
        events = [ev(1, "say", "x" * 500)]
        for width in (140, 288, 600, 1920):
            geometry = view.plan((0, 0, width, 600))
            out = view.build_vars(COL, STREAM, events, geometry)
            for value in out.values():
                self.assertLessEqual(len(value), geometry["chars"], (width, value))
        self.assertTrue(out["r0"] == "" or out["r0"].endswith(view.ELLIPSIS))

    def test_slots_beyond_the_plan_stay_blank(self):
        out = view.build_vars(COL, STREAM, [ev(1)], view.plan((0, 0, 600, 600)))
        self.assertEqual(sum(1 for r in rows_of(out) if r), 1)
        self.assertEqual(len(rows_of(out)), view.ROW_SLOTS)

    def test_calls_view(self):
        col = dict(COL, view="calls")
        out = view.build_vars(col, STREAM, [
            ev(1, "call", {"name": "Bash", "state": "running"}),
            ev(2, "call", {"name": "Edit", "status": "done"})],
            view.plan((0, 0, 600, 600)))
        self.assertEqual([r for r in rows_of(out) if r], ["Bash running", "Edit done"])

    def test_status_view_shows_only_the_latest_event(self):
        col = dict(COL, view="status")
        out = view.build_vars(col, STREAM, [ev(1, "s", {"a": 1}),
                                            ev(2, "s", {"b": 2, "a": 3})],
                              view.plan((0, 0, 600, 600)))
        self.assertEqual([r for r in rows_of(out) if r], ["a: 3", "b: 2"])

    def test_an_undeclared_stream_is_said_not_hidden(self):
        out = view.build_vars(COL, None, [], view.plan((0, 0, 600, 600)))
        self.assertIn("not declared", out["r0"])
        self.assertIn("WAITING", out["sub"])
        self.assertEqual(out["title"], "agent")

    def test_a_view_the_stream_does_not_offer_is_said(self):
        out = view.build_vars(COL, dict(STREAM, views=["chart"]), [ev(1)],
                              view.plan((0, 0, 900, 600)))
        self.assertIn("offers chart", out["r0"])
        self.assertIn("VIEW", out["sub"])

    def test_a_view_displayd_does_not_draw_is_said(self):
        col = dict(COL, view="chart")
        out = view.build_vars(col, dict(STREAM, views=[]), [ev(1)],
                              view.plan((0, 0, 900, 600)))
        self.assertIn("not drawn by displayd-html", out["r0"])

    def test_non_dict_and_unserializable_data_never_raise(self):
        for data in (None, 5, [1, 2], {"k": object()}, "text"):
            out = view.build_vars(COL, STREAM, [ev(1, "say", data)],
                                  view.plan((0, 0, 600, 600)))
            self.assertTrue(out["r0"])


if __name__ == "__main__":
    unittest.main()
