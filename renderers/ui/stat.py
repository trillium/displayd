"""The stat component: a label plus a value line.

Every list view on this panel is the same shape -- a small muted label, a
value under it, a supporting line, and sometimes a meter for a fraction
of a whole -- and two views hand-drew that shape: ``resources_draw`` and
``services_draw`` each carried their own type sizes (44/170/130/40/36),
their own truncation and their own set of palette tuple constants
(``C_TEXT``, ``C_DIM``, ``C_LINE``, ``C_OK``, ``C_WARN``, ``C_BAD``,
``C_UP``, ``C_DOWN``, ``C_FAILED`` -- sixteen literals across the two
files). Nothing owned "a label plus a value line", so the palette was
re-authored per view.

What this owns:

- ``label`` / ``value`` / ``body`` -- the three type steps of a stat, each
  one palette role, each fitted to its room;
- ``row`` -- the component itself: a label with its value under it, the
  one definition of "a label plus a value line";
- ``meter`` -- a fraction of a whole, as an outlined bar. A view that has
  a percentage (memory, disk) has a meter, not a rectangle.

Nothing here raises, and every colour is a theme role: a stat that cannot
draw is a missing line, never a blank panel.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import theme
from ui import text as ui_text

LABEL_SIZE = 44   # the small muted label above a value
VALUE_SIZE = 170  # the headline value
COUNT_SIZE = 130  # a numeric tally: three of them across the panel
ROW_SIZE = 40     # a list row
BODY_SIZE = 36    # supporting text under a value
LINE_SIZE = 30    # the smallest supporting line
GAP = 30          # label -> value
BAR_INSET = 4     # a meter's fill inset from its own outline
BAR_WIDTH = 2     # the meter's outline width


def label(img, screen, xy, text, ink=None, size=LABEL_SIZE, room=None):
    """The stat's label: small, muted, bold."""
    return ui_text.write(img, screen, xy, text,
                         theme.rgb("muted") if ink is None else ink,
                         size, bold=True, room=room)


def value(img, screen, xy, text, ink=None, size=VALUE_SIZE, room=None):
    """The stat's value: the biggest type on the panel."""
    return ui_text.write(img, screen, xy, text,
                         theme.rgb("ink") if ink is None else ink,
                         size, bold=True, room=room)


def body(img, screen, xy, text, ink=None, size=BODY_SIZE, bold=False,
         room=None):
    """A supporting line: body copy under a value, or a list row."""
    return ui_text.write(img, screen, xy, text,
                         theme.rgb("muted") if ink is None else ink,
                         size, bold=bold, room=room)


def row(img, screen, xy, name, value_text, value_ink=None, name_ink=None,
        gap=GAP, name_size=LABEL_SIZE, value_size=VALUE_SIZE, room=None):
    """A label with its value under it -- the component, once.

    Returns the frame, like every other entry point in the layer. The
    caller keeps the geometry: this draws exactly two lines at ``xy`` and
    ``xy + gap``, so a row's place on the panel stays readable as data.
    """
    x, y = int(xy[0]), int(xy[1])
    label(img, screen, (x, y), name, ink=name_ink, size=name_size,
          room=room)
    return value(img, screen, (x, y + int(gap)), value_text, ink=value_ink,
                 size=value_size, room=room)


def width(screen, text, size=BODY_SIZE, bold=False):
    """Text width in px, so a view can flow several entries onto one line
    without owning a font or a Draw handle of its own. 0 means unknown."""
    return ui_text.width(screen, text, size, bold=bold)


def meter(img, screen, rect, frac, ink=None):
    """A fraction of a whole: an outlined bar, filled to ``frac``.

    The outline is the palette's ``edge`` and the fill is the caller's
    ink (defaulting to the accent). ``frac`` is clamped, so a source that
    reports 103% or -1 draws a full or an empty bar rather than spilling
    outside its own rect -- and a zero or unreadable fraction draws the
    outline alone, which is the honest reading of "no data".
    """
    try:
        from PIL import ImageDraw
        x, y, w, h = (int(v) for v in rect)
        w = max(2, w)
        h = max(4, h)
        draw = ImageDraw.Draw(img)
        draw.rectangle([x, y, x + w - 1, y + h - 1],
                       outline=theme.rgb("edge"), width=BAR_WIDTH)
        try:
            part = max(0.0, min(1.0, float(frac)))
        except (TypeError, ValueError):
            part = 0.0
        filled = (w - 2 * BAR_INSET) * part
        if filled > 2:
            draw.rectangle([x + BAR_INSET, y + BAR_INSET,
                            x + BAR_INSET + int(filled), y + h - BAR_INSET],
                           fill=theme.rgb("accent") if ink is None else ink)
    except Exception:
        pass
    return img


def rule(img, screen, y, pad=60):
    """The divider between two rows, in the palette's edge role (the band
    rule under a title is the stronger ``rule`` role -- shell owns that)."""
    try:
        return ui_text.line(img, theme.rgb("edge"), (pad, int(y)),
                            (int(screen.W) - pad, int(y)), 2)
    except Exception:
        return img
