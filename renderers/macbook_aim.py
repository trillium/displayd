"""AIM drawing for the merged macbook feature (drawn pixels only).

The fresh review capture fills the whole panel edge-to-edge (cover-fit,
centre-cropped so the capture centre -- the cursor it was taken around --
lands exactly on the crosshair). The pointer position is deliberately
DROPPED here: choosing where to click needs the picture, not the dot.
A missing or stale capture degrades to a visible hint frame, never a
blank screen and never an old screenshot presented as current.
"""

import base64
import io
import time

import macbook_layout as lay

ZOOM_FRESH = 30.0

C_DIM = (140, 140, 150)
C_ZOOM = (120, 200, 255)
C_STALE = (255, 180, 80)


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


def cover(shot_w, shot_h, panel_w, panel_h):
    """Cover-fit scale + crop box: the grown shot always covers the panel.

    Returns (scale, left, top, cw, ch): paste grown.crop((left, top,
    left + cw, top + ch)) at the centred panel offset. Never raises."""
    try:
        scale = max(float(panel_w) / max(1, float(shot_w)),
                    float(panel_h) / max(1, float(shot_h)))
    except (TypeError, ValueError, ArithmeticError):
        return (1.0, 0, 0, 0, 0)
    gw, gh = max(1, int(shot_w * scale)), max(1, int(shot_h * scale))
    cw, ch = min(panel_w, gw), min(panel_h, gh)
    return (scale, (gw - cw) // 2, (gh - ch) // 2, cw, ch)


def back_chip(draw, h, font):
    """Way back to GLANCE, bottom-left over the image (the touch
    region lives in config; this is its visible affordance)."""
    try:
        x, y, w, hh = lay.back_rect(h)
        draw.rounded_rectangle([x + 8, y + 6, x + w - 8, y + hh - 6],
                               radius=10, outline=C_DIM, width=2)
        draw.text((x + 44, y + 10), "< GLANCE", font=font, fill=C_DIM)
    except Exception:
        pass


def degraded(draw, w, h, stale, font):
    """No live capture: visible hint frame, never blank. Returns nothing."""
    back_chip(draw, h, font)
    draw.text((48, 60), "AIM -- no review capture",
              font=font, fill=C_DIM)
    draw.text((48, 120), "tap the GLANCE map to position, then open AIM "
              "-- a magnified review fills this screen",
              font=font, fill=C_DIM)
    if stale:
        draw.text((48, 180), "STALE -- macbook feed quiet >3s",
                  font=font, fill=C_STALE)


def draw(img, draw, screen, zoom, stale, font):
    """Paint the AIM frame: edge-to-edge capture or the hint. Returns True
    when a live capture is on screen."""
    if not fresh(zoom):
        degraded(draw, screen.W, screen.H, stale, font)
        return False
    shot = decode(zoom)
    if shot is None or screen.W <= 0 or screen.H <= 0:
        draw.text((48, 60), "AIM -- review capture unreadable",
                  font=font, fill=C_STALE)
        draw.text((48, 120), "tap the GLANCE map to position again",
                  font=font, fill=C_DIM)
        return False
    scale, left, top, cw, ch = cover(shot.size[0], shot.size[1],
                                     screen.W, screen.H)
    grown = shot.resize((max(1, int(shot.size[0] * scale)),
                         max(1, int(shot.size[1] * scale))))
    img.paste(grown.crop((left, top, left + cw, top + ch)),
              ((screen.W - cw) // 2, (screen.H - ch) // 2))
    cx, cy = screen.W // 2, screen.H // 2
    draw.line([(cx - 26, cy), (cx + 26, cy)], fill=C_ZOOM, width=3)
    draw.line([(cx, cy - 26), (cx, cy + 26)], fill=C_ZOOM, width=3)
    draw.ellipse([cx - 30, cy - 30, cx + 30, cy + 30],
                 outline=C_ZOOM, width=3)
    try:
        age = max(0, time.time() - float(zoom.get("ts", 0)))
    except (TypeError, ValueError):
        age = 0
    caption = "AIM %.1fx -- %ds ago -- tap image to click" % (scale, age)
    try:
        tw = draw.textlength(caption, font=font)
    except Exception:
        tw = 0
    # Bottom-right: bottom-left belongs to the back chip.
    cx0 = screen.W - 48 - tw
    draw.rectangle([cx0 - 10, screen.H - 96, screen.W - 38,
                    screen.H - 36], fill=(10, 10, 14))
    draw.text((cx0, screen.H - 88), caption, font=font, fill=C_ZOOM)
    if stale:
        draw.text((48, 60), "STALE -- macbook feed quiet >3s",
                  font=font, fill=C_STALE)
    back_chip(draw, screen.H, font)
    return True
