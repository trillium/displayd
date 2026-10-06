"""The shell, stat and text components -- and the two views they migrated.

The band a full-panel view wears and the label/value rows under it were
hand-drawn twice (``resources_draw``, ``services_draw``, plus a third copy
in ``row_draw``), with sixteen colour literals and four copies of the font,
truncation and age helpers between them. This pins the one definition:

- the band's title, health dot, rule and footer come from the palette, and
  the rule is the palette's ``rule`` role -- the old per-view
  ``C_LINE = (60, 60, 70)`` is exactly what this test fails on;
- the stat row and its meter come from the palette too, and a meter is
  clamped so a source reporting 300% cannot paint outside its own rect;
- the two migrated views hold no colour, no font loader, no truncation
  rule and no drawing primitive of their own.

Run from the repo root:  python3 -m unittest tests.test_shell -v
"""

import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

from PIL import Image

import theme
from ui import shell, stat
from ui import text as ui_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
MIGRATED = ("renderers/resources_draw.py", "renderers/services_draw.py",
            "renderers/feed_health.py", "renderers/activity.py")


class FakeScreen:
    """A screen with no fonts at all, like a host without a font package:
    every component has to survive that."""

    def __init__(self, W=1920, H=1080):
        self.W, self.H = W, H
        self.frames = []

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    @staticmethod
    def font_path(family="DejaVuSans-Bold"):
        return None


def page(screen=None):
    return (screen or FakeScreen()).new_image(theme.rgb("page"))


def count(img, colour):
    return sum(n for n, c in img.getcolors(maxcolors=1 << 24) if c == colour)


class TextTest(unittest.TestCase):
    """One face, one measurement, one fitting rule."""

    def setUp(self):
        self.screen = FakeScreen()

    def test_face_is_never_none_even_without_fonts(self):
        self.assertIsNotNone(ui_text.face(self.screen, 72))
        self.assertIsNotNone(ui_text.face(self.screen, 44, bold=True))

    def test_width_grows_with_the_size_and_is_zero_for_nothing(self):
        self.assertEqual(ui_text.width(self.screen, "", 40), 0)
        self.assertGreater(ui_text.width(self.screen, "MEM", 40), 0)

    def test_fit_leaves_a_fitting_string_alone(self):
        self.assertEqual(ui_text.fit(self.screen, "MEM", 40, room=1000), "MEM")

    def test_fit_trims_a_long_string_and_honours_zero_room(self):
        long = "x" * 400
        self.assertLess(len(ui_text.fit(self.screen, long, 40, room=200)),
                        len(long))
        self.assertEqual(ui_text.fit(self.screen, long, 40, room=0), long)

    def test_write_paints_and_never_raises(self):
        img = page()
        out = ui_text.write(img, self.screen, (10, 10), "hello", (1, 2, 3), 40)
        self.assertIs(out, img)
        self.assertGreater(count(img, (1, 2, 3)), 0)
        # A frame that cannot be drawn on is returned unchanged, not an error.
        self.assertIs(ui_text.write(None, self.screen, (0, 0), "x", (0, 0, 0), 9),
                      None)

    def test_wrap_breaks_a_paragraph_to_the_room_it_is_given(self):
        words = " ".join(["word"] * 60)
        wide = ui_text.wrap(self.screen, words, 34, 900, rows=None)
        narrow = ui_text.wrap(self.screen, words, 34, 300, rows=None)
        self.assertTrue(all(ui_text.width(self.screen, line, 34) <= 900
                            for line in wide))
        self.assertGreater(len(narrow), len(wide))
        self.assertTrue(all(ui_text.width(self.screen, line, 34) <= 300
                            for line in narrow))

    def test_wrap_is_total_and_never_empty(self):
        for value in (None, "", "junk", 7, [1, 2]):
            with self.subTest(value=value):
                self.assertEqual(len(ui_text.wrap(self.screen, value, 34, 0)), 1)
        self.assertEqual(ui_text.wrap(self.screen, "a b c d", 34, 0, rows=1),
                         ["a b c d"])
        self.assertEqual(len(ui_text.wrap(self.screen, "a " * 90, 34, 300,
                                          rows=3)), 3)
        self.assertEqual(ui_text.wrap(self.screen, "a\nb", 34, 0),
                         ["a", "b"])


