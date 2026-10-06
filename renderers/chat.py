"""Two-pane chat: the viewers in the channel on the left, the chat on the
right, sized to read across a room on a 1920x1080 panel.

Fed live while running -- or while idle -- through POST /feed/chat/message
(one chat line, or a join event with ``"join": true``) and POST
/feed/chat/delete ({messageId} retraction). Inputs pushed while another view
is selected accumulate in the daemon's feed cache, so switching here is
instantly populated, never empty.

Presence arrives on its own input: POST /feed/chat/roster carries the current
viewer list, and the left pane draws it. Neither the join events nor the pane
is fetched here -- ``bridges/firebot_roster.py`` polls Firebot ONCE per cycle
and pushes both off that single read, so the panel never talks to a vendor
protocol and never holds two answers to the same question.

Retention is unchanged and pinned (project-a4t.8): a message leaves state only
on a moderation delete, never for age or count. The visible window is
screen-bounded here, budgeted from the newest event backwards so the line that
just arrived is always on screen. With nothing pushed at all the panel says it
is waiting (project-a4t.8.1) rather than framing an empty roster column.

The panel is drawn by litehtml from ``html-templates/chat.html`` with the
geometry in ``renderers/chat_panes.py`` (and the text fitting in
``renderers/chat_fit.py``), so this view needs the built engine exactly as the
html, picker, options and home views do; without it the panel shows the red
"build it" card instead of the chat.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _html_error
import _html_native
import _html_templates as templates
import chat_panes as panes

NAME = "chat"
DESCRIPTION = ("Two-pane chat: present-viewer roster + rolling messages "
               "(POST /feed/chat/message, /feed/chat/roster)")
STATIC = False
CAPABILITY = "partial"  # the pane split follows the region
PARAMS = {
    "title": {"type": "string", "help": "header text, default CHAT"},
    "lines": {"type": "integer", "help": "messages on screen, default 7"},
    "background": {"type": "string", "help": "background colour, default near-black"},
    "color": {"type": "string", "help": "text colour, default near-white"},
}
INPUTS = {
    "message": {
        "type": "object",
        "help": "one chat line, or one join event: {author, text, join}",
        "required": ["author", "text"],
        "properties": {
            "id": {"type": "string"},
            "author": {"type": "string"},
            "display_name": {"type": "string"},
            "text": {"type": "string"},
            "join": {"type": "boolean"},
            "color": {"type": "string"},
            "badges": {"type": "array"},
            "timestamp": {"type": "number"},
        },
        # Persistent retention: 0 means unbounded -- a message leaves
        # state only on moderation delete, never for age or count. The
        # visible window stays screen-bounded in chat_fit.feed_lines.
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
    "roster": {
        "type": "object",
        "help": "the present-viewer snapshot: {viewers:[{id,username,"
                "display_name}], ts}",
        "required": ["viewers"],
        "properties": {
            "viewers": {
                "type": "array",
                "items": {"type": "object",
                          "properties": {"id": {"type": "string"},
                                         "username": {"type": "string"},
                                         "display_name": {"type": "string"}}},
            },
            "count": {"type": "integer"},
            "ts": {"type": "number"},
        },
        # A snapshot, not a stream: only the latest one is the truth, so
        # the buffer is one and a slow pane can never draw a stale list
        # under a newer count.
        "buffer": 1,
    },
}

POLL = 0.4  # seconds between buffer checks; draws happen only on change
TEMPLATE = "chat.html"
BUILD_HINT = "build it: tools/build_litehtml.sh"
# The panel's ink: palette tuples the screen already parsed, never caller
# text. chat_panes turns them into #rrggbb strings for the document.
DIM = (140, 160, 190)
# The bar colour this view declares to the playlist (playlist_color.accent_for
# reads a renderer's ACCENT by name). Still a literal: folding these into
# theme.ACCENT_SLOTS is the rest of the token migration, tracked in notes.md.
ACCENT = (127, 209, 255)
LINE = (35, 43, 58)
TEXT = (235, 235, 240)
AUTHOR = (120, 200, 255)
JOIN = (120, 220, 150)


def _snapshot(screen):
    """Current view of the world: ordered unique events minus retractions.

    Joins arrive on this same input (the bridge flags them), so this is the
    one ordered timeline the panel draws and the order is arrival order --
    no second clock, and no second feed to interleave.
    """
    seen = {}
    for msg in screen.get_input(NAME, "message"):
        if not isinstance(msg, dict):
            continue
        key = msg.get("id") or (msg.get("author"), msg.get("text"))
        seen[key] = msg
    gone = set()
    for d in screen.get_input(NAME, "delete"):
        if isinstance(d, dict) and d.get("messageId"):
            gone.add(d["messageId"])
    return [m for k, m in seen.items() if k not in gone]


def _roster(screen):
    """(viewers, latest payload). The pane's data, straight off the feed:
    no fetch here, and a payload that is not one is ignored rather than
    drawn as an empty room."""
    pushed = screen.get_input(NAME, "roster")
    latest = pushed[-1] if pushed else None
    if not isinstance(latest, dict):
        return [], None
    rows = latest.get("viewers")
    if not isinstance(rows, (list, tuple)):
        return [], latest
    return [v for v in rows if isinstance(v, dict)], latest


def _key(events, viewers, state, age, max_lines):
    """Change detector for the poll loop: the frame is a function of the
    visible window, the roster and how fresh it is. Nothing else redraws,
    so a quiet panel stays quiet."""
    visible = [e for e in events[-max(1, int(max_lines)):]]
    return (
        tuple((e.get("id"), e.get("join"), len(str(e.get("text") or "")))
              for e in visible if isinstance(e, dict)),
        tuple((v.get("display_name"), v.get("username")) for v in viewers),
        state, None if age is None else int(age // 10), max_lines)


def _document(screen, title, events, max_lines, viewers, ink, state, age):
    """The filled template for this state -- what the panel draws, as text.

    Separated from the render so a test can assert on the document (the
    escape, the pane markup, the waiting line) without reading pixels.
    """
    lay = panes.layout(screen.W, screen.H)
    words = panes.notice(events, viewers)
    colours = panes.chrome(ink, title, len(viewers), state, age, words)
    raw = panes.panes(events, viewers, lay, ink, words, max_lines)
    document, root = templates.load(TEMPLATE, colours, raw={"panes": raw})
    return document, root


def _frame(screen, title, events, max_lines, viewers, ink, state, age):
    """One complete frame, or a card if the engine will not draw it. Never
    raises: a blank panel is indistinguishable from a dead daemon."""
    bg = tuple(ink["bg"])
    try:
        document, root = _document(screen, title, events, max_lines, viewers,
                                   ink, state, age)
        image, _height = _html_native.render(
            document, screen.W, screen.H, background=bg, root=root)
        canvas = screen.new_image(bg)
        canvas.paste(image, (0, 0))
        return canvas
    except templates.TemplateError as err:
        return _html_error.error_frame(screen, "chat: " + str(err),
                                       "fix the template, then re-show")
    except _html_native.NativeMissing as err:
        return _html_error.error_frame(screen, "chat: " + str(err), BUILD_HINT)
    except _html_native.HtmlRenderError as err:
        return _html_error.error_frame(screen, "chat: " + str(err),
                                       "template parsed but would not draw")
    except Exception as err:  # never a blank panel, whatever happens
        return _html_error.error_frame(screen, "chat: %s" % err,
                                       "the chat could not draw")


def _ink(fg, bg=(10, 10, 14)):
    """The panel's palette for one run. One dict, so a colour is resolved once
    and the pane layer cannot disagree with the chrome."""
    return {"bg": bg, "text": fg, "dim": DIM, "line": LINE,
            "author": AUTHOR, "join": JOIN}


def _draw(screen, title, messages, max_lines, bg, roster=(),
          fg=TEXT, state="none", age=None):
    """One complete frame of the two panes. Kept as the drawing entry point
    the panel and the tests call."""
    return _frame(screen, title, list(messages or []), max_lines,
                  list(roster or []), _ink(fg, bg), state, age)


def run(screen, params, stop):
    params = params or {}
    title = str(params.get("title") or panes.TITLE)
    try:
        max_lines = max(1, min(12, int(params.get("lines") or 7)))
    except (TypeError, ValueError):
        max_lines = 7
    bg = screen.color(params.get("background"), (10, 10, 14))
    fg = screen.color(params.get("color"), TEXT)
    ink = _ink(fg, bg)

    last_key = object()
    while not stop.is_set():
        events = _snapshot(screen)
        viewers, pushed = _roster(screen)
        state, age = panes.roster_state(pushed, time.time())
        key = _key(events, viewers, state, age, max_lines)
        if key != last_key:
            last_key = key
            # One complete frame, one swap: never a partial chat window.
            screen.present(_frame(screen, title, events, max_lines, viewers,
                                  ink, state, age))
        stop.wait(POLL)
