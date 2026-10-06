"""Tests for the unified home screen (picker tiles + live apps dock).

The dock is a litehtml document (html-templates/dock.html), so these
tests also pin the two things that migration can silently break: the
strip must be drawn *inside* the rect the apps-dock tap region targets,
and a dock that gains, loses or breaks its feed must not move a tile.

Run from the repo root:  python3 -m unittest tests.test_unified -v
"""

import os
import re
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

from PIL import Image, ImageChops

import _html_native
import touch_audit
from renderers.ui import system_buttons as buttons
from renderers import unified as un
from renderers import unified_dock as dock

native_built = unittest.skipUnless(
    any(os.path.exists(os.path.join(_html_native.NATIVE_DIR, name))
        for name in _html_native.LIB_NAMES),
    "native library not built (tools/build_litehtml.sh)")

W, H = 1920, 1080
VIEWS_18 = ["activity", "beads", "beads-detail", "chat", "clock",
            "feed_health", "life", "macbook", "options", "picker",
            "resources", "retro_grid", "row", "services", "sleep",
            "solid", "stream", "touch_confidence"]
LEFT_11 = ["Calendar", "Find My", "Mail", "Maps", "Music", "Notes",
           "Photos", "Safari", "Slack", "Talon", "Terminal"]
RIGHT_9 = ["Activity Monitor", "Code", "Discord", "Finder", "Firebot v5",
           "Google Chrome", "Google Chrome Beta", "interceptor-bridge",
           "LuLu"]
DISPLAYS = [{"bounds": {"x": 0, "y": 0}, "main": True},
            {"bounds": {"x": 1920, "y": 0}}]


def live_state(ts=None):
    windows = {a: {"d": 0} for a in LEFT_11}
    windows.update({a: {"d": 1} for a in RIGHT_9})
    return {"ts": time.time() if ts is None else ts,
            "apps": LEFT_11 + RIGHT_9, "focused": "Google Chrome",
            "windows": windows, "displays": DISPLAYS}


class FakeScreen:
    W, H = W, H

    def __init__(self, states=()):
        self.frames = []
        self._states = list(states)

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        return default

    def font_path(self, family="DejaVuSans-Bold"):
        return None

    def get_input(self, renderer, input_name):
        if renderer == "talon_apps" and input_name == "state":
            return list(self._states)
        return []


def fake_renderers():
    table = {}
    for name in VIEWS_18:
        table[name] = {"module": object(), "params": {}}
    table["unified"] = {"module": object(), "params": {}}
    table["image"] = {"module": object(),
                      "params": {"path": {"type": "string",
                                          "required": True}}}
    return table


class DefaultsTest(unittest.TestCase):
    def test_grid_and_dock_defaults_at_panel_size(self):
        self.assertEqual(un.default_grid(W, H), [160, 40, 1600, 700])
        self.assertEqual(un.coerce_grid({}, W, H), [160, 40, 1600, 700])
        self.assertEqual(un.default_dock(W, H), [80, 770, 1760, 230])
        self.assertEqual(un.coerce_dock({}, W, H), [80, 770, 1760, 230])

    def test_garbage_rects_fall_back(self):
        self.assertEqual(un.coerce_grid({"rect": "nope"}, W, H),
                         [160, 40, 1600, 700])
        self.assertEqual(un.coerce_dock({"dock": [0, 0, -5, 10]}, W, H),
                         [80, 770, 1760, 230])

    def test_tile_views_explicit_wins(self):
        self.assertEqual(un.tile_views({"views": ["clock"]}), ["clock"])

    def test_tile_views_live_minus_self(self):
        self.assertEqual(un.live_tile_views(fake_renderers()), VIEWS_18)
        self.assertEqual(un.tile_views({}, fake_renderers()), VIEWS_18)

    def test_tile_views_fallback_without_table(self):
        from renderers import picker as pk
        self.assertEqual(un.tile_views({}), list(pk.DEFAULT_VIEWS))


