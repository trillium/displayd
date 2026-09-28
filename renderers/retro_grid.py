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

Split across retro_grid_geom.py (pure geometry + tap resolution) and
retro_grid_draw.py (frame rendering) to stay within the project's
250-line budget; names used by existing tests are re-exported here.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from retro_grid_draw import (
    DEFAULT_BG,
    DEFAULT_BORDER,
    DEFAULT_INK,
    FLASH_BORDER,
    PALETTE,
    _contain,
    _dither,
    _fit_font,
    _font,
    _is_url,
    _lighten,
    _load_center,
    _shade,
    draw,
    draw_cell,
)
from retro_grid_geom import (
    DEFAULT_COLS,
    DEFAULT_ROWS,
    _region_index,
    cell_at_point,
    center_kind,
    coerce_boxes,
    current_highlight,
    default_gutter,
    grid_geometry,
    resolve_tap,
    touch_regions,
)

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

DEFAULT_FLASH_SECONDS = 1.2


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
