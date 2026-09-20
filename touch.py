"""Touch input for displayd: evdev touchscreen -> displayd HTTP actions.

Standalone, stdlib-only companion to the output-only ``displayd`` daemon.
It reads a Linux input device (``/dev/input/event*``), decodes evdev
pointer/multitouch events, normalizes them into display coordinates, hit-tests
them against a small configured region list, and invokes existing safe
displayd HTTP actions for taps.

Nothing here touches the renderer core or the framebuffer path: the only
coupling to displayd is its public HTTP API (the same endpoints a remote
operator would call). The trust boundary is therefore the displayd HTTP API
itself, which is unauthenticated by design -- see "Transport and trust
boundary" in TOUCH.md.

Run foreground (smoke test)::

    python3 touch.py --list-devices
    python3 touch.py --device /dev/input/event8 --width 1920 --height 1080 --dry-run
    python3 touch.py --config touch.json

See TOUCH.md for device discovery, permissions, calibration, supervision,
and rollback. See tests/test_touch.py for the deterministic fixture suite
(no touchscreen hardware required).
"""

import argparse
import glob
import json
import logging
import os
import signal
import struct
import sys
import time
import urllib.request
import urllib.error

LOG = logging.getLogger("displayd-touch")

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

# Documented local-machine default: on lnx-server the attached panel was
# previously identified as `G2Touch Multi-Touch`. This is NOT universal --
# always confirm with --list-devices on the target host.
DEFAULT_DEVICE = "/dev/input/event8"
DEFAULT_ENDPOINT = "http://127.0.0.1:8980"

# Actions this module may ever invoke. Anything not listed here is rejected
# by dispatch_action(); there is deliberately no "run arbitrary POST" entry,
# so a compromised/mis-edited config cannot become command execution.
ALLOWED_ACTIONS = (
    "playlist_next",
    "playlist_pause",
    "playlist_resume",
    "screen_on",
    "screen_off",
    "clear",
    "show",
    "notify",
)


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


# ---------------------------------------------------------------------------
# Coordinate normalization (raw device units -> display pixels)
# ---------------------------------------------------------------------------

def normalize(raw_x, raw_y, width, height, calibration=None):
    """Map raw touch units into display pixels.

    calibration keys (all optional): x_min, x_max, y_min, y_max (raw range
    reported by the device -- from `evtest` or sysfs `input/abs*` bounds),
    swap_xy, invert_x, invert_y, rotation (one of 0/90/180/270, clockwise
    degrees applied after swap/invert). Returns (x_px, y_px) clamped to
    [0, width-1] x [0, height-1]; None input on either axis yields None
    on that axis (contact without position yet).
    """
    cal = calibration or {}
    x_min = cal.get("x_min", 0)
    x_max = cal.get("x_max")
    y_min = cal.get("y_min", 0)
    y_max = cal.get("y_max")
    if x_max is None or y_max is None:
        raise ValueError("calibration needs x_max/y_max (device raw range)")
    if x_max <= x_min or y_max <= y_min:
        raise ValueError("calibration range must be non-empty")

    def scale(raw, lo, hi, size):
        if raw is None:
            return None
        frac = (raw - lo) / float(hi - lo)
        frac = max(0.0, min(1.0, frac))
        return int(round(frac * (size - 1)))

    x = scale(raw_x, x_min, x_max, width)
    y = scale(raw_y, y_min, y_max, height)
    if cal.get("swap_xy"):
        x, y = y, x
        width, height = height, width
    if x is not None and cal.get("invert_x"):
        x = (width - 1) - x
    if y is not None and cal.get("invert_y"):
        y = (height - 1) - y
    rotation = cal.get("rotation", 0)
    if rotation not in (0, 90, 180, 270):
        raise ValueError("rotation must be one of 0/90/180/270")
    if rotation and x is not None and y is not None:
        if rotation == 90:
            x, y = (width - 1) - y, x
            width, height = height, width
        elif rotation == 180:
            x, y = (width - 1) - x, (height - 1) - y
        elif rotation == 270:
            x, y = y, (height - 1) - x
            width, height = height, width
    return x, y


