"""Fitting chat text into the panel's boxes: measurement, wrapping, and the
roster/feed rows themselves.

Split out of ``renderers/chat_panes.py`` because these are two concepts: how
one name or one message becomes measured rows (here), and the panel pixels
those rows are drawn at (there). ``chat_panes`` imports this; never the other
way round.

Everything here measures with ``_html_native.ui_font`` -- the same candidate
faces litehtml draws document text with -- so a row this module says fits is a
row the engine draws inside its box. That is the picker's rule (`label_px` and
`label_box` measure rather than hope) applied to prose.

Colours are palette tuples in and palette tuples out (in the ``runs`` below);
this module never reads a caller's colour text and never builds markup, so the
escaping and CSS-safe colour rules stay in one place, the trust boundary.
"""

import _html_native

MAX_ROWS_PER_MESSAGE = 3   # one message is capped, never dropped
ROLE_TAGS = (("isMod", "MOD"), ("isVip", "VIP"),
             ("isSubscriber", "SUB"), ("isFirstChat", "NEW"))
JOINED = "joined the chat"
NO_ROSTER = "no roster yet"
WAITING = "waiting for chat \u2014 feed a message to wake this view"


def font(size, bold=False):
    """The face document text uses at this size. Never returns None."""
    return _html_native.ui_font(max(8, int(size)), bold=bold)


def width(text, size, bold=False):
    """Text width in px, measured. Never raises: an unmeasurable string
    falls back to a character estimate rather than dropping the line."""
    try:
        return int(font(size, bold).getlength(str(text)))
    except Exception:
        return int(len(str(text)) * size * 0.55)


def split_token(row, size, budget):
    """Break one over-long token (a pasted URL) into budget-sized pieces, so
    a single word cannot run out of its pane."""
    out, current = [], ""
    for char in row:
        if current and width(current + char, size) > budget:
            out.append(current)
            current = char
        else:
            current += char
    return out + ([current] if current else [])


def wrap(text, size, budget, max_rows=MAX_ROWS_PER_MESSAGE):
    """Greedy word wrap into at most ``max_rows`` rows, measured. A message
    that does not fit is truncated the way the old draw loop truncated it:
    retention over completeness, and the state is never touched."""
    rows = []
    for para in str(text).splitlines() or [""]:
        current = ""
        for word in para.split():
            trial = (current + " " + word).strip()
            if current and width(trial, size) > budget:
                rows.extend(split_token(current, size, budget))
                current = word
            else:
                current = trial
        rows.extend(split_token(current, size, budget) or [""])
    return rows[:max_rows] or [""]


def roster_rows(viewers, rect, lay):
    """The left pane's rows: present viewers, alphabetical by the name that
    is drawn, plus an overflow row. ``[(name, dim_flag)]``.

    Alphabetical rather than Firebot's database order: this pane is read at a
    glance from across a room, and a stable order is what makes a name
    findable. The overflow row keeps the count in the pane head honest.
    """
    names = []
    for viewer in viewers or []:
        if not isinstance(viewer, dict):
            continue
        name = viewer.get("display_name") or viewer.get("username")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    names.sort(key=lambda text: text.lower())
    room = max(0, (int(rect[3]) - lay["ph_h"] - lay["inside"]) // lay["row_h"])
    if room <= 0:
        return []   # a degenerate panel draws no rows rather than past them
    if not names:
        return [(NO_ROSTER, True)]
    if len(names) > room:
        keep = max(1, room - 1)
        return [(name, False) for name in names[:keep]] + \
            [("+%d more" % (len(names) - keep), True)]
    return [(name, False) for name in names]


def window(events, max_messages):
    """The events in view: the last ``max_messages`` CHAT messages plus any
    join events interleaved with them. A join does not consume the budget --
    the ``lines`` param means messages, and a join is one line of the same
    stream."""
    events = [e for e in (events or []) if isinstance(e, dict)]
    keep, seen, start = max(1, int(max_messages)), 0, 0
    for i in range(len(events) - 1, -1, -1):
        if not events[i].get("join"):
            seen += 1
            start = i
            if seen >= keep:
                break
    return events[start:]


def message_lines(msg, lay, budget, ink):
    """One chat message as draws: the author prefix in its own colour, then
    the wrapped text indented under it. ``runs`` are ``(dx, text, colour,
    bold)`` with ``colour`` a palette tuple."""
    author = str(msg.get("display_name") or msg.get("author") or "???")
    tags = "".join("[%s] " % tag for flag, tag in ROLE_TAGS if msg.get(flag))
    prefix = tags + author + "  "
    used = width(prefix, lay["line_font"], bold=True)
    rows = wrap(msg.get("text", ""), lay["line_font"], max(60, budget - used))
    out = [{"runs": [(0, prefix, ink["author"], True),
                     (used, rows[0], ink["text"], False)], "gap": 0}]
    for text in rows[1:]:
        out.append({"runs": [(40, text, ink["text"], False)], "gap": 0})
    return out


def feed_lines(events, rect, lay, max_messages, ink):
    """Every line the right pane draws, oldest first.

    Budgeted from the NEWEST event backwards, so the message that just
    arrived is always on screen and older ones leave only when the height
    budget or the ``lines`` param pushes them off. State is never aged out --
    the daemon keeps every message until a moderation delete -- only this
    visible window is screen-bounded (project-a4t.8).
    """
    room = max(0, (int(rect[3]) - lay["ph_h"] - lay["inside"]) // lay["line_h"])
    if room <= 0:
        return []   # a degenerate panel draws no lines rather than past them
    budget = max(1, int(rect[2]) - 2 * lay["border"] - 2 * lay["inside"])
    kept, used = [], 0
    for msg in reversed(window(events, max_messages)):
        if msg.get("join"):
            name = str(msg.get("display_name") or msg.get("author") or "???")
            draw = [{"runs": [(0, name + " " + JOINED, ink["join"], True)],
                     "gap": lay["gap"]}]
        else:
            draw = message_lines(msg, lay, budget, ink)
            draw[-1]["gap"] = lay["gap"]
        if kept and used + len(draw) > room:
            break
        kept.append(draw)
        used += len(draw)
        if used >= room:
            break
    if not kept:
        kept = [[{"runs": [(0, WAITING, ink["dim"], False)], "gap": 0}]]
    if used > room:
        # One message taller than the pane (a tiny panel): truncate it to the
        # room there is, never draw past the pane's bottom.
        kept[-1] = kept[-1][:room]
    lines = [line for draw in reversed(kept) for line in draw]
    lines[-1]["gap"] = 0
    return lines