class ShellTest(unittest.TestCase):
    """The band: title, health status, dot, rule, footer."""

    def setUp(self):
        self.screen = FakeScreen()

    def test_health_ink_is_the_palette_role_for_each_health_word(self):
        self.assertEqual(shell.health_ink("cold"), theme.rgb("muted"))
        self.assertEqual(shell.health_ink("warm"), theme.rgb("ok"))
        self.assertEqual(shell.health_ink("stale"), theme.rgb("attention"))
        self.assertEqual(shell.health_ink("error"), theme.rgb("alert"))

    def test_health_ink_is_total_on_a_word_it_has_never_seen(self):
        for word in (None, "", "melted", 7, object()):
            with self.subTest(word=word):
                self.assertEqual(shell.health_ink(word), theme.rgb("muted"))

    def test_every_health_role_is_a_real_token(self):
        # A typo in the map must fail here, not hand a view a KeyError.
        for word, role in shell.HEALTH_ROLE.items():
            with self.subTest(word=word):
                self.assertEqual(shell.health_ink(word), theme.rgb(role))

    def test_age_is_honest_and_total(self):
        now = time.time()
        self.assertEqual(shell.age(None), "no data yet")
        self.assertEqual(shell.age(0), "no data yet")
        self.assertEqual(shell.age("junk"), "no data yet")
        self.assertEqual(shell.age(now - 30), "updated 30s ago")
        self.assertEqual(shell.age(now - 90), "updated 1m ago")
        self.assertEqual(shell.age(now - 7200), "updated 2h ago")

    def test_short_age_owns_the_buckets(self):
        # Five copies of this rule used to exist (this module's ``age`` and
        # each polled dashboard's own); the band's line is built on it.
        self.assertEqual(shell.short_age(None), "never")
        self.assertEqual(shell.short_age("junk"), "never")
        self.assertEqual(shell.short_age(-5), "0s")
        self.assertEqual(shell.short_age(59.9), "59s")
        self.assertEqual(shell.short_age(60), "1m")
        self.assertEqual(shell.short_age(3599), "59m")
        self.assertEqual(shell.short_age(3600), "1h")
        self.assertEqual(shell.short_age(86400 * 3), "3d")
        self.assertEqual(shell.age(time.time() - 86400 * 3),
                         "updated 3d ago")

    def test_head_colours_its_status_line_on_request(self):
        img = page(self.screen)
        shell.head(img, self.screen, "FEED HEALTH", status="ALL HEALTHY",
                   status_ink=theme.rgb("ok"))
        self.assertGreater(count(img, theme.rgb("ok")), 50)
        plain = page(self.screen)
        shell.head(plain, self.screen, "FEED HEALTH", status="ALL HEALTHY")
        self.assertGreater(count(plain, theme.rgb("muted")), 50)

    def test_status_line_names_the_health_and_the_age(self):
        self.assertEqual(shell.status_line("warm", 0), "warm \u00b7 no data yet")
        self.assertEqual(shell.status_line("warm", time.time() - 5),
                         "warm \u00b7 updated 5s ago")
        self.assertEqual(shell.status_line(None), "")

    def test_head_paints_the_title_from_the_palette(self):
        img = page(self.screen)
        shell.head(img, self.screen, "RESOURCES", detail="lnx-server",
                   health="warm", updated=time.time())
        self.assertGreater(count(img, theme.rgb("ink")), 50)

    def test_head_rule_is_the_palette_rule_role(self):
        # The pre-change tree drew this line in each view's own
        # C_LINE = (60, 60, 70); the band's rule is the same job as the
        # template chrome's .rule, so it is the same token.
        img = page(self.screen)
        shell.head(img, self.screen, "RESOURCES")
        rule = theme.rgb("rule")
        self.assertIn(rule, [img.getpixel((960, shell.RULE_Y)),
                             img.getpixel((960, shell.RULE_Y + 1))])
        self.assertGreater(count(img, rule), 1000)
        self.assertNotIn((60, 60, 70), [c for _n, c in img.getcolors(1 << 24)])

    def test_head_dot_wears_the_health_role(self):
        for word, role in (("warm", "ok"), ("error", "alert"),
                           ("melted", "muted")):
            img = page(self.screen)
            shell.head(img, self.screen, "T", health=word, updated=time.time())
            cx = self.screen.W - shell.PAD - shell.DOT_X + 10
            with self.subTest(health=word):
                self.assertEqual(img.getpixel((cx, shell.DOT_Y + 10)),
                                 theme.rgb(role))

    def test_head_without_health_draws_no_dot(self):
        img = page(self.screen)
        shell.head(img, self.screen, "TITLE")
        cx = self.screen.W - shell.PAD - shell.DOT_X + 10
        self.assertEqual(img.getpixel((cx, shell.DOT_Y + 10)),
                         theme.rgb("page"))

    def test_head_fits_a_long_title_before_the_status_line(self):
        img = page(self.screen)
        shell.head(img, self.screen, "T" * 200, detail="D" * 200,
                   health="warm", updated=time.time())
        # The status line's own pixels survive: a fitted title never runs
        # under it, and the band never drops the status to make room.
        self.assertGreater(count(img, theme.rgb("muted")), 10)

    def test_foot_and_rule_never_raise_on_a_bare_screen(self):
        img = page(self.screen)
        self.assertIs(shell.foot(img, self.screen, "up 2d 3h"), img)
        self.assertIs(shell.rule(img, self.screen, 500), img)
        self.assertGreater(count(img, theme.rgb("muted")), 0)
        self.assertGreater(count(img, theme.rgb("rule")), 0)


