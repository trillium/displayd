"""Geometry for the html painter: clip boxes, colours, and the corner-radius
masks. Pure functions over litehtml's resolved boxes; no state.
"""

import math

from PIL import Image, ImageChops

from ui import radius as ui_radius
from _html_abi import SIDE_BOTTOM, SIDE_LEFT, SIDE_RIGHT, SIDE_TOP, _Radii


def _clip_box(clip):
    """A clipping rect as PIL's inclusive-exclusive box, or None."""
    if clip is None or clip.w <= 0 or clip.h <= 0:
        return None
    x0, y0 = int(clip.x), int(clip.y)
    return (x0, y0, x0 + int(clip.w) + 1, y0 + int(clip.h) + 1)


def _visible(box, clip):
    """Intersect a draw box with the clip; None when fully clipped away.

    Rounded to whole pixels here so every painter downstream works on ints:
    PIL rejects float sizes, and litehtml hands out fractional geometry.
    """
    x0, y0, x1, y1 = box
    cb = _clip_box(clip)
    if cb:
        x0, y0 = max(x0, cb[0]), max(y0, cb[1])
        x1, y1 = min(x1, cb[2]), min(y1, cb[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (int(x0), int(y0), int(x1), int(y1))


def _rgba(color, size):
    """web_color (0..255) to an RGBA tuple PIL will blend."""
    if color.a >= 255:
        return (color.r, color.g, color.b, 255)
    return (color.r, color.g, color.b, color.a)


def _radii(raw):
    """A ctypes lhtml_radii into a rounding key, or None for "square".

    None is the answer for every degenerate case, and is the reason an
    absent or malformed radius is safe: a NULL pointer from the container,
    an all-zero struct, a negative value, and a NaN litehtml failed to
    resolve all collapse to the same square path this renderer took before
    radii existed. It never raises and never draws half a shape.
    """
    if raw is None or not raw:
        return None
    # ctypes hands a NULL lhtml_radii* over as None and a real one as an
    # LP__Radii, so the struct is one dereference in.
    values = [float(getattr(raw.contents, name)) for name, _ in _Radii._fields_]
    if not all(math.isfinite(v) and v >= 0 for v in values):
        return None
    return tuple(values) if any(values) else None


def _border_ring(box, sides, radii):
    """The border ring of `box` as a coverage mask, and the origin it sits at.

    CSS draws a border as the outer rounded edge minus the inner one, and
    the inner edge is the outer box deflated by each side's own width with
    the radii deflated by the adjacent side widths (a left/top pair for the
    top-left corner, and so on), clamped at zero. Intersecting each side
    rectangle with this ring is what puts the corner pixels on the sides
    that reach them.

    The origin is returned because the mask is cut up per side, and a crop
    has to know which pixel the mask's own (0, 0) is.
    """
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return None, (0, 0)
    outer = ui_radius.shape(w, h, radii)
    left = sides[SIDE_LEFT].width
    top = sides[SIDE_TOP].width
    right = sides[SIDE_RIGHT].width
    bottom = sides[SIDE_BOTTOM].width
    ix0, iy0 = int(x0 + left), int(y0 + top)
    ix1, iy1 = int(x1 - right), int(y1 - bottom)
    if ix1 <= ix0 or iy1 <= iy0:
        return outer, (x0, y0)  # borders meet: the whole box is border
    inner = ui_radius.shape(ix1 - ix0, iy1 - iy0, (
        max(0.0, radii[0] - left), max(0.0, radii[1] - top),
        max(0.0, radii[2] - right), max(0.0, radii[3] - top),
        max(0.0, radii[4] - right), max(0.0, radii[5] - bottom),
        max(0.0, radii[6] - left), max(0.0, radii[7] - bottom)))
    hole = Image.new("L", (w, h), 0)
    hole.paste(inner, (ix0 - x0, iy0 - y0))
    return ImageChops.subtract(outer, hole), (x0, y0)


def _cut(mask, area, origin):
    """`mask` cropped to `area`, in area-local coordinates.

    Both are already clipped to the same region, which is what makes the
    crop exact: the mask and the pixels it covers are the same size, so a
    paste can never be handed a shape it does not line up with.
    """
    return mask.crop((area[0] - origin[0], area[1] - origin[1],
                      area[2] - origin[0], area[3] - origin[1]))


def _shape_for(box, clip, radii):
    """The coverage mask for one fill, or None to take the square path.

    Resolved here, once, from what the container actually sent -- so the
    painter only ever sees "a whole mask" or "no mask", never a half-built
    one. The key includes the visible (clipped) size, because the mask is
    drawn at that size; a radius is only ever honoured where there are
    pixels for it.
    """
    key = _radii(radii)
    if key is None:
        return None
    area = _visible(box, clip)
    if not area:
        return None
    return ui_radius.shape(area[2] - area[0], area[3] - area[1], key)
