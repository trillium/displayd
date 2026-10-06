"""The options name grid: geometry -> one litehtml document.

Split out of renderers/options.py the same way _picker_tiles.py splits
out the picker's tile layer. options.py keeps the view contract (params,
the pinned picks, the failure card); this module owns drawing that
contract -- name divs at the very rects ``grid_geometry`` hands out, plus
the ``options.html`` chrome variables.

Nothing here trusts a caller. View names arrive from a ``views`` param,
which is caller-supplied, so each name is escaped here before it reaches
the document: the markup below is generated in-process from fixed
arithmetic, and the only part of it that came from outside is already
escaped. This is the same rule the picker tile layer follows, and the
reason {{names|raw}} is a renderer-only slot.

Pixel facts this depends on, both verified live:

- an absolutely positioned child offsets from its positioned parent, so a
  name label is placed in cell-local pixels;
- litehtml does not centre a label the way a browser would, so the offset
  is measured here from the real face rather than left to flex centring.
"""

import _html_native
import _html_templates as templates

MAX_NAME_PX = 84
DEFAULT_COLS = 2
# Hard ceiling on the number of name cells. The old Pillow loop drew every
# name at a row height that shrank towards zero, so a caller could hand it
# a thousand names and it cost nothing but an overlap. This geometry
# function allocates a rect per name, so the count is bounded twice: once
# at the parse boundary (options.coerce_views drops the overflow, so the
# header's count stays honest) and once here, because grid_geometry is
# public and must stay total and bounded on any input at all.
MAX_CELLS = 48

# Chrome slots the options surface draws that are not content: the
# gesture-strip labels and the foot. Kept as constants so the template
# contract test and the renderer read the same strings.
HINT_LEFT = "ON"
HINT_RIGHT = "NEXT"
EYEBROW = "DISPLAYD"
FOOTER_RIGHT = "litehtml"


def hex_colour(color):
    """Palette tuple -> CSS colour. Lives in the trust boundary because a
    template that takes colours in a style attribute needs one rule for
    it: the colour came from a screen that already parsed it, never from
    caller text."""
    return templates.hex_colour(color)


def grid_rect(w, h):
    """The area the names live in: between the title band and the footer.

    Full-panel proportions, so the grid is centred on any panel size and
    the footer band below it is never overrun.
    """
    return [int(w * 0.08), int(h * 0.30), int(w * 0.84), int(h * 0.50)]


def cols_for(count):
    """Two columns once there is more than one name; a single name gets
    the whole width. The old draw loop chose the same way."""
    return DEFAULT_COLS if count > 2 else 1


def cell_count(value):
    """A name count that is always an int in [1, MAX_CELLS]. Total: a
    string, None, a float or a huge int all come back bounded, because
    this function's callers feed it whatever a `views` param held."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(MAX_CELLS, count))


def grid_geometry(rect, count, cols=None, gutter=None):
    """Name rects row-major inside `rect`. Pure, bounded and total -- the
    one place a name is placed, so the drawn cell and the geometry a
    caller would read are the same four numbers by construction."""
    count = cell_count(count)
    if cols is None:
        cols = max(1, min(DEFAULT_COLS, count))
    else:
        try:
            cols = max(1, min(DEFAULT_COLS, int(cols)))
        except (TypeError, ValueError):
            cols = 1
    rows = (count + cols - 1) // cols
    rx, ry, rw, rh = (int(v) for v in rect)
    g = max(8, min(rw, rh) // 45) if gutter is None else max(0, int(gutter))
    cw = max(1, (rw - (cols + 1) * g) // cols)
    ch = max(1, (rh - (rows + 1) * g) // rows)
    return [(rx + g + (i % cols) * (cw + g),
             ry + g + (i // cols) * (ch + g), cw, ch)
            for i in range(count)]


def label_px(name, cw, start=MAX_NAME_PX, max_height=None):
    """Biggest name that fits its cell, shrinking in steps. Width and (when
    given) height both constrain it, because a deep name list makes short
    cells. Never raises."""
    try:
        size = max(12, int(start))
    except (TypeError, ValueError):
        size = MAX_NAME_PX
    budget = max(8, int(cw) - 32)
    tall = max(8, int(max_height)) if max_height else None
    while size > 12:
        font = _html_native.ui_font(size, bold=True)
        try:
            ascent, descent = font.getmetrics()
            fits = (font.getlength(str(name)) <= budget and
                    (tall is None or ascent + descent <= tall))
        except Exception:
            fits = True
        if fits:
            break
        size -= 4
    return max(12, size)


def label_box(name, rect, size):
    """Where the name goes inside its cell: centred, exact pixels, relative
    to the cell. Measured, not left to flex centring."""
    _x, _y, cw, ch = (int(v) for v in rect)
    try:
        font = _html_native.ui_font(size, bold=True)
        width = int(font.getlength(str(name)))
        ascent, descent = font.getmetrics()
        height = int(ascent + descent)
    except Exception:
        width, height = int(cw * 0.6), int(size)
    return max(0, (cw - width) // 2), max(0, (ch - height) // 2)


def _cell(name, rect, colour, size):
    """One name div. The name is escaped here, which is what makes this
    markup safe to hand the engine at all."""
    x, y, cw, ch = (int(v) for v in rect)
    cx, cy = label_box(name, rect, size)
    return ('<div class="cell" style="left:%dpx; top:%dpx; width:%dpx; '
            'height:%dpx;"><div class="name" style="left:%dpx; top:%dpx; '
            'font-size:%dpx; color:%s;">%s</div></div>'
            % (x, y, cw, ch, cx, cy, size, hex_colour(colour),
               templates.escape(str(name))))


def name_markup(names, geometry, colour, start=MAX_NAME_PX):
    """The whole name layer for {{names|raw}}, in paint order. Pure."""
    parts = []
    for name, rect in zip(names, geometry):
        _cw, ch = int(rect[2]), int(rect[3])
        size = label_px(name, rect[2], start, max_height=ch - 8)
        parts.append(_cell(name, rect, colour, size))
    return "".join(parts)


def chrome(names, bg, fg, title, instructions, footer):
    """The options template's variables, everything except the name layer.

    ``background``/``color`` are #rrggbb strings built from colours the
    screen already parsed, never the caller's own text, so the template
    can take them in a style attribute without opening a CSS injection.
    The rest of the chrome's colours are design tokens now, drawn by the
    shared partial, so they are not parameters any more. The set is
    pinned against the template by a test.
    """
    count = len(names)
    return {
        "background": hex_colour(bg),
        "color": hex_colour(fg),
        "eyebrow": EYEBROW,
        "title": title,
        "status": "%d VIEW%s" % (count, "" if count == 1 else "S"),
        "subtitle": instructions,
        "hint_left": HINT_LEFT,
        "hint_right": HINT_RIGHT,
        "footer": footer,
        "footer_right": FOOTER_RIGHT,
    }