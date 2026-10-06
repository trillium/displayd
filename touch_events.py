"""evdev wire format: constants, the 24-byte input_event record, and the
parser that turns raw records into normalized touch events.

Single concept: decoding the touchscreen device. Owns the evdev constants
we read, ``EVENT_FORMAT``/``EVENT_SIZE``, ``READ_POLL_SECONDS`` (the bounded
wait before the reader re-checks its stop flag), ``pack_event`` /
``parse_event`` (also the test fixture surface), ``TouchEvent`` and
``EvdevParser``. Nothing here knows about config, regions, or HTTP.
"""

import struct


# ---------------------------------------------------------------------------
# evdev constants (from linux/input-event-codes.h; only what we decode)
# ---------------------------------------------------------------------------

EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03

SYN_REPORT = 0

BTN_TOUCH = 330

ABS_X = 0x00
ABS_Y = 0x01
ABS_MT_SLOT = 0x2F          # 47
ABS_MT_TOUCH_MAJOR = 0x30   # 48 (ignored, kept for clarity)
ABS_MT_POSITION_X = 0x35    # 53
ABS_MT_POSITION_Y = 0x36    # 54
ABS_MT_TRACKING_ID = 0x39   # 57

# struct input_event on the supported target, 64-bit Linux (x86_64):
# timeval (2x 8-byte long: tv_sec, tv_usec) + __u16 type + __u16 code +
# __s32 value = 24 bytes total. value is SIGNED (e.g. ABS_MT_TRACKING_ID
# -1 means "contact lifted").
#
# Explicitly little-endian ("<qqHHi") rather than native ("@llHHi") so
# the size is pinned at 24 bytes wherever this code runs; a native long
# would silently shrink to 4 bytes on a 32-bit build and reintroduce the
# EINVAL below.
#
# 32-bit kernels emit 16-byte records (timeval with 4-byte longs,
# format "<llHHi"). That layout is NOT supported by this build: the
# reader requests full 24-byte records and the kernel rejects a 16-byte
# read() on a 64-bit device with EINVAL, and vice versa a 24-byte read
# on a 16-byte-record device would misframe. Do not "guess" between
# the two at runtime -- if 32-bit support is ever needed, it must be an
# explicit, tested target, not a silent fallback.
EVENT_FORMAT = "<qqHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
assert EVENT_SIZE == 24, "64-bit input_event must be 24 bytes"

# Upper bound on stop latency while the device is idle: the evdev read
# loop waits in select() for at most this long before re-checking the
# stop flag, so SIGTERM/SIGINT is honored within ~a second even when no
# touch event ever arrives. (The hang this prevents: a bare blocking
# read() never returns while idle, so request_stop() was never observed
# and the unit wedged in `deactivating` until systemd timed out the stop.)
READ_POLL_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Low-level event packing / parsing (also the test fixture surface)
# ---------------------------------------------------------------------------

def pack_event(ev_type, code, value, sec=0, usec=0):
    """Pack one input_event record. Used by the live reader's inverse and
    by tests to synthesize device streams."""
    return struct.pack(EVENT_FORMAT, sec, usec, ev_type, code, value)


def parse_event(record):
    """Parse one EVENT_SIZE record -> (type, code, value). Raises ValueError
    on short/corrupt records."""
    if len(record) != EVENT_SIZE:
        raise ValueError("short input_event: %d bytes" % len(record))
    _sec, _usec, ev_type, code, value = struct.unpack(EVENT_FORMAT, record)
    return ev_type, code, value


# ---------------------------------------------------------------------------
# Touch lifecycle tracking
# ---------------------------------------------------------------------------

class TouchEvent:
    """One normalized lifecycle event emitted on SYN_REPORT."""

    DOWN = "down"
    MOVE = "move"
    UP = "up"

    def __init__(self, kind, slot, x, y, tracking_id=-1):
        self.kind = kind
        self.slot = slot
        self.x = x          # raw device units (normalization is separate)
        self.y = y
        self.tracking_id = tracking_id

    def __repr__(self):
        return ("TouchEvent(%s slot=%d x=%r y=%r tid=%r)"
                % (self.kind, self.slot, self.x, self.y, self.tracking_id))


