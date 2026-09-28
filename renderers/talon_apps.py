"""Running Talon applications: tap-to-focus list for the panel.

Fed by the Mac-side poller (``bridges/talon_apps.py``) through
``POST /feed/talon_apps/state`` (one document, buffer 1 -- latest only):
``{"ts", "apps": [names...], "focused": name}``. Draws one fixed-height
row per app; tapping a row POSTs ``/talon/focus`` with the tap point and
the daemon maps it back to the app name through ``hit()`` below, so the
touch action stays closed (coordinates only, never a name).

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

NAME = "talon_apps"
DESCRIPTION = "Running apps: tap a row to focus it"
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
        },
        "buffer": 1,
    },
}

POLL = 0.25
# Event-driven feed: the bridge heartbeats every 10s, so quiet >30s
# means the bridge is genuinely dead (not just no app launched).
STALE_AFTER = 30.0
HEARTBEAT_AFTER = 10.0
PAD = 48
HEADER_H = 210
LIST_TOP = 250
ROW_H = 72
MAX_ROWS = 10
NAME_CHARS = 48
APP_SIZE = 72
ROW_SIZE = 38
META_SIZE = 34

C_STALE = (255, 180, 80)
C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)
C_ROW = (255, 255, 255)
C_FOCUS_BG = (38, 66, 44)


def clean(name):
    """Bound one OS app name for display. Never raises."""
    text = name if isinstance(name, str) else ""
    text = "".join(ch for ch in text.strip() if ch.isprintable())
    return text[:NAME_CHARS]


def row_rect(index, width):
    """Panel rect of row `index`: (x, y, w, h). Pure geometry shared
    with the daemon's tap mapping so draw and hit-test cannot drift."""
    return (PAD, LIST_TOP + index * ROW_H, width - 2 * PAD, ROW_H)


def hit(px, py, width, height, count):
    """Row index for a panel tap, or None on a miss (outside the list
    area, beyond the last row, or beyond the apps on screen)."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        return None
    if count <= 0:
        return None
    rows = min(count, MAX_ROWS)
    for i in range(rows):
        x, y, w, h = row_rect(i, width)
        if x <= px < x + w and y <= py < y + h:
            return i
    return None


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
    return (apps, state.get("focused"), stale)


def _draw(screen, title, state, stale, bg):
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
    apps = [clean(a) for a in raw[:MAX_ROWS]] \
        if isinstance(raw, list) else []
    focused = clean(state.get("focused"))
    draw.text((PAD, 70), "apps (%d)" % len(apps),
              font=app_font or plain, fill=(255, 255, 255))
    y = 70 + APP_SIZE + 8
    if stale:
        draw.text((PAD, y), "STALE -- feed quiet >30s",
                  font=meta_font or plain, fill=C_STALE)
    draw.line([(PAD, HEADER_H - 14), (screen.W - PAD, HEADER_H - 14)],
              fill=C_LINE, width=2)
    for i, name in enumerate(apps):
        x, ry, w, h = row_rect(i, screen.W)
        if name and name == focused:
            draw.rounded_rectangle([x, ry, x + w, ry + h - 8],
                                   radius=10, fill=C_FOCUS_BG)
            label = "* " + name
        else:
            label = "  " + (name or "unknown")
        draw.text((x + 18, ry + 14), label, font=row_font or plain,
                  fill=C_ROW)
    if not apps:
        draw.text((PAD, LIST_TOP + 20), "no running apps in feed",
                  font=meta_font or plain, fill=C_DIM)
    elif isinstance(raw, list) and len(raw) > MAX_ROWS:
        draw.text((PAD, LIST_TOP + MAX_ROWS * ROW_H),
                  "+%d more" % (len(raw) - MAX_ROWS),
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
                screen.present(_draw(screen, title, state, stale, bg))
            except Exception:
                pass
        stop.wait(POLL)
