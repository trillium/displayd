"""Retro 4x3 button grid for the lnx-server jumbotron.

A chunky arcade-style grid of tappable boxes (default 4 columns x 3 rows =
12 cells), each showing a big number/label or a contain-fit image. Frogger /
Mario / SNES vibes: thick dark outlines, bevel highlight + shadow, dithered
fills, limited arcade palette, bold pixel-ish type readable across a room.

Feed taps while running through POST /feed/retro_grid/tap and the tapped
cell flashes inverted on the next frame (visible in GET /snapshot). The tap
payload may name the cell directly ({"cell": 5}) or carry raw coordinates
({"x": 960, "y": 540} / {"region": "retro-cell-5"}) -- the latter is what
touch.py posts when its confidence_feedback switch points at
renderer "retro_grid", input "tap", so every panel tap flashes the right
cell with zero extra wiring. Per-cell actions (notify/feedback/show) still
go through the existing touch.py ACTION_TABLE allowlist; see
touch-retro-grid.json.example and TOUCH.md ("Retro grid wiring").

Tap wiring quick start (1920x1080 panel)::

    cp touch-retro-grid.json.example touch.json   # then confirm device/range
    python3 touch.py --config touch.json

Recompute the 12 hit rects for another panel size::

    python3 renderers/retro_grid.py --width 800 --height 480
"""

import os
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

NAME = "retro_grid"
DESCRIPTION = ("Retro 4x3 arcade button grid (tap via "
               "POST /feed/retro_grid/tap)")
STATIC = False
ACCENT = "#FFD23F"
PARAMS = {
    "boxes": {"type": "array",
              "help": "12 cells: [{label|text, image, color, text_color, "
                      "text_size}]; default labels 1-12. image = file path "
                      "or http(s) URL, contain-fit; falls back to the label "
                      "when unloadable"},
    "columns": {"type": "integer",
                "help": "grid columns, default 4"},
    "rows": {"type": "integer",
             "help": "grid rows, default 3"},
    "background": {"type": "string",
                   "help": "screen background colour, default deep arcade navy"},
    "gutter": {"type": "integer",
               "help": "gutter px (outer margin + between cells), default "
                       "scales with screen"},
    "border": {"type": "string",
               "help": "cell outline colour, default near-black"},
    "flash_seconds": {"type": "number",
                      "help": "tap-flash hold time, default 1.2"},
}
INPUTS = {
    "tap": {
        "type": "object",
        "help": "flash one cell: {cell (1-based), label, id, region, x, y} "
                "-- x/y form is what touch.py confidence_feedback posts",
        "required": [],
        "properties": {
            "cell": {"type": "integer"},
            "id": {"type": "string"},
            "label": {"type": "string"},
            "region": {"type": "string"},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "x_norm": {"type": "number"},
            "y_norm": {"type": "number"},
            "hit": {"type": "boolean"},
        },
        "buffer": 200,
    },
}

POLL = 0.1  # tap polling; draws happen only on change/expiry

DEFAULT_COLS = 4
DEFAULT_ROWS = 3
DEFAULT_FLASH_SECONDS = 1.2
DEFAULT_BG = (16, 12, 40)
DEFAULT_BORDER = (12, 8, 20)
DEFAULT_INK = (18, 12, 32)  # dark label ink on bright fills

# Limited arcade palette, cycled per cell unless a box sets "color".
PALETTE = (
    (255, 82, 82),    # arcade red
    (255, 210, 63),   # coin yellow
    (80, 220, 120),   # frogger green
    (90, 200, 255),   # mario sky
    (200, 120, 255),  # power-up purple
    (255, 140, 60),   # sunset orange
    (120, 220, 220),  # ice cyan
    (255, 130, 180),  # player-2 pink
    (150, 255, 120),  # lime
    (120, 140, 255),  # indigo
    (255, 240, 150),  # pale coin
    (100, 235, 200),  # mint
)

FLASH_BORDER = (255, 255, 255)


# ---------------------------------------------------------------------------
# Pure geometry + config helpers (unit-testable, no screen needed)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Drawing (one complete PIL image + single screen.present, per contract)
# ---------------------------------------------------------------------------

def _font(screen, size):
    try:
        path = screen.font_path("DejaVuSans-Bold")
    except Exception:
        path = None
    if path:
        try:
            return ImageFont.truetype(path, max(8, int(size)))
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=max(8, int(size)))
    except Exception:
        return ImageFont.load_default()


def _shade(rgb, factor):
    return tuple(max(0, min(255, int(v * factor))) for v in rgb)


def _lighten(rgb, amount):
    return tuple(max(0, min(255, int(v + (255 - v) * amount))) for v in rgb)


