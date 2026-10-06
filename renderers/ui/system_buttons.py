"""The system buttons: the home badge and the sleep badge.

One component, two buttons, composited onto every presented frame through
``Screen.overlay`` next to the playlist progress bar. They are the reason
the component layer exists: before this module they were
``home_chrome.py`` and ``sleep_chrome.py``, two near-identical modules
that each carried a tile fill, a glyph, a strip width, a rect, a region,
a draw function and a suppression list. Nothing owned "a badge".

One module now owns the shared shape; only the glyph differs:

- ``_paint_tile`` draws the dark rounded tile both badges sit on, from
  the palette's ``badge`` fill and ``ink-strong`` outline; ``_house`` /
  ``_moon`` draw the glyph inside it.
- ``home_rect``/``sleep_rect`` and ``home_region``/``sleep_region`` are
  the touch geometry, inside the left/right gesture strips so the badges
  never cover view content (picker tiles start at x=160, the macbook map
  and talon columns at y=250). List the home region FIRST among the
  left-strip entries and the sleep region before ``playlist-next``:
  ``hit_test`` gives earlier entries every overlap, so the badge wins the
  strip's top square while the rest of the strip still works.
- ``audit_exact`` projects exactly the badges DRAWN for a view, in
  composited order, so the touch audit and the renderer cannot disagree.

Suppression (deliberate; see TOUCH.md "Home button"/"Sleep button"):
``home`` is meaningless on the home screens (``unified`` IS home,
``picker``/``options`` re-show), on the reload proof (its QR keeps every
corner pixel), on a transient, and asleep -- so the overlay stops drawing
there rather than pretending a tap lands. ``sleep`` is hidden asleep and
on transients, but stays VISIBLE on ``picker``/``unified``, where
sleeping is meaningful and the corner is tile-free.

Every draw function returns its frame unchanged on ANY failure: a missing
badge beats a missing frame. ``buttons_overlay`` composes through
``ui.base.chain``, so that guarantee holds for the whole button set.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import theme
from ui import base

STRIP = 160  # both badges live inside a gesture strip's width, so they
# never cover view content (the picker grid starts at x=160).
HOME_VIEW = "unified"  # select_view target: the merged home screen.
HOME_SUPPRESSED = ("unified", "picker", "reload", "notice", "sleep")
SLEEP_SUPPRESSED = ("sleep", "reload", "notice")
BUTTONS = ("home", "sleep")


def _side(w, h):
    """Badge side length for a screen, clamped to small panels. Pure."""
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        return STRIP
    if w <= 0 or h <= 0:
        return STRIP
    return max(64, min(STRIP, w // 4, h // 4))


def home_rect(w=1920, h=1080):
    """Home badge square [x, y, rw, rh] at the top-left. Pure."""
    side = _side(w, h)
    return [0, 0, side, side]


def sleep_rect(w=1920, h=1080):
    """Sleep badge square [x, y, rw, rh] at the top-right. Pure."""
    side = _side(w, h)
    try:
        w = int(w)
    except (TypeError, ValueError):
        w = 1920
    if w <= 0:
        w = 1920
    return [w - side, 0, side, side]


def home_region(w=1920, h=1080, view=HOME_VIEW):
    """touch.json region entry for the home badge. Paste FIRST under
    "regions" (earlier entries win overlaps). Fires the existing
    select_view action -- no new action. Pure."""
    return {"id": "home",
            "rect": home_rect(w, h),
            "action": {"name": "select_view", "view": view}}


def sleep_region(w=1920, h=1080):
    """touch.json region entry for the sleep badge. Paste before
    "playlist-next" (earlier entries win overlaps). Fires the existing
    screen_off action -- no new action. Pure."""
    return {"id": "screen-off",
            "rect": sleep_rect(w, h),
            "action": {"name": "screen_off"}}


def _home_visible(view):
    return view not in HOME_SUPPRESSED


def _sleep_visible(view):
    return view not in SLEEP_SUPPRESSED


def _paint_tile(img, rect):
    """The one badge tile both system buttons sit on.

    Returns ``(draw, box, fill, glyph)`` for the glyph to draw into,
    where ``box`` is the tile's inset square. Fill and outline are
    palette roles read at draw time, so the badges cannot drift."""
    from PIL import ImageDraw

    x0, y0, rw, rh = (int(v) for v in rect)
    side = max(16, min(rw, rh))
    pad = max(2, side // 18)
    box = (x0 + pad, y0 + pad, x0 + side - pad, y0 + side - pad)
    fill = theme.rgb("badge")
    glyph = theme.rgb("ink-strong")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(list(box), radius=max(4, side // 8),
                           fill=fill, outline=glyph,
                           width=max(2, side // 48))
    return draw, box, fill, glyph


def _house(draw, box, fill, glyph):
    """House glyph: roof polygon over a body rect, door cut out in the
    badge fill. Proportional, so small panels stay legible."""
    bx0, by0, bx1, by1 = box
    bw = bx1 - bx0
    cx = (bx0 + bx1) // 2
    roof_y = by0 + int(bw * 0.30)
    eave_y = by0 + int(bw * 0.52)
    body_y1 = by1 - int(bw * 0.10)
    inset = int(bw * 0.22)
    eave = int(bw * 0.10)
    draw.polygon([(cx, roof_y),
                  (bx1 - eave, eave_y),
                  (bx0 + eave, eave_y)], fill=glyph)
    draw.rectangle([bx0 + inset, eave_y, bx1 - inset, body_y1], fill=glyph)
    dw = max(3, int(bw * 0.12))
    dh = max(4, int(bw * 0.20))
    draw.rectangle([cx - dw, body_y1 - dh, cx + dw, body_y1], fill=fill)


def _moon(draw, box, fill, glyph):
    """Moon glyph: a full disc with an offset badge-coloured cutout."""
    bx0, by0, bx1, by1 = box
    bw = bx1 - bx0
    cx = (bx0 + bx1) // 2
    cy = (by0 + by1) // 2
    r = bw * 0.30
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=glyph)
    ox, oy, orad = cx + r * 0.55, cy - r * 0.35, r * 0.85
    draw.ellipse([ox - orad, oy - orad, ox + orad, oy + orad], fill=fill)


def draw_home_button(img, rect=None):
    """Paste the home badge onto an already-composed full-screen frame.

    ``rect`` defaults to ``home_rect()`` for the frame's size. Returns
    ``img``; never raises (a missing badge beats a missing frame)."""
    try:
        rect = list(rect) if rect else home_rect(*img.size)
        draw, box, fill, glyph = _paint_tile(img, rect)
        _house(draw, box, fill, glyph)
    except Exception:
        pass
    return img


def draw_sleep_button(img, rect=None):
    """Paste the sleep badge onto an already-composed full-screen frame.

    ``rect`` defaults to ``sleep_rect()`` for the frame's size. Returns
    ``img``; never raises (a missing badge beats a missing frame)."""
    try:
        rect = list(rect) if rect else sleep_rect(*img.size)
        draw, box, fill, glyph = _paint_tile(img, rect)
        _moon(draw, box, fill, glyph)
    except Exception:
        pass
    return img


def audit_exact(view, w=1920, h=1080, which=BUTTONS):
    """touch_audit.py projection: the region entries the DRAWN badges
    correspond to, in composited order, or ``[]`` where this view
    suppresses them. Pure."""
    out = []
    if "home" in which and _home_visible(view):
        region = home_region(w, h)
        out.append({"id": region["id"],
                    "rect": [int(v) for v in region["rect"]],
                    "action": None, "required": True})
    if "sleep" in which and _sleep_visible(view):
        region = sleep_region(w, h)
        out.append({"id": region["id"],
                    "rect": [int(v) for v in region["rect"]],
                    "action": None, "required": True})
    return out


def buttons_overlay(screen, which=BUTTONS):
    """Overlay-fn factory for ``Screen.overlay``.

    Reads ``screen.current_view`` live, so transients and the home screens
    stay chrome-free without any per-renderer code. Composed through
    ``ui.base.chain``: a failing button is skipped rather than blanking
    the panel. Pure factory; never raises."""
    def make(visible, draw):
        def apply(img):
            if visible(getattr(screen, "current_view", None)):
                return draw(img)
            return img
        return apply

    wanted = {"home": (_home_visible, draw_home_button),
              "sleep": (_sleep_visible, draw_sleep_button)}
    layers = []
    for name in BUTTONS:
        visible, draw = wanted[name]
        if name in which:
            layers.append(make(visible, draw))
    return base.chain(*layers)


def system_overlay(screen, base_layer=None, which=BUTTONS):
    """The daemon's single entry point: the system buttons composed over
    an existing overlay layer (the playlist progress bar), through
    ``ui.base.chain``. Pure factory; never raises."""
    return base.chain(base_layer, buttons_overlay(screen, which))


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Print the touch.json region "
                                 "entries for the system buttons (default "
                                 "1920x1080); see the module docstring.")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    args = ap.parse_args()
    print(json.dumps([home_region(args.width, args.height),
                      sleep_region(args.width, args.height)], indent=2))
