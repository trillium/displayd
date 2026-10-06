"""The zoom/click wire between the MacBook bridge and the panel.

Single concept: the two documents that cross the feed boundary for a
magnified tap -- POSTing one zoom capture to /feed/macbook/zoom, and
fetching the panel's pending click command (with an optional bounded
long-poll hold so a tap wakes the fetch early), including the staleness TTL
past which a queued click is refused rather than fired late.
"""

import json
import time
import urllib.request

CLICK_TTL = 15.0  # mirrors the daemon slot: stale taps never fire


def zoom_doc(x, y, jpeg_text, now):
    """One /feed/macbook/zoom document. Never raises (None on garbage)."""
    try:
        if not jpeg_text or not isinstance(jpeg_text, str):
            return None
        return {"ts": float(now), "x": float(x), "y": float(y),
                "jpeg": jpeg_text}
    except (TypeError, ValueError):
        return None


def post_zoom(displayd_base, doc, timeout=5.0):
    """POST one zoom document to the panel feed; True on success."""
    url = displayd_base.rstrip("/") + "/feed/macbook/zoom"
    req = urllib.request.Request(url, data=json.dumps(doc).encode("utf-8"),
                                 headers={"Content-Type":
                                          "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1024)
    except Exception:
        return False
    return True


def fetch_click_command(displayd_base, since=0.0, timeout=2.0, wait=0.0):
    """Pending click newer than `since`; None when idle or stale.
    `wait` holds the GET on the daemon (bounded server-side) so a tap
    queued mid-hold wakes this fetch; 0 is today's immediate reply.
    The HTTP timeout always covers the hold, so a hung turn is
    impossible."""
    url = (displayd_base.rstrip("/") + "/macbook/click"
           + "?since=%s" % since)
    if wait and wait > 0:
        url += "&wait=%s" % wait
    try:
        with urllib.request.urlopen(url,
                                     timeout=timeout + max(0.0, wait)) as resp:
            doc = json.loads(resp.read(4096).decode("utf-8", "replace"))
        cmd = doc.get("command") if isinstance(doc, dict) else None
        x, y, ts = float(cmd["x"]), float(cmd["y"]), float(cmd.get("ts", 0))
    except Exception:
        return None
    if abs(x) > 100000 or abs(y) > 100000:
        return None
    if time.time() - ts > CLICK_TTL:
        return None
    return {"x": x, "y": y, "ts": ts,
            "display_index": cmd.get("display_index")}
