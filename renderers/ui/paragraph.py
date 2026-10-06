"""The paragraph component: a fitted multi-line block inside a rect.

``ui.text`` owns one line of type -- a face, a width, a fit. Turning that
into a *paragraph* (wrap to the column, shrink until the whole block fits
the box, read left to right with the first line emphasised) was a rule
``reload`` owned privately: its own wrap, its own shrink search, its own
three colours, and the grid-independent geometry of where a block sits
vertically. Five views had a version of "break a paragraph to a width"
(``activity``, ``beads_common``, ``macbook_strip``, the failure card, and
``reload_highlights``); this is the one that also fits the result to a box.

It lives here rather than in ``ui/text.py`` because that module is at the
250-line budget and because the two are different jobs: ``text`` measures
and draws a line, ``paragraph`` composes lines into a block. ``reload``'s
highlights summary is the first consumer; a summary block is the general
case.

The component layer's obligations hold: every colour is a ``theme`` role,
and a block that cannot be drawn returns the frame unchanged with 0 lines
drawn -- never a raise, never a blank panel.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import theme  # noqa: E402
from ui import text as ui_text  # noqa: E402

SIZE = 56           # the size to scale down from
FLOOR = 20          # below this the block was wrong for a glanced-at panel
STEP = 4            # how much one attempt shrinks by
ROWS = 6            # at most this many lines ever draw
LINE_GAP = 1.28     # line height as a fraction of the size


def paragraph(img, screen, rect, text, size=SIZE, floor=FLOOR, step=STEP,
              rows=ROWS, ink=None, first_ink=None, bold_first=True,
              family=None):
    """Draw ``text`` left-aligned inside ``rect``; return the lines drawn.

    The block wraps to the rect's width at the largest size <= ``size``
    (and >= ``floor``) whose lines all fit across and whose whole block
    fits down, then centres vertically and reads left to right. The first
    line is emphasised -- a summary's subject -- and the rest wear ``ink``
    (``muted-soft`` by default). Returns 0 when even ``floor`` does not
    fit, and never raises: a failure is a missing block, not a crash.

    ``rows`` bounds the block even when the text is longer than the rect
    can show (``None`` keeps every line), so a caller's own bounds -- the
    highlights sanitiser -- are never the only thing standing between the
    panel and a wall of text.
    """
    try:
        if img is None:
            return 0
        x0, y0, x1, y1 = (int(v) for v in rect)
        room_w, room_h = max(1, x1 - x0), max(1, y1 - y0)
        body = str(text if text is not None else "")
        if not body.strip():
            return 0
        low, drawn = max(ui_text.FLOOR, int(floor)), max(ui_text.FLOOR,
                                                        int(size))
        lines, line_h = [], 1
        while drawn >= low and not lines:
            for i, para in enumerate(body.splitlines()):
                lines.extend(ui_text.wrap(screen, para, drawn, room_w,
                                          rows=None,
                                          bold=bold_first and i == 0,
                                          family=family))
            if rows:
                lines = lines[:max(1, int(rows))]
            wide = max([ui_text.width(screen, ln, drawn,
                                      bold=bold_first and i == 0,
                                      family=family)
                        for i, ln in enumerate(lines)] or [0])
            line_h = max(1, int(drawn * LINE_GAP))
            if wide > room_w or len(lines) * line_h > room_h:
                drawn, lines = drawn - max(1, int(step)), []
        if not lines:
            return 0
        top = theme.rgb("ink-strong") if first_ink is None else first_ink
        rest = theme.rgb("muted-soft") if ink is None else ink
        y = y0 + (room_h - len(lines) * line_h) // 2
        for i, line in enumerate(lines):
            head = bold_first and i == 0
            ui_text.write(img, screen, (x0, y), line, top if head else rest,
                          drawn, bold=head, room=room_w, family=family,
                          anchor="la")
            y += line_h
        return len(lines)
    except Exception:
        return 0