def _is_url(path):
    return path.startswith("http://") or path.startswith("https://")


def _load_center(source, timeout=10):
    """Load one cell image (file path or URL); None when unloadable.

    The caller falls back to the text label, so a bad path/URL degrades
    one cell instead of failing the frame.
    """
    if not isinstance(source, str) or not source.strip():
        return None
    source = source.strip()
    try:
        if _is_url(source):
            with urllib.request.urlopen(source, timeout=timeout) as resp:
                return Image.open(resp).convert("RGB")
        return Image.open(os.path.expanduser(source)).convert("RGB")
    except Exception:
        return None


def _contain(img, width, height):
    scale = min(width / max(1, img.width), height / max(1, img.height))
    return img.resize((max(1, int(img.width * scale)),
                       max(1, int(img.height * scale))))


def _fit_font(draw, label, font_maker, max_w, max_h, start):
    size = max(8, int(start))
    while size > 8:
        font = font_maker(size)
        try:
            box = draw.textbbox((0, 0), label, font=font)
            if box[2] - box[0] <= max_w and box[3] - box[1] <= max_h:
                return font
        except Exception:
            return font
        size = int(size * 0.85)
    return font_maker(8)


def _dither(draw, rect, base):
    """Checkerboard darkening over the fill: cheap CRT-dither feel."""
    dark = _shade(base, 0.88)
    x, y, w, h = rect
    for py in range(y + 2, y + h - 2, 2):
        for px in range(x + 2 + (py % 4 == 0) * 1, x + w - 2, 2):
            draw.point((px, py), fill=dark)


