#!/usr/bin/env python3
"""Beads-bridge activity -> displayd bridge (polling).

The beads-bridge sidecar already publishes every completed MCP tool call as
an observational live view (``GET /live/recent`` + ``GET /live/events``
SSE). This bridge polls that ring and pushes each new event across the
tailnet into displayd's feed API::

    beads-bridge http://<bridge-host>:3737 --HTTP--> bridge --HTTP--> displayd

The bridge owns the beads-bridge protocol; displayd stays content-agnostic
and only ever sees validated ``ActivityEvent`` payloads on
``/feed/activity/event`` (``renderers/activity.py``).

Protocol notes (from beads-bridge docs/live-activity.md -- do not re-derive):
  * ``GET /live/recent?limit=N`` answers ``{"events": [...]}`` newest-first;
  * ``GET /live/config`` answers ``{"autoFollowDefault": true,
    "maxEvents": 200}``;
  * an ActivityEvent carries ``seq, at, tool, outcome ('ok'|'error'),
    caller, sessionId, client, authed, durationMs, argNames, beadRefs,
    summary`` -- arg NAMES only, values never recorded;
  * the ring is ephemeral (restart clears it); ``seq`` is monotonic per
    process, so a restart (seq resets) is detected and the seen-set is
    reseeded rather than replayed.

Safety model (inherited from the sidecar -- do not weaken):
  * observational only: this bridge never mutates a bead, creates work, or
    writes to a store -- it polls two GET endpoints and POSTs feed payloads;
  * bounded payloads and bounded memory: every field is truncated/capped,
    one poll forwards at most POLL_CAP events, the seen-set is capped;
  * loop prevention: nothing here triggers MCP tool calls, so the ring
    cannot re-trigger itself through this bridge;
  * failure isolation: a dead upstream or a dead displayd is a reconnect
    with backoff, never a process death; a malformed event is dropped with
    a debug log, never forwarded.

Stdlib only, no credentials anywhere (neither endpoint needs any). Exits
non-zero only on configuration errors; a dropped upstream connection is a
reconnect, never a death.
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

LOG = logging.getLogger("beads-activity-bridge")

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

SEEN_CAP = 1000
POLL_CAP = 50          # max events forwarded from a single poll
BACKOFF_FIRST = 1.0
BACKOFF_MAX = 30.0
HEALTHY_RESET_AFTER = 30.0  # a poll run living this long resets the ladder

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


# ---- bridge -------------------------------------------------------------------

class ActivityBridge:
    """One bridge: beads-bridge /live/recent -> displayd /feed/activity/event."""

    def __init__(self, bridge_base, displayd_base, interval=3.0, limit=50,
                 backfill=8, fetch_fn=None, post_fn=None):
        if not bridge_base:
            raise ValueError("beads-bridge base URL is required")
        if not displayd_base:
            raise ValueError("displayd base URL is required")
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            raise ValueError("interval must be a number")
        if not 1.0 <= interval <= 120.0:
            raise ValueError("interval must be within 1..120 seconds")
        self.bridge_base = bridge_base.rstrip("/")
        self.displayd_base = displayd_base.rstrip("/")
        self.interval = interval
        self.limit = max(1, min(200, int(limit or 50)))
        self.backfill = max(0, min(50, int(backfill or 0)))
        self._fetch_fn = fetch_fn or self._fetch_http
        self._post_fn = post_fn or self._post_http
        self.seen = set()
        self.seen_order = []
        self.seeded = False  # first poll seeds history, forwards backfill only
        self._state = None  # last logged state; None = nothing logged yet
        self.forwarded = 0
        self.dropped = 0  # refused upstream events

    # -- state-change-only logging --------------------------------------
    def _set_state(self, state, msg, *args):
        if state == self._state:
            LOG.debug(msg, *args)
            return False
        self._state = state
        LOG.info(msg, *args)
        return True

    # -- seen-set --------------------------------------------------------
    def _remember(self, seq):
        if seq is None:
            return
        if seq in self.seen:
            return
        self.seen.add(seq)
        self.seen_order.append(seq)
        while len(self.seen_order) > SEEN_CAP:
            self.seen.discard(self.seen_order.pop(0))

    # -- upstream ----------------------------------------------------------
    def _fetch_http(self, url):
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read(1024 * 1024).decode("utf-8", "replace"))

    def fetch_recent(self):
        """Newest-first raw event list. Raises on any failure."""
        data = self._fetch_fn("%s/live/recent?limit=%d"
                              % (self.bridge_base, self.limit))
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return data["events"]
        if isinstance(data, list):
            return data
        raise ValueError("unexpected /live/recent shape")

    # -- displayd push ------------------------------------------------------
    def _post_http(self, payload):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.displayd_base + "/feed/activity/event", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read(1024)

    def push(self, payload):
        """POST one validated event; True on success, False when down."""
        try:
            self._post_fn(payload)
        except Exception as err:
            self._set_state("displayd-unreachable",
                            "displayd unreachable (%s); dropping events", err)
            return False
        if self._state == "displayd-unreachable":
            self._set_state("streaming",
                            "displayd reachable again; streaming activity")
        elif self._state != "streaming":
            self._set_state("streaming", "streaming beads activity")
        self.forwarded += 1
        return True

    # -- poll -----------------------------------------------------------------
    def poll_once(self):
        """One upstream poll -> forward new events oldest-first.

        Returns (forwarded, outcome) where outcome is "ok", "upstream-down",
        or "stopped". A seq reset (upstream restart) reseeds the seen-set so
        the ring replay is not re-forwarded as new.
        """
        try:
            raw_events = self.fetch_recent()
        except Exception as err:
            self._set_state("upstream-down",
                            "beads-bridge unreachable (%s); retrying", err)
            return 0, "upstream-down"
        if self._state == "upstream-down":
            self._set_state("streaming", "beads-bridge reachable again")
        events = [e for e in (normalize_event(r) for r in raw_events)
                  if e is not None]
        self.dropped += len(raw_events) - len(events)
        if len(raw_events) != len(events):
            LOG.debug("refused %d malformed event(s)",
                      len(raw_events) - len(events))
        by_seq = sorted((e for e in events if e.get("seq") is not None),
                        key=lambda e: e["seq"])
        if by_seq and self.seen and by_seq[-1]["seq"] < max(self.seen):
            # Upstream restarted (seq rewound): the ring is new-process
            # history, so reseed and run the first-poll path (backfill)
            # instead of replaying the whole ring onto the panel.
            LOG.info("upstream seq reset (restart?); reseeding seen-set")
            self.seen.clear()
            self.seen_order = []
            self.seeded = False
        if not self.seeded:
            self.seeded = True
            for e in by_seq:
                self._remember(e.get("seq"))
            fresh = by_seq[-self.backfill:] if self.backfill else []
        else:
            fresh = [e for e in by_seq if e.get("seq") not in self.seen]
        nodeless = [e for e in events if e.get("seq") is None]
        fresh = (fresh + nodeless)[:POLL_CAP]
        # Oldest first, so the panel ring reads in completion order.
        fresh.sort(key=lambda e: (e.get("seq") is None, e.get("seq") or 0))
        delivered = 0
        for event in fresh:
            self._remember(event.get("seq"))
            if self.push(event):
                delivered += 1
        if fresh and delivered != len(fresh):
            self._set_state("displayd-unreachable",
                            "displayd unreachable; dropping events")
        return delivered, "ok"

    def run_forever(self, stop=None):
        backoff = BACKOFF_FIRST
        while True:
            if stop is not None and stop():
                return
            t0 = time.monotonic()
            try:
                _, outcome = self.poll_once()
            except Exception as err:  # never die on a bad tick
                LOG.warning("poll tick failed: %s", err)
                outcome = "failed"
            if outcome == "stopped":
                return
            uptime = time.monotonic() - t0
            if outcome == "ok" and uptime > self.interval:
                pass  # a slow-but-healthy poll paces itself
            elif uptime > HEALTHY_RESET_AFTER:
                backoff = BACKOFF_FIRST  # healthy run; reset the ladder
            elif outcome != "ok":
                backoff = min(backoff * 2, BACKOFF_MAX)
            else:
                backoff = BACKOFF_FIRST
            if outcome != "ok":
                sleep = min(backoff, BACKOFF_MAX) * (0.8 + 0.4 * random.random())
                LOG.info("retrying in %.1fs", sleep)
                deadline = time.monotonic() + sleep
                while time.monotonic() < deadline:
                    if stop is not None and stop():
                        return
                    time.sleep(min(0.2, deadline - time.monotonic()))
            else:
                elapsed = time.monotonic() - t0
                wait = max(0.05, self.interval - elapsed)
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    if stop is not None and stop():
                        return
                    time.sleep(min(0.2, deadline - time.monotonic()))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Beads-bridge live activity -> displayd bridge")
    ap.add_argument("--bridge",
                    default=os.environ.get("BEADS_BRIDGE_URL",
                                           "http://127.0.0.1:3737"),
                    help="beads-bridge base URL (default: %(default)s)")
    ap.add_argument("--displayd",
                    default=os.environ.get("DISPLAYD_BASE",
                                           "http://100.81.88.113:8980"),
                    help="displayd base URL (default: %(default)s)")
    ap.add_argument("--interval", type=float,
                    default=float(os.environ.get("ACTIVITY_INTERVAL", "3.0")),
                    help="poll seconds, 1..120 (default: %(default)s)")
    ap.add_argument("--limit", type=int,
                    default=int(os.environ.get("ACTIVITY_LIMIT", "50")),
                    help="events per poll, 1..200 (default: %(default)s)")
    ap.add_argument("--backfill", type=int,
                    default=int(os.environ.get("ACTIVITY_BACKFILL", "8")),
                    help="newest events to forward on first poll, 0..50 "
                         "(default: %(default)s)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        bridge = ActivityBridge(args.bridge, args.displayd,
                                interval=args.interval, limit=args.limit,
                                backfill=args.backfill)
    except ValueError as err:
        ap.error(str(err))
        return 2
    bridge.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
