"""View-selection screen for the lnx-server jumbotron.

The tap-anywhere target: a tap that no configured touch region consumes
routes here (see touch.py ``tap_options``), so every fullscreen view has a
tap path to a screen that names the way back. Selection itself happens on
the phone-first control page (``GET /`` one-tap grid) or via ``POST /show``
-- the panel has no per-pixel buttons because touch regions are
host-configured and global, not per-view. This screen therefore shows the
fastest picks plus the return path, and never traps the user: a tap while
already here simply re-shows this view.

Isolated by design: this renderer reads its own selection params only and
never touches the policy clock, the playlist, feeds, or any other view.
"""

from PIL import ImageDraw, ImageFont

NAME = "options"
DESCRIPTION = ("View selection: tap-anywhere landing screen naming the "
               "fastest view picks and the way back")
STATIC = True
ACCENT = "#9CC8FF"
PARAMS = {
    "title": {"type": "string",
              "help": "header text, default OPTIONS"},
    "instructions": {"type": "string",
                     "help": "sub-header, default 'tap reached options -- pick a view'"},
    "views": {"type": "array",
              "help": "view names to list, default the pinned four "
                      "(clock, chat, row, stream); malformed entries skipped"},
    "background": {"type": "string",
                   "help": "background colour, default near-black"},
    "color": {"type": "string",
              "help": "primary text colour, default white"},
}

DEFAULT_TITLE = "OPTIONS"
DEFAULT_INSTRUCTIONS = "tap reached options \u2014 pick a view on the control page"
DEFAULT_VIEWS = ("clock", "chat", "row", "stream")
FOOTER = "control page one-tap grid \u00b7 POST /show \u00b7 playlist: POST /playlist/resume"


def _font(screen, size):
    try:
        path = screen.font_path("DejaVuSans-Bold")
    except Exception:
        return None
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, max(8, int(size)))
    except Exception:
        return None


def coerce_views(params):
    """Parse the views param into a clean list of names.

    Missing/empty falls back to DEFAULT_VIEWS; non-string, blank, and
    slash-containing entries are skipped (feed/renderer names are plain).
    Never raises on bad user input -- worst case is the default four."""
    try:
        raw = (params or {}).get("views")
    except AttributeError:
        return list(DEFAULT_VIEWS)
    if raw is None:
        return list(DEFAULT_VIEWS)
    if not isinstance(raw, (list, tuple)):
        return list(DEFAULT_VIEWS)
    cleaned = [v.strip() for v in raw
               if isinstance(v, str) and v.strip() and "/" not in v]
    return cleaned or list(DEFAULT_VIEWS)


def run(screen, params, stop):
    params = params or {}
    title = str(params.get("title") or DEFAULT_TITLE)
    instructions = str(params.get("instructions") or DEFAULT_INSTRUCTIONS)
    views = coerce_views(params)
    bg = screen.color(params.get("background"), (8, 10, 16))
    fg = screen.color(params.get("color"), (255, 255, 255))
    dim = (140, 160, 190)

    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    w, h = screen.W, screen.H

    title_font = _font(screen, min(h // 8, 160))
    item_font = _font(screen, min(h // 14, 84))
    small_font = _font(screen, min(h // 24, 44))

    y = int(h * 0.08)
    if title_font is not None:
        draw.text((w // 2, y), title, font=title_font, fill=fg, anchor="ma")
        y += title_font.size + int(h * 0.02)
    else:
        draw.text((20, y), title, fill=fg)
        y += 60
    if small_font is not None:
        draw.text((w // 2, y), instructions, font=small_font,
                  fill=dim, anchor="ma")
        y += small_font.size + int(h * 0.05)
    else:
        draw.text((20, y), instructions, fill=fg)
        y += 60

    # Two-column grid of view names, screen-bounded at any panel size.
    cols = 2 if len(views) > 2 else 1
    rows = (len(views) + cols - 1) // cols
    row_h = min((int(h * 0.62) // max(rows, 1)) or 1, 160)
    for idx, name in enumerate(views):
        cx = int(w * (0.27 if idx % 2 == 0 else 0.73)) if cols == 2 \
            else w // 2
        cy = y + (idx // cols) * row_h + row_h // 2
        if item_font is not None:
            draw.text((cx, cy), name, font=item_font, fill=fg, anchor="mm")
        else:
            draw.text((cx - 40, cy - 10), name, fill=fg)

    if small_font is not None:
        draw.text((w // 2, int(h * 0.92)), FOOTER, font=small_font,
                  fill=dim, anchor="ma")
    else:
        draw.text((20, h - 40), FOOTER, fill=fg)
    screen.present(img)