class StatTest(unittest.TestCase):
    """A label plus a value line, and a meter for a fraction."""

    def setUp(self):
        self.screen = FakeScreen()

    def test_row_paints_its_label_and_its_value(self):
        img = page(self.screen)
        stat.row(img, self.screen, (60, 170), "CPU", "12%",
                 value_ink=(1, 2, 3))
        self.assertGreater(count(img, theme.rgb("muted")), 0)
        self.assertGreater(count(img, (1, 2, 3)), 0)

    def test_body_defaults_to_muted_and_accepts_an_ink(self):
        img = page(self.screen)
        stat.body(img, self.screen, (60, 300), "load 0.5", ink=(9, 8, 7))
        self.assertGreater(count(img, (9, 8, 7)), 0)

    def test_meter_outline_and_fill_come_from_the_palette(self):
        img = page(self.screen)
        stat.meter(img, self.screen, (100, 100, 200, 40), 0.5,
                   ink=theme.rgb("ok"))
        self.assertEqual(img.getpixel((100, 100)), theme.rgb("edge"))
        self.assertEqual(img.getpixel((150, 120)), theme.rgb("ok"))

    def test_meter_is_clamped_and_empty_when_the_fraction_is_unknown(self):
        for frac, filled in ((0, False), (0.0, False), (None, False),
                             ("junk", False), (5.0, True), (1.0, True)):
            img = page(self.screen)
            stat.meter(img, self.screen, (100, 100, 200, 40), frac,
                       ink=(4, 5, 6))
            with self.subTest(frac=frac):
                self.assertEqual(count(img, (4, 5, 6)) > 0, filled)
                # Never outside its own rect, whatever the source claims.
                self.assertNotEqual(img.getpixel((305, 120)), (4, 5, 6))

    def test_meter_is_total_on_garbage(self):
        img = page(self.screen)
        for rect in (None, "x", (1, 2), (1, 2, 3, 4, 5)):
            with self.subTest(rect=rect):
                self.assertIs(stat.meter(img, self.screen, rect, 0.5), img)

    def test_width_measures_without_a_draw_handle_of_the_caller_s_own(self):
        self.assertEqual(stat.width(self.screen, "", stat.ROW_SIZE), 0)
        self.assertGreater(stat.width(self.screen, "PORTS", stat.BODY_SIZE), 0)

    def test_list_row_paints_a_dot_a_name_and_a_right_aligned_value(self):
        img = page(self.screen)
        stat.list_row(img, self.screen, (60, 200), "chat.message",
                      "WARM  5s ago  x2", ink=theme.rgb("ink"),
                      meta_ink=theme.rgb("ok"), dot_ink=theme.rgb("ok"))
        self.assertGreater(count(img, theme.rgb("ok")), 100)
        self.assertGreater(count(img, theme.rgb("ink")), 20)
        # The value is flush right to the same pad the band uses.
        right = self.screen.W - 60
        self.assertGreater(count(img.crop((right - 400, 200, right, 240)),
                                 theme.rgb("ok")), 0)

    def test_list_row_defaults_to_the_palette_and_never_raises(self):
        img = page(self.screen)
        stat.list_row(img, self.screen, (60, 200), "name", "value")
        self.assertGreater(count(img, theme.rgb("muted")), 20)
        for xy in (None, "x", (1,), ("a", "b")):
            with self.subTest(xy=xy):
                self.assertIs(stat.list_row(img, self.screen, xy, "n"), img)

    def test_list_row_fits_a_long_name_clear_of_its_value(self):
        img = page(self.screen)
        stat.list_row(img, self.screen, (60, 200), "n" * 300, "VALUE",
                      ink=theme.rgb("ink"), meta_ink=theme.rgb("attention"))
        # The value keeps its own pixels: a name never runs under it.
        self.assertGreater(count(img, theme.rgb("attention")), 20)


