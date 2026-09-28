"""Tappable view picker for the lnx-server jumbotron.

A STATIC grid of labelled view tiles: each tile names one selectable view
and the matching touch region fires the ``select_view`` named action, so a
panel tap reroutes the displayed view with no phone in hand. Hit rects for
``touch.json`` come from ``picker_regions()`` below -- generate, never
hand-compute::

    python3 renderers/picker.py --width 1920 --height 1080 \\
        --views clock,chat,row,stream,activity,options

A sibling of ``options.py`` by choice: options only NAMES the picks and
the return path, the picker SELECTS. Either can target the other without
trapping: a dead tap while the picker shows re-shows it, and ``options``
stays one tile away.

Isolated by design: reads its own ``views``/``rect`` params only, never the
policy clock, playlist, feeds, or any other view. The offered list is
bounded (at most 24 tiles): an explicit ``views`` list wins, otherwise the
daemon fills in the live advertised set (``GET /renderers``) minus views
that need params -- never a probe, never a hardcoded list. Overflow past
the cap drops the alphabetically-last names. Membership is enforced where
the set lives: ``show()`` rejects unknown renderers, current view kept.
"""

from PIL import ImageDraw, ImageFont

NAME = "picker"
DESCRIPTION = ("Tappable view picker: tile grid rerouting the panel "
               "via the select_view touch action")
STATIC = True
ACCENT = "#7BDFF2"
PARAMS = {
    "views": {"type": "array",
              "help": "view names to offer as tiles (absent: the live "
                      "advertised set); malformed skipped, capped at 24"},
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

# Fallback only: the daemon fills the live set when views is absent.
DEFAULT_VIEWS = ("clock", "chat", "row", "stream", "activity", "options")
MAX_VIEWS = 24  # 3 columns x 8 rows max; overflow drops alphabetically-last
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


def live_views(renderers):
    """Tile default from the advertised set: working renderers showable
    with empty params (no required PARAMS), sorted. Param-gated views
    (reload, notice, qr, image, text) and broken entries are out -- a
    tile posts {"renderer": view, "params": {}} and show() would
    reject them. Garbage falls back to DEFAULT_VIEWS. Pure."""
    try:
        items = list((renderers or {}).items())
    except AttributeError:
        return list(DEFAULT_VIEWS)
    out = []
    for name, entry in items:
        schema = (entry.get("params") or {} if isinstance(entry, dict)
                  else None)
        if (not isinstance(name, str) or not name or "/" in name
                or not isinstance(entry, dict)
                or "module" not in entry
                or not isinstance(schema, dict)
                or any(isinstance(s, dict) and s.get("required")
                       for s in schema.values())):
            continue
        out.append(name)
    return sorted(out) or list(DEFAULT_VIEWS)


def coerce_views(params):
    """Views param into at most MAX_VIEWS clean names. Missing/empty
    falls back to DEFAULT_VIEWS; non-string, blank, and slash entries
    skipped; extras past the cap dropped. Never raises."""
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
    """Rect param into [x, y, rw, rh], clamped; garbage falls back to
    default_rect(). Never raises."""
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
    """Tile rects [(x, y, cw, ch)] row-major inside `rect` (cols cap at
    DEFAULT_COLS; gutter doubles as inner spacing). Pure."""
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
    """touch.json entries for the grid: one rect per view, each firing
    the allowlisted ``select_view`` action (fixed-shape POST /show --
    no generic action). List FIRST: earlier entries win overlaps."""
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
    """One complete frame: header, tiles, side-hint labels. Pure."""
    img = screen.new_image(bg)
    d = ImageDraw.Draw(img)
    w, h = screen.W, screen.H
    rx, ry, rw, rh = rect
    tile_font = _font(screen, min(h // 14, 84))
    hint_font = _font(screen, min(h // 30, 36))

    # Header lives in the top margin, skipped when the rect leaves no
    # room (a fullscreen grid has no margin to write in).
    if ry >= 28:
        title_font = _font(screen, min(ry - 12, h // 24, 44))
        if title_font is not None:
            d.text((w // 2, ry // 2), title, font=title_font,
                   fill=fg, anchor="mm")

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

    # Side hints name the host gesture strips (both have live regions);
    # no bottom-bar hint -- that bar has no touch region by design.
    if hint_font is not None:
        if rx >= 80:
            d.text((rx // 2, h // 2), "ON", font=hint_font,
                   fill=dim, anchor="mm")
        if w - (rx + rw) >= 80:
            d.text((rx + rw + (w - rx - rw) // 2, h // 2), "NEXT",
                   font=hint_font, fill=dim, anchor="mm")
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
        description="Print touch.json entries for the picker tile grid "
                    "(default 1920x1080, fallback six views). Paste FIRST "
                    "under regions (earlier entries win overlaps).")
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
