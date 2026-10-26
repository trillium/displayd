"""What a column draws on the panel: geometry plan and template variables.

Single concept: the displayd-html implementation's *column views*. Pure
functions, no daemon, no framebuffer: ``plan(rect)`` says how many text rows
and characters fit a region, ``build_vars`` turns a column plus its stream's
events into the escaped-scalar variables of ``html-templates/column.html``.

The html renderer takes scalars only (no loops, no markup from a feed), so
the template has ``ROW_SLOTS`` fixed row slots ``r0..r31`` and this module
fills the first ``plan(rect).rows`` of them, truncated to ``plan(rect).chars``.
The numbers below are the template's pixel metrics (``column.html``) and are
pinned against it by ``tests/test_column_view.py``.

Everything is sized by ``metrics(scale)``: the panel's accessibility scale
(``panel.config.scale``) multiplies the pixel metrics, so a larger scale means
bigger type and fewer, shorter rows -- the template has no fixed size of its
own, it takes them as variables.

Views drawn here: ``log`` (newest events, one per row), ``calls`` (tool calls,
``name state``), ``status`` (the latest event as ``key: value`` rows). Any
other view token is shown as a one-line notice, never silently dropped.
"""

import json
import time

TEMPLATE = "column.html"
ROW_SLOTS = 32
HEAD_PX, PAD_PX = 96, 16               # header band, side/row-block padding
ROW_PX, CHAR_PX = 30, 13               # row pitch, conservative glyph width
MIN_CHARS = 8
VIEWS = ("log", "calls", "status")
ELLIPSIS = "…"


TITLE_PX, SUB_PX, FONT_PX = 34, 20, 22  # type sizes at scale 1
BORDER_PX = 3                           # rule under the header (not scaled)


def metrics(scale=1.0):
    """The template's pixel metrics at ``scale`` (integers, >= 1)."""
    def px(n):
        return max(1, int(round(n * scale)))
    return {"head": px(HEAD_PX), "pad": px(PAD_PX), "row": px(ROW_PX),
            "char": px(CHAR_PX), "title": px(TITLE_PX), "sub": px(SUB_PX),
            "font": px(FONT_PX), "title_pad": px(14)}


def plan(rect, scale=1.0):
    """``{"rows", "chars", "px"}`` that fit a region ``(x, y, w, h)`` at
    ``scale`` (0 rows when the header and padding alone overflow it); ``px``
    is ``metrics(scale)``."""
    _, _, w, h = rect
    m = metrics(scale)
    rows = (h - m["head"] - 2 * m["pad"]) // m["row"]
    chars = (w - 2 * m["pad"]) // m["char"]
    return {"rows": max(0, min(ROW_SLOTS, rows)), "chars": max(MIN_CHARS, chars),
            "px": m}


def fit(text, chars):
    text = " ".join(str(text).split())
    return text if len(text) <= chars else text[:max(1, chars - 1)] + ELLIPSIS


def _compact(data):
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(data)


def _clock(at):
    return time.strftime("%H:%M:%S", time.localtime(at)) if at else "--:--:--"


def _log_row(event):
    data = event.get("data")
    text = data.get("text", data.get("message")) if isinstance(data, dict) else data
    return "%s %s %s" % (_clock(event.get("at")), event["type"],
                         _compact(data if text is None else text))


def _calls_row(event):
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    return "%s %s" % (data.get("name", event["type"]),
                       data.get("state", data.get("status", "")))


def _status_rows(events):
    if not events:
        return []
    data = events[-1].get("data")
    if not isinstance(data, dict):
        return [_compact(data)]
    return ["%s: %s" % (k, _compact(v)) for k, v in sorted(data.items())]


def rows_for(view, events):
    """All rows for ``view``, oldest first (the caller keeps the tail)."""
    if view == "log":
        return [_log_row(e) for e in events]
    if view == "calls":
        return [_calls_row(e) for e in events]
    if view == "status":
        return _status_rows(events)
    return ["view %r is not drawn by displayd-html (log, calls, status)" % view]


def build_vars(column, stream, events, geometry):
    """The ``column.html`` variables for one column, sized to ``geometry``
    (a ``plan`` result): newest rows, each fitted, empty slots blank."""
    rows, chars = geometry["rows"], geometry["chars"]
    views = (stream or {}).get("views") or []
    if stream is None:
        shown, state = ["stream %s is not declared" % column["stream"]], "WAITING"
    elif views and column["view"] not in views:
        shown, state = ["stream offers %s, not %r" % (", ".join(views), column["view"])], "VIEW"
    else:
        shown, state = rows_for(column["view"], events), "LIVE"
    shown = [fit(r, chars) for r in shown[-rows:]] if rows else []
    count = "%d event%s" % (len(events), "" if len(events) == 1 else "s")
    out = {"title": fit((stream or {}).get("name") or column["stream"], chars),
           "sub": fit("%s \u00b7 %s \u00b7 %s" % (column["view"], state, count), chars)}
    for key, value in (geometry.get("px") or metrics()).items():
        if key != "char":  # a planning number only: the template has no glyph width
            out["px_" + key] = str(value)
    for i in range(ROW_SLOTS):
        out["r%d" % i] = shown[i] if i < len(shown) else ""
    return out
