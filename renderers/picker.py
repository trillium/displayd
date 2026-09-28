"""Tappable view picker for the lnx-server jumbotron.

A STATIC grid of labelled view tiles: each tile names one selectable view
and the matching touch region fires the ``select_view`` named action, so a
panel tap reroutes the displayed view with no phone in hand. Hit rects for
``touch.json`` come from ``picker_regions()`` below -- generate, never
hand-compute::

    python3 renderers/picker.py --width 1920 --height 1080 \\
        --views clock,chat,row,stream,activity,options

A sibling of ``options.py``, not an extension of it, by choice: options is
the never-trapping screen that only NAMES the picks and the return path,
and the tap-anywhere fallback depends on that contract. The picker is the
screen that SELECTS. Either can target the other without trapping: a dead
tap while the picker shows simply re-shows it, and ``options`` stays one
tile away.

Isolated by design: reads its own ``views``/``rect`` params only, never the
policy clock, playlist, feeds, or any other view. The offered list is
bounded (at most 12 tiles) and must be derived from the daemon's advertised
set (``GET /renderers``) or an explicit configured list -- never a probe.
Membership is enforced where the set lives: the daemon's ``show()``
rejects unknown renderers with the current view undisturbed.
"""

from PIL import ImageDraw, ImageFont

NAME = "picker"
DESCRIPTION = ("Tappable view picker: tile grid rerouting the panel "
               "via the select_view touch action")
STATIC = True
ACCENT = "#7BDFF2"
PARAMS = {
    "views": {"type": "array",
              "help": "view names to offer as tiles, default the pinned "
                      "six; malformed entries skipped, capped at 12"},
    "rect": {"type": "array",
             "help": "grid area [x, y, w, h] display px, default the "
                     "screen minus side strips and a bottom button bar"},
    "title": {"type": "string",
              "help": "header text, default PICK A VIEW"},
    "background": {"type": "string",
                   "help": "background colour, default near-black"},
    "color": {"type": "string",
              "help": "primary text colour, default white"},
}

DEFAULT_VIEWS = ("clock", "chat", "row", "stream", "activity", "options")
MAX_VIEWS = 12
DEFAULT_COLS = 3

PALETTE = (
    (90, 200, 255),
    (80, 220, 120),
    (255, 210, 63),
    (200, 120, 255),
    (255, 140, 60),
    (255, 130, 180),
)
INK = (18, 12, 32)


def default_rect(w, h):
    """Grid area: full screen minus side gesture strips and a bottom
    button bar (dead taps there route back here via tap_options)."""
    mx, top, bar = w // 12, h // 27, h // 6
    return [mx, top, w - 2 * mx, h - top - bar]


def coerce_views(params):
    """Parse the views param into at most MAX_VIEWS clean names.

    Missing/empty falls back to DEFAULT_VIEWS; non-string, blank, and
    slash-containing entries are skipped; extras beyond the cap are
    dropped so the tile list stays bounded. Never raises."""
    try:
        raw = (params or {}).get("views")
    except AttributeError:
        return list(DEFAULT_VIEWS)
    if raw is None:
        return list(DEFAULT_VIEWS)
    if not isinstance(raw, (list, tuple)):
        return list(DEFAULT_VIEWS)
    cleaned = [v.strip() for v in raw
               if isinstance(v, str) and v.strip() and "/" not in v]
    return (cleaned or list(DEFAULT_VIEWS))[:MAX_VIEWS]


def coerce_rect(params, w, h):
    """Parse the rect param into [x, y, rw, rh], clamped to the screen.

    Anything unusable falls back to default_rect(). Never raises."""
    try:
        raw = (params or {}).get("rect")
    except AttributeError:
        raw = None
    if (isinstance(raw, (list, tuple)) and len(raw) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    for v in raw)):
        x, y, rw, rh = (int(v) for v in raw)
        if rw <= 0 or rh <= 0:
            return default_rect(w, h)
        x = max(0, min(w - 1, x))
        y = max(0, min(h - 1, y))
        return [x, y, min(rw, w - x), min(rh, h - y)]
    return default_rect(w, h)


