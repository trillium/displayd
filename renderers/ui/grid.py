"""The grid of tiles: how many columns, and where each box lands.

``tile`` owns one box; this owns a grid of them. The arithmetic lived
twice -- ``picker.grid_geometry`` and ``_options_grid.grid_geometry``
carried the same gutter rule, the same ``cw``/``ch`` division and the same
row-major walk, differing only in how many columns they allowed and how
far they bounded a count. Here it is once, and a surface keeps only its
own policy as data (the picker's three-column cap, the options grid's
count-based rule), so the two can no longer disagree about where a box
goes.

It also owns the answer to "this region is 288px wide, so how many
columns?" A fixed three columns is a 69-pixel tile in an application
band -- a label no wider than the gutter around it. ``columns`` reads the
count off the region's shape instead: the column count that makes a box
closest to square, so a narrow band is ONE application column and a wide
grid takes the surface's own cap. An explicit ``cols`` still wins,
because a caller forcing a shape is stating a fact this cannot derive.

Everything here is pure and total: a garbage rect, count, column count or
gutter has an answer, never a raise. The count is capped, because a
caller handing this function ``10 ** 9`` used to mean one rect allocated
per name, which is an out-of-memory, not a validation error.
"""

import math

TILE_CAP = 48    # most boxes any grid places (the options grid's own cap)
COL_CAP = 6      # most columns any grid takes
GUTTER_MIN = 8   # smallest gap between boxes, in px
GUTTER_DIV = 45  # the default gap is the region's short side over this


def _int(value, fallback=0):
    """``int(value)`` or ``fallback``; never raises."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _count(value):
    """A box count that is always an int in ``[1, TILE_CAP]``."""
    return max(1, min(TILE_CAP, _int(value, 1)))


def _rect(rect):
    """``(x, y, w, h)`` with positive sizes; ``(0, 0, 1, 1)`` on garbage."""
    try:
        x, y, w, h = (int(v) for v in list(rect)[:4])
    except (TypeError, ValueError):
        return 0, 0, 1, 1
    return x, y, max(1, w), max(1, h)


def columns(rect, count, cap=COL_CAP, cols=None):
    """How many columns a grid of ``count`` boxes takes in ``rect``.

    An explicit ``cols`` wins, clamped to ``cap`` and to the count (a grid
    never has an empty column). Absent one, the count is the one that makes
    a box closest to square in this region -- read off the shape, not
    fixed, which is what turns a narrow application band into a single
    usable column. Total: any argument at all has an int answer.
    """
    count = _count(count)
    _x, _y, rw, rh = _rect(rect)
    chosen = None if cols is None else _int(cols, None)
    if chosen is None:
        try:
            chosen = int(round(math.sqrt(count * rw / rh)))
        except Exception:
            chosen = 1
    return max(1, min(_int(cap, COL_CAP), count, chosen))


def grid(rect, count, cols, gutter=None):
    """Box rects, row-major inside ``rect``.

    ``gutter`` is px; absent, it is the region's short side over
    ``GUTTER_DIV`` with a ``GUTTER_MIN`` floor, so the grid breathes on a
    big panel and still has a gap on a small one. A box is never smaller
    than one pixel, so an over-packed grid overlaps rather than vanishing.
    """
    count = _count(count)
    cols = max(1, min(_int(cols, 1), count))
    rows = (count + cols - 1) // cols
    rx, ry, rw, rh = _rect(rect)
    gap = (max(GUTTER_MIN, min(rw, rh) // GUTTER_DIV)
           if gutter is None else max(0, _int(gutter, 0)))
    cw = max(1, (rw - (cols + 1) * gap) // cols)
    ch = max(1, (rh - (rows + 1) * gap) // rows)
    return [(rx + gap + (i % cols) * (cw + gap),
             ry + gap + (i // cols) * (ch + gap), cw, ch)
            for i in range(count)]
