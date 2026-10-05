"""View-selection screen for the lnx-server jumbotron.

The tap-anywhere target: a tap that no configured touch region consumes
routes here (see touch.py ``tap_options``), so every fullscreen view has a
tap path to a screen that names the way back. Selection itself happens on
the phone-first control page (``GET /`` one-tap grid) or via ``POST /show``
-- the panel has no per-pixel buttons because touch regions are
host-configured and global, not per-view. This screen therefore shows the
fastest picks plus the return path, and never traps the user: a tap while
already here simply re-shows this view.

The screen is drawn by the litehtml engine from
``html-templates/options.html``: the shared panel chrome (header bands,
side gesture strips, footer) plus a name layer the renderer positions at
the rectangles ``_options_grid.grid_geometry()`` derives. The layout --
header, sub-header, two-column name grid, footer -- is the template's,
and the pixel behaviour the old Pillow loop had, so this renderer owns
the view contract and the failure card only. See docs/HTML_RENDERER.md.

A sibling of ``picker.py``: options NAMES the picks, the picker SELECTS;
either can target the other without trapping.

Isolated by design: this renderer reads its own selection params only and
never touches the policy clock, the playlist, feeds, or any other view.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _html_error
import _html_native
import _html_templates as templates
import _options_grid as grid

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
                      "(clock, chat, row, stream); malformed entries "
                      "skipped, and only the first MAX_CELLS are shown"},
    "background": {"type": "string",
                   "help": "background colour, default near-black"},
    "color": {"type": "string",
              "help": "primary text colour, default white"},
}

DEFAULT_TITLE = "OPTIONS"
DEFAULT_INSTRUCTIONS = "tap reached options \u2014 pick a view on the control page"
DEFAULT_VIEWS = ("clock", "chat", "row", "stream")
FOOTER = "control page one-tap grid \u00b7 POST /show \u00b7 playlist: POST /playlist/resume"

TEMPLATE = "options.html"
BUILD_HINT = "build it: tools/build_litehtml.sh"
DIM = (140, 160, 190)


def coerce_views(params):
    """Parse the views param into a clean list of names.

    Missing/empty falls back to DEFAULT_VIEWS; non-string, blank, and
    slash-containing entries are skipped (feed/renderer names are plain),
    and the list is truncated at ``grid.MAX_CELLS`` so the drawn cells and
    the count in the header always agree. Never raises on bad user input --
    worst case is the default four."""
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
    return (cleaned or list(DEFAULT_VIEWS))[:grid.MAX_CELLS]


def options_regions(views=None):
    """No per-view hit regions: options NAMES picks, the picker SELECTS.

    Kept as an explicit empty answer rather than nothing at all, so a
    caller (TOUCH.md's wiring, a test, an audit) asking "what does a tap
    here do?" gets a documented [] instead of an AttributeError, and the
    non-selecting nature of this surface is stated in code.
    """
    return []


def draw(screen, views, geometry, bg, fg, dim, accent,
         title=DEFAULT_TITLE, instructions=DEFAULT_INSTRUCTIONS):
    """One complete frame: the chrome plus the name layer, rendered by
    litehtml from html-templates/options.html. Never raises: a failure
    here is a card, never a blank panel."""
    try:
        document, root = templates.load(
            TEMPLATE,
            grid.chrome(views, bg, fg, dim, accent, title, instructions,
                        FOOTER),
            raw={"names": grid.name_markup(
                views, geometry, fg,
                min(grid.MAX_NAME_PX, max(12, screen.H // 12)))})
        image, _height = _html_native.render(
            document, screen.W, screen.H, background=tuple(bg), root=root)
        canvas = screen.new_image(bg)
        canvas.paste(image, (0, 0))
        return canvas
    except templates.TemplateError as err:
        return _html_error.error_frame(screen, "options: " + str(err),
                                       "fix the template, then re-show")
    except _html_native.NativeMissing as err:
        return _html_error.error_frame(screen, "options: " + str(err),
                                       BUILD_HINT)
    except _html_native.HtmlRenderError as err:
        return _html_error.error_frame(screen, "options: " + str(err),
                                       "template parsed but would not draw")
    except Exception as err:  # never a blank panel, whatever happens
        return _html_error.error_frame(screen, "options: %s" % err,
                                       "the options screen could not draw")


def run(screen, params, stop):
    params = params or {}
    views = coerce_views(params)
    bg = screen.color(params.get("background"), (8, 10, 16))
    fg = screen.color(params.get("color"), (255, 255, 255))
    accent = screen.color(ACCENT, (156, 200, 255))
    geometry = grid.grid_geometry(grid.grid_rect(screen.W, screen.H),
                                  len(views))
    screen.present(draw(screen, views, geometry, bg, fg, DIM, accent,
                        str(params.get("title") or DEFAULT_TITLE),
                        str(params.get("instructions")
                            or DEFAULT_INSTRUCTIONS)))