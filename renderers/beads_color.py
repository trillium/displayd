"""beads_color - colour parsing and deterministic unknown-store colours.

Single concept: turning a colour *value* into an (r, g, b) tuple. The
store-config lookup (JSON files, cache, icon glyphs) lives in
beads_style.py; this module only knows how to parse a configured value
and how to derive a stable colour for a store that has none.
"""

import colorsys
import hashlib

NAMED_COLORS = {
    "red": (255, 90, 90),
    "green": (80, 220, 120),
    "blue": (110, 180, 255),
    "cyan": (80, 220, 220),
    "magenta": (255, 120, 200),
    "yellow": (255, 220, 100),
    "orange": (255, 165, 0),
    "white": (235, 235, 240),
    "grey": (140, 140, 150),
    "gray": (140, 140, 150),
}


def parse_color(value, fallback):
    """Accept '#rgb', '#rrggbb', a few names, or an [r, g, b] triple."""
    if value is None or value == "":
        return fallback
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return tuple(max(0, min(255, int(v))) for v in value)
        except (TypeError, ValueError):
            return fallback
    text = str(value).strip()
    if text.lower() in NAMED_COLORS:
        return NAMED_COLORS[text.lower()]
    digits = text.lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    if len(digits) == 6:
        try:
            return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return fallback
    return fallback


def unknown_color(name):
    """Deterministic dark-panel-legible colour for an unconfigured store.

    sha1(name) -> hue, fixed saturation/lightness. Same name always maps
    to the same colour, in any process, with no config entry needed.
    """
    digest = hashlib.sha1(str(name or "?").encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65536.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.65, 0.70)
    return (int(r * 255), int(g * 255), int(b * 255))
