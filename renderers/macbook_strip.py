"""The GLANCE app strip: chip labels, chip painting, slide composite.

Single concept: everything that makes one chip of the header's tab strip --
fitting a chip label to the chip width, painting a chip (filled for the
Mac's live focus, outlined otherwise), and compositing the two windows of a
page-turn frame onto the transparent layer the header pastes. Geometry comes
from macbook_layout, colours from macbook_glance_color.
"""

import os
import sys
import textwrap

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import macbook_layout as lay
import talon_apps as ta
from macbook_glance_color import (C_FOCUS, C_FOCUS_BG, C_LINE, C_ROW)


def _chip_label(draw, name, font, max_w):
    """Chip label fitted to the chip width: cleaned, truncated."""
    text = ta.clean(name)
    if not text:
        return "unknown"
    try:
        if draw.textlength(text, font=font) <= max_w:
            return text
        while len(text) > 1:
            text = text[:-1]
            if draw.textlength(text + "\u2026", font=font) <= max_w:
                return text + "\u2026"
        return "\u2026"
    except Exception:
        return ta.label(name)


def _chip(d, x, y, cw, ch, text, focused, font):
    """One app chip at absolute x: filled when it is the Mac's live
    focus (real feed state), outlined otherwise. No other emphasis --
    a highlight that selects nothing is decoration, not a control."""
    if focused:
        d.rounded_rectangle([x, y, x + cw, y + ch],
                            radius=10, fill=C_FOCUS_BG)
    else:
        d.rounded_rectangle([x, y, x + cw, y + ch],
                            radius=10, outline=C_LINE, width=2)
    mark = "*" if focused else " "
    d.text((x + 14, y + 8), mark + text, font=font,
           fill=C_FOCUS if focused else C_ROW)


def _strip_layer(aw, ah, cw, old_slots, new_slots, labels, focused_set,
                 font, offset_old, offset_new):
    """Chip band for one slide frame: the old window sliding out and
    the new window sliding in, composited on a transparent layer so no
    chip can bleed over the steppers. Offsets are in layer px. The
    layer is 2px wider than the band: chip_rect floats can round a
    rightmost outline 1px past the band edge, and clipping it would
    leave the landed frame 1px off the steady one."""
    layer = Image.new("RGBA", (int(aw) + 2, int(ah) + 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for pos, index in enumerate(old_slots):
        x = pos * (cw + lay.GAP) + offset_old
        if x + cw < 0 or x > aw:
            continue
        _chip(d, x, 0, cw, ah, labels.get(index, ""),
              index in focused_set, font)
    for pos, index in enumerate(new_slots):
        x = pos * (cw + lay.GAP) + offset_new
        if x + cw < 0 or x > aw:
            continue
        _chip(d, x, 0, cw, ah, labels.get(index, ""),
              index in focused_set, font)
    return layer


def _wrap(draw, text, font, max_w, rows=1, width=90):
    if font is not None:
        try:
            avg = draw.textlength("0123456789", font=font) / 10.0
            width = max(12, int(max_w / max(avg, 1)))
        except Exception:
            pass
    out = []
    for para in str(text or "").splitlines() or [""]:
        out.extend(textwrap.wrap(para, width) or [""])
    return out[:rows]
