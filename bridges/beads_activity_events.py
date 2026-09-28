"""Beads-bridge activity event validation (pure functions, no I/O).

Single concept: turn one raw upstream ``/live/recent`` event into the
validated displayd feed payload, or refuse it (return None, never raise).
Every field is truncated/capped here so a hostile or drifting upstream
can never grow the panel payload without bound; the bridge
(``beads_activity.py``) additionally caps events forwarded per poll and
the size of its seen-set.

Protocol notes (from beads-bridge docs/live-activity.md -- do not re-derive):
  * ``GET /live/recent?limit=N`` answers ``{"events": [...]}`` newest-first;
  * an ActivityEvent carries ``seq, at, tool, outcome ('ok'|'error'),
    caller, sessionId, client, authed, durationMs, argNames, beadRefs,
    summary`` -- arg NAMES only, values never recorded;
  * the ring is ephemeral (restart clears it); ``seq`` is monotonic per
    process, so a restart (seq resets) is detected by the bridge, which
    reseeds its seen-set rather than replaying.
"""

import re

# ---- bounds (mirror the sidecar's server-side caps) ---------------------------

SUMMARY_CHARS = 300   # ACTIVITY_SUMMARY_CHARS default
MAX_BEADREFS = 10     # ACTIVITY_MAX_BEADREFS default
MAX_ARG_NAMES = 20
ARG_NAME_LEN = 64
TOOL_LEN = 64
CALLER_LEN = 64
CLIENT_LEN = 32
SESSION_LEN = 64
AT_LEN = 40
SUMMARY_IN_LEN = 4096  # inbound head accepted before truncation

# Same id shape as the sidecar's extractBeadRefs (activity.ts), minus the
# storeFromId check this side cannot run: free-text hyphenations
# ("follow-on", "end-to-end") fail the shape test and are dropped.
BEAD_RE = re.compile(r"^[a-z][a-z0-9]+-[a-z0-9]{3,}(?:\.[0-9]+)*$")


# ---- validation (pure functions, test hooks) ----------------------------------

def _str(value, max_len):
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_len]


def _str_list(value, max_items, max_len):
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        text = _str(item, max_len)
        if text:
            out.append(text)
            if len(out) >= max_items:
                break
    return out


def normalize_event(raw):
    """Validate one upstream event -> displayd feed payload, or None.

    Refusals (return None, never raise): non-dict input, missing/empty
    ``tool``, ``outcome`` outside {'ok', 'error'}. Everything else is
    coerced and truncated so a hostile or drifting upstream can never grow
    the panel payload without bound.
    """
    if not isinstance(raw, dict):
        return None
    tool = _str(raw.get("tool"), TOOL_LEN)
    if not tool:
        return None
    outcome = raw.get("outcome")
    if outcome not in ("ok", "error"):
        return None
    try:
        seq = int(raw.get("seq")) if raw.get("seq") is not None else None
    except (TypeError, ValueError):
        seq = None
    try:
        duration = raw.get("durationMs")
        duration_ms = int(duration) if duration is not None else 0
    except (TypeError, ValueError):
        duration_ms = 0
    duration_ms = max(0, min(3600000, duration_ms))
    session = raw.get("sessionId")
    bead_refs = [b for b in _str_list(raw.get("beadRefs"), MAX_BEADREFS,
                                      SESSION_LEN)
                 if BEAD_RE.match(b)]
    event = {
        "seq": seq,
        "at": _str(raw.get("at"), AT_LEN),
        "tool": tool,
        "outcome": outcome,
        "caller": _str(raw.get("caller"), CALLER_LEN),
        "sessionId": _str(session, SESSION_LEN),
        "client": _str(raw.get("client"), CLIENT_LEN),
        "authed": bool(raw.get("authed")),
        "durationMs": duration_ms,
        "argNames": _str_list(raw.get("argNames"), MAX_ARG_NAMES, ARG_NAME_LEN),
        "beadRefs": bead_refs,
        "summary": _str(raw.get("summary"), SUMMARY_IN_LEN)[:SUMMARY_CHARS],
    }
    # Absent upstream values stay absent: the daemon validates present
    # fields (None is not a valid string/number), and the renderer reads
    # everything through .get with defaults.
    return {k: v for k, v in event.items() if v is not None}
