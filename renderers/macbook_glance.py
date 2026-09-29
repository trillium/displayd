"""GLANCE drawing for the merged macbook feature (drawn pixels only).

Slim full-width header (macbook_layout.HDR_H): state line, focused app +
window, mouse position, and the tab-through app strip -- then the display
map fills everything below it. No screenshot here; AIM owns review. Pure
apart from PIL; geometry comes from macbook_layout so draw, tap, and
region cannot drift.
"""

import textwrap

import macbook_layout as lay
import macbook_map
import talon_apps as ta

ACCENT = "#4DA3FF"
TITLE_SIZE = 30
APP_SIZE = 44
ROW_SIZE = 32
META_SIZE = 30

MODE_COLORS = {
    "command": (110, 220, 130),
    "dictation": (255, 200, 90),
    "mixed": (120, 200, 255),
    "sleep": (120, 120, 140),
    "other": (150, 150, 150),
}
C_STALE = (255, 180, 80)
C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)
C_ROW = (255, 255, 255)
C_TAB_BG = (38, 66, 44)
C_FOCUS = (110, 220, 130)


def _chip_label(draw, name, font, max_w):
    """Chip label fitted to the chip width: cleaned, truncated."""
    text = ta.clean(name)
    if not text:
        return "unknown"
    try:
        if draw.textlength(text, font=font) <= max_w:
            return text
        while len(text) > 1:
            text = text[:-1]
            if draw.textlength(text + "\u2026", font=font) <= max_w:
                return text + "\u2026"
        return "\u2026"
    except Exception:
        return ta.label(name)


def _wrap(draw, text, font, max_w, rows=1, width=90):
    if font is not None:
        try:
            avg = draw.textlength("0123456789", font=font) / 10.0
            width = max(12, int(max_w / max(avg, 1)))
        except Exception:
            pass
    out = []
    for para in str(text or "").splitlines() or [""]:
        out.extend(textwrap.wrap(para, width) or [""])
    return out[:rows]


def waiting(screen, img, draw, title, font):
    """No macbook payload yet: named waiting frame, never invented data."""
    draw.text((lay.PAD, lay.TITLE_Y), title, font=font, fill=C_DIM)
    draw.text((lay.PAD, lay.HDR_H + 40),
              "waiting for macbook feed -- run bridges/macos_state.py",
              font=font, fill=(120, 120, 130))


