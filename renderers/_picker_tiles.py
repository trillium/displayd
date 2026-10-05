"""The picker tile layer: geometry -> one litehtml document.

Split out of renderers/picker.py because the picker's own job is the view
contract (params, offered views, touch geometry), while this is the drawing
of that geometry: tile divs at the very rects ``picker.grid_geometry``
hands the touch regions, plus the layout.html chrome variables.

Nothing here trusts a caller. The only variable that can be odd is the
view name, and it is escaped here before it reaches the document, so the
markup this module builds is the same kind of value the html renderer's
trust boundary always escapes -- it is trusted only in the sense that it
is generated in-process from a fixed palette and fixed arithmetic.

Pixel facts this depends on (both verified live, both easy to get wrong):

- litehtml puts border and padding OUTSIDE a declared width, so a tile's
  declared size is its touch rect minus the frame. Without the
  compensation every tile is 12px wider than the region that taps it.
- an absolutely positioned child offsets from its positioned parent, so a
  tile label is placed in tile-local pixels.
"""

import _html_native
import _html_templates as templates

MAX_LABEL_PX = 84
BORDER_PX = 6          # template .tile border, on every side
SHADE_OFFSET = 4       # the drop shadow under each label
TITLE = "PICK A VIEW"


def hex_colour(color):
    """Palette tuple -> CSS colour. The table is trusted and constant."""
    return "#%02x%02x%02x" % tuple(int(v) for v in color[:3])


def content_size(rect):
    """CSS content box for a tile of this rect (border is outside it)."""
    _x, _y, cw, ch = (int(v) for v in rect)
    edge = 2 * BORDER_PX
    return max(1, cw - edge), max(1, ch - edge)


def label_px(name, cw, start=MAX_LABEL_PX, max_height=None):
    """Biggest label that fits the tile, shrinking in steps: the whole-PIL
    behaviour of the old draw loop, measured with the face the document
    text uses. Width and (when given) height both constrain it, because a
    deep tile list makes short tiles. Never raises."""
    try:
        size = max(12, int(start))
    except (TypeError, ValueError):
        size = MAX_LABEL_PX
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
    """Where the tile label goes inside the tile: centred, exact pixels,
    relative to the tile. Measured here rather than left to flex centring,
    which litehtml does not do the way a browser would."""
    cw, ch = content_size(rect)
    try:
        font = _html_native.ui_font(size, bold=True)
        width = int(font.getlength(str(name)))
        ascent, descent = font.getmetrics()
        height = int(ascent + descent)
    except Exception:
        width, height = int(cw * 0.6), int(size)
    return max(0, (cw - width) // 2), max(0, (ch - height) // 2)


def _tile_markup(name, rect, fill, size):
    """One tile div. The name is escaped here, which is what makes this
    markup safe to hand the engine at all."""
    x, y, _cw, _ch = (int(v) for v in rect)
    cw, ch = content_size(rect)
    cx, cy = label_box(name, rect, size)
    label = templates.escape(str(name))
    return (
        '<div class="tile" style="left:%dpx; top:%dpx; width:%dpx; '
        'height:%dpx; background:%s;">'
        '<div class="shade" style="left:%dpx; top:%dpx; font-size:%dpx;">%s</div>'
        '<div class="lab" style="left:%dpx; top:%dpx; font-size:%dpx;">%s</div>'
        '</div>'
        % (x, y, cw, ch, hex_colour(fill), cx + SHADE_OFFSET,
           cy + SHADE_OFFSET, size, label, cx, cy, size, label))


def tile_markup(views, geometry, fills, start=MAX_LABEL_PX):
    """The whole tile layer for {{tiles|raw}}, in paint order. Pure."""
    fills = list(fills) or [(255, 255, 255)]
    parts = []
    for i, (name, rect) in enumerate(zip(views, geometry)):
        _cw, ch = content_size(rect)
        size = label_px(name, rect[2], start, max_height=ch - 8)
        parts.append(_tile_markup(name, rect, fills[i % len(fills)], size))
    return "".join(parts)


def chrome(screen, rect, views, bg, fg, title=TITLE):
    """The picker template's variables, filled from the geometry.

    An empty value collapses its slot, which is how a tight custom rect
    drops the bands it has no room for -- the old draw() skipped them the
    same way, so a custom rect still renders the same content.

    ``background`` and ``color`` are #rrggbb strings built from the
    colours the screen already parsed, never the caller's own text, so
    the template can take them in a style attribute without opening a CSS
    injection. The set is pinned against the template by a test.
    """
    rx, ry, rw, rh = (int(v) for v in rect)
    w, h = screen.W, screen.H
    roomy_top = ry >= 28
    roomy_bar = h - (ry + rh) >= 120
    return {
        "background": hex_colour(bg),
        "color": hex_colour(fg),
        "eyebrow": "DISPLAYD" if roomy_top else "",
        "title": (title or TITLE) if roomy_top else "",
        "status": ("%d VIEW%s" % (len(views), "" if len(views) == 1 else "S")
                   if roomy_top else ""),
        "subtitle": "tap a tile to switch the panel view" if roomy_top else "",
        "hint_left": "ON" if rx >= 80 else "",
        "hint_right": "NEXT" if w - (rx + rw) >= 80 else "",
        "footer": "picker" if roomy_bar else "",
        "footer_right": "litehtml" if roomy_bar else "",
    }