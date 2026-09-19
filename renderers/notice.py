"""Transient notice card for the policy layer's notification feature.

Shown switch-then-return, never composed: the daemon puts this view on
screen for a configured duration and then restores whatever was showing.
See ISA decision D6 -- this is the cheap approximation, not composition,
and it interrupts what is showing by design.

Severity maps to an accent colour; an explicit `color` param wins.
"""

from PIL import ImageDraw, ImageFont

NAME = "notice"
DESCRIPTION = "Transient notification card (policy layer shows it, then returns)"
STATIC = True
PARAMS = {
    "title": {"type": "string", "required": True, "help": "notice headline"},
    "body": {"type": "string", "help": "longer text, \n starts a new line"},
    "severity": {"type": "string", "help": "info (default), warn, or critical"},
    "color": {"type": "string", "help": "accent colour override; default follows severity"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}

SEVERITY_COLORS = {
    "info": (90, 200, 255),
    "warn": (255, 165, 0),
    "critical": (255, 70, 70),
}

TITLE_SIZE = 110
BODY_SIZE = 54


def _font(screen, name, size):
    path = screen.font_path(name)
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return None


def _shrink_to_fit(draw, text, font, max_width):
    """Binary-search the largest size <= font.size that fits max_width."""
    if font is None or not text:
        return font
    size = font.size
    low, high = 12, size
    best = low
    while low <= high:
        mid = (low + high) // 2
        try:
            probe = ImageFont.truetype(font.path, mid)
        except Exception:
            break
        try:
            w = draw.textlength(text, font=probe)
        except Exception:
            break
        if w <= max_width:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    try:
        return ImageFont.truetype(font.path, best)
    except Exception:
        return font


def run(screen, params, stop):
    title = str(params.get("title", "") or "").strip() or "(notice)"
    body = str(params.get("body", "") or "")
    severity = str(params.get("severity", "info") or "info").lower()
    accent = SEVERITY_COLORS.get(severity, SEVERITY_COLORS["info"])
    if params.get("color"):
        accent = screen.color(params.get("color"), accent)
    bg = screen.color(params.get("background"), (10, 10, 14))

    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)

    # Severity bar across the top: the glanceable bit from across the room.
    draw.rectangle([0, 0, screen.W, 18], fill=accent)

    title_font = _font(screen, "DejaVuSans-Bold", TITLE_SIZE)
    body_font = _font(screen, "DejaVuSans", BODY_SIZE)
    plain = title_font or body_font
    if title_font is not None:
        title_font = _shrink_to_fit(draw, title.split("\n")[0][:48],
                                    title_font, screen.W * 0.86)
    cy = screen.H // 2 - (60 if body else 0)
    draw.multiline_text(
        (screen.W // 2, cy), title,
        font=title_font or plain, fill=(255, 255, 255),
        anchor="mm", align="center",
    )
    if body:
        draw.multiline_text(
            (screen.W // 2, cy + 140), body,
            font=body_font or plain, fill=(200, 200, 205),
            anchor="ma", align="center", spacing=12,
        )
    # Severity tag, bottom-left.
    tag_font = _font(screen, "DejaVuSans-Bold", 36)
    draw.text((60, screen.H - 100), severity.upper(),
              font=tag_font or plain, fill=accent)
    screen.present(img)
