"""The progress component: a fraction of a whole across a flush edge strip.

The playlist's progress bar was the last component the layer had not
taken. ``playlist_bar`` owned the geometry and the compositing, and
``playlist`` carried a second, byte-identical copy of the same three
functions -- a copy that silently *shadowed* the import naming
``playlist_bar`` as the owner, so the module the docstrings pointed at
was dead code at the call site. Both copies were also the only place the
bar's track colour and its computed border lived, so the same progress
bar had two owners and no palette.

One definition now. What it owns:

- ``boxes`` -- where the track strip and the fill land, for the four edge
  placements, in PIL coordinates;
- ``shown`` -- the direction rule (``drain`` counts a dwell down);
- ``contrast`` -- the 1px border that reads on a bright fill over both a
  dark page and a light one;
- ``draw`` -- composite both onto a frame, in place, and return it.

The colours are the palette's: the strip is ``theme.rgb("track")``, and
the border is a role rather than a literal -- ``on-accent`` when the fill
is bright, ``ink-strong`` when it is dark.

``draw`` never raises: an unknown placement, an un-parsable thickness or
a colour Pillow refuses is a bar that is not drawn, never a blank panel.
``boxes`` is the pure geometry function, so it keeps the one loud path --
an unknown placement is a ``ValueError`` -- because a caller asking for
geometry directly is stating where it wants the bar.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import theme

PLACEMENTS = ("top", "left", "bottom", "right")
DIRECTIONS = ("fill", "drain")
EDGE = 1  # smallest thickness worth drawing, in px


def shown(fraction, direction):
    """The drawn share: ``drain`` reverses the reading, ``fill`` does not.

    Total: a garbage fraction clamps into [0, 1], an unknown direction
    reads as ``fill``.
    """
    value = _number(fraction, 0.0)
    value = max(0.0, min(1.0, value))
    return 1.0 - value if direction == "drain" else value


def _number(value, fallback):
    """``float(value)`` or ``fallback``; never raises."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def boxes(width, height, placement, thickness, fraction):
    """``(track_box, fill_box)`` in PIL coordinates, either possibly None.

    The track owns a flush strip along ``placement``; the fill grows
    left-to-right on horizontal edges and bottom-to-top on vertical ones
    (like a meter rising). ``fraction`` is the already-direction-applied
    filled share in [0, 1]. ``None`` means there is nothing to draw: a
    zero-thickness strip, or an empty fill (so an empty bar is a track,
    never a missing bar). Total on a garbage thickness or fraction; an
    unknown ``placement`` is the one loud path.
    """
    t = max(EDGE, int(_number(thickness, EDGE)))
    fraction = max(0.0, min(1.0, _number(fraction, 0.0)))
    if placement == "top":
        track = (0, 0, width, t)
        w = int(width * fraction)
        fill = (0, 0, w, t) if w > 0 else None
    elif placement == "bottom":
        track = (0, height - t, width, height)
        w = int(width * fraction)
        fill = (0, height - t, w, height) if w > 0 else None
    elif placement == "left":
        track = (0, 0, t, height)
        h = int(height * fraction)
        fill = (0, height - h, t, height) if h > 0 else None
    elif placement == "right":
        track = (width - t, 0, width, height)
        h = int(height * fraction)
        fill = (width - t, height - h, width, height) if h > 0 else None
    else:
        raise ValueError("placement must be one of %s" % "/".join(PLACEMENTS))
    return track, fill


def contrast(color):
    """The border role that reads against ``color`` and any page.

    A bright fill takes the dark role, a dark fill the bright one. Never
    raises: a colour this cannot measure takes the bright border, which is
    what a white default fill wants anyway.
    """
    try:
        lum = (0.299 * color[0] + 0.587 * color[1]
               + 0.114 * color[2]) / 255.0
    except Exception:
        lum = 1.0
    return theme.rgb("on-accent") if lum > 0.55 else theme.rgb("ink-strong")


def draw(img, placement, thickness, fraction, direction, color):
    """Composite the bar onto ``img``, in place. Returns the frame.

    The component-layer obligation: on any failure the frame comes back
    exactly as it went in, so a bar that cannot be drawn is a missing
    bar and never a blank panel. The colour is validated before the
    first pixel, so a frame Pillow would refuse half-way through the
    strip (a track without its fill) is not a state this can leave.
    """
    try:
        from PIL import ImageDraw

        parts = [int(component) for component in color]
        if len(parts) != 3 or any(c < 0 or c > 255 for c in parts):
            return img
        track, fill = boxes(img.size[0], img.size[1], placement, thickness,
                            shown(fraction, direction))
        handle = ImageDraw.Draw(img)
        if track is not None:
            handle.rectangle(track, fill=theme.rgb("track"))
        if fill is not None:
            fill_color = tuple(parts)
            handle.rectangle(fill, fill=fill_color)
            handle.rectangle(fill, outline=contrast(fill_color), width=1)
    except Exception:
        pass
    return img
