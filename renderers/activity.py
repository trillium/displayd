"""Live MCP activity for the jumbotron: what the agents just did.

Fed live while running -- or while idle -- through
``POST /feed/activity/event`` (one validated ActivityEvent per completed
beads-bridge MCP tool call). The bridge (``bridges/beads_activity.py``)
owns the beads-bridge protocol; this renderer stays content-agnostic and
only ever draws validated payloads from its feed buffer.

Ambient-first: newest at the top, one row per call (tool + outcome +
caller/duration meta + summary). Inputs pushed while another view is
selected accumulate in the daemon's feed cache, so switching here is
instantly populated, never empty.

Presentation only: the band is ``ui.shell``, each row is ``ui.stat``'s
type steps, and a long summary is broken by ``ui.text.wrap``. The outcome
colour is the palette's ``ok`` / ``alert`` role, so this module holds no
colour literal, no font loader and no wrap rule of its own.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import theme
from ui import shell, stat
from ui import text as ui_text

NAME = "activity"
DESCRIPTION = "Live beads-bridge MCP activity fed live (POST /feed/activity/event)"
STATIC = False
# A band over a list of rows: a preset may put it in a reduced region.
CAPABILITY = "partial"
PARAMS = {
    "title": {"type": "string", "help": "header text, default ACTIVITY"},
    "lines": {"type": "integer", "help": "events on screen, default 8"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}
INPUTS = {
    "event": {
        "type": "object",
        "help": "one ActivityEvent: {tool, outcome, ...}",
        "required": ["tool", "outcome"],
        "properties": {
            "seq": {"type": "number"},
            "at": {"type": "string"},
            "tool": {"type": "string"},
            "outcome": {"type": "string"},
            "caller": {"type": "string"},
            "sessionId": {"type": "string"},
            "client": {"type": "string"},
            "authed": {"type": "boolean"},
            "durationMs": {"type": "number"},
            "argNames": {"type": "array"},
            "beadRefs": {"type": "array"},
            "summary": {"type": "string"},
        },
        # Bounded ring, not an archive: the bridge already dedupes by seq,
        # and the panel keeps only the latest window.
        "buffer": 30,
    },
}

POLL = 0.5      # seconds between buffer checks; draws happen only on change
ROW_Y = 150     # the first event's tool line, under the band's rule
TOOL_SIZE = 44
META_SIZE = 34
INDENT = 40     # the meta and the summary hang under the tool line
LINE_GAP = 16
ROOM_Y = 60     # room kept at the bottom of the panel


def _snapshot(screen):
    """Current view of the world: buffered events, deduped by seq, oldest
    first. Seq-less payloads (should not happen from the bridge) are kept
    in arrival order at the end."""
    ordered = {}
    nodeless = []
    for event in screen.get_input("activity", "event"):
        if not isinstance(event, dict):
            continue
        seq = event.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, (int, float)):
            nodeless.append(event)
        elif seq in ordered:
            ordered[seq] = event
        else:
            ordered[seq] = event
    return [ordered[k] for k in sorted(ordered)] + nodeless


def _meta(event):
    parts = []
    caller = event.get("caller")
    if caller:
        parts.append(str(caller))
    try:
        parts.append("%dms" % int(event.get("durationMs") or 0))
    except (TypeError, ValueError):
        pass
    at = str(event.get("at") or "")
    if len(at) >= 19:
        parts.append(at[11:19])  # HH:MM:SS from the ISO timestamp
    elif at:
        parts.append(at)
    refs = event.get("beadRefs")
    if isinstance(refs, list) and refs:
        parts.append("%d bead%s" % (len(refs), "" if len(refs) == 1 else "s"))
    return " \u00b7 ".join(parts)


def _draw(screen, title, events, max_lines, bg):
    img = screen.new_image(bg)
    col_w = screen.W - 2 * shell.PAD
    shown = events[-max_lines:]
    shell.head(img, screen, title, detail="%d recent" % len(shown))

    y = ROW_Y
    for event in shown:
        ok = event.get("outcome") == "ok"
        ink = theme.rgb("ok") if ok else theme.rgb("alert")
        glyph = "\u2713" if ok else "\u2717"  # never blank, never emoji-dependent
        tool = str(event.get("tool") or "???")
        stat.label(img, screen, (shell.PAD, y), "%s %s" % (glyph, tool),
                   ink=ink, size=TOOL_SIZE, room=col_w)
        y += TOOL_SIZE + 6
        meta = _meta(event)
        if meta:
            stat.body(img, screen, (shell.PAD + INDENT, y), meta,
                      size=META_SIZE, room=col_w - INDENT)
            y += META_SIZE + 4
        summary = str(event.get("summary") or "")
        if summary:
            for row in ui_text.wrap(screen, summary, META_SIZE,
                                    col_w - INDENT, rows=2):
                stat.body(img, screen, (shell.PAD + INDENT, y), row,
                          ink=theme.rgb("muted-soft"), size=META_SIZE,
                          room=col_w - INDENT)
                y += META_SIZE + 4
        y += LINE_GAP
        if y > screen.H - ROOM_Y:
            break

    if not shown:
        stat.body(img, screen, (shell.PAD, ROW_Y),
                  "waiting for activity \u2014 bridge feeds /feed/activity/event",
                  size=META_SIZE, room=col_w)
    return img


def run(screen, params, stop):
    title = str((params or {}).get("title") or "ACTIVITY")
    try:
        max_lines = max(1, min(12, int((params or {}).get("lines") or 8)))
    except (TypeError, ValueError):
        max_lines = 8
    bg = screen.color((params or {}).get("background"), theme.rgb("page"))

    last_key = None
    while not stop.is_set():
        events = _snapshot(screen)
        key = (len(events),
               events[-1].get("seq") if events else None)
        if key != last_key:
            last_key = key
            try:
                # One complete frame, one swap: never a partial window.
                screen.present(_draw(screen, title, events, max_lines, bg))
            except Exception:
                # A draw failure must not kill the daemon loop; the last
                # good frame stays on the panel.
                pass
        stop.wait(POLL)