# ---------------------------------------------------------------------------
# Hit regions + safe action dispatch
# ---------------------------------------------------------------------------

def hit_test(x, y, regions):
    """Return the id of the first region containing (x, y), else None.

    A region is {"id": str, "rect": [x, y, w, h]} in display pixels.
    Regions are evaluated in config order, so earlier entries win overlaps.
    """
    if x is None or y is None:
        return None
    for region in regions:
        rid = region.get("id")
        rect = region.get("rect")
        if not rid or not rect or len(rect) != 4:
            continue
        rx, ry, rw, rh = rect
        if rx <= x < rx + rw and ry <= y < ry + rh:
            return rid
    return None


def action_request(action):
    """Translate a validated action dict into (method, path, body).

    Raises ValueError for unknown action names (closed allowlist) or
    malformed payloads. Returned bodies are fixed-shape; free-form renderer
    params are allowed only for the `show` target renderer named in config.
    """
    name = action.get("name")
    if name not in ALLOWED_ACTIONS:
        raise ValueError("refusing unknown action: %r" % (name,))
    if name == "playlist_next":
        return "POST", "/playlist/next", {}
    if name == "playlist_pause":
        return "POST", "/playlist/pause", {}
    if name == "playlist_resume":
        return "POST", "/playlist/resume", {}
    if name == "screen_on":
        return "POST", "/screen/on", {}
    if name == "screen_off":
        return "POST", "/screen/off", {}
    if name == "clear":
        return "POST", "/clear", {}
    if name == "show":
        renderer = action.get("renderer")
        if not renderer or not isinstance(renderer, str):
            raise ValueError("show action needs a renderer name")
        params = action.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("show params must be an object")
        return "POST", "/show", {"renderer": renderer, "params": params}
    if name == "notify":
        title = action.get("title")
        if not title or not isinstance(title, str):
            raise ValueError("notify action needs a title")
        body = {"title": title}
        for key in ("body", "severity", "duration", "color"):
            if key in action:
                body[key] = action[key]
        return "POST", "/notify", body
    raise ValueError("refusing unknown action: %r" % (name,))  # pragma: no cover


