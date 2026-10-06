"""Merged home screen: picker tiles plus a live apps dock (E layout).

Tiles at real picker geometry, dock below (count + focused app +
LEFT/RIGHT split + mode line, one tap to the merged macbook screen). Home
suppressed here (this screen IS home); moon via the shared overlay.
The dock reads the talon_apps feed in place -- empty means no payload
yet, stale means quiet past ``STALE_AFTER``, neither ever moves a
tile. Regions from ``unified_regions()`` -- generate, never
hand-compute. Tiles exclude this view itself (a self tile no-ops).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import picker as pk
import talon_apps as ta
import unified_dock as dock_mod
from ui import system_buttons as buttons

coerce_views = pk.coerce_views  # audit contract: explicit list, else
# fallback (live-minus-self comes from tile_views with a table)

NAME = "unified"
DESCRIPTION = ("Merged home: view tiles plus a live apps dock; "
               "one dock tap opens the merged macbook screen")
STATIC = False
PARAMS = {
    "views": {"type": "array", "help": "tile views (absent: live "
                                       "set minus this view, max 24)"},
    "rect": {"type": "array", "help": "tile grid [x, y, w, h]"},
    "dock": {"type": "array", "help": "apps dock [x, y, w, h]"},
    "title": {"type": "string", "help": "grid header"},
    "background": {"type": "string", "help": "background colour"},
    "color": {"type": "string", "help": "primary text colour"},
}

POLL = 0.25


def default_grid(w, h):
    """Tile area: picker-like margins, bottom reserved for the dock."""
    mx, top = w // 12, h // 27
    return [mx, top, w - 2 * mx, max(1, h - top - h * 340 // 1080)]


def default_dock(w, h):
    """Dock strip: full width minus margins, bottom-anchored."""
    dh = max(120, h * 230 // 1080)
    side = w * 80 // 1920
    return [side, h - 80 * h // 1080 - dh, w - 2 * side, dh]


def live_tile_views(renderers):
    """Live advertised set minus this view (a self tile would no-op)."""
    return [v for v in pk.live_views(renderers) if v != NAME]


def tile_views(params, renderers=None):
    """Tile list: explicit wins, else live-minus-self, else fallback."""
    params = params if isinstance(params, dict) else {}
    if renderers is not None and not isinstance(params.get("views"),
                                                (list, tuple)):
        try:
            if live_tile_views(renderers):
                return live_tile_views(renderers)
        except Exception:
            pass
    return pk.coerce_views(params)


def _coerce_box(params, key, default, w, h):
    try:
        raw = (params or {}).get(key)
    except AttributeError:
        raw = None
    ok = (isinstance(raw, (list, tuple)) and len(raw) == 4 and all(
        isinstance(v, (int, float)) and not isinstance(v, bool)
        for v in raw))
    if ok:
        x, y, rw, rh = (int(v) for v in raw)
        if rw > 0 and rh > 0:
            return [max(0, min(w - 1, x)), max(0, min(h - 1, y)),
                    min(rw, w - x), min(rh, h - y)]
    return default(w, h)


def coerce_grid(params, w, h):
    """Grid rect param, clamped; garbage -> default."""
    return _coerce_box(params, "rect", default_grid, w, h)


def coerce_dock(params, w, h):
    """Dock rect param, clamped; garbage -> default."""
    return _coerce_box(params, "dock", default_dock, w, h)


def unified_regions(w=1920, h=1080, views=None, rect=None, dock=None,
                    gutter=None):
    """touch.json entries: sleep badge FIRST, tile rects, dock LAST.

    Tile ids carry a ``uview-`` prefix (not the picker's ``view-``):
    announce ids are unique across global AND every scoped set, so the
    picker scope and this scope cannot share ids. Rects and actions
    match the picker tiles one-for-one."""
    views = tile_views({"views": views} if views is not None else {})
    grid = list(rect) if rect is not None else default_grid(w, h)
    box = list(dock) if dock is not None else default_dock(w, h)
    tiles = [{"id": "uview-%s" % n, "rect": list(r),
              "action": {"name": "select_view", "view": n}}
             for n, r in zip(views, pk.grid_geometry(grid, len(views),
                                                     gutter=gutter))]
    return ([buttons.sleep_region(w, h)] + tiles +
            [{"id": "apps-dock", "rect": [int(v) for v in box],
              "action": {"name": "select_view", "view": "macbook"}}])


def audit_exact(w=1920, h=1080, views=None, params=None):
    """touch_audit.py projection: tile + dock entries (the sleep badge
    is asserted via the chrome helper, same split as picker). Pure."""
    grid = coerce_grid(params or {}, w, h)
    box = coerce_dock(params or {}, w, h)
    return [{"id": e["id"], "rect": [int(v) for v in e["rect"]],
             "action": e["action"], "required": True}
            for e in unified_regions(w, h, views, grid, box)
            if e["id"] != "screen-off"]


def draw(screen, views, geometry, grid, dock, state, stale, bg, fg,
         title="PICK A VIEW"):
    """One complete frame: picker grid via picker.draw, dock below.

    Both halves are litehtml documents now (picker.html, dock.html), so
    this composes two rendered strips and no longer owns a pixel: the
    dock paste touches only the dock rect, which is what keeps a dock
    that gained or lost its feed from moving a tile.
    """
    img = pk.draw(screen, views, geometry, grid, pk.PALETTE,
                  bg, fg, (140, 160, 190), title)
    return dock_mod.draw_dock(img, screen, dock, state, stale, bg, fg)


def _latest(screen):
    try:
        states = [st for st in screen.get_input("talon_apps", "state")
                  if isinstance(st, dict)]
    except Exception:
        return None
    return states[-1] if states else None


def _key(state):
    if not isinstance(state, dict):
        return (None,)
    apps = state.get("apps")
    return (tuple(apps) if isinstance(apps, list) else None,
            state.get("focused"), state.get("ts"),
            repr(state.get("windows")), repr(state.get("displays")))


def run(screen, params, stop):
    params = params or {}
    views = tile_views(params)
    grid = coerce_grid(params, screen.W, screen.H)
    dock = coerce_dock(params, screen.W, screen.H)
    bg = screen.color(params.get("background"), (8, 10, 16))
    fg = screen.color(params.get("color"), (255, 255, 255))
    title = str(params.get("title") or "PICK A VIEW")
    geometry = pk.grid_geometry(grid, len(views))
    last = None
    while not stop.is_set():
        state = _latest(screen)
        stale = bool(state) and \
            time.time() - state.get("ts", 0) > ta.STALE_AFTER
        key = (tuple(views), _key(state), stale)
        if key != last:
            last = key
            try:
                screen.present(draw(screen, views, geometry, grid, dock,
                                    state, stale, bg, fg, title))
            except Exception:
                pass
        stop.wait(POLL)


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="touch.json unified entries")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--views", default=",".join(pk.DEFAULT_VIEWS))
    ap.add_argument("--rect", default=None)
    ap.add_argument("--dock", default=None)
    ap.add_argument("--gutter", type=int, default=None)
    args = ap.parse_args()
    grid = ([int(v) for v in args.rect.split(",")] if args.rect
            else default_grid(args.width, args.height))
    box = ([int(v) for v in args.dock.split(",")] if args.dock
           else default_dock(args.width, args.height))
    print(json.dumps(unified_regions(args.width, args.height,
                                     args.views.split(","), grid, box,
                                     args.gutter), indent=2))
