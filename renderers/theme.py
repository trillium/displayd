"""The panel's design tokens: the one module that owns colour.

Every surface on this panel -- the litehtml templates and the Pillow
renderers that have not migrated yet -- reads its colours from here, so a
colour literal in a view is a defect this module exists to delete. Names
are semantic roles, not descriptions of the value: a view asks for
``muted`` or ``accent``, never for "a grey".

How the two paths consume it, and why they differ:

- **Templates** get the palette as CSS custom properties. ``css_root()``
  renders the ``:root { --page: #07080c; ... }`` block, and
  ``_html_compose`` injects it into every document at load time. The
  pinned litehtml revision implements css-variables-2 (``subst_var`` in
  ``style.cpp``) and inherits a custom property up the element tree, so
  ``color: var(--ink)`` in a template resolves to exactly ``INK``.
- **Pillow renderers** import the module and call ``rgb()`` for the role
  they mean. ``rgb()`` is the one conversion point, so a token can never
  be a tuple in one place and a hex string in another.

The palette is deliberately small. Two near-identical near-blacks are
drift, not design, so ``PAGE``/``BAND``/``PANEL``/``PANE`` cover the
surfaces and the four text steps (``INK``/``MUTED``/``MUTED_SOFT``/
``FAINT``) cover type. Add a role only when it is genuinely a different
job, and add it here -- never in a view.

Renderer accents live here too (``ACCENT_SLOTS``): the per-view colour a
progress bar wears, keyed by view name. ``renderer_registry`` resolves the
slot into every entry it loads (``accent_slot``) and
``playlist_color.accent_for`` reads it, so a shipped view declares no
colour at all; a renderer from outside this tree may still declare its own
``ACCENT = "#rrggbb"``, which wins over the slot.
"""

# ---------------------------------------------------------------- palette

PAGE = "#07080c"        # panel background, behind everything
BAND = "#0b0d13"        # chrome band / gesture strip fill
PANEL = "#161b22"       # raised surface: card, tile, pane
PANE = "#11141b"        # a pane inside the content band
EDGE = "#2b3240"        # hairline border of a raised surface
RULE = "#24405c"        # the rule under a title
BADGE = "#0d111c"       # the system-button tile fill
TRACK = "#26262e"       # the progress bar's strip, under its own fill

INK = "#eef2fa"         # primary text
INK_STRONG = "#ffffff"  # the biggest type, and glyphs on a dark tile
INK_SOFT = "#f4f7ff"    # the lead line: one notch under INK
MUTED = "#8b93a7"       # labels, status, secondary text
MUTED_SOFT = "#b9c2d6"  # body copy under a lead
FAINT = "#5c6780"       # footer and strip hints: present, not competing

# The tile: a bright fill takes dark ink, and a label on it wears a soft
# offset copy instead of a hard shadow. Both are roles rather than a
# per-view literal because the picker, the options grid and the arcade
# grid all label a bright surface (renderers/ui/tile.py owns the rest).
ON_ACCENT = "#120c20"    # ink -- and the frame -- on an accent-filled tile
LABEL_SHADE = "#5a4670"  # the offset copy behind a label on a bright fill

ACCENT = "#7fd1ff"      # the default accent: eyebrow, focus, live marker

# The alert family: one failure, from the dark page it sits on to the
# readable line above the rule. The Pillow error card draws all four.
ALERT = "#d64a4a"       # the rule on a failed render
ALERT_INK = "#ffc4c4"   # the headline under that rule
ALERT_BODY = "#e2d8dc"  # supporting text on the card
ALERT_PAGE = "#1c0a0e"  # the card's own dark red page

ATTENTION = "#ffb450"   # stale, waiting, needs-a-human amber
OK = "#50dc78"          # confirmed, landed, healthy green

# Ordered so the generated block is stable: token order is the palette's
# reading order, and a diff of the CSS is then a diff of the palette.
TOKENS = (
    ("page", PAGE),
    ("band", BAND),
    ("panel", PANEL),
    ("pane", PANE),
    ("edge", EDGE),
    ("rule", RULE),
    ("badge", BADGE),
    ("track", TRACK),
    ("ink", INK),
    ("ink-strong", INK_STRONG),
    ("ink-soft", INK_SOFT),
    ("muted", MUTED),
    ("muted-soft", MUTED_SOFT),
    ("faint", FAINT),
    ("on-accent", ON_ACCENT),
    ("label-shade", LABEL_SHADE),
    ("accent", ACCENT),
    ("alert", ALERT),
    ("alert-ink", ALERT_INK),
    ("alert-body", ALERT_BODY),
    ("alert-page", ALERT_PAGE),
    ("attention", ATTENTION),
    ("ok", OK),
)

