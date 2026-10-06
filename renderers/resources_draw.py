"""Frame rendering for the resources renderer.

Single concept: draw the cached poll snapshot to the panel. A poll never
blocks a draw, and a cold start (first poll not back yet) renders a
sensible waiting frame -- never blank, never broken.

Presentation only, and every pixel comes from the component layer: the
band and footer are ``ui.shell``, the label/value rows, supporting lines
and meters are ``ui.stat``. This module owns which number goes where and
nothing else -- it holds no colour, no font, no drawing primitive, and no
truncation rule of its own, because those are the things that used to be
duplicated between this module and ``services_draw``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from resources_poll import _get_state
import theme
from ui import shell, stat

CPU_Y = 170       # the first row's top
MEM_Y = 450       # after the CPU rule
DISK_Y = 740      # after the memory rule
ROW_RULE_1 = 420
ROW_RULE_2 = 710
DISK_STEP = 68    # one mount row
DISK_BAR_W = 520  # the disk meter's width, flush right
DISK_BAR_H = 40
MEM_BAR_X = 1300  # the memory meter starts here
MEM_BAR_H = 44
DETAIL_X = 520    # the load line sits beside the CPU value
WAITING_Y = 260
ERROR_Y = 340


def _gb(n):
    return "%.1f" % (float(n) / (1024 ** 3))


def _uptime(s):
    s = max(0, int(s or 0))
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    mins, _ = divmod(s, 60)
    if days:
        return "up %dd %dh" % (days, hours)
    if hours:
        return "up %dh %dm" % (hours, mins)
    if mins:
        return "up %dm" % mins
    return "up %ds" % s


def _cpu_ink(pct):
    """The CPU value's colour: the palette's state roles, at two thresholds."""
    if pct is None:
        return theme.rgb("muted")
    if pct < 70:
        return theme.rgb("ok")
    if pct < 90:
        return theme.rgb("attention")
    return theme.rgb("alert")


def _used_ink(frac, warn=0.8, bad=0.93):
    """A used-fraction's colour. Two thresholds, one rule, both rows."""
    try:
        frac = float(frac)
    except (TypeError, ValueError):
        return theme.rgb("muted")
    if frac < warn:
        return theme.rgb("ok")
    if frac < bad:
        return theme.rgb("attention")
    return theme.rgb("alert")


def _draw(screen, title, bg):
    _, snap, updated, health, error = _get_state()
    img = screen.new_image(bg)
    host = (snap or {}).get("hostname", "") if snap else ""
    shell.head(img, screen, title, detail=host, health=health,
               updated=updated)
    col_w = screen.W - 2 * shell.PAD

    if snap is None:
        msg = "waiting for first poll \u2014 reading /proc" \
            if health == "cold" else "no data: last poll failed"
        stat.body(img, screen, (shell.PAD, WAITING_Y), msg,
                  size=stat.LABEL_SIZE, bold=True, room=col_w)
        if error:
            stat.body(img, screen, (shell.PAD, ERROR_Y),
                      "last error: " + error, ink=theme.rgb("alert"),
                      size=stat.ROW_SIZE, room=col_w)
        screen.present(img)
        return

    # Row 1: CPU -- the giant number. Load matters on this host, so it gets
    # the sub-line in full: 1/5/15 plus core count and thread pressure.
    pct = snap["cpu_pct"]
    cpu_big = "sampling\u2026" if pct is None else "%d%%" % int(round(pct))
    stat.row(img, screen, (shell.PAD, CPU_Y), "CPU", cpu_big,
             value_ink=_cpu_ink(pct))
    l1, l5, l15 = snap["load"]
    load_line = "load %.2f  %.2f  %.2f   \u00b7   %d cores   \u00b7   %s procs" % (
        l1, l5, l15, snap["ncpu"], snap["procs"])
    stat.body(img, screen, (shell.PAD + DETAIL_X, CPU_Y + 110), load_line,
              ink=theme.rgb("ink"), room=col_w - DETAIL_X)
    stat.rule(img, screen, ROW_RULE_1)

    # Row 2: memory + swap.
    mem_line = "%s / %s GB" % (_gb(snap["mem_used"]), _gb(snap["mem_total"]))
    mem_frac = (float(snap["mem_used"]) / snap["mem_total"]) \
        if snap["mem_total"] else 0.0
    stat.row(img, screen, (shell.PAD, MEM_Y), "MEM", mem_line,
             value_ink=theme.rgb("ink"), room=col_w - 500)
    stat.meter(img, screen,
               (shell.PAD + MEM_BAR_X, MEM_Y + 90, col_w - MEM_BAR_X,
                MEM_BAR_H), mem_frac, ink=_used_ink(mem_frac))
    if snap["swap_total"]:
        swap_line = "swap %s / %s GB" % (_gb(snap["swap_used"]),
                                         _gb(snap["swap_total"]))
    else:
        swap_line = "no swap"
    stat.body(img, screen, (shell.PAD + MEM_BAR_X, MEM_Y + 150), swap_line,
              room=col_w - MEM_BAR_X)
    stat.rule(img, screen, ROW_RULE_2)

    # Row 3: disk, one line per mount.
    stat.label(img, screen, (shell.PAD, DISK_Y), "DISK")
    y = DISK_Y + 62
    for disk in snap["disks"][:3]:  # wall space is finite: three mounts max
        if "error" in disk:
            stat.body(img, screen, (shell.PAD, y),
                      "%s: %s" % (disk["mount"], disk["error"]),
                      ink=theme.rgb("alert"), size=stat.ROW_SIZE,
                      room=col_w)
        else:
            frac = disk["pct"] / 100.0
            stat.body(img, screen, (shell.PAD, y),
                      "%s  %s / %s GB  %d%%" % (
                          disk["mount"], _gb(disk["used"]),
                          _gb(disk["total"]), int(round(disk["pct"]))),
                      ink=theme.rgb("ink"), size=stat.ROW_SIZE,
                      room=col_w - 560)
            stat.meter(img, screen,
                       (shell.PAD + col_w - DISK_BAR_W, y + 2, DISK_BAR_W,
                        DISK_BAR_H), frac, ink=_used_ink(frac, 0.8, 0.92))
        y += DISK_STEP

    # Footer: uptime, and the poll error when stale (honest staleness).
    foot = _uptime(snap["uptime_s"])
    if health in ("stale", "error") and error:
        foot += "   [last poll failed: %s]" % error
    shell.foot(img, screen, foot)
    screen.present(img)
