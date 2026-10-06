"""The text component: one font, one measurement, one fitting rule.

Every Pillow view needs the same three things -- a face at a size, the
width of a string in that face, and a string cut to a width -- and every
Pillow view wrote them again: ``_font``, ``_font_or_default``, ``_fit``
and a truncation loop appear in ``resources_draw``, ``services_draw``,
``row_draw``, ``stream`` and ``text``. They were the same three rules with
four copies of the code between them, because nothing owned "a line of
type".

Two guarantees, both load-bearing for the panel:

- **``face`` is never None.** A host without ``/usr/share/fonts`` still
  puts words on the panel -- Pillow's default bitmap face, small but
  readable -- because a missing font must not become a missing line.
- **nothing here raises.** A component that cannot draw returns the frame
  unchanged, which is how one broken piece never blanks the panel; that
  has to survive a measure on a font object Pillow cannot introspect.

Sizes are the caller's: a component names its own scale (``shell.HEAD_SIZE``,
``stat.LABEL_SIZE``) so the type scale of the panel is legible as data
rather than scattered as magic numbers through the drawing code.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAMILY = "DejaVuSans"        # the regular face
BOLD_FAMILY = "DejaVuSans-Bold"
FLOOR = 8                    # smallest size worth asking a face for
FIT_FLOOR = 4                # never trim a string below this many chars


def font(screen, size, bold=False, family=None):
    """The truetype face at ``size``, or None when it cannot be loaded.

    ``screen.font_path`` is the one place that knows where fonts live, so a
    test double can hand back None and the panel still renders.
    """
    try:
        from PIL import ImageFont
        name = family or (BOLD_FAMILY if bold else FAMILY)
        path = screen.font_path(name)
        if not path:
            return None
        return ImageFont.truetype(path, max(FLOOR, int(size)))
    except Exception:
        return None


def face(screen, size, bold=False, family=None):
    """``font`` with the never-None guarantee: a small face, not no line."""
    found = font(screen, size, bold=bold, family=family)
    if found is not None:
        return found
    try:
        from PIL import ImageFont
        try:
            return ImageFont.load_default(size=max(FLOOR, int(size)))
        except Exception:
            return ImageFont.load_default()
    except Exception:
        return None


_MEASURE = None


def _measurer():
    """A reusable Draw handle for measuring: textlength needs a draw, and
    measuring must not need an image (or a caller-owned Draw)."""
    global _MEASURE
    if _MEASURE is None:
        from PIL import Image, ImageDraw
        _MEASURE = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    return _MEASURE


def width(screen, text, size, bold=False, family=None):
    """``text``'s width in px in that face; 0 when it cannot be measured.

    0 means "unknown", and every caller here reads it as "assume it fits".
    """
    try:
        handle = _measurer()
        return int(handle.textlength(str(text if text is not None else ""),
                                     font=face(screen, size, bold=bold,
                                               family=family)))
    except Exception:
        return 0


def fit(screen, text, size, bold=False, room=0, family=None):
    """``text`` cut to ``room`` px. Pure-ish; never raises.

    The rule the views already used, kept exactly: a string that fits is
    untouched, and one that does not is trimmed two characters at a time
    (so the cut lands inside a word rather than at an arbitrary column).
    ``room`` of 0 or less means "no limit".
    """
    out = str(text if text is not None else "")
    try:
        if not room or int(room) <= 0 or not out:
            return out
        handle = _measurer()
        font_ = face(screen, size, bold=bold, family=family)
        while len(out) > FIT_FLOOR and handle.textlength(out, font=font_) > room:
            out = out[:-2]
        return out
    except Exception:
        return str(text if text is not None else "")


def write(img, screen, xy, text, ink, size, bold=False, room=None,
          family=None):
    """One line of type at ``xy``, fitted to ``room`` px first.

    Returns the frame unchanged on any failure -- the component layer's
    obligation -- so a line that cannot be drawn is a missing line and
    never a blank panel.
    """
    try:
        from PIL import ImageDraw
        line = fit(screen, text, size, bold=bold, room=room, family=family)
        if not line:
            return img
        ImageDraw.Draw(img).text(
            (int(xy[0]), int(xy[1])), line,
            font=face(screen, size, bold=bold, family=family), fill=ink)
    except Exception:
        pass
    return img


def line(img, ink, xy, xy2, width_px=2):
    """A horizontal/vertical hairline between two points. Never raises.

    Here rather than in each view because a divider is part of the same
    vocabulary as the type sitting on it, and because the rule under a
    band and the rule between two rows must be the same colour.
    """
    try:
        from PIL import ImageDraw
        ImageDraw.Draw(img).line([tuple(xy), tuple(xy2)], fill=ink,
                                 width=max(1, int(width_px)))
    except Exception:
        pass
    return img
