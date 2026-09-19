"""Render text, centred and auto-fitted to the screen."""

from PIL import ImageDraw, ImageFont

NAME = "text"
DESCRIPTION = "Show a message centred on the screen, auto-sized to fill it"
STATIC = True
# Playlist progress-bar colour for this view (see playlist.accent_for).
ACCENT = "#FFFFFF"
PARAMS = {
    "text": {"type": "string", "required": True, "help": "message to show; \\n starts a new line"},
    "size": {"type": "integer", "help": "font pixel height; auto-fitted when omitted"},
    "font": {"type": "string", "help": "font family, default DejaVuSans-Bold"},
    "color": {"type": "string", "help": "text colour, default white"},
    "background": {"type": "string", "help": "background colour, default black"},
}


def _fits(draw, text, font, width, height, margin):
    box = draw.multiline_textbbox((0, 0), text, font=font, spacing=max(4, font.size // 4))
    return (box[2] - box[0]) <= width * margin and (box[3] - box[1]) <= height * margin


def _autofit(draw, text, path, width, height, margin=0.88):
    best = 12
    low, high = 12, 900
    while low <= high:
        mid = (low + high) // 2
        font = ImageFont.truetype(path, mid)
        if _fits(draw, text, font, width, height, margin):
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def run(screen, params, stop):
    text = str(params.get("text", ""))
    if not text:
        return
    fg = screen.color(params.get("color"), (255, 255, 255))
    bg = screen.color(params.get("background"), (0, 0, 0))
    path = screen.font_path(params.get("font") or "DejaVuSans-Bold")

    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    if path is None:
        draw.text((20, 20), text, fill=fg)
        screen.present(img)
        return

    size = int(params.get("size") or 0) or _autofit(draw, text, path, screen.W, screen.H)
    font = ImageFont.truetype(path, size)
    spacing = max(4, size // 4)
    draw.multiline_text(
        (screen.W // 2, screen.H // 2),
        text,
        font=font,
        fill=fg,
        spacing=spacing,
        anchor="mm",
        align="center",
    )
    screen.present(img)
