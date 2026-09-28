"""Playlist progress-bar geometry and compositing.

Single concept: how the bar is drawn. The bar fills monotonically from
empty to full across the current view's dwell; at switch time it is full,
which reads unambiguously as "this view's time is up".
``direction: "drain"`` reverses it for operators who prefer the countdown
reading. Fill direction follows the edge: ``top``/``bottom`` fill left to
right; ``left``/``right`` fill bottom to top (like a meter rising).

The bar owns a flush edge strip (thin by default, configurable 2..64px,
no inset). Whatever colour wins, the fill is drawn with a contrast border
over a dark track, so it reads on both dark views (beads) and light ones.
"""

from playlist_color import TRACK_COLOR

PLACEMENTS = ("top", "left", "bottom", "right")
DIRECTIONS = ("fill", "drain")


def bar_boxes(width, height, placement, thickness, fraction):
    """Return (track_box, fill_box) in PIL coordinates.

    The track owns a flush strip along ``placement``; the fill grows
    left-to-right on horizontal edges and bottom-to-top on vertical ones.
    ``fraction`` is the filled share in [0, 1] (already direction-applied
    by the caller). Either box may be ``None`` when there is nothing to
    draw (zero thickness or empty fill)."""
    t = max(1, int(thickness))
    fraction = max(0.0, min(1.0, fraction))
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


def _contrast(color):
    """Border colour that reads against both the accent and any page."""
    lum = (0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]) / 255.0
    return (10, 10, 12) if lum > 0.55 else (235, 235, 240)


def draw_bar(img, placement, thickness, fraction, direction, color):
    """Composite the progress bar onto a PIL image, in place. Returns img."""
    from PIL import ImageDraw

    shown = fraction if direction != "drain" else 1.0 - fraction
    track, fill = bar_boxes(img.size[0], img.size[1], placement,
                            thickness, shown)
    draw = ImageDraw.Draw(img)
    if track is not None:
        draw.rectangle(track, fill=TRACK_COLOR)
    if fill is not None:
        draw.rectangle(fill, fill=tuple(color))
        draw.rectangle(fill, outline=_contrast(tuple(color)), width=1)
    return img
