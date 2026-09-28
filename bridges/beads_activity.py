#!/usr/bin/env python3
"""Beads-bridge activity -> displayd bridge (polling).

Single concept: poll the beads-bridge ``/live/recent`` ring and push each
new event across the tailnet into displayd's feed API::

    beads-bridge http://<bridge-host>:3737 --HTTP--> bridge --HTTP--> displayd

Event validation lives in ``beads_activity_events`` (pure functions, test
hooks); the process entry point lives in ``beads_activity_cli`` (this file
stays directly executable -- see its ``__main__`` guard).

Safety model (inherited from the sidecar -- do not weaken): observational
only (two GETs polled, feed payloads POSTed, never a bead mutation);
bounded payloads and memory; nothing here triggers MCP tool calls, so the
ring cannot re-trigger itself; a dead upstream or displayd is a reconnect
with backoff, never a process death.

Stdlib only, no credentials anywhere (neither endpoint needs any). Exits
non-zero only on configuration errors; a dropped upstream connection is a
reconnect, never a death.
"""

import json
import logging
import random
import sys
import time
import urllib.error
import urllib.request

from beads_activity_events import (
    ARG_NAME_LEN,  # re-exported for backwards compatibility (see below)
    MAX_ARG_NAMES,
    MAX_BEADREFS,
    SUMMARY_CHARS,
    normalize_event,
)

LOG = logging.getLogger("beads-activity-bridge")

# ---- poll bounds (mirror the sidecar's server-side caps) ------------------------

SEEN_CAP = 1000
POLL_CAP = 50          # max events forwarded from a single poll
BACKOFF_FIRST = 1.0
BACKOFF_MAX = 30.0
HEALTHY_RESET_AFTER = 30.0  # a poll run living this long resets the ladder

# Validation bounds (SUMMARY_CHARS, MAX_BEADREFS, MAX_ARG_NAMES,
# ARG_NAME_LEN) and normalize_event are imported from
# beads_activity_events above and re-exported here so existing
# ``import beads_activity`` users keep working.


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


if __name__ == "__main__":
    from beads_activity_cli import main
    sys.exit(main())
