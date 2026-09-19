"""Rolling chat window, sized to read across a room on a 1920x1080 panel.

Fed live while running -- or while idle -- through POST /feed/chat/message
(one chat line) and POST /feed/chat/delete ({messageId} retraction). Inputs
pushed while another view is selected accumulate in the daemon's feed cache,
so switching here is instantly populated, never empty.

Ambient-first: scroll retention matters more than feature count. No emote
image downloads in v1 -- message text plus cheap role/badge tags only.
"""

import textwrap
import time

from PIL import ImageDraw, ImageFont

NAME = "chat"
DESCRIPTION = "Rolling chat window fed live (POST /feed/chat/message)"
STATIC = False
PARAMS = {
    "title": {"type": "string", "help": "header text, default CHAT"},
    "lines": {"type": "integer", "help": "messages on screen, default 7"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}
INPUTS = {
    "message": {
        "type": "object",
        "help": "one chat line: {author, text, ...}",
        "required": ["author", "text"],
        "properties": {
            "id": {"type": "string"},
            "author": {"type": "string"},
            "display_name": {"type": "string"},
            "text": {"type": "string"},
            "color": {"type": "string"},
            "badges": {"type": "array"},
            "timestamp": {"type": "number"},
        },
        # Persistent retention: 0 means unbounded -- a message leaves
        # state only on moderation delete, never for age or count. The
        # visible window stays screen-bounded in _draw via max_lines.
        "buffer": 0,
    },
    "delete": {
        "type": "object",
        "help": "retract one line: {messageId}",
        "required": ["messageId"],
        "properties": {
            "messageId": {"type": "string"},
            "animate": {"type": "boolean"},
        },
        # Unbounded like message: an evicted retraction would let its
        # (retained) message resurface, so deletes persist too.
        "buffer": 0,
    },
}

POLL = 0.4  # seconds between buffer checks; draws happen only on change
HEADER_H = 110
PAD = 48
AUTHOR_SIZE = 46
TEXT_SIZE = 44
LINE_GAP = 14

ROLE_TAGS = (  # cheap badge distinction: (payload flag, tag, colour)
    ("isMod", "MOD", (255, 80, 80)),
    ("isVip", "VIP", (200, 120, 255)),
    ("isSubscriber", "SUB", (90, 200, 255)),
    ("isFirstChat", "NEW", (120, 220, 120)),
)


def _font(screen, name, size):
    path = screen.font_path(name)
    if path is None:
        return None
    return ImageFont.truetype(path, size)


def _snapshot(screen):
    """Current view of the world: ordered unique messages minus retractions."""
    seen = {}
    for msg in screen.get_input("chat", "message"):
        if not isinstance(msg, dict):
            continue
        key = msg.get("id") or (msg.get("author"), msg.get("text"))
        seen[key] = msg
    gone = set()
    for d in screen.get_input("chat", "delete"):
        if isinstance(d, dict) and d.get("messageId"):
            gone.add(d["messageId"])
    return [m for k, m in seen.items() if k not in gone]


def _wrap(draw, text, font, max_w):
    if font is None:
        return textwrap.wrap(text, 52)
    try:
        avg = draw.textlength("0123456789", font=font) / 10.0
    except Exception:
        avg = TEXT_SIZE * 0.55
    width = max(12, int(max_w / max(avg, 1)))
    out = []
    for para in str(text).splitlines() or [""]:
        out.extend(textwrap.wrap(para, width) or [""])
    return out[:3]  # cap a single message at 3 rows: retention over completeness


def _draw(screen, title, messages, max_lines, bg):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    author_font = _font(screen, "DejaVuSans-Bold", AUTHOR_SIZE)
    text_font = _font(screen, "DejaVuSans", TEXT_SIZE)
    head_font = _font(screen, "DejaVuSans-Bold", 54)

    # Header: title + live count.
    draw.text((PAD, 26), title, font=head_font or text_font, fill=(255, 255, 255))
    count = "%d in view" % len(messages[-max_lines:])
    if head_font is not None:
        draw.text((screen.W - PAD - draw.textlength(count, font=text_font),
                   40), count, font=text_font, fill=(140, 140, 150))
    draw.line([(PAD, HEADER_H - 14), (screen.W - PAD, HEADER_H - 14)],
              fill=(60, 60, 70), width=2)

    y = HEADER_H
    max_text_w = screen.W - 2 * PAD
    for msg in messages[-max_lines:]:
        author = str(msg.get("display_name") or msg.get("author") or "???")
        acolor = screen.color(msg.get("color"), (120, 200, 255))
        tags = []
        for flag, tag, tcolor in ROLE_TAGS:
            if msg.get(flag):
                tags.append((tag, tcolor))
        x = PAD
        if author_font is not None:
            for tag, tcolor in tags:
                draw.text((x, y), "[" + tag + "] ", font=text_font, fill=tcolor)
                x += draw.textlength("[" + tag + "] ", font=text_font)
            draw.text((x, y), author, font=author_font, fill=acolor)
            x += draw.textlength(author + "  ", font=author_font)
        else:
            prefix = "".join("[%s] " % t for t, _ in tags) + author + ": "
            draw.text((x, y), prefix, fill=acolor)
            x += 10
        rows = _wrap(draw, msg.get("text", ""), text_font, max_text_w - (x - PAD))
        if author_font is not None and rows:
            draw.text((x, y), rows[0], font=text_font, fill=(235, 235, 240))
            rows = rows[1:]
        for row in rows:
            y += TEXT_SIZE + 6
            draw.text((PAD + 40, y), row, font=text_font, fill=(235, 235, 240))
        y += AUTHOR_SIZE + LINE_GAP
        if y > screen.H - 60:
            break

    if not messages:
        idle = "waiting for chat \u2014 feed a message to wake this view"
        draw.text((PAD, HEADER_H + 40), idle, font=text_font, fill=(120, 120, 130))
    return img


def run(screen, params, stop):
    title = str(params.get("title") or "CHAT")
    try:
        max_lines = max(1, min(12, int(params.get("lines") or 7)))
    except (TypeError, ValueError):
        max_lines = 7
    bg = screen.color(params.get("background"), (10, 10, 14))

    last_key = None
    while not stop.is_set():
        messages = _snapshot(screen)
        key = (len(messages),
               messages[-1].get("id") if messages else None,
               len(screen.get_input("chat", "delete")))
        if key != last_key:
            last_key = key
            # One complete frame, one swap: never a partial chat window.
            screen.present(_draw(screen, title, messages, max_lines, bg))
        stop.wait(POLL)