class RegionsTest(unittest.TestCase):
    def test_sample_map_shape(self):
        regs = un.unified_regions(W, H, list(VIEWS_18))
        self.assertEqual(len(regs), 20)
        self.assertEqual(regs[0]["id"], "screen-off")
        self.assertEqual(regs[0]["rect"], [1760, 0, 160, 160])
        self.assertEqual(regs[0]["action"], {"name": "screen_off"})
        tiles = regs[1:-1]
        self.assertEqual(len(tiles), 18)
        for entry, view in zip(tiles, VIEWS_18):
            self.assertEqual(entry["id"], "uview-%s" % view)
            self.assertEqual(entry["action"],
                             {"name": "select_view", "view": view})
        self.assertEqual(regs[-1]["id"], "apps-dock")
        self.assertEqual(regs[-1]["rect"], [80, 770, 1760, 230])
        self.assertEqual(regs[-1]["action"],
                         {"name": "select_view", "view": "macbook"})
        self.assertFalse([r for r in regs if r["id"] == "home"])

    def test_tile_rects_match_picker_geometry(self):
        from renderers import picker as pk
        regs = un.unified_regions(W, H, list(VIEWS_18))
        expect = pk.grid_geometry([160, 40, 1600, 700], 18)
        for entry, rect in zip(regs[1:-1], expect):
            self.assertEqual(entry["rect"], list(rect))


class DockSummaryTest(unittest.TestCase):
    def test_real_content_summary(self):
        summ = dock.dock_summary(live_state())
        self.assertEqual(summ["mode"], "left=D1 right=other")
        self.assertEqual(summ["count"], 20)
        self.assertEqual(summ["focused"], "Google Chrome")
        self.assertEqual(summ["overflow"], 2)
        self.assertEqual((summ["left"], summ["right"]), (11, 9))

    def test_empty_and_garbage_never_raise(self):
        self.assertEqual(dock.dock_summary(None)["count"], 0)
        self.assertEqual(dock.dock_summary("nope")["mode"],
                         "one screen: split")


def grid_frame():
    """The tile grid on its own, i.e. the frame with no dock composited."""
    from renderers import picker as pk
    screen = FakeScreen()
    grid = un.coerce_grid({}, W, H)
    views = un.tile_views({"views": list(VIEWS_18)})
    return pk.draw(screen, views, pk.grid_geometry(grid, len(views)),
                   grid, pk.PALETTE, (8, 10, 16), (255, 255, 255),
                   (140, 160, 190))


def frame_with_dock(state, stale, dock_rect=None):
    """The whole merged home frame: tiles plus the live apps dock."""
    from renderers import picker as pk
    screen = FakeScreen()
    grid = un.coerce_grid({}, W, H)
    views = un.tile_views({"views": list(VIEWS_18)})
    return un.draw(screen, views, pk.grid_geometry(grid, len(views)),
                   grid, un.coerce_dock({}, W, H) if dock_rect is None
                   else dock_rect, state, stale, (8, 10, 16), (255, 255, 255))


class FramesTest(unittest.TestCase):
    def _frame(self, state, stale):
        return frame_with_dock(state, stale)

    def test_all_states_render_at_panel_size(self):
        for state, stale in ((live_state(), False), (None, False),
                             (live_state(ts=time.time() - 99), True)):
            frame = self._frame(state, stale)
            self.assertEqual(frame.size, (W, H))

    def test_tiles_untouched_across_dock_states(self):
        healthy = self._frame(live_state(), False)
        empty = self._frame(None, False)
        stale = self._frame(live_state(ts=time.time() - 99), True)
        grid_zone = (0, 0, W, 740)  # grid bottom; dock starts at y=770
        for other in (empty, stale):
            diff = ImageChops.difference(
                healthy.crop(grid_zone), other.crop(grid_zone))
            self.assertIsNone(diff.getbbox())

    # The dock is a litehtml document now, so telling an empty feed from
    # a stale one is a pixel claim about a rendered strip: without the
    # engine both states fall back to the same error card and there is
    # nothing to compare. Same gate, same reason, as DockPixelsTest --
    # the intent is unchanged, it just needs a dock that can draw.
    @native_built
    def test_empty_and_stale_differ_in_dock_only(self):
        empty = self._frame(None, False)
        stale = self._frame(live_state(ts=time.time() - 99), True)
        dock_zone = (80, 770, 1840, 1000)
        diff = ImageChops.difference(empty.crop(dock_zone),
                                     stale.crop(dock_zone))
        self.assertIsNotNone(diff.getbbox())


