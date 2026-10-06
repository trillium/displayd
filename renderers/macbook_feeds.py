"""Reading the macbook view's feeds and its redraw identity.

Single concept: what one frame of the merged macbook feature reads from the
daemon's feed buffers -- the newest valid document for each of the three
inputs (macOS state, zoom capture, per-display preview), the app list
normalized through talon_apps, and the identity tuple that decides whether
the frame must be drawn again -- plus the staleness window those decisions
are judged against.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import macbook_aim
import macbook_preview
import talon_apps as ta

# Feed age past which the frame is drawn as STALE rather than current.
STALE_AFTER = 3.0


def _latest(screen, renderer, name):
    states = [s for s in screen.get_input(renderer, name)
              if isinstance(s, dict)]
    return states[-1] if states else None


def _apps(apps_state):
    raw = (apps_state or {}).get("apps")
    return [ta.clean(a) for a in raw] if isinstance(raw, list) else []


def _key(state, apps_state, zoom, preview, mode, tab):
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
            macbook_aim.key(zoom), stale,
            macbook_preview.mode(preview),
            (preview or {}).get("ts") if isinstance(preview, dict)
            else None)
