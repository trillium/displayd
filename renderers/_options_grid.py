"""The options name grid: geometry -> the rects a name is drawn at.

Split out of renderers/options.py the same way ``_picker_tiles.py`` splits
out the picker's chrome. options.py keeps the view contract (params, the
pinned picks, the failure card); this module owns the grid arithmetic and
the ``options.html`` chrome variables.

The DRAWING of a name is not here any more: a name cell is a tile with no
frame, so it comes from ``renderers/ui/tile.py`` -- the same component the
picker's filled tiles use, which is why the two surfaces can no longer
disagree about how a label is fitted or centred.

``grid_geometry`` is load-bearing twice over: the renderer places a name at
exactly these rects, so the drawn cell and the geometry a caller would read
are the same four numbers by construction.

Pixel facts this depends on, both verified live:

- an absolutely positioned child offsets from its positioned parent, so a
  name label is placed in cell-local pixels (``ui.tile`` does that now);
- litehtml does not centre a label the way a browser would, which is why
  the offset is measured from the real face rather than left to flex.
"""

import _html_templates as templates
from ui import grid as ui_grid

DEFAULT_COLS = 2
# Hard ceiling on the number of name cells. The old Pillow loop drew every
# name at a row height that shrank towards zero, so a caller could hand it
# a thousand names and it cost nothing but an overlap. This geometry
# function allocates a rect per name, so the count is bounded twice: once
# at the parse boundary (options.coerce_views drops the overflow, so the
# header's count stays honest) and once here, because grid_geometry is
# public and must stay total and bounded on any input at all. The
# component places no more boxes than this either (`ui.grid.TILE_CAP`).
MAX_CELLS = ui_grid.TILE_CAP

# Chrome slots the options surface draws that are not content: the
# gesture-strip labels and the foot. Kept as constants so the template
# contract test and the renderer read the same strings.
HINT_LEFT = "ON"
HINT_RIGHT = "NEXT"
EYEBROW = "DISPLAYD"
FOOTER_RIGHT = "litehtml"


def hex_colour(color):
    """Palette tuple -> CSS colour (the trust boundary's one rule)."""
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
    """Name rects row-major inside `rect`. Pure, bounded and total --
    the arithmetic is the tile component's (`ui.grid.grid`), so the picker's
    grid and this one can no longer disagree about where a box goes; this
    function keeps the surface's own name-count policy (`cell_count`) and
    its column rule (`cols_for`), and must stay total on any input, which
    is why the count is bounded here and not only at the parse boundary."""
    count = cell_count(count)
    if cols is None:
        cols = cols_for(count)
    return ui_grid.grid(rect, count, cols, gutter=gutter)


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
