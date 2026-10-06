"""Frame drawing for the touch_confidence renderer.

One complete frame per call: accent bar, title, region map band,
diagnostics band. Pure draw (no I/O): tests call this directly.

Every line of type here is the panel component's ``block`` and the map
boxes are the tile component's ``draw``, so this view owns its geometry
and its words and nothing else. It used to carry a private font loader
(``font_for``), six colour literals and the same centred-text call four
times.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import theme
from touch_confidence_regions import REGION_COLORS, format_label
from touch_confidence_taps import _last_line
from ui import panel as ui_panel
from ui import text as ui_text
from ui import tile as ui_tile

__all__ = ["draw"]


def _line(img, screen, x, y, text, ink, size, bold=False, family=None):
    """One centred line of type -- the card component's own block."""
    return ui_panel.block(img, screen, text, ink=theme.rgb(ink), size=size,
                          centre=(int(x), int(y)), bold=bold, family=family)


def draw(screen, title, instructions, regions, summary, bg, accent=None):
    """One complete frame. Pure draw (no I/O): tests call this directly.

    accent resolves in run(); tests omit it and get the palette's slot."""
    if accent is None:
        accent = theme.accent_rgb("touch_confidence")
    img = screen.new_image(bg)
    w, h = int(screen.W), int(screen.H)

    # Accent bar across the top: the glanceable bit from across the room.
    ui_panel.bar(img, screen, accent, height=max(6, h // 60))

    title_y = int(h * 0.12)
    _line(img, screen, w // 2, title_y, title, "ink-strong", h // 11,
          bold=True)
    _line(img, screen, w // 2, title_y + int(h * 0.07), instructions,
          "muted-soft", h // 22)

    # Region map: the middle band. Boxes in config order with labels.
    map_top = int(h * 0.26)
    map_bottom = int(h * 0.66)
    if regions:
        box_font = ui_text.face(screen, max(12, h // 30), bold=True)
        for i, (rid, (x, y, rw, rh), action) in enumerate(regions):
            color = REGION_COLORS[i % len(REGION_COLORS)]
            # Scale the configured box into the map band vertically so the
            # map always fits on screen regardless of panel geometry.
            frac_top = y / float(max(1, h))
            frac_h = rh / float(max(1, h))
            by = map_top + int(frac_top * (map_bottom - map_top))
            bh = max(8, int(frac_h * (map_bottom - map_top)))
            by = max(map_top, min(map_bottom - 8, by))
            bh = max(8, min(map_bottom - by, bh))
            bx, bw = x, rw  # horizontal: rects already span the panel width
            # A region box is a tile: a bounded box with a label. Same
            # component (and so the same label fit and truncation) as the
            # picker's tiles, drawn unfilled because the map stays a map.
            ui_tile.draw(img, [bx, by, bw, bh], format_label(rid, action),
                         ink=color, font=box_font, outline=color,
                         width=max(2, w // 320),
                         place=ui_tile.TOPLEFT, pad=(8, 6), border=0)
    else:
        _line(img, screen, w // 2, (map_top + map_bottom) // 2,
              "(no regions configured)", "faint", h // 22)

    # Diagnostics: the bottom band. Counters + last tap + result/error.
    dy = int(h * 0.70)
    lh = max(16, int(h * 0.055))
    mono = "DejaVuSansMono"
    diag_size = max(12, h // 32)
    total, hits, misses = (
        summary.get("total", 0),
        summary.get("hits", 0),
        summary.get("misses", 0),
    )
    _line(img, screen, w // 2, dy,
          "taps %d    hits %d    miss %d" % (total, hits, misses),
          "ink-strong", diag_size, family=mono)
    _line(img, screen, w // 2, dy + lh, _last_line(summary), "attention",
          diag_size, family=mono)
    extra = ""
    last = summary.get("last")
    if last:
        if last.get("error"):
            extra = "error: %s" % last.get("error")
        elif last.get("result"):
            extra = "result: %s" % last.get("result")
    if extra:
        _line(img, screen, w // 2, dy + 2 * lh, extra[:90], "alert",
              diag_size, family=mono)
    per_region = summary.get("per_region") or {}
    if per_region:
        chips = "   ".join("%s: %d" % (rid, per_region[rid])
                           for rid in sorted(per_region))[:110]
        _line(img, screen, w // 2, dy + (3 if extra else 2) * lh, chips,
              "accent", diag_size, family=mono)
    return img
