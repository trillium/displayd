"""Live monitor previews under the macbook map overlays.

GLANCE display boxes become live per-display JPEGs (bridges/mac_preview.py
-> /feed/macbook/preview -> renderers/macbook_preview.py); the
focused-window rectangle and the pointer dot stay exactly where they
were, drawn OVER the photos. Degraded states stay explicit in both
glance and aim: PREVIEW OFF (boxes), PREVIEW STALE (last frames).

Hermetic: synthetic JPEGs generated in-memory, no Quartz, no network
(the live permission + cost numbers were measured on the MacBook
2026-09-29 and are stated in the PR, not asserted here).

Run from the repo root:  python3 -m unittest tests.test_macbook_preview -v
"""

import base64
import io
import json
import math
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "bridges"))

from PIL import Image, ImageDraw

from displayd import validate_value
import macbook as macbook_renderer
import macbook_glance as glance
import macbook_layout as lay
import macbook_map
import macbook_preview as prev
import mac_preview as bridge
import theme

PANEL_W, PANEL_H = 1920, 1080
BG = (10, 10, 14)
DISPLAYS = [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117},
             "main": True},
            {"bounds": {"x": 1728, "y": 0, "w": 1920, "h": 1080},
             "main": False}]


class FakeScreen:
    W, H = PANEL_W, PANEL_H

    def new_image(self, bg):
        return Image.new("RGB", (self.W, self.H), tuple(bg))

    def color(self, value, default):
        return tuple(default)

    def font_path(self, name):
        return None


def solid_frame(color=(200, 30, 30), size=(96, 60)):
    """Synthetic camera frame: solid JPEG -> base64 text."""
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", quality=60)
    return base64.b64encode(out.getvalue()).decode("ascii")


def state_payload(ts=None):
    return {
        "ts": ts if ts is not None else time.time(),
        "accessibility_trusted": True,
        "focus": {"app_name": "WezTerm",
                  "window_title": "macbookpro: fm-",
                  "window_bounds": {"x": 0, "y": 0,
                                    "w": 1728, "h": 1117},
                  "display_index": 0},
        "mouse": {"x": 464, "y": 283, "display_index": 0},
        "displays": [{"bounds": dict(d["bounds"]), "main": d["main"]}
                     for d in DISPLAYS],
        "talon": {"mode": "command", "muted": False},
    }


def preview_payload(ts=None):
    return {
        "ts": ts if ts is not None else time.time(),
        "frames": [
            {"display_index": 0, "w": 96, "h": 60,
             "jpeg": solid_frame((200, 30, 30))},
            {"display_index": 1, "w": 96, "h": 60,
             "jpeg": solid_frame((30, 90, 200))},
        ],
    }


def draw_map(state, preview):
    screen = FakeScreen()
    img = screen.new_image(BG)
    draw = ImageDraw.Draw(img)
    glance.draw_map(img, draw, screen, state, preview, None)
    return img


class BridgeCase(unittest.TestCase):
    def test_frame_jpeg_bounds_and_cap(self):
        shot = Image.new("RGB", (800, 600), (10, 200, 10))
        data, w, h = bridge.frame_jpeg(shot, max_w=480, quality=60)
        self.assertLessEqual(w, 480)
        self.assertLessEqual(len(data), bridge.FRAME_JPEG_CAP)
        self.assertIsNone(bridge.frame_jpeg(shot, cap=10))
        self.assertIsNone(bridge.frame_jpeg(None))

    def test_encode_rejects_over_cap(self):
        self.assertIsNone(bridge.encode(b"x" * (bridge.FRAME_JPEG_CAP + 1)))
        self.assertIsNone(bridge.encode(None))
        self.assertIsNotNone(bridge.encode(b"abc"))

    def test_preview_doc_none_when_empty(self):
        self.assertIsNone(bridge.preview_doc([]))
        self.assertIsNone(bridge.preview_doc(None))
        doc = bridge.preview_doc([{"display_index": 0}], now=123.0)
        self.assertEqual(doc, {"ts": 123.0,
                               "frames": [{"display_index": 0}]})

    def test_preview_doc_validates_against_schema(self):
        frames = [{"display_index": i, "w": 96, "h": 60,
                   "jpeg": solid_frame()} for i in (0, 1)]
        doc = bridge.preview_doc(frames)
        validate_value(doc, macbook_renderer.INPUTS["preview"], "preview")

    def test_capture_set_never_raises_without_quartz(self):
        real = sys.modules.get("Quartz")
        sys.modules["Quartz"] = None
        try:
            self.assertEqual(bridge.capture_set(), [])
        finally:
            if real is not None:
                sys.modules["Quartz"] = real
            else:
                del sys.modules["Quartz"]