# Per-view accent, keyed by the view name a renderer declares. These were
# `ACCENT = "#rrggbb"` literals in sixteen renderer modules; the bar
# colour, the picker tile and the home tile all ask for the view's slot
# here instead. `accent()` returns the default for an unknown view.
ACCENT_SLOTS = {
    "activity": "#50DC78",
    "chat": "#7FD1FF",
    "clock": "#4DC3FF",
    "dock": "#6EC887",
    "macbook": "#4DA3FF",
    "notice": "#5AC8FF",
    "options": "#9CC8FF",
    "picker": "#7BDFF2",
    "reload": "#50DC78",
    "retro_grid": "#FFD23F",
    "row": "#5CFF9D",
    "stream": "#FF4D4D",
    "text": "#FFFFFF",
    "touch_confidence": "#50DC78",
    "unified": "#7BDFF2",
}


def _rgb(value):
    """'#rrggbb' (or '#rgb') -> (r, g, b). Raises ValueError on nonsense."""
    digits = str(value).strip().lstrip("#")
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    if len(digits) != 6:
        raise ValueError("not a colour: %r" % (value,))
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))


def css(name):
    """The token's CSS colour, or the default accent for an accent slot.

    A name this palette does not define raises: a typo must not silently
    hand a view the wrong colour, and the caller is choosing from a set
    that is right here in the file.
    """
    table = dict(TOKENS)
    if name in table:
        return table[name]
    if name in ACCENT_SLOTS:
        return ACCENT_SLOTS[name]
    raise KeyError("no token %r" % (name,))


def rgb(name):
    """The token's PIL colour tuple. The one conversion point."""
    return _rgb(css(name))


def accent(view, default=ACCENT):
    """The accent for a view name: its slot, else the default."""
    return ACCENT_SLOTS.get(str(view), default)


def accent_rgb(view, default=ACCENT):
    return _rgb(accent(view, default))


BRIGHT_SUM = 384  # sum(r,g,b) above which a surface reads as bright


def bright(surface):
    """True when a surface reads as bright: the ink on it must be dark.

    ``surface`` is a PIL tuple or a '#rrggbb' string. Anything that cannot
    be read counts as dark, so an unreadable surface gets the light ink
    rather than dark type on a dark panel -- a legibility failure is worse
    than a small colour mistake.
    """
    try:
        rgb_ = _rgb(surface) if isinstance(surface, str) else tuple(surface)
        return sum(int(channel) for channel in rgb_[:3]) > BRIGHT_SUM
    except Exception:
        return False


def ink_on(surface, dark=None, light=None):
    """The ink that reads on ``surface``: ``on-accent`` on a bright fill,
    ``muted`` on a dark one.

    A view that paints a surface the caller chose -- a QR card's white
    page, an operator's background param -- cannot name one ink, and this
    is the one rule that decides. Both candidates are roles here, so such a
    view still states no colour of its own.
    """
    return (rgb("on-accent") if dark is None else dark) if bright(surface) \
        else (rgb("muted") if light is None else light)


def css_root(selector=":root"):
    """The token block every template document inherits.

    ``:root`` is supported by the pinned engine's selector matcher, and a
    custom property set on it is found by every descendant through
    ``html_tag::get_custom_property``. Pure and deterministic: same
    palette in, byte-identical block out.
    """
    lines = ["%s {" % selector]
    for name, value in TOKENS:
        lines.append("  --%s: %s;" % (name, value))
    lines.append("}")
    return "\n".join(lines)


def slot_root(selector=":root"):
    """The per-view accent custom properties, as a second block.

    Kept apart from ``css_root`` because the two answer different
    questions -- the palette is the design, the slots are view identity --
    and because only a handful of surfaces need the slots.
    """
    lines = ["%s {" % selector]
    for view in sorted(ACCENT_SLOTS):
        var = view.replace("_", "-")
        lines.append("  --accent-%s: %s;" % (var, ACCENT_SLOTS[view]))
    lines.append("}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(css_root())
    print(slot_root())
