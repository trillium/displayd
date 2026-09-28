"""MacBook screen helper for the jumbotron: what is happening on the Mac.

Fed by the Mac-side poller (``bridges/macos_state.py``) through
``POST /feed/macbook/state`` (one polled-state document, buffer 1 --
latest only). Draws the frontmost app, focused window title, Talon mode,
and a two-screen map with the focused-window rectangle and the pointer
dotted in panel space. Observed, never interactive: a draw never blocks
on the feed ("a stale frame beats a pause").

Degraded, never invented: no payload yet -> waiting frame; payload older
than STALE_AFTER -> amber STALE tag on the last frame; app without window
fields -> app-only frame (Accessibility off or no focused window).
"""

import textwrap
import time

from PIL import ImageDraw, ImageFont

import macbook_map

NAME = "macbook"
DESCRIPTION = "MacBook state: frontmost app, focused window, screens, Talon mode"
STATIC = False
ACCENT = "#4DA3FF"
PARAMS = {
    "title": {"type": "string", "help": "header text, default MACBOOK"},
    "background": {"type": "string", "help": "background colour"},
}
INPUTS = {
    "state": {
        "type": "object",
        "help": "macOS state document (see bridges/macos_state.py)",
        "required": ["ts", "focus"],
        "properties": {
            "ts": {"type": "number"},
            "accessibility_trusted": {"type": "boolean"},
            "focus": {
                "type": "object",
                "required": ["app_name"],
                "properties": {
                    "app_name": {"type": "string"},
                    "bundle_id": {"type": "string"},
                    "pid": {"type": "number"},
                    "window_title": {"type": "string"},
                    "window_bounds": {"type": "object"},
                    "display_index": {"type": "number"},
                },
            },
            "mouse": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "display_index": {"type": "number"},
                },
            },
            "displays": {"type": "array"},
            "talon": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string"},
                    "microphone": {"type": "string"},
                    "muted": {"type": "boolean"},
                },
            },
        },
        "buffer": 1,
    },
}

POLL = 0.25
STALE_AFTER = 3.0
PAD = 48
HEADER_H = 210
MAP_TOP = 250
APP_SIZE = 72
TITLE_SIZE = 40
META_SIZE = 34

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


def _font(screen, name, size):
    path = screen.font_path(name)
    return ImageFont.truetype(path, size) if path else None


def _latest(screen):
    states = [s for s in screen.get_input("macbook", "state")
              if isinstance(s, dict)]
    return states[-1] if states else None


def _wrap(draw, text, font, max_w, rows=2, width=60):
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


def _key(state):
    """Redraw identity: app/title/display/mode/pointer-cell/staleness."""
    if not state:
        return (None,)
    focus, mouse, talon = state.get("focus") or {}, state.get("mouse") or {}, \
        state.get("talon") or {}
    cell = None
    if isinstance(mouse.get("x"), (int, float)):
        cell = (int(mouse["x"] // 6), int(mouse.get("y", 0) // 6))
    stale = time.time() - state.get("ts", 0) > STALE_AFTER
    return (focus.get("app_name"), focus.get("window_title"),
            focus.get("display_index"), cell, talon.get("mode"),
            talon.get("muted"), stale)


def _draw_header(draw, screen, title, state, stale, fonts):
    app_font, title_font, meta_font = fonts
    plain = app_font or title_font or meta_font
    draw.text((PAD, 26), title, font=plain, fill=(255, 255, 255))
    if state is None:
        return HEADER_H
    focus, talon = state.get("focus") or {}, state.get("talon") or {}
    app_name = str(focus.get("app_name") or "unknown")
    draw.text((PAD, 70), app_name, font=app_font or plain,
              fill=(255, 255, 255))
    mode = str(talon.get("mode") or "other")
    color = MODE_COLORS.get(mode, MODE_COLORS["other"])
    chip = mode.upper() + (" + MUTED" if talon.get("muted") else "")
    try:
        w = draw.textlength(chip, font=meta_font or plain)
    except Exception:
        w = 0
    draw.rounded_rectangle([screen.W - PAD - w - 36, 70,
                            screen.W - PAD, 70 + META_SIZE + 28],
                           radius=10, fill=tuple(color))
    draw.text((screen.W - PAD - w - 18, 78), chip,
              font=meta_font or plain, fill=(10, 10, 14))
    y = 70 + APP_SIZE + 10
    window_title = focus.get("window_title")
    if window_title:
        for row in _wrap(draw, window_title, title_font or plain,
                         screen.W - 2 * PAD):
            draw.text((PAD, y), row, font=title_font or plain,
                      fill=(225, 225, 232))
            y += TITLE_SIZE + 6
    elif state.get("accessibility_trusted") is False:
        draw.text((PAD, y), "app only -- accessibility off?",
                  font=meta_font or plain, fill=C_DIM)
    else:
        draw.text((PAD, y), "app only -- no focused window",
                  font=meta_font or plain, fill=C_DIM)
    if stale:
        draw.text((PAD, y + TITLE_SIZE + 10), "STALE -- feed quiet >3s",
                  font=meta_font or plain, fill=C_STALE)
    draw.line([(PAD, HEADER_H - 14), (screen.W - PAD, HEADER_H - 14)],
              fill=C_LINE, width=2)
    return HEADER_H


def _draw_map(draw, screen, state, fonts):
    _, _, meta_font = fonts
    plain = meta_font
    displays = state.get("displays") or []
    box = macbook_map.union(displays)
    if box is None:
        draw.text((PAD, MAP_TOP + 20), "no display geometry in feed",
                  font=plain, fill=C_DIM)
        return
    area_w, area_h = screen.W - 2 * PAD, screen.H - MAP_TOP - PAD
    scale, ox, oy = macbook_map.fit(box, area_w, area_h)
    oy += MAP_TOP
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
        px, py = macbook_map.project(mouse["x"], mouse["y"], scale, ox, oy)
        draw.ellipse([px - 9, py - 9, px + 9, py + 9],
                     fill=(255, 255, 255), outline=(0, 0, 0), width=2)


def _draw(screen, title, state, stale, bg):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    fonts = (_font(screen, "DejaVuSans-Bold", APP_SIZE),
             _font(screen, "DejaVuSans", TITLE_SIZE),
             _font(screen, "DejaVuSans", META_SIZE))
    if state is None:
        plain = fonts[0] or fonts[1] or fonts[2]
        draw.text((PAD, 26), title, font=plain, fill=(255, 255, 255))
        draw.text((PAD, HEADER_H + 40), "waiting for macbook feed -- run bridges/macos_state.py", font=fonts[2] or plain, fill=(120, 120, 130))
        return img
    _draw_header(draw, screen, title, state, stale, fonts)
    _draw_map(draw, screen, state, fonts)
    return img


def run(screen, params, stop):
    title = str((params or {}).get("title") or "MACBOOK")
    bg = screen.color((params or {}).get("background"), (10, 10, 14))
    last_key = None
    while not stop.is_set():
        state = _latest(screen)
        stale = bool(state) and \
            time.time() - state.get("ts", 0) > STALE_AFTER
        key = (_key(state), stale)
        if key != last_key:
            last_key = key
            try:
                screen.present(_draw(screen, title, state, stale, bg))
            except Exception:
                pass
        stop.wait(POLL)
