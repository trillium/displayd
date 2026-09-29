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
live apps (``tab`` param, default 0, is the FIRST VISIBLE app index --
the window start, not a highlight). ``POST /talon/tab {"dir": +1|-1}``
pages the window by ``PAGE_STRIDE`` (VISIBLE - 1, so the new window
overlaps the old by one chip) with a rapid slide, clamped at both ends
-- no wraparound. Tapping a chip focuses that app (coordinate-only,
like every touch action here). There is no movable highlight: the only
emphasis is the Mac's live focused app from the feed, which is real
state. A press carries the old window in ``tab_from`` so the fresh
renderer thread can slide old-to-new instead of jumping.
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
            "help": "first visible app-chip index (window start), "
                    "default 0; steppers page it by %d, clamped" %
                    (lay.PAGE_STRIDE,)},
    "tab_from": {"type": "integer",
                 "help": "previous window start: set by POST "
                         "/talon/tab so the fresh draw slides "
                         "old-to-new instead of jumping"},
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
# Page-turn slide: 6 frames at 40ms ~= 240ms. The frames present
# directly (bypassing the change-identity dedup), so each one swaps to
# the panel -- a rapid slide, not a collapsed single-frame jump.
SLIDE_FRAMES = 6
SLIDE_DT = 0.04


def coerce_mode(params):
    """View mode from params: 'aim' or 'glance' (default). Never raises."""
    try:
        mode = str((params or {}).get("mode") or "glance").lower()
    except Exception:
        return "glance"
    return "aim" if mode == "aim" else "glance"


def coerce_tab(params):
    """Window start from params: int >= 0, default 0. Never raises.

    This is the FIRST VISIBLE chip, not a highlight -- the daemon
    clamps it to the page range once the live app count is known."""
    try:
        tab = int((params or {}).get("tab", 0))
    except (TypeError, ValueError):
        return 0
    return max(0, tab)


def coerce_tab_from(params):
    """Previous window start for the slide animation: int >= 0, or
    None when this draw is not a page turn. Never raises."""
    try:
        raw = (params or {}).get("tab_from", None)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


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
          zoom, slide=None):
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
    macbook_glance.header(draw, screen, img, title, state, stale, apps,
                          apps_stale, tab, fonts, slide=slide)
    macbook_glance.draw_map(img, draw, screen, state, meta or plain)
    return img


def _play_slide(screen, title, old_start, new_start, bg, stop):
    """Page-turn animation: SLIDE_FRAMES presents over ~240ms.

    A tab press re-shows the view, which restarts this thread on the
    NEW window -- without help that is an instant jump (and the daemon
    helpfully pre-presents the cached OLD frame underneath). Each
    frame draws the old window sliding out as the new slides in, so
    the eye follows the page. Best-effort: any failure (or a stop)
    falls through to the steady loop, which draws the new window.
    Never raises."""
    try:
        state = _latest(screen, "macbook", "state")
        apps_state = _latest(screen, "talon_apps", "state")
        zoom = _latest(screen, "macbook", "zoom")
        if state is None:
            return
        apps = _apps(apps_state)
        if not apps:
            return
        old_c = lay.page_start(old_start, len(apps))
        new_c = lay.page_start(new_start, len(apps))
        if old_c == new_c:
            return
        stale = time.time() - state.get("ts", 0) > STALE_AFTER
        apps_stale = bool(apps_state) and \
            time.time() - apps_state.get("ts", 0) > ta.STALE_AFTER
        direction = 1 if new_c > old_c else -1
        for frame in range(1, SLIDE_FRAMES + 1):
            if stop.is_set():
                return
            progress = frame / float(SLIDE_FRAMES)
            try:
                screen.present(_draw(screen, title, "glance", new_c,
                                     state, stale, apps, apps_stale, bg,
                                     zoom,
                                     slide=(old_c, progress, direction)))
            except Exception:
                return
            stop.wait(SLIDE_DT)
    except Exception:
        return


def run(screen, params, stop):
    title = str((params or {}).get("title") or "MACBOOK")
    bg = screen.color((params or {}).get("background"), (10, 10, 14))
    mode, tab = coerce_mode(params), coerce_tab(params)
    tab_from = coerce_tab_from(params)
    if tab_from is not None and tab_from != tab and mode == "glance":
        _play_slide(screen, title, tab_from, tab, bg, stop)
        if stop.is_set():
            return
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
