"""Retro-grid frame rendering (one complete PIL image per frame).

Split out of renderers/retro_grid.py to stay within the project's
250-line budget. Owns the arcade palette, the color defaults, and
every draw helper including draw_cell() and draw().
renderers/retro_grid.py re-exports the names tests use.
"""

import os
import urllib.request

from PIL import Image, ImageDraw, ImageFont

DEFAULT_BG = (16, 12, 40)
DEFAULT_BORDER = (12, 8, 20)
DEFAULT_INK = (18, 12, 32)  # dark label ink on bright fills

# Limited arcade palette, cycled per cell unless a box sets "color".
PALETTE = (
    (255, 82, 82),    # arcade red
    (255, 210, 63),   # coin yellow
    (80, 220, 120),   # frogger green
    (90, 200, 255),   # mario sky
    (200, 120, 255),  # power-up purple
    (255, 140, 60),   # sunset orange
    (120, 220, 220),  # ice cyan
    (255, 130, 180),  # player-2 pink
    (150, 255, 120),  # lime
    (120, 140, 255),  # indigo
    (255, 240, 150),  # pale coin
    (100, 235, 200),  # mint
)

FLASH_BORDER = (255, 255, 255)


def _font(screen, size):
    try:
        path = screen.font_path("DejaVuSans-Bold")
    except Exception:
        path = None
    if path:
        try:
            return ImageFont.truetype(path, max(8, int(size)))
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=max(8, int(size)))
    except Exception:
        return ImageFont.load_default()


def _shade(rgb, factor):
    return tuple(max(0, min(255, int(v * factor))) for v in rgb)


def _lighten(rgb, amount):
    return tuple(max(0, min(255, int(v + (255 - v) * amount))) for v in rgb)


def _is_url(path):
    return path.startswith("http://") or path.startswith("https://")


def _load_center(source, timeout=10):
    """Load one cell image (file path or URL); None when unloadable.

    The caller falls back to the text label, so a bad path/URL degrades
    one cell instead of failing the frame.
    """
    if not isinstance(source, str) or not source.strip():
        return None
    source = source.strip()
    try:
        if _is_url(source):
            with urllib.request.urlopen(source, timeout=timeout) as resp:
                return Image.open(resp).convert("RGB")
        return Image.open(os.path.expanduser(source)).convert("RGB")
    except Exception:
        return None


def _contain(img, width, height):
    scale = min(width / max(1, img.width), height / max(1, img.height))
    return img.resize((max(1, int(img.width * scale)),
                       max(1, int(img.height * scale))))


def _fit_font(draw, label, font_maker, max_w, max_h, start):
    size = max(8, int(start))
    while size > 8:
        font = font_maker(size)
        try:
            box = draw.textbbox((0, 0), label, font=font)
            if box[2] - box[0] <= max_w and box[3] - box[1] <= max_h:
                return font
        except Exception:
            return font
        size = int(size * 0.85)
    return font_maker(8)


def _dither(draw, rect, base):
    """Checkerboard darkening over the fill: cheap CRT-dither feel."""
    dark = _shade(base, 0.88)
    x, y, w, h = rect
    for py in range(y + 2, y + h - 2, 2):
        for px in range(x + 2 + (py % 4 == 0) * 1, x + w - 2, 2):
            draw.point((px, py), fill=dark)


def draw_cell(draw, img, screen, box, rect, fill, border, highlight,
              image=None, content_pad=14):
    """Draw one beveled arcade button into the in-progress frame."""
    x, y, w, h = rect
    bw = max(4, min(screen.W, screen.H) // 135)  # chunky outline (~8px@1080p)
    pop = max(3, bw // 2) if highlight else 0
    x0, y0, x1, y1 = x - pop, y - pop, x + w + pop, y + h + pop

    # Drop shadow (skipped when popped: the flash lifts the button).
    if not highlight:
        draw.rectangle([x0 + 6, y0 + 8, x1 + 6, y1 + 8], fill=(0, 0, 0))

    body = _lighten(fill, 0.35) if highlight else fill
    draw.rectangle([x0, y0, x1, y1], fill=body)
    if not highlight:
        _dither(draw, (x0, y0, x1 - x0, y1 - y0), body)

    # Thick outline; flashing cells invert to white-hot.
    draw.rectangle([x0, y0, x1, y1], outline=FLASH_BORDER if highlight else border,
                   width=bw + (2 if highlight else 0))
    # Bevel: light top/left, dark bottom/right (inset inside the outline).
    inset = bw + 2
    hi = _lighten(body, 0.45)
    lo = _shade(body, 0.55)
    draw.line([(x0 + inset, y0 + inset), (x1 - inset, y0 + inset)], fill=hi, width=3)
    draw.line([(x0 + inset, y0 + inset), (x0 + inset, y1 - inset)], fill=hi, width=3)
    draw.line([(x0 + inset, y1 - inset), (x1 - inset, y1 - inset)], fill=lo, width=3)
    draw.line([(x1 - inset, y0 + inset), (x1 - inset, y1 - inset)], fill=lo, width=3)

    # Center content inside the bevel.
    pad = inset + content_pad
    cw, ch = x1 - x0 - 2 * pad, y1 - y0 - 2 * pad
    if cw < 8 or ch < 8:
        return
    if image is not None:
        fitted = _contain(image, cw, ch)
        # Dark plate behind photos so any image reads as one button face.
        plate = [x0 + pad - 4, y0 + pad - 4,
                 x0 + pad + cw + 4, y0 + pad + ch + 4]
        draw.rectangle(plate, fill=(0, 0, 0))
        draw.rectangle(plate, outline=hi if not highlight else FLASH_BORDER, width=2)
        img.paste(fitted, (x0 + pad + (cw - fitted.width) // 2,
                           y0 + pad + (ch - fitted.height) // 2))
        caption = box.get("label", "")
        if caption and ch - fitted.height > 40:
            cap_font = _font(screen, max(12, ch // 10))
            draw.text(((x0 + x1) // 2, y1 - pad - 6), caption,
                      font=cap_font, fill=DEFAULT_INK, anchor="mb")
        return
    label = box.get("label", "")
    if not label:
        return
    try:
        want = int(box.get("text_size") or 0)
    except (TypeError, ValueError):
        want = 0
    start = want or min(cw // max(1, len(label)), ch)
    font = _fit_font(draw, label, lambda s: _font(screen, s),
                     cw, int(ch * 0.72), start)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    ink = screen.color(box.get("text_color"), DEFAULT_INK)
    # Hard offset shadow: the readable-at-distance arcade punch.
    draw.text((cx + 3, cy + 4), label, font=font,
              fill=_shade(ink, 0.4) if not highlight else (80, 60, 0), anchor="mm")
    draw.text((cx, cy), label, font=font,
              fill=(255, 255, 255) if highlight else ink, anchor="mm")


def draw(screen, boxes, geometry, fills, border, highlight, images, bg=None):
    """One complete frame. Pure draw (no I/O): tests call this directly."""
    img = screen.new_image(DEFAULT_BG if bg is None else bg)
    d = ImageDraw.Draw(img)
    for i, rect in enumerate(geometry):
        box = boxes[i] if i < len(boxes) else {"label": str(i + 1)}
        draw_cell(d, img, screen, box, rect, fills[i], border,
                  highlight == i, image=(images or {}).get(i))
    return img

