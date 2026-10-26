"""One line of type on a draw handle a view already holds.

``ui.text.write`` owns "a line of type" for views that hand over an image
and a screen. Several views (the macbook header, the beads rows, the retro
grid) hold a ``draw`` and a font they built themselves and call ``.text``
directly; this is their path onto the same declaration: the line is cut to
its ``room`` with an ellipsis -- always visible, never silent -- and the
box it was meant to stay inside is declared to ``ui.textbox`` so the text
check can compare what landed against it.

Nothing here raises: a line that cannot be measured is drawn as given.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui import textbox

ELLIPSIS = "…"


def clip(draw, text, font, room):
    """``text`` ending in an ellipsis once it is wider than ``room`` px."""
    out = str(text if text is not None else "")
    try:
        if not room or int(room) <= 0 or draw.textlength(out, font=font) <= room:
            return out
        while out and draw.textlength(out + ELLIPSIS, font=font) > room:
            out = out[:-1]
        return out.rstrip() + ELLIPSIS if out else ""
    except Exception:
        return str(text if text is not None else "")


def put(draw, xy, text, font, fill, room=None, box=None, cut=True,
        label=None, anchor=None):
    """Draw ``text`` at ``xy`` within ``room`` px; returns what was drawn."""
    line = clip(draw, text, font, room)
    if not line:
        return ""
    try:
        if textbox.active():
            textbox.note(text if text is not None else "",
                         box or textbox.derive(xy, room, anchor), cut, label)
        kwargs = {} if anchor is None else {"anchor": str(anchor)}
        draw.text(tuple(xy), line, font=font, fill=fill, **kwargs)
    except Exception:
        pass
    return line
