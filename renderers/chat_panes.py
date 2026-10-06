"""The chat panel's two-pane layer: geometry -> one litehtml document.

Split out of ``renderers/chat.py`` the same way ``_picker_tiles.py`` splits
out the picker's tile layer and ``_options_grid.py`` the options name grid.
``chat.py`` keeps the view contract (params, the ordered event timeline, the
poll loop); this module owns drawing it: a present-viewer roster on the left,
the chat feed on the right, every pane, row and line placed at panel pixels
computed here, so the row the arithmetic budgeted for and the row the engine
draws are the same numbers. ``chat_fit`` does the measuring.

Nothing here trusts a caller. Author names, display names and message text all
arrive from feed payloads -- the one genuinely untrusted input on a panel -- so
each one is escaped here before it reaches the document, and every colour goes
through ``templates.hex_colour()`` over a palette tuple the screen already
parsed, never the caller's own string, so a style attribute cannot be
injected. That is the same rule ``{{panes|raw}}`` is built under (see the
picker tile layer).

Two pixel facts this depends on, both easy to get wrong:

- litehtml puts border and padding OUTSIDE a declared width, so a pane
  declares its rect minus the frame;
- an absolutely positioned child offsets from its positioned parent, so a row
  is placed in pane-local pixels, measured with the same face the document
  draws with, never left to flex centring litehtml does not do.
"""

import time

import _html_templates as templates
import chat_fit as fit

# ---- the pixel contract, authored at 1920x1080 -------------------------
# chat.html's CSS owns the chrome; these two numbers are the band heights the
# pane layer has to leave room for, and a rendered-pixel test pins them
# against the real document, because a band that grew would put the last feed
# lines under the footer.
HEAD_BAND = 170
FOOT_BAND = 62
PAD = 176         # panel margin: clears the persistent home/sleep badges,
#                   which own the top-left and top-right 160px squares of
#                   every presented frame (ui/system_buttons.STRIP).
#                   A title under a badge is a title nobody can read --
#                   this is the lesson the first live render taught, and
#                   the test below pins it.
GUTTER = 28       # between the two panes
BOARD_W = 520     # the roster pane's width
BORDER = 2        # each pane's frame, outside its declared width
INSIDE = 26       # padding inside a pane
PH_H = 74         # a pane's head strip: label + note
ROW_H = 60        # one roster row
ROW_FONT = 38
LINE_H = 46       # one chat line
LINE_FONT = 34
HEAD_FONT = 30
GAP = 6           # between two events in the feed

TITLE = "CHAT"
BOARD_LABEL = "HERE NOW"
FEED_LABEL = "CHAT"
ROSTER_STALE_AFTER = 120.0   # the bridge pushes every ~10s; 12 misses is death