class PreviewModuleCase(unittest.TestCase):
    def test_fresh_stale_off(self):
        now = time.time()
        self.assertTrue(prev.fresh(preview_payload(now), now))
        self.assertEqual(prev.mode(preview_payload(now), now), "live")
        old = preview_payload(now - 30)
        self.assertFalse(prev.fresh(old, now))
        self.assertTrue(prev.stale(old, now))
        self.assertEqual(prev.mode(old, now), "stale")
        for bad in (None, {}, {"ts": now, "frames": []},
                    {"ts": now, "frames": [{"nope": 1}]}):
            self.assertFalse(prev.fresh(bad, now))
            self.assertEqual(prev.mode(bad, now), "off")
        self.assertFalse(prev.fresh("garbage", now))

    def test_decode_round_trip_and_garbage(self):
        shot = prev.decode({"jpeg": solid_frame((200, 30, 30))})
        self.assertIsNotNone(shot)
        r, g, b = shot.getpixel((10, 10))
        self.assertGreater(r, 150)
        self.assertIsNone(prev.decode({}))
        self.assertIsNone(prev.decode({"jpeg": "!!!not-base64!!!"}))
        self.assertIsNone(prev.decode(None))

    def test_by_display_indexes_frames(self):
        by = prev.by_display(preview_payload())
        self.assertEqual(sorted(by), [0, 1])
        self.assertEqual(prev.by_display(None), {})
        self.assertEqual(prev.by_display({"frames": "nope"}), {})

    def test_paint_exact_fit_and_never_raises(self):
        img = Image.new("RGB", (200, 200), BG)
        shot = Image.new("RGB", (96, 60), (200, 30, 30))
        self.assertTrue(prev.paint(img, shot, (10, 20, 110, 80)))
        self.assertEqual(img.getpixel((60, 50)), (200, 30, 30))
        self.assertFalse(prev.paint(img, None, (10, 20, 110, 80)))
        self.assertFalse(prev.paint(img, shot, None))
        self.assertFalse(prev.paint(None, shot, (10, 20, 110, 80)))

    def test_badge_never_raises(self):
        img = Image.new("RGB", (400, 200), BG)
        draw = ImageDraw.Draw(img)
        for state in ("live", "stale", "off", None, 42):
            prev.badge(draw, 10, 10, state, None)


