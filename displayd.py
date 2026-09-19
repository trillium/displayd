#!/usr/bin/env python3
"""displayd - a tiny content-agnostic display daemon.

Owns the physical screen of a headless Linux box and exposes it through a
small JSON API.  All content comes from renderer plugins dropped into
renderers/ -- the core knows nothing about any particular one.

API
  GET  /                             web control page (this panel)
  GET  /health                       liveness
  GET  /state                        what is showing + screen power
  GET  /renderers                    available renderers and their params
  GET  /snapshot                     PNG of the last presented frame
  POST /show    {"renderer":"name","params":{...}}
  POST /feed/<renderer>/<input>  push a validated payload into a view
  POST /notify  {"title":...,"body"?,"severity"?,"duration"?} transient notice
  GET  /policy  autonomous-behaviour config + activity clock
  POST /policy  {"idle":{...},"chat_attention":{...},"notifications":{...}}
  POST /clear                        blank the screen to black
  POST /screen  {"power":"on"|"off"} also /screen/on and /screen/off

The API has no authentication, so it listens on 127.0.0.1 by default.  Bind
wider only deliberately -- see --bind / --port (or DISPLAYD_BIND / DISPLAYD_PORT).
"""

import argparse
import collections
import importlib.util
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from PIL import Image

import policy as policy_module
import feedback as feedback_module

FB = "/dev/fb0"
FB_SYS = "/sys/class/graphics/fb0/"
BACKLIGHT_GLOB = "/sys/class/backlight"
# The API is unauthenticated, so the default is loopback only: reaching it from
# another machine is a deliberate choice (--bind or DISPLAYD_BIND), and should be
# paired with a host firewall or a private network such as a VPN or tailnet.
PORT = int(os.environ.get("DISPLAYD_PORT", "8980"))
BIND = os.environ.get("DISPLAYD_BIND", "127.0.0.1")
RENDERER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "renderers")
VT = os.environ.get("DISPLAYD_VT", "/dev/tty1")
POLICY_FILE = os.environ.get(
    "DISPLAYD_POLICY",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.json"),
)

KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01


def _read(path, default=None):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


