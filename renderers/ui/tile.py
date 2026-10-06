"""The tile component: one definition of a bounded box with a label.

``system_buttons`` folded two badge modules into one badge; this folds the
panel's two tile layers into one tile. ``_picker_tiles.py`` and
``_options_grid.py`` each carried the same four things -- the border
compensation a declared litehtml box needs, the shrink-until-it-fits rule,
the centring measured from the real face, and the palette -> CSS colour
step. They were the same component twice, because nothing owned "a tile".

Both paths ask this module for it: the picker and options surfaces take the
**markup half** (``cell`` / ``layer``) -- the only place a raw slot's tile
divs are built, and so the only reason a caller's label must be escaped
here -- and a Pillow view takes the **drawing half** (``draw``), which
reuses the same ``content_size`` / ``fit_size`` / ``label_box`` rules.

Colours are palette roles, never literals: a tile's fill is the caller's
accent, ink on a bright fill is ``on-accent``, and the offset copy behind a
label is ``label-shade``. The template path takes those from the token
block worn by the shared stylesheet, so the surface owns the look, this
module owns the geometry, and neither half may raise: a missing label beats
a missing frame.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import theme

BORDER = 6       # the frame a document tile draws OUTSIDE its declared box
SHADE = 4        # how far a label's offset copy sits from the label
LABEL_MAX = 84   # largest label size to try, in px
LABEL_MIN = 12   # never shrink below this
LABEL_ROOM = 32  # px of the box a label gives up to its own padding
LABEL_PAD = 8    # px a label keeps clear of the box's own edges
STEP = 4         # shrink step, in px
CENTER = "center"
TOPLEFT = "topleft"
HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def _int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def content_size(rect, border=BORDER):
    """The declared (content) box of a tile whose rect is ``rect``.

    litehtml puts the border OUTSIDE a declared width, so a tile that must
    not spill past the region that taps it declares the region minus the
    frame. Getting this wrong draws every tile 2*BORDER wider than its
    touch target -- silently, because it still looks like a grid.
    """
    try:
        _x, _y, cw, ch = (int(v) for v in rect)
    except (TypeError, ValueError):
        return 1, 1
    edge = 2 * _int(border, 0)
    return max(1, cw - edge), max(1, ch - edge)


def css_colour(value):
    """A palette tuple (or an already-converted ``#rrggbb`` string) as CSS.

    A string is accepted only when it is exactly a hex colour: what reaches
    a style attribute is a colour a screen already parsed, and refusing
    anything else is what keeps caller text out of the CSS.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value if HEX_RE.match(value) else ""
    try:
        import _html_templates as templates
        return templates.hex_colour(value)
    except Exception:
        return ""


def measure_font(font, label):
    """``(width, height)`` of ``label`` in ``font``; ``(0, 0)`` if it
    cannot be measured, which callers read as \"assume it fits\"."""
    try:
        width = int(font.getlength(str(label)))
        ascent, descent = font.getmetrics()
        return width, int(ascent + descent)
    except Exception:
        return 0, 0


def document_font(size):
    """The face a document label is measured with: the document's own, so a
    tile label and the text beside it agree. Imported lazily, so the
    drawing half never needs the template engine."""
    import _html_native
    return _html_native.ui_font(max(LABEL_MIN, _int(size, LABEL_MIN)), bold=True)


def fit_size(label, room, height=None, start=LABEL_MAX, font_for=None):
    """The largest size at or below ``start`` whose label fits the box.

    The one shrink rule both halves use: the markup half measures with the
    document face, a Pillow caller passes its own ``font_for``. Never
    raises -- an unmeasurable font reads as fitting, and the floor is
    ``LABEL_MIN``.
    """
    maker = font_for or document_font
    label = str(label)
    width = max(8, _int(room, 8) - LABEL_ROOM)
    tall = max(8, _int(height)) if height else None
    size = max(LABEL_MIN, _int(start, LABEL_MAX))
    while size > LABEL_MIN:
        font = maker(size)
        wide, high = measure_font(font, label) if font is not None else (0, 0)
        if not wide and not high:
            return size
        if wide <= width and (tall is None or high <= tall):
            return size
        size -= STEP
    return LABEL_MIN


def label_box(label, rect, size, border=BORDER, font_for=None):
    """Where a centred label goes, tile-local, measured from the real face.

    litehtml does not centre a label the way a browser would.
    """
    cw, ch = content_size(rect, border)
    maker = font_for or document_font
    font = maker(size)
    width, height = measure_font(font, label) if font is not None else (0, 0)
    if not width and not height:
        width, height = int(cw * 0.6), _int(size)
    return max(0, (cw - width) // 2), max(0, (ch - height) // 2)


def cell(rect, label, border=BORDER, fill=None, ink=None, shadow=False,
         box_cls="tile", label_cls="lab", start=LABEL_MAX):
    """One tile div for a document's raw slot.

    ``label`` is the only caller-supplied string that reaches a document,
    and it is escaped HERE -- that is what makes the markup safe to hand the
    engine. Colours left out (the frame, the offset copy) come from the
    shared stylesheet's tokens instead.
    """
    import _html_templates as templates
    x, y, _rw, _rh = (int(v) for v in rect)
    cw, ch = content_size(rect, border)
    size = fit_size(label, cw, height=ch - LABEL_PAD, start=start)
    cx, cy = label_box(label, rect, size, border=border)
    text = templates.escape(str(label))
    background = css_colour(fill)
    colour = css_colour(ink)
    shade = ('<div class="shade" style="left:%dpx; top:%dpx; '
             'font-size:%dpx;">%s</div>'
             % (cx + SHADE, cy + SHADE, size, text)) if shadow else ""
    return ('<div class="%s" style="left:%dpx; top:%dpx; width:%dpx; '
            'height:%dpx;%s">%s<div class="%s" style="left:%dpx; '
            'top:%dpx; font-size:%dpx;%s">%s</div></div>'
            % (box_cls, x, y, cw, ch,
               " background:%s;" % background if background else "",
               shade, label_cls, cx, cy, size,
               " color:%s;" % colour if colour else "", text))


def layer(labels, geometry, fills=None, border=BORDER, ink=None,
          shadow=False, box_cls="tile", label_cls="lab", start=LABEL_MAX):
    """The whole tile layer for a template's raw slot, in paint order.

    Pure. ``fills`` cycles; ``None`` means no fill (the page shows through).
    """
    fills = list(fills) if fills else [None]
    return "".join(
        cell(rect, label, border=border, fill=fills[i % len(fills)],
             ink=ink, shadow=shadow, box_cls=box_cls, label_cls=label_cls,
             start=start)
        for i, (label, rect) in enumerate(zip(labels, geometry)))


def _pad(pad):
    """A ``(x, y)`` inset from an int or a pair. Never raises."""
    if isinstance(pad, (list, tuple)):
        values = [_int(v) for v in pad][:2]
        values += [8, 6][len(values):]
        return values[0], values[1]
    return _int(pad, 8), _int(pad, 8)


def _default_font(size):
    """The face of last resort: a missing font is a small label, not none."""
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=max(LABEL_MIN, _int(size, LABEL_MIN)))
    except Exception:
        return ImageFont.load_default()


def _truncate(draw, text, font, room):
    """``text`` cut to ``room`` px with an ellipsis; unchanged if it fits."""
    try:
        width = draw.textlength(text, font=font)
        if width <= room or width <= 0:
            return text
        keep = max(4, int(len(text) * room / width) - 1)
        return text[:keep] + "\u2026"
    except Exception:
        return text


def draw(img, rect, label="", ink=None, font=None, fill=None, outline=None,
         width=1, place=CENTER, pad=(8, 6), border=BORDER):
    """The drawing half: the box ``rect`` (x, y, w, h), then its label.

    Returns the frame unchanged on any failure, so a tile that cannot draw
    is a missing label and never a blank panel. ``place`` is ``center`` (a
    grid tile) or ``topleft`` (a labelled region, truncated to fit).
    """
    try:
        from PIL import ImageDraw
        x, y, w, h = (int(v) for v in rect)
        box = (x, y, w, h)
        draw_ = ImageDraw.Draw(img)
        if fill is not None or outline is not None:
            draw_.rectangle([x, y, x + w - 1, y + h - 1], fill=fill,
                            outline=outline, width=max(1, int(width)))
        text = "" if label is None else str(label)
        if not text:
            return img
        if font is None:
            font = _default_font(LABEL_MIN)
        ink = theme.rgb("on-accent") if ink is None else ink
        cw, ch = content_size(box, border)
        if str(place).lower() in (TOPLEFT, "top-left"):
            px, py = _pad(pad)
            room = max(8, cw - 2 * px)
            draw_.text((x + px, y + py), _truncate(draw_, text, font, room),
                       font=font, fill=ink)
            return img
        size = _int(getattr(font, "size", None), LABEL_MIN)
        off_x, off_y = label_box(text, box, size, border=border,
                                 font_for=lambda _size: font)
        draw_.text((x + off_x, y + off_y), text, font=font, fill=ink)
        return img
    except Exception:
        return img
