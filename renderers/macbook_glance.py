"""GLANCE drawing for the merged macbook feature (drawn pixels only).

Slim full-width header (macbook_layout.HDR_H): state line, focused app +
window, mouse position, and the tab-through app strip. No screenshot here;
AIM owns review. Pure apart from PIL; geometry comes from macbook_layout so
draw, tap, and region cannot drift.

The other two thirds live next door and stay importable from here, because
macbook.py and the tests import this module by name: the app-strip chip
layer in macbook_strip.py and the display-map painter in
macbook_glance_map.py.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import macbook_layout as lay
import talon_apps as ta
from macbook_glance_color import (APP_SIZE, C_DIM, C_FOCUS_BG,
                                  C_LINE, C_ROW, C_STALE, META_SIZE,
                                  MODE_COLORS, ROW_SIZE, TITLE_SIZE)
from macbook_glance_map import draw_map
from macbook_strip import _chip, _chip_label, _strip_layer
from ui import line as ui_line

TITLE_ROOM_END = lay.CHROME_L + 64 - 12  # the GLANCE tag starts at +64
CHIP_ROOM = 480          # the widest a Talon mode chip may grow
STALE_ROOM = 420         # the right-hand STALE tag's reserved width


def waiting(screen, img, draw, title, font):
    """No macbook payload yet: named waiting frame, never invented data."""
    room = screen.W - 2 * lay.PAD
    ui_line.put(draw, (lay.PAD, lay.TITLE_Y), title, font, C_DIM,
                room=TITLE_ROOM_END - lay.PAD, label="waiting title")
    ui_line.put(draw, (lay.PAD, lay.HDR_H + 40),
                "waiting for macbook feed -- run bridges/macos_state.py",
                font, (120, 120, 130), room=room, label="waiting hint")


def header(draw, screen, img, title, state, stale, apps, apps_stale,
           tab, fonts, slide=None):
    """Slim header: state, app strip, mode chip. Returns nothing.

    `tab` is the first visible chip index (window start); the steppers
    page it, they do not move a highlight. The only emphasis is the
    Mac's live focused app (filled chip, real feed state). Dead-end
    steppers draw dim. `slide` is (old_start, progress, direction) for
    one animation frame: the old window slides out as the new window
    slides in."""
    title_font, app_font, row_font, meta_font = fonts
    plain = title_font or app_font or row_font or meta_font
    focus, mouse, talon = state.get("focus") or {}, state.get("mouse") or {}, \
        state.get("talon") or {}
    shown = ui_line.put(draw, (lay.PAD, lay.TITLE_Y), title,
                        meta_font or plain, C_DIM,
                        room=TITLE_ROOM_END - lay.PAD, label="title")
    try:
        title_w = draw.textlength(shown, font=meta_font or plain)
    except Exception:
        title_w = 0
    # Talon mode chip, top-right (the retired AIM button's slot stays
    # empty: the map tap is the zoom entry, so no control lives here).
    mode = str(talon.get("mode") or "other")
    color = MODE_COLORS.get(mode, MODE_COLORS["other"])
    chip = ui_line.clip(draw, mode.upper() + (
        " + MUTED" if talon.get("muted") else ""), meta_font or plain,
        CHIP_ROOM)
    try:
        chw = draw.textlength(chip, font=meta_font or plain)
    except Exception:
        chw = 0
    mx = screen.W - lay.CHROME_R - chw - 36
    draw.rounded_rectangle([mx, lay.TITLE_Y, mx + chw + 36,
                            lay.TITLE_Y + META_SIZE + 22],
                           radius=10, fill=tuple(color))
    ui_line.put(draw, (mx + 18, lay.TITLE_Y + 8), chip, meta_font or plain,
                (10, 10, 14), room=chw + 18, label="mode chip")
    # The count rides after the title, never under the GLANCE tag or the
    # mode chip: its room is whatever the title and the chip leave.
    tag_x = lay.CHROME_L + 64
    ui_line.put(draw, (tag_x, lay.TITLE_Y), "GLANCE",
                meta_font or plain, C_DIM, label="GLANCE tag")
    tag_w = draw.textlength("GLANCE", font=meta_font or plain)
    count_x = max(lay.PAD + title_w, tag_x + tag_w) + 24
    # a stale app list says so here, where the count is (not over the chips)
    ui_line.put(draw, (count_x, lay.TITLE_Y),
                "apps (%d)%s" % (len(apps), " STALE" if apps_stale else ""),
                meta_font or plain, C_STALE if apps_stale else C_ROW,
                room=mx - 24 - count_x, label="apps count")
    # State line: focused app, window, mouse, capture hint.
    app_name = ta.clean(focus.get("app_name")) or "unknown"
    line = app_name
    window_title = focus.get("window_title")
    if window_title:
        line += " -- " + str(window_title).splitlines()[0]
    elif state.get("accessibility_trusted") is False:
        line += " -- app only (accessibility off?)"
    elif not focus.get("window_bounds"):
        line += " -- app only (no focused window)"
    tail = ""
    if isinstance(mouse.get("x"), (int, float)) and \
            isinstance(mouse.get("y"), (int, float)):
        tail = "   @ (%d, %d)" % (mouse["x"], mouse["y"])
    # The mouse position is the part that must survive: the app and window
    # are cut to what it leaves, and the STALE tag owns the right edge.
    line_room = screen.W - 2 * lay.PAD - (STALE_ROOM + 24 if stale else 0)
    try:
        tail_w = draw.textlength(tail, font=row_font or plain)
    except Exception:
        tail_w = 0
    head = ui_line.clip(draw, line, row_font or plain, line_room - tail_w)
    ui_line.put(draw, (lay.PAD, lay.SUB_Y), head + tail, row_font or plain,
                C_ROW, room=line_room, label="state line")
    if stale:
        ui_line.put(draw, (screen.W - lay.PAD - STALE_ROOM, lay.SUB_Y),
                    "STALE -- feed quiet >3s", row_font or plain, C_STALE,
                    room=STALE_ROOM, label="stale tag")
    # App strip: steppers, visible window, focus marker. The highlight
    # is gone: it selected nothing. A dead-end stepper draws dim so a
    # press that would do nothing looks like it.
    total = len(apps)
    start = lay.page_start(tab, total)
    at_first, at_last = lay.page_bounds(start, total)
    lx, _, _, _ = lay.stepper_rect("left", screen.W)
    draw.text((lx + 24, lay.STRIP_Y + 10), "<", font=row_font or plain,
              fill=C_DIM if at_first else C_ROW)
    rx, _, _, _ = lay.stepper_rect("right", screen.W)
    draw.text((rx + 24, lay.STRIP_Y + 10), ">", font=row_font or plain,
              fill=C_DIM if at_last else C_ROW)
    ax, ay, aw, ah = lay.chip_area(screen.W)
    if not apps:
        ui_line.put(draw, (ax, lay.STRIP_Y + 10),
                    "no running apps in feed" if not apps_stale else
                    "apps feed quiet >30s -- last known lost",
                    row_font or plain, C_STALE if apps_stale else C_DIM,
                    room=aw, label="no apps")
    else:
        _, slots = lay.page_slots(start, total)
        focused = ta.clean((state.get("focus") or {}).get("app_name"))
        font = row_font or plain
        animated = False
        if slide is not None and img is not None and total > 0:
            try:
                old_start, progress, direction = slide
                progress = max(0.0, min(1.0, float(progress)))
                direction = 1 if int(direction) >= 0 else -1
            except (TypeError, ValueError):
                old_start, progress, direction = start, 1.0, 1
            old_start = lay.page_start(old_start, total)
            if old_start != start:
                _, old_slots = lay.page_slots(old_start, total)
                # Chip width matches chip_rect (uniform across slots).
                _, _, ccw, _ = lay.chip_rect(0, screen.W)
                per = {}
                for index in set(old_slots) | set(slots):
                    name = apps[index] if 0 <= index < total else ""
                    per[index] = _chip_label(draw, name, font, ccw - 28)
                focus_set = {i for i in per
                             if focused and apps[i] == focused}
                travel = aw + lay.GAP
                layer = _strip_layer(
                    aw, ah, ccw, old_slots, slots, per, focus_set,
                    font, -direction * progress * travel,
                    direction * (1.0 - progress) * travel)
                img.paste(layer, (int(ax), int(ay)), layer)
                animated = True
        if not animated:
            for pos, index in enumerate(slots):
                x, y, cw, ch = lay.chip_rect(pos, screen.W)
                name = apps[index] if 0 <= index < total else ""
                _chip(draw, x, y, cw, ch,
                      _chip_label(draw, name, font, cw - 28),
                      bool(name and name == focused), font)
    draw.line([(lay.PAD, lay.HDR_H - 12),
               (screen.W - lay.PAD, lay.HDR_H - 12)],
              fill=C_LINE, width=2)
