"""Frame composition for the merged macbook feature (both modes).

Single concept: turning the feeds' documents into a presented frame -- the
font set, the full-frame painter that delegates to GLANCE or AIM, and the
page-turn slide animation that recomposes the same frame while the app
window moves. No feed reads and no loop: macbook_feeds.py reads, run()
owns the loop.
"""

import os
import sys
import time

from PIL import ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import macbook_aim
import macbook_glance
import talon_apps as ta
from macbook_feeds import STALE_AFTER, _apps, _latest

# Page-turn slide: 6 frames at 40ms ~= 240ms. The frames present
# directly (bypassing the change-identity dedup), so each one swaps to
# the panel -- a rapid slide, not a collapsed single-frame jump.
SLIDE_FRAMES = 6
SLIDE_DT = 0.04


def _font(screen, name, size):
    path = screen.font_path(name)
    return ImageFont.truetype(path, size) if path else None


def _draw(screen, title, mode, tab, state, stale, apps, apps_stale, bg,
          zoom, preview, slide=None):
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
    macbook_glance.draw_map(img, draw, screen, state, preview,
                            meta or plain)
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
        preview = _latest(screen, "macbook", "preview")
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
                                     zoom, preview,
                                     slide=(old_c, progress, direction)))
            except Exception:
                return
            stop.wait(SLIDE_DT)
    except Exception:
        return
