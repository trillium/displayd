"""The resident beads poll cache: one snapshot for every beads view.

Single concept: the process-wide poll state -- the background poll thread
that keeps one snapshot warm from beads_source, the health/staleness marks
that survive a failed poll, and the live single-bead fetch used when a focus
target is missing from the mirror. Resident by design: re-selecting any
beads view is instantly populated, never empty. All I/O happens on this
thread, off the draw path.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from beads_buckets import _classify, find_in_snapshot
from beads_common import POLL_FALLBACK_INTERVAL
from beads_issue import _norm_issue
from beads_source import _poll_once, _show_bead


# Resident process-wide poll state: survives view switches, so re-selecting
# any beads view is instantly populated, never empty.
_POLL = {
    "lock": threading.Lock(),
    "thread": None,
    "wake": threading.Event(),
    "cfg": {},
    "snapshot": None,   # dict with buckets, attention, stores, index, bare
    "updated": 0.0,
    "health": "cold",   # cold | warm | stale | error
    "error": None,
    "source": None,
    "focus_cache": {},  # bead_id -> normalized issue (CLI-fetched extras)
    "focus_fetching": set(),
}


def _fetch_focus_worker(bead_id, stores):
    try:
        for store in stores:
            hit = _show_bead(store, bead_id)
            if hit is not None:
                norm = _norm_issue(hit[0], hit[1])
                if norm is not None:
                    with _POLL["lock"]:
                        _POLL["focus_cache"][bead_id] = norm
                        _POLL["focus_fetching"].discard(bead_id)
                return
    except Exception:
        pass
    finally:
        with _POLL["lock"]:
            _POLL["focus_fetching"].discard(bead_id)


def request_focus(bead_id, stores):
    """Ensure a bead outside the mirror gets fetched live. Non-blocking:
    spawns one worker at most per id; the draw keeps showing cache meanwhile."""
    if not bead_id:
        return
    with _POLL["lock"]:
        if bead_id in _POLL["focus_cache"] or bead_id in _POLL["focus_fetching"]:
            return
        _POLL["focus_fetching"].add(bead_id)
    thread = threading.Thread(target=_fetch_focus_worker,
                              args=(bead_id, list(stores or ())), daemon=True)
    thread.start()


def resolve_focus(bead_id, snap):
    """Best-known normalized issue for a bead id: snapshot first, then the
    live-fetch cache. Returns (issue, source) with issue possibly None."""
    issue = find_in_snapshot(snap, bead_id)
    if issue is not None:
        return issue, "mirror"
    with _POLL["lock"]:
        issue = _POLL["focus_cache"].get(bead_id)
    if issue is not None:
        return issue, "live"
    return None, None


def get_state():
    with _POLL["lock"]:
        return (dict(_POLL["cfg"]), _POLL["snapshot"], _POLL["updated"],
                _POLL["health"], _POLL["error"], _POLL["source"])


def _poll_loop():
    last_sig = None
    while True:
        with _POLL["lock"]:
            cfg = dict(_POLL["cfg"])
            interval = cfg.get("interval") or POLL_FALLBACK_INTERVAL
            interval = max(5, min(3600, int(interval)))
            sig = (cfg.get("mirror"), cfg.get("stores"), interval)
            focus_param = (cfg.get("focus") or "").strip()
            stores = [s.strip() for s in str(cfg.get("stores") or "").split(",")
                      if s.strip()]
        if sig != last_sig:
            last_sig = sig
        try:
            pairs, source = _poll_once(cfg)
            snap = _classify(pairs)
            if focus_param and find_in_snapshot(snap, focus_param) is None:
                request_focus(focus_param, stores)
            with _POLL["lock"]:
                _POLL["snapshot"] = snap
                _POLL["updated"] = time.time()
                _POLL["health"] = "warm"
                _POLL["error"] = None
                _POLL["source"] = source
        except Exception as err:
            with _POLL["lock"]:
                _POLL["error"] = str(err)[:160]
                # A failed poll never discards the last good frame's data:
                # keep the snapshot, mark it stale (or error when cold).
                if _POLL["snapshot"] is None:
                    _POLL["health"] = "error"
                else:
                    _POLL["health"] = "stale"
        _POLL["wake"].wait(max(5, interval))
        _POLL["wake"].clear()


def ensure_poll(cfg):
    with _POLL["lock"]:
        _POLL["cfg"] = dict(cfg)
        alive = _POLL["thread"] is not None and _POLL["thread"].is_alive()
        if not alive:
            thread = threading.Thread(target=_poll_loop, daemon=True)
            _POLL["thread"] = thread
            thread.start()
        else:
            _POLL["wake"].set()
