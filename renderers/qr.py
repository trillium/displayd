"""A scannable QR code on the panel, with configurable content.

STATIC: drawn once, then parked. No inputs, no polling.

Legibility is the feature: this is read off a 1920x1080 panel at
phone-camera distance, possibly at an angle. So the symbol is rendered
large (default 800px incl. quiet zone), with integer module scaling
(crisp edges, never smooth-scaled), a 4-module quiet zone, and
near-black on near-white by default regardless of the panel's dark
theme. See renderers/qr_common.py for the shared piece.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qr_common
from _qrcodegen import QrCode

from PIL import ImageDraw, ImageFont

NAME = "qr"
DESCRIPTION = "Scannable QR code, centred large with a caption"
STATIC = True
PARAMS = {
    "data": {"type": "string", "required": True,
             "help": "what the code encodes, typically a URL; "
                     "defaults to the displayd control page when omitted"},
    "caption": {"type": "string",
                "help": "text shown beneath the code so a human knows "
                        "what it is before scanning"},
    "size": {"type": "integer",
             "help": "QR side length in pixels incl. quiet zone, "
                     "default 800 (module size is derived, integer)"},
    "color": {"type": "string", "help": "module colour, default black"},
    "background": {"type": "string",
                   "help": "card/quiet-zone colour, default white"},
}

DEFAULT_SIZE = 800
CAPTION_SIZE = 52
PROMPT_TITLE_SIZE = 120
PROMPT_BODY_SIZE = 48


def _font(screen, name, size):
    try:
        path = screen.font_path(name)
    except Exception:
        return None
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return None


def _prompt(screen, bg, title, body):
    """Sensible placeholder: never a crash, never a blank panel."""
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    title_font = _font(screen, "DejaVuSans-Bold", PROMPT_TITLE_SIZE)
    body_font = _font(screen, "DejaVuSans", PROMPT_BODY_SIZE)
    ink = (90, 90, 100) if sum(bg) > 384 else (160, 160, 170)
    draw.multiline_text(
        (screen.W // 2, screen.H // 2 - 40), title,
        font=title_font or body_font, fill=ink,
        anchor="mm", align="center",
    )
    if body:
        draw.multiline_text(
            (screen.W // 2, screen.H // 2 + 120), body,
            font=body_font or title_font, fill=ink,
            anchor="ma", align="center", spacing=10,
        )
    screen.present(img)


def run(screen, params, stop):
    params = params or {}
    fg = screen.color(params.get("color"), qr_common.QR_FG)
    bg = screen.color(params.get("background"), qr_common.QR_BG)
    try:
        size = int(params.get("size") or DEFAULT_SIZE)
    except (TypeError, ValueError):
        size = DEFAULT_SIZE
    size = max(120, min(size, min(screen.W, screen.H) - 40))

    raw = params.get("data")
    if raw is None:
        raw = qr_common.DEFAULT_URL
    data = str(raw)
    caption = params.get("caption")
    if caption is None:
        caption = data if data else "QR"
    caption = str(caption)

    if not data:
        _prompt(screen, bg, "QR",
                "pass data to encode\n"
                "e.g. {\"data\": \"%s\"}" % qr_common.DEFAULT_URL)
        return

    try:
        qr = qr_common.encode(data, QrCode.Ecc.MEDIUM)
    except ValueError:
        # Payload beyond QR capacity: a clear message, never a smear.
        _prompt(screen, bg, "QR payload too long",
                "%d characters exceeds QR capacity\n"
                "use a shorter URL" % len(data))
        return

    scale, actual = qr_common.fit_scale(qr, size)
    symbol = qr_common.render_symbol(qr, fg=fg, bg=bg, scale=scale)

    img = screen.new_image(bg)
    cap_font = _font(screen, "DejaVuSans", CAPTION_SIZE)
    cap_h = 0
    if caption:
        cap_h = CAPTION_SIZE + 36
    total_h = actual + cap_h
    top = (screen.H - total_h) // 2
    left = (screen.W - actual) // 2
    img.paste(symbol, (left, top))

    if caption:
        draw = ImageDraw.Draw(img)
        # Shrink the caption to fit rather than clipping it.
        font = cap_font
        if font is not None:
            try:
                size_px = CAPTION_SIZE
                while (size_px > 20 and
                       draw.textlength(caption, font=font) > screen.W * 0.9):
                    size_px -= 4
                    font = _font(screen, "DejaVuSans", size_px)
                    if font is None:
                        break
            except Exception:
                font = cap_font
        draw.text((screen.W // 2, top + actual + 24), caption,
                  font=font, fill=fg, anchor="ma")

    screen.present(img)
