"""Frame rendering for the rowing-streak view. Single concept: draw.

Presentation only, and every pixel comes from the component layer: the
band (title, health dot, honest age line, rule) and the footer are
``ui.shell``, the type steps are ``ui.stat``, and the face, measurement,
fit and hero-shrink rules are ``ui.text``. This module owns which number
goes where and nothing else.

That is the whole point of the migration this file just went through. It
used to carry its own font loader (``_font``), its own never-None wrapper
(``_font_or_default``), its own truncation rule (``_fit``), its own age
buckets (``_age``), its own health-word colour map and its own band --
the same forty lines ``resources_draw`` and ``services_draw`` drew, and
the same five rules ``ui.text``/``ui.shell``/``ui.stat`` now own. A view
that draws its own band is a defect the component layer exists to delete.

Four deliberate pixel changes fall out of that, all of them the same
one-owner move:

- the page/rule/text greys are the palette's roles (``page``, ``rule``,
  ``ink``, ``muted``) rather than this view's near-identical literals;
- the live hero wears the view's palette slot accent
  (``theme.accent_rgb("row")``), the colour its progress bar and picker
  tile already use, instead of a view-local orange;
- the health dot and the status line are ``ui.shell.HEALTH_ROLE``, so the
  four poll words map to ``muted``/``ok``/``attention``/``alert`` here
  exactly as they do on every other polled view;
- an age past a day reads in days (``shell.age``), not in ever-growing
  hours.

The caller (``row.py``) passes only the frame, the subject words and the
palette role an error wears; nothing here raises, and a draw that fails
leaves the last good frame on the panel.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import theme
from ui import shell, stat
from ui import text as ui_text

BIG_SIZE = 300        # the hero number's ceiling
HERO_ROOM = 0.55      # fraction of the column the hero may use before shrinking
HERO_FLOOR = 60       # ... and the smallest size it shrinks to
HERO_STEP = 20        # how coarsely it shrinks
ROW_SIZE = 48         # the "last row" and "year pace" lines

# The type scale is declared by the components, not restated here.
HEADER_SIZE = shell.HEAD_SIZE
LABEL_SIZE = stat.LABEL_SIZE
FOOT_SIZE = shell.FOOT_SIZE
PAD = shell.PAD
HERO_Y = 150          # the hero's box top
LABEL_Y = 200         # "DAY STREAK" beside it
BANK_Y = 280          # rows/bank under the label
DIVIDER_Y = 560       # between the hero and the glanceable lines
LAST_Y = 586          # "last row <date>"
PACE_Y = 656          # the year-pace line
STATUS_Y = 726        # the streak status line
MESSAGE_Y = 220       # the waiting/error card's big word
NOTE_Y = 300          # ... and its note under it


def _draw_message(screen, title, bg, big, sub, foot, color):
    """The waiting/error card: band, one big coloured word, a note."""
    img = screen.new_image(bg)
    col_w = screen.W - 2 * PAD
    shell.head(img, screen, title)
    ui_text.write(img, screen, (PAD, MESSAGE_Y), big, color, LABEL_SIZE,
                  bold=True, room=col_w)
    if sub:
        ui_text.write(img, screen, (PAD, NOTE_Y), sub, theme.rgb("muted"),
                      ROW_SIZE, room=col_w)
    if foot:
        shell.foot(img, screen, foot)
    screen.present(img)


def _draw(screen, title, bg, snap, label, health, error, updated):
    """The streak frame: the hero number, its label, and the glance lines."""
    img = screen.new_image(bg)
    col_w = screen.W - 2 * PAD
    shell.head(img, screen, title, health=health, updated=updated)

    ds, rs, bank = snap["day_streak"], snap["row_streak"], snap["bank"]

    # Hero: the day streak, wearing the view's accent while it is alive.
    hero = "%d" % ds
    hero_size = ui_text.fit_size(screen, hero, BIG_SIZE, col_w * HERO_ROOM,
                                 floor=HERO_FLOOR, step=HERO_STEP, bold=True)
    hero_ink = theme.accent_rgb("row") if ds > 0 else theme.rgb("muted")
    ui_text.write(img, screen, (PAD, HERO_Y), hero, hero_ink, hero_size,
                  bold=True)
    hero_w = ui_text.width(screen, hero, hero_size, bold=True)
    beside = PAD + hero_w + 40
    stat.label(img, screen, (beside, LABEL_Y), "DAY STREAK",
               ink=theme.rgb("ink"), room=col_w - hero_w - 40)
    stat.body(img, screen, (beside, BANK_Y),
              "%d rows \u00b7 bank %d" % (rs, bank), size=ROW_SIZE,
              room=col_w - hero_w - 40)

    shell.rule(img, screen, DIVIDER_Y)

    # Last row + year pace: the glanceable second line.
    last = snap["last_day"] or "\u2014"
    stat.body(img, screen, (PAD, LAST_Y), "last row  %s" % last,
              ink=theme.rgb("ink"), size=ROW_SIZE, room=col_w)

    pace = snap["pace"]
    if pace > 0:
        pace_text = "year %d/%d \u00b7 %d ahead of pace" % (
            snap["rows_year"], snap["days_in_year"], pace)
        pace_ink = theme.rgb("ok")
    elif pace < 0:
        pace_text = "year %d/%d \u00b7 %d behind pace" % (
            snap["rows_year"], snap["days_in_year"], -pace)
        pace_ink = theme.rgb("attention")
    else:
        pace_text = "year %d/%d \u00b7 on pace" % (snap["rows_year"],
                                                   snap["days_in_year"])
        pace_ink = theme.rgb("ink")
    stat.body(img, screen, (PAD, PACE_Y), pace_text, ink=pace_ink,
              size=ROW_SIZE, room=col_w)
    stat.body(img, screen, (PAD, STATUS_Y), snap["status"],
              size=stat.BODY_SIZE, room=col_w)

    foot = label or "no log configured"
    if health == "stale":
        foot += "   [STALE \u2014 last-known streak]"
    if health in ("stale", "error") and error:
        foot += "   [read failed: %s]" % error
    shell.foot(img, screen, foot)
    screen.present(img)


def _snapshot_key(snap, health):
    if snap is None:
        return ("cold", health)
    return (snap["day_streak"], snap["row_streak"], snap["bank"],
            snap["last_ts"], snap["rows_year"], health)
