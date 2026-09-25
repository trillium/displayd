"""Tap diagnostics for the touch_confidence renderer.

Reads buffered tap payloads from the touch_confidence/tap feed and rolls
them into counters. Pure aggregation: unit-testable without a screen.
"""


def valid_taps(screen):
    """Buffered tap payloads that carry numeric x/y, oldest first.
    Non-dict or coordinate-less entries are skipped (defensive: the feed
    API normally rejects those, but the renderer must never crash)."""
    taps = []
    try:
        buffered = screen.get_input("touch_confidence", "tap")
    except Exception:
        return []
    for payload in buffered or []:
        if not isinstance(payload, dict):
            continue
        x, y = payload.get("x"), payload.get("y")
        if isinstance(x, bool) or isinstance(y, bool):
            continue
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        taps.append(payload)
    return taps


def summarize(taps):
    """Roll buffered taps into counters. Pure: unit-testable without a screen."""
    total = len(taps)
    hits = 0
    per_region = {}
    for tap in taps:
        rid = tap.get("region") if isinstance(tap, dict) else None
        hit = tap.get("hit") if isinstance(tap, dict) else None
        if hit is None:
            hit = bool(rid)
        if hit:
            hits += 1
            if isinstance(rid, str) and rid:
                per_region[rid] = per_region.get(rid, 0) + 1
    last = taps[-1] if taps else None
    return {
        "total": total,
        "hits": hits,
        "misses": total - hits,
        "per_region": per_region,
        "last": last,
    }


def last_line(summary):
    """One-line text for the most recent tap (or the idle message)."""
    last = summary.get("last")
    if not last:
        return "waiting for taps"
    x, y = last.get("x"), last.get("y")
    rid = last.get("region")
    action = last.get("action")
    if isinstance(action, dict):
        action = action.get("name")
    if rid:
        return "last %s,%s \u2192 %s (%s)" % (x, y, rid, action or "no action")
    return "last %s,%s \u2192 DEAD ZONE \u2014 no action" % (x, y)


# Historical name kept so existing imports keep working.
_last_line = last_line