def layout(w, h):
    """Everything the layer draws from, for this panel size.

    Identical to the constants above at 1920x1080 and proportional elsewhere,
    so a smaller panel keeps the same layout instead of overflowing it. The
    two chrome bands stay fixed px because the template's CSS is fixed px
    (litehtml has no viewport units), which is the same deal every template in
    this tree makes.
    """
    w, h = max(1, int(w)), max(1, int(h))
    k = max(0.35, min(w / 1920.0, h / 1080.0))

    def px(value):
        return max(1, int(round(value * k)))

    pad, gutter = px(PAD), px(GUTTER)
    left = min(pad, max(0, w // 4))
    right = max(left + 2, w - left)
    top = min(max(0, HEAD_BAND), max(0, h - 2))
    bottom = max(top + 1, h - min(FOOT_BAND, max(0, h // 3)))
    board_w = min(px(BOARD_W), max(1, (right - left) // 2))
    feed_x = left + board_w + gutter
    return {
        "board": [left, top, board_w, bottom - top],
        "feed": [feed_x, top, max(1, right - feed_x), bottom - top],
        "border": max(1, px(BORDER)),
        "inside": px(INSIDE),
        "ph_h": px(PH_H),
        "row_h": px(ROW_H),
        "row_font": px(ROW_FONT),
        "line_h": px(LINE_H),
        "line_font": px(LINE_FONT),
        "head_font": px(HEAD_FONT),
        "gap": px(GAP),
    }


def roster_state(pushed, now=None):
    """("none" | "live" | "stale", age seconds or None) for the roster feed.

    A snapshot with no timestamp reads as live: the bridge always stamps one,
    and inventing staleness from a missing field would libel a working bridge.
    """
    if not isinstance(pushed, dict):
        return "none", None
    ts = pushed.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return "live", None
    age = (time.time() if now is None else now) - float(ts)
    return ("live" if age <= ROSTER_STALE_AFTER else "stale"), age


def age_label(seconds):
    """Coarse on purpose: a per-second label would redraw a quiet panel every
    second for no new information."""
    try:
        value = max(0, int(seconds))
    except (TypeError, ValueError):
        return "just now"
    if value < 15:
        return "just now"
    if value < 90:
        return "%ds" % (value // 10 * 10)
    return "%dm" % (value // 60)


def notice(events, viewers):
    """The panel's words when there is nothing to show at all.

    project-a4t.8.1: an empty panel that says it is waiting is the healthy
    idle state, not a bug, so it keeps saying exactly that -- and the two
    panes are not drawn empty around it, because an empty roster column and
    silence is the worse panel. ``""`` the moment there is anything to look
    at: one viewer in the roster or one event on the feed.
    """
    return "" if events or viewers else fit.WAITING


def _words(state, count, age):
    if state == "none":
        return "NO ROSTER", "no roster yet \u2014 the bridge pushes POST /feed/chat/roster"
    if state == "stale":
        return "ROSTER STALE", "roster %s old" % age_label(age)
    return "%d HERE" % count, "roster %s" % age_label(age)


def chrome(ink, title, count, state, age, notice_text,
           footer_right="litehtml"):
    """chat.html's variables, everything except the pane layer.

    ``ink`` is a dict of palette tuples the screen already parsed, never the
    caller's own text, so the template can take them in a style attribute
    without opening a CSS injection. The set is pinned against the template by
    a test.
    """
    status, footer = _words(state, count, age)
    return {
        "background": templates.hex_colour(ink["bg"]),
        "color": templates.hex_colour(ink["text"]),
        "dim": templates.hex_colour(ink["dim"]),
        "accent": templates.hex_colour(ink["accent"]),
        "line": templates.hex_colour(ink["line"]),
        "eyebrow": "DISPLAYD",
        "title": str(title or TITLE),
        "status": status,
        "notice": notice_text,
        "footer": footer,
        "footer_right": footer_right,
    }


def _box(cls, style, inner=""):
    return '<div class="%s" style="%s">%s</div>' % (cls, style, inner)


def _pane(rect, cls, lay, label, note, rows):
    """One pane: frame, head strip, rows. The rows are pane-local."""
    x, y, w, h = (int(v) for v in rect)
    border, inside, size = lay["border"], lay["inside"], lay["head_font"]
    cw = max(1, w - 2 * border)
    top = max(2, (lay["ph_h"] - size) // 2)
    note_x = max(inside, cw - inside - fit.width(note, size, bold=True))
    head = _box("ph", "left:%dpx; top:%dpx; font-size:%dpx;"
                % (inside, top, size), templates.escape(label))
    head += _box("ph note", "left:%dpx; top:%dpx; font-size:%dpx;"
                 % (note_x, top, size), templates.escape(note))
    return _box("pane " + cls, "left:%dpx; top:%dpx; width:%dpx; height:%dpx;"
                % (x, y, cw, max(1, h - 2 * border)), head + "".join(rows))


def _feed_rows(events, rect, lay, max_messages, ink):
    """The right pane's rows: one div per measured line, escaped."""
    parts, y = [], lay["ph_h"]
    for line in fit.feed_lines(events, rect, lay, max_messages, ink):
        for dx, text, colour, bold in line["runs"]:
            parts.append(_box(
                "ln" + (" b" if bold else ""),
                "left:%dpx; top:%dpx; font-size:%dpx; color:%s;"
                % (lay["inside"] + dx, y, lay["line_font"],
                   templates.hex_colour(colour)),
                templates.escape(text)))
        y += lay["line_h"] + line["gap"]
    return parts


def panes(events, viewers, lay, ink, notice_text, max_messages):
    """The whole pane layer for ``{{panes|raw}}``, in paint order.

    Empty in the cold state, so the panel says it is waiting instead of
    framing an empty roster column and silence.
    """
    if notice_text:
        return ""
    events, viewers = list(events or []), list(viewers or [])
    rows = []
    for i, (name, dim) in enumerate(fit.roster_rows(viewers, lay["board"],
                                                    lay)):
        rows.append(_box(
            "row" + (" dim" if dim else ""),
            "left:%dpx; top:%dpx; font-size:%dpx;"
            % (lay["inside"], lay["ph_h"] + i * lay["row_h"], lay["row_font"]),
            templates.escape(name)))
    board = _pane(lay["board"], "board", lay, BOARD_LABEL,
                  "%d" % len(viewers) if viewers else "", rows)
    shown = len([e for e in fit.window(events, max_messages)
                 if not e.get("join")])
    feed = _pane(lay["feed"], "feed", lay, FEED_LABEL, "%d in view" % shown,
                 _feed_rows(events, lay["feed"], lay, max_messages, ink))
    return board + feed