class EvdevParser:
    """Decode evdev pointer + multitouch streams into TouchEvents.

    Handles both protocols seen on real panels:
      * single-touch: ABS_X/ABS_Y position with BTN_TOUCH down/up, and
      * multitouch: ABS_MT_SLOT + ABS_MT_TRACKING_ID lifecycle with
        ABS_MT_POSITION_X/Y (falling back to ABS_X/Y when a device
        reports position only through the single-touch axes).

    Call feed() per (type, code, value) triple; each SYN_REPORT flushes
    and returns the list of TouchEvents for that frame (possibly empty).
    """

    def __init__(self):
        self._slot = 0
        # slot -> {tracking_id, x, y, down}
        self._slots = {}
        # single-touch protocol state
        self._st_x = None
        self._st_y = None
        self._st_down = False
        self._pending_mt = {}   # slot -> dict of staged changes
        self._pending_st = {}   # staged single-touch changes

    def _contact(self, slot):
        return self._slots.setdefault(
            slot, {"tracking_id": -1, "x": None, "y": None, "down": False})

    def feed(self, ev_type, code, value):
        if ev_type == EV_ABS and code == ABS_MT_SLOT:
            self._slot = value
            self._pending_mt.setdefault(value, {})
        elif ev_type == EV_ABS and code == ABS_MT_TRACKING_ID:
            staged = self._pending_mt.setdefault(self._slot, {})
            staged["tracking_id"] = value
        elif ev_type == EV_ABS and code in (ABS_MT_POSITION_X, ABS_X):
            if code == ABS_MT_POSITION_X:
                self._pending_mt.setdefault(self._slot, {})["x"] = value
            else:
                self._pending_st["x"] = value
        elif ev_type == EV_ABS and code in (ABS_MT_POSITION_Y, ABS_Y):
            if code == ABS_MT_POSITION_Y:
                self._pending_mt.setdefault(self._slot, {})["y"] = value
            else:
                self._pending_st["y"] = value
        elif ev_type == EV_KEY and code == BTN_TOUCH:
            self._pending_st["down"] = bool(value)
        elif ev_type == EV_SYN and code == SYN_REPORT:
            return self._flush()
        return []

    def _flush(self):
        out = []
        # Multitouch slots first (authoritative when present).
        for slot, staged in sorted(self._pending_mt.items()):
            if not staged:
                continue
            contact = self._contact(slot)
            if "tracking_id" in staged:
                tid = staged["tracking_id"]
                if tid >= 0 and not contact["down"]:
                    contact["down"] = True
                    contact["tracking_id"] = tid
                    if "x" in staged:
                        contact["x"] = staged["x"]
                    if "y" in staged:
                        contact["y"] = staged["y"]
                    out.append(TouchEvent(TouchEvent.DOWN, slot,
                                          contact["x"], contact["y"], tid))
                    continue
                elif tid < 0 and contact["down"]:
                    contact["down"] = False
                    contact["tracking_id"] = -1
                    out.append(TouchEvent(TouchEvent.UP, slot,
                                          contact["x"], contact["y"],
                                          contact["tracking_id"]))
                    continue
            if contact["down"] and ("x" in staged or "y" in staged):
                if "x" in staged:
                    contact["x"] = staged["x"]
                if "y" in staged:
                    contact["y"] = staged["y"]
                out.append(TouchEvent(TouchEvent.MOVE, slot,
                                      contact["x"], contact["y"],
                                      contact["tracking_id"]))
        self._pending_mt = {}
        # Single-touch protocol (slot 0 mirror). Skipped when this frame
        # already produced MT events, so hybrid panels don't double-report.
        if not out and self._pending_st:
            staged = self._pending_st
            if "x" in staged:
                self._st_x = staged["x"]
            if "y" in staged:
                self._st_y = staged["y"]
            if "down" in staged:
                if staged["down"] and not self._st_down:
                    self._st_down = True
                    out.append(TouchEvent(TouchEvent.DOWN, 0,
                                          self._st_x, self._st_y))
                elif not staged["down"] and self._st_down:
                    self._st_down = False
                    out.append(TouchEvent(TouchEvent.UP, 0,
                                          self._st_x, self._st_y))
            elif self._st_down and ("x" in staged or "y" in staged):
                out.append(TouchEvent(TouchEvent.MOVE, 0,
                                      self._st_x, self._st_y))
        self._pending_st = {}
        return out

    def feed_frame(self, triples):
        """Convenience: feed a list of (type, code, value) ending in an
        implicit SYN_REPORT; returns the flushed events."""
        out = []
        for ev_type, code, value in triples:
            out.extend(self.feed(ev_type, code, value))
        out.extend(self.feed(EV_SYN, SYN_REPORT, 0))
        return out