def draw_cell(draw, img, screen, box, rect, fill, border, highlight,
              image=None, content_pad=14):
    """Draw one beveled arcade button into the in-progress frame."""
    x, y, w, h = rect
    bw = max(4, min(screen.W, screen.H) // 135)  # chunky outline (~8px@1080p)
    pop = max(3, bw // 2) if highlight else 0
    x0, y0, x1, y1 = x - pop, y - pop, x + w + pop, y + h + pop

    # Drop shadow (skipped when popped: the flash lifts the button).
    if not highlight:
        draw.rectangle([x0 + 6, y0 + 8, x1 + 6, y1 + 8], fill=(0, 0, 0))

    body = _lighten(fill, 0.35) if highlight else fill
    draw.rectangle([x0, y0, x1, y1], fill=body)
    if not highlight:
        _dither(draw, (x0, y0, x1 - x0, y1 - y0), body)

    # Thick outline; flashing cells invert to white-hot.
    draw.rectangle([x0, y0, x1, y1], outline=FLASH_BORDER if highlight else border,
                   width=bw + (2 if highlight else 0))
    # Bevel: light top/left, dark bottom/right (inset inside the outline).
    inset = bw + 2
    hi = _lighten(body, 0.45)
    lo = _shade(body, 0.55)
    draw.line([(x0 + inset, y0 + inset), (x1 - inset, y0 + inset)], fill=hi, width=3)
    draw.line([(x0 + inset, y0 + inset), (x0 + inset, y1 - inset)], fill=hi, width=3)
    draw.line([(x0 + inset, y1 - inset), (x1 - inset, y1 - inset)], fill=lo, width=3)
    draw.line([(x1 - inset, y0 + inset), (x1 - inset, y1 - inset)], fill=lo, width=3)

    # Center content inside the bevel.
    pad = inset + content_pad
    cw, ch = x1 - x0 - 2 * pad, y1 - y0 - 2 * pad
    if cw < 8 or ch < 8:
        return
    if image is not None:
        fitted = _contain(image, cw, ch)
        # Dark plate behind photos so any image reads as one button face.
        plate = [x0 + pad - 4, y0 + pad - 4,
                 x0 + pad + cw + 4, y0 + pad + ch + 4]
        draw.rectangle(plate, fill=(0, 0, 0))
        draw.rectangle(plate, outline=hi if not highlight else FLASH_BORDER, width=2)
        img.paste(fitted, (x0 + pad + (cw - fitted.width) // 2,
                           y0 + pad + (ch - fitted.height) // 2))
        caption = box.get("label", "")
        if caption and ch - fitted.height > 40:
            cap_font = _font(screen, max(12, ch // 10))
            draw.text(((x0 + x1) // 2, y1 - pad - 6), caption,
                      font=cap_font, fill=DEFAULT_INK, anchor="mb")
        return
    label = box.get("label", "")
    if not label:
        return
    try:
        want = int(box.get("text_size") or 0)
    except (TypeError, ValueError):
        want = 0
    start = want or min(cw // max(1, len(label)), ch)
    font = _fit_font(draw, label, lambda s: _font(screen, s),
                     cw, int(ch * 0.72), start)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    ink = screen.color(box.get("text_color"), DEFAULT_INK)
    # Hard offset shadow: the readable-at-distance arcade punch.
    draw.text((cx + 3, cy + 4), label, font=font,
              fill=_shade(ink, 0.4) if not highlight else (80, 60, 0), anchor="mm")
    draw.text((cx, cy), label, font=font,
              fill=(255, 255, 255) if highlight else ink, anchor="mm")


def draw(screen, boxes, geometry, fills, border, highlight, images, bg=None):
    """One complete frame. Pure draw (no I/O): tests call this directly."""
    img = screen.new_image(DEFAULT_BG if bg is None else bg)
    d = ImageDraw.Draw(img)
    for i, rect in enumerate(geometry):
        box = boxes[i] if i < len(boxes) else {"label": str(i + 1)}
        draw_cell(d, img, screen, box, rect, fills[i], border,
                  highlight == i, image=(images or {}).get(i))
    return img


def run(screen, params, stop):
    params = params or {}
    try:
        cols = max(1, min(8, int(params.get("columns") or DEFAULT_COLS)))
    except (TypeError, ValueError):
        cols = DEFAULT_COLS
    try:
        rows = max(1, min(8, int(params.get("rows") or DEFAULT_ROWS)))
    except (TypeError, ValueError):
        rows = DEFAULT_ROWS
    try:
        gutter = params.get("gutter")
        gutter = None if gutter is None else max(0, int(gutter))
    except (TypeError, ValueError):
        gutter = None
    try:
        flash = float(params.get("flash_seconds") or DEFAULT_FLASH_SECONDS)
    except (TypeError, ValueError):
        flash = DEFAULT_FLASH_SECONDS
    flash = max(0.05, min(10.0, flash))
    bg = screen.color(params.get("background"), DEFAULT_BG)
    border = screen.color(params.get("border"), DEFAULT_BORDER)

    geometry = grid_geometry(screen.W, screen.H, cols, rows, gutter)
    boxes = coerce_boxes(params, len(geometry))
    fills = [screen.color(box.get("color"), PALETTE[i % len(PALETTE)])
             for i, box in enumerate(boxes)]

    # Center images load once up front (file or URL, contain-fit); a bad
    # source degrades that cell to its text label, never the frame.
    images = {}
    for i, box in enumerate(boxes):
        if center_kind(box) == "image":
            loaded = _load_center(box["image"])
            if loaded is not None:
                images[i] = loaded

    def frame(highlight):
        return draw(screen, boxes, geometry, fills, border, highlight,
                    images, bg=bg)

    screen.present(frame(None))
    seen_at = {}  # repr(payload) -> first-seen monotonic: the flash clock
    last_key = None
    while not stop.is_set():
        try:
            buffered = screen.get_input("retro_grid", "tap") or []
        except Exception:
            buffered = []
        now = time.monotonic()
        # Stamp only newly arrived taps: re-stamping the whole buffer
        # every pass would freeze the flash on forever, so the flash
        # expiry below would never fire.
        live = set()
        for payload in buffered:
            try:
                key = repr(sorted(payload.items())) if isinstance(payload, dict) else repr(payload)
            except Exception:
                key = repr(payload)
            live.add(key)
            seen_at.setdefault(key, now)
        for key in [k for k in seen_at if k not in live]:
            del seen_at[key]
        by_key = {}
        for payload in buffered:
            try:
                key = repr(sorted(payload.items())) if isinstance(payload, dict) else repr(payload)
            except Exception:
                key = repr(payload)
            by_key[key] = payload
        taps = [(by_key[k], seen_at[k]) for k in live if k in by_key]
        hl = current_highlight(taps, boxes, geometry, screen.W, screen.H,
                               now, flash)
        if hl != last_key:
            last_key = hl
            # One complete frame, one swap: never a partial grid. The
            # hl -> None transition on expiry also lands here, so the
            # flash visibly clears with no new input.
            screen.present(frame(hl))
        stop.wait(POLL)


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Print touch.json region entries matching the retro grid "
                    "layout (default 1920x1080, 4x3). Paste under \"regions\" "
                    "and point confidence_feedback at renderer retro_grid / "
                    "input tap for in-grid flash.")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--cols", type=int, default=DEFAULT_COLS)
    ap.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    ap.add_argument("--gutter", type=int, default=None)
    args = ap.parse_args()
    print(json.dumps(touch_regions(args.width, args.height, args.cols,
                                   args.rows, args.gutter), indent=2))
