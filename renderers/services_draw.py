"""Frame rendering for the services renderer.

Single concept: draw the cached poll snapshot to the panel. A poll never
blocks a draw, and a cold start (first poll not back yet) renders a
sensible waiting frame -- never blank, never broken.

Presentation only, and every pixel comes from the component layer: the
band and footer are ``ui.shell``, the tallies, headings and list rows are
``ui.stat``. This module owns the inventory's shape on the wall and
nothing else -- it holds no colour, no font and no truncation rule of its
own, because those used to be this module's second copy of
``resources_draw``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services_poll import _get_state
import theme
from ui import shell, stat

TALLY_Y = 150        # the count above its label
TALLY_LABEL_Y = 300
TRACKED_Y = 372
RULE_Y = 440
FAILED_Y = 466       # the FAILED heading, and the no-failures line
FAILED_STEP = 60
UNIT_STEP = 56
SERVICE_STEP = 56    # one watched service row
CHIP_GAP = 60        # between two watched services on one line
CONTAINER_STEP = 58
PORTS_STEP = 56
COLD_Y = 220
COLD_SUB_Y = 300
COLD_URL_Y = 360
COLD_ERROR_Y = 430

# A watched service's state -> its palette role. The inventory's three
# words (up/down/failed) plus anything unexpected (unknown) make a fourth.
STATE_ROLE = {"up": "ok", "failed": "alert", "unknown": "attention"}


def _state_ink(state):
    return theme.rgb(STATE_ROLE.get(str(state), "attention"))


def _short_name(unit):
    for suffix in (".service",):
        if unit.endswith(suffix):
            return unit[: -len(suffix)]
    return unit


def _cold(img, screen, url, health, error):
    """No snapshot yet: say so plainly rather than showing an empty column."""
    if health == "error":
        big, ink = "SOURCE NOT ANSWERING", theme.rgb("alert")
        sub = "lnx-viz inventory unreachable"
    else:
        big, ink = "waiting for first poll", theme.rgb("muted")
        sub = "fetching lnx-viz inventory"
    col_w = screen.W - 2 * shell.PAD
    stat.body(img, screen, (shell.PAD, COLD_Y), big, ink=ink,
              size=stat.LABEL_SIZE, bold=True, room=col_w)
    stat.body(img, screen, (shell.PAD, COLD_SUB_Y), sub,
              size=stat.ROW_SIZE, room=col_w)
    stat.body(img, screen, (shell.PAD, COLD_URL_Y), url, room=col_w)
    if error:
        stat.body(img, screen, (shell.PAD, COLD_ERROR_Y),
                  "last error: " + error, ink=theme.rgb("alert"),
                  room=col_w)
    return img


def _failed_units(img, screen, snap, y, col_w):
    """Failed units by name -- the thing the captain actually needs."""
    if snap["failed_units"]:
        stat.label(img, screen, (shell.PAD, y), "FAILED",
                   ink=theme.rgb("alert"))
        y += FAILED_STEP
        for unit in snap["failed_units"][:3]:
            stat.body(img, screen, (shell.PAD, y), "\u25cf " + unit,
                      ink=theme.rgb("alert"), size=stat.ROW_SIZE, room=col_w)
            y += UNIT_STEP
    else:
        stat.body(img, screen, (shell.PAD, y), "no failed units",
                  size=stat.ROW_SIZE, room=col_w)
        y += UNIT_STEP
    return y


def _watched(img, screen, snap, y, col_w):
    """Watched services: dots, never a table. Wraps on the panel's width."""
    y += 10
    x = shell.PAD
    for item in snap["watched"][:4]:
        text = "\u25cf %s" % _short_name(item["name"])
        wide = stat.width(screen, text, stat.ROW_SIZE)
        if x + wide > screen.W - shell.PAD and x > shell.PAD:
            x = shell.PAD
            y += SERVICE_STEP
        stat.body(img, screen, (x, y), text, ink=_state_ink(item["state"]),
                  size=stat.ROW_SIZE)
        x += wide + CHIP_GAP
    return y + 62


def _containers(img, screen, snap, y):
    """Containers: one segment per container, green when running."""
    if not snap["containers"]:
        return y
    x = shell.PAD
    head_w = stat.width(screen, "CONTAINERS  ", stat.ROW_SIZE)
    stat.body(img, screen, (x, y), "CONTAINERS", size=stat.ROW_SIZE)
    x += head_w
    for container in snap["containers"][:4]:
        running = container["state"] == "running"
        seg = "%s %s (%s)" % ("\u25cf" if running else "\u25cb",
                              container["name"], container["state"])
        wide = stat.width(screen, seg + "   ", stat.ROW_SIZE)
        if x + wide > screen.W - shell.PAD and x > shell.PAD + head_w:
            break  # wall space is finite: show fewer, never truncate
        stat.body(img, screen, (x, y), seg,
                  ink=theme.rgb("ok") if running else theme.rgb("attention"),
                  size=stat.ROW_SIZE)
        x += wide
    return y + CONTAINER_STEP


def _ports(img, screen, snap, y, col_w):
    """Listening ports: drop whole entries until the line fits a cut word
    helps nobody, fewer complete entries do."""
    shown = list(snap["ports"])
    line = ""
    while shown:
        line = "PORTS  " + "   ".join(
            "%d/%s" % (port["port"], port["process"]) for port in shown)
        if stat.width(screen, line, stat.BODY_SIZE) <= col_w:
            break
        shown.pop()
        line = ""
    if line:
        stat.body(img, screen, (shell.PAD, y), line, room=col_w)
    return y + PORTS_STEP


def _draw(screen, title, bg, url):
    _, snap, updated, health, error = _get_state()
    img = screen.new_image(bg)
    host = snap.get("hostname", "") if snap else ""
    shell.head(img, screen, title, detail=host, health=health,
               updated=updated)
    col_w = screen.W - 2 * shell.PAD

    if snap is None:
        _cold(img, screen, url, health, error)
        screen.present(img)
        return

    # Tally strip: three glanceable counts.
    third = col_w / 3.0
    tallies = (("UP", snap["up"], theme.rgb("ok")),
               ("DOWN", snap["down"], theme.rgb("muted")),
               ("FAILED", snap["failed"],
                theme.rgb("alert") if snap["failed"] else theme.rgb("muted")))
    for idx, (name, count, ink) in enumerate(tallies):
        x = int(shell.PAD + idx * third)
        stat.value(img, screen, (x, TALLY_Y), "%d" % count, ink=ink,
                   size=stat.COUNT_SIZE)
        stat.label(img, screen, (x + 6, TALLY_LABEL_Y), name,
                   ink=theme.rgb("ink"))
    stat.body(img, screen, (shell.PAD, TRACKED_Y),
              "%d services tracked" % snap["total"], room=col_w)
    stat.rule(img, screen, RULE_Y)

    y = _failed_units(img, screen, snap, FAILED_Y, col_w)
    y = _watched(img, screen, snap, y, col_w)
    stat.rule(img, screen, y)
    y = _containers(img, screen, snap, y + 26)
    _ports(img, screen, snap, y, col_w)

    # Footer: host uptime, and the poll error when stale (honest staleness).
    foot = snap["host_uptime"]
    if health in ("stale", "error") and error:
        foot += "   [inventory poll failed: %s]" % error
    shell.foot(img, screen, foot)
    screen.present(img)
