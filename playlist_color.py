"""Playlist bar colours: parsing and per-view resolution.

Single concept: what colour the progress bar wears. Resolution order is
per-view item ``color`` (operator override) > the renderer's own ``ACCENT``
> the view's palette accent slot (``theme.ACCENT_SLOTS``, resolved by
``renderer_registry`` into the entry's ``accent_slot``) > the playlist-level
``color`` default > white fallback.

A shipped view owns no colour constant: its accent is its palette slot, so
nothing here repeats ``theme.py``. A renderer may still declare
``ACCENT = "#rrggbb"`` (or a colour name, or an ``(r, g, b)`` tuple) to opt
in, which is how a plugin outside this repo keeps its identity colour; a
renderer that declares neither resolves exactly as before.
"""

DEFAULT_COLOR = (255, 255, 255)
# The strip under the fill is not a playlist colour: it is the progress
# component's own surface, so it is the palette's ``track`` token
# (``renderers/ui/progress.py``) and nothing here repeats it.

NAMED = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "grey": (128, 128, 128),
    "gray": (128, 128, 128),
    "orange": (255, 165, 0),
}


def parse_color(value, default=None):
    """Accept '#rgb', '#rrggbb', a few names, or an (r,g,b) tuple."""
    if value is None or value == "":
        return default
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return tuple(max(0, min(255, int(v))) for v in value)
        except (TypeError, ValueError):
            return default
    text = str(value).strip()
    if text.lower() in NAMED:
        return NAMED[text.lower()]
    digits = text.lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    if len(digits) == 6:
        try:
            return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return default
    return default


def accent_for(renderer_entry, item_color=None, default=DEFAULT_COLOR):
    """Resolve the bar colour for one view.

    ``renderer_entry`` is a registry entry as built by
    ``displayd.load_renderers`` (``{"module": mod, "accent_slot": ...}``);
    entries without a module (broken plugins) fall through to the default.
    Precedence: per-view item ``color`` > the module's own ``ACCENT`` >
    the entry's palette ``accent_slot`` > the default. The order puts a
    plugin's declared identity first and the palette second, so a shipped
    view (which declares no ``ACCENT``) reads ``theme.py`` and nothing
    else. Never raises: garbage resolves to the default."""
    entry = renderer_entry if isinstance(renderer_entry, dict) else {}
    for candidate in (item_color,
                      getattr(entry.get("module"), "ACCENT", None),
                      entry.get("accent_slot"),
                      default):
        parsed = parse_color(candidate, None)
        if parsed is not None:
            return parsed
    return DEFAULT_COLOR
