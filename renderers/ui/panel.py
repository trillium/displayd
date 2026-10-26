"""The panel component: a titled region with a body.

What this owns:

- ``fit_size`` -- the one rule for "these words must fit this region":
  the largest size up to the declared one whose *whole* block fits, never
  a truncation.
- ``headline`` -- the title, scaled to the region and centred.
- ``block`` -- a centred multi-line body, hung from a stated top if given.
- ``tag`` -- the corner label under the block.
- ``bar`` -- the accent band across the top of the region.
- ``card`` -- the component itself: an optional accent bar, a title
  scaled to the region, an optional body under it, an optional tag.
- ``strip`` -- the card confined to a rect of a bigger frame, for a view
  composited into a larger one.

Card and strip wear ``round_box`` corners. Nothing here raises or holds a
colour literal; a card that cannot draw is a missing card.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import theme
from ui import round_box
from ui import text as ui_text
from ui import textbox

PAD = 60            # the tag's inset from the region's own edge
BAR = 18            # the accent bar's height, across the top
FLOOR = 12          # the smallest headline worth drawing
STRIP_MARGIN = 74   # a strip's side margin, as a fraction of its width
STRIP_RULE = 24     # a strip's bar height, as a fraction of its height
MARGIN = 0.88       # the room a block may use, as a fraction of the region
SPACING_DIV = 4     # line spacing as a fraction of the size
TITLE_SIZE = 110    # a card headline
BODY_SIZE = 54      # the body under it
TAG_SIZE = 36       # the corner tag
TAG_Y = 100         # the tag's inset from the region's bottom edge
LIFT = 60           # how far a headline sits above centre with a body below
BODY_GAP = 140      # headline centre -> body top


def _room(value):
    """A region dimension as usable room; 0 means no limit on this axis."""
    try:
        return max(0.0, float(value) * MARGIN)
    except (TypeError, ValueError):
        return 0.0


def _spacing(size):
    return max(4, int(size) // SPACING_DIV)


def _box(screen, text, size, bold, family):
    """``(width, height)`` of a whole multi-line block, or None unseen."""
    try:
        from PIL import Image, ImageDraw
        handle = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        area = handle.multiline_textbbox(
            (0, 0), text,
            font=ui_text.face(screen, size, bold=bold, family=family),
            spacing=_spacing(size))
        return area[2] - area[0], area[3] - area[1]
    except Exception:
        return None


def fits(screen, text, size, room_w=0, room_h=0, bold=True, family=None):
    """True when the whole block fits the region at that size.

    ``room_w``/``room_h`` are the region's own dimensions -- ``MARGIN`` is
    applied here, so a caller states the region and nothing else. A room
    of 0 means no limit on that axis, and a block that cannot be measured
    is treated as fitting, exactly as ``ui_text.width`` reads 0 as
    unknown.
    """
    box = _box(screen, str(text if text is not None else ""), size, bold,
               family)
    if box is None:
        return True
    room_w, room_h = _room(room_w), _room(room_h)
    return ((not room_w or box[0] <= room_w)
            and (not room_h or box[1] <= room_h))


def fit_size(screen, text, size, room_w=0, room_h=0, bold=True, family=None):
    """The largest size <= ``size`` whose whole block fits the region.

    Never raises: garbage in gives the floor, an unmeasurable block keeps
    the declared size, and no room at all means the declared size stands.
    """
    out = str(text if text is not None else "")
    try:
        want = max(FLOOR, int(size))
    except (TypeError, ValueError):
        return FLOOR
    if not out.strip() or (not _room(room_w) and not _room(room_h)):
        return want
    low, high, best = FLOOR, want, None
    while low <= high:
        mid = (low + high) // 2
        if fits(screen, out, mid, room_w, room_h, bold=bold, family=family):
            best, low = mid, mid + 1
        else:
            high = mid - 1
    return best if best is not None else FLOOR


def block(img, screen, text, ink=None, size=BODY_SIZE, centre=None,
          top=False, bold=False, family=None):
    """A centred multi-line block, wrapped and shrunk to fit the region.

    ``top`` hangs the block's top edge at ``centre`` y (a card body).
    """
    try:
        from PIL import ImageDraw
        W, H = int(screen.W), int(screen.H)
        x, y = centre if centre else (W // 2, H // 2)
        # wrap to the region, then shrink until the whole block fits it
        text = "\n".join(ui_text.wrap(screen, str(text if text is not None
                         else ""), size, int(W * MARGIN), rows=None,
                         bold=bold, family=family))
        down = H - int(y) - TAG_Y if top else 2 * min(int(y), H - int(y))
        size = fit_size(screen, text, size, W, max(1, down) / MARGIN,
                        bold=bold, family=family)
        with textbox.boxed((0, 0, W, H), label="panel block"):
            ImageDraw.Draw(img).multiline_text(
                (int(x), int(y)), text,
                font=ui_text.face(screen, size, bold=bold, family=family),
                fill=theme.rgb("muted-soft") if ink is None else ink,
                spacing=_spacing(size), anchor="ma" if top else "mm",
                align="center")
    except Exception:
        pass
    return img


def headline(img, screen, text, ink=None, size=None, centre=None,
             bold=True, family=None, room_w=0, room_h=0):
    """The card's title: scaled to the region, then centred on it.

    ``size`` is the size to scale *down from* (``TITLE_SIZE`` by default),
    so a long title gets a smaller face rather than a truncated one.
    """
    try:
        wanted = TITLE_SIZE if size is None else size
        drawn = fit_size(screen, text, wanted,
                         room_w or int(screen.W), room_h or int(screen.H),
                         bold=bold, family=family)
    except Exception:
        drawn = TITLE_SIZE
    return block(img, screen, text,
                 ink=theme.rgb("ink-strong") if ink is None else ink,
                 size=drawn, centre=centre, bold=bold, family=family)


def bar(img, screen, ink=None, height=BAR, rect=None):
    """The accent band across the top of the region, or of ``rect``."""
    try:
        from PIL import ImageDraw
        x, y, w = (0, 0, int(screen.W)) if rect is None else (
            int(rect[0]), int(rect[1]), int(rect[2]))
        ImageDraw.Draw(img).rectangle(
            [x, y, x + max(1, w) - 1, y + max(1, int(height)) - 1],
            fill=theme.rgb("accent") if ink is None else ink)
    except Exception:
        pass
    return img


def tag(img, screen, text, ink=None, size=TAG_SIZE, family=None):
    """The corner label under the block: the severity, the state, the key."""
    try:
        ui_text.write(img, screen, (PAD, int(screen.H) - TAG_Y),
                      str(text if text is not None else ""),
                      theme.rgb("muted") if ink is None else ink, size,
                      bold=True, family=family)
    except Exception:
        pass
    return img


def strip(img, screen, rect, title, detail="", title_size=None,
          body_size=None, bar_ink=None, title_ink=None, body_ink=None):
    """The card confined to ``rect`` of a bigger frame, copy left to right."""
    try:
        x, y, w, h = (int(v) for v in rect)
        x0, y0, w, h = x, y, max(1, w), max(1, h)
        rule, margin = max(4, h // STRIP_RULE), max(8, w // STRIP_MARGIN)
        room = max(1, w - 2 * margin)
        bar(img, screen, theme.rgb("alert") if bar_ink is None else bar_ink,
            height=rule, rect=(x, y, w, h))
        y += rule + max(6, h // 32)
        blocks = (
            (max(14, int(title_size or h // 8)), True, title,
             theme.rgb("alert-ink") if title_ink is None else title_ink),
            (max(11, int(body_size or h // 12)), False, detail,
             theme.rgb("alert-body") if body_ink is None else body_ink))
        for size, bold, text, ink in blocks:
            for line in ui_text.wrap(screen, text, size, room, rows=None):
                if y + size > int(rect[1]) + h:
                    break
                ui_text.write(img, screen, (x + margin, y), line, ink, size,
                              bold=bold)
                y += size + _spacing(size)
        round_box.cut(img, (x0, y0, w, h), round_box.RADIUS, theme.rgb("page"))
    except Exception:
        pass
    return img


def card(img, screen, title, body="", ink=None, tag_text=None, tag_ink=None,
         accent=None, family=None, bold=True, title_size=TITLE_SIZE,
         body_size=BODY_SIZE, body_ink=None):
    """The component: a titled region with a body.

    Draws, in order: an accent ``bar`` across the top (``True`` for the
    palette accent, or the colour to use), the title scaled to the region
    and centred above the middle, an optional body under it, and an
    optional ``tag_text`` in the bottom corner. ``ink`` is the headline's
    colour -- a severity accent, an operator's chosen text colour -- and
    defaults to the palette's strongest ink. Everything is optional but
    the title, and nothing here raises.
    """
    try:
        if accent:
            bar(img, screen, theme.rgb("accent") if accent is True else accent)
        title_text = str(title if title is not None else "")
        body_text = str(body if body is not None else "")
        middle = int(screen.H) // 2 - (LIFT if body_text else 0)
        headline(img, screen, title_text, ink=ink, size=title_size,
                 centre=(int(screen.W) // 2, middle), bold=bold,
                 family=family)
        if body_text:
            block(img, screen, body_text, ink=body_ink, size=body_size,
                  family=family,
                  centre=(int(screen.W) // 2, middle + BODY_GAP), top=True)
        if tag_text:
            tag(img, screen, tag_text, ink=tag_ink, family=family)
        round_box.cut(img, (0, 0, int(screen.W), int(screen.H)),
                      round_box.RADIUS, theme.rgb("page"))
    except Exception:
        pass
    return img
