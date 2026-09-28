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
"""

import textwrap
import time

from PIL import ImageDraw, ImageFont

NAME = "activity"
DESCRIPTION = "Live beads-bridge MCP activity fed live (POST /feed/activity/event)"
STATIC = False
ACCENT = "#50DC78"
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

POLL = 0.5  # seconds between buffer checks; draws happen only on change
HEADER_H = 110
PAD = 48
TOOL_SIZE = 44
META_SIZE = 34
LINE_GAP = 16

C_OK = (110, 220, 130)
C_ERR = (255, 110, 100)


def _font(screen, name, size):
    path = screen.font_path(name)
    if path is None:
        return None
    return ImageFont.truetype(path, size)


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


def _wrap(draw, text, font, max_w, rows=2, width=52):
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


def _draw(screen, title, events, max_lines, bg):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    tool_font = _font(screen, "DejaVuSans-Bold", TOOL_SIZE)
    meta_font = _font(screen, "DejaVuSans", META_SIZE)
    head_font = _font(screen, "DejaVuSans-Bold", 54)
    plain = head_font or tool_font or meta_font

    # Header: title + live count.
    draw.text((PAD, 26), title, font=plain, fill=(255, 255, 255))
    count = "%d recent" % len(events[-max_lines:])
    if meta_font is not None:
        try:
            w = draw.textlength(count, font=meta_font)
        except Exception:
            w = 0
        draw.text((screen.W - PAD - w, 40), count, font=meta_font,
                  fill=(140, 140, 150))
    draw.line([(PAD, HEADER_H - 14), (screen.W - PAD, HEADER_H - 14)],
              fill=(60, 60, 70), width=2)

    y = HEADER_H
    max_text_w = screen.W - 2 * PAD
    for event in events[-max_lines:]:
        ok = event.get("outcome") == "ok"
        color = C_OK if ok else C_ERR
        glyph = "\u2713" if ok else "\u2717"  # never blank, never emoji-dependent
        tool = str(event.get("tool") or "???")
        row0 = "%s %s" % (glyph, tool)
        draw.text((PAD, y), row0, font=tool_font or plain, fill=color)
        y += TOOL_SIZE + 6
        meta = _meta(event)
        if meta:
            for row in _wrap(draw, meta, meta_font, max_text_w, rows=1):
                draw.text((PAD + 40, y), row, font=meta_font or plain,
                          fill=(140, 140, 150))
                y += META_SIZE + 4
        summary = str(event.get("summary") or "")
        if summary:
            for row in _wrap(draw, summary, meta_font, max_text_w - 40):
                draw.text((PAD + 40, y), row, font=meta_font or plain,
                          fill=(225, 225, 232))
                y += META_SIZE + 4
        y += LINE_GAP
        if y > screen.H - 60:
            break

    if not events:
        idle = "waiting for activity \u2014 bridge feeds /feed/activity/event"
        draw.text((PAD, HEADER_H + 40), idle, font=meta_font or plain,
                  fill=(120, 120, 130))
    return img


def run(screen, params, stop):
    title = str((params or {}).get("title") or "ACTIVITY")
    try:
        max_lines = max(1, min(12, int((params or {}).get("lines") or 8)))
    except (TypeError, ValueError):
        max_lines = 8
    bg = screen.color((params or {}).get("background"), (10, 10, 14))

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
