"""A large running clock. Re-renders once a second."""

import time

from PIL import ImageDraw, ImageFont

NAME = "clock"
DESCRIPTION = "A large clock that updates every second"
STATIC = False
PARAMS = {
    "format": {"type": "string", "help": "strftime pattern, default %H:%M:%S"},
    "color": {"type": "string", "help": "text colour, default white"},
    "background": {"type": "string", "help": "background colour, default black"},
}


def _autofit(draw, text, path, width, height):
    best = 12
    low, high = 12, 900
    while low <= high:
        mid = (low + high) // 2
        font = ImageFont.truetype(path, mid)
        box = draw.textbbox((0, 0), text, font=font)
        if (box[2] - box[0]) <= width * 0.92 and (box[3] - box[1]) <= height * 0.92:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def run(screen, params, stop):
    pattern = params.get("format") or "%H:%M:%S"
    fg = screen.color(params.get("color"), (255, 255, 255))
    bg = screen.color(params.get("background"), (0, 0, 0))
    path = screen.font_path("DejaVuSans-Bold")

    last = None
    while not stop.is_set():
        text = time.strftime(pattern)
        if text != last:
            last = text
            img = screen.new_image(bg)
            draw = ImageDraw.Draw(img)
            if path is None:
                draw.text((20, 20), text, fill=fg)
            else:
                size = _autofit(draw, text, path, screen.W, screen.H)
                font = ImageFont.truetype(path, size)
                draw.text((screen.W // 2, screen.H // 2), text, font=font, fill=fg, anchor="mm")
            screen.present(img)
        stop.wait(0.25)
