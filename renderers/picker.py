"""Tappable view picker for the lnx-server jumbotron.

A STATIC grid of labelled view tiles: each tile names one selectable view
and the matching touch region fires the ``select_view`` named action, so a
panel tap reroutes the displayed view with no phone in hand. Hit rects
for ``touch.json`` come from ``picker_regions()`` -- generate, never
hand-compute (TOUCH.md has the live-set command).

The tiles are drawn by the litehtml engine from
``html-templates/picker.html``: the renderer emits one absolutely
positioned tile div per slot, at exactly the rects ``grid_geometry()``
hands the touch regions, so a drawn tile and its tap target are the same
numbers by construction. The chrome (strips, title band, footer) is the
shared panel chrome -- see docs/HTML_RENDERER.md.

A sibling of ``options.py``: options NAMES the picks, the picker
SELECTS; either can target the other without trapping.

Isolated by design: reads its own ``views``/``rect`` params only, never the
policy clock, playlist, feeds, or any other view. The offered list is
bounded (at most 24 tiles): an explicit ``views`` list wins, otherwise the
daemon fills in the live advertised set (``GET /renderers``) minus views
that need params -- never a probe, never a hardcoded list. Overflow past
the cap drops the alphabetically-last names. Membership is enforced where
the set lives: ``show()`` rejects unknown renderers, current view kept.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _html_error
import _html_native
import _html_templates as templates
import _picker_tiles
from ui import grid as ui_grid
from ui import tile as ui_tile

NAME = "picker"
DESCRIPTION = ("Tappable view picker: tile grid rerouting the panel "
               "via the select_view touch action")
STATIC = True
CAPABILITY = "partial"  # a tile grid reflows into any region
PARAMS = {
    "views": {"type": "array",
              "help": "view names to offer as tiles (absent: the live "
                      "advertised set); malformed skipped, capped at 24"},
    "rect": {"type": "array",
             "help": "grid area [x, y, w, h] display px, default the "
                     "screen minus side strips and a bottom button bar"},
    "cols": {"type": "integer",
             "help": "grid columns (absent: read off the region's shape "
                     "-- a narrow band gets one application column)"},
    "title": {"type": "string",
              "help": "header text, default PICK A VIEW"},
    "background": {"type": "string",
                   "help": "background colour, default near-black"},
    "color": {"type": "string",
              "help": "primary text colour, default white"},
}

# Fallback six; daemon fills the live set when views is absent. Cap 24.
DEFAULT_VIEWS = ("clock", "chat", "row", "stream", "activity", "options")
MAX_VIEWS = 24
# The picker's own column policy: never more than three. The number of
# columns actually drawn is the tile component's (`ui.grid.columns`),
# which reads it off the region's shape -- so a 288px application band is
# one column of readable tiles rather than three 69px ones.
DEFAULT_COLS = 3

PALETTE = (
    (90, 200, 255),
    (80, 220, 120),
    (255, 210, 63),
    (200, 120, 255),
    (255, 140, 60),
    (255, 130, 180),
)
TEMPLATE = "picker.html"
BUILD_HINT = "build it: tools/build_litehtml.sh"


def default_rect(w, h):
    """Grid area: full screen minus side gesture strips and a bottom
    button bar (dead taps there route back here via tap_options)."""
    mx, top, bar = w // 12, h // 27, h // 6
    return [mx, top, w - 2 * mx, h - top - bar]


def live_views(renderers):
    """Tile default from the advertised set: working renderers showable
    with empty params (no required PARAMS), sorted. Param-gated and
    broken entries are out -- a tile posts empty params and show()
    would reject them. Garbage falls back to DEFAULT_VIEWS. Pure."""
    try:
        items = list((renderers or {}).items())
    except AttributeError:
        return list(DEFAULT_VIEWS)
    out = []
    for name, entry in items:
        schema = entry.get("params") if isinstance(entry, dict) else None
        schema = schema or {}
        if (not isinstance(name, str) or not name or "/" in name
                or not isinstance(entry, dict)
                or "module" not in entry
                or any(isinstance(s, dict) and s.get("required")
                       for s in schema.values())):
            continue
        out.append(name)
    return sorted(out) or list(DEFAULT_VIEWS)


def coerce_views(params):
    """Views param into at most MAX_VIEWS clean names (missing/empty
    falls back to DEFAULT_VIEWS; bad entries skipped). Never raises."""
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
    """Rect param into [x, y, rw, rh], clamped; garbage -> default."""
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


def coerce_cols(params):
    """The ``cols`` param, or None when absent or unusable (the
    component then reads the count off the region's shape). Never raises."""
    raw = params.get("cols") if isinstance(params, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw) if int(raw) > 0 else None


def grid_geometry(rect, count, cols=None, gutter=None):
    """Tile rects row-major inside `rect`.

    The arithmetic is the tile component's (`ui.grid`): the picker keeps
    only its cap and its name list, so a drawn tile and a tap target are
    the same four numbers by construction. At most `DEFAULT_COLS`.
    """
    columns = ui_grid.columns(rect, count, cap=DEFAULT_COLS, cols=cols)
    return ui_grid.grid(rect, count, columns, gutter=gutter)


def picker_regions(w=1920, h=1080, views=None, rect=None, gutter=None,
                   cols=None):
    """touch.json entries: one rect per view firing ``select_view``
    (fixed-shape POST /show). List FIRST: earlier wins overlaps. The same
    geometry the renderer draws (`cols`/`gutter` included)."""
    views = coerce_views({"views": views} if views is not None else {})
    rect = list(rect) if rect is not None else default_rect(w, h)
    return [{"id": "view-%s" % name,
             "rect": list(r),
             "action": {"name": "select_view", "view": name}}
            for name, r in zip(views, grid_geometry(rect, len(views),
                                                    cols=cols,
                                                    gutter=gutter))]


def draw(screen, views, geometry, rect, fills, bg, fg, dim,
         title=_picker_tiles.TITLE):
    """One complete frame: the chrome plus the tile layer, rendered by
    litehtml from html-templates/picker.html. `dim` stays in the
    signature because the sibling callers (unified) pass the old
    side-hint colour; the template owns that colour now.
    Never raises: a failure here is a card, never a blank."""
    try:
        document, root = templates.load(
            TEMPLATE,
            _picker_tiles.chrome(screen, rect, views, bg, fg, title),
            raw={"tiles": ui_tile.layer(
                views, geometry, fills, shadow=True,
                start=min(ui_tile.LABEL_MAX, max(12, screen.H // 14)))})
        image, _height = _html_native.render(
            document, screen.W, screen.H, background=tuple(bg), root=root)
        canvas = screen.new_image(bg)
        canvas.paste(image, (0, 0))
        return canvas
    except templates.TemplateError as err:
        return _html_error.error_frame(screen, "picker: " + str(err),
                                       "fix the template, then re-show")
    except _html_native.NativeMissing as err:
        return _html_error.error_frame(screen, "picker: " + str(err),
                                       BUILD_HINT)
    except _html_native.HtmlRenderError as err:
        return _html_error.error_frame(screen, "picker: " + str(err),
                                       "template parsed but would not draw")
    except Exception as err:  # never a blank panel, whatever happens
        return _html_error.error_frame(screen, "picker: %s" % err,
                                       "the picker could not draw")


def run(screen, params, stop):
    params = params or {}
    views = coerce_views(params)
    rect = coerce_rect(params, screen.W, screen.H)
    bg = screen.color(params.get("background"), (8, 10, 16))
    fg = screen.color(params.get("color"), (255, 255, 255))
    geometry = grid_geometry(rect, len(views), cols=coerce_cols(params))
    screen.present(draw(screen, views, geometry, rect, PALETTE,
                        bg, fg, (140, 160, 190),
                        str(params.get("title")
                            or _picker_tiles.TITLE)))


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Print touch.json tile-grid entries (defaults "
                    "1920x1080, fallback six views). Paste FIRST.")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--views", default=",".join(DEFAULT_VIEWS),
                    help="comma-separated view names (max %d)" % MAX_VIEWS)
    ap.add_argument("--rect", default=None,
                    help="grid area x,y,w,h (default derived from size)")
    ap.add_argument("--gutter", type=int, default=None)
    ap.add_argument("--cols", type=int, default=None)
    args = ap.parse_args()
    rect = ([int(v) for v in args.rect.split(",")]
            if args.rect else default_rect(args.width, args.height))
    print(json.dumps(picker_regions(args.width, args.height,
                                    args.views.split(","), rect,
                                    args.gutter, args.cols), indent=2))
