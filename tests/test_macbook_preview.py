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
import os
import sys
import time
import unittest

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
        # Focus rect == full first display: top-edge midpoint is ACCENT.
        accent = tuple(int(glance.ACCENT[i:i + 2], 16)
                       for i in (1, 3, 5))
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


if __name__ == "__main__":
    unittest.main()
