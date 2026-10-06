"""The text component: one font, one measurement, one fitting rule.

Every Pillow view needs the same few things -- a face at a size, the width
of a string in that face, a string cut to a width, the largest size at
which a line still fits, and a paragraph broken to a column -- and each
view wrote them again before this module owned "a line of type".

Three guarantees, all load-bearing for the panel. ``face`` is never None:

- a host without ``/usr/share/fonts`` still prints a line, in Pillow's
  default bitmap face; a missing font must not become a missing line.
- nothing here raises: a component that cannot draw returns the frame
  unchanged, so one broken piece never blanks the panel -- including a
  measure on a font object Pillow cannot introspect.
- ``fit_size`` shrinks rather than cuts, never below its own floor.

Sizes are the caller's: a component names its own scale (``shell.HEAD_SIZE``,
``stat.LABEL_SIZE``) so the panel's type scale is legible as data rather
than magic numbers scattered through drawing code. The multi-line
composition of these primitives is ``ui.paragraph`` (a fitted block in a
rect), and ``retro_grid_draw`` still carries its own shrink-to-fit copy;
it folds in when that view moves.
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


def fit_size(screen, text, size, room, floor=FLOOR, step=2, bold=False,
             family=None):
    """The largest size <= ``size`` at which ``text`` still fits ``room`` px.

    The one rule behind "make this line fit its column" -- a hero number
    too wide for the space beside it shrinks rather than being cut, and it
    never shrinks below ``floor``. ``room`` of 0 or less, or an
    unmeasurable string, keeps the declared size (``width`` reads 0 as
    unknown, exactly as it does everywhere else). Never raises.
    """
    out = str(text if text is not None else "")
    try:
        want = max(FLOOR, int(size))
        room = int(room)
        step = max(1, int(step))
    except (TypeError, ValueError):
        return FLOOR
    floor = max(FLOOR, min(int(floor or FLOOR), want))
    if room <= 0 or not out:
        return want
    try:
        drawn = want
        while drawn > floor:
            if width(screen, out, drawn, bold=bold, family=family) <= room:
                return drawn
            drawn = max(floor, drawn - step)
        return drawn
    except Exception:
        return want


def write(img, screen, xy, text, ink, size, bold=False, room=None,
          family=None, anchor=None):
    """One line of type at ``xy``, fitted to ``room`` px first.

    ``xy`` is the text box's top-left corner unless ``anchor`` states
    otherwise (PIL's own anchor letters: ``mm`` centred on ``xy``, ``ma``
    centred on ``xy`` by its top edge), which is what a view that centres
    a caption on the panel needs. Returns the frame unchanged on any
    failure -- the component layer's obligation -- so a line that cannot
    be drawn is a missing line and never a blank panel.
    """
    try:
        from PIL import ImageDraw
        line = fit(screen, text, size, bold=bold, room=room, family=family)
        if not line:
            return img
        kwargs = {} if anchor is None else {"anchor": str(anchor)}
        ImageDraw.Draw(img).text(
            (int(xy[0]), int(xy[1])), line,
            font=face(screen, size, bold=bold, family=family), fill=ink,
            **kwargs)
    except Exception:
        pass
    return img


def wrap(screen, text, size, room, rows=2, bold=False, family=None,
         assumed=52):
    """``text`` broken to ``room`` px, at most ``rows`` lines.

    The panel had four copies of this rule -- ``activity``, ``beads_common``,
    ``macbook_strip`` and the failure card each measured an average
    character width and handed a column count to :mod:`textwrap` -- so
    "how a paragraph is broken to a width" had no owner. This owns it.

    ``assumed`` is the column count used when the face cannot be measured,
    which is why the result is never empty: a block that cannot be
    measured still says something. ``rows=None`` keeps every line.
    """
    import textwrap
    out = []
    for para in str(text if text is not None else "").splitlines() or [""]:
        columns = assumed
        try:
            average = width(screen, "0123456789", size, bold=bold,
                            family=family) / 10.0
            if average > 0 and room and int(room) > 0:
                columns = max(12, int(int(room) / average))
        except Exception:
            columns = assumed
        out.extend(textwrap.wrap(para, columns) or [""])
    if rows is not None:
        out = out[:max(1, int(rows))]
    return out


def line(img, ink, xy, xy2, width_px=2):
    """A hairline between two points, horizontal or vertical. Never raises.

    Here rather than in each view because a divider is part of the same
    vocabulary as the type sitting on it, and the rule under a band and the
    rule between two rows must be one colour.
    """
    try:
        from PIL import ImageDraw
        ImageDraw.Draw(img).line([tuple(xy), tuple(xy2)], fill=ink,
                                 width=max(1, int(width_px)))
    except Exception:
        pass
    return img
