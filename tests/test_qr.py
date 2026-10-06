"""Tests for the qr renderer + qr_common shared piece.

The hard requirement: decode the rendered PNG back to the exact input
string, at real output resolution (1920x1080), with an independent
decoder -- never eyeballing. Decoder of choice is OpenCV's
QRCodeDetector (present in this environment); pyzbar is the fallback.
If neither is installed the round-trip tests skip rather than fake it.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import io
import os
import re
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
from PIL import Image

import theme
from renderers import qr
from renderers import qr_common

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def decode_png(png_bytes):
    """Independent decode: returns the decoded string or raises/None."""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        data, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
        if data:
            return data
    except ImportError:
        pass
    try:
        from pyzbar.pyzbar import decode as zbar_decode
        found = zbar_decode(Image.open(io.BytesIO(png_bytes)))
        if found:
            return found[0].data.decode("utf-8")
    except ImportError:
        pass
    return None


def decoder_available():
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import pyzbar  # noqa: F401
        return True
    except ImportError:
        return False


class FakeScreen:
    W, H = 1920, 1080

    def __init__(self):
        self.frames = []

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    def clear(self, background=(0, 0, 0)):
        self.present(self.new_image(background))

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        return displayd.Screen.color(value, default)

    @staticmethod
    def font_path(family="DejaVuSans-Bold"):
        return displayd.Screen.font_path(family)


def run_qr(params):
    screen = FakeScreen()
    qr.run(screen, params, threading.Event())
    assert screen.frames, "qr.run presented nothing"
    return screen.frames[-1]


def to_png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@unittest.skipUnless(decoder_available(),
                     "no independent QR decoder installed (cv2/pyzbar)")
class TestRoundTrip(unittest.TestCase):
    def test_default_url_decodes_exactly(self):
        img = run_qr({"data": qr_common.DEFAULT_URL})
        self.assertEqual(img.size, (1920, 1080))
        self.assertEqual(decode_png(to_png(img)), qr_common.DEFAULT_URL)

    def test_two_payloads_decode_differently(self):
        first = run_qr({"data": "http://lnx-server:8980/"})
        second = run_qr({"data": "http://lnx-server:8181/"})
        self.assertEqual(decode_png(to_png(first)), "http://lnx-server:8980/")
        self.assertEqual(decode_png(to_png(second)), "http://lnx-server:8181/")
        self.assertNotEqual(first.tobytes(), second.tobytes(),
                            "changing data must change the code")

    def test_badge_decodes_exactly(self):
        screen = FakeScreen()
        img = screen.new_image((8, 8, 12))
        rect = qr_common.draw_qr_badge(img, screen, qr_common.DEFAULT_URL)
        self.assertEqual(decode_png(to_png(img)), qr_common.DEFAULT_URL)
        x0, y0, x1, y1 = rect
        self.assertTrue(0 <= x0 < x1 <= 1920 and 0 <= y0 < y1 <= 1080)


class TestContract(unittest.TestCase):
    def test_renderer_loads_and_advertises(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("qr", found)
        self.assertIn("module", found["qr"], found["qr"].get("broken"))
        mod = found["qr"]["module"]
        self.assertTrue(mod.STATIC)
        self.assertTrue(callable(mod.run))
        self.assertTrue(mod.PARAMS["data"].get("required"))
        for pname, spec in mod.PARAMS.items():
            self.assertIn(spec.get("type"),
                          ("string", "integer", "number", "boolean"))

    def test_helpers_are_not_renderers(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertNotIn("qr_common", found)
        self.assertNotIn("_qrcodegen", found)

    def test_integer_module_scaling(self):
        img = run_qr({"data": qr_common.DEFAULT_URL})
        code = qr_common.encode(qr_common.DEFAULT_URL)
        total = qr_common.symbol_modules(code)
        scale, actual = qr_common.fit_scale(code, qr.DEFAULT_SIZE)
        self.assertGreaterEqual(scale, 8,
                                "default modules should be chunky, got %r" % scale)
        self.assertEqual(actual % total, 0,
                         "symbol must be an integer multiple of modules")

    def test_empty_data_prompts_instead_of_crashing(self):
        img = run_qr({"data": ""})
        self.assertEqual(img.size, (1920, 1080))
        self.assertTrue(any(img.tobytes()), "prompt frame must not be blank")

    def test_missing_data_defaults_to_control_page(self):
        img = run_qr({})
        if decoder_available():
            self.assertEqual(decode_png(to_png(img)), qr_common.DEFAULT_URL)
        else:
            self.assertTrue(any(img.tobytes()))

    def test_oversize_payload_fails_clearly(self):
        img = run_qr({"data": "x" * 4000})
        self.assertEqual(img.size, (1920, 1080))
        self.assertTrue(any(img.tobytes()),
                        "oversize payload must message, not blank")


class TestComponentMigration(unittest.TestCase):
    """The QR view draws nothing itself: the panel and the layer's line.

    It used to hold a fourth font loader, a prompt with two grey literals
    chosen by an inline brightness test, and a caption shrink loop.
    """

    def source(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def test_the_module_holds_no_drawing_primitive_or_font_loader(self):
        src = self.source(os.path.join("renderers", "qr.py"))
        self.assertNotIn("ImageDraw", src)
        self.assertNotIn("from PIL", src)
        self.assertNotIn("def _font", src)
        self.assertEqual(HEX_RE.findall(src), [])
        self.assertIn("from ui import", src)
        self.assertIn("fit_size", src)

    def test_the_prompt_wears_the_ink_that_reads_on_the_card(self):
        # The default QR card is white: the prompt must be the dark role,
        # not the old (90, 90, 100) / (160, 160, 170) pair.
        img = run_qr({"data": ""})
        painted = {c for _n, c in img.getcolors(1 << 24)}
        self.assertIn(theme.ink_on((255, 255, 255)), painted)
        self.assertNotIn((90, 90, 100), painted)
        self.assertNotIn((160, 160, 170), painted)
        # And the other way round on a dark card.
        dark = run_qr({"data": "", "background": "#000000"})
        dark_painted = {c for _n, c in dark.getcolors(1 << 24)}
        self.assertIn(theme.ink_on((0, 0, 0)), dark_painted)

    def test_a_long_caption_is_fitted_inside_the_panel(self):
        from ui import text as ui_text

        caption = "x" * 90
        img = run_qr({"data": qr_common.DEFAULT_URL, "caption": caption})
        screen = FakeScreen()
        room = int(screen.W * 0.9)
        size = ui_text.fit_size(screen, caption, qr.CAPTION_SIZE, room,
                                floor=20, step=4)
        self.assertLessEqual(ui_text.width(screen, caption, size), room)
        # The caption sits under the code; a fitted line keeps a margin, a
        # clipped one would run to both panel edges.
        rows = [img.getpixel((x, y))
                for y in range(915, 975)
                for x in list(range(0, 40)) + list(range(1880, 1920))]
        self.assertEqual({c for c in rows if c != qr_common.QR_BG}, set())


if __name__ == "__main__":
    unittest.main()
