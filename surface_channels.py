"""Per-surface content slots and touch-event log, with a bounded long-poll.

Single concept: the two things a surface *exchanges* after registering.
Content flows to it (``put_content`` per region, read by a Sidecar with
``read_content(since=...)``); touch events flow back (``add_event``, read by
consumers with ``read_events(since=...)``). Each stream has a per-surface
monotonic ``seq`` so a reader asks "anything after N?" and never double-acts.
``wait`` holds a read until something newer lands or the bound passes
(``MAX_WAIT``), so a Sidecar wakes on the send instead of its next poll.
"""

import collections
import threading
import time

MAX_WAIT = 25.0
EVENT_LOG = 128


class Waiter:
    """Bounded long-poll over ``self._cond`` (a ``threading.Condition``)."""

    @staticmethod
    def _bound(wait):
        try:
            return max(0.0, min(float(wait or 0), MAX_WAIT))
        except (TypeError, ValueError):
            return 0.0

    def _wait_until(self, ready, wait):
        """Hold (condition lock held) until ready() or the bound passes."""
        end = time.monotonic() + self._bound(wait)
        result = ready()
        while not result:
            left = end - time.monotonic()
            if left <= 0:
                break
            self._cond.wait(left)
            result = ready()
        return result


class Channels(Waiter):
    """Content slots + event ring per surface id. Thread-safe."""

    def __init__(self, clock=time.time, seq_base=0):
        self._clock = clock
        self._base = seq_base  # restart generation: a seq never regresses
        self._cond = threading.Condition()
        self._content = {}  # id -> {"seq": n, "slots": {region: slot}}
        self._events = {}   # id -> {"seq": n, "ring": deque}

    # ---- content -------------------------------------------------------

    def put_content(self, sid, region, content):
        with self._cond:
            stream = self._content.setdefault(sid, {"seq": self._base, "slots": {}})
            stream["seq"] += 1
            stream["slots"][region] = {"seq": stream["seq"], "content": content,
                                       "at": self._clock()}
            self._cond.notify_all()
            return stream["seq"]

    def read_content(self, sid, since=0, wait=0, region=None):
        """``{"seq", "contents": {region: slot}}`` for slots newer than
        ``since``; ``contents`` is empty when the hold expired quietly."""
        def newer():
            stream = self._content.get(sid)
            if not stream:
                return {}
            return {name: dict(slot) for name, slot in stream["slots"].items()
                    if slot["seq"] > since and region in (None, name)}

        with self._cond:
            found = self._wait_until(newer, wait)
            stream = self._content.get(sid) or {"seq": 0}
            return {"surface": sid, "seq": stream["seq"], "contents": found}

    # ---- touch events --------------------------------------------------

    def add_event(self, sid, event):
        with self._cond:
            stream = self._events.setdefault(
                sid, {"seq": self._base,
                      "ring": collections.deque(maxlen=EVENT_LOG)})
            stream["seq"] += 1
            event = dict(event, seq=stream["seq"], at=self._clock())
            stream["ring"].append(event)
            self._cond.notify_all()
            return event

    def read_events(self, sid, since=0, wait=0):
        def newer():
            stream = self._events.get(sid)
            if not stream:
                return []
            return [dict(e) for e in stream["ring"] if e["seq"] > since]

        with self._cond:
            found = self._wait_until(newer, wait)
            stream = self._events.get(sid) or {"seq": 0}
            return {"surface": sid, "seq": stream["seq"], "events": found}

    # ---- lifecycle -----------------------------------------------------

    def drop(self, sid):
        with self._cond:
            self._content.pop(sid, None)
            self._events.pop(sid, None)
            self._cond.notify_all()

    def keep_regions(self, sid, names):
        """Forget content for regions a re-registration no longer declares."""
        with self._cond:
            stream = self._content.get(sid)
            if stream:
                for gone in [r for r in stream["slots"] if r not in names]:
                    del stream["slots"][gone]
