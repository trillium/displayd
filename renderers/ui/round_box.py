"""Rounded corners for the surfaces Pillow draws itself (card, strip, tile).

The html path rounds through ``border-radius`` and the C ABI; the shapes in
``ui.panel`` and ``ui.tile`` never meet litehtml, so they round here, with
the same anti-aliased coverage mask (``ui.radius``) and no engine work.

ONE radius, applied to all four corners. ``ui.radius`` can hold eight
independent values, but nothing drawn from Pillow needs them, so this API
does not pretend to take them.

HOW THE MASK IS BUILT: ``radius.shape`` draws each corner arc at 4x and
box-filters it back down, so the corner carries intermediate coverage
levels instead of Pillow's aliased staircase. The straight runs sit on
integer pixel boundaries and stay solid.

WHY IT IS SAFE TO CACHE: the mask is a pure function of ``(w, h, radius)``
-- never of colour or canvas -- so a hit cannot be stale; a different size
or radius misses and builds its own. ``radius`` bounds the cache.

Nothing here raises: a shape that cannot round is the square it always was.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageChops, ImageOps

from ui import radius as ui_radius

RADIUS = 24     # the corner radius a card, strip or tile wears, in px


def _mask(w, h, radius):
    """The cached rounded coverage mask: one radius on all four corners."""
    r = max(0, int(radius))
    return ui_radius.shape(w, h, (r,) * 8)


def fill(img, rect, radius, ink):
    """``ink`` over ``rect`` (x, y, w, h), its corners cut to ``radius``."""
    try:
        x, y, w, h = (int(v) for v in rect)
        img.paste(ink, (x, y, x + w, y + h), _mask(w, h, radius))
    except Exception:
        pass
    return img


def ring(img, rect, radius, ink, width=1):
    """A ``width`` px outline hugging the rounded edge of ``rect``."""
    try:
        x, y, w, h = (int(v) for v in rect)
        k = max(1, int(width))
        inner = Image.new("L", (w, h), 0)
        if w > 2 * k and h > 2 * k:
            inner.paste(_mask(w - 2 * k, h - 2 * k, max(0, int(radius) - k)),
                        (k, k))
        img.paste(ink, (x, y, x + w, y + h),
                  ImageChops.subtract(_mask(w, h, radius), inner))
    except Exception:
        pass
    return img


def cut(img, rect, radius, backdrop):
    """Repaint what lies outside the rounded ``rect`` in ``backdrop``.

    Used on a card whose fill is already down: only the four corner wedges
    change, so the fill, the edge and every inset stay where they were.
    """
    try:
        x, y, w, h = (int(v) for v in rect)
        img.paste(backdrop, (x, y, x + w, y + h),
                  ImageOps.invert(_mask(w, h, radius)))
    except Exception:
        pass
    return img