def grid_geometry(rect, count, cols=DEFAULT_COLS, gutter=None):
    """Tile rects [(x, y, cw, ch)] row-major inside `rect`.

    Columns cap at DEFAULT_COLS (fewer tiles -> fewer columns, one row
    when that fits); the gutter doubles as the inner spacing. Pure."""
    count = max(1, int(count))
    cols = max(1, min(DEFAULT_COLS, count))
    rows = (count + cols - 1) // cols
    rx, ry, rw, rh = (int(v) for v in rect)
    g = max(8, min(rw, rh) // 45) if gutter is None else max(0, int(gutter))
    cw = max(1, (rw - (cols + 1) * g) // cols)
    ch = max(1, (rh - (rows + 1) * g) // rows)
    return [(rx + g + (i % cols) * (cw + g),
             ry + g + (i // cols) * (ch + g), cw, ch)
            for i in range(count)]


def picker_regions(w=1920, h=1080, views=None, rect=None, gutter=None):
    """touch.json region entries for the tile grid: one rect per view.

    Each tile fires the allowlisted ``select_view`` action (fixed-shape
    POST /show, no params passthrough) -- no generic action. List these
    FIRST: hit_test() gives earlier entries every overlap."""
    views = coerce_views({"views": views} if views is not None else {})
    rect = list(rect) if rect is not None else default_rect(w, h)
    return [{"id": "view-%s" % name,
             "rect": list(r),
             "action": {"name": "select_view", "view": name}}
            for name, r in zip(views, grid_geometry(rect, len(views),
                                                    gutter=gutter))]


def _font(screen, size):
    try:
        path = screen.font_path("DejaVuSans-Bold")
    except Exception:
        return None
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, max(8, int(size)))
    except Exception:
        return None


def draw(screen, views, geometry, rect, fills, bg, fg, dim,
         title="PICK A VIEW"):
    """One complete frame: header, tiles, and margin-hint labels. Pure
    draw (no I/O): tests call this directly."""
    img = screen.new_image(bg)
    d = ImageDraw.Draw(img)
    w, h = screen.W, screen.H
    rx, ry, rw, rh = rect
    title_font = _font(screen, min(h // 20, 54))
    tile_font = _font(screen, min(h // 14, 84))
    hint_font = _font(screen, min(h // 30, 36))

    if title_font is not None:
        d.text((w // 2, max(8, ry // 2)), title, font=title_font,
               fill=fg, anchor="ma")

    for i, (name, (x, y, cw, ch)) in enumerate(zip(views, geometry)):
        fill = fills[i % len(fills)]
        d.rectangle([x, y, x + cw, y + ch], fill=fill,
                    outline=INK, width=max(3, min(w, h) // 270))
        if tile_font is not None:
            d.text((x + cw // 2 + 2, y + ch // 2 + 3), name,
                   font=tile_font, fill=(90, 70, 110), anchor="mm")
            d.text((x + cw // 2, y + ch // 2), name,
                   font=tile_font, fill=INK, anchor="mm")
        else:
            d.text((x + 8, y + 8), name, fill=INK)

    # Margin hints describe the reference wiring (side gesture strips +
    # bottom button bar); drawn only where the rect leaves room.
    if hint_font is not None:
        if rx >= 80:
            d.text((rx // 2, h // 2), "ON", font=hint_font,
                   fill=dim, anchor="mm")
        if w - (rx + rw) >= 80:
            d.text((rx + rw + (w - rx - rw) // 2, h // 2), "NEXT",
                   font=hint_font, fill=dim, anchor="mm")
        if h - (ry + rh) >= 60:
            d.text((w // 2, ry + rh + (h - ry - rh) // 2),
                   "TAP A TILE · TAP HERE FOR VIEWS", font=hint_font,
                   fill=dim, anchor="mm")
    return img


def run(screen, params, stop):
    params = params or {}
    views = coerce_views(params)
    rect = coerce_rect(params, screen.W, screen.H)
    bg = screen.color(params.get("background"), (8, 10, 16))
    fg = screen.color(params.get("color"), (255, 255, 255))
    geometry = grid_geometry(rect, len(views))
    screen.present(draw(screen, views, geometry, rect, PALETTE,
                        bg, fg, (140, 160, 190),
                        str(params.get("title") or "PICK A VIEW")))


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Print touch.json region entries for the picker tile "
                    "grid (default 1920x1080, pinned six views). Paste "
                    "FIRST under \"regions\" (earlier entries win "
                    "overlaps), then the gesture strips, then the "
                    "fullscreen reload_confirm last.")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--views", default=",".join(DEFAULT_VIEWS),
                    help="comma-separated view names (max %d)" % MAX_VIEWS)
    ap.add_argument("--rect", default=None,
                    help="grid area x,y,w,h (default derived from size)")
    ap.add_argument("--gutter", type=int, default=None)
    args = ap.parse_args()
    rect = ([int(v) for v in args.rect.split(",")]
            if args.rect else default_rect(args.width, args.height))
    print(json.dumps(picker_regions(args.width, args.height,
                                    args.views.split(","), rect,
                                    args.gutter), indent=2))
