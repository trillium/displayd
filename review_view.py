"""What the review decision page draws: variables and touch regions.

Single concept: the ``html-templates/review.html`` surface for one
``ReviewFlow`` snapshot. Pure: a snapshot in, escaped-scalar variables and
touch rects out. The pixel metrics below are the template's (1920x1080
design) and are pinned against it by ``tests/test_review_view.py``.

Layout: header 0-120, context 120-380, five option rows from y=400 on a
100px pitch, footer 920-1080 split into BACK/CLOSE (left half) and
CONFIRM/RETRY (right half). Rows are text, not framed buttons, so the unused
slots of a two-option bead are quiet rather than empty boxes.
"""

from review_decision import MAX_OPTIONS, fit
from review_summary import LABEL

TEMPLATE = "review.html"
DESIGN_W, DESIGN_H = 1920, 1080
ROWS_Y, ROW_PITCH = 400, 100
FOOT_Y = 920
TITLE_CHARS, CONTEXT_CHARS = 70, 560

EYEBROW = {"idle": "REVIEW", "showing": "REVIEW · CHOOSE", "confirming": "CONFIRM",
           "sending": "SENDING", "done": "SENT", "failed": "NOT SENT"}


def _context(snap):
    state, decision = snap["state"], snap["decision"]
    if state == "idle":
        return "No review bead loaded. POST /review/load, or run the review bridge."
    if state == "confirming":
        return "Send this answer? %s" % snap["chosen"]["text"]
    if state == "sending":
        return "Sending your answer to the review store..."
    if state in ("done", "failed"):
        tail = " Tap RETRY to send again, or BACK to choose." if state == "failed" else ""
        return "%s%s" % (snap["message"] or state, tail)
    body = LABEL + decision["summary"] if decision.get("summary") else decision["context"]
    if not decision["actionable"]:
        return "Read only: %s. %s" % (decision["reason"], body)
    return body


def _status(snap):
    decision = snap["decision"]
    if decision is None:
        return ""
    count = len(decision["options"])
    return "%s · %d option%s" % (decision["id"], count, "" if count == 1 else "s")


def build_vars(snap):
    """The ``review.html`` variables for a flow snapshot (all keys, always)."""
    state, decision = snap["state"], snap["decision"]
    choose = state == "showing" and decision is not None and decision["actionable"]
    opts = decision["options"] if choose else []
    out = {"eyebrow": EYEBROW[state],
           "title": fit(decision["title"], TITLE_CHARS) if decision else "Review decision",
           "status": _status(snap),
           "context": fit(_context(snap), CONTEXT_CHARS),
           "foot_left": {"confirming": "BACK", "failed": "BACK", "done": "DONE",
                         "sending": ""}.get(state, "CLOSE"),
           "foot_right": {"confirming": "CONFIRM", "failed": "RETRY"}.get(state, "")}
    for i in range(MAX_OPTIONS):
        out["o%d" % (i + 1)] = "%d   %s" % (i + 1, opts[i]["label"]) if i < len(opts) else ""
    return out


def review_regions(w=DESIGN_W, h=DESIGN_H):
    """``touch.json`` entries for the review view (scope ``view_regions.review``).

    Option rows, then the two footer halves. Scaled from the design size, so
    a panel that is not 1920x1080 still taps what it sees."""
    sx, sy = w / DESIGN_W, h / DESIGN_H

    def rect(x, y, rw, rh):
        left, top = round(x * sx), round(y * sy)  # snap edges, so neighbours abut
        return [left, top, round((x + rw) * sx) - left, round((y + rh) * sy) - top]

    out = [{"id": "rv-opt%d" % (i + 1),
            "rect": rect(0, ROWS_Y + i * ROW_PITCH, DESIGN_W, ROW_PITCH),
            "action": {"name": "review_select", "slot": i + 1}}
           for i in range(MAX_OPTIONS)]
    foot_h = DESIGN_H - FOOT_Y
    out.append({"id": "rv-back", "rect": rect(0, FOOT_Y, DESIGN_W // 2, foot_h),
                "action": {"name": "review_back"}})
    out.append({"id": "rv-confirm",
                "rect": rect(DESIGN_W // 2, FOOT_Y, DESIGN_W // 2, foot_h),
                "action": {"name": "review_confirm"}})
    return out