def header(draw, screen, title, state, stale, apps, apps_stale, tab,
           fonts):
    """Slim header: state, app strip, mode chip. Returns nothing."""
    title_font, app_font, row_font, meta_font = fonts
    plain = title_font or app_font or row_font or meta_font
    focus, mouse, talon = state.get("focus") or {}, state.get("mouse") or {}, \
        state.get("talon") or {}
    draw.text((lay.PAD, lay.TITLE_Y), title, font=meta_font or plain,
              fill=C_DIM)
    try:
        title_w = draw.textlength(title, font=meta_font or plain)
    except Exception:
        title_w = 0
    draw.text((lay.PAD + title_w + 24, lay.TITLE_Y),
              "apps (%d)" % len(apps), font=meta_font or plain,
              fill=C_ROW)
    # Talon mode chip, top-right next to the AIM button.
    mode = str(talon.get("mode") or "other")
    color = MODE_COLORS.get(mode, MODE_COLORS["other"])
    chip = mode.upper() + (" + MUTED" if talon.get("muted") else "")
    try:
        chw = draw.textlength(chip, font=meta_font or plain)
    except Exception:
        chw = 0
    bx, by, bw, _ = lay.mode_rect(screen.W)
    mx = bx - lay.GAP - chw - 36
    draw.rounded_rectangle([mx, lay.TITLE_Y, mx + chw + 36,
                            lay.TITLE_Y + META_SIZE + 22],
                           radius=10, fill=tuple(color))
    draw.text((mx + 18, lay.TITLE_Y + 8), chip, font=meta_font or plain,
              fill=(10, 10, 14))
    draw.text((lay.CHROME_L + 64, lay.TITLE_Y),
              "GLANCE", font=meta_font or plain, fill=C_DIM)
    # State line: focused app, window, mouse, capture hint.
    app_name = ta.clean(focus.get("app_name")) or "unknown"
    line = app_name
    window_title = focus.get("window_title")
    if window_title:
        line += " -- " + (_wrap(draw, window_title, row_font or plain,
                                  screen.W - 2 * lay.PAD) or [""])[0]
    elif state.get("accessibility_trusted") is False:
        line += " -- app only (accessibility off?)"
    elif not focus.get("window_bounds"):
        line += " -- app only (no focused window)"
    if isinstance(mouse.get("x"), (int, float)) and \
            isinstance(mouse.get("y"), (int, float)):
        line += "   @ (%d, %d)" % (mouse["x"], mouse["y"])
    draw.text((lay.PAD, lay.SUB_Y), line, font=row_font or plain,
              fill=C_ROW)
    if stale:
        draw.text((screen.W - lay.PAD - 420, lay.SUB_Y),
                  "STALE -- feed quiet >3s", font=row_font or plain,
                  fill=C_STALE)
    # App strip: steppers, visible window, tab highlight, focus marker.
    total = len(apps)
    lx, _, _, _ = lay.stepper_rect("left", screen.W)
    draw.text((lx + 24, lay.STRIP_Y + 10), "<", font=row_font or plain,
              fill=C_ROW)
    rx, _, _, _ = lay.stepper_rect("right", screen.W)
    draw.text((rx + 24, lay.STRIP_Y + 10), ">", font=row_font or plain,
              fill=C_ROW)
    ax, ay, _, _ = lay.chip_area(screen.W)
    if not apps:
        draw.text((ax, lay.STRIP_Y + 10),
                  "no running apps in feed" if not apps_stale else
                  "apps feed quiet >30s -- last known lost",
                  font=row_font or plain,
                  fill=C_STALE if apps_stale else C_DIM)
    else:
        start, slots = lay.visible_slots(tab, total)
        focused = ta.clean((state.get("focus") or {}).get("app_name"))
        for pos, index in enumerate(slots):
            x, y, cw, ch = lay.chip_rect(pos, screen.W)
            name = apps[index] if 0 <= index < total else ""
            is_tab, is_focus = (index == tab % total), (name and
                                                        name == focused)
            if is_tab:
                draw.rounded_rectangle([x, y, x + cw, y + ch],
                                       radius=10, fill=C_TAB_BG)
            else:
                draw.rounded_rectangle([x, y, x + cw, y + ch],
                                       radius=10, outline=C_LINE, width=2)
            mark = "*" if is_focus else " "
            draw.text((x + 14, y + 8),
                      mark + _chip_label(draw, name, row_font or plain,
                                         cw - 28),
                      font=row_font or plain,
                      fill=C_FOCUS if is_focus else C_ROW)
    if apps_stale and apps:
        draw.text((screen.W - lay.PAD - 300, lay.STRIP_Y + 10),
                  "apps STALE", font=row_font or plain, fill=C_STALE)
    # AIM button label (the region lives in touch config).
    draw.rounded_rectangle([bx, by, bx + bw, by + lay.MODE_H],
                           radius=10, outline=C_ROW, width=2)
    draw.text((bx + 70, by + 10), "AIM >", font=row_font or plain,
              fill=C_ROW)
    draw.line([(lay.PAD, lay.HDR_H - 12),
               (screen.W - lay.PAD, lay.HDR_H - 12)],
              fill=C_LINE, width=2)


def draw_map(img, draw, screen, state, meta_font):
    """Display map filling header..base: screens, focus rect, pointer."""
    plain = meta_font
    displays = state.get("displays") or []
    box = macbook_map.union(displays)
    if box is None:
        draw.text((lay.PAD, lay.HDR_H + 20),
                  "no display geometry in feed",
                  font=plain, fill=C_DIM)
        return
    scale, ox, oy = macbook_map.frame(box, screen.W, screen.H,
                                      top=lay.HDR_H, bottom=screen.H)
    if scale <= 0:
        return
    focus, mouse = state.get("focus") or {}, state.get("mouse") or {}
    active = focus.get("display_index")
    for i, d in enumerate(displays):
        if not isinstance(d, dict):
            continue
        r = macbook_map.rect((d.get("bounds") or {}), scale, ox, oy)
        if r is None:
            continue
        is_active = (i == active)
        draw.rectangle(r, outline=ACCENT if is_active else (90, 90, 110),
                       width=5 if is_active else 2)
        tag = macbook_map.label(i, bool(d.get("main")))
        if is_active:
            tag += " FOCUS"
        draw.text((r[0] + 10, r[1] + 8), tag, font=plain,
                  fill=(255, 255, 255) if is_active else C_DIM)
    bounds = focus.get("window_bounds")
    rect = macbook_map.rect(bounds, scale, ox, oy) \
        if isinstance(bounds, dict) else None
    if rect is not None:
        draw.rectangle(rect, outline=ACCENT, width=3)
    if isinstance(mouse.get("x"), (int, float)) and \
            isinstance(mouse.get("y"), (int, float)):
        px, py = macbook_map.project(mouse["x"], mouse["y"],
                                     scale, ox, oy)
        draw.ellipse([px - 9, py - 9, px + 9, py + 9],
                     fill=(255, 255, 255), outline=(0, 0, 0), width=2)