class OneBandTwoPathsTest(unittest.TestCase):
    """The band is drawn twice -- a template's CSS and a Pillow view's
    pixels -- but the rule under it is ONE token, so the two paths cannot
    drift on the one thing both actually draw."""

    def setUp(self):
        self.screen = FakeScreen()

    def test_the_band_rule_is_the_rule_token_on_both_paths(self):
        with open(os.path.join(ROOT, "html-templates", "_chrome.html"),
                  encoding="utf-8") as handle:
            chrome = handle.read()
        # The template half authors the rule as the palette's --rule ...
        self.assertIn("background: var(--rule)", chrome)
        # ... and the Pillow half draws the same token, not a literal.
        img = page(self.screen)
        shell.rule(img, self.screen, 300)
        self.assertGreater(count(img, theme.rgb("rule")), 1000)
        self.assertEqual(theme.rgb("rule"), theme._rgb(theme.RULE))


class MigratedViewsTest(unittest.TestCase):
    """The two views the components pulled: they draw through the layer."""

    def source(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def test_neither_view_draws_or_owns_a_palette_of_its_own(self):
        # This is the anti-drift rule the gate cannot express: the gate
        # fails on ImageDraw, this fails on a view re-authoring a colour,
        # a font loader, an age line or a truncation rule.
        for rel in MIGRATED:
            src = self.source(rel)
            with self.subTest(module=rel):
                self.assertNotIn("ImageDraw", src)
                self.assertNotIn("from PIL", src)
                self.assertEqual(HEX_RE.findall(src), [])
                self.assertNotIn("def _font", src)
                self.assertNotIn("def _fit", src)
                self.assertNotIn("def _age", src)
                self.assertNotIn("def _bar", src)
                self.assertIn("from ui import", src)

    def test_feed_health_paints_the_component_s_roles(self):
        import feed_health

        rows = feed_health.collect_rows({
            "chat": {"message": {"count": 2, "updated_at": 1,
                                 "age_seconds": 900, "health": "stale"}}})
        summary, summary_ink = feed_health.summarize(rows)
        screen = FakeScreen()
        frame = feed_health._draw(screen, "FEED HEALTH", rows, summary,
                                  summary_ink, theme.rgb("page"))
        self.assertGreater(count(frame, theme.rgb("attention")), 200)
        # The band's rule is the shared token, and the old per-view yellow
        # (240, 200, 60) and grey (128, 128, 128) are gone from the panel.
        self.assertIn(theme.rgb("rule"),
                      [frame.getpixel((960, shell.RULE_Y)),
                       frame.getpixel((960, shell.RULE_Y + 1))])
        colours = [c for _n, c in frame.getcolors(1 << 24)]
        self.assertNotIn((240, 200, 60), colours)
        self.assertNotIn((128, 128, 128), colours)
        self.assertNotIn((60, 60, 70), colours)

    def test_activity_paints_the_component_s_roles(self):
        import activity

        events = [{"seq": 1, "tool": "beads_show", "outcome": "ok",
                   "caller": "firstmate", "durationMs": 12,
                   "summary": "summary " * 60},
                  {"seq": 2, "tool": "beads_close", "outcome": "error",
                   "caller": "ship"}]
        screen = FakeScreen()
        frame = activity._draw(screen, "ACTIVITY", events, 8,
                               theme.rgb("page"))
        self.assertGreater(count(frame, theme.rgb("ok")), 50)
        self.assertGreater(count(frame, theme.rgb("alert")), 50)
        self.assertIn(theme.rgb("rule"),
                      [frame.getpixel((960, shell.RULE_Y)),
                       frame.getpixel((960, shell.RULE_Y + 1))])
        # The old per-view okay-red (255, 110, 100) and green
        # (110, 220, 130) are gone; every line is the band's type.
        colours = [c for _n, c in frame.getcolors(1 << 24)]
        self.assertNotIn((110, 220, 130), colours)
        self.assertNotIn((255, 110, 100), colours)

    def test_activity_never_draws_an_empty_panel(self):
        import activity

        screen = FakeScreen()
        frame = activity._draw(screen, "ACTIVITY", [], 8, theme.rgb("page"))
        self.assertGreater(count(frame, theme.rgb("muted")), 50)

    def test_resources_view_paints_the_palette_band_and_rows(self):
        import resources

        snap = {
            "cpu_pct": 12.5, "load": (0.5, 0.4, 0.3), "procs": "2/446",
            "mem_used": 2 * 1024 ** 3, "mem_total": 16 * 1024 ** 3,
            "swap_used": 0, "swap_total": 4 * 1024 ** 3,
            "disks": [{"mount": "/", "used": 24 * 1024 ** 3,
                       "total": 98 * 1024 ** 3, "pct": 24.5}],
            "uptime_s": 61200, "hostname": "lnx-server", "ncpu": 4,
        }
        with resources._POLL["lock"]:
            resources._POLL.update(snapshot=snap, health="warm",
                                   error=None, updated=time.time())
        screen = FakeScreen()
        resources._draw(screen, "RESOURCES", theme.rgb("page"))
        frame = screen.frames[-1]
        self.assertGreater(count(frame, theme.rgb("rule")), 1000)
        self.assertGreater(count(frame, theme.rgb("edge")), 1000)
        # The band is on it, and the component drew the header rule at the
        # band's own y -- not wherever the view used to put its line.
        self.assertIn(theme.rgb("rule"),
                      [frame.getpixel((960, shell.RULE_Y)),
                       frame.getpixel((960, shell.RULE_Y + 1))])

    def test_services_view_paints_the_palette_band_and_rows(self):
        import services

        snap = services._build_snapshot({
            "hostname": "lnx-server", "host_uptime": "up 17 hours",
            "services": [
                {"name": "displayd.service", "state": "up", "detail": "a",
                 "uptime": "today", "restarts": "0"},
                {"name": "docker.service", "state": "failed", "detail": "b",
                 "uptime": "", "restarts": "1"}],
            "containers": [{"name": "coder", "state": "running",
                            "status": "Up"}],
            "listeners": [{"address": "0.0.0.0", "port": 22,
                           "process": "sshd"}],
        }, "displayd.service")
        with services._POLL["lock"]:
            services._POLL.update(snapshot=snap, health="warm",
                                  error=None, updated=time.time())
        screen = FakeScreen()
        services._draw(screen, "SERVICES", theme.rgb("page"), "http://x")
        frame = screen.frames[-1]
        self.assertGreater(count(frame, theme.rgb("rule")), 1000)
        self.assertGreater(count(frame, theme.rgb("edge")), 1000)
        self.assertGreater(count(frame, theme.rgb("alert")), 0)


def _render(view, screen):
    """The last frame a view presented -- the frame the panel would show."""
    return screen.frames[-1]


if __name__ == "__main__":
    unittest.main()