class DockTemplateTest(unittest.TestCase):
    """The migration, stated as an invariant on the shipped source.

    Pure, no engine: a drifted template or a re-grown Pillow path must
    fail on any checkout, built engine or not.
    """

    def source(self):
        with open(os.path.join(os.path.dirname(__file__), os.pardir,
                               "renderers", "unified_dock.py"),
                  encoding="utf-8") as handle:
            return handle.read()

    def test_no_pillow_drawing_left_in_the_dock(self):
        # The oracle for this increment. A stray ImageDraw here means the
        # Pillow path is still live and the template path is decoration.
        self.assertNotIn("ImageDraw", self.source())
        self.assertNotIn("ImageFont", self.source())

    def test_the_dock_renders_through_the_template(self):
        self.assertIn(dock.TEMPLATE, self.source())
        self.assertTrue(os.path.isfile(os.path.join(
            os.path.dirname(__file__), os.pardir, "html-templates",
            "dock.html")), "html-templates/dock.html is the shipped strip")

    def test_unified_composes_without_pillow(self):
        # The caller hands over an image now, not a draw context.
        with open(un.__file__, encoding="utf-8") as handle:
            self.assertNotIn("ImageDraw", handle.read())
        self.assertNotIn("ImageDraw.Draw", self.source())

    def declared(self):
        with open(os.path.join(os.path.dirname(__file__), os.pardir,
                               "html-templates", "dock.html"),
                  encoding="utf-8") as handle:
            text = handle.read()
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        return set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)", text)), text

    def test_the_shipped_template_matches_the_rendered_variables(self):
        # Every placeholder the file declares is a key dock_variables()
        # fills, and vice versa: a half-filled strip is a red card on the
        # panel, which is exactly what a drift here would cost.
        declared, text = self.declared()
        for state, stale in ((live_state(), False), (None, False),
                             (live_state(ts=time.time() - 99), True)):
            with self.subTest(state=state is None, stale=stale):
                supplied = set(dock.dock_variables(state, stale))
                self.assertEqual(declared, supplied)
        self.assertNotIn("|raw", text, "the dock strip takes no markup")

    def test_no_javascript_and_no_remote_resources(self):
        _declared, text = self.declared()
        lowered = text.lower()
        for banned in ("<script", "javascript:", "@import", "http://",
                       "https://"):
            self.assertNotIn(banned, lowered)

    def test_the_three_states_name_themselves(self):
        live = dock.dock_variables(live_state(), False)
        self.assertIn("apps (20)", live["title"])
        self.assertIn("Google Chrome", live["title"])
        self.assertIn("11 left / 9 right", live["body"])
        self.assertEqual(live["status"], "left=D1 right=other")
        self.assertEqual(live["marker"], "", "a live feed shows no marker")
        empty = dock.dock_variables(None, False)
        self.assertIn("waiting for talon feed", empty["title"])
        self.assertNotIn("apps (", empty["title"])
        stale = dock.dock_variables(live_state(ts=time.time() - 99), True)
        self.assertTrue(stale["marker"].startswith("STALE"))
        self.assertIn("apps (20)", stale["title"], "stale keeps last known")
        self.assertEqual(stale["alert"], "#ffb450")

    def test_garbage_never_raises(self):
        for bad in (None, "nope", 42, {"apps": "no"}, {"apps": [None, 3]}):
            with self.subTest(bad=type(bad).__name__):
                self.assertTrue(dock.dock_variables(bad, False)["title"])


