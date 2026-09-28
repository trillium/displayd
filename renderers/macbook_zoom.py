"""Magnified-tap review pane for the macbook view (drawn geometry).

The strip along the panel bottom (ZOOM_TOP to the base) shows the
fresh ``macbook/zoom`` capture -- a crop taken on the laptop around
the last positioned point -- blown up around the cursor with a
crosshair, so the captain sees exactly what the second tap will
click. A stale (> ZOOM_FRESH) or missing capture draws a hint, NEVER
an old screenshot presented as current. Pure apart from PIL; the pane
rect here is the single source for the macbook_click touch region.
"""

import base64
import io
import time

import macbook_map

ZOOM_TOP = 740
ZOOM_FRESH = 30.0
PAD = 48
META_SIZE = 34

C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)
C_ROW = (255, 255, 255)
C_ZOOM = (120, 200, 255)
C_STALE = (255, 180, 80)

# The map fills header..MAP_BOTTOM, the pane ZOOM_TOP..base: equal by
# construction, so the drawn map always matches the tap math.
MAP_BOTTOM = ZOOM_TOP


def pane_rect(width, height):
    """Touch-strip rect for the zoom pane: [x, y, w, h]."""
    return [0, ZOOM_TOP, width, max(0, height - ZOOM_TOP)]


def fresh(zoom, now=None):
    """True when `zoom` is a live review surface. Never raises."""
    try:
        if not isinstance(zoom, dict):
            return False
        if not zoom.get("jpeg") or not isinstance(zoom["jpeg"], str):
            return False
        now = time.time() if now is None else float(now)
        age = now - float(zoom.get("ts", 0))
        return 0 <= age < ZOOM_FRESH
    except (TypeError, ValueError):
        return False


def decode(zoom):
    """Zoom document -> PIL image, or None on any failure."""
    try:
        from PIL import Image
        raw = base64.b64decode(zoom.get("jpeg") or "")
        if not raw or len(raw) > 4 * 1024 * 1024:
            return None
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


def key(zoom):
    """Redraw identity: capture ts + point, or None when not fresh."""
    try:
        if not fresh(zoom):
            return None
        return (zoom.get("ts"), zoom.get("x"), zoom.get("y"))
    except Exception:
        return None


def draw_map(img, draw, screen, state, meta_font, accent):
    """Paint the shrunken display map (above the review pane).

    Lives here -- not in macbook.py -- so the map frame, the pane
    rect, and the daemon's tap math share one module and cannot
    drift: the map always fills header..MAP_BOTTOM, the pane always
    fills ZOOM_TOP..base, and MAP_BOTTOM == ZOOM_TOP by construction."""
    plain = meta_font
    displays = state.get("displays") or []
    box = macbook_map.union(displays)
    if box is None:
        draw.text((PAD, macbook_map.MAP_TOP + 20),
                  "no display geometry in feed",
                  font=plain, fill=C_DIM)
        return
    scale, ox, oy = macbook_map.frame(box, screen.W, screen.H,
                                      bottom=MAP_BOTTOM)
    if scale <= 0:
        return
    focus, mouse = state.get("focus") or {}, state.get("mouse") or {}
    active = focus.get("display_index")
    for i, d in enumerate(displays):
        if not isinstance(d, dict):
            continue
        r = macbook_map.rect((d.get("bounds") or {}), scale, ox, oy)
        if r is None:
            continue
        is_active = (i == active)
        draw.rectangle(r, outline=accent if is_active else (90, 90, 110),
                       width=5 if is_active else 2)
        tag = macbook_map.label(i, bool(d.get("main")))
        if is_active:
            tag += " FOCUS"
        draw.text((r[0] + 10, r[1] + 8), tag, font=plain,
                  fill=(255, 255, 255) if is_active else C_DIM)
    bounds = focus.get("window_bounds")
    rect = macbook_map.rect(bounds, scale, ox, oy) \
        if isinstance(bounds, dict) else None
    if rect is not None:
        draw.rectangle(rect, outline=accent, width=3)
    if isinstance(mouse.get("x"), (int, float)) and \
            isinstance(mouse.get("y"), (int, float)):
        px, py = macbook_map.project(mouse["x"], mouse["y"],
                                     scale, ox, oy)
        draw.ellipse([px - 9, py - 9, px + 9, py + 9],
                     fill=(255, 255, 255), outline=(0, 0, 0), width=2)


def draw(img, draw, screen, zoom, meta_font):
    """Paint the pane: magnified crop + crosshair, or the hint."""
    plain = meta_font
    draw.line([(PAD, ZOOM_TOP - 14), (screen.W - PAD, ZOOM_TOP - 14)],
              fill=C_LINE, width=2)
    if not fresh(zoom):
        draw.text((PAD, ZOOM_TOP + 16), "tap the map to position -- "
                  "a magnified review appears here, then tap it "
                  "to click", font=plain, fill=C_DIM)
        return
    shot = decode(zoom)
    inner_w, inner_h = screen.W - 2 * PAD, screen.H - ZOOM_TOP - PAD
    if shot is None or inner_w <= 0 or inner_h <= 0:
        draw.text((PAD, ZOOM_TOP + 16), "review capture unreadable -- "
                  "tap the map to position again", font=plain,
                  fill=C_STALE)
        return
    scale = max(2.0, inner_h / max(1, shot.size[1]))
    grown = shot.resize((max(1, int(shot.size[0] * scale)),
                         max(1, int(shot.size[1] * scale))))
    # Center-crop when the grown shot overflows the pane, center the
    # piece when it underflows: either way the shot centre (the cursor
    # the crop was taken around) lands exactly on the crosshair.
    cw, ch = min(inner_w, grown.size[0]), min(inner_h, grown.size[1])
    left, top = ((grown.size[0] - cw) // 2, (grown.size[1] - ch) // 2)
    img.paste(grown.crop((left, top, left + cw, top + ch)),
              (PAD + (inner_w - cw) // 2,
               ZOOM_TOP + 14 + (inner_h - ch) // 2))
    cx, cy = PAD + inner_w // 2, ZOOM_TOP + 14 + inner_h // 2
    draw.line([(cx - 26, cy), (cx + 26, cy)], fill=C_ZOOM, width=3)
    draw.line([(cx, cy - 26), (cx, cy + 26)], fill=C_ZOOM, width=3)
    draw.ellipse([cx - 30, cy - 30, cx + 30, cy + 30],
                 outline=C_ZOOM, width=3)
    try:
        age = max(0, time.time() - float(zoom.get("ts", 0)))
    except (TypeError, ValueError):
        age = 0
    draw.text((PAD, ZOOM_TOP + 20), "TAP IMAGE TO CLICK  %.1fx  "
              "(%.0f, %.0f)  %ds ago" % (
                  scale, zoom.get("x") or 0, zoom.get("y") or 0, age),
              font=plain, fill=C_ZOOM)
