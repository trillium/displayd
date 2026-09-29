"""Apps-dock drawing for the merged home screen. NOT a renderer: no
run(), so the daemon's loader skips this file (same convention as
home_chrome.py / row_draw.py).

One module owns the dock strip so renderers/unified.py stays a thin
composition (grid via picker + dock here + chrome via overlay): the
summary content (``dock_summary``, pure) and its pixels (``draw_dock``).
Empty means no feed payload yet, stale means quiet past STALE_AFTER --
both render inside the strip only, tiles never move. Never raises: a
missing dock beats a missing frame.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import ImageFont

import talon_apps as ta


def dock_summary(state):
    """Dock content from a feed state (pure): mode/count/focus/split."""
    grouped = ta.groups(state)
    state = state if isinstance(state, dict) else {}
    raw = state.get("apps")
    apps = [ta.clean(a) for a in raw] if isinstance(raw, list) else []
    if grouped.get("mode") == "sides":
        mode = "left=D%d right=other" % ((grouped.get("left_display")
                                          or 0) + 1)
    else:
        mode = "one screen: split"
    return {"mode": mode, "count": len(apps),
            "focused": ta.clean(state.get("focused")),
            "overflow": grouped.get("overflow") or 0,
            "left": grouped.get("left_total", len(grouped.get("left", []))),
            "right": grouped.get("right_total",
                                 len(grouped.get("right", [])))}


def _font(screen, bold, size):
    try:
        path = screen.font_path("DejaVuSans-Bold" if bold else "DejaVuSans")
    except Exception:
        return None
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, max(8, int(size)))
    except Exception:
        return None


def draw_dock(d, screen, dock, state, stale):
    """Dock strip onto a grid frame. Never raises."""
    try:
        _draw(d, screen, dock, state, stale)
    except Exception:
        pass


def _draw(d, screen, dock, state, stale):
    dx, dy, dw, dh = (int(v) for v in dock)
    s = dh / 230.0
    pad = int(24 * s)
    f_head = _font(screen, True, 36 * s)
    f_sub = _font(screen, False, 30 * s)
    f_main = _font(screen, True, 54 * s)
    dim, green, amber = (140, 150, 175), (110, 200, 135), (255, 180, 80)
    d.rounded_rectangle([dx, dy, dx + dw, dy + dh], radius=int(18 * s),
                        outline=ta.C_LINE, width=2)
    summ = dock_summary(state)
    d.ellipse([dx + pad, dy + int(20 * s), dx + pad + int(20 * s),
               dy + int(40 * s)], fill=green)
    d.text((dx + pad + int(34 * s), dy + int(12 * s)), "MAC APPS",
           font=f_head, fill=(255, 255, 255))
    d.text((dx + dw - pad, dy + int(14 * s)), summ["mode"],
           font=f_sub, fill=dim, anchor="rt")
    d.text((dx + pad, dy + int(58 * s)),
           "live summary -- tap: open macbook screen",
           font=f_sub, fill=dim)
    if state is None:
        d.text((dx + pad, dy + int(104 * s)),
               "waiting for talon feed -- run", font=f_sub, fill=dim)
        d.text((dx + pad, dy + int(140 * s)),
               "bridges/talon_apps.py  (views above still work)",
               font=f_sub, fill=dim)
    elif stale:
        bx = dx + pad
        d.rounded_rectangle([bx, dy + int(100 * s), bx + int(200 * s),
                             dy + int(150 * s)], radius=int(10 * s),
                            outline=amber, width=3)
        d.text((bx + int(18 * s), dy + int(106 * s)), "STALE",
               font=f_head, fill=amber)
        d.text((bx + int(220 * s), dy + int(106 * s)),
               "feed quiet >30s -- last known:", font=f_sub, fill=amber)
        known = "apps (%d)" % summ["count"]
        if summ["focused"]:
            known += "   * " + ta.label(summ["focused"])
        if summ["overflow"]:
            known += "   +%d more" % summ["overflow"]
        d.text((dx + pad, dy + int(158 * s)), known,
               font=f_sub, fill=(255, 255, 255))
    else:
        main = "apps (%d)" % summ["count"]
        if summ["focused"]:
            main += "   * " + ta.label(summ["focused"])
        d.text((dx + pad, dy + int(100 * s)), main,
               font=f_main, fill=(255, 255, 255))
        tail = "+%d more -- " % summ["overflow"] if summ["overflow"] else ""
        d.text((dx + pad, dy + int(168 * s)),
               tail + "%d left / %d right" % (summ["left"], summ["right"]),
               font=f_sub, fill=dim)
    d.text((dx + dw - pad, dy + int(168 * s)), "tap dock: macbook",
           font=f_sub, fill=green, anchor="rt")