class MapOverlayCase(unittest.TestCase):
    def test_live_content_painted_under_overlays(self):
        img = draw_map(state_payload(), preview_payload())
        box = macbook_map.union(DISPLAYS)
        scale, ox, oy = macbook_map.frame(box, PANEL_W, PANEL_H,
                                          top=lay.HDR_H, bottom=PANEL_H)
        r0 = macbook_map.rect(DISPLAYS[0]["bounds"], scale, ox, oy)
        cx, cy = int((r0[0] + r0[2]) / 2), int((r0[1] + r0[3]) / 2)
        pixel = img.getpixel((cx, cy))
        self.assertNotEqual(pixel, BG)  # photo, not a box
        r1 = macbook_map.rect(DISPLAYS[1]["bounds"], scale, ox, oy)
        cx1 = int((r1[0] + r1[2]) / 2)
        self.assertNotEqual(img.getpixel((cx1, cy)), BG)

    def test_focus_rect_and_pointer_survive_previews(self):
        state = state_payload()
        img = draw_map(state, preview_payload())
        box = macbook_map.union(DISPLAYS)
        scale, ox, oy = macbook_map.frame(box, PANEL_W, PANEL_H,
                                          top=lay.HDR_H, bottom=PANEL_H)
        r0 = macbook_map.rect(DISPLAYS[0]["bounds"], scale, ox, oy)
        # Focus rect == full first display: top-edge midpoint is the
        # view's palette accent.
        accent = theme.rgb("macbook")
        self.assertEqual(img.getpixel((int((r0[0] + r0[2]) / 2),
                                       int(r0[1]) + 1)), accent)
        # Pointer dot centre is white fill.
        px, py = macbook_map.project(464, 283, scale, ox, oy)
        self.assertEqual(img.getpixel((int(px), int(py))),
                         (255, 255, 255))

    def test_off_mode_renders_boxes_explicitly(self):
        img = draw_map(state_payload(), None)
        box = macbook_map.union(DISPLAYS)
        scale, ox, oy = macbook_map.frame(box, PANEL_W, PANEL_H,
                                          top=lay.HDR_H, bottom=PANEL_H)
        r0 = macbook_map.rect(DISPLAYS[0]["bounds"], scale, ox, oy)
        cx, cy = int((r0[0] + r0[2]) / 2), int((r0[1] + r0[3]) / 2)
        self.assertEqual(img.getpixel((cx, cy)), BG)  # box, not photo
        self.assertEqual(prev.mode(None), "off")

    def test_stale_frames_still_paint_with_tag(self):
        img = draw_map(state_payload(), preview_payload(time.time() - 30))
        box = macbook_map.union(DISPLAYS)
        scale, ox, oy = macbook_map.frame(box, PANEL_W, PANEL_H,
                                          top=lay.HDR_H, bottom=PANEL_H)
        r0 = macbook_map.rect(DISPLAYS[0]["bounds"], scale, ox, oy)
        cx, cy = int((r0[0] + r0[2]) / 2), int((r0[1] + r0[3]) / 2)
        self.assertNotEqual(img.getpixel((cx, cy)), BG)

    def test_partial_frames_degrade_per_display(self):
        only_second = {"ts": time.time(),
                       "frames": preview_payload()["frames"][1:]}
        img = draw_map(state_payload(), only_second)
        box = macbook_map.union(DISPLAYS)
        scale, ox, oy = macbook_map.frame(box, PANEL_W, PANEL_H,
                                          top=lay.HDR_H, bottom=PANEL_H)
        r0 = macbook_map.rect(DISPLAYS[0]["bounds"], scale, ox, oy)
        cx, cy = int((r0[0] + r0[2]) / 2), int((r0[1] + r0[3]) / 2)
        self.assertEqual(img.getpixel((cx, cy)), BG)  # box for D1...
        r1 = macbook_map.rect(DISPLAYS[1]["bounds"], scale, ox, oy)
        self.assertNotEqual(img.getpixel((int((r1[0] + r1[2]) / 2), cy)),
                            BG)  # ...photo for D2

    def test_tap_mapping_unchanged_by_previews(self):
        box = macbook_map.union(DISPLAYS)
        scale, ox, oy = macbook_map.frame(box, PANEL_W, PANEL_H,
                                          top=lay.HDR_H, bottom=PANEL_H)
        px, py = macbook_map.project(1800, 500, scale, ox, oy)
        hit = macbook_map.locate(px, py, DISPLAYS, PANEL_W, PANEL_H,
                                 top=lay.HDR_H, bottom=PANEL_H)
        self.assertEqual(hit["display_index"], 1)
        self.assertAlmostEqual(hit["x"], 1800, places=3)


class KeyCase(unittest.TestCase):
    def test_key_moves_with_preview_ts(self):
        state, apps = state_payload(), None
        k1 = macbook_renderer._key(state, apps, None,
                                   preview_payload(time.time()),
                                   "glance", 0)
        k2 = macbook_renderer._key(state, apps, None,
                                   preview_payload(time.time() + 5),
                                   "glance", 0)
        self.assertNotEqual(k1, k2)
        k3 = macbook_renderer._key(state, apps, None, None,
                                   "glance", 0)
        self.assertNotEqual(k1, k3)


# --- Staggered tick: measured frame age, CPU, and wire ----------------
#
# The live two-display comparison could not be reproduced this session
# (the external display was disconnected), so the frame-age claim is
# measured here instead: a virtual clock drives the REAL preview loop,
# and a test-local replay of the base-commit batched schedule runs over
# the same fake capture wall -- same hardware model, two scheduling
# policies. Capture wall and JPEG size are the measured 2026-09-29
# numbers (1.5 s wall per display, ~0.4 s CPU, ~9 KB/frame at 480 px q6),
# so delivery age (capture -> first POST) comes out in panel units.
CAPTURE_WALL = 1.5       # measured ffmpeg wall per display (device open)
FAST_WALL = 0.4          # measured ffmpeg CPU per display: skip-able tick
REAL_FRAME_BYTES = 9000  # measured JPEG bytes per frame at 480 px q6
WIRE_CEILING = 100000    # objective: macbook -> panel stays under ~100 KB/s