class Framebuffer:
    """The physical screen: pixels, blanking, and backlight."""

    def __init__(self):
        self.width = int(_read(FB_SYS + "virtual_size", "1920,1080").split(",")[0])
        self.height = int(_read(FB_SYS + "virtual_size", "1920,1080").split(",")[1])
        self.bpp = int(_read(FB_SYS + "bits_per_pixel", "32"))
        self.stride = int(_read(FB_SYS + "stride", str(self.width * 4)))
        self.fd = os.open(FB, os.O_RDWR)
        self.last_frame = None
        self.blanked = False
        self.saved_brightness = None
        self.backlight = self._find_backlight()
        self.vt_fd = None

    # ---- pixels -------------------------------------------------------

    def present(self, img):
        """Push a PIL RGB image to the screen."""
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height))
        data = img.convert("RGB").tobytes("raw", "BGRX")
        self.raw(data)

    def raw(self, data):
        os.pwrite(self.fd, data, 0)
        self.last_frame = data

    def repaint(self):
        if self.last_frame is not None:
            os.pwrite(self.fd, self.last_frame, 0)

    def black(self):
        self.raw(b"\x00" * (self.stride * self.height))

    # ---- console handover ---------------------------------------------

    def take_console(self):
        """Tell the kernel console to stop painting over us."""
        try:
            import fcntl

            self.vt_fd = os.open(VT, os.O_RDWR)
            fcntl.ioctl(self.vt_fd, KDSETMODE, KD_GRAPHICS)
            return True
        except Exception:
            self.vt_fd = None
            return False

    def release_console(self):
        if self.vt_fd is None:
            return
        try:
            import fcntl

            fcntl.ioctl(self.vt_fd, KDSETMODE, KD_TEXT)
        except Exception:
            pass
        finally:
            try:
                os.close(self.vt_fd)
            except OSError:
                pass
            self.vt_fd = None

    # ---- screen power --------------------------------------------------

    def _find_backlight(self):
        try:
            names = sorted(os.listdir(BACKLIGHT_GLOB))
        except OSError:
            return None
        for name in names:
            base = os.path.join(BACKLIGHT_GLOB, name)
            if os.path.exists(os.path.join(base, "brightness")):
                return base
        return None

    def _read_int(self, path):
        val = _read(path)
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    def backlight_state(self):
        if not self.backlight:
            return {"available": False}
        return {
            "available": True,
            "path": self.backlight,
            "value": self._read_int(os.path.join(self.backlight, "brightness")),
            "max": self._read_int(os.path.join(self.backlight, "max_brightness")),
            "saved": self.saved_brightness,
        }

    def set_brightness(self, value):
        """Write brightness, then confirm the driver actually took it."""
        if not self.backlight:
            return False
        maxv = self._read_int(os.path.join(self.backlight, "max_brightness")) or 100
        value = max(0, min(int(value), maxv))
        path = os.path.join(self.backlight, "brightness")
        for _ in range(3):
            try:
                with open(path, "w") as fh:
                    fh.write(str(value))
            except OSError:
                continue
            if self._read_int(path) == value:
                return True
            time.sleep(0.15)
        return False

    # A dimmed or near-zero reading is never a sane restore target: only
    # preserve a value bright enough to actually read (task-i0agw -- the
    # daemon used to capture the dimmed value, leaving the panel near-black
    # after an on/off cycle). Floor is 10% of max, minimum 1.
    def _preserve_floor(self):
        maxv = self._read_int(os.path.join(self.backlight, "max_brightness")) or 100
        return max(1, maxv // 10)

    def _restore_target(self):
        maxv = self._read_int(os.path.join(self.backlight, "max_brightness")) or 100
        floor = self._preserve_floor()
        saved = self.saved_brightness
        if saved and saved >= floor:
            return saved
        return maxv

    def set_blank(self, value):
        try:
            with open(FB_SYS + "blank", "w") as fh:
                fh.write(str(value))
            return True
        except OSError:
            return False

    def get_blank(self):
        return self._read_int(FB_SYS + "blank")

    def power_off(self):
        """Darken the panel for real: backlight first, then the framebuffer."""
        result = {"fb_blank": False, "backlight": False}
        if self.backlight:
            current = self._read_int(os.path.join(self.backlight, "brightness"))
            if current and current >= self._preserve_floor():
                self.saved_brightness = current
            result["backlight"] = self.set_brightness(0)
        result["fb_blank"] = self.set_blank(4)
        self.blanked = True
        return result

    def power_on(self):
        """Bring it back, restoring whatever was on screen."""
        result = {"fb_blank": False, "backlight": False, "repaint": False}
        result["fb_blank"] = self.set_blank(0)
        if self.backlight:
            result["backlight"] = self.set_brightness(self._restore_target())
        self.blanked = False
        self.repaint()
        result["repaint"] = self.last_frame is not None
        return result

    def status(self):
        return {
            "width": self.width,
            "height": self.height,
            "bpp": self.bpp,
            "stride": self.stride,
            "fb_blank": self.get_blank(),
            "backlight": self.backlight_state(),
        }


class HeadlessFramebuffer(Framebuffer):
    """In-memory stand-in for the physical screen.

    Active ONLY when DISPLAYD_FAKE_FB=1 (headless CI and local MCP demos
    with no /dev/fb0). The deployed daemon never sets it. Drawing goes to
    an in-memory BGRX buffer with the same layout snapshot() expects, so
    show/feed/snapshot behave identically minus photons."""

    def __init__(self, width=1920, height=1080):
        self.width = width
        self.height = height
        self.bpp = 32
        self.stride = self.width * 4
        self.fd = None
        self.last_frame = None
        self.blanked = False
        self.saved_brightness = None
        self.backlight = None
        self.vt_fd = None

    def raw(self, data):
        self.last_frame = bytes(data)

    def repaint(self):
        pass

    def black(self):
        self.raw(bytes(self.width * self.height * 4))

    def take_console(self):
        return False

    def release_console(self):
        pass

    def set_blank(self, value):
        self.blanked = (int(value) != 0)
        return True

    def get_blank(self):
        return 4 if self.blanked else 0

    def power_off(self):
        self.set_blank(4)
        return {"fb_blank": True, "backlight": False}

    def power_on(self):
        self.set_blank(0)
        return {"fb_blank": True, "backlight": False,
                "repaint": self.last_frame is not None}


class Screen:
    """The handle handed to renderers, plus the shared helpers they need."""

    NAMED_COLORS = {
        "black": (0, 0, 0),
        "white": (255, 255, 255),
        "red": (255, 0, 0),
        "green": (0, 255, 0),
        "blue": (0, 0, 255),
        "yellow": (255, 255, 0),
        "cyan": (0, 255, 255),
        "magenta": (255, 0, 255),
        "grey": (128, 128, 128),
        "gray": (128, 128, 128),
        "orange": (255, 165, 0),
    }

    def __init__(self, fb):
        self.fb = fb
        self.W = fb.width
        self.H = fb.height
        self.present_lock = threading.Lock()
        self.feeds = None  # wired by DisplayDaemon to its FeedStore
        self.current_view = None
        self.on_present = None  # daemon hook(img): frame cache + switch timing

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        """Compose-then-swap: the caller hands over one complete frame and
        the daemon swaps it in with a single write -- never a blank or a
        partial frame."""
        with self.present_lock:
            self.fb.present(img)
            hook = self.on_present
        if hook is not None:
            try:
                hook(img)
            except Exception:
                pass

    def clear(self, background=(0, 0, 0)):
        self.present(self.new_image(background))

    def get_input(self, renderer, input_name):
        """Buffered payloads pushed to (renderer, input), oldest first.
        Empty when nothing has arrived yet -- renderers must handle that."""
        store = self.feeds
        if store is None:
            return []
        try:
            return store.get(renderer, input_name)
        except Exception:
            return []

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        """Accept '#rgb', '#rrggbb', a few names, or an (r,g,b) tuple."""
        if value is None or value == "":
            return default
        if isinstance(value, (list, tuple)) and len(value) == 3:
            return tuple(int(v) for v in value)
        text = str(value).strip()
        if text.lower() in cls.NAMED_COLORS:
            return cls.NAMED_COLORS[text.lower()]
        digits = text.lstrip("#")
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        try:
            return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return default

    @staticmethod
    def font_path(family="DejaVuSans-Bold"):
        candidates = [
            os.path.join("/usr/share/fonts/truetype/dejavu", family + ".ttf"),
            os.path.join("/usr/share/fonts/truetype/dejavu", "DejaVuSans-Bold.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None


def load_renderers(directory):
    """Every module in renderers/ becomes a renderer. Drop a file in, it appears."""
    found = {}
    if not os.path.isdir(directory):
        return found
    for entry in sorted(os.listdir(directory)):
        if not entry.endswith(".py") or entry.startswith("_") or entry.startswith("."):
            continue
        path = os.path.join(directory, entry)
        name = entry[:-3]
        try:
            spec = importlib.util.spec_from_file_location("displayd_renderer_" + name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as err:  # a broken plugin must not take the daemon down
            found[name] = {"broken": str(err)}
            continue
        if not hasattr(mod, "run"):
            continue
        found[getattr(mod, "NAME", name)] = {
            "module": mod,
            "description": getattr(mod, "DESCRIPTION", ""),
            "params": getattr(mod, "PARAMS", {}),
            "inputs": getattr(mod, "INPUTS", {}),
            "static": getattr(mod, "STATIC", True),
        }
    return found


INPUT_TYPES = {"string", "integer", "number", "boolean", "object", "array"}


def _type_ok(value, typename):
    if typename == "string":
        return isinstance(value, str)
    if typename == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typename == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typename == "boolean":
        return isinstance(value, bool)
    if typename == "object":
        return isinstance(value, dict)
    if typename == "array":
        return isinstance(value, (list, tuple))
    return False


def validate_value(value, spec, where="value"):
    """Validate a feed payload (or a selection param) against one schema
    spec. Raises ValueError on mismatch. Unknown object keys are allowed so
    enriched payloads keep passing when the sender adds a field."""
    if not isinstance(spec, dict):
        raise ValueError("%s: bad schema spec" % where)
    typename = spec.get("type", "string")
    if typename not in INPUT_TYPES:
        raise ValueError("%s: unknown type %r" % (where, typename))
    if not _type_ok(value, typename):
        raise ValueError("%s: expected %s, got %s"
                         % (where, typename, type(value).__name__))
    if typename == "object":
        for key in spec.get("required") or []:
            if key not in value:
                raise ValueError("%s: missing required field %r" % (where, key))
        for key, subspec in (spec.get("properties") or {}).items():
            if key in value:
                validate_value(value[key], subspec, "%s.%s" % (where, key))
    if typename == "array" and "items" in spec:
        for i, item in enumerate(value):
            validate_value(item, spec["items"], "%s[%d]" % (where, i))


def validate_params(params, schema):
    """Selection-time check: required params present, supplied params typed.
    Unknown params are ignored (forward compatibility)."""
    params = params or {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    for key, spec in (schema or {}).items():
        if not isinstance(spec, dict):
            continue
        if spec.get("required") and key not in params:
            raise ValueError("missing required param %r" % key)
        if key in params:
            validate_value(params[key], spec, "param %r" % key)
    return params


class FeedStore:
    """Background feed cache: what bridges push into running views.

    Pushes never touch the draw path -- they append to an in-memory deque
    under a short lock, so a switch or a draw never awaits I/O. A view that
    is not currently selected still accumulates inputs, so selecting it later
    is instantly populated. No framebuffer needed; unit-testable."""

    DEFAULT_BUFFER = 100
    STALE_AFTER = 300.0  # seconds without a push before health reads "stale"

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}  # (renderer, input) -> {values, updated_at}

    @classmethod
    def _buffer_for(cls, spec):
        try:
            return max(1, int((spec or {}).get("buffer", cls.DEFAULT_BUFFER)))
        except (TypeError, ValueError):
            return cls.DEFAULT_BUFFER

    def declare(self, renderer, input_name, spec):
        """Register an input so it shows up as cold before anything arrives."""
        with self._lock:
            entry = self._data.get((renderer, input_name))
            if entry is None:
                self._data[(renderer, input_name)] = {
                    "values": collections.deque(maxlen=self._buffer_for(spec)),
                    "updated_at": None,
                }

    def push(self, renderer, input_name, payload, spec):
        validate_value(payload, spec or {}, "%s.%s" % (renderer, input_name))
        with self._lock:
            entry = self._data.get((renderer, input_name))
            if entry is None:
                entry = {"values": collections.deque(maxlen=self._buffer_for(spec)),
                         "updated_at": None}
                self._data[(renderer, input_name)] = entry
            entry["values"].append(payload)
            entry["updated_at"] = time.time()
            return {"count": len(entry["values"]), "updated_at": entry["updated_at"]}

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
            if updated is None:
                health, age = "cold", None
            else:
                age = round(now - updated, 1)
                health = "warm" if age < self.STALE_AFTER else "stale"
            out.setdefault(renderer, {})[input_name] = {
                "count": len(entry["values"]),
                "updated_at": updated,
                "age_seconds": age,
                "health": health,
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
        if updated is None:
            health, age = "cold", None
        else:
            age = round(time.time() - updated, 1)
            health = "warm" if age < self.STALE_AFTER else "stale"
        values = list(entry["values"])
        return {
            "renderer": renderer,
            "input": input_name,
            "count": len(values),
            "updated_at": updated,
            "age_seconds": age,
            "health": health,
            "latest": values[-1] if values else None,
        }


class DisplayDaemon:
    def __init__(self, policy_path=None, clock=None, feedback_path=None):
        if os.environ.get("DISPLAYD_FAKE_FB") == "1":
            self.fb = HeadlessFramebuffer()
        else:
            self.fb = Framebuffer()
        self.screen = Screen(self.fb)
        self.renderers = load_renderers(RENDERER_DIR)
        self.lock = threading.Lock()
        self.cache_lock = threading.Lock()
        self.feeds = FeedStore()
        self.screen.feeds = self.feeds
        self.screen.on_present = self._note_frame
        for name, entry in self.renderers.items():
            if "module" not in entry:
                continue
            for input_name, spec in (entry.get("inputs") or {}).items():
                self.feeds.declare(name, input_name, spec)
        self.frame_cache = {}   # renderer name -> last composed PIL image
        self.switch_pending = None  # start time of the in-flight switch
        self.last_switch_at = None
        self.last_switch_ms = None  # request -> first presented pixel
        self.last_frame_ms = None   # request -> first freshly drawn frame
        # Policy layer: activity clock, transient switching, idle-off.
        # Only mutating POSTs touch the clock -- GETs (including the
        # control page's 2s state/snapshot poll) are observation, not
        # activity, or idle-off could never fire while the page is open.
        self.policy = policy_module.Policy(
            policy_path if policy_path is not None else POLICY_FILE,
            clock=clock)
        # Display-feedback log (feedback.py): durable JSONL + per-note PNG
        # frames. feedback_path is a test hook; production uses the env
        # default next to the daemon, mirroring POLICY_FILE.
        self.feedback = feedback_module.FeedbackStore(path=feedback_path)
        self.transient_timer = None
        self.watchdog_stop = threading.Event()
        self.watchdog_thread = None
        self.stop_event = None
        self.thread = None
        self.current = None
        self.started_at = None
        self.last_error = None
        self.console_taken = self.fb.take_console()
        self.fb.set_blank(0)
        if self.fb.backlight:
            maxv = self.fb._read_int(os.path.join(self.fb.backlight, "max_brightness")) or 100
            cur = self.fb._read_int(os.path.join(self.fb.backlight, "brightness")) or 0
            if cur == 0:
                self.fb.set_brightness(maxv)

    def _note_frame(self, img):
        """Every composed frame lands here: cached per view for instant
        re-entry, and timed when a switch is in flight. Never blocks: the
        copy is a memcpy, no I/O."""
        now = time.time()
        with self.cache_lock:
            if self.current is not None:
                try:
                    self.frame_cache[self.current] = img.copy()
                except Exception:
                    pass
            if self.switch_pending is not None:
                self.last_frame_ms = round((now - self.switch_pending) * 1000, 1)
                if self.last_switch_ms is None:
                    # No cached frame covered this switch: the fresh draw
                    # is also the first pixel.
                    self.last_switch_ms = self.last_frame_ms
                self.switch_pending = None

    # ---- content -------------------------------------------------------

    def _run(self, entry, params, stop):
        try:
            entry["module"].run(self.screen, params, stop)
        except Exception as err:
            self.last_error = "%s: %s" % (type(err).__name__, err)

    def _start_view(self, name, params):
        """Put a view on screen without touching policy state.

        Manual entries (show/clear) go through the public methods, which
        record the base view and cancel transients first. Transient entries
        and returns come here directly, so a timer firing can never rewrite
        the base view or defeat a manual selection."""
        entry = self.renderers.get(name)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % name)
        started = time.time()
        with self.lock:
            with self.cache_lock:
                cached = self.frame_cache.get(name)
            with self.screen.present_lock:
                self.screen.current_view = name
                if cached is not None:
                    # Stale frame beats a pause: memcpy the last known good
                    # frame now; the fresh draw follows on the new thread.
                    try:
                        self.fb.present(cached)
                    except Exception:
                        cached = None
            self._stop_locked()
            stop = threading.Event()
            self.stop_event = stop
            self.current = name
            self.started_at = started
            self.last_error = None
            self.last_switch_at = started
            with self.cache_lock:
                if cached is not None:
                    self.last_switch_ms = round((time.time() - started) * 1000, 1)
                    self.switch_pending = started  # still time the fresh draw
                else:
                    self.last_switch_ms = None
                    self.switch_pending = started
            self.thread = threading.Thread(
                target=self._run, args=(entry, params or {}, stop), daemon=True
            )
            self.thread.start()
        return self.state()

    def _clear_internal(self):
        with self.lock:
            self._stop_locked()
            self.current = None
            self.started_at = None
            self.screen.current_view = None
            with self.cache_lock:
                self.switch_pending = None
        self.screen.clear()
        return self.state()

    def _cancel_transient_timer(self):
        timer, self.transient_timer = self.transient_timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _arm_transient(self, kind, token, duration):
        self._cancel_transient_timer()
        timer = threading.Timer(duration, self._transient_expired,
                                  args=(kind, token))
        timer.daemon = True
        self.transient_timer = timer
        timer.start()

    def _transient_expired(self, kind, token):
        """Return timer fired: restore the base view only if nothing
        manual happened since (generation check inside end_transient)."""
        base, ok = self.policy.end_transient(kind, token)
        if not ok:
            return
        try:
            if base is None:
                self._clear_internal()
            else:
                self._start_view(base["renderer"], base["params"])
        except (KeyError, ValueError):
            pass

    def _wake_if_idle(self):
        """Activity arrived while idle-off held the panel dark: power back
        on and repaint. Manual power-off is NOT woken -- the operator owns
        that state; only the watchdog's own power-off auto-wakes."""
        if self.policy.idle_off and self.fb.blanked:
            self.fb.power_on()
            self.policy.idle_off = False

    def show(self, name, params):
        entry = self.renderers.get(name)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % name)
        # Validate before touching what is on screen: a bad selection is
        # rejected and the current view keeps running undisturbed.
        validate_params(params or {}, entry.get("params") or {})
        self.policy.note_select(name, params)
        with self.lock:
            self._cancel_transient_timer()
            self._wake_if_idle()
        return self._start_view(name, params)

    def clear(self):
        self.policy.note_clear()
        with self.lock:
            self._cancel_transient_timer()
            self._wake_if_idle()
        return self._clear_internal()

    def _stop_locked(self):
        if self.stop_event is not None:
            self.stop_event.set()
        self.thread = None
        self.stop_event = None

    # ---- feeds + policy-driven behaviour -------------------------------

    def feed(self, renderer, input_name, payload):
        """Deliver a validated payload to a view's buffer, then consult
        the policy: a chat event may pull the panel to the chat view.
        Pure cache write first -- never disturbs what is on screen.
        Raises KeyError (unknown renderer/input) or ValueError (schema
        mismatch); both map to HTTP errors without side effects."""
        entry = self.renderers.get(renderer)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % renderer)
        spec = (entry.get("inputs") or {}).get(input_name)
        if spec is None:
            raise KeyError("unknown input: %s.%s" % (renderer, input_name))
        pushed = self.feeds.push(renderer, input_name, payload, spec)
        self.policy.note_feed()
        with self.lock:
            self._wake_if_idle()
        return {"feed": pushed,
                "attention": self._maybe_attention(renderer, input_name)}

    def _maybe_attention(self, fed_renderer, fed_input):
        """Chat-attention decision point. OFF by default; when enabled, a
        chat event pulls the panel to the configured view for return_after
        seconds (re-armed by each new event), then back to the base view.
        Only a feed to the configured view+input counts as a chat event;
        manual selections always win; a notice in flight suppresses."""
        cfg = self.policy.get_config()["chat_attention"]
        if not cfg["enabled"]:
            return {"switched": False, "reason": "disabled"}
        if fed_renderer != cfg["view"] or fed_input != cfg["input"]:
            return {"switched": False, "reason": "not-a-chat-event"}
        view = cfg["view"]
        if self.current == view:
            active = self.policy.active
            if active is not None and active["kind"] == "attention":
                token, _ = self.policy.begin_transient("attention", cfg["return_after"])
                with self.lock:
                    self._arm_transient("attention", token, cfg["return_after"])
                return {"switched": True, "reason": "re-armed"}
            return {"switched": False, "reason": "already-showing"}
        if self.fb.blanked:
            return {"switched": False, "reason": "screen-off"}
        entry = self.renderers.get(view)
        if not entry or "module" not in entry:
            return {"switched": False, "reason": "unknown-view"}
        token, superseded = self.policy.begin_transient("attention", cfg["return_after"])
        if token is None:
            return {"switched": False, "reason": "notice-active"}
        with self.lock:
            self._arm_transient("attention", token, cfg["return_after"])
        try:
            self._start_view(view, {})
        except (KeyError, ValueError):
            return {"switched": False, "reason": "unknown-view"}
        out = {"switched": True, "reason": "pulled", "view": view}
        if superseded:
            out["superseded"] = superseded
        return out

    # ---- display feedback --------------------------------------------

    def record_feedback(self, view, rating, categories=None, notes="",
                          params=None, agent="anonymous", include_frame=True):
        """Record one judgement about what a view looks like on the panel.

        Captures a /snapshot at feedback time so a later reader sees exactly
        what was judged. Raises KeyError (unknown view) or ValueError (bad
        rating/categories). Deliberately does NOT touch the policy clock:
        annotating the panel is not display activity, and counting it would
        keep an idle panel awake while reviewers write notes about it."""
        entry = self.renderers.get(view)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % view)
        png, size = None, None
        if include_frame:
            png = self.snapshot()
            if png is not None:
                size = (self.fb.width, self.fb.height)
        stored = self.feedback.record(
            view, rating, categories=categories, notes=notes,
            params=params, agent=agent, frame_png=png, frame_size=size)
        stored["snapshot_captured"] = png is not None
        return stored

    def list_feedback(self, view=None, limit=50):
        entries = self.feedback.list(view=view, limit=limit)
        return {"feedback": entries, "count": len(entries)}

    def get_feedback(self, entry_id):
        return self.feedback.get(entry_id)

    def feedback_frame(self, entry_id):
        """Raw PNG bytes of a note's frame. Raises KeyError/IOError."""
        with open(self.feedback.frame_path(entry_id), "rb") as fh:
            return fh.read()

    def feedback_summary(self):
        return self.feedback.summary()

    SEVERITIES = ("info", "warn", "critical")

    def notify(self, title, body="", severity="info", color=None, duration=None):
        """Show a transient notice, then return to the base view.

        Cheap switch-then-return, not composition (ISA D6 option B would
        draw over the current view; this interrupts it instead -- said
        plainly so the captain can decide). Notices preempt chat-attention;
        the return always goes to the base view. Raises ValueError on bad
        input."""
        cfg = self.policy.get_config()["notifications"]
        title = str(title or "").strip()
        if not title:
            raise ValueError("title is required")
        severity = str(severity or "info").lower()
        if severity not in self.SEVERITIES:
            raise ValueError("severity must be one of %s" % "/".join(self.SEVERITIES))
        if duration is None:
            duration = cfg["default_duration"]
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            raise ValueError("duration must be a number of seconds")
        if not (1 <= duration <= 300):
            raise ValueError("duration must be within [1, 300]")
        params = {"title": title, "severity": severity}
        if body:
            params["body"] = str(body)
        if color:
            params["color"] = str(color)
        entry = self.renderers.get("notice")
        if not entry or "module" not in entry:
            raise KeyError("notice renderer is not installed")
        validate_params(params, entry.get("params") or {})
        self.policy.note_api()
        token, superseded = self.policy.begin_transient("notice", duration)
        with self.lock:
            self._arm_transient("notice", token, duration)
            self._wake_if_idle()
        self._start_view("notice", params)
        out = {"view": "notice", "params": params, "return_in": duration}
        if superseded:
            out["superseded"] = superseded
        return out

    # ---- policy configuration surface ------------------------------------

    def get_policy(self):
        return {
            "config": self.policy.get_config(),
            "activity": self.policy.activity_snapshot(),
            "transient": self.policy.transient_status(),
            "idle_off": self.policy.idle_off,
        }

    def set_policy(self, patch):
        config, persisted = self.policy.update_config(patch or {})
        self.policy.note_api()
        state = self.get_policy()
        state["persisted"] = persisted
        return state

    # ---- idle watchdog ------------------------------------------------------

    def check_idle(self):
        """One watchdog tick: blank the panel when the inactivity window
        has elapsed. Public so tests can drive it deterministically."""
        if not self.policy.idle_due():
            return {"idle_off": self.policy.idle_off}
        with self.lock:
            if not self.policy.idle_due() or self.fb.blanked:
                return {"idle_off": self.policy.idle_off}
            self.fb.power_off()
            self.policy.idle_off = True
            return {"idle_off": True, "at": self.policy.last_activity()}

    def start_watchdog(self, interval=1.0):
        if self.watchdog_thread is not None:
            return
        self.watchdog_stop.clear()

        def _tick():
            while not self.watchdog_stop.wait(interval):
                try:
                    self.check_idle()
                except Exception:
                    pass

        self.watchdog_thread = threading.Thread(target=_tick, daemon=True)
        self.watchdog_thread.start()

    def stop_watchdog(self):
        self.watchdog_stop.set()
        self.watchdog_thread = None

    # ---- screen power --------------------------------------------------

    def set_power(self, power):
        power = (power or "").lower()
        if power not in ("on", "off"):
            raise ValueError("power must be 'on' or 'off'")
        self.policy.note_api()
        with self.lock:
            # A manual power call means the operator owns the power state:
            # it clears the watchdog's idle_off claim either way.
            self.policy.idle_off = False
            if power == "off":
                result = self.fb.power_off()
            else:
                result = self.fb.power_on()
        state = self.state()
        state["applied"] = result
        return state

    # ---- state ---------------------------------------------------------

    def state(self):
        return {
            "renderer": self.current,
            "params": None,
            "started_at": self.started_at,
            "age_seconds": round(time.time() - self.started_at, 1) if self.started_at else None,
            "screen": {
                "power": "off" if self.fb.blanked else "on",
                "fb_blank": self.fb.get_blank(),
                "backlight": self.fb.backlight_state(),
                "console_handover": self.console_taken,
            },
            "display": self.fb.status(),
            "last_error": self.last_error,
            "feeds": self.feeds.snapshot(),
            "policy": {
                "transient": self.policy.transient_status(),
                "idle_off": self.policy.idle_off,
            },
            "switch": {
                "at": self.last_switch_at,
                "first_pixel_ms": self.last_switch_ms,
                "fresh_frame_ms": self.last_frame_ms,
            },
        }

    def renderer_list(self):
        out = []
        for name, entry in sorted(self.renderers.items()):
            if "module" not in entry:
                out.append({"name": name, "broken": entry["broken"]})
                continue
            out.append(
                {
                    "name": name,
                    "description": entry["description"],
                    "params": entry["params"],
                    "inputs": entry.get("inputs", {}),
                    "static": entry["static"],
                }
            )
        return out

    def snapshot(self):
        if self.fb.last_frame is None:
            return None
        img = Image.frombytes(
            "RGB", (self.fb.width, self.fb.height), self.fb.last_frame, "raw", "BGRX"
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


CONTROL_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>displayd control</title>
<style>
  :root { color-scheme: dark; }
  body { background: #111; color: #eee; font-family: system-ui, sans-serif;
         max-width: 860px; margin: 0 auto; padding: 16px; }
  h1 { font-size: 1.4em; margin: 0 0 4px; }
  h2 { font-size: 1.1em; margin-top: 24px; border-bottom: 1px solid #333;
       padding-bottom: 4px; }
  .row { display: flex; gap: 16px; flex-wrap: wrap; }
  .card { background: #1c1c1c; border: 1px solid #333; border-radius: 8px;
          padding: 12px; flex: 1 1 240px; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
         background: #666; margin-right: 6px; vertical-align: baseline; }
  .dot.ok { background: #3d3; } .dot.bad { background: #f44; }
  #preview { width: 100%; aspect-ratio: 16/9; background: #000; object-fit: contain;
             border: 1px solid #333; border-radius: 8px; }
  label { display: block; margin: 8px 0 2px; font-size: 0.9em; }
  label .req { color: #f88; }
  label .help { color: #999; font-size: 0.85em; display: block; }
  input[type=text], input[type=number], select {
    width: 100%; box-sizing: border-box; padding: 6px;
    background: #222; color: #eee; border: 1px solid #444; border-radius: 4px; }
  button { padding: 8px 14px; margin: 8px 8px 0 0; cursor: pointer;
           background: #2a5; color: #061; border: 0; border-radius: 4px;
           font-weight: bold; }
  button.warn { background: #a53; color: #fff; }
  button.ghost { background: #333; color: #eee; }
  #result { margin-top: 12px; min-height: 1.4em; font-size: 0.9em; color: #9cf; }
  .meta { color: #aaa; font-size: 0.9em; }
  code { background: #222; padding: 1px 5px; border-radius: 3px; }
</style>
</head>
<body>
<h1>displayd control</h1>
<div class="meta"><span id="health" class="dot"></span><span id="healthtext">connecting&hellip;</span></div>

<h2>Now showing</h2>
<div class="row">
  <div class="card">
    <div>Renderer: <code id="cur-renderer">&ndash;</code></div>
    <div>On screen for: <span id="cur-age">&ndash;</span></div>
    <div>Power: <code id="cur-power">&ndash;</code></div>
    <div>Backlight: <span id="cur-bl">&ndash;</span></div>
    <div>Framebuffer blank: <code id="cur-blank">&ndash;</code></div>
    <div>Feeds: <span id="cur-feeds">&ndash;</span></div>
    <div>Last switch: <span id="cur-switch">&ndash;</span></div>
    <div>Last error: <span id="cur-err">none</span></div>
  </div>
  <div class="card">
    <img id="preview" alt="live preview of the panel">
  </div>
</div>

<h2>Show something</h2>
<div class="card">
  <label for="renderer">Renderer</label>
  <select id="renderer"></select>
  <div id="rdesc" class="meta"></div>
  <div id="params"></div>
  <button id="show">Show</button>
  <button id="clear" class="ghost">Blank screen</button>
</div>

<h2>Screen power</h2>
<div class="card">
  <button id="pon">Turn on</button>
  <button id="poff" class="warn">Turn off</button>
  <span class="meta">Off darkens the backlight and blanks the framebuffer;
  on restores both and repaints the last frame.</span>
</div>

<h2>Policy: what the screen does on its own</h2>
<div class="card">
  <label><input type="checkbox" id="pol-idle-en" style="width:auto"> Screen off after inactivity</label>
  <label for="pol-idle-after">Idle window (seconds)<span class="help">no mutating API or feed activity for this long blanks the panel; any activity wakes it (number, 5-86400)</span></label>
  <input type="number" id="pol-idle-after" min="5" max="86400">
  <label><input type="checkbox" id="pol-att-en" style="width:auto"> Chat attention: pull panel to chat on new message (off by default)</label>
  <label for="pol-att-view">Attention view<span class="help">renderer a chat event pulls to (string)</span></label>
  <input type="text" id="pol-att-view">
  <label for="pol-att-ret">Stay on chat per message (seconds)<span class="help">re-armed by each new message, then returns (number, 5-600)</span></label>
  <input type="number" id="pol-att-ret" min="5" max="600">
  <label for="pol-notify-dur">Notice duration default (seconds)<span class="help">how long a notification stays up when unset (number, 1-300)</span></label>
  <input type="number" id="pol-notify-dur" min="1" max="300">
  <div class="meta" id="pol-status">policy: loading&hellip;</div>
  <button id="polsave">Save policy</button>
</div>

<h2>Notify</h2>
<div class="card">
  <label for="nt-title">Title<span class="req"> *</span></label>
  <input type="text" id="nt-title">
  <label for="nt-body">Body</label>
  <input type="text" id="nt-body">
  <label for="nt-sev">Severity</label>
  <select id="nt-sev"><option>info</option><option>warn</option><option>critical</option></select>
  <label for="nt-dur">Duration (seconds, blank for policy default)</label>
  <input type="number" id="nt-dur" min="1" max="300">
  <button id="notify">Show notice</button>
  <span class="meta">Interrupts what is showing, then returns. Cheap switch-then-return, not composition (ISA D6).</span>
</div>

<div id="result"></div>

<script>
async function api(path, opts) {
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((body && body.error) || ("HTTP " + r.status));
  return body;
}
function say(msg, isErr) {
  const el = document.getElementById("result");
  el.textContent = msg; el.style.color = isErr ? "#f88" : "#9cf";
}
let SCHEMAS = {};
async function refreshState() {
  try {
    await api("/health");
    const s = await api("/state");
    document.getElementById("health").className = "dot ok";
    document.getElementById("healthtext").textContent =
      "healthy \u00b7 " + s.display.width + "x" + s.display.height +
      " \u00b7 " + Object.keys(SCHEMAS).length + " renderers";
    document.getElementById("cur-renderer").textContent = s.renderer || "(blank)";
    document.getElementById("cur-age").textContent =
      s.age_seconds == null ? "\u2013" : Math.round(s.age_seconds) + "s";
    document.getElementById("cur-power").textContent = s.screen.power;
    const bl = s.screen.backlight;
    document.getElementById("cur-bl").textContent = bl.available
      ? (bl.value + " / " + bl.max) : "no backlight device";
    document.getElementById("cur-blank").textContent = s.screen.fb_blank;
    const feeds = s.feeds || {};
    const bits = [];
    for (const [r, inputs] of Object.entries(feeds)) {
      for (const [i, f] of Object.entries(inputs)) {
        bits.push(r + "." + i + ":" + f.health + "(" + f.count + ")");
      }
    }
    document.getElementById("cur-feeds").textContent = bits.length ? bits.join(" ") : "none";
    const sw = s.switch || {};
    document.getElementById("cur-switch").textContent =
      (sw.first_pixel_ms == null ? "\u2013" : (sw.first_pixel_ms + " ms to pixel")) +
      (sw.fresh_frame_ms == null ? "" : (" / " + sw.fresh_frame_ms + " ms fresh"));
    const e = document.getElementById("cur-err");
    e.textContent = s.last_error || "none";
    e.style.color = s.last_error ? "#f88" : "";
  } catch (err) {
    document.getElementById("health").className = "dot bad";
    document.getElementById("healthtext").textContent = "unreachable: " + err.message;
  }
}
function buildParams(name) {
  const box = document.getElementById("params");
  box.innerHTML = "";
  const schema = (SCHEMAS[name] && SCHEMAS[name].params) || {};
  for (const [key, spec] of Object.entries(schema)) {
    const t = (spec && spec.type) || "string";
    const lab = document.createElement("label");
    lab.htmlFor = "p_" + key;
    lab.appendChild(document.createTextNode(key));
    if (spec && spec.required) {
      const r = document.createElement("span");
      r.className = "req"; r.textContent = " *";
      lab.appendChild(r);
    }
    if (spec && spec.help) {
      const h = document.createElement("span");
      h.className = "help"; h.textContent = spec.help + " (" + t + ")";
      lab.appendChild(h);
    }
    box.appendChild(lab);
    let inp;
    if (t === "boolean") {
      inp = document.createElement("input");
      inp.type = "checkbox";
    } else if (t === "integer" || t === "number") {
      inp = document.createElement("input");
      inp.type = "number";
      if (t === "number") inp.step = "any";
    } else {
      inp = document.createElement("input");
      inp.type = "text";
    }
    inp.id = "p_" + key;
    inp.dataset.pname = key;
    box.appendChild(inp);
  }
}
async function refreshRenderers(keep) {
  const data = await api("/renderers");
  const sel = document.getElementById("renderer");
  const prev = keep ? sel.value : null;
  sel.innerHTML = "";
  SCHEMAS = {};
  for (const r of data.renderers) {
    SCHEMAS[r.name] = r;
    const o = document.createElement("option");
    o.value = r.name;
    o.textContent = r.broken ? (r.name + " (broken: " + r.broken + ")")
                             : (r.name + (r.description ? (" \u2014 " + r.description) : ""));
    if (r.broken) o.disabled = true;
    sel.appendChild(o);
  }
  if (prev && SCHEMAS[prev]) sel.value = prev;
  const showDesc = () => {
    const r = SCHEMAS[sel.value];
    document.getElementById("rdesc").textContent = r
      ? ((r.static ? "static" : "animated") + (r.description ? (" \u00b7 " + r.description) : ""))
      : "";
    buildParams(sel.value);
  };
  sel.onchange = showDesc;
  showDesc();
}
function collectParams(name) {
  const schema = (SCHEMAS[name] && SCHEMAS[name].params) || {};
  const out = {};
  for (const [key, spec] of Object.entries(schema)) {
    const el = document.getElementById("p_" + key);
    if (!el) continue;
    const t = (spec && spec.type) || "string";
    if (t === "boolean") { out[key] = el.checked; continue; }
    const v = el.value.trim();
    if (v === "") continue;
    if (t === "integer") { const n = parseInt(v, 10); if (!Number.isNaN(n)) out[key] = n; }
    else if (t === "number") { const n = parseFloat(v); if (!Number.isNaN(n)) out[key] = n; }
    else out[key] = v;
  }
  return out;
}
async function refreshPreview() {
  const img = document.getElementById("preview");
  try {
    const r = await fetch("/snapshot?t=" + Date.now());
    if (!r.ok) return;
    const blob = await r.blob();
    const old = img.src;
    img.src = URL.createObjectURL(blob);
    if (old.startsWith("blob:")) URL.revokeObjectURL(old);
  } catch (e) { /* preview is best-effort; state poll reports health */ }
}
document.getElementById("show").onclick = async () => {
  const name = document.getElementById("renderer").value;
  try {
    await api("/show", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ renderer: name, params: collectParams(name) }) });
    say("showing " + name);
    refreshState(); refreshPreview();
  } catch (err) { say("show failed: " + err.message, true); }
};
document.getElementById("clear").onclick = async () => {
  try { await api("/clear", { method: "POST" }); say("screen blanked");
    refreshState(); refreshPreview(); }
  catch (err) { say("blank failed: " + err.message, true); }
};
document.getElementById("pon").onclick = async () => {
  try { await api("/screen/on", { method: "POST" }); say("screen on"); refreshState(); }
  catch (err) { say("power on failed: " + err.message, true); }
};
document.getElementById("poff").onclick = async () => {
  try { await api("/screen/off", { method: "POST" }); say("screen off"); refreshState(); }
  catch (err) { say("power off failed: " + err.message, true); }
};
async function refreshPolicy() {
  try {
    const p = await api("/policy");
    const c = p.config;
    document.getElementById("pol-idle-en").checked = !!c.idle.enabled;
    document.getElementById("pol-idle-after").value = c.idle.after_seconds;
    document.getElementById("pol-att-en").checked = !!c.chat_attention.enabled;
    document.getElementById("pol-att-view").value = c.chat_attention.view;
    document.getElementById("pol-att-ret").value = c.chat_attention.return_after;
    document.getElementById("pol-notify-dur").value = c.notifications.default_duration;
    const t = p.transient;
    document.getElementById("pol-status").textContent =
      "policy: idle " + (c.idle.enabled ? ("on after " + c.idle.after_seconds + "s") : "off") +
      " · attention " + (c.chat_attention.enabled ? ("on → " + c.chat_attention.view) : "off") +
      " · transient " + (t.active || "none") +
      (p.idle_off ? " · PANEL IDLE-OFF" : "");
  } catch (err) { say("policy load failed: " + err.message, true); }
}
document.getElementById("polsave").onclick = async () => {
  const num = (id) => { const v = document.getElementById(id).value.trim();
    return v === "" ? undefined : Number(v); };
  const patch = { idle: {}, chat_attention: {}, notifications: {} };
  patch.idle.enabled = document.getElementById("pol-idle-en").checked;
  const ia = num("pol-idle-after"); if (ia !== undefined) patch.idle.after_seconds = ia;
  patch.chat_attention.enabled = document.getElementById("pol-att-en").checked;
  const av = document.getElementById("pol-att-view").value.trim();
  if (av !== "") patch.chat_attention.view = av;
  const ar = num("pol-att-ret"); if (ar !== undefined) patch.chat_attention.return_after = ar;
  const nd = num("pol-notify-dur"); if (nd !== undefined) patch.notifications.default_duration = nd;
  try { await api("/policy", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch) });
    say("policy saved"); refreshPolicy(); }
  catch (err) { say("policy save failed: " + err.message, true); }
};
document.getElementById("notify").onclick = async () => {
  const body = { title: document.getElementById("nt-title").value,
    body: document.getElementById("nt-body").value,
    severity: document.getElementById("nt-sev").value };
  const d = document.getElementById("nt-dur").value.trim();
  if (d !== "") body.duration = Number(d);
  try { await api("/notify", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    say("notice showing"); refreshState(); refreshPreview(); }
  catch (err) { say("notify failed: " + err.message, true); }
};
(async function init() {
  try { await refreshRenderers(false); }
  catch (err) { say("could not load renderers: " + err.message, true); }
  await refreshState();
  refreshPreview();
  refreshPolicy();
  setInterval(refreshState, 2000);
  setInterval(refreshPreview, 2000);
  setInterval(() => refreshRenderers(true), 15000);
})();
</script>
</body>
</html>
"""


DAEMON = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_raw(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None, "empty body"
        try:
            return json.loads(self.rfile.read(length).decode() or "null"), None
        except ValueError:
            return None, "invalid JSON"

    def _body(self):
        payload, _ = self._body_raw()
        return payload if isinstance(payload, dict) else {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, CONTROL_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path in ("/health", "/healthz"):
            return self._send(200, {"ok": True})
        if path == "/state":
            return self._send(200, DAEMON.state())
        if path == "/renderers":
            return self._send(200, {"renderers": DAEMON.renderer_list()})
        if path == "/snapshot":
            png = DAEMON.snapshot()
            if png is None:
                return self._send(404, {"error": "nothing has been drawn yet"})
            return self._send(200, png, "image/png")
        if path == "/policy":
            return self._send(200, DAEMON.get_policy())
        if path == "/feedback":
            query = parse_qs(urlsplit(self.path).query)
            try:
                return self._send(200, DAEMON.list_feedback(
                    view=(query.get("view", [None])[0]),
                    limit=(query.get("limit", [50])[0])))
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
        if path == "/feedback/summary":
            return self._send(200, DAEMON.feedback_summary())
        if path.startswith("/feedback/"):
            parts = path.split("/")
            if len(parts) == 3 and parts[2]:
                try:
                    return self._send(200, DAEMON.get_feedback(parts[2]))
                except KeyError as exc:
                    return self._send(404, {"error": str(exc)})
            if len(parts) == 4 and parts[2] and parts[3] == "frame":
                try:
                    return self._send(200, DAEMON.feedback_frame(parts[2]),
                                      "image/png")
                except KeyError as exc:
                    return self._send(404, {"error": str(exc)})
                except IOError as exc:
                    return self._send(404, {"error": str(exc),
                                            "frame_present": False})
            return self._send(404, {"error": "not found"})
        if path.startswith("/feed/"):
            parts = path.split("/")
            if len(parts) != 4 or not parts[2] or not parts[3]:
                return self._send(404, {"error": "use /feed/<renderer>/<input>"})
            try:
                return self._send(200, DAEMON.feeds.status(parts[2], parts[3]))
            except KeyError as exc:
                return self._send(404, {"error": str(exc)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path.startswith("/feed/"):
            parts = path.split("/")
            if len(parts) != 4 or not parts[2] or not parts[3]:
                return self._send(404, {"error": "use /feed/<renderer>/<input>"})
            payload, err = self._body_raw()
            if err:
                return self._send(400, {"error": err})
            try:
                result = DAEMON.feed(parts[2], parts[3], payload)
            except KeyError as exc:
                return self._send(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(200, {"ok": True, "renderer": parts[2],
                                    "input": parts[3], "feed": result["feed"],
                                    "attention": result["attention"]})
        if path == "/notify":
            body = self._body()
            if not DAEMON.policy.get_config()["notifications"]["enabled"]:
                return self._send(409, {"error": "notifications are disabled"})
            try:
                result = DAEMON.notify(
                    body.get("title"), body.get("body", ""),
                    body.get("severity", "info"), body.get("color"),
                    body.get("duration"))
            except KeyError as exc:
                return self._send(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(200, result)
        if path == "/policy":
            try:
                return self._send(200, DAEMON.set_policy(self._body()))
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
        if path == "/feedback":
            body = self._body()
            try:
                stored = DAEMON.record_feedback(
                    body.get("view"), body.get("rating"),
                    categories=body.get("categories"),
                    notes=body.get("notes", ""),
                    params=body.get("params"),
                    agent=body.get("agent", "anonymous"),
                    include_frame=body.get("include_frame", True))
            except KeyError as exc:
                return self._send(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            except TypeError as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(201, stored)
        body = self._body()
        try:
            if path == "/show":
                name = body.get("renderer") or body.get("name")
                if not name:
                    return self._send(400, {"error": "renderer is required"})
                return self._send(200, DAEMON.show(name, body.get("params")))
            if path == "/clear":
                return self._send(200, DAEMON.clear())
            if path in ("/screen", "/screen/off", "/screen/on"):
                power = body.get("power")
                if path.endswith("/off"):
                    power = "off"
                elif path.endswith("/on"):
                    power = "on"
                return self._send(200, DAEMON.set_power(power))
        except KeyError as err:
            return self._send(404, {"error": str(err)})
        except ValueError as err:
            return self._send(400, {"error": str(err)})
        return self._send(404, {"error": "not found"})


def main():
    global DAEMON
    parser = argparse.ArgumentParser(description="displayd - API-driven display server")
    parser.add_argument("--bind", default=BIND,
                        help="address to listen on (default: %(default)s, loopback only)")
    parser.add_argument("--port", type=int, default=PORT,
                        help="TCP port to listen on (default: %(default)s)")
    args = parser.parse_args()
    DAEMON = DisplayDaemon()
    DAEMON.clear()
    DAEMON.start_watchdog()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print("displayd listening on %s:%d with %d renderer(s)" % (args.bind, args.port, len(DAEMON.renderers)))
    try:
        server.serve_forever()
    finally:
        DAEMON.fb.release_console()


if __name__ == "__main__":
    main()
