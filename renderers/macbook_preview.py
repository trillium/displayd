"""Live preview frames under the macbook map overlays (drawn pixels only).

The map keeps its geometry: frames paste exact-fit into the same
display rects (macbook_map.rect) the boxes used, so draw and tap
cannot drift. The caller then draws the focused-window rectangle and
the pointer dot on top, UNCHANGED -- previews are background, overlays
are truth. Degraded states stay explicit: no frames -> boxes +
PREVIEW OFF; aged frames -> last frames + amber STALE tag.

Pure apart from PIL/base64; never raises out of the paint path (a bad
frame paints nothing, never a crash).
"""

import base64
import io
import time

PREVIEW_FRESH = 3.0  # mirrors the state STALE_AFTER: ~3 missed frames

C_LIVE = (110, 220, 130)
C_STALE = (255, 180, 80)
C_OFF = (140, 140, 150)


def fresh(preview, now=None):
    """True when `preview` carries live frames. Never raises."""
    try:
        if not isinstance(preview, dict):
            return False
        frames = preview.get("frames")
        if not isinstance(frames, list) or not frames:
            return False
        if not any(isinstance(f, dict) and isinstance(f.get("jpeg"), str)
                   and f["jpeg"] for f in frames):
            return False
        now = time.time() if now is None else float(now)
        age = now - float(preview.get("ts", 0))
        return 0 <= age < PREVIEW_FRESH
    except (TypeError, ValueError):
        return False


def stale(preview, now=None):
    """True when frames exist but are aged out. Never raises."""
    try:
        if not isinstance(preview, dict):
            return False
        frames = preview.get("frames")
        if not isinstance(frames, list) or not frames:
            return False
        now = time.time() if now is None else float(now)
        return (now - float(preview.get("ts", 0))) >= PREVIEW_FRESH
    except (TypeError, ValueError):
        return False


def decode(frame):
    """One preview frame -> PIL RGB image, or None. Never raises."""
    try:
        from PIL import Image
        raw = base64.b64decode(frame.get("jpeg") or "")
        if not raw or len(raw) > 4 * 1024 * 1024:
            return None
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


def by_display(preview):
    """Preview document -> {display_index: frame}. Never raises."""
    try:
        frames = preview.get("frames") if isinstance(preview, dict) \
            else None
        if not isinstance(frames, list):
            return {}
        out = {}
        for f in frames:
            if not isinstance(f, dict):
                continue
            try:
                out[int(f.get("display_index"))] = f
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return {}


def paint(img, shot, rect):
    """Paste one decoded frame exact-fit into its display rect.

    The rect already has the display's aspect (uniform map scale), so
    exact-fit is aspect-correct. False when there is nothing to paint.
    Never raises."""
    try:
        if shot is None or rect is None:
            return False
        x0, y0, x1, y1 = rect
        w, h = max(1, int(x1 - x0)), max(1, int(y1 - y0))
        if w <= 0 or h <= 0:
            return False
        img.paste(shot.resize((w, h)), (int(x0), int(y0)))
        return True
    except Exception:
        return False


def mode(preview, now=None):
    """'live' | 'stale' | 'off': which badge the map shows. Never raises."""
    try:
        if fresh(preview, now):
            return "live"
        if stale(preview, now):
            return "stale"
        return "off"
    except Exception:
        return "off"


def badge(draw, x, y, state, font):
    """Map-mode chip: LIVE / PREVIEW STALE / PREVIEW OFF. Never raises."""
    try:
        text = {"live": "LIVE", "stale": "PREVIEW STALE"}.get(state,
                                                              "PREVIEW OFF")
        color = {"live": C_LIVE, "stale": C_STALE}.get(state, C_OFF)
        try:
            tw = draw.textlength(text, font=font)
        except Exception:
            tw = len(text) * 18
        draw.rectangle([x, y, x + tw + 36, y + 52], fill=(10, 10, 14))
        draw.rectangle([x, y, x + tw + 36, y + 52], outline=color,
                       width=2)
        draw.text((x + 18, y + 8), text, font=font, fill=color)
    except Exception:
        pass
