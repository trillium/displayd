"""Single-bead detail card: drill down from the parade overview to one bead.

Selection is two-layered, per the ISA's PARAMS/INPUTS split:

- PARAM `focus` seeds the target when the view is SELECTED. This works on
  any core, old or new: POST /show {"renderer":"beads-detail",
  "params":{"focus":"task-nh3y"}}.
- INPUT `focus` steers the target while RUNNING, without re-selecting:
  POST /feed/beads-detail/focus {"bead_id":"task-nh3y"}. The daemon buffers
  pushes while this view is idle, so flipping between beads while streaming
  never lands on an empty card.

Target priority: newest pushed input > PARAM focus > last shown (module
memory, survives switch-away-and-back) > "no bead selected" card. A daemon
restart clears memory back to the PARAM (or the empty card).

Data comes from the same poll cache as the overview (mirror first, live
store export where CLIs exist). A bead missing from the mirror is fetched
once via `<store> show <id> --json` in a worker thread -- the draw never
waits on it. Every field is real store data; nothing is placeholder.
"""

import os
import sys
import time

from PIL import ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import beads_common as common
from beads_common import (
    BUCKET_BY_KEY,
    C_BG,
    C_DIM,
    C_LINE,
    C_STALLED,
    C_TEXT,
    DRAW_REFRESH,
    POLL_FALLBACK_INTERVAL,
    _age,
    _fit,
    _font,
    _wrap,
    age_of,
)

