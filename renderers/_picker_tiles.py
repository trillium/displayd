"""The picker surface's chrome variables (its tile layer is a component).

Split out of renderers/picker.py because the picker's own job is the view
contract (params, offered views, touch geometry), while this is the
picker's chrome: the template variables ``picker.html`` owes, filled from
the geometry.

The TILE LAYER is not here any more. A tile -- its declared box, the
shrink-until-it-fits rule, the measured centring, and the markup a raw
slot receives -- is one component, ``renderers/ui/tile.py``, because
``_options_grid.py`` carried the same four helpers verbatim. This module
keeps only what is picker-specific: the variable contract, the title, and
the "empty collapses the slot" rules a tight rect depends on.

The variables are colours the renderer built from palette tuples, never
caller text, so the template can take them in a style attribute without
opening a CSS injection. The set is pinned against the template by a test.
"""

import _html_templates as templates

TITLE = "PICK A VIEW"


def hex_colour(color):
    """Palette tuple -> CSS colour (the trust boundary's one rule)."""
    return templates.hex_colour(color)


def chrome(screen, rect, views, bg, fg, title=TITLE):
    """The picker template's variables, filled from the geometry.

    An empty value collapses its slot, which is how a tight custom rect
    drops the bands it has no room for -- the old draw() skipped them the
    same way, so a custom rect still renders the same content.
    """
    rx, ry, rw, rh = (int(v) for v in rect)
    w, h = screen.W, screen.H
    roomy_top = ry >= 28
    roomy_bar = h - (ry + rh) >= 120
    return {
        "background": hex_colour(bg),
        "color": hex_colour(fg),
        "eyebrow": "DISPLAYD" if roomy_top else "",
        "title": (title or TITLE) if roomy_top else "",
        "status": ("%d VIEW%s" % (len(views), "" if len(views) == 1 else "S")
                   if roomy_top else ""),
        "subtitle": "tap a tile to switch the panel view" if roomy_top else "",
        "hint_left": "ON" if rx >= 80 else "",
        "hint_right": "NEXT" if w - (rx + rw) >= 80 else "",
        "footer": "picker" if roomy_bar else "",
        "footer_right": "litehtml" if roomy_bar else "",
    }
