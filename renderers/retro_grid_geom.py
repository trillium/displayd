"""Retro-grid geometry + tap resolution (pure, unit-testable, no screen).

Split out of renderers/retro_grid.py to stay within the project's
250-line budget. No PIL, no I/O: grid rects, box normalisation,
tap-payload resolution, flash timing, and the touch.json region
helper. renderers/retro_grid.py re-exports the names tests use.
"""

import time

DEFAULT_COLS = 4
DEFAULT_ROWS = 3


def default_gutter(w, h):
    """Gutter px that stays chunky from 480p to 1080p panels."""
    return max(8, min(int(w), int(h)) // 45)


def grid_geometry(w, h, cols=DEFAULT_COLS, rows=DEFAULT_ROWS, gutter=None):
    """Cell rects [(x, y, cw, ch)] row-major with even gutters.

    The gutter doubles as the outer margin, so the grid fills the screen
    with uniform spacing on all sides. Pure: no screen needed, which is
    also what the touch.json recompute helper below uses.
    """
    cols = max(1, int(cols or DEFAULT_COLS))
    rows = max(1, int(rows or DEFAULT_ROWS))
    g = default_gutter(w, h) if gutter is None else max(0, int(gutter))
    cw = (int(w) - (cols + 1) * g) // cols
    ch = (int(h) - (rows + 1) * g) // rows
    cw, ch = max(1, cw), max(1, ch)
    rects = []
    for r in range(rows):
        for c in range(cols):
            rects.append((g + c * (cw + g), g + r * (ch + g), cw, ch))
    return rects


def coerce_boxes(params, count):
    """Normalise the boxes param to exactly `count` cell dicts.

    Each cell: {"label", "image", "color", "text_color", "text_size"}.
    Missing cells default to labels "1".."N"; extra boxes are ignored.
    Never raises on bad user input.
    """
    params = params or {}
    raw = params.get("boxes")
    if not isinstance(raw, (list, tuple)):
        raw = []
    boxes = []
    for i in range(count):
        entry = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
        label = entry.get("label", entry.get("text", str(i + 1)))
        if label is None:
            label = str(i + 1)
        boxes.append({
            "label": str(label),
            "image": entry.get("image"),
            "color": entry.get("color"),
            "text_color": entry.get("text_color"),
            "text_size": entry.get("text_size"),
        })
    return boxes


def center_kind(box):
    """Which center content a cell wants: 'image' or 'text'.

    An image wins only when the box names one; unloadable images fall
    back to text at draw time (see _load_center).
    """
    img = (box or {}).get("image")
    if isinstance(img, str) and img.strip():
        return "image"
    return "text"


def cell_at_point(x, y, geometry):
    """Index of the cell containing display point (x, y), or None."""
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        return None
    for i, (cx, cy, cw, ch) in enumerate(geometry):
        if cx <= fx < cx + cw and cy <= fy < cy + ch:
            return i
    return None


def _region_index(token, boxes):
    """Index for an id/label/region token: 'retro-cell-N', 'N', or label."""
    if not isinstance(token, str) or not token:
        return None
    text = token.strip()
    for prefix in ("retro-cell-", "retro_cell_", "cell-", "cell_"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    try:
        n = int(text)
    except ValueError:
        n = None
    if n is not None:
        if 1 <= n <= len(boxes):
            return n - 1
        if 0 <= n < len(boxes):
            return n
        return None
    lowered = token.strip().lower()
    for i, box in enumerate(boxes):
        if box.get("label", "").strip().lower() == lowered:
            return i
    return None


def resolve_tap(payload, boxes, geometry, w, h):
    """Map one tap payload to a cell index, or None (dead zone / garbage).

    Accepts direct ({cell} / {label} / {id}) and coordinate
    ({x, y} / {x_norm, y_norm} / {region}) forms. Pure.
    """
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("cell"), int) and not isinstance(payload.get("cell"), bool):
        n = payload["cell"]
        if 1 <= n <= len(boxes):
            return n - 1
        if 0 <= n < len(boxes):
            return n
        return None
    for key in ("id", "label", "region"):
        idx = _region_index(payload.get(key), boxes)
        if idx is not None:
            return idx
    x, y = payload.get("x"), payload.get("y")
    if (not isinstance(x, (int, float)) or isinstance(x, bool)
            or not isinstance(y, (int, float)) or isinstance(y, bool)):
        xn, yn = payload.get("x_norm"), payload.get("y_norm")
        if (isinstance(xn, (int, float)) and isinstance(yn, (int, float))):
            x, y = xn * w, yn * h
        else:
            return None
    return cell_at_point(x, y, geometry)


def current_highlight(taps, boxes, geometry, w, h, now, flash_seconds):
    """Newest tap still inside its flash window -> cell index, else None.

    `taps` is oldest-first [(payload, arrived_monotonic)]; a payload "ts"
    (unix seconds) overrides the arrival time so replays behave. Pure.
    """
    best = None  # (time, index)
    for payload, arrived in taps:
        idx = resolve_tap(payload, boxes, geometry, w, h)
        if idx is None:
            continue
        ts = payload.get("ts") if isinstance(payload, dict) else None
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            t = float(ts)
            base = now - (time.time() - t)  # unix ts -> monotonic frame
        else:
            t = None
        moment = base if t is not None else arrived
        if best is None or moment >= best[0]:
            best = (moment, idx)
    if best is None:
        return None
    if now - best[0] <= max(0.05, float(flash_seconds)):
        return best[1]
    return None


def touch_regions(w=1920, h=1080, cols=DEFAULT_COLS, rows=DEFAULT_ROWS,
                  gutter=None, boxes=None):
    """touch.json region entries for this grid: one rect per cell.

    Each region drives the existing allowlisted `notify` action (transient
    "CELL N" notice, then automatic return) -- no new generic action.
    In-grid flash comes from touch.py's confidence_feedback switch pointed
    at renderer "retro_grid", input "tap" (see touch-retro-grid.json.example).
    """
    geometry = grid_geometry(w, h, cols, rows, gutter)
    cells = boxes if boxes is not None else coerce_boxes({}, len(geometry))
    regions = []
    for i, (x, y, cw, ch) in enumerate(geometry):
        label = cells[i].get("label", str(i + 1)) if i < len(cells) else str(i + 1)
        regions.append({
            "id": "retro-cell-%d" % (i + 1),
            "rect": [x, y, cw, ch],
            "action": {"name": "notify", "title": "CELL %s" % label,
                       "body": "retro grid cell %d tapped" % (i + 1),
                       "severity": "info"},
        })
    return regions

