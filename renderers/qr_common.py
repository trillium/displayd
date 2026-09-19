"""Shared QR piece for the jumbotron. NOT a renderer: no run(), so the
daemon's loader skips this file (same convention as beads_common.py).

Both the standalone `qr` view (renderers/qr.py) and any composed caller
-- e.g. the beads overview -- import this module, so the code on the wall
and the code on a badge can never drift apart.

Encoding is the vendored Nayuki generator (renderers/_qrcodegen.py, MIT):
pure Python, standard library only. Drawing needs only Pillow. Nothing
here touches the network, the framebuffer, or any credential.

Legibility rules (the whole feature):
- integer module scaling only: the symbol is rasterised 1px-per-module
  then upscaled with NEAREST, so module edges stay crisp. A smooth-scaled
  QR is a common scanning failure.
- quiet zone of at least 4 modules is always part of the symbol.
- near-black on near-white by default, regardless of the panel theme:
  scanners want contrast, not aesthetics.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _qrcodegen import QrCode  # noqa: E402

# The displayd control page: what drives the display. (The separate
# service visualizer lives at http://lnx-server:8181/ -- that shows the
# fleet, it does not drive this panel, so it is not the default.)
DEFAULT_URL = "http://lnx-server:8980/"

QR_FG = (0, 0, 0)
QR_BG = (255, 255, 255)

# Spec-minimum quiet zone, in modules. Always rendered; never zero.
QUIET_MODULES = 4

# Badge geometry for composed callers (beads overview): a white card in
# the bottom-right corner. ~9px modules at version 2: comfortable at
# arm's length, not a distance-scannable poster -- that is what the
# standalone qr view is for.
BADGE_SIDE = 300
BADGE_MARGIN = 36
BADGE_CAPTION = "displayd control"


def encode(data, ecl=QrCode.Ecc.MEDIUM):
    """Encode text to a QrCode, auto-selecting the minimal version.

    Raises _qrcodegen.DataTooLongError (a ValueError) when the payload
    exceeds version-40 capacity -- callers must catch it and draw a
    message, never an unscannable smear.
    """
    return QrCode.encode_text(data, ecl)


def symbol_modules(qr, border=QUIET_MODULES):
    """Total side length in modules, quiet zone included."""
    return qr.get_size() + 2 * border


def render_symbol(qr, fg=QR_FG, bg=QR_BG, scale=1, border=QUIET_MODULES):
    """Rasterise a QrCode to a PIL RGB image with crisp integer scaling."""
    from PIL import Image

    size = qr.get_size()
    total = size + 2 * border
    scale = max(1, int(scale))
    canvas = Image.new("RGB", (total, total), bg)
    px = canvas.load()
    for y in range(size):
        for x in range(size):
            if qr.get_module(x, y):
                px[x + border, y + border] = fg
    if scale != 1:
        canvas = canvas.resize((total * scale, total * scale), Image.NEAREST)
    return canvas


def fit_scale(qr, target_px, border=QUIET_MODULES):
    """Largest integer module size such that the symbol fits target_px.

    Returns (scale, actual_px). scale is >= 1: an absurdly small target
    overflows rather than blurring -- crispness is never sacrificed.
    """
    total = symbol_modules(qr, border)
    return max(1, target_px // total), max(1, target_px // total) * total


def draw_qr_badge(img, screen, data=None, side=BADGE_SIDE,
                  margin=BADGE_MARGIN, caption=BADGE_CAPTION):
    """Paste a scannable QR card onto an already-composed full-screen frame.

    `img` is the 1920x1080 (or whatever the panel is) PIL image the caller
    is about to present; `screen` supplies colour/font helpers. `data`
    falsy selects DEFAULT_URL, so a caller that has no URL configured
    still shows a working code, never a blank hole.

    Returns the card rect (x0, y0, x1, y1). Never raises: an
    over-long payload draws a small "too long" card instead.
    """
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(img)
    text = (data or "") if isinstance(data, str) else str(data or "")
    if not text:
        text = DEFAULT_URL

    cap_font = None
    try:
        path = screen.font_path("DejaVuSans")
        if path:
            cap_font = ImageFont.truetype(path, 28)
    except Exception:
        cap_font = None

    cap_h = 40 if caption else 8
    pad = 16
    try:
        qr = encode(text)
    except ValueError:
        # Too long: a legible message card, never a smear.
        x0 = img.width - side - margin
        y0 = img.height - side - margin
        draw.rectangle([x0, y0, img.width - margin, img.height - margin],
                       fill=QR_BG, outline=(200, 60, 60), width=3)
        draw.text((x0 + pad, y0 + pad), "QR payload too long",
                  font=cap_font, fill=(160, 30, 30))
        return (x0, y0, img.width - margin, img.height - margin)

    scale, actual = fit_scale(qr, side - 2 * pad)
    symbol = render_symbol(qr, scale=scale)
    card_w = actual + 2 * pad
    card_h = actual + 2 * pad + cap_h
    x0 = img.width - card_w - margin
    y0 = img.height - card_h - margin
    draw.rectangle([x0, y0, x0 + card_w, y0 + card_h], fill=QR_BG)
    img.paste(symbol, (x0 + pad, y0 + pad))
    if caption:
        draw.text((x0 + card_w // 2, y0 + pad + actual + 8), caption,
                  font=cap_font, fill=(40, 40, 40), anchor="ma")
    return (x0, y0, x0 + card_w, y0 + card_h)
