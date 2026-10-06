"""Render text, centred and auto-fitted to the screen.

The card is the component layer's ``ui.panel``: the largest face up to a
ceiling whose whole block fits the panel, centred on it. This module owns
the params and nothing else -- no font loader, no fitting search, and no
colour.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import theme
from ui import panel

NAME = "text"
DESCRIPTION = "Show a message centred on the screen, auto-sized to fill it"
STATIC = True
CAPABILITY = "partial"  # centred and auto-sized to the region
PARAMS = {
    "text": {"type": "string", "required": True, "help": "message to show; \\n starts a new line"},
    "size": {"type": "integer", "help": "font pixel height; auto-fitted when omitted"},
    "font": {"type": "string", "help": "font family, default DejaVuSans-Bold"},
    "color": {"type": "string", "help": "text colour, default the palette's strongest ink"},
    "background": {"type": "string", "help": "background colour, default the panel background"},
}

CEILING = 900  # the biggest face an auto-fitted message may reach


def run(screen, params, stop):
    text = str(params.get("text", ""))
    if not text:
        return
    fg = screen.color(params.get("color"), theme.rgb("ink-strong"))
    bg = screen.color(params.get("background"), theme.rgb("page"))
    family = params.get("font") or "DejaVuSans-Bold"

    img = screen.new_image(bg)
    # An explicit size is used as given; without one the face is fitted to
    # the panel, so a long message shrinks rather than running off it.
    size = int(params.get("size") or 0) or CEILING
    panel.card(img, screen, text, ink=fg, family=family, title_size=size)
    screen.present(img)
