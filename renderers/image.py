"""Show a picture: any image file, scaled to the screen."""

import os
import urllib.request

from PIL import Image

NAME = "image"
DESCRIPTION = "Display an image file (local path or http(s) URL), scaled to the screen"
STATIC = True
PARAMS = {
    "path": {"type": "string", "required": True, "help": "file path or http(s) URL"},
    "fit": {"type": "string", "help": "contain (default), cover, or stretch"},
    "background": {"type": "string", "help": "letterbox colour, default black"},
}


def _load(path):
    if path.startswith("http://") or path.startswith("https://"):
        with urllib.request.urlopen(path, timeout=30) as resp:
            return Image.open(resp).convert("RGB")
    return Image.open(os.path.expanduser(path)).convert("RGB")


def _contain(img, width, height):
    scale = min(width / img.width, height / img.height)
    return img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))


def _cover(img, width, height):
    scale = max(width / img.width, height / img.height)
    resized = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def run(screen, params, stop):
    path = params.get("path")
    if not path:
        return
    fit = (params.get("fit") or "contain").lower()
    bg = screen.color(params.get("background"), (0, 0, 0))

    img = _load(path)
    if fit == "stretch":
        canvas = img.resize((screen.W, screen.H))
    elif fit == "cover":
        canvas = _cover(img, screen.W, screen.H)
    else:
        canvas = screen.new_image(bg)
        fitted = _contain(img, screen.W, screen.H)
        canvas.paste(fitted, ((screen.W - fitted.width) // 2, (screen.H - fitted.height) // 2))
    screen.present(canvas)