def legacy_batched_loop(displayd_base, interval, stop):
    """Base-commit schedule: capture BOTH displays, then one POST.

    A verbatim replay of the pre-stagger preview_loop body (capture all
    displays, then POST the set) so the harness compares scheduling
    policies over identical capture/POST fakes, never two hand-written
    models."""
    failures = 0
    while not stop.is_set():
        t0 = bridge.time.monotonic()
        try:
            frames = []
            for index in range(len(bridge.active_displays() or [])):
                got = bridge.ffmpeg_frame(index)
                if got is None:
                    continue
                text = bridge.encode(got[0])
                if text is None:
                    continue
                frames.append({"display_index": index, "w": got[1],
                               "h": got[2], "jpeg": text})
            doc = bridge.preview_doc(frames)
            if doc is not None and bridge.post_preview(displayd_base, doc):
                failures = 0
            elif doc is not None:
                failures += 1
        except Exception:  # never die on a bad tick (base-commit shape)
            failures += 1
        wait = max(0.2, interval - (bridge.time.monotonic() - t0))
        if failures >= 5:
            wait = max(wait, 5.0)
        stop.wait(wait)


class VirtualClock:
    """monotonic/time/sleep stand-in for bridge.time."""

    def __init__(self):
        self.now = 0.0
        self.deadline = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000.0 + self.now

    def sleep(self, seconds):
        self.now += max(0.0, float(seconds))


class VirtualStop:
    """Event stand-in: wait() is the only thing that advances time."""

    def __init__(self, clock):
        self.clock = clock

    def is_set(self):
        return self.clock.now >= self.clock.deadline

    def wait(self, seconds):
        self.clock.sleep(seconds)
        return self.is_set()


class VirtualHarness:
    """Fake displays + capture + POST over a virtual clock."""

    def __init__(self, capture_wall=CAPTURE_WALL, static=False,
                 display_count=2, fail_index=None, fail_after=0):
        self.clock = VirtualClock()
        self.capture_wall = float(capture_wall)
        self.static = bool(static)
        self.display_count = int(display_count)
        self.fail_index = fail_index
        self.fail_after = int(fail_after)
        self.captures = []  # (display_index, capture_start, payload)
        self.posts = []     # {"at", "doc", "bytes"}

    def displays(self):
        return [(i, 0.0, 0.0, 1440.0, 900.0, i == 0)
                for i in range(self.display_count)]

    def ffmpeg_frame(self, index, *args, **kwargs):
        start = self.clock.now
        self.clock.now += self.capture_wall
        index = int(index)
        ok = sum(1 for i, _s, _p in self.captures if i == index)
        if self.fail_index == index and ok >= self.fail_after:
            return None
        if self.static:
            payload = b"static-jpeg-%d" % index
        else:
            payload = b"jpeg-%d-%r" % (index, start)
        self.captures.append((index, start, payload))
        return (payload, 96, 60)

    def post_preview(self, displayd_base, doc, timeout=5.0):
        self.posts.append({"at": self.clock.now, "doc": doc,
                           "bytes": len(json.dumps(doc))})
        return True

    def run(self, loop, seconds=30.0, interval=None, dedup=True):
        self.clock.deadline = float(seconds)
        stop = VirtualStop(self.clock)
        if interval is None:
            interval = bridge.PREVIEW_INTERVAL
        patches = [
            mock.patch.object(bridge, "time", self.clock),
            mock.patch.object(bridge, "active_displays", self.displays),
            mock.patch.object(bridge, "ffmpeg_frame", self.ffmpeg_frame),
            mock.patch.object(bridge, "post_preview", self.post_preview),
        ]
        if not dedup:
            patches.append(mock.patch.object(bridge._plan, "may_skip_post",
                                             lambda *a, **k: False))
        for patch in patches:
            patch.start()
        try:
            loop("http://panel.invalid", interval, stop)
        finally:
            for patch in reversed(patches):
                patch.stop()
        return self


def p95(values):
    """Nearest-rank p95; 0.0 when there is nothing to measure."""
    vals = sorted(values)
    if not vals:
        return 0.0
    return vals[min(len(vals) - 1, int(math.ceil(0.95 * len(vals))) - 1)]


def capture_starts(harness):
    """Posted base64 jpeg text -> the capture start that produced it."""
    return {base64.b64encode(payload).decode("ascii"): start
            for _i, start, payload in harness.captures}


