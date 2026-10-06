"""A scannable QR code on the panel, with configurable content.

STATIC: drawn once, then parked. No inputs, no polling.

Legibility is the feature: this is read off a 1920x1080 panel at
phone-camera distance, possibly at an angle. So the symbol is rendered
large (default 800px incl. quiet zone), with integer module scaling
(crisp edges, never smooth-scaled), a 4-module quiet zone, and
near-black on near-white by default regardless of the panel's dark
theme. See renderers/qr_common.py for the shared piece.

Presentation is the component layer's: the placeholder is ``ui.panel``,
the caption is the layer's one line-of-type rule, and the ink on the
card's own page colour is ``theme.ink_on`` -- this module draws nothing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qr_common
import theme
from _qrcodegen import QrCode
from ui import panel as ui_panel
from ui import text as ui_text

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


def _prompt(screen, bg, title, body):
    """Sensible placeholder: never a crash, never a blank panel.

    Drawn by the panel component on the caller's own page colour, with
    the ink the token layer picks for that surface -- a white QR card gets
    dark type instead of white-on-white.
    """
    img = screen.new_image(bg)
    ink = theme.ink_on(bg)
    ui_panel.block(img, screen, title, ink=ink, size=PROMPT_TITLE_SIZE,
                   centre=(screen.W // 2, screen.H // 2 - 40), bold=True)
    if body:
        ui_panel.block(img, screen, body, ink=ink, size=PROMPT_BODY_SIZE,
                       centre=(screen.W // 2, screen.H // 2 + 120), top=True)
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
    cap_h = CAPTION_SIZE + 36 if caption else 0
    total_h = actual + cap_h
    top = (screen.H - total_h) // 2
    left = (screen.W - actual) // 2
    img.paste(symbol, (left, top))

    if caption:
        # Shrink the caption to fit rather than clipping it: the layer's
        # one fit rule, centred on the panel by its top edge.
        size = ui_text.fit_size(screen, caption, CAPTION_SIZE,
                                int(screen.W * 0.9), floor=20, step=4)
        ui_text.write(img, screen, (screen.W // 2, top + actual + 24),
                      caption, ink=fg, size=size, anchor="ma")

    screen.present(img)
