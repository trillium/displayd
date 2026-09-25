"""Frame drawing for the touch_confidence renderer.

One complete frame per call: accent bar, title, region map band,
diagnostics band. Pure draw (no I/O): tests call this directly.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import ImageDraw, ImageFont

from touch_confidence_regions import REGION_COLORS, format_label
from touch_confidence_taps import _last_line


def font_for(screen, name, size):
    """Named screen font at size, or None when unavailable."""
    try:
        path = screen.font_path(name)
    except Exception:
        return None
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, max(8, int(size)))
    except Exception:
        return None


# Historical name kept so existing imports keep working.
__all__ = ["font_for", "draw"]


def draw(screen, title, instructions, regions, summary, bg, accent=None):
    """One complete frame. Pure draw (no I/O): tests call this directly.

    accent resolves in run(); tests omit it and get the default."""
    if accent is None:
        accent = screen.color("#50DC78", (80, 220, 120))
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)

    # Accent bar across the top: the glanceable bit from across the room.
    draw.rectangle([0, 0, screen.W, max(6, screen.H // 60)], fill=accent)

    title_font = font_for(screen, "DejaVuSans-Bold", screen.H // 11)
    sub_font = font_for(screen, "DejaVuSans", screen.H // 22)
    box_font = font_for(screen, "DejaVuSans-Bold", max(12, screen.H // 30))
    diag_font = font_for(screen, "DejaVuSansMono", max(12, screen.H // 32))
    diag_font = diag_font or font_for(screen, "DejaVuSans", max(12, screen.H // 32))
    plain = title_font or sub_font or box_font or diag_font

    title_y = int(screen.H * 0.12)
    draw.text(
        (screen.W // 2, title_y), title, font=title_font or plain, fill=(255, 255, 255), anchor="mm"
    )
    draw.text(
        (screen.W // 2, title_y + int(screen.H * 0.07)),
        instructions,
        font=sub_font or plain,
        fill=(180, 180, 190),
        anchor="mm",
    )

    # Region map: the middle band. Boxes in config order with labels.
    map_top = int(screen.H * 0.26)
    map_bottom = int(screen.H * 0.66)
    if regions:
        for i, (rid, (x, y, w, h), action) in enumerate(regions):
            color = REGION_COLORS[i % len(REGION_COLORS)]
            # Scale the configured box into the map band vertically so the
            # map always fits on screen regardless of panel geometry.
            frac_top = y / float(max(1, screen.H))
            frac_h = h / float(max(1, screen.H))
            by = map_top + int(frac_top * (map_bottom - map_top))
            bh = max(8, int(frac_h * (map_bottom - map_top)))
            by = max(map_top, min(map_bottom - 8, by))
            bh = max(8, min(map_bottom - by, bh))
            bx, bw = x, w  # horizontal: rects already span the panel width
            draw.rectangle(
                [bx, by, bx + bw - 1, by + bh - 1], outline=color, width=max(2, screen.W // 320)
            )
            label = format_label(rid, action)
            try:
                tw = draw.textlength(label, font=box_font or plain)
                if tw > bw - 12 and tw > 0:
                    label = label[: max(4, int(len(label) * (bw - 12) / tw) - 1)] + "\u2026"
            except Exception:
                pass
            draw.text((bx + 8, by + 6), label, font=box_font or plain, fill=color)
    else:
        draw.text(
            (screen.W // 2, (map_top + map_bottom) // 2),
            "(no regions configured)",
            font=sub_font or plain,
            fill=(140, 140, 150),
            anchor="mm",
        )

    # Diagnostics: the bottom band. Counters + last tap + result/error.
    dy = int(screen.H * 0.70)
    lh = max(16, int(screen.H * 0.055))
    total, hits, misses = (
        summary.get("total", 0),
        summary.get("hits", 0),
        summary.get("misses", 0),
    )
    draw.text(
        (screen.W // 2, dy),
        "taps %d    hits %d    miss %d" % (total, hits, misses),
        font=diag_font or plain,
        fill=(255, 255, 255),
        anchor="mm",
    )
    draw.text(
        (screen.W // 2, dy + lh),
        _last_line(summary),
        font=diag_font or plain,
        fill=(255, 255, 160),
        anchor="mm",
    )
    extra = ""
    last = summary.get("last")
    if last:
        if last.get("error"):
            extra = "error: %s" % last.get("error")
        elif last.get("result"):
            extra = "result: %s" % last.get("result")
    if extra:
        draw.text(
            (screen.W // 2, dy + 2 * lh),
            extra[:90],
            font=diag_font or plain,
            fill=(255, 150, 150),
            anchor="mm",
        )
    per_region = summary.get("per_region") or {}
    if per_region:
        chips = "   ".join("%s: %d" % (rid, per_region[rid]) for rid in sorted(per_region))[:110]
        draw.text(
            (screen.W // 2, dy + (3 if extra else 2) * lh),
            chips,
            font=diag_font or plain,
            fill=(160, 200, 255),
            anchor="mm",
        )
    return img
