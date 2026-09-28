"""Running Talon applications: tap-to-focus side buttons for the panel.

Fed by the Mac-side poller (``bridges/talon_apps.py``) through
``POST /feed/talon_apps/state`` (one document, buffer 1 -- latest only):
``{"ts", "apps": [names...], "focused": name}`` plus optional
``windows`` (per-app window centres) and ``displays`` (screen bounds)
for the side grouping. Tapping a button POSTs ``/talon/focus`` with
the tap point and the daemon maps it back through ``talon_layout``
below, so the touch action stays closed (coordinates only, never a
name). Buttons sit LEFT/RIGHT by the screen the app is on; with fewer
than two screens the list splits evenly so no side stays empty.

App names come from the OS: treated as untrusted (``clean()`` bounds
length and strips control characters) and never interpolated into a
shell anywhere on this path. Observed, never blocking: no payload yet
-> waiting frame; stale payload -> amber STALE tag on the last frame.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import ImageDraw, ImageFont

import talon_layout

NAME = "talon_apps"
DESCRIPTION = "Running apps: tap a side button to focus it"
STATIC = False
ACCENT = "#7FD08A"
PARAMS = {
    "title": {"type": "string", "help": "header text, default APPS"},
    "background": {"type": "string", "help": "background colour"},
}
INPUTS = {
    "state": {
        "type": "object",
        "help": "running-app document (see bridges/talon_apps.py)",
        "required": ["ts", "apps"],
        "properties": {
            "ts": {"type": "number"},
            "apps": {"type": "array",
                     "items": {"type": "string"}},
            "focused": {"type": "string"},
            "windows": {"type": "object"},
            "displays": {"type": "array"},
        },
        "buffer": 1,
    },
}

POLL = 0.25
# Event-driven feed: the bridge heartbeats every 10s, so quiet >30s
# means the bridge is genuinely dead (not just no app launched).
STALE_AFTER = 30.0
HEARTBEAT_AFTER = 10.0
PAD = talon_layout.PAD
HEADER_H = 210
LIST_TOP = talon_layout.LIST_TOP
NAME_CHARS = 48
LABEL_CHARS = 26
APP_SIZE = 72
ROW_SIZE = 38
META_SIZE = 34

C_STALE = (255, 180, 80)
C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)
C_ROW = (255, 255, 255)
C_FOCUS_BG = (38, 66, 44)
C_SIDE = (110, 190, 130)


def clean(name):
    """Bound one OS app name for display. Never raises."""
    text = name if isinstance(name, str) else ""
    text = "".join(ch for ch in text.strip() if ch.isprintable())
    return text[:NAME_CHARS]


def label(name):
    """One button label: cleaned, truncated with an ellipsis."""
    text = clean(name)
    if len(text) > LABEL_CHARS:
        text = text[:LABEL_CHARS - 1] + "…"
    return text or "unknown"


def groups(state):
    """Side grouping for a feed state (pure; shared with tap map)."""
    state = state if isinstance(state, dict) else {}
    apps = state.get("apps")
    apps = apps if isinstance(apps, list) else []
    return talon_layout.group(apps, state.get("windows"),
                              state.get("displays"))


def hit(px, py, width, height, state):
    """App index for a panel tap, or None on a miss. `state` is the
    feed document (grouping needs its windows/displays)."""
    return talon_layout.hit(px, py, width, height, groups(state))


def _font(screen, name, size):
    path = screen.font_path(name)
    return ImageFont.truetype(path, size) if path else None


def _latest(screen):
    states = [s for s in screen.get_input("talon_apps", "state")
              if isinstance(s, dict)]
    return states[-1] if states else None


def _key(state):
    if not state:
        return (None,)
    apps = state.get("apps")
    apps = tuple(apps) if isinstance(apps, list) else None
    stale = time.time() - state.get("ts", 0) > STALE_AFTER
    return (apps, state.get("focused"), stale,
            repr(state.get("windows")), repr(state.get("displays")))


def _column(screen, draw, side, indices, apps, focused, fonts):
    row_font, meta_font = fonts
    plain = row_font or meta_font
    tag = "LEFT" if side == "left" else "RIGHT"
    draw.text((talon_layout.button_rect(side, 0, screen.W)[0], 216),
              tag, font=meta_font or plain, fill=C_SIDE)
    for slot, index in enumerate(indices):
        x, ry, w, h = talon_layout.button_rect(side, slot, screen.W)
        name = apps[index] if 0 <= index < len(apps) else ""
        if name and name == focused:
            draw.rounded_rectangle([x, ry, x + w, ry + h],
                                   radius=10, fill=C_FOCUS_BG)
            text = "* " + label(name)
        else:
            draw.rounded_rectangle([x, ry, x + w, ry + h],
                                   radius=10, outline=C_LINE, width=2)
            text = "  " + label(name)
        draw.text((x + 18, ry + 12), text, font=row_font or plain,
                  fill=C_ROW)


def _draw(screen, title, state, stale, bg, grouped):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    app_font = _font(screen, "DejaVuSans-Bold", APP_SIZE)
    row_font = _font(screen, "DejaVuSans", ROW_SIZE)
    meta_font = _font(screen, "DejaVuSans", META_SIZE)
    plain = app_font or row_font or meta_font
    draw.text((PAD, 26), title, font=meta_font or plain, fill=C_DIM)
    if state is None:
        draw.text((PAD, 70), "apps", font=app_font or plain,
                  fill=(255, 255, 255))
        draw.text((PAD, HEADER_H + 40),
                  "waiting for talon feed -- run bridges/talon_apps.py",
                  font=meta_font or plain, fill=(120, 120, 130))
        return img
    raw = state.get("apps")
    apps = [clean(a) for a in raw] if isinstance(raw, list) else []
    focused = clean(state.get("focused"))
    draw.text((PAD, 70), "apps (%d)" % len(apps),
              font=app_font or plain, fill=(255, 255, 255))
    y = 70 + APP_SIZE + 8
    if grouped.get("mode") == "sides":
        mode = "left=D%d right=other" % \
            ((grouped.get("left_display") or 0) + 1)
    else:
        mode = "one screen: split"
    draw.text((screen.W - PAD - 520, 26), mode,
              font=meta_font or plain, fill=C_DIM)
    if stale:
        draw.text((PAD, y), "STALE -- feed quiet >30s",
                  font=meta_font or plain, fill=C_STALE)
    draw.line([(PAD, HEADER_H - 14), (screen.W - PAD, HEADER_H - 14)],
              fill=C_LINE, width=2)
    if not apps:
        draw.text((PAD, LIST_TOP + 20), "no running apps in feed",
                  font=meta_font or plain, fill=C_DIM)
        return img
    fonts = (row_font, meta_font)
    _column(screen, draw, "left", grouped["left"], apps, focused, fonts)
    _column(screen, draw, "right", grouped["right"], apps, focused,
            fonts)
    if grouped.get("overflow"):
        cx = PAD + talon_layout.COL_W + 40
        draw.text((cx, LIST_TOP + 20), "+%d more" % grouped["overflow"],
                  font=meta_font or plain, fill=C_DIM)
    return img


def run(screen, params, stop):
    title = str((params or {}).get("title") or "APPS")
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
                screen.present(_draw(screen, title, state, stale, bg,
                                     groups(state)))
            except Exception:
                pass
        stop.wait(POLL)