def delivery_ages(harness):
    """capture -> first POST age per captured frame.

    A restated last-known frame is not a new delivery (it carries no new
    pixels), so each (display, jpeg) counts once -- the moment that
    frame's bytes first reached the wire. The clock starts when the
    capture STARTS, which is the conservative end-to-end reading
    (capture wall included); measuring from capture END would drop the
    ~1.5 s the ffmpeg itself takes and flatter the result."""
    starts = capture_starts(harness)
    seen, ages = set(), []
    for post in harness.posts:
        for frame in post["doc"]["frames"]:
            key = (frame["display_index"], frame["jpeg"])
            if key in seen or frame["jpeg"] not in starts:
                continue
            seen.add(key)
            ages.append(post["at"] - starts[frame["jpeg"]])
    return ages


def displayed_ages(harness, step=0.25):
    """Panel-visible frame age, sampled between POSTs."""
    starts = capture_starts(harness)
    out, index, at = [], 0, 0.0
    while at < harness.clock.deadline and harness.posts:
        while (index + 1 < len(harness.posts)
               and harness.posts[index + 1]["at"] <= at):
            index += 1
        if harness.posts[index]["at"] <= at:
            for frame in harness.posts[index]["doc"]["frames"]:
                if frame["jpeg"] in starts:
                    out.append(at - starts[frame["jpeg"]])
        at += step
    return out


def post_gaps(harness):
    times = [post["at"] for post in harness.posts]
    return [b - a for a, b in zip(times, times[1:])]


def projected_wire(harness, seconds):
    """Bytes/second at the measured real frame size, not the fake one."""
    if not seconds:
        return 0.0
    frames = sum(len(post["doc"]["frames"]) for post in harness.posts)
    return frames * REAL_FRAME_BYTES / float(seconds)


class BridgeBinaryCase(unittest.TestCase):
    """Which ffmpeg the bridge captures with (live-verified breakage)."""

    def test_override_wins_and_homebrew_beats_system_profile(self):
        self.assertEqual(
            bridge.resolve_ffmpeg(env={"DISPLAYD_FFMPEG": "/x/ff"},
                                  exists=lambda p: True), "/x/ff")
        # Homebrew holds the Screen Recording grant (verified live
        # 2026-09-29); the system profile is a fallback only.
        self.assertEqual(
            bridge.resolve_ffmpeg(env={}, exists=lambda p: True),
            "/opt/homebrew/bin/ffmpeg")
        self.assertEqual(
            bridge.resolve_ffmpeg(
                env={}, exists=lambda p: p == "/opt/homebrew/bin/ffmpeg"),
            "/opt/homebrew/bin/ffmpeg")
        self.assertEqual(
            bridge.resolve_ffmpeg(env={}, exists=lambda p: False), "ffmpeg")

    def test_resolve_never_raises_on_bad_exists(self):
        def boom(_path):
            raise OSError("nope")
        self.assertEqual(bridge.resolve_ffmpeg(env={}, exists=boom),
                         "ffmpeg")
        self.assertEqual(bridge.resolve_ffmpeg(env=None), bridge.FFMPEG_BIN)


