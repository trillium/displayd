"""Pure geometry for the macbook renderer: Quartz displays -> panel rects.

All inputs are plain dicts/tuples in one coordinate system (Quartz:
origin top-left of the menu-bar display, y grows downward -- exactly what
the poller emits). No PIL, no daemon state, so this module is unit-tested
directly and the renderer stays under the file-size budget.
"""

PAD = 8.0  # inset inside the map area, panel px


def union(displays):
    """Bounding box of all display bounds -> (x, y, w, h). None if empty."""
    boxes = []
    for d in displays or []:
        b = d.get("bounds") if isinstance(d, dict) else None
        if not isinstance(b, dict):
            continue
        try:
            boxes.append((float(b["x"]), float(b["y"]),
                          float(b["w"]), float(b["h"])))
        except (KeyError, TypeError, ValueError):
            continue
    boxes = [b for b in boxes if b[2] > 0 and b[3] > 0]
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1 - x0, y1 - y0)


def fit(box, area_w, area_h, pad=PAD):
    """Uniform scale + offsets mapping Quartz box into an area.

    Returns (scale, ox, oy): panel = (q - origin) * scale + offset.
    Aspect preserved; letterboxed. Zero-area inputs -> scale 0.
    """
    try:
        _, _, bw, bh = box
        aw, ah = float(area_w), float(area_h)
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0)
    inner_w, inner_h = aw - 2 * pad, ah - 2 * pad
    if bw <= 0 or bh <= 0 or inner_w <= 0 or inner_h <= 0:
        return (0.0, 0.0, 0.0)
    scale = min(inner_w / bw, inner_h / bh)
    ox = pad + (inner_w - bw * scale) / 2.0 - box[0] * scale
    oy = pad + (inner_h - bh * scale) / 2.0 - box[1] * scale
    return (scale, ox, oy)


def project(x, y, scale, ox, oy):
    """One Quartz point -> panel point."""
    return (x * scale + ox, y * scale + oy)


def rect(bounds, scale, ox, oy, min_px=3.0):
    """Quartz bounds dict -> panel (x0, y0, x1, y1), clamped to min size."""
    try:
        x0, y0 = project(float(bounds["x"]), float(bounds["y"]),
                         scale, ox, oy)
        x1, y1 = project(float(bounds["x"]) + float(bounds["w"]),
                         float(bounds["y"]) + float(bounds["h"]),
                         scale, ox, oy)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if x1 - x0 < min_px:
        x1 = x0 + min_px
    if y1 - y0 < min_px:
        y1 = y0 + min_px
    return (x0, y0, x1, y1)


def label(index, main=False):
    """Short display tag: D1, D2(menu) for the menu-bar screen."""
    text = "D%d" % (index + 1)
    return text + "(menu)" if main else text
