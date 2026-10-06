"""The shell component: the band a full-panel view wears.

A panel view is a band and a body. The band is the same everywhere -- the
view's title, what it is showing, a feed-health dot with its honest age,
a rule under all three -- and so is the footer line at the bottom. Three
views hand-drew that band: ``resources_draw`` and ``services_draw`` were
byte-for-byte the same forty lines with different variable names, and
``row_draw`` was a third copy with a slightly better title fit. All three
now compose this module and own only which words go where. A view that
draws its own band is a defect this module exists to delete.

The health vocabulary belongs here for the same reason. ``cold`` /
``warm`` / ``stale`` / ``error`` is the poll-store contract
(``resources_poll``, ``services_poll``, ``row_poll``, ``beads_poll`` all
declare it), and three views each held their own map from those four
words to four colours. One map, in the palette's roles: a health word
this module has never seen is a grey dot, not a crash.

The age vocabulary does too: the buckets behind ``"12s"`` / ``"3m"`` /
``"2h"`` were implemented five times, in this module's ``age`` and in
``feed_health``, ``row_draw``, ``beads_age`` and ``activity``. Three of
those are gone; ``beads_age``'s goes with that view's migration.

Nothing here raises: a band that cannot be drawn is a missing band, never
a blank panel.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import theme
from ui import system_buttons
from ui import text as ui_text

PAD = 60          # the plain side inset: footer, and a narrow region's band
# The band's inset on a panel-wide frame. The gesture strips own the top
# corners and the home/sleep badges are painted inside them over every
# frame, so a band drawn at PAD put its title, its health dot and the
# tail of its status line *under* a badge -- measured live: "ROWING"
# rendered as "WING". Nothing owned "the space the badges leave", so the
# band now asks the badge component for it instead of guessing.
STRIP_GAP = 24    # breathing room between a badge and the band's own content
BAND_PAD = system_buttons.STRIP + STRIP_GAP
HEAD_SIZE = 72    # the title's size
HEAD_Y = 24       # the title's baseline box top
RULE_Y = 128      # the rule under the title
RULE_WIDTH = 2
DOT = 22          # the health dot's diameter
DOT_X = 22        # its inset from the band's right edge
DOT_Y = 52
STATUS_SIZE = 30  # the health status line
STATUS_GAP = 36   # the status' inset from the right edge
TITLE_GAP = 80    # room kept between a long title and the status line
FOOT_SIZE = 30    # the footer line
FOOT_Y = 56       # the footer's inset from the bottom edge
FOOT_ROOM = 200   # chars a footer falls back to when it cannot measure

# The poll-store health vocabulary -> the palette role it wears. The four
# words are declared by every poll module; the roles are theme's.
HEALTH_ROLE = {
    "cold": "muted",        # nothing has arrived yet: present, not alarming
    "warm": "ok",           # fresh data
    "stale": "attention",   # last-known value, honestly past its window
    "error": "alert",       # the source failed
}


def band_pad(screen):
    """The band's inset for this frame.

    A panel-wide frame clears the gesture strips, because the badges are
    painted inside them over every frame; a region too narrow to hold
    both strips keeps the plain ``PAD``, so a layout's band column never
    shrinks its own title to make room for badges that are not over it.
    """
    try:
        width = int(screen.W)
    except Exception:
        return BAND_PAD
    return BAND_PAD if width >= 2 * BAND_PAD + TITLE_GAP else PAD


def health_ink(health):
    """The colour a health word wears. Unknown words read as ``cold`` so a
    new store state is a grey dot rather than an exception."""
    role = HEALTH_ROLE.get(str(health if health is not None else ""), "muted")
    try:
        return theme.rgb(role)
    except Exception:
        return theme.rgb("muted")


def short_age(seconds):
    """The compact age of a sample: ``12s``, ``3m``, ``2h``, ``3d``,
    ``never``.

    The bucket rule behind every age the panel shows: the band's own
    ``updated ... ago`` line and the per-entry ages on the health
    dashboard. One copy, so retuning the buckets cannot move one surface
    and leave the other. Never raises: no age at all says ``never``
    rather than pretending to be zero.
    """
    if seconds is None:
        return "never"
    try:
        secs = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "never"
    if secs < 60:
        return "%ds" % int(secs)
    if secs < 3600:
        return "%dm" % int(secs // 60)
    if secs < 86400:
        return "%dh" % int(secs // 3600)
    return "%dd" % int(secs // 86400)


def age(updated):
    """The honest age line every polled view shows.

    No timestamp -> says so rather than inventing "0s ago"; garbage -> same.
    Built on :func:`short_age`, so the buckets have one owner.
    """
    if not updated:
        return "no data yet"
    try:
        secs = max(0.0, time.time() - float(updated))
    except (TypeError, ValueError):
        return "no data yet"
    return "updated %s ago" % short_age(secs)


def status_line(health, updated=None):
    """``"warm · updated 4s ago"``: the one spelling of the status line."""
    if health is None:
        return ""
    if updated is None:
        return str(health)
    return "%s \u00b7 %s" % (health, age(updated))


def rule(img, screen, y=RULE_Y, pad=PAD):
    """The divider under a band, in the palette's rule role."""
    try:
        return ui_text.line(img, theme.rgb("rule"), (pad, int(y)),
                            (int(screen.W) - pad, int(y)), RULE_WIDTH)
    except Exception:
        return img


