"""Talking to displayd from the macbook bridge: state POST, commands.

Single concept: the two directions of the bridge's wire -- POSTing one state
payload to /feed/macbook/state, and fetching the pending cursor/click
command (a bounded long-poll hold so a tap wakes it early) plus the atomic
CGWarpMouseCursorPosition that lands it. Every failure is best-effort: a
missed command moves nothing.
"""

import json
import logging
import urllib.request

LOG = logging.getLogger("macos-state-bridge")

TIMEOUT = 5.0
MOUSE_FETCH_TIMEOUT = 2.0  # idle command fetch must never slow the 2 Hz tick
LONGPOLL_WAIT_MAX = 2.0  # fetch hold never exceeds this; the tick keeps
# its cadence (state POSTs every loop), taps just wake the fetch early.
MOUSE_TTL = 10.0  # mirrors the daemon slot: stale taps never fire


def make_sender(displayd_base):
    """POST one payload to /feed/macbook/state; True on success."""
    url = displayd_base.rstrip("/") + "/feed/macbook/state"

    def send(payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp: resp.read(1024)
        except Exception as err:
            LOG.info("displayd unreachable (%s); retrying", err)
            return False
        return True
    return send


def fetch_mouse_command(displayd_base, since=0.0, wait=0.0):
    """Pending cursor command newer than `since`; None when idle.
    Best-effort: any failure means no move -- the tap simply does not
    fire, and the daemon TTL-expires it, so failure can never land the
    cursor somewhere unexpected. `wait` holds the GET on the daemon
    (bounded server-side) so a tap queued mid-hold wakes this fetch;
    0 is today's immediate reply. The HTTP timeout always covers the
    hold, so a hung turn is impossible."""
    url = (displayd_base.rstrip("/") + "/macbook/mouse"
           + "?since=%s" % since)
    if wait and wait > 0:
        url += "&wait=%s" % wait
    try:
        with urllib.request.urlopen(
                url,
                timeout=MOUSE_FETCH_TIMEOUT + max(0.0, wait)) as resp:
            doc = json.loads(resp.read(4096).decode("utf-8", "replace"))
        cmd = doc.get("command") if isinstance(doc, dict) else None
        x, y, ts = float(cmd["x"]), float(cmd["y"]), float(cmd.get("ts", 0))
    except Exception as err:
        LOG.debug("mouse fetch skipped (%s)", err)
        return None
    if abs(x) > 100000 or abs(y) > 100000:
        LOG.warning("mouse command out of range ignored: %r", cmd)
        return None
    return {"x": x, "y": y, "ts": ts,
            "display_index": cmd.get("display_index")}


def warp_mouse(x, y):
    """Move the cursor to Quartz (x, y) in one atomic OS call.

    Direct CGWarpMouseCursorPosition, NOT Talon: Talon follows the OS
    cursor (verified 2026-09-28 -- a bare warp reads back identically
    through the Talon REPL), so there is no desync to avoid and no
    Talon-running dependency to add. Either the whole point lands or
    nothing does; raises on failure so the caller logs and skips."""
    import Quartz
    Quartz.CGWarpMouseCursorPosition((float(x), float(y)))
