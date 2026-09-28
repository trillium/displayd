"""Panel sleep: the dedicated dark view behind the wake target.

Shown ONLY by the daemon's power-off path (see DisplayDaemon.set_power):
going dark also switches here, so the touch service's per-view wake
region (view_regions "sleep": one fullscreen screen_on target) goes live
exactly while the panel is asleep and never otherwise. A dedicated name
is the point: repurposing a generic fill (e.g. solid) would arm the wake
target on every unrelated use and shadow that view's own controls.

Normally unseen -- the backlight is off while this shows. The dim hint
is for the lit case only (a manual POST /show sleep demo): it names the
way back so the view never traps anyone. STATIC, no inputs."""

from PIL import ImageDraw, ImageFont

NAME = "sleep"
DESCRIPTION = ("Panel sleep: near-black view shown while the backlight "
               "is off; any tap wakes via the sleep wake region")
STATIC = True
PARAMS = {
    "hint": {"type": "string",
             "help": "centered hint, default 'asleep -- tap anywhere "
                     "to wake'"},
    "background": {"type": "string",
                   "help": "background colour, default black"},
    "color": {"type": "string",
              "help": "hint colour, default dim gray"},
}

DEFAULT_HINT = "asleep \u2014 tap anywhere to wake"


def _font(screen, size):
    try:
        path = screen.font_path("DejaVuSans")
    except Exception:
        return None
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, max(8, int(size)))
    except Exception:
        return None


def run(screen, params, stop):
    params = params or {}
    hint = str(params.get("hint") or DEFAULT_HINT)
    bg = screen.color(params.get("background"), (0, 0, 0))
    fg = screen.color(params.get("color"), (70, 74, 88))
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    w, h = screen.W, screen.H
    font = _font(screen, min(h // 20, 48))
    if font is not None:
        draw.text((w // 2, h // 2), hint, font=font, fill=fg, anchor="mm")
    else:
        draw.text((20, h // 2), hint, fill=fg)
    screen.present(img)
