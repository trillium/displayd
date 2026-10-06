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
  a percentage (memory, disk) has a meter, not a rectangle;
- ``pill`` -- a dot and a line of type on the panel's own page surface,
  the overlay a view wears when its label sits over content it did not
  paint (a live frame).

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
DOT = 32          # the status dot's diameter in a list row
DOT_GAP = 24      # the name's inset after that dot
META_GAP = 44     # room kept between a name and the row's right-aligned value
PILL_SIZE = 34    # the status pill's line of type
PILL_PAD = 14     # its inset from that line's box
PILL_GAP = 16     # room between the pill's dot and its label
PILL_DOT = 28     # the dot's diameter in a pill
PILL_RADIUS = 10  # the pill's rounded corner


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


def _dot(img, xy, ink, size=DOT):
    """A filled status dot. Never raises."""
    try:
        from PIL import ImageDraw
        x, y = int(xy[0]), int(xy[1])
        ImageDraw.Draw(img).ellipse([x, y, x + size, y + size], fill=ink)
    except Exception:
        pass
    return img


def list_row(img, screen, xy, name, meta="", ink=None, meta_ink=None,
             dot_ink=None, name_size=ROW_SIZE, meta_size=LINE_SIZE, pad=60):
    """One line of a list: an optional dot, a bold name, a trailing value.

    The horizontal sibling of :func:`row`, for a dashboard that puts many
    entries down the panel instead of one giant number. ``dot_ink`` draws
    a status dot (``shell.health_ink``); the name is fitted to whatever
    room the trailing value leaves, and the value is right-aligned ``pad``
    from the frame's own right edge. Both default to the palette's
    ``muted`` ink, so a caller only colours what genuinely differs.
    Nothing here raises.
    """
    try:
        x, y = int(xy[0]), int(xy[1])
        if dot_ink is not None:
            _dot(img, (x, y + max(0, (int(name_size) - DOT) // 2)), dot_ink)
            x += DOT + DOT_GAP
        text = str(meta if meta is not None else "")
        drawn = width(screen, text, meta_size) if text else 0
        room = max(0, int(screen.W) - int(pad) - x - META_GAP - drawn)
        label(img, screen, (x, y), name, ink=ink, size=name_size, room=room)
        if text:
            ui_text.write(
                img, screen,
                (int(screen.W) - int(pad) - drawn,
                 y + max(0, (int(name_size) - int(meta_size)) // 2)),
                text, theme.rgb("muted") if meta_ink is None else meta_ink,
                meta_size)
    except Exception:
        pass
    return img


def pill(img, screen, xy, text, ink=None, dot_ink=None, size=PILL_SIZE,
         bold=False, pad=PILL_PAD, radius=PILL_RADIUS):
    """A status pill: an optional dot and one line of type on the panel's
    own ``page`` surface, drawn with its top-left corner at ``xy``.

    What a view wears when its label sits over content the view did not
    paint -- a live video frame -- so the words stay legible whatever is
    underneath. ``dot_ink`` is the caller's state (a view's own accent
    while live, ``muted`` while its source is quiet); the label wears
    ``ink-strong``, which is what reads on the page surface. Nothing here
    raises: an unreadable pill is a missing pill, never a blank panel.
    """
    try:
        from PIL import ImageDraw
        line = str(text if text is not None else "")
        size, pad = max(1, int(size)), max(0, int(pad))
        lead = (PILL_DOT + PILL_GAP) if dot_ink is not None else 0
        x, y = int(xy[0]), int(xy[1])
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            [x, y, x + 2 * pad + lead + width(screen, line, size),
             y + size + 2 * pad],
            radius=max(0, int(radius)), fill=theme.rgb("page"))
        if dot_ink is not None:
            _dot(img, (x + pad, y + pad + max(0, (size - PILL_DOT) // 2)),
                 dot_ink, size=PILL_DOT)
        ui_text.write(img, screen, (x + pad + lead, y + pad), line,
                      theme.rgb("ink-strong") if ink is None else ink,
                      size, bold=bold)
    except Exception:
        pass
    return img


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
