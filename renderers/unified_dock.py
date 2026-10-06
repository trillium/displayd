"""Apps-dock content for the merged home screen. NOT a renderer: no
run(), so the daemon's loader skips this file (same convention as
row_draw.py).

One module owns the dock strip so renderers/unified.py stays a thin
composition (grid via picker + dock here + chrome via overlay): the
summary (``dock_summary``, pure), the document variables
(``dock_variables``, pure) and the composite (``draw_dock``). The strip
is drawn by litehtml from ``html-templates/dock.html`` -- authored at
DESIGN, the dock rect default_dock() derives at 1920x1080, and scaled
to whatever rect the caller passed -- so there is no Pillow drawing on
this path at all.

Empty means no feed payload yet, stale means quiet past STALE_AFTER;
both render inside the strip only, so a tile never moves. Never raises:
a missing dock beats a missing frame, and a dock that cannot be drawn
says so inside its own rect instead of disappearing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image

import _html_error
import _html_native
import _html_templates as templates
import talon_apps as ta

TEMPLATE = "dock.html"
BUILD_HINT = "build it: tools/build_litehtml.sh"
DESIGN = (1760, 230)  # the size dock.html is authored at

LABEL = "MAC APPS"
HINT = "live summary -- tap: open macbook screen"
TAP = "tap dock: macbook"
EMPTY_TITLE = "waiting for talon feed"
EMPTY_BODY = "run bridges/talon_apps.py  (views above still work)"
STALE_BODY = "feed quiet >30s -- last known"
# A non-breaking space, not a plain one: HTML collapses runs of
# whitespace, so an ordinary trailing space would not separate the
# marker from the title it introduces.
STALE_MARKER = "STALE\u00a0\u00a0"

DIM = (140, 150, 175)     # secondary text
ACCENT = (110, 200, 135)  # live green: the head dot, the tap hint
ALERT = (255, 180, 80)    # stale amber


def dock_summary(state):
    """Dock content from a feed state (pure): mode/count/focus/split."""
    grouped = ta.groups(state)
    state = state if isinstance(state, dict) else {}
    raw = state.get("apps")
    apps = [ta.clean(a) for a in raw] if isinstance(raw, list) else []
    if grouped.get("mode") == "sides":
        mode = "left=D%d right=other" % ((grouped.get("left_display")
                                          or 0) + 1)
    else:
        mode = "one screen: split"
    return {"mode": mode, "count": len(apps),
            "focused": ta.clean(state.get("focused")),
            "overflow": grouped.get("overflow") or 0,
            "left": grouped.get("left_total", len(grouped.get("left", []))),
            "right": grouped.get("right_total",
                                 len(grouped.get("right", [])))}


def _main(summ):
    """The big line: how many apps, which one is focused, what is hidden."""
    text = "apps (%d)" % summ["count"]
    if summ["focused"]:
        text += "   * " + ta.label(summ["focused"])
    if summ["overflow"]:
        text += "   +%d more" % summ["overflow"]
    return text


def dock_variables(state, stale, bg=(8, 10, 16), fg=(255, 255, 255)):
    """The dock template's variables for a feed state (pure).

    Three states, one strip: no payload yet, a live payload, and a
    payload that has gone quiet. The stale one keeps the last known
    numbers and adds a marker rather than showing an empty dock, because
    "quiet" must never read as "nothing is running".
    """
    summ = dock_summary(state)
    marker = ""
    if state is None:
        title, body = EMPTY_TITLE, EMPTY_BODY
    elif stale:
        marker, title, body = STALE_MARKER, _main(summ), STALE_BODY
    else:
        title, body = _main(summ), "%d left / %d right" % (summ["left"],
                                                            summ["right"])
    colour = templates.hex_colour
    return {
        "background": colour(bg),
        "color": colour(fg),
        "eyebrow": LABEL,
        "status": summ["mode"],
        "subtitle": HINT,
        "title": title,
        "body": body,
        "footer_right": TAP,
        "marker": marker,
        "dim": colour(DIM),
        "accent": colour(ACCENT),
        "alert": colour(ALERT),
        "line": colour(ta.C_LINE),
    }


def _strip(state, stale, bg, fg):
    """The strip as its own image, at the size dock.html is authored at."""
    document, root = templates.load(
        TEMPLATE, dock_variables(state, stale, bg, fg))
    image, _height = _html_native.render(
        document, DESIGN[0], DESIGN[1], background=tuple(bg), root=root)
    return image


def _scaled(image, width, height):
    if image.size == (width, height):
        return image
    return image.resize((width, height), Image.BILINEAR)


def _fail(img, screen, rect, title, detail):
    """Say what went wrong inside the strip, leaving the rest of the
    frame exactly as it was. Never raises."""
    try:
        x, y, w, h = (int(v) for v in rect)
        card = _html_error.error_strip(screen, (x, y, w, h), title, detail)
        img.paste(card.crop((x, y, x + w, y + h)), (x, y))
    except Exception:
        pass


def draw_dock(img, screen, dock, state, stale,
              bg=(8, 10, 16), fg=(255, 255, 255)):
    """Composite the dock strip onto a finished grid frame, in place.

    Only the dock rect is touched, which is what keeps a dock that gained
    or lost its feed from disturbing the tiles above it. Returns the same
    image so the caller can chain, and never raises.
    """
    x, y, w, h = (int(v) for v in dock)
    try:
        img.paste(_scaled(_strip(state, stale, bg, fg), w, h), (x, y))
    except templates.TemplateError as err:
        _fail(img, screen, (x, y, w, h), "dock: " + str(err),
              "fix the template, then re-show")
    except _html_native.NativeMissing as err:
        _fail(img, screen, (x, y, w, h), "dock: " + str(err), BUILD_HINT)
    except _html_native.HtmlRenderError as err:
        _fail(img, screen, (x, y, w, h), "dock: " + str(err),
              "template parsed but would not draw")
    except Exception as err:  # never a blank panel, whatever happens
        _fail(img, screen, (x, y, w, h), "dock: %s" % err,
              "the dock could not draw")
    return img