def head(img, screen, title, detail="", health=None, updated=None,
         status=None, status_ink=None, pad=None, rule_y=RULE_Y):
    """The header band: title (and an optional detail), health status, rule.

    ``status`` overrides the derived ``"<health> · <age>"`` line; passing
    neither leaves the band as title plus rule. ``status_ink`` colours
    that line (a dashboard's summary says its own health in its own
    colour); the default is the palette's ``muted``. The title is fitted
    to the room the status leaves it, so a long title can never run under
    it, and ``pad`` defaults to :func:`band_pad` -- the band clears the
    badges painted over the panel's top corners.
    """
    try:
        pad = band_pad(screen) if pad is None else int(pad)
        title_text = str(title if title is not None else "")
        if detail:
            title_text = "%s  \u00b7  %s" % (title_text, detail)
        line = status if status is not None else status_line(health, updated)
        room = int(screen.W) - 2 * pad
        drawn_status = 0
        if line:
            drawn_status = ui_text.width(screen, line, STATUS_SIZE)
            room = max(TITLE_GAP, room - drawn_status - TITLE_GAP)
        ui_text.write(img, screen, (pad, HEAD_Y), title_text,
                      theme.rgb("ink"), HEAD_SIZE, bold=True, room=room)
        if line:
            ui_text.write(img, screen,
                          (int(screen.W) - pad - drawn_status - STATUS_GAP, 34),
                          line,
                          theme.rgb("muted") if status_ink is None
                          else status_ink, STATUS_SIZE)
        if health is not None:
            _dot(img, screen, health, pad)
        rule(img, screen, rule_y, pad=pad)
    except Exception:
        pass
    return img


def _dot(img, screen, health, pad=None):
    """The health dot at the band's right: filled, no outline."""
    try:
        from PIL import ImageDraw
        pad = band_pad(screen) if pad is None else int(pad)
        right = int(screen.W) - pad
        ImageDraw.Draw(img).ellipse(
            [right - DOT_X, DOT_Y, right - 2, DOT_Y + DOT - 2],
            fill=health_ink(health))
    except Exception:
        pass
    return img


def foot(img, screen, text, pad=PAD):
    """The footer line: the honest staleness note, bottom-left."""
    try:
        room = int(screen.W) - 2 * pad
        ui_text.write(img, screen, (pad, int(screen.H) - FOOT_Y), text,
                      theme.rgb("muted"), FOOT_SIZE,
                      room=room or FOOT_ROOM)
    except Exception:
        pass
    return img
