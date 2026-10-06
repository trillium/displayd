"""Wall-friendly ages for the beads views.

Single concept: every "how long ago" string a beads frame shows -- the poll
freshness line ("updated 3m ago") and the compact bead age ("12d") -- from
one ISO timestamp parser. Pure except for the wall clock; never raises.
"""

import time


def _parse_ts(value):
    try:
        text = str(value).strip().replace("Z", "+00:00")
        import datetime as _dt
        return _dt.datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def _age(updated):
    if not updated:
        return "no data yet"
    secs = max(0, time.time() - updated)
    if secs < 60:
        return "updated %ds ago" % int(secs)
    if secs < 3600:
        return "updated %dm ago" % int(secs // 60)
    return "updated %dh ago" % int(secs // 3600)


def age_of(iso_ts, now=None):
    """Wall-friendly age of an ISO timestamp: 12d, 3h, 20m."""
    ts = _parse_ts(iso_ts)
    if ts is None:
        return "?"
    secs = max(0, (now if now is not None else time.time()) - ts)
    if secs < 3600:
        return "%dm" % max(1, int(secs // 60))
    if secs < 86400 * 2:
        return "%dh" % int(secs // 3600)
    if secs < 86400 * 60:
        return "%dd" % int(secs // 86400)
    return "%dmo" % int(secs // (86400 * 30))
