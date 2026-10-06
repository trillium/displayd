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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import beads_buckets as buckets
import beads_poll as poll
from beads_age import _age
from beads_common import (
    C_BG,
    DRAW_REFRESH,
    POLL_FALLBACK_INTERVAL,
)

import beads_detail_card as _card

# Re-exported for backwards compatibility (card painters live in
# beads_detail_card.py; this view owns selection, polling, and the loop).
PAD = _card.PAD
TITLE_SIZE = _card.TITLE_SIZE
HEAD_SIZE = _card.HEAD_SIZE
META_SIZE = _card.META_SIZE
ROW_SIZE = _card.ROW_SIZE
SMALL_SIZE = _card.SMALL_SIZE
_state_line = _card._state_line
_draw_card = _card._draw_card
_draw_empty = _card._draw_empty

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


def empty_state(target, snap, updated):
    """The honest empty card, as (headline, subline). Pure: unit-tested.

    The rule that matters: a missing snapshot means the poll has not
    returned, never that the bead does not exist."""
    select_hint = ("no bead selected \u2014 POST /show {\"renderer\":\"beads-detail\", "
                   "\"params\":{\"focus\":\"<id>\"}}")
    if snap is None:
        if target:
            return ("looking for %s \u2026" % target,
                    "waiting for first poll \u2014 mirrors or store export")
        return (select_hint,
                "waiting for first poll \u2014 mirrors or store export")
    if not target:
        return (select_hint, _age(updated))
    return ("no such bead: %s" % target,
            "still looking \u2014 %s" % _age(updated))


def _snapshot_key(target):
    _, snap, updated, health, _, _ = poll.get_state()
    with poll._POLL["lock"]:
        focus_cached = tuple(sorted(poll._POLL["focus_cache"]))
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
    poll.ensure_poll({"mirror": params.get("mirror") or "",
                        "stores": stores, "interval": interval,
                        "focus": params_focus or ""})

    def frame():
        target = current_target(screen, params_focus)
        cfg, snap, updated, health, error, source = poll.get_state()
        if target and (snap is None or
                       buckets.find_in_snapshot(snap, target) is None):
            # Not in the mirror (or no mirror yet): fetch live in a worker.
            # The draw below still goes up immediately from cache.
            poll.request_focus(target, store_list)
        if snap is None or not target:
            msg, hint = empty_state(target, snap, updated)
            _draw_empty(screen, msg, hint, error, bg)
            return
        issue, _ = poll.resolve_focus(target, snap)
        if issue is None:
            msg, hint = empty_state(target, snap, updated)
            _draw_empty(screen, msg, hint, error, bg)
            return
        bucket, waiters = buckets.bucket_of(issue, snap)
        dependents = buckets.dependents_of(issue, snap)
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