NAME = "beads-detail"
DESCRIPTION = "One bead in full: state, priority, blockers, dependents, latest notes"
STATIC = False
PARAMS = {
    "focus": {"type": "string", "help": "bead id to show, e.g. task-nh3y"},
    "mirror": {"type": "string", "help": "mirror file(s), comma-separated JSON/JSONL; auto-discovered when omitted"},
    "stores": {"type": "string", "help": "live stores for export/show fallback, comma-separated CLI names; default task,brain,robots,review,ideas; empty disables"},
    "interval": {"type": "integer", "help": "poll seconds, default 60"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}
INPUTS = {
    "focus": {
        "type": "object",
        "help": "steer the card live: {bead_id}",
        "required": ["bead_id"],
        "properties": {"bead_id": {"type": "string"}},
        "buffer": 10,
    },
}

PAD = 60
TITLE_SIZE = 64
HEAD_SIZE = 44
META_SIZE = 34
ROW_SIZE = 38
SMALL_SIZE = 30

# Last shown target: survives switch-away-and-back within a daemon lifetime.
_LAST_FOCUS = {"bead_id": None}


def _inputs(screen):
    try:
        getter = getattr(screen, "get_input", None)
    except Exception:
        return []
    if getter is None:  # old core: no feed mechanism, PARAM seeds only
        return []
    try:
        return getter(NAME, "focus") or []
    except Exception:
        return []


def current_target(screen, params_focus):
    """Newest pushed input > PARAM focus > memory > None."""
    for pushed in reversed(_inputs(screen)):
        if isinstance(pushed, dict) and pushed.get("bead_id"):
            bead_id = str(pushed["bead_id"]).strip()
            if bead_id:
                _LAST_FOCUS["bead_id"] = bead_id
                return bead_id
    if params_focus:
        _LAST_FOCUS["bead_id"] = params_focus
        return params_focus
    return _LAST_FOCUS["bead_id"]


def _state_line(issue):
    bits = ["P%d" % issue["priority"], issue["issue_type"]]
    who = issue.get("assignee") or issue.get("owner")
    if who:
        bits.append("owner %s" % who)
    if issue.get("created_at"):
        bits.append("age %s" % age_of(issue["created_at"]))
    if issue.get("updated_at"):
        bits.append("touched %s ago" % age_of(issue["updated_at"]))
    return " \u00b7 ".join(bits)


def _draw_card(screen, issue, bucket, waiters, dependents, snap,
               health, updated, source, bg):
    glyph, label, color = BUCKET_BY_KEY[bucket]
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font(screen, "DejaVuSans-Bold", TITLE_SIZE)
    sec_font = _font(screen, "DejaVuSans-Bold", HEAD_SIZE)
    meta_font = _font(screen, "DejaVuSans", META_SIZE)
    row_font = _font(screen, "DejaVuSans", ROW_SIZE)
    small_font = _font(screen, "DejaVuSans", SMALL_SIZE)
    plain = head_font or row_font
    W = screen.W - 2 * PAD
    bottom = screen.H - 70

    y = 24
    # Header: identity left, bucket pill + health right.
    ident = "[%s %s]" % (issue["store"], issue["id"])
    draw.text((PAD, y + 8), ident, font=meta_font or plain, fill=C_DIM)
    pill = "%s %s" % (glyph, label.upper())
    pill_w = 0
    if sec_font is not None:
        try:
            pill_w = draw.textlength(pill, font=sec_font)
        except Exception:
            pass
    draw.text((screen.W - PAD - pill_w, y), pill, font=sec_font or plain,
              fill=color)
    status = "%s \u00b7 %s" % (health, _age(updated))
    draw.text((screen.W - PAD - pill_w, y + 56),
              _fit(draw, status, small_font, pill_w, 40),
              font=small_font or plain, fill=C_DIM)
    y += 118
    draw.line([(PAD, y), (screen.W - PAD, y)], fill=C_LINE, width=2)
    y += 22

    # Title, up to three wall-sized rows.
    for row in _wrap(draw, issue["title"], head_font, W, 3):
        if y > bottom - 200:
            break
        draw.text((PAD, y), row, font=plain, fill=C_TEXT)
        y += TITLE_SIZE + 12
    y += 6
    draw.text((PAD, y), _fit(draw, _state_line(issue), meta_font, W),
              font=meta_font or plain, fill=C_DIM)
    y += META_SIZE + 22

    # About: first breath of the description for context, never the whole.
    about = (issue.get("description") or "").strip().splitlines()
    about = " ".join(line.strip() for line in about if line.strip())
    if about and y <= bottom - 200:
        for row in _wrap(draw, about, meta_font, W, 2):
            if y > bottom - 200:
                break
            draw.text((PAD, y), row, font=meta_font or plain, fill=C_DIM)
            y += META_SIZE + 10
        y += 10

    def section(head, color=C_TEXT):
        nonlocal_y = [y]

        def emit(line, font, fill, indent=0):
            yy = nonlocal_y[0]
            if yy > bottom - 40:
                return False
            draw.text((PAD + indent, yy),
                      _fit(draw, line, font, W - indent),
                      font=font or plain, fill=fill)
            nonlocal_y[0] = yy + ROW_SIZE + 10
            return True

        if nonlocal_y[0] > bottom - 120:
            return None
        draw.text((PAD, nonlocal_y[0]), head, font=sec_font or plain,
                  fill=color)
        nonlocal_y[0] += HEAD_SIZE + 12
        return emit

    # Why this bucket: the derivation, not a status read.
    emit = section("WHY " + label.upper(), C_STALLED)
    if emit is not None:
        if bucket == "stalled":
            for w in waiters[:3]:
                if not emit("waits on %s \u2014 %s (%s)" % (
                        w["id"], w["title"], w["status"].upper()),
                        row_font, C_TEXT, indent=20):
                    break
        elif bucket == "past":
            closed = issue.get("closed_at")
            note = "closed"
            if closed:
                note += " " + str(closed)[:10]
            if issue.get("close_reason"):
                note += " \u2014 %s" % issue["close_reason"]
            emit(note, row_font, C_TEXT, indent=20)
        else:
            emit("nothing unfinished in the way", row_font, C_TEXT, indent=20)

    # What it holds up.
    live_deps = [d for d in dependents if d["status"] != "closed"]
    if live_deps:
        emit = section("HOLDS UP %d" % len(live_deps))
        if emit is not None:
            for d in live_deps[:2]:
                if not emit("%s \u2014 %s (%s)" % (
                        d["id"], d["title"], d["status"].upper()),
                        row_font, C_TEXT, indent=20):
                    break

    # Latest notes: the freshest few earn wall space, history does not.
    comments = issue.get("comments") or []
    if comments:
        emit = section("LATEST")
        if emit is not None:
            for c in comments[-3:]:
                line = "%s: %s" % (c.get("author") or "?",
                                   (c.get("text") or "").splitlines()[0]
                                   if (c.get("text") or "").splitlines() else "")
                if c.get("created_at"):
                    line += "  [%s ago]" % age_of(c["created_at"])
                if not emit(line, row_font, C_TEXT, indent=20):
                    break

    # Footer: provenance + the way back to the overview.
    foot = "back: POST /show {\"renderer\":\"beads\"}"
    if source:
        foot += "   (%s)" % source
    draw.text((PAD, screen.H - 56), _fit(draw, foot, small_font, W),
              font=small_font or plain, fill=C_DIM)
    screen.present(img)


def _draw_empty(screen, target, health, updated, error, bg, hint):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    row_font = _font(screen, "DejaVuSans", ROW_SIZE)
    small_font = _font(screen, "DejaVuSans", SMALL_SIZE)
    plain = row_font
    if target:
        msg = "no such bead: %s" % target
    else:
        msg = "no bead selected \u2014 POST /show {\"renderer\":\"beads-detail\", \"params\":{\"focus\":\"<id>\"}}"
    draw.text((PAD, 220), _fit(draw, msg, row_font, screen.W - 2 * PAD),
              font=plain, fill=C_DIM)
    draw.text((PAD, 300), _fit(draw, hint, small_font, screen.W - 2 * PAD),
              font=small_font or plain, fill=C_DIM)
    if error:
        draw.text((PAD, 360),
                  _fit(draw, "last error: " + error, small_font,
                       screen.W - 2 * PAD),
                  font=small_font or plain, fill=(255, 90, 90))
    screen.present(img)


def _snapshot_key(target):
    _, snap, updated, health, _, _ = common.get_state()
    with common._POLL["lock"]:
        focus_cached = tuple(sorted(common._POLL["focus_cache"]))
    if snap is None:
        return ("cold", target, health)
    return (target, updated, len(snap["rolling"]), len(snap["linedup"]),
            len(snap["stalled"]), len(snap["past"]), health, focus_cached)


def run(screen, params, stop):
    params = params or {}
    params_focus = str(params.get("focus") or "").strip() or None
    bg = screen.color(params.get("background"), C_BG)
    try:
        interval = int(params.get("interval") or POLL_FALLBACK_INTERVAL)
    except (TypeError, ValueError):
        interval = POLL_FALLBACK_INTERVAL
    interval = max(5, min(3600, interval))
    stores = params.get("stores")
    if stores is None:
        stores = "task,brain,robots,review,ideas"
    store_list = [s.strip() for s in str(stores).split(",") if s.strip()]
    common.ensure_poll({"mirror": params.get("mirror") or "",
                        "stores": stores, "interval": interval,
                        "focus": params_focus or ""})

    def frame():
        target = current_target(screen, params_focus)
        cfg, snap, updated, health, error, source = common.get_state()
        if target and (snap is None or
                       common.find_in_snapshot(snap, target) is None):
            # Not in the mirror (or no mirror yet): fetch live in a worker.
            # The draw below still goes up immediately from cache.
            common.request_focus(target, store_list)
        if snap is None:
            _draw_empty(screen, target, health, updated, error, bg,
                        "waiting for first poll \u2014 mirrors or store export")
            return
        if not target:
            _draw_empty(screen, None, health, updated, error, bg,
                        _age(updated))
            return
        issue, _ = common.resolve_focus(target, snap)
        if issue is None:
            _draw_empty(screen, target, health, updated, error, bg,
                        "still looking \u2014 %s" % _age(updated))
            return
        bucket, waiters = common.bucket_of(issue, snap)
        dependents = common.dependents_of(issue, snap)
        _draw_card(screen, issue, bucket, waiters, dependents, snap,
                   health, updated, source, bg)

    # First frame goes up immediately from cache (or the empty card) --
    # switching here never waits on I/O.
    try:
        frame()
    except Exception:
        pass
    last_key = _snapshot_key(current_target(screen, params_focus))
    last_draw = time.time()
    while not stop.is_set():
        try:
            target = current_target(screen, params_focus)
            key = _snapshot_key(target)
        except Exception:
            key = last_key
        now = time.time()
        if key != last_key or (now - last_draw) >= DRAW_REFRESH:
            last_key = key
            last_draw = now
            try:
                frame()
            except Exception:
                # A draw failure must not kill the daemon loop; the last
                # good frame stays on the panel.
                pass
        stop.wait(1.0)
