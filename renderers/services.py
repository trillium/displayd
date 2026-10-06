"""Service and container status for the jumbotron: what is actually running.

Polled-state archetype: this renderer polls on its own interval in a
background thread and draws from cache. A poll never blocks a draw, a
down or malformed source never kills the frame, and a cold start (first
poll not back yet) renders a sensible waiting frame -- never blank, never
broken.

This view reimplements no discovery. It consumes the lnx-viz inventory
API on this same host (`GET <url>` returning `containers`, `systemd`,
`listeners`, `services`, `host_uptime`, `hostname`, `generated_at`) and
formats it for a wall: tallies of up/down/failed, the failed units by
name, container state, a watch list of named services, and the notable
listening ports. If lnx-viz is not answering, the panel says so -- never
a blank frame, never stale data passed off as fresh.

Split across services_shape.py (inventory shaping), services_poll.py
(poll-thread lifecycle), and services_draw.py (frame rendering) to stay
within the project's 250-line budget; names used by existing tests are
re-exported here.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import theme
from services_draw import _draw
from services_poll import (
    DEFAULT_URL,
    FETCH_TIMEOUT,
    POLL_DEFAULT_INTERVAL,
    _POLL,
    _ensure_poll,
    _fetch,
    _get_state,
    _poll_loop,
    _poll_once,
)
from services_shape import DEFAULT_WATCH, _build_snapshot

NAME = "services"
DESCRIPTION = "Service status: up/down/failed tallies, failed units, containers, watched services, listening ports (via lnx-viz inventory)"
STATIC = False
PARAMS = {
    "title": {"type": "string", "help": "header text, default SERVICES"},
    "url": {"type": "string", "help": "inventory URL, default http://127.0.0.1:8181/api/inventory"},
    "watch": {"type": "string", "help": "comma-separated service names to spotlight, default displayd.service,lnx-viz.service,docker.service,containerd.service"},
    "interval": {"type": "integer", "help": "poll seconds, default 30"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}

DRAW_REFRESH = 15  # re-render at least this often so the age line stays honest


def _snapshot_key():
    _, snap, updated, health, _ = _get_state()
    if snap is None:
        return ("cold", health)
    return (updated, snap["up"], snap["down"], snap["failed"],
            [c["state"] for c in snap["containers"]],
            [(w["name"], w["state"]) for w in snap["watched"]], health)


def run(screen, params, stop):
    params = params or {}
    title = str(params.get("title") or "SERVICES").upper()
    bg = screen.color(params.get("background"), theme.rgb("page"))
    url = str(params.get("url") or DEFAULT_URL)
    try:
        interval = int(params.get("interval") or POLL_DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = POLL_DEFAULT_INTERVAL
    interval = max(5, min(3600, interval))
    _ensure_poll({"url": url,
                  "watch": params.get("watch") or DEFAULT_WATCH,
                  "interval": interval})

    # First frame goes up immediately from cache (or the cold frame) --
    # switching here never waits on I/O.
    _draw(screen, title, bg, url)
    last_key = _snapshot_key()
    last_draw = time.time()
    while not stop.is_set():
        key = _snapshot_key()
        now = time.time()
        if key != last_key or (now - last_draw) >= DRAW_REFRESH:
            last_key = key
            last_draw = now
            try:
                _draw(screen, title, bg, url)
            except Exception:
                # A draw failure must not kill the daemon loop; the last
                # good frame stays on the panel.
                pass
        stop.wait(1.0)