class DisplaydClient:
    """Minimal stdlib HTTP client for the displayd API."""

    def __init__(self, base_url=DEFAULT_ENDPOINT, timeout=5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def post(self, path, body):
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(payload or "{}")
                except ValueError:
                    return resp.status, {"raw": payload}
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            raise RuntimeError("displayd %s -> HTTP %s: %s"
                               % (path, exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("displayd %s unreachable at %s: %s"
                               % (path, self.base_url, exc.reason)) from exc

    def dispatch(self, action, dry_run=False):
        """Validate + (unless dry_run) POST an action dict. Returns a
        summary dict describing what was (or would be) done."""
        method, path, body = action_request(action)
        summary = {"action": action.get("name"), "method": method,
                   "path": path, "body": body, "dry_run": bool(dry_run)}
        if dry_run:
            return summary
        status, resp = self.post(path, body)
        summary["status"] = status
        summary["response"] = resp
        return summary


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def default_config():
    """Conservative shipped default: two wide tap zones on a 1080p panel.

    Right third advances the playlist, left third wakes the screen. The
    middle does nothing (avoids accidental taps while reading). Tune
    width/height/calibration/regions per panel in touch.json."""
    return {
        "device": DEFAULT_DEVICE,
        "endpoint": DEFAULT_ENDPOINT,
        "width": 1920,
        "height": 1080,
        "calibration": {"x_min": 0, "x_max": 4095,
                        "y_min": 0, "y_max": 4095,
                        "swap_xy": False, "invert_x": False,
                        "invert_y": False, "rotation": 0},
        "tap_max_seconds": 0.5,
        "tap_max_pixels": 40,
        "debounce_seconds": 0.3,
        "regions": [
            {"id": "playlist-next",
             "rect": [1280, 0, 640, 1080],
             "action": {"name": "playlist_next"}},
            {"id": "screen-on",
             "rect": [0, 0, 640, 1080],
             "action": {"name": "screen_on"}},
        ],
    }


def load_config(path=None):
    """Load JSON config file over the defaults. Env overrides (highest
    precedence): DISPLAYD_TOUCH_DEVICE, DISPLAYD_URL/DISPLAYD_ENDPOINT,
    DISPLAYD_TOUCH_WIDTH/HEIGHT. Validates regions/actions eagerly so a
    bad config fails before the device is opened."""
    cfg = default_config()
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            overlay = json.load(fh)
        if not isinstance(overlay, dict):
            raise ValueError("touch config must be a JSON object")
        for key, value in overlay.items():
            if key == "calibration" and isinstance(value, dict):
                cfg["calibration"].update(value)
            else:
                cfg[key] = value
    env = os.environ
    if env.get("DISPLAYD_TOUCH_DEVICE"):
        cfg["device"] = env["DISPLAYD_TOUCH_DEVICE"]
    if env.get("DISPLAYD_URL"):
        cfg["endpoint"] = env["DISPLAYD_URL"]
    if env.get("DISPLAYD_ENDPOINT"):
        cfg["endpoint"] = env["DISPLAYD_ENDPOINT"]
    for key, var in (("width", "DISPLAYD_TOUCH_WIDTH"),
                     ("height", "DISPLAYD_TOUCH_HEIGHT")):
        if env.get(var):
            cfg[key] = int(env[var])
    if cfg["width"] <= 0 or cfg["height"] <= 0:
        raise ValueError("width/height must be positive")
    regions = cfg.get("regions") or []
    seen = set()
    for region in regions:
        rid = region.get("id")
        if not rid or rid in seen:
            raise ValueError("regions need unique ids")
        seen.add(rid)
        rect = region.get("rect")
        if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                or any(not isinstance(v, (int, float)) for v in rect)):
            raise ValueError("region %r needs rect [x, y, w, h]" % (rid,))
        action_request(region.get("action") or {})  # fail fast on bad actions
    return cfg


def list_input_devices():
    """Enumerate /dev/input/event* with sysfs names. Best-effort: missing
    sysfs entries yield name=None rather than failing."""
    found = []
    for path in sorted(glob.glob("/dev/input/event*")):
        name = None
        try:
            num = "".join(c for c in os.path.basename(path)
                          if c.isdigit())
            candidates = glob.glob(
                "/sys/class/input/event%s/device/name" % num)
            if candidates:
                with open(candidates[0], "r", encoding="utf-8",
                          errors="replace") as fh:
                    name = fh.read().strip()
        except OSError:
            pass
        found.append({"path": path, "name": name})
    return found


# ---------------------------------------------------------------------------
# Tap detection + service loop
# ---------------------------------------------------------------------------

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


class TouchService:
    """Foreground reader: device -> parser -> normalize -> tap -> action."""

    def __init__(self, config, client=None, clock=None):
        self.config = config
        self.client = client or DisplaydClient(config["endpoint"])
        self.parser = EvdevParser()
        self.taps = TapDetector(
            tap_max_seconds=config.get("tap_max_seconds", 0.5),
            tap_max_pixels=config.get("tap_max_pixels", 40),
            debounce_seconds=config.get("debounce_seconds", 0.3),
            clock=clock)
        self._stop = False

    def request_stop(self, *_args):
        LOG.info("touch service stopping")
        self._stop = True

    def handle_frame(self, touch_events, dry_run=False):
        """Process one SYN_REPORT frame's TouchEvents. Returns the dispatch
        summary for a tap that hit a region, else None."""
        now = time.monotonic()
        for event in touch_events:
            event.x, event.y = normalize(
                event.x, event.y, self.config["width"],
                self.config["height"], self.config.get("calibration"))
            tap = self.taps.feed(event, timestamp=now)
            if tap is None:
                continue
            region_id = hit_test(tap[0], tap[1],
                                 self.config.get("regions") or [])
            if region_id is None:
                LOG.info("tap at %d,%d hit no region", tap[0], tap[1])
                continue
            region = next(r for r in self.config["regions"]
                          if r["id"] == region_id)
            LOG.info("tap at %d,%d -> region %r -> action %r",
                     tap[0], tap[1], region_id,
                     region["action"].get("name"))
            try:
                return self.client.dispatch(region["action"],
                                            dry_run=dry_run)
            except Exception as exc:  # keep serving touches on HTTP failure
                LOG.warning("action %r failed: %s",
                            region["action"].get("name"), exc)
                return {"action": region["action"].get("name"),
                        "error": str(exc)}
        return None

    def iter_device_events(self, stream):
        """Yield (type, code, value) triples from a raw device stream,
        tolerating short reads at EOF.

        Each read() requests exactly the remainder of one 24-byte
        input_event record. The kernel validates the count against its
        native record size, so requesting anything other than a full
        record (e.g. the old 16-byte size) fails with EINVAL -- hence
        the pinned EVENT_SIZE above."""
        buf = b""
        while not self._stop:
            chunk = stream.read(EVENT_SIZE - len(buf))
            if not chunk:
                if buf:
                    raise ValueError("truncated input_event at EOF")
                return
            buf += chunk
            if len(buf) < EVENT_SIZE:
                continue
            yield parse_event(buf)
            buf = b""

    def run(self, dry_run=False):
        device = self.config["device"]
        LOG.info("touch service starting: device=%s endpoint=%s "
                 "display=%dx%d%s", device, self.config["endpoint"],
                 self.config["width"], self.config["height"],
                 " (dry-run, no HTTP)" if dry_run else "")
        try:
            stream = open(device, "rb", buffering=0)
        except FileNotFoundError:
            LOG.error("device %s not found; run --list-devices", device)
            raise SystemExit(2)
        except PermissionError:
            LOG.error("permission denied on %s; see TOUCH.md "
                      "(input group / udev rule)", device)
            raise SystemExit(2)
        with stream:
            for ev_type, code, value in self.iter_device_events(stream):
                for event in self.parser.feed(ev_type, code, value):
                    LOG.debug("touch %s", event)
                    self.handle_frame([event], dry_run=dry_run)
                if self._stop:
                    break
        LOG.info("touch service stopped")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="displayd touch input: evdev touchscreen -> "
                    "displayd HTTP actions")
    parser.add_argument("--config", default=None,
                        help="JSON config file (default: built-ins + env)")
    parser.add_argument("--device", default=None,
                        help="input device (default: %s; always confirm "
                             "with --list-devices)" % DEFAULT_DEVICE)
    parser.add_argument("--endpoint", default=None,
                        help="displayd base URL "
                             "(default: http://127.0.0.1:8980)")
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="decode + hit-test, log actions, send no HTTP")
    parser.add_argument("--list-devices", action="store_true",
                        help="list /dev/input/event* with names and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.list_devices:
        for dev in list_input_devices():
            print("%s\t%s" % (dev["path"], dev["name"] or "(unknown)"))
        return 0
    cfg = load_config(args.config)
    if args.device:
        cfg["device"] = args.device
    if args.endpoint:
        cfg["endpoint"] = args.endpoint
    if args.width:
        cfg["width"] = args.width
    if args.height:
        cfg["height"] = args.height
    service = TouchService(cfg)
    signal.signal(signal.SIGINT,
                  lambda *_a: service.request_stop())
    signal.signal(signal.SIGTERM,
                  lambda *_a: service.request_stop())
    service.run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
