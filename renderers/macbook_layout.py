"""Shared geometry for the merged macbook feature (GLANCE + AIM modes).

Pure layout (no PIL, no daemon state): the macbook renderer draws it,
the daemon hit-tests taps against it, and the touch regions cover it --
one source of truth so draw, tap, and region cannot drift (same contract
as talon_layout.py, which this module supersedes for the merged view).

GLANCE claims a slim full-width header (HDR_H) for the tab-through app
strip plus state, and the display map fills everything below it -- no
fixed 210/250 bands, no bottom pane. AIM drops every overlay: the
review capture fills the whole panel edge-to-edge.

Touch order (see touch_regions): back/tab/focus first, map next, the
full-panel click region LAST so it only catches taps outside the header
controls. The map tap is the zoom entry: the daemon warps the cursor
to the tapped point AND re-pins the view to AIM in the same gesture,
so no AIM button exists (retired -- the tap is the entry, not a mode
control). In AIM the header rects still route (the corner one is the
way back to GLANCE); the daemon's mode gates refuse anything else, so
a tap can never mis-focus or mis-move -- it either clicks or is
refused.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from macbook_paging import (PAGE_STRIDE, VISIBLE, page, page_bounds,
                            page_slots, page_start)

PAD = 48
HDR_H = 148

# Composited chrome keep-outs: the home badge (top-left) and the sleep
# badge (top-right) overlay every view, so no header control may live
# under them -- hidden controls still win taps (scoped regions precede
# global ones) and would steal badge taps. The strip sits between the
# badges; the back corner sits bottom-left (free in both modes: in
# GLANCE it is a harmless no-op zone on the map, in AIM it is the way
# back). No AIM button: the map tap enters the zoom directly.
CHROME_L = 176
CHROME_R = 176

# Header rows (panel px, 1920x1080 canvas; parametric in w below).
TITLE_Y = 14
SUB_Y = 52
STRIP_Y = 88
STRIP_H = 52

STEP_W = 72
BACK_W = 180
BACK_H = 56
GAP = 12


def header_bottom():
    """GLANCE map top: the slim header claims 0..HDR_H, nothing more."""
    return HDR_H


def back_rect(h=1080):
    """'back to GLANCE' corner rect: (x, y, w, h), bottom-left.

    Harmless in GLANCE (re-pins glance); the way back while AIM shows."""
    return (0, h - BACK_H, BACK_W, BACK_H)


def stepper_rect(side, w):
    """Tab-stepper rect for -1 (left) or +1 (right): (x, y, w, h)."""
    if side == "right":
        return (w - CHROME_R - STEP_W, STRIP_Y, STEP_W, STRIP_H)
    return (CHROME_L, STRIP_Y, STEP_W, STRIP_H)


def strip_rect(w):
    """Whole app-strip band (steppers + chips): [x, y, w, h] for touch."""
    return [CHROME_L, STRIP_Y, w - CHROME_L - CHROME_R, STRIP_H]


def chip_area(w):
    """Chip band between the steppers: (x, y, w, h)."""
    left = CHROME_L + STEP_W + GAP
    right = w - CHROME_R - STEP_W - GAP
    return (left, STRIP_Y, max(0, right - left), STRIP_H)


def chip_rect(slot, w):
    """Panel rect of one visible chip slot: (x, y, w, h)."""
    ax, ay, aw, ah = chip_area(w)
    cw = (aw - GAP * (VISIBLE - 1)) / float(VISIBLE)
    return (ax + slot * (cw + GAP), ay, cw, ah)


def chip_hit(px, py, w, apps, start):
    """App index for a strip tap, or None on a miss. Never raises."""
    try:
        count = len(apps) if isinstance(apps, (list, tuple)) else 0
        _, slots = page_slots(start, count)
        for pos, index in enumerate(slots):
            x, y, cw, ch = chip_rect(pos, float(w))
            if x <= float(px) < x + cw and y <= float(py) < y + ch:
                return int(index)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return None


def focus_region(w):
    """Touch-strip rect over the app chips: [x, y, w, h]."""
    return strip_rect(w)


def map_region(w, h):
    """Touch-strip rect over the GLANCE map: [x, y, w, h]."""
    return [0, HDR_H, w, max(0, h - HDR_H)]


def click_region(w, h):
    """AIM click rect: the whole panel, [x, y, w, h]."""
    return [0, 0, w, h]


def touch_regions(w=1920, h=1080):
    """touch.json entries for the macbook scope: generate, never hand-compute.

    Order matters (first hit wins): back/tab/focus, then the map (the
    zoom entry: warp + AIM in one gesture), then the full-panel click
    catcher last. No AIM button: the tap is the entry."""
    bw, bh = int(w), int(h)
    entries = [
        {"id": "mac-to-glance",
         "_comment": "back to GLANCE (pins glance params); bottom-left, "
                     "free of the composited badges: harmless no-op "
                     "while glance shows, the way back while AIM shows. "
                     "Rect is macbook_layout.back_rect() -- never hand-edit",
         "rect": [int(v) for v in back_rect(h)],
         "action": {"name": "macbook_mode", "mode": "glance"}},
        {"id": "mac-tab-prev",
         "_comment": "page the app strip one window back (clamps at "
                     "the first page). Rect is "
                     "macbook_layout.stepper_rect('left') -- never "
                     "hand-edit",
         "rect": [int(v) for v in stepper_rect("left", bw)],
         "action": {"name": "talon_tab", "dir": -1}},
        {"id": "mac-tab-next",
         "_comment": "page the app strip one window forward (clamps "
                     "at the last page). Rect is "
                     "macbook_layout.stepper_rect('right') -- never "
                     "hand-edit",
         "rect": [int(v) for v in stepper_rect("right", bw)],
         "action": {"name": "talon_tab", "dir": 1}},
        {"id": "mac-focus",
         "_comment": "tap a header app chip to focus it (view+mode gated, "
                     "coordinate-only). Rect is macbook_layout.focus_region()",
         "rect": focus_region(bw),
         "action": {"name": "talon_focus"}},
        {"id": "mac-map",
         "_comment": "tap the display map to warp the cursor AND enter "
                     "the fullscreen AIM review in the same gesture "
                     "(view+mode gated, coordinate-only). Rect is "
                     "macbook_layout.map_region()",
         "rect": map_region(bw, bh),
         "action": {"name": "macbook_mouse"}},
        {"id": "mac-zoom",
         "_comment": "AIM: tap the fullscreen review to click the reviewed "
                     "point (fresh-capture + cursor-still gates). Catcher "
                     "LAST: header controls win their pixels first. Rect is "
                     "macbook_layout.click_region()",
         "rect": click_region(bw, bh),
         "action": {"name": "macbook_click"}},
    ]
    return entries