@native_built
class DockPixelsTest(unittest.TestCase):
    """What the panel actually shows, in the rect the tap targets."""

    DOCK = (80, 770, 1760, 230)

    def count(self, frame, colour, box=None):
        crop = frame.crop(box) if box else frame
        return sum(1 for pixel in crop.convert("RGB").getdata()
                   if pixel == colour)

    def lit(self, frame, box):
        return max(sum(pixel) for pixel in
                   frame.crop(box).convert("RGB").getdata())

    def test_the_dock_only_touches_its_own_rect(self):
        # The dock is a strip pasted into a finished frame: if it ever
        # paints anywhere else, a feed change could move a tile.
        healthy = frame_with_dock(live_state(), False)
        box = ImageChops.difference(healthy, grid_frame()).getbbox()
        self.assertIsNotNone(box)
        x0, y0, x1, y1 = box
        self.assertGreaterEqual(x0, self.DOCK[0])
        self.assertGreaterEqual(y0, self.DOCK[1])
        self.assertLessEqual(x1, self.DOCK[0] + self.DOCK[2])
        self.assertLessEqual(y1, self.DOCK[1] + self.DOCK[3])

    def test_the_strip_draws_its_summary(self):
        frame = frame_with_dock(live_state(), False)
        for label, box in (("head", (100, 780, 700, 830)),
                           ("subtitle", (100, 830, 900, 870)),
                           ("main line", (100, 860, 1200, 940)),
                           ("tail row", (100, 940, 700, 990))):
            with self.subTest(band=label):
                self.assertGreater(self.lit(frame, box), 300)

    def test_the_live_dot_and_tap_hint_are_accent(self):
        frame = frame_with_dock(live_state(), False)
        self.assertGreater(self.count(frame, dock.ACCENT,
                                     (80, 770, 1840, 1000)), 0)

    def test_stale_marks_the_dock_in_amber(self):
        live = frame_with_dock(live_state(), False)
        stale = frame_with_dock(live_state(ts=time.time() - 99), True)
        zone = (80, 770, 1840, 1000)
        self.assertEqual(self.count(live, dock.ALERT, zone), 0)
        self.assertGreater(self.count(stale, dock.ALERT, zone), 0)

    def test_a_custom_dock_rect_gets_its_own_strip(self):
        # The strip is authored once and scaled to the rect it is given,
        # so a custom dock is drawn -- not clipped and not empty.
        rect = [0, 960, 960, 120]
        frame = frame_with_dock(live_state(), False, rect)
        self.assertGreater(self.lit(frame, (10, 975, 940, 1070)), 300)
        box = ImageChops.difference(frame, grid_frame()).getbbox()
        self.assertIsNotNone(box)
        self.assertGreaterEqual(box[1], rect[1])

    def test_a_broken_template_is_a_card_inside_the_strip(self):
        # Never a blank strip and never a full-screen card: the tiles
        # around it have to survive a dock that cannot draw. Patched
        # through unified's own reference, because renderers/unified.py
        # imports the dock as a top-level module -- a different module
        # object than `from renderers import unified_dock`.
        saved = un.dock_mod.TEMPLATE
        un.dock_mod.TEMPLATE = "no_such_template.html"
        try:
            frame = frame_with_dock(live_state(), False)
        finally:
            un.dock_mod.TEMPLATE = saved
        self.assertGreater(self.count(frame, (214, 74, 74),
                                      (80, 770, 1840, 1000)), 0)
        outside = frame.crop((0, 0, W, 740))
        self.assertIsNone(ImageChops.difference(
            outside, grid_frame().crop((0, 0, W, 740))).getbbox())


class RunLoopTest(unittest.TestCase):
    def test_run_presents_live_frame(self):
        screen = FakeScreen([live_state()])
        stop = threading.Event()
        worker = threading.Thread(target=un.run,
                                  args=(screen, {}, stop))
        worker.start()
        deadline = time.time() + 5
        while not screen.frames and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        worker.join(timeout=5)
        self.assertTrue(screen.frames)
        self.assertEqual(screen.frames[-1].size, (W, H))

    def test_run_without_feed_still_presents(self):
        screen = FakeScreen()
        stop = threading.Event()
        worker = threading.Thread(target=un.run,
                                  args=(screen, {}, stop))
        worker.start()
        deadline = time.time() + 5
        while not screen.frames and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        worker.join(timeout=5)
        self.assertTrue(screen.frames)


class HomeWiringTest(unittest.TestCase):
    def test_unified_is_home(self):
        self.assertEqual(buttons.HOME_VIEW, "unified")
        self.assertIn("unified", buttons.HOME_SUPPRESSED)
        self.assertIn("picker", buttons.HOME_SUPPRESSED)
        self.assertNotIn("unified", ("sleep", "reload", "notice"))

    def test_audit_expects_tiles_plus_dock(self):
        expected = touch_audit.expected_for_view(
            "unified", {"views": list(VIEWS_18)}, W, H,
            picker_views=list(VIEWS_18))
        self.assertTrue(expected["checkable"])
        ids = [e["id"] for e in expected["exact"]]
        self.assertEqual(ids[:-1],
                         ["uview-%s" % v for v in VIEWS_18] + ["apps-dock"])
        self.assertEqual(ids[-1], "screen-off")
        dock_entry = [e for e in expected["exact"]
                      if e["id"] == "apps-dock"][0]
        self.assertEqual(dock_entry["action"],
                         {"name": "select_view", "view": "macbook"})
        self.assertFalse([i for i in ids if i == "home"])


if __name__ == "__main__":
    unittest.main()
