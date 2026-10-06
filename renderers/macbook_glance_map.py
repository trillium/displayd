"""The display-map half of the GLANCE frame.

Single concept: painting the display map that fills everything below the
header -- live preview frames pasted exact-fit into the display rects, each
display's label/FOCUS/LIVE tag, the focused-window rectangle, the pointer
dot, and the PREVIEW OFF/STALE badge. Split out of macbook_glance.py so the
header half and the map half stay separate concepts.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import macbook_layout as lay
import macbook_map
import macbook_preview
import theme
from macbook_glance_color import C_DIM


def draw_map(img, draw, screen, state, preview, meta_font):
    """Display map filling header..base: live previews, focus rect, pointer.

    Preview frames paste exact-fit into the display rects; the
    focused-window rectangle and the pointer dot draw on top,
    unchanged. No frames -> the old boxes + PREVIEW OFF badge."""
    plain = meta_font
    displays = state.get("displays") or []
    box = macbook_map.union(displays)
    if box is None:
        draw.text((lay.PAD, lay.HDR_H + 20),
                  "no display geometry in feed",
                  font=plain, fill=C_DIM)
        return
    scale, ox, oy = macbook_map.frame(box, screen.W, screen.H,
                                      top=lay.HDR_H, bottom=screen.H)
    if scale <= 0:
        return
    focus, mouse = state.get("focus") or {}, state.get("mouse") or {}
    active = focus.get("display_index")
    frames = macbook_preview.by_display(preview)
    for i, d in enumerate(displays):
        if not isinstance(d, dict):
            continue
        r = macbook_map.rect((d.get("bounds") or {}), scale, ox, oy)
        if r is None:
            continue
        is_active = (i == active)
        shot = macbook_preview.decode(frames[i]) \
            if i in frames else None
        live = macbook_preview.paint(img, shot, r)
        outline = theme.rgb("macbook") if is_active else (90, 90, 110)
        draw.rectangle(r, outline=outline, width=5 if is_active else 2)
        tag = macbook_map.label(i, bool(d.get("main")))
        if is_active:
            tag += " FOCUS"
        if live:
            tag += " LIVE"
            try:
                tw = draw.textlength(tag, font=plain)
            except Exception:
                tw = 0
            draw.rectangle([r[0] + 4, r[1] + 4,
                            r[0] + 16 + tw, r[1] + 44],
                           fill=(10, 10, 14))
        draw.text((r[0] + 10, r[1] + 8), tag, font=plain,
                  fill=(255, 255, 255) if is_active else C_DIM)
    bounds = focus.get("window_bounds")
    rect = macbook_map.rect(bounds, scale, ox, oy) \
        if isinstance(bounds, dict) else None
    if rect is not None:
        draw.rectangle(rect, outline=theme.rgb("macbook"), width=3)
    if isinstance(mouse.get("x"), (int, float)) and \
            isinstance(mouse.get("y"), (int, float)):
        px, py = macbook_map.project(mouse["x"], mouse["y"],
                                     scale, ox, oy)
        draw.ellipse([px - 9, py - 9, px + 9, py + 9],
                     fill=(255, 255, 255), outline=(0, 0, 0), width=2)
    macbook_preview.badge(draw, lay.PAD, lay.HDR_H + 8,
                          macbook_preview.mode(preview), plain)