class StaggeredTickCase(unittest.TestCase):
    """Frame age, CPU proxy, wire, and freshness of the staggered tick."""

    SECONDS = 30.0

    def runs(self, capture_wall=CAPTURE_WALL, static=False, **kwargs):
        # The baseline ran the old ~1 Hz tick; the staged loop runs the
        # production interval, so the comparison uses both real configs.
        legacy = VirtualHarness(capture_wall, static, **kwargs).run(
            legacy_batched_loop, self.SECONDS, interval=1.0)
        staged = VirtualHarness(capture_wall, static, **kwargs).run(
            bridge.preview_loop, self.SECONDS)
        return legacy, staged

    def test_p95_delivery_age_halved_at_measured_capture_wall(self):
        legacy, staged = self.runs()
        base_p95 = p95(delivery_ages(legacy))
        new_p95 = p95(delivery_ages(staged))
        # Baseline: d0 waits out d1's capture (3.0 s); staggered: each
        # frame is POSTed after its own capture (1.5 s).
        self.assertAlmostEqual(base_p95, 2 * CAPTURE_WALL, places=6)
        self.assertLessEqual(new_p95, base_p95 / 2.0 + 1e-6)

    def test_panel_visible_age_improves(self):
        legacy, staged = self.runs()
        self.assertLess(p95(displayed_ages(staged)),
                        p95(displayed_ages(legacy)))

    def test_cpu_proxy_not_above_baseline(self):
        # Captures are the whole CPU cost, so captures/second is the
        # per-set CPU proxy: the stagger never captures more often.
        legacy, staged = self.runs()
        self.assertTrue(legacy.captures and staged.captures)
        self.assertLessEqual(len(staged.captures), len(legacy.captures))
        self.assertLessEqual(len(staged.captures) / self.SECONDS,
                             len(legacy.captures) / self.SECONDS)

    def test_wire_stays_under_ceiling_and_feed_never_goes_stale(self):
        legacy, staged = self.runs()
        self.assertLess(projected_wire(staged, self.SECONDS), WIRE_CEILING)
        self.assertLess(projected_wire(legacy, self.SECONDS), WIRE_CEILING)
        # The staggered tick posts sooner and more often than the batched
        # set did (whose own set period sat at the 3.0 s stale edge), so
        # no POST gap may reach the renderer's freshness window.
        self.assertLess(max(post_gaps(staged)), prev.PREVIEW_FRESH)

    def test_single_display_is_a_no_op(self):
        # The machine we could actually measure: no second display to
        # wait out, so the stagger must not change age or cost at all.
        legacy, staged = self.runs(display_count=1)
        self.assertAlmostEqual(p95(delivery_ages(staged)),
                               p95(delivery_ages(legacy)), places=6)
        self.assertAlmostEqual(len(staged.captures),
                               len(legacy.captures), delta=1)

    def test_failed_capture_becomes_a_box_never_a_stale_photo(self):
        # Display 1 dies after two good captures: it must leave the
        # POSTed set (the renderer draws a box) like the batched
        # baseline's missing frame, not linger as yesterday's pixels.
        harness = VirtualHarness(FAST_WALL, static=True, fail_index=1,
                                 fail_after=2).run(bridge.preview_loop,
                                                   self.SECONDS)
        seen = [{f["display_index"] for f in post["doc"]["frames"]}
                for post in harness.posts]
        self.assertIn(1, seen[1])           # it did deliver first
        self.assertNotIn(1, seen[-1])       # then it was dropped
        self.assertEqual(seen[-1], {0})

    def test_every_posted_document_stays_schema_valid(self):
        _legacy, staged = self.runs()
        self.assertTrue(staged.posts)
        for post in staged.posts:
            validate_value(post["doc"], macbook_renderer.INPUTS["preview"],
                           "preview")
            for frame in post["doc"]["frames"]:
                self.assertLessEqual(len(frame["jpeg"]), 56000)


class ByteIdenticalSkipCase(unittest.TestCase):
    """Byte-identical frames skip the wire POST (when the tick allows)."""

    SECONDS = 30.0

    def test_static_screen_skips_posts_without_going_stale(self):
        # A tick fast enough to skip (measured CPU wall, not device wall):
        # identical sets leave the wire quiet, and the POST that does go
        # out always lands inside the freshness window.
        skipped = VirtualHarness(FAST_WALL, static=True).run(
            bridge.preview_loop, self.SECONDS)
        sent = VirtualHarness(FAST_WALL, static=True).run(
            bridge.preview_loop, self.SECONDS, dedup=False)
        self.assertLess(len(skipped.posts), len(skipped.captures))
        self.assertEqual(len(sent.posts), len(sent.captures))
        self.assertLess(len(skipped.posts), len(sent.posts))
        self.assertLess(projected_wire(skipped, self.SECONDS),
                        projected_wire(sent, self.SECONDS))
        self.assertLess(max(post_gaps(skipped)), prev.PREVIEW_FRESH)

    def test_changed_screen_always_posts(self):
        # A screen that really changes must never be deduped away.
        changing = VirtualHarness(FAST_WALL, static=False).run(
            bridge.preview_loop, self.SECONDS)
        self.assertEqual(len(changing.posts), len(changing.captures))

    def test_device_wall_tick_posts_once_per_tick(self):
        # Honest degradation: at the measured 1.5 s capture wall the tick
        # is slower than the freshness budget can absorb, so dedup skips
        # nothing and the live badge always wins over wire savings.
        staged = VirtualHarness(CAPTURE_WALL, static=True).run(
            bridge.preview_loop, self.SECONDS)
        self.assertEqual(len(staged.posts), len(staged.captures))
        self.assertLess(max(post_gaps(staged)), prev.PREVIEW_FRESH)


if __name__ == "__main__":
    unittest.main()
