"""Panel sleep: the dedicated dark view behind the wake target.

Shown ONLY by the daemon's power-off path (see DisplayDaemon.set_power):
going dark also switches here, so the touch service's per-view wake
region (view_regions "sleep": one fullscreen screen_on target) goes live
exactly while the panel is asleep and never otherwise. A dedicated name
is the point: repurposing a generic fill (e.g. solid) would arm the wake
target on every unrelated use and shadow that view's own controls.

Normally unseen -- the backlight is off while this shows. The dim hint is
for the lit case only (a manual POST /show sleep demo): it names the way
back so the view never traps anyone. STATIC, no inputs.

The hint is the component layer's ``ui.panel`` card: a centred line in the
palette's faint role, so this view owns no font, no colour and no drawing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import theme
from ui import panel

NAME = "sleep"
DESCRIPTION = ("Panel sleep: near-black view shown while the backlight "
               "is off; any tap wakes via the sleep wake region")
STATIC = True
PARAMS = {
    "hint": {"type": "string",
             "help": "centered hint, default 'asleep -- tap anywhere "
                     "to wake'"},
    "background": {"type": "string",
                   "help": "background colour, default the panel background"},
    "color": {"type": "string",
              "help": "hint colour, default the palette's faint ink"},
}

DEFAULT_HINT = "asleep \u2014 tap anywhere to wake"
CEILING = 48  # the hint never gets bigger than this


def run(screen, params, stop):
    params = params or {}
    hint = str(params.get("hint") or DEFAULT_HINT)
    bg = screen.color(params.get("background"), theme.rgb("page"))
    fg = screen.color(params.get("color"), theme.rgb("faint"))
    img = screen.new_image(bg)
    panel.card(img, screen, hint, ink=fg, bold=False, family="DejaVuSans",
               title_size=min(int(screen.H) // 20, CEILING))
    screen.present(img)
