"""Region geometry for the touch_confidence renderer.

Parses the regions param (mirroring touch.json) into drawable boxes:
scaled from the params coordinate space, clipped to the screen,
malformed entries skipped -- never raises on bad user input.
"""

# Outline palette cycled per region so adjacent boxes stay distinguishable.
REGION_COLORS = (
    (80, 220, 120),
    (90, 200, 255),
    (255, 200, 90),
    (200, 120, 255),
    (255, 130, 130),
    (120, 220, 220),
)


def format_label(region_id, action):
    """Human text for one box: 'id -> action'. Accepts the touch.json
    action object ({"name": ...}) as well as a plain action-name string."""
    if isinstance(action, dict):
        action = action.get("name")
    return "%s \u2192 %s" % (region_id, action or "no action")


def coerce_regions(params, screen_w, screen_h):
    """Parse the regions param into drawable boxes.

    Returns [(id, (x, y, w, h) in screen px, action)]. Rects are scaled
    from the params width/height coordinate space (default: screen size,
    i.e. no scaling), clipped to the screen, and malformed entries are
    skipped -- never raises on bad user input.
    """
    params = params or {}
    try:
        src_w = int(params.get("width") or screen_w)
        src_h = int(params.get("height") or screen_h)
    except (TypeError, ValueError):
        src_w, src_h = screen_w, screen_h
    if src_w <= 0:
        src_w = screen_w
    if src_h <= 0:
        src_h = screen_h
    raw = params.get("regions")
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for entry in raw:
        try:
            if not isinstance(entry, dict):
                continue
            rid = entry.get("id")
            rect = entry.get("rect")
            if not rid or not isinstance(rid, str):
                continue
            if not isinstance(rect, (list, tuple)) or len(rect) != 4:
                continue
            x, y, w, h = (float(v) for v in rect)
            if w <= 0 or h <= 0:
                continue
            sx = screen_w / float(src_w)
            sy = screen_h / float(src_h)
            ix, iy = int(round(x * sx)), int(round(y * sy))
            iw, ih = max(1, int(round(w * sx))), max(1, int(round(h * sy)))
            ix = max(0, min(screen_w - 1, ix))
            iy = max(0, min(screen_h - 1, iy))
            iw = max(1, min(screen_w - ix, iw))
            ih = max(1, min(screen_h - iy, ih))
            out.append((rid, (ix, iy, iw, ih), entry.get("action")))
        except (TypeError, ValueError, ArithmeticError):
            continue
    return out
