"""Tap detection: folding raw TouchEvents into taps.

Single concept: a down followed by an up on the same slot, inside the
configured time and pixel budgets, is a tap; anything else (swipe, long
press, bounce, multitouch echo) is not. Emits display-pixel tap positions
for hit testing.
"""

import time

from touch_events import TouchEvent


class TapDetector:
    """Fold raw TouchEvents into taps: down followed by up on the same slot
    within tap_max_seconds and tap_max_pixels (display-pixel distance).
    Moves beyond the pixel budget cancel the candidate (it was a swipe).
    Emits (x, y) display-pixel tap positions for hit testing."""

    def __init__(self, tap_max_seconds=0.5, tap_max_pixels=40,
                 debounce_seconds=0.3, clock=None):
        self.tap_max_seconds = tap_max_seconds
        self.tap_max_pixels = tap_max_pixels
        self.debounce_seconds = debounce_seconds
        self._clock = clock or time.monotonic
        self._pending = {}   # slot -> (x, y, timestamp)
        self._last_tap_at = None

    def feed(self, event, timestamp=None):
        """Feed one TouchEvent with display-pixel x/y. Returns a (x, y)
        tap or None."""
        now = timestamp if timestamp is not None else self._clock()
        if event.kind == TouchEvent.DOWN:
            if event.x is not None and event.y is not None:
                self._pending[event.slot] = (event.x, event.y, now)
            return None
        if event.kind == TouchEvent.MOVE:
            start = self._pending.get(event.slot)
            if (start and event.x is not None and event.y is not None
                    and max(abs(event.x - start[0]),
                            abs(event.y - start[1])) > self.tap_max_pixels):
                self._pending.pop(event.slot, None)  # became a swipe
            return None
        if event.kind == TouchEvent.UP:
            start = self._pending.pop(event.slot, None)
            if not start:
                return None
            sx, sy, t0 = start
            if now - t0 > self.tap_max_seconds:
                return None  # long press: ignore
            if (event.x is not None and event.y is not None
                    and max(abs(event.x - sx),
                            abs(event.y - sy)) > self.tap_max_pixels):
                return None  # released far away: swipe
            if (self._last_tap_at is not None
                    and now - self._last_tap_at < self.debounce_seconds):
                return None  # bounce / multitouch echo
            self._last_tap_at = now
            return (sx, sy)
        return None
