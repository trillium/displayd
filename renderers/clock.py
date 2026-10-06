"""A large running clock. Re-renders once a second.

One line of type, centred, scaled to fit the panel -- which is exactly the
panel component's ``headline``. This view owns the pattern, the refresh
and nothing else: it used to carry its own binary-search font fitter (the
fourth copy of "make this line fit") and its own default inks, so the
clock could disagree with the panel's palette about what "big type" is.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import theme
from ui import panel as ui_panel

NAME = "clock"
DESCRIPTION = "A large clock that updates every second"
STATIC = False
CAPABILITY = "partial"  # the digit auto-fits the region
PARAMS = {
    "format": {"type": "string", "help": "strftime pattern, default %H:%M:%S"},
    "color": {"type": "string", "help": "text colour, default white"},
    "background": {"type": "string", "help": "background colour, default black"},
}

DIGIT_MAX = 900  # the size to scale down from: the panel's tallest type


def run(screen, params, stop):
    pattern = params.get("format") or "%H:%M:%S"
    fg = screen.color(params.get("color"), theme.rgb("ink-strong"))
    bg = screen.color(params.get("background"), theme.rgb("page"))

    last = None
    while not stop.is_set():
        text = time.strftime(pattern)
        if text != last:
            last = text
            img = screen.new_image(bg)
            ui_panel.headline(img, screen, text, ink=fg, size=DIGIT_MAX)
            screen.present(img)
        stop.wait(0.25)
