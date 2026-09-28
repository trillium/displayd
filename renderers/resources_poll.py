"""Poll-thread lifecycle for the resources renderer.

Single concept: own the resident process-wide poll state (_POLL) and the
background thread that refreshes it. A failed poll never discards the
last good snapshot: it marks the state stale (or error when cold).
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from resources_proc import _poll_once

POLL_DEFAULT_INTERVAL = 5

# Resident process-wide poll state: survives view switches, so re-selecting
# this view is instantly populated, never empty.
_POLL = {
    "lock": threading.Lock(),
    "thread": None,
    "wake": threading.Event(),
    "cfg": {},
    "snapshot": None,
    "updated": 0.0,
    "health": "cold",  # cold | warm | stale | error
    "error": None,
    "prev_cpu": None,  # (total, idle) from the previous poll
}


def _poll_loop():
    while True:
        with _POLL["lock"]:
            cfg = dict(_POLL["cfg"])
            interval = cfg.get("interval") or POLL_DEFAULT_INTERVAL
            try:
                interval = max(2, min(3600, int(interval)))
            except (TypeError, ValueError):
                interval = POLL_DEFAULT_INTERVAL
        try:
            with _POLL["lock"]:
                prev = _POLL["prev_cpu"]
            snap, cur = _poll_once(cfg, prev)
            with _POLL["lock"]:
                _POLL["prev_cpu"] = cur
                _POLL["snapshot"] = snap
                _POLL["updated"] = time.time()
                _POLL["health"] = "warm"
                _POLL["error"] = None
        except Exception as err:
            with _POLL["lock"]:
                _POLL["error"] = str(err)[:160]
                # A failed poll never discards the last good frame's data:
                # keep the snapshot, mark it stale (or error when cold).
                if _POLL["snapshot"] is None:
                    _POLL["health"] = "error"
                else:
                    _POLL["health"] = "stale"
        _POLL["wake"].wait(max(2, interval))
        _POLL["wake"].clear()


def _ensure_poll(cfg):
    with _POLL["lock"]:
        _POLL["cfg"] = dict(cfg)
        alive = _POLL["thread"] is not None and _POLL["thread"].is_alive()
        if not alive:
            thread = threading.Thread(target=_poll_loop, daemon=True)
            _POLL["thread"] = thread
            thread.start()
        else:
            _POLL["wake"].set()


def _get_state():
    with _POLL["lock"]:
        return (dict(_POLL["cfg"]), _POLL["snapshot"], _POLL["updated"],
                _POLL["health"], _POLL["error"])
