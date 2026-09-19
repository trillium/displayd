"""Per-store colours and icons for the beads overview.

NOT a renderer: no run(), so the daemon's loader skips this file. Only
beads.py (the parade overview) imports it; beads_detail.py is untouched.

Why this file exists: react-icons is a JavaScript package and cannot be
imported from this Python/Pillow renderer. The equivalent here is single
glyphs drawn with a TrueType font Pillow can already load. Every default
icon below was checked against the cmap of DejaVu Sans -- the font the
overview already draws with -- so they render as real glyphs on the panel,
not tofu. No Nerd Font or Font Awesome install is required. If the captain
installs one, a store entry may name it with the optional "font" key and
use any of its codepoints instead.

Store look (JSON -- stdlib only, so no new dependency):

    {"stores": {"task": {"color": "#6EB4FF", "icon": "\\u25cf"},
                ...},
     "default_icon": "\\u25c7"}

Lookup order, highest priority first (later sources only overlay the
stores they name; the rest fall through):

    1. the renderer's "store_config" param -- an explicit path handed to
       POST /show, e.g. {"renderer": "beads", "params": {"store_config":
       "/home/trillium/displayd/state/beads-stores.json"}}
    2. the $DISPLAYD_BEADS_STORES environment variable (a path)
    3. <daemon-root>/state/beads-stores.json -- the captain's working
       copy; it survives redeploys because install.sh never touches state/
    4. renderers/beads_stores.json next to this module (shipped defaults)
    5. STORE_DEFAULTS below -- always present, so the renderer never
       crashes and never blanks, even with every file missing or broken

Reload: load_store_styles() re-reads a file when its mtime changes, and
the overview calls it on every draw -- edit the JSON and the panel picks
it up within a draw cycle, no restart needed. (A restart also works: the
mapping is read fresh at renderer start.)

Unknown stores degrade gracefully, never crash, never blank, never
collide: the colour is derived deterministically from the store name
(sha1 -> hue at fixed saturation/lightness chosen for a dark panel), and
the icon falls back to default_icon. Two unknown stores always look
different.

Adding a store: append one entry to the file from source 3 above, e.g.

    "garden": {"color": "#7DE3A8", "icon": "\\u2698"}

No code change, no restart. "color" accepts #rgb, #rrggbb, a few colour
names, or an [r, g, b] triple; a malformed colour falls back to the
deterministic name-derived colour for that store.
"""

import colorsys
import hashlib
import json
import os

CONFIG_FILENAME = "beads_stores.json"
STATE_FILENAME = "beads-stores.json"
ENV_VAR = "DISPLAYD_BEADS_STORES"
PARAM_KEY = "store_config"

DEFAULT_ICON = "\u25c7"  # WHITE DIAMOND: generic marker, in DejaVu Sans

STORE_DEFAULTS = {
    # colour: distinct hues, all bright on the near-black panel.
    # icon: single DejaVu Sans glyph (cmap-verified, never tofu).
    "task": {"color": "#6EB4FF", "icon": "\u25cf"},    # BLUE CIRCLE
    "brain": {"color": "#BE82FF", "icon": "\u25c6"},   # VIOLET DIAMOND
    "robots": {"color": "#50DCDC", "icon": "\u2699"},  # CYAN GEAR
    "review": {"color": "#FF78B4", "icon": "\u2605"},  # ROSE STAR
    "ideas": {"color": "#DCDC64", "icon": "\u2600"},   # GOLD SUN
}

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

_CACHE = {"key": None, "styles": None, "source": None, "error": None}


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


def format_tag(issue):
    """'[bead-hash]' alone -- the store prefix inside the brackets was
    redundant ([review review-a2n] said 'review' twice). The trailing
    reason annotation is kept by the caller; only this tag shrinks."""
    return "[%s]" % (issue.get("id") if isinstance(issue, dict)
                     else issue)


def _daemon_root():
    try:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        return None


def _candidate_files(explicit):
    """(path, priority) low -> high; later entries overlay earlier ones."""
    cands = []
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(here, CONFIG_FILENAME))
    root = _daemon_root()
    if root:
        cands.append(os.path.join(root, "state", STATE_FILENAME))
    env = os.environ.get(ENV_VAR)
    if env and env.strip():
        cands.append(env.strip())
    if explicit and str(explicit).strip():
        cands.append(str(explicit).strip())
    seen, out = set(), []
    for path in cands:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _overlay(base, data):
    """Merge one parsed config file onto base. Never raises."""
    if not isinstance(data, dict):
        return "top level must be an object"
    stores = data.get("stores", {})
    if not isinstance(stores, dict):
        return "'stores' must be an object"
    for store, entry in stores.items():
        if not isinstance(entry, dict):
            continue
        name = str(store).strip().lower()
        if not name:
            continue
        slot = base.setdefault(name, {})
        if "color" in entry or "colour" in entry:
            slot["color"] = entry.get("color", entry.get("colour"))
        if "icon" in entry:
            glyph = str(entry.get("icon") or "")
            if glyph:
                slot["icon"] = glyph[:2]
        if "font" in entry:
            fam = str(entry.get("font") or "").strip()
            if fam:
                slot["font"] = fam
    default_icon = data.get("default_icon")
    if isinstance(default_icon, str) and default_icon:
        base[""] = {"icon": default_icon[:2]}
    return None


def load_store_styles(explicit=None):
    """Merged {store: {color, icon, font?}} plus meta. Never raises.

    Returns (styles, source, error): styles always holds at least the
    built-ins; source names the highest-priority file read (or 'builtins');
    error notes skipped malformed files, or None.
    """
    files = _candidate_files(explicit)
    try:
        key = (tuple(files), tuple(
            os.path.getmtime(p) if os.path.isfile(p) else None
            for p in files))
    except Exception:
        key = None
    if key is not None and key == _CACHE["key"] and _CACHE["styles"] is not None:
        return _CACHE["styles"], _CACHE["source"], _CACHE["error"]
    merged = {name: dict(entry) for name, entry in STORE_DEFAULTS.items()}
    source, errors = "builtins", []
    for path in files:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as err:
            errors.append("%s: %s" % (path, err))
            continue
        problem = _overlay(merged, data)
        if problem is not None:
            errors.append("%s: %s" % (path, problem))
            continue
        source = path
    styles = dict(merged)
    _CACHE.update(key=key, styles=styles, source=source,
                  error="; ".join(errors) if errors else None)
    return styles, source, ("; ".join(errors) if errors else None)


def style_for(store, styles=None):
    """(color_tuple, icon_glyph, known) for a store. Never raises, never
    blank: unknown stores get their deterministic colour + default icon."""
    name = str(store or "?").strip().lower() or "?"
    if styles is None:
        try:
            styles, _, _ = load_store_styles()
        except Exception:
            styles = {}
    try:
        entry = styles.get(name) or {}
        default_entry = styles.get("") or {}
    except Exception:
        entry, default_entry = {}, {}
    icon = entry.get("icon") or default_entry.get("icon") or DEFAULT_ICON
    if "color" in entry:
        # Configured (or misconfigured) colour: parse strictly, fall back
        # to the deterministic name colour on garbage.
        color = parse_color(entry.get("color"), unknown_color(name))
    elif name in STORE_DEFAULTS:
        color = parse_color(STORE_DEFAULTS[name]["color"],
                            unknown_color(name))
    else:
        color = unknown_color(name)
    try:
        known = name in styles
    except Exception:
        known = False
    return color, (str(icon)[:2] or DEFAULT_ICON), known


def clear_cache():
    _CACHE.update(key=None, styles=None, source=None, error=None)
