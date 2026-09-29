"""Merged MacBook + Talon feature: what is live, where the mouse is, state.

ONE feature with two modes, each claiming the full canvas (no fixed
bands, no bottom pane, no separate app-list view):

- GLANCE (default): slim full-width header -- tab-through app strip,
  focused app + window, mouse position, Talon mode -- with the display
  map filling everything below it. No screenshot.
- AIM: the fresh review capture fills the screen edge-to-edge and the
  pointer position is deliberately dropped -- choosing where to click
  needs the picture, not the dot. Tap the image to click it.

Fed by the Mac-side pollers through ``POST /feed/macbook/state``,
``POST /feed/macbook/zoom`` (both buffer 1 -- latest only), and the
retired talon_apps view's feed namespace ``talon_apps/state`` (read in
place, like the unified dock -- the app list lives in the header here,
not in a second view). Observed, never interactive: a draw never blocks
on a feed ("a stale frame beats a pause").

Degraded, never invented, in BOTH modes: no payload yet -> waiting
frame; payload older than STALE_AFTER -> amber STALE tag on the last
frame; app without window fields -> app-only line (Accessibility off or
no focused window). In AIM a missing capture degrades visibly rather
than showing a blank screen.

Tab-through, mechanically: the header shows a scrolling window of the
live apps (``tab`` param, default 0); ``POST /talon/tab`` steps the
highlight with wraparound; tapping a chip focuses that app
(coordinate-only, like every touch action here).
"""

import time

from PIL import ImageDraw, ImageFont

import macbook_aim
import macbook_glance
import macbook_layout as lay
import talon_apps as ta

NAME = "macbook"
DESCRIPTION = ("MacBook + apps: GLANCE (live apps, mouse, state) and AIM "
               "(fullscreen click review)")
STATIC = False
ACCENT = "#4DA3FF"
PARAMS = {
    "mode": {"type": "string",
             "help": "glance (default) or aim (fullscreen review)"},
    "tab": {"type": "integer",
            "help": "highlighted app index, default 0 (wraps)"},
    "title": {"type": "string", "help": "header text, default MACBOOK"},
    "background": {"type": "string", "help": "background colour"},
}
INPUTS = {
    "state": {
        "type": "object",
        "help": "macOS state document (see bridges/macos_state.py)",
        "required": ["ts", "focus"],
        "properties": {
            "ts": {"type": "number"},
            "accessibility_trusted": {"type": "boolean"},
            "focus": {
                "type": "object",
                "required": ["app_name"],
                "properties": {
                    "app_name": {"type": "string"},
                    "bundle_id": {"type": "string"},
                    "pid": {"type": "number"},
                    "window_title": {"type": "string"},
                    "window_bounds": {"type": "object"},
                    "display_index": {"type": "number"},
                },
            },
            "mouse": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "display_index": {"type": "number"},
                },
            },
            "displays": {"type": "array"},
            "talon": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string"},
                    "microphone": {"type": "string"},
                    "muted": {"type": "boolean"},
                },
            },
        },
        "buffer": 1,
    },
    "zoom": {
        "type": "object",
        "help": "magnified review capture (see bridges/mac_zoom.py)",
        "required": ["ts", "x", "y", "jpeg"],
        "properties": {
            "ts": {"type": "number"},
            "x": {"type": "number"},
            "y": {"type": "number"},
            "jpeg": {"type": "string", "maxLength": 140000},
        },
        "buffer": 1,
    },
}

POLL = 0.25
STALE_AFTER = 3.0


def coerce_mode(params):
    """View mode from params: 'aim' or 'glance' (default). Never raises."""
    try:
        mode = str((params or {}).get("mode") or "glance").lower()
    except Exception:
        return "glance"
    return "aim" if mode == "aim" else "glance"


def coerce_tab(params):
    """Tab index from params: int >= 0, default 0. Never raises."""
    try:
        tab = int((params or {}).get("tab", 0))
    except (TypeError, ValueError):
        return 0
    return max(0, tab)


def _font(screen, name, size):
    path = screen.font_path(name)
    return ImageFont.truetype(path, size) if path else None


def _latest(screen, renderer, name):
    states = [s for s in screen.get_input(renderer, name)
              if isinstance(s, dict)]
    return states[-1] if states else None


def _apps(apps_state):
    raw = (apps_state or {}).get("apps")
    return [ta.clean(a) for a in raw] if isinstance(raw, list) else []


def _key(state, apps_state, zoom, mode, tab):
    """Redraw identity: mode/tab/app/title/mode/pointer-cell/staleness."""
    if not state:
        return (mode, None)
    focus, mouse, talon = state.get("focus") or {}, state.get("mouse") or {}, \
        state.get("talon") or {}
    cell = None
    if isinstance(mouse.get("x"), (int, float)):
        cell = (int(mouse["x"] // 6), int(mouse.get("y", 0) // 6))
    stale = time.time() - state.get("ts", 0) > STALE_AFTER
    apps = tuple(_apps(apps_state))
    apps_stale = bool(apps_state) and \
        time.time() - apps_state.get("ts", 0) > ta.STALE_AFTER
    return (mode, tab, focus.get("app_name"), focus.get("window_title"),
            focus.get("display_index"), cell, talon.get("mode"),
            talon.get("muted"), stale, apps,
            (apps_state or {}).get("focused"), apps_stale,
            macbook_aim.key(zoom), stale)


def _draw(screen, title, mode, tab, state, stale, apps, apps_stale, bg,
          zoom):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    meta = _font(screen, "DejaVuSans", macbook_glance.META_SIZE)
    plain = meta
    if state is None:
        if mode == "aim":
            macbook_aim.degraded(draw, screen.W, screen.H, False, meta)
        else:
            macbook_glance.waiting(screen, img, draw, title, meta or plain)
        return img
    if mode == "aim":
        macbook_aim.draw(img, draw, screen, zoom, stale, meta or plain)
        return img
    fonts = (_font(screen, "DejaVuSans", macbook_glance.TITLE_SIZE),
             _font(screen, "DejaVuSans-Bold", macbook_glance.APP_SIZE),
             _font(screen, "DejaVuSans", macbook_glance.ROW_SIZE), meta)
    macbook_glance.header(draw, screen, title, state, stale, apps,
                          apps_stale, tab, fonts)
    macbook_glance.draw_map(img, draw, screen, state, meta or plain)
    return img


def run(screen, params, stop):
    title = str((params or {}).get("title") or "MACBOOK")
    bg = screen.color((params or {}).get("background"), (10, 10, 14))
    mode, tab = coerce_mode(params), coerce_tab(params)
    last_key = None
    while not stop.is_set():
        state = _latest(screen, "macbook", "state")
        apps_state = _latest(screen, "talon_apps", "state")
        zoom = _latest(screen, "macbook", "zoom")
        stale = bool(state) and \
            time.time() - state.get("ts", 0) > STALE_AFTER
        apps_stale = bool(apps_state) and \
            time.time() - apps_state.get("ts", 0) > ta.STALE_AFTER
        key = _key(state, apps_state, zoom, mode, tab)
        if key != last_key:
            last_key = key
            try:
                screen.present(_draw(screen, title, mode, tab, state,
                                     stale, _apps(apps_state), apps_stale,
                                     bg, zoom))
            except Exception:
                pass
        stop.wait(POLL)
