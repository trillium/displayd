"""Feed cache: what bridges push into running views.

Single concept: the per-(renderer, input) buffer -- push, retention, and the
cold/warm/stale/error health classification. Pushes never touch the draw path:
a push appends under a short lock, so a switch or a draw never awaits I/O. A
view that is not selected still accumulates inputs, so selecting it later is
instantly populated. No framebuffer needed; unit-testable.
"""

import collections
import threading
import time

from schema import validate_value

class FeedStore:
    """Background feed cache: what bridges push into running views.

    Pushes never touch the draw path -- they append to an in-memory deque
    under a short lock, so a switch or a draw never awaits I/O. A view that
    is not currently selected still accumulates inputs, so selecting it later
    is instantly populated. No framebuffer needed; unit-testable."""

    DEFAULT_BUFFER = 100
    STALE_AFTER = 300.0  # seconds without a push before health reads "stale"

    @staticmethod
    def classify_health(updated_at, error_at=None, now=None):
        """Health label for one feed.

        cold  -- never received a value (and no failed attempt either)
        error -- the most recent update attempt failed
        warm  -- last value arrived within STALE_AFTER seconds
        stale -- last value is older than STALE_AFTER seconds
        """
        if now is None:
            now = time.time()
        if updated_at is None:
            return "error" if error_at is not None else "cold"
        if error_at is not None and error_at >= updated_at:
            return "error"
        return "warm" if (now - updated_at) < FeedStore.STALE_AFTER else "stale"

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}  # (renderer, input) -> {values, updated_at, error, error_at}

    @classmethod
    def _buffer_for(cls, spec):
        """Backlog cap for one input. A spec of {"buffer": 0} (or null)
        means retain everything: the deque is unbounded and entries leave
        state only when the consumer drops them (chat keeps messages until
        a moderation delete; the visible window stays screen-bounded in
        the renderer, not here)."""
        raw = (spec or {}).get("buffer", cls.DEFAULT_BUFFER)
        if raw is None:
            return None
        try:
            size = int(raw)
        except (TypeError, ValueError):
            return cls.DEFAULT_BUFFER
        if size <= 0:
            return None
        return size

    def _blank_entry(self, spec):
        return {
            "values": collections.deque(maxlen=self._buffer_for(spec)),
            "updated_at": None,
            "error": None,
            "error_at": None,
        }

    def declare(self, renderer, input_name, spec):
        """Register an input so it shows up as cold before anything arrives."""
        with self._lock:
            entry = self._data.get((renderer, input_name))
            if entry is None:
                self._data[(renderer, input_name)] = self._blank_entry(spec)

    def push(self, renderer, input_name, payload, spec):
        validate_value(payload, spec or {}, "%s.%s" % (renderer, input_name))
        with self._lock:
            entry = self._data.get((renderer, input_name))
            if entry is None:
                entry = self._blank_entry(spec)
                self._data[(renderer, input_name)] = entry
            entry["values"].append(payload)
            entry["updated_at"] = time.time()
            entry["error"] = None  # a good push clears the last failure
            entry["error_at"] = None
            return {"count": len(entry["values"]), "updated_at": entry["updated_at"]}

    def note_error(self, renderer, input_name, message):
        """Record a failed update attempt (e.g. a schema mismatch) so the
        feed reads "error" until a later push succeeds. Creates the entry
        when the pair was never declared; no-ops on nothing."""
        with self._lock:
            entry = self._data.get((renderer, input_name))
            if entry is None:
                entry = self._blank_entry(None)
                self._data[(renderer, input_name)] = entry
            entry["error"] = str(message)[:160]
            entry["error_at"] = time.time()

    def get(self, renderer, input_name):
        """Buffered payloads, oldest first. Never blocks on I/O."""
        with self._lock:
            entry = self._data.get((renderer, input_name))
            if entry is None:
                return []
            return list(entry["values"])

    def snapshot(self):
        """Per-feed last-known value metadata for /state."""
        now = time.time()
        with self._lock:
            items = list(self._data.items())
        out = {}
        for (renderer, input_name), entry in items:
            updated = entry["updated_at"]
            error_at = entry.get("error_at")
            age = round(now - updated, 1) if updated is not None else None
            out.setdefault(renderer, {})[input_name] = {
                "count": len(entry["values"]),
                "updated_at": updated,
                "age_seconds": age,
                "health": self.classify_health(updated, error_at, now),
                "last_error": entry.get("error"),
            }
        return out

    def status(self, renderer, input_name):
        """One feed's health plus its latest value. Raises KeyError when
        the (renderer, input) pair was never declared."""
        with self._lock:
            entry = self._data.get((renderer, input_name))
        if entry is None:
            raise KeyError("unknown input: %s.%s" % (renderer, input_name))
        updated = entry["updated_at"]
        error_at = entry.get("error_at")
        now = time.time()
        age = round(now - updated, 1) if updated is not None else None
        values = list(entry["values"])
        return {
            "renderer": renderer,
            "input": input_name,
            "count": len(values),
            "updated_at": updated,
            "age_seconds": age,
            "health": self.classify_health(updated, error_at, now),
            "last_error": entry.get("error"),
            "latest": values[-1] if values else None,
        }
