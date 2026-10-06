"""Machine resources for the jumbotron: CPU, load, memory, disk, uptime.

Polled-state archetype: this renderer polls on its own interval in a
background thread and draws from cache. A poll never blocks a draw, a
missing or malformed /proc never kills the frame, and a cold start (first
poll not back yet) renders a sensible waiting frame -- never blank, never
broken.

Data: /proc/stat, /proc/meminfo, /proc/loadavg, /proc/uptime, and
os.statvfs for disk. Nothing is shelled out to: /proc is cheap, always
present, and never blocks.

CPU percentage is a *delta between consecutive polls*, never an
instantaneous reading. The host this panel serves has run at very high
load, so a naive single-sample read would show nonsense. The first poll
only banks a sample; the frame says "sampling" until the second poll
lands a real delta.

Split across resources_proc.py (/proc sampling), resources_poll.py
(poll-thread lifecycle), and resources_draw.py (frame rendering) to stay
within the project's 250-line budget; names used by existing tests are
re-exported here.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import theme
from resources_draw import _draw
from resources_poll import (
    POLL_DEFAULT_INTERVAL,
    _POLL,
    _ensure_poll,
    _get_state,
    _poll_loop,
)
from resources_proc import (
    _cpu_pct,
    _parse_cpu_line,
    _parse_loadavg,
    _parse_meminfo,
    _poll_once,
    _read,
)

NAME = "resources"
DESCRIPTION = "Machine resources: CPU, load average, memory, swap, disk, uptime"
STATIC = False
PARAMS = {
    "title": {"type": "string", "help": "header text, default RESOURCES"},
    "mounts": {"type": "string", "help": "comma-separated mount points to show, default /"},
    "interval": {"type": "integer", "help": "poll seconds, default 5"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}

DRAW_REFRESH = 5  # re-render at least this often so the age line stays honest


def _snapshot_key():
    _, snap, updated, health, _ = _get_state()
    if snap is None:
        return ("cold", health)
    return (updated, snap["cpu_pct"], snap["load"], snap["mem_used"],
            [(d.get("mount"), d.get("used")) for d in snap["disks"]], health)


def run(screen, params, stop):
    params = params or {}
    title = str(params.get("title") or "RESOURCES").upper()
    bg = screen.color(params.get("background"), theme.rgb("page"))
    try:
        interval = int(params.get("interval") or POLL_DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = POLL_DEFAULT_INTERVAL
    interval = max(2, min(3600, interval))
    _ensure_poll({"mounts": params.get("mounts") or "/", "interval": interval})

    # First frame goes up immediately from cache (or the cold frame) --
    # switching here never waits on I/O.
    _draw(screen, title, bg)
    last_key = _snapshot_key()
    last_draw = time.time()
    while not stop.is_set():
        key = _snapshot_key()
        now = time.time()
        if key != last_key or (now - last_draw) >= DRAW_REFRESH:
            last_key = key
            last_draw = now
            try:
                _draw(screen, title, bg)
            except Exception:
                # A draw failure must not kill the daemon loop; the last
                # good frame stays on the panel.
                pass
        stop.wait(1.0)
