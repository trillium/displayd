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

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import talon_apps as ta

import macbook_layout as lay
from macbook_draw import _draw, _play_slide
from macbook_feeds import STALE_AFTER, _apps, _key, _latest

# _draw and _key are re-exported above for the existing
# importers (the macbook tests call both by name).

NAME = "macbook"
DESCRIPTION = ("MacBook + apps: GLANCE (live apps, mouse, state) and AIM "
               "(fullscreen click review)")
STATIC = False
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
    "preview": {
        "type": "object",
        "help": "live per-display previews (see bridges/mac_preview.py)",
        "required": ["ts", "frames"],
        "properties": {
            "ts": {"type": "number"},
            "frames": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["display_index", "w", "h", "jpeg"],
                    "properties": {
                        "display_index": {"type": "number"},
                        "w": {"type": "number"},
                        "h": {"type": "number"},
                        "jpeg": {"type": "string",
                                 "maxLength": 56000},
                    },
                },
            },
        },
        "buffer": 1,
    },
}

POLL = 0.25


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
        preview = _latest(screen, "macbook", "preview")
        stale = bool(state) and \
            time.time() - state.get("ts", 0) > STALE_AFTER
        apps_stale = bool(apps_state) and \
            time.time() - apps_state.get("ts", 0) > ta.STALE_AFTER
        key = _key(state, apps_state, zoom, preview, mode, tab)
        if key != last_key:
            last_key = key
            try:
                screen.present(_draw(screen, title, mode, tab, state,
                                     stale, _apps(apps_state), apps_stale,
                                     bg, zoom, preview))
            except Exception:
                pass
        stop.wait(POLL)
