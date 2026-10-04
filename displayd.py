#!/usr/bin/env python3
"""displayd - a tiny content-agnostic display daemon.

Owns the physical screen of a headless Linux box and exposes it through a
small JSON API.  All content comes from renderer plugins dropped into
renderers/ -- the core knows nothing about any particular one.

API
  GET  /                             web control page (this panel)
  GET  /health                       liveness
  GET  /version                      application semver (APP_VERSION)
  GET  /state                        what is showing + screen power
  GET  /renderers                    available renderers and their params
  GET  /snapshot                     PNG of the last presented frame
  POST /show    {"renderer":"name","params":{...}}
  POST /layout  {"regions":[{"name":...,"renderer":...,"params"?,
               "height"?/"width"?/"rect"?/"row"?/"col"?...}]} static regions
  GET  /layout                       current layout (null when inactive)
  DELETE /layout                     clear the layout (blank screen)
  POST /feed/<renderer>/<input>  push a validated payload into a view
  POST /notify  {"title":...,"body"?,"severity"?,"duration"?} transient notice
  POST /reload  {"sha":..., "highlights"?} reload confirmation
               (RELOADED + SHA + QR, plus an optional bounded
               commit-message summary drawn as text only, never in
               the QR), stays until confirmed: a scan of the relay QR
               or a tap returns early (POST /reload/confirm,
               POST /touch/tap)
  GET  /r/<token>  one-time scan relay: 302 to the commit page + confirm
  POST /reload/confirm  {"via"?} tap/scan confirm path for the reload
               view only (409 when none showing)
  POST /touch/tap  dismiss an active reload transient (tap-to-return),
               no-op for anything else
  POST /touch/resolve  {x,y} or {x_norm,y_norm}: read-only tap
               resolution against the CURRENT UI (region + semantic
               action, view/mode-gated like the touch path); dispatches
               nothing, changes no state
  GET  /policy  autonomous-behaviour config + activity clock
  POST /policy  {"idle":{...},"chat_attention":{...},"notifications":{...}}
  POST /clear                        blank the screen to black
  POST /screen  {"power":"on"|"off"} also /screen/on and /screen/off

The API has no authentication, so it listens on 127.0.0.1 by default.  Bind
wider only deliberately -- see --bind / --port (or DISPLAYD_BIND / DISPLAYD_PORT).
"""

import argparse
import collections
import copy
import importlib.util
import io
import ipaddress
import json
import os
import re
import secrets
import threading
import hmac
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from PIL import Image

import playlist as playlist_module
import policy as policy_module
import feedback as feedback_module
from renderers import reload_highlights as reload_highlights_module

FB = "/dev/fb0"
FB_SYS = "/sys/class/graphics/fb0/"
BACKLIGHT_GLOB = "/sys/class/backlight"
# The API is unauthenticated, so the default is loopback only: reaching it from
# another machine is a deliberate choice (--bind or DISPLAYD_BIND), and should be
# paired with a host firewall or a private network such as a VPN or tailnet.
PORT = int(os.environ.get("DISPLAYD_PORT", "8980"))
BIND = os.environ.get("DISPLAYD_BIND", "127.0.0.1")
# Application version: the single source of truth for displayd's semver.
# It lives here -- not in pyproject.toml -- because the daemon ships as a
# plain script (rsync + systemd, never pip-installed) and still supports
# Python 3.8+, so it cannot rely on importlib.metadata or tomllib to read
# a [project] table. Bump per CHANGELOG.md's convention on every change;
# the daemon reports it via GET /version, GET /state's "version" key,
# and the startup log line in main().
APP_VERSION = "0.6.0"
# Optional shared secret for the HTTP API. When set, every request (except
# the unauthenticated health probes below) must carry
#   Authorization: Bearer <token>
# Unset means the API stays open -- the historical default -- and the
# loopback binding remains the only protection. Setting this on a box that
# binds wider than 127.0.0.1 is how one deliberately exposes the API
# off-host with a shared secret in front of it.
API_TOKEN = os.environ.get("DISPLAYD_API_TOKEN", "").encode()
RENDERER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "renderers")


def _load_shared_helper(name):
    """Load a shared renderers/ helper for daemon-side reuse.

    Helpers have no run(), so the renderer loader skips them -- but the
    daemon itself can still use them (home_chrome's compositor chrome).
    Raises like any import: callers that must survive a broken helper
    catch it (a missing badge must never take the daemon down)."""
    path = os.path.join(RENDERER_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("displayd_" + name,
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    home_chrome_module = _load_shared_helper("home_chrome")
except Exception:
    home_chrome_module = None
try:
    sleep_chrome_module = _load_shared_helper("sleep_chrome")
except Exception:
    sleep_chrome_module = None
# Dedicated dark view behind the wake target (renderers/sleep.py):
# powering off also switches here so the touch service's per-view wake
# region goes live exactly while the panel is asleep (see set_power).
SLEEP_VIEW = "sleep"
try:
    macbook_map_module = _load_shared_helper("macbook_map")
except Exception:
    macbook_map_module = None
try:
    talon_apps_module = _load_shared_helper("talon_apps")
except Exception:
    talon_apps_module = None
# (The old side-column geometry, talon_layout, is still used by
# talon_apps.groups for the unified dock summary; the merged macbook
# header maps taps through macbook_layout instead.)
try:
    macbook_layout_module = _load_shared_helper("macbook_layout")
except Exception:
    macbook_layout_module = None
try:
    import touch_audit
except Exception:
    touch_audit = None
# Host touch.json (host-local, never overwritten by deploys): read-only
# context for /touch/check, never the live set (see touch_audit.py).
TOUCH_JSON_PATH = os.environ.get(
    "DISPLAYD_TOUCH_JSON",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "touch.json"),
)
VT = os.environ.get("DISPLAYD_VT", "/dev/tty1")
POLICY_FILE = os.environ.get(
    "DISPLAYD_POLICY",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.json"),
)
# Delivery stamp (written by deploy.sh, read by GET /deploy and the
# control page): JSON {"date": <UTC ISO-8601>, "sha": <40-char commit>,
# "deployer": <user>}. Host-side only -- never committed to the repo --
# so a missing or unreadable file simply means "never recorded".
DEPLOY_STAMP_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "DEPLOYED")


def deploy_stamp_path():
    return os.environ.get("DISPLAYD_DEPLOY_STAMP", DEPLOY_STAMP_FILE)


def read_deploy_stamp(path=None):
    """Last delivery stamp as a JSON-safe dict.

    Returns {"deployed": True, "date": ..., "sha": ..., "deployer": ...}
    when the stamp file holds JSON with a sha, else {"deployed": False}.
    Read-only and total: a missing or corrupt file is "never recorded",
    never an error."""
    try:
        with open(path or deploy_stamp_path()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"deployed": False}
    if not isinstance(data, dict) or not data.get("sha"):
        return {"deployed": False}
    return {"deployed": True,
            "date": data.get("date"),
            "sha": data.get("sha"),
            "deployer": data.get("deployer")}
# Reload confirmation (POST /reload): the QR payload is a panel-served
# one-time relay URL (GET /r/<token>) -- never the commit page directly,
# never the repository homepage, never a caller-supplied URL. Scanning the
# relay 302-redirects the scanner to the commit page AND confirms the view
# (the panel returns to whatever was showing). A screen tap while the
# reload view is showing confirms the same way (POST /reload/confirm).
# Mirrors renderers/reload.py COMMIT_URL_PREFIX and SHA_RE;
# tests/test_reload.py asserts the two agree so the rule cannot drift
# between validation and rendering.
RELOAD_COMMIT_URL_PREFIX = "https://github.com/trillium/displayd/commit/"
RELOAD_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
# Reload confirmation (POST /reload) stays up indefinitely until dismissed
# by a touchscreen tap (POST /touch/tap) or cancelled by a manual /show or
# /clear -- it never expires by duration. The duration constants below are
# kept only to validate a legacy `duration` field when callers still send
# one (accepted, ignored for expiry); new clients should omit it.
RELOAD_DEFAULT_DURATION = None  # indefinite: no automatic return
RELOAD_DURATION_MIN, RELOAD_DURATION_MAX = 1, 300
# One-time relay tokens: url-safe, single-scan, valid only while the
# confirmation window is open (expiry == the reload duration). The token
# shape below is also the renderer's relay-URL acceptance rule, so the
# renderer can never be talked into encoding an arbitrary caller URL.
RELOAD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
RELOAD_RELAY_PATH = "/r/"
# Tailnet carrier-grade NAT range: the captain's phone and the panel meet
# here. The relay QR is only useful when the panel binds inside it.
TAILNET_CGNAT = "100.64.0.0/10"


def _relay_bind_host():
    """Address the panel binds (read live so tests can override the env)."""
    return (os.environ.get("DISPLAYD_BIND") or BIND or "127.0.0.1").strip()


def _relay_port():
    try:
        return int(os.environ.get("DISPLAYD_PORT") or PORT)
    except (TypeError, ValueError):
        return PORT


def relay_base_url():
    """(base, reachable, reason) for the panel-served relay URL.

    Reachability reasoning (explicit, per the task constraint): the
    captain scans with his phone on the tailnet (Tailscale), so the relay
    is reachable only when the panel itself binds a tailnet address
    (100.64.0.0/10 -- the address the panel already binds on lnx-server
    via DISPLAYD_BIND). A loopback bind (127.0.0.0/8, ::1, localhost),
    a wildcard bind (0.0.0.0 -- never used here: the API has no auth),
    a LAN literal, a public IP, or a hostname is NOT reachable from the
    phone, so the caller must fall back to tap-only and say so plainly --
    never serve a dead QR silently.
    """
    host = _relay_bind_host()
    port = _relay_port()
    bare = host.strip().strip("[]")
    try:
        addr = ipaddress.ip_address(bare.lower() if bare else "")
    except ValueError:
        if bare.lower() in ("localhost",) or bare.lower().endswith(".localhost"):
            return None, False, ("loopback bind %r: the phone on the tailnet "
                                 "cannot reach it; tap-only" % host)
        return None, False, ("non-IP bind %r: not a tailnet literal the phone "
                             "can reach; tap-only" % host)
    if addr.is_loopback:
        return None, False, ("loopback bind %r: the phone on the tailnet "
                             "cannot reach it; tap-only" % host)
    try:
        tailnet = ipaddress.ip_network(TAILNET_CGNAT)
    except ValueError:  # pragma: no cover - constant is fixed
        return None, False, "tailnet range misconfigured; tap-only"
    if addr in tailnet:
        return ("http://%s:%d" % (bare, port), True,
                "tailnet bind %s: reachable from the phone on the tailnet"
                % bare)
    if bare == "0.0.0.0":
        return None, False, ("wildcard bind: no single address the phone can "
                             "use; tap-only (and never bind 0.0.0.0: no auth)")
    return None, False, ("bind %r is outside %s: the phone on the tailnet "
                         "cannot reach it; tap-only" % (host, TAILNET_CGNAT))

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
        self.overlay = None  # playlist progress bar hook: fn(img) -> img
        self._base = None  # last pre-overlay frame, for overlay repaints

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        """Compose-then-swap: the caller hands over one complete frame and
        the daemon swaps it in with a single write -- never a blank or a
        partial frame."""
        with self.present_lock:
            if self.overlay is None:
                self._base = None
                self.fb.present(img)
            else:
                try:
                    self._base = img.copy()
                    self.fb.present(self.overlay(img.copy()))
                except Exception:
                    self._base = None
                    self.fb.present(img)
            hook = self.on_present
        if hook is not None:
            try:
                # Pre-overlay frame: the frame cache stays bar-free, so a
                # cached re-entry never serves a stale progress bar.
                hook(img)
            except Exception:
                pass

    def repaint_overlay(self):
        """Re-composite the overlay onto the last frame and present.

        Lets the playlist bar advance smoothly on STATIC views that park
        after a single present. Best-effort: never raises. Bypasses the
        on_present hook on purpose: overlay ticks are not fresh draws and
        must not pollute the frame cache or switch timing."""
        with self.present_lock:
            if self.overlay is None or self._base is None:
                return
            try:
                self.fb.present(self.overlay(self._base.copy()))
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


class RegionScreen(Screen):
    """A Screen bound to one layout region (ISA D6 option B).

    Same renderer contract -- W/H, new_image, present, clear, get_input,
    color, font_path -- but scoped to the region's pixel box. present()
    caches this region's frame and recomposites the whole panel from the
    per-region cache, so updating one region never disturbs the others."""

    def __init__(self, daemon, region_name, width, height):
        self.fb = daemon.fb
        self.W = width
        self.H = height
        self.present_lock = daemon.screen.present_lock
        self.feeds = daemon.feeds
        self.current_view = None  # set to the bound renderer on start
        self.on_present = None  # unused: regions report via _present_region
        self._daemon = daemon
        self.region_name = region_name

    def present(self, img):
        self._daemon._present_region(self.region_name, img)


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
    if typename == "string" and "maxLength" in spec:
        try:
            cap = int(spec["maxLength"])
        except (TypeError, ValueError):
            cap = -1
        if cap >= 0 and len(value) > cap:
            raise ValueError("%s: string length %d over cap %d"
                             % (where, len(value), cap))


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


MAX_LAYOUT_REGIONS = 16


def _parse_dim(value, total, where, allow_zero=False):
    """Parse one region dimension: px int, \"NNpx\", or \"NN%\" of total.
    Returns int px. Raises ValueError on anything unusable."""
    if isinstance(value, bool):
        raise ValueError("%s: must be a pixel size or percentage" % where)
    if isinstance(value, (int, float)):
        px = int(value)
        if px < (0 if allow_zero else 1):
            raise ValueError("%s: must be at least %dpx" %
                             (where, 0 if allow_zero else 1))
        return px
    text = str(value).strip().lower()
    if text.endswith("%"):
        try:
            frac = float(text[:-1].strip()) / 100.0
        except ValueError:
            raise ValueError("%s: bad percentage %r" % (where, value))
        if not 0 < frac <= 1:
            raise ValueError("%s: percentage must be within (0, 100]" % where)
        return max(1, int(round(total * frac)))
    if text.endswith("px"):
        text = text[:-2].strip()
    try:
        px = int(float(text))
    except ValueError:
        raise ValueError("%s: bad size %r" % (where, value))
    if px < 1:
        raise ValueError("%s: must be at least 1px" % where)
    return px


def _grid_rect(row, col, row_span, col_span, rows, cols, width, height):
    """One grid cell as absolute px, tiling the screen edge to edge."""
    x = col * width // cols
    w = (col + col_span) * width // cols - x
    y = row * height // rows
    h = (row + row_span) * height // rows - y
    return (x, y, max(1, w), max(1, h))


def parse_layout(payload, width, height, renderers):
    """Validate a POST /layout body into bound regions.

    Pure: no side effects, so a bad layout is rejected without disturbing
    what is on screen -- the same atomicity guarantee POST /show gives.
    Raises KeyError (unknown renderer) or ValueError (bad shape/geometry).

    Returns a list of {"name", "renderer", "params", "rect": (x, y, w, h)}.

    Geometry, per region (first match wins):
      rect:  {"x","y","w"/"width","h"/"height"} or [x, y, w, h]
             (each px or %; x/w relative to width, y/h to height)
      grid:  row/col (+ row_span/col_span, default 1) tiled over
             top-level rows x cols (inferred when omitted)
      stack: height (vertical split, full width) or width (horizontal
             split, full height); omitted sizes share the screen evenly.
    """
    if not isinstance(payload, dict):
        raise ValueError("layout must be an object")
    regions = payload.get("regions")
    if not isinstance(regions, list) or not regions:
        raise ValueError("'regions' must be a non-empty list")
    if len(regions) > MAX_LAYOUT_REGIONS:
        raise ValueError("at most %d regions" % MAX_LAYOUT_REGIONS)

    names = set()
    bound = []
    for i, region in enumerate(regions):
        where = "regions[%d]" % i
        if not isinstance(region, dict):
            raise ValueError("%s: must be an object" % where)
        name = region.get("name", "region-%d" % i)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("%s: 'name' must be a non-empty string" % where)
        if name in names:
            raise ValueError("duplicate region name %r" % name)
        names.add(name)
        renderer = region.get("renderer")
        if not renderer:
            raise ValueError("%s: 'renderer' is required" % where)
        entry = (renderers or {}).get(renderer)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % renderer)
        params = validate_params(region.get("params"),
                                 entry.get("params") or {})
        bound.append({"name": name, "renderer": renderer,
                      "params": params, "_spec": region, "_where": where})

    grid_mode = (payload.get("rows") is not None
                 or payload.get("cols") is not None
                 or any("row" in r["_spec"] or "col" in r["_spec"]
                          for r in bound))
    rect_mode = not grid_mode and any(isinstance(r["_spec"].get("rect"),
                                                 (dict, list, tuple))
                                      for r in bound)
    if grid_mode:
        _assign_grid(payload, bound, width, height)
    elif rect_mode:
        for r in bound:
            r["rect"] = _assign_rect(r["_spec"], r["_where"], width, height)
    else:
        _assign_stack(bound, width, height)

    for r in bound:
        x, y, w, h = r["rect"]
        # Clip to the panel; a region reduced to nothing is a bad layout.
        x = max(0, min(x, width - 1))
        y = max(0, min(y, height - 1))
        w = max(1, min(w, width - x))
        h = max(1, min(h, height - y))
        r["rect"] = (x, y, w, h)
        del r["_spec"]
        del r["_where"]
    return bound


def _assign_grid(payload, bound, width, height):
    """Tile regions over a rows x cols grid. Missing row/col auto-fills
    free cells in order; spans default to 1."""
    spans = []
    for r in bound:
        spec, where = r["_spec"], r["_where"]
        try:
            rs = int(spec.get("row_span", spec.get("rowspan", 1)))
            cs = int(spec.get("col_span", spec.get("colspan", 1)))
        except (TypeError, ValueError):
            raise ValueError("%s: spans must be integers" % where)
        if rs < 1 or cs < 1:
            raise ValueError("%s: spans must be at least 1" % where)
        spans.append((rs, cs))
    rows = payload.get("rows")
    cols = payload.get("cols")
    try:
        rows = int(rows) if rows is not None else None
        cols = int(cols) if cols is not None else None
    except (TypeError, ValueError):
        raise ValueError("'rows'/'cols' must be integers")
    need_rows = 0
    need_cols = 0
    for r, (rs, cs) in zip(bound, spans):
        spec = r["_spec"]
        if "row" in spec:
            need_rows = max(need_rows, int(spec["row"]) + rs)
        if "col" in spec:
            need_cols = max(need_cols, int(spec["col"]) + cs)
    if rows is None:
        rows = max(need_rows, 1)
    if cols is None:
        cols = max(need_cols, 1)
    if rows < 1 or cols < 1:
        raise ValueError("'rows'/'cols' must be at least 1")
    if rows * cols > MAX_LAYOUT_REGIONS * 4:
        raise ValueError("'rows' x 'cols' grid is too large")
    taken = set()
    auto = 0
    for r, (rs, cs) in zip(bound, spans):
        spec, where = r["_spec"], r["_where"]
        if "row" in spec or "col" in spec:
            try:
                row = int(spec.get("row", 0))
                col = int(spec.get("col", 0))
            except (TypeError, ValueError):
                raise ValueError("%s: row/col must be integers" % where)
        else:
            while auto in taken:
                auto += 1
            row, col = auto // cols, auto % cols
        if not (0 <= row < rows) or not (0 <= col < cols):
            raise ValueError("%s: row/col outside the %dx%d grid"
                             % (where, rows, cols))
        if row + rs > rows or col + cs > cols:
            raise ValueError("%s: span overflows the %dx%d grid"
                             % (where, rows, cols))
        for rr in range(row, row + rs):
            for cc in range(col, col + cs):
                if (rr, cc) in taken:
                    raise ValueError("%s: overlaps another region" % where)
                taken.add((rr, cc))
        auto = row * cols + col + 1
        r["rect"] = _grid_rect(row, col, rs, cs, rows, cols, width, height)


def _assign_rect(spec, where, width, height):
    """Explicit rect: [x, y, w, h] or {x, y, w/width, h/height}."""
    raw = spec.get("rect")
    if isinstance(raw, (list, tuple)):
        if len(raw) != 4:
            raise ValueError("%s.rect: want [x, y, w, h]" % where)
        x, y, w, h = raw
    elif isinstance(raw, dict):
        x = raw.get("x", 0)
        y = raw.get("y", 0)
        w = raw.get("w", raw.get("width"))
        h = raw.get("h", raw.get("height"))
        if w is None or h is None:
            raise ValueError("%s.rect: w and h are required" % where)
    else:
        raise ValueError("%s.rect: must be an object or [x, y, w, h]" % where)
    return (_parse_dim(x, width, where + ".rect.x", allow_zero=True),
            _parse_dim(y, height, where + ".rect.y", allow_zero=True),
            _parse_dim(w, width, where + ".rect.w"),
            _parse_dim(h, height, where + ".rect.h"))


def _assign_stack(bound, width, height):
    """Simple split: heights stack vertically (full width); widths alone
    stack horizontally (full height). Omitted sizes share evenly."""
    horizontal = (any("width" in r["_spec"] for r in bound)
                  and not any("height" in r["_spec"] for r in bound))
    if horizontal:
        share = width // len(bound)
        cursor = 0
        for k, r in enumerate(bound):
            spec, where = r["_spec"], r["_where"]
            last = (k == len(bound) - 1)
            w = (width - cursor) if last and "width" not in spec else \
                _parse_dim(spec.get("width", share), width, where + ".width")
            r["rect"] = (cursor, 0, w, height)
            cursor += w
    else:
        share = height // len(bound)
        cursor = 0
        for k, r in enumerate(bound):
            spec, where = r["_spec"], r["_where"]
            last = (k == len(bound) - 1)
            h = (height - cursor) if last and "height" not in spec else \
                _parse_dim(spec.get("height", share), height,
                           where + ".height")
            r["rect"] = (0, cursor, width, h)
            cursor += h


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


class DisplayDaemon:
    def __init__(self, policy_path=None, clock=None, feedback_path=None):
        if os.environ.get("DISPLAYD_FAKE_FB") == "1":
            self.fb = HeadlessFramebuffer()
        else:
            self.fb = Framebuffer()
        self.screen = Screen(self.fb)
        self.renderers = load_renderers(RENDERER_DIR)
        self.lock = threading.Lock()
        # Long-poll wakeups: every request_* command queue below
        # notifies this condition, so a GET ?wait= hold on a command
        # endpoint wakes the instant a tap queues (never the next
        # poll tick). Own lock -- never held together with self.lock,
        # so queue and wait cannot deadlock.
        self.cmd_cond = threading.Condition()
        self.cache_lock = threading.Lock()
        self.feeds = FeedStore()
        self.screen.feeds = self.feeds
        self.screen.on_present = self._note_frame
        for name, entry in self.renderers.items():
            if "module" not in entry:
                continue
            for input_name, spec in (entry.get("inputs") or {}).items():
                self.feeds.declare(name, input_name, spec)
        # Retired-view feed namespace (see feed() compat): declared cold
        # so /feed/talon_apps/state health reads cold before the first
        # bridge push, exactly like a real input.
        if talon_apps_module is not None:
            try:
                self.feeds.declare("talon_apps", "state",
                                   talon_apps_module.STATE_SCHEMA)
            except Exception:
                pass
        self.frame_cache = {}   # renderer name -> last composed PIL image
        self.switch_pending = None  # start time of the in-flight switch
        self.last_switch_at = None
        self.last_switch_ms = None  # request -> first presented pixel
        self.last_frame_ms = None   # request -> first freshly drawn frame
        # Static-region composition (ISA D6 option B): an opt-in layer
        # over the single-view core. Each region owns a renderer thread
        # drawing into a RegionScreen plus a cached last-good frame;
        # presents recomposite from that per-region cache, so one region
        # updating never disturbs the others. self.lock serializes every
        # mode change; layout_lock guards the region tables only.
        self.layout_lock = threading.Lock()
        self.layout = None  # list of bound regions or None (single mode)
        self.region_threads = {}  # region name -> thread record
        self.region_frames = {}   # region name -> last-good PIL frame
        self.region_errors = {}   # region name -> "Error: detail"
        self.region_updated = {}  # region name -> timestamp of last frame
        self.layout_started_at = None
        self.layout_pending = None  # start time of the in-flight layout
        self.layout_switch_ms = None  # request -> first composited pixel
        self.layout_gen = 0  # bumps on every mode change; stale presents bail
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
        # Reload scan-relay: one-time tokens -> {sha, commit_url,
        # expires_at}. Guarded by self.lock; pruned on every issue/scan.
        # No tracking beyond the confirm event itself: entries hold only
        # what the redirect needs, and single-scan consumption deletes
        # them outright.
        self.reload_tokens = {}
        self.reload_token = None  # token of the currently showing reload
        # MacBook cursor slot (POST /macbook/mouse, fetched by the
        # Mac-side poller): latest pending {x, y, display_index, ts, id}
        # in Quartz coordinates, or None. Single slot, TTL-expiry -- a
        # tap never fires minutes late. Guarded by self.lock.
        # Playlist rotation: scheduler on top of _start_view, overlay hook
        # for the progress bar. Starts enabled only from persisted config.
        self.playlist = playlist_module.Playlist(self)
        # Shared screen chrome, composed not replaced: the playlist bar,
        # the home button, and the sleep badge draw through one chained
        # overlay so all stay visible at once (a second plain assignment
        # here would silently disable the earlier layers). Each badge
        # reads screen.current_view live and suppresses itself where it
        # is meaningless; see renderers/home_chrome.py and
        # renderers/sleep_chrome.py.
        self.screen.overlay = self.playlist.overlay_image
        if home_chrome_module is not None:
            self.screen.overlay = home_chrome_module.chain_overlays(
                self.screen.overlay,
                home_chrome_module.home_overlay(self.screen))
        if (sleep_chrome_module is not None
                and home_chrome_module is not None):
            # One chain primitive (home's): both badge helpers ship
            # together, and a missing badge must never take the daemon
            # down, so the sleep badge rides only when the chain does.
            self.screen.overlay = home_chrome_module.chain_overlays(
                self.screen.overlay,
                sleep_chrome_module.sleep_overlay(self.screen))
        self.watchdog_stop = threading.Event()
        self.watchdog_thread = None
        self.stop_event = None
        self.thread = None
        self.current = None
        self.current_params = None  # params the showing view was started with
        self.started_at = None
        self.last_error = None
        # Last POST /touch/announce: the region set the running touch
        # service is actually dispatching (None until it announces).
        self.touch_live = None
        # Pre-sleep return target ({"renderer", "params"}), captured
        # by the power-off path and consumed by the power-on path. None
        # means no return pending (never slept, or already restored).
        self.sleep_restore = None
        self.playlist.start()
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

    def _picker_live_params(self, entry, params):
        """Tile-grid default views: no explicit list means the live
        advertised set, so a new view appears with no config edit.
        Explicit lists win. The unified home screen draws every view
        but itself (a self tile would just re-show home). Never raises."""
        params = dict(params or {})
        mod = (entry or {}).get("module")
        name = getattr(mod, "NAME", "")
        if name in ("picker", "unified") and "views" not in params:
            try:
                if name == "unified":
                    params["views"] = mod.live_tile_views(self.renderers)
                else:
                    params["views"] = mod.live_views(self.renderers)
            except Exception:
                pass
        return params

    def _run(self, entry, params, stop):
        try:
            entry["module"].run(
                self.screen, self._picker_live_params(entry, params), stop)
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
            self.current_params = dict(params or {})
            # Single funnel: every navigation voids a pending sleep
            # return, so a later power-on never yanks back a view the
            # operator already replaced (e.g. a manual /show while
            # dark). Only _enter_sleep_view re-arms the slot, after
            # the sleep switch lands.
            self.sleep_restore = None
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
            self.current_params = None
            self.started_at = None
            self.screen.current_view = None
            # Blanking voids a pending sleep return with it (the funnel
            # above only covers _start_view; see layout()/clear_layout).
            self.sleep_restore = None
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
        manual happened since (generation check inside end_transient).
        A reload with no explicit base view (the normal state right
        after a restart) falls back to the clock renderer instead of
        blanking the panel; every other kind keeps the blank return."""
        base, ok = self.policy.end_transient(kind, token)
        if not ok:
            return
        if kind == "reload":
            # The confirmation window is over: the one-time token dies
            # with the view (late scans get 410, never a stale confirm).
            with self.lock:
                if self.reload_token is not None:
                    self.reload_tokens.pop(self.reload_token, None)
                    self.reload_token = None
        try:
            if base is None:
                if kind == "reload":
                    try:
                        self._start_view("clock", {})
                    except (KeyError, ValueError):
                        self._clear_internal()
                else:
                    self._clear_internal()
            else:
                self._start_view(base["renderer"], base["params"])
        except (KeyError, ValueError):
            pass

    def _wake_if_idle(self):
        """Activity arrived while idle-off held the panel dark: power back
        on and repaint. Manual power-off is NOT woken -- the operator owns
        that state; only the watchdog's own power-off auto-wakes.
        Returns True when it woke the panel (the caller then owns the
        sleep-view return -- most callers switch views right after)."""
        if self.policy.idle_off and self.fb.blanked:
            self.fb.power_on()
            self.policy.idle_off = False
            return True
        return False

    def show(self, name, params):
        entry = self.renderers.get(name)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % name)
        # Validate before touching what is on screen: a bad selection is
        # rejected and the current view keeps running undisturbed.
        validate_params(params or {}, entry.get("params") or {})
        self.policy.note_select(name, params)
        self.playlist.on_manual()  # manual choice wins: hold the rotation
        with self.lock:
            self._cancel_transient_timer()
            self._wake_if_idle()
            if self.reload_token is not None:
                # Manual navigation cancels the confirmation window.
                self.reload_tokens.pop(self.reload_token, None)
                self.reload_token = None
        return self._start_view(name, params)

    def clear(self):
        self.policy.note_clear()
        self.playlist.on_manual()
        with self.lock:
            self._cancel_transient_timer()
            self._wake_if_idle()
            if self.reload_token is not None:
                self.reload_tokens.pop(self.reload_token, None)
                self.reload_token = None
        return self._clear_internal()

    def _stop_locked(self):
        """Stop everything drawing: the single view (if any) and every
        layout region (if any). A layout cleared here means late region
        frames are dropped by the generation check in _present_region.
        Callers hold self.lock."""
        if self.stop_event is not None:
            self.stop_event.set()
        self.thread = None
        self.stop_event = None
        for record in self.region_threads.values():
            try:
                record["stop"].set()
            except Exception:
                pass
        self.region_threads = {}
        with self.layout_lock:
            self.layout = None
            self.region_frames = {}
            self.region_errors = {}
            self.region_updated = {}
            self.layout_pending = None
            self.layout_gen += 1

    # ---- static-region composition (ISA D6 option B) --------------------

    def _run_region(self, entry, screen, params, stop, region_name):
        """One region's draw loop. A crash is contained (ISA D7): the
        error is recorded for /state and the region keeps its
        last-good-frame -- other regions never notice."""
        try:
            entry["module"].run(
                screen, self._picker_live_params(entry, params), stop)
        except Exception as err:
            with self.layout_lock:
                if (self.layout is not None and
                        any(r["name"] == region_name for r in self.layout)):
                    self.region_errors[region_name] = "%s: %s" % (
                        type(err).__name__, err)

    def _present_region(self, region_name, img):
        """Cache one region's frame and recomposite the panel from the
        per-region cache. Never holds layout_lock while presenting, so a
        slow framebuffer write can never stall another region's update."""
        try:
            frame = img.copy()
        except Exception as err:
            with self.layout_lock:
                self.region_errors[region_name] = "%s: %s" % (
                    type(err).__name__, err)
            return
        with self.layout_lock:
            if self.layout is None:
                return
            match = [r for r in self.layout if r["name"] == region_name]
            if not match:
                return
            rect = match[0]["rect"]
            gen = self.layout_gen
            try:
                if frame.size != (rect[2], rect[3]):
                    frame = frame.resize((rect[2], rect[3]))
                frame = frame.convert("RGB")
            except Exception as err:
                self.region_errors[region_name] = "%s: %s" % (
                    type(err).__name__, err)
                return
            self.region_frames[region_name] = frame
            self.region_updated[region_name] = time.time()
            # A fresh frame clears the region's error: the renderer is
            # visibly alive again, so /state should say so.
            self.region_errors.pop(region_name, None)
            base = Image.new("RGB", (self.fb.width, self.fb.height),
                               (0, 0, 0))
            for region in self.layout:
                cached = self.region_frames.get(region["name"])
                if cached is None:
                    continue
                try:
                    base.paste(cached, (region["rect"][0],
                                        region["rect"][1]))
                except Exception:
                    continue  # one bad frame never breaks the composite
        with self.screen.present_lock:
            with self.layout_lock:
                if gen != self.layout_gen or self.layout is None:
                    return  # superseded by a mode change; drop it
            try:
                self.fb.present(base)
            except Exception:
                pass
        with self.layout_lock:
            if self.layout_pending is not None and gen == self.layout_gen:
                self.layout_switch_ms = round(
                    (time.time() - self.layout_pending) * 1000, 1)
                self.layout_pending = None

    def _composite_now(self):
        """Present the current per-region cache immediately (black for
        regions that have not drawn yet), so a layout switch paints its
        first pixel without waiting on any renderer."""
        with self.layout_lock:
            if self.layout is None:
                return
            gen = self.layout_gen
            base = Image.new("RGB", (self.fb.width, self.fb.height),
                               (0, 0, 0))
            for region in self.layout:
                cached = self.region_frames.get(region["name"])
                if cached is None:
                    continue
                try:
                    base.paste(cached, (region["rect"][0],
                                        region["rect"][1]))
                except Exception:
                    continue
        with self.screen.present_lock:
            with self.layout_lock:
                if gen != self.layout_gen or self.layout is None:
                    return
            try:
                self.fb.present(base)
            except Exception:
                pass
        with self.layout_lock:
            if self.layout_pending is not None and gen == self.layout_gen:
                self.layout_switch_ms = round(
                    (time.time() - self.layout_pending) * 1000, 1)
                self.layout_pending = None

    def set_layout(self, payload):
        """Activate a static-region layout, replacing the single view.

        Validation (unknown renderer, bad params, bad geometry) happens
        first via parse_layout: a bad layout is rejected and whatever is
        on screen keeps running undisturbed. A bare POST /show exits
        layout mode and returns to single-renderer behaviour."""
        regions = parse_layout(payload, self.fb.width, self.fb.height,
                               self.renderers)
        started = time.time()
        self.policy.note_api()  # mutating POST: activity, but the base
        # view is untouched -- transients cannot interrupt a layout.
        self.playlist.on_manual()  # an explicit layout wins: hold rotation
        with self.lock:
            self._cancel_transient_timer()
            self._wake_if_idle()
            self._stop_locked()
            self.sleep_restore = None  # a layout owns the panel now
            with self.layout_lock:
                self.layout = regions
                self.layout_started_at = started
                self.layout_pending = started
                self.layout_switch_ms = None
                for region in regions:
                    x, y, w, h = region["rect"]
                    self.region_frames[region["name"]] = Image.new(
                        "RGB", (w, h), (0, 0, 0))
            self.current = None
            self.current_params = None
            self.started_at = None
            self.screen.current_view = "layout"
            for region in regions:
                x, y, w, h = region["rect"]
                screen = RegionScreen(self, region["name"], w, h)
                screen.current_view = region["renderer"]
                stop = threading.Event()
                entry = self.renderers[region["renderer"]]
                thread = threading.Thread(
                    target=self._run_region,
                    args=(entry, screen, region["params"], stop,
                          region["name"]),
                    daemon=True)
                self.region_threads[region["name"]] = {
                    "thread": thread, "stop": stop,
                    "renderer": region["renderer"],
                    "params": region["params"], "rect": region["rect"],
                    "screen": screen,
                }
                thread.start()
            with self.cache_lock:
                self.last_switch_at = started
        self._composite_now()
        return self.state()

    def clear_layout(self):
        """Drop the layout and blank the screen (DELETE /layout)."""
        self.policy.note_clear()
        self.playlist.on_manual()
        with self.lock:
            self._cancel_transient_timer()
            self._wake_if_idle()
            self._stop_locked()
            self.current = None
            self.current_params = None
            self.started_at = None
            self.screen.current_view = None
            self.sleep_restore = None  # blank owns the panel now
            with self.cache_lock:
                self.switch_pending = None
        self.screen.clear()
        return self.state()

    def layout_state(self):
        """The layout fragment of /state: None when inactive."""
        with self.layout_lock:
            if self.layout is None:
                return None
            regions = []
            for region in self.layout:
                x, y, w, h = region["rect"]
                regions.append({
                    "name": region["name"],
                    "renderer": region["renderer"],
                    "params": region["params"],
                    "rect": {"x": x, "y": y, "w": w, "h": h},
                    "error": self.region_errors.get(region["name"]),
                    "updated_at": self.region_updated.get(region["name"]),
                })
            return {
                "regions": regions,
                "started_at": self.layout_started_at,
                "first_pixel_ms": self.layout_switch_ms,
            }

    # ---- feeds + policy-driven behaviour -------------------------------

    def feed(self, renderer, input_name, payload):
        """Deliver a validated payload to a view's buffer, then consult
        the policy: a chat event may pull the panel to the chat view.
        Pure cache write first -- never disturbs what is on screen.
        Raises KeyError (unknown renderer/input) or ValueError (schema
        mismatch); both map to HTTP errors without side effects."""
        # Feed compat for the retired talon_apps view: the Mac-side poller
        # still posts /feed/talon_apps/state, and that namespace is owned
        # by the merged macbook feature now (the app list lives in its
        # header). Validated against the helper's schema and stored under
        # the same key every reader already uses -- no Mac-side change.
        if renderer == "talon_apps" and input_name == "state":
            if talon_apps_module is None:
                raise KeyError("unknown renderer: %s" % renderer)
            try:
                pushed = self.feeds.push(renderer, input_name, payload,
                                         talon_apps_module.STATE_SCHEMA)
            except ValueError as exc:
                self.feeds.note_error(renderer, input_name, exc)
                raise
            self.policy.note_feed()
            with self.lock:
                woke = self._wake_if_idle()
            if woke:
                self._exit_sleep_view()
            return {"feed": pushed,
                    "attention": self._maybe_attention(renderer,
                                                       input_name)}
        entry = self.renderers.get(renderer)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % renderer)
        spec = (entry.get("inputs") or {}).get(input_name)
        if spec is None:
            raise KeyError("unknown input: %s.%s" % (renderer, input_name))
        try:
            pushed = self.feeds.push(renderer, input_name, payload, spec)
        except ValueError as exc:
            # Schema mismatch: mark the feed errored (until a later push
            # succeeds) before mapping to the HTTP error. Raising KeyError
            # paths stay untouched -- an unknown feed has no entry to mark.
            self.feeds.note_error(renderer, input_name, exc)
            raise
        self.policy.note_feed()
        with self.lock:
            woke = self._wake_if_idle()
        if woke:
            # The only wake path with no follow-up view switch: land the
            # sleep return now, or the panel lights up on the sleep view.
            self._exit_sleep_view()
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
        # A layout owns the panel region by region: a feed just updates
        # the bound region's buffer (it is already visible), so a
        # full-screen pull would destroy the layout to show what is
        # already on screen. Feeds route; the panel does not switch.
        with self.layout_lock:
            if self.layout is not None:
                return {"switched": False, "reason": "layout-active"}
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
        token, active_kind = self.policy.begin_transient("attention", cfg["return_after"])
        if token is None:
            # A higher-or-equal transient holds the screen: the attention
            # pull is suppressed. The reason names the holder ("notice" in
            # the long-standing case), so the panel state stays legible.
            return {"switched": False, "reason": "%s-active" % active_kind}
        with self.lock:
            self._arm_transient("attention", token, cfg["return_after"])
        try:
            self._start_view(view, {})
        except (KeyError, ValueError):
            return {"switched": False, "reason": "unknown-view"}
        out = {"switched": True, "reason": "pulled", "view": view}
        if active_kind:
            out["superseded"] = active_kind
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

        An explicit operator interrupt: while a static-region layout is
        active the notice takes over the full screen (clearing the
        layout) and the return goes to the base view. Chat-attention
        pulls, in contrast, never interrupt a layout -- feeds just update
        the bound region in place.

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
            if superseded == "reload" and self.reload_token is not None:
                # The notice took the panel: the reload confirmation
                # window (and its one-time token) dies with the view.
                self.reload_tokens.pop(self.reload_token, None)
                self.reload_token = None
        self._start_view("notice", params)
        out = {"view": "notice", "params": params, "return_in": duration}
        if superseded:
            out["superseded"] = superseded
        return out

    def reload(self, sha=None, duration=None, highlights=None):
        """Show the reload confirmation until a tap dismisses it.

        `sha` must be the full 40-character hexadecimal deployed commit
        SHA; the screen shows RELOADED, the SHA, and a QR code whose payload
        is a panel-served one-time relay URL (relay_base_url() + /r/<token>)
        when the panel binds a tailnet address the captain's phone can
        reach -- otherwise the QR falls back to the commit page and the
        response says tap-only plainly. Scanning the relay 302-redirects
        the scanner to the commit page AND confirms the view (early return
        to whatever was showing); a tap while the reload view shows
        confirms the same way via confirm_reload()/POST /reload/confirm.
        Tokens are single-scan with no time expiry: each dies with its
        view (scan/tap confirm, tap-dismiss, manual navigation, a
        superseding notice, or a newer reload all invalidate it). Raises ValueError
        on bad input (HTTP 400) or KeyError when the reload renderer is not
        installed (404).

        Unlike notify(), there is no return timer: the screen stays on the
        reload view indefinitely. A touchscreen tap (POST /touch/tap via
        dismiss_reload()) returns through the existing return path -- the
        saved base view, or the clock after a fresh restart with no base
        view. A manual /show or /clear cancels the transient outright, and
        reload shares notice's top priority level so the newest of the
        two wins. A legacy `duration` field is still validated when
        supplied but never armed: it is accepted and ignored, and the
        response reports "return_in": None (indefinite). `highlights`
        is an optional bounded commit-message summary (subject + a few
        body lines, extracted Mac-side by deploy.sh where git works);
        it is sanitised and capped here, drawn as plain text only, and a
        missing/malformed value renders the classic view unchanged. It
        never reaches the QR payload."""
        if sha is None or (isinstance(sha, str) and not sha.strip()):
            raise ValueError("sha is required: post the full 40-character "
                             "deployed commit SHA")
        if not isinstance(sha, str):
            raise ValueError("sha must be a string, got %s"
                             % type(sha).__name__)
        if len(sha) != 40:
            raise ValueError("sha must be a full 40-character commit SHA, "
                             "got %d characters" % len(sha))
        if not RELOAD_SHA_RE.match(sha):
            raise ValueError("sha must be hexadecimal (0-9, a-f): "
                             "%r is not a commit SHA" % sha)
        sha = sha.lower()
        if duration is not None:
            # Legacy field: validated for compatibility, ignored for
            # expiry -- the reload view never returns on its own.
            try:
                legacy = float(duration)
            except (TypeError, ValueError):
                raise ValueError("duration must be a number of seconds")
            if not (RELOAD_DURATION_MIN <= legacy <= RELOAD_DURATION_MAX):
                raise ValueError("duration must be within [%d, %d]"
                                 % (RELOAD_DURATION_MIN, RELOAD_DURATION_MAX))
        params = {"sha": sha}
        clean_hl = reload_highlights_module.sanitize(highlights)
        if clean_hl:
            # Plain text beside the code: bounded, no control
            # characters, never URL-shaped into the QR (the renderer
            # enforces the same bounds for /show callers). Absent or
            # malformed input leaves params exactly as before.
            params["highlights"] = clean_hl
        commit_url = RELOAD_COMMIT_URL_PREFIX + sha
        entry = self.renderers.get("reload")
        if not entry or "module" not in entry:
            raise KeyError("reload renderer is not installed")
        now = time.time()
        with self.lock:
            self._prune_reload_tokens_locked(now)
            scan_token = secrets.token_urlsafe(16)
            while scan_token in self.reload_tokens:
                scan_token = secrets.token_urlsafe(16)
            self.reload_tokens[scan_token] = {
                "sha": sha, "commit_url": commit_url,
                # No time expiry: the view stays indefinitely, so the
                # token dies with the view (confirm, tap-dismiss, manual
                # nav, superseding notice, or a newer reload invalidate
                # it) rather than with a duration.
                "expires_at": None,
            }
        base, reachable, reason = relay_base_url()
        if reachable:
            relay_url = base + RELOAD_RELAY_PATH + scan_token
            params["relay_url"] = relay_url
        else:
            # Tap-only fallback: loopback (or otherwise unreachable) bind
            # means the phone could never fetch a relay URL, so never put
            # one in the QR. The renderer draws the commit QR plus an
            # explicit tap-to-confirm note; the response says why.
            # Invalidate the token at once so no dead /r/ URL exists.
            with self.lock:
                self.reload_tokens.pop(scan_token, None)
            scan_token = None
            relay_url = None
        validate_params(params, entry.get("params") or {})
        self.policy.note_api()
        token, superseded = self.policy.begin_transient("reload", None)
        with self.lock:
            # Indefinite: cancel any in-flight return timer and arm none.
            # A stale timer holding an older token is then harmless --
            # end_transient's token check rejects it.
            self._cancel_transient_timer()
            self._wake_if_idle()
            if self.reload_token is not None and self.reload_token != scan_token:
                # A replaced window's token dies with it: one live token
                # per showing view, so a stale QR never redirects to an
                # older commit after a newer reload took the panel.
                self.reload_tokens.pop(self.reload_token, None)
            self.reload_token = scan_token
        self._start_view("reload", params)
        out = {"view": "reload", "params": params,
               "commit_url": commit_url,
               "relay_url": relay_url,
               "relay_reachable": reachable,
               "return_in": None}
        if not reachable:
            out["relay_note"] = reason
        if superseded:
            out["superseded"] = superseded
        return out

    def _prune_reload_tokens_locked(self, now=None):
        """Drop expired reload tokens. Callers hold self.lock.

        Tokens with expires_at None never expire by time -- they die
        with the view -- so pruning only touches time-bounded ones."""
        now = time.time() if now is None else now
        dead = [tok for tok, rec in self.reload_tokens.items()
                if rec.get("expires_at") is not None
                and rec.get("expires_at", 0) <= now]
        for tok in dead:
            self.reload_tokens.pop(tok, None)

    def _return_from_base(self, base):
        """Restore the base view after a confirm (no lock held).

        Mirrors _transient_expired's reload branch: with no explicit base
        view the clock resumes instead of a blank panel. Runs outside
        self.lock because _start_view locks internally (it must -- like
        _transient_expired, callers never hold the daemon lock here)."""
        if base is None:
            try:
                return self._start_view("clock", {})
            except (KeyError, ValueError):
                return self._clear_internal()
        try:
            return self._start_view(base["renderer"], base["params"])
        except (KeyError, ValueError):
            pass

    def confirm_reload(self, source="tap", token=None):
        """Confirm the showing reload view: return to the base view early.

        `source` is "tap" (POST /reload/confirm, touch.py reload_confirm)
        or "scan" (GET /r/<token>). Scan confirms only with the live
        one-time token; tap confirms whatever reload is showing. Both are
        view-gated: with no active reload transient the call is a 409-style
        miss ({confirmed: False}), never a view change. Idempotent: the
        second confirm of the same window misses the same way. Returns a
        JSON-safe dict; raises nothing."""
        source = str(source or "tap").lower()
        if source not in ("tap", "scan"):
            return {"confirmed": False, "reason": "unknown source %r"
                    % (source,)}
        with self.lock:
            active = self.policy.active
            if (active is None or active.get("kind") != "reload"):
                return {"confirmed": False,
                        "reason": "no-reload-active"}
            if source == "scan":
                if not token or not isinstance(token, str):
                    return {"confirmed": False,
                            "reason": "token-required"}
                self._prune_reload_tokens_locked()
                rec = self.reload_tokens.get(token)
                if rec is None:
                    return {"confirmed": False,
                            "reason": "unknown-or-expired-token"}
                if token != self.reload_token:
                    return {"confirmed": False,
                            "reason": "stale-token"}
                # Single-scan: consume first, then return the panel.
                self.reload_tokens.pop(token, None)
                self.reload_token = None
            else:
                if self.reload_token is not None:
                    self.reload_tokens.pop(self.reload_token, None)
                    self.reload_token = None
            ok = self.policy.end_transient(
                "reload", active.get("token"))
            if not ok[1]:
                return {"confirmed": False,
                        "reason": "already-confirmed"}
            self._cancel_transient_timer()
            self.policy.note_api()
            base = copy.deepcopy(self.policy.base)
        # Outside self.lock: _start_view locks internally (same shape as
        # _transient_expired -- holding both would deadlock).
        self._return_from_base(base)
        return {"confirmed": True, "via": source}

    def handle_relay_scan(self, token):
        """One scan of GET /r/<token>: consume + redirect + confirm.

        Returns (status, payload): (302, commit_url) on the single valid
        scan -- the handler 302-redirects there and the panel has been
        returned early when it was still showing; (410, {...}) when the
        token was already consumed or expired; (404, {...}) when unknown
        or malformed. The token dies with the view (timeout, manual
        navigation, superseding notice, or a newer reload all invalidate
        it), so a scan after the return gets 410 and confirms nothing."""
        if not token or not isinstance(token, str) \
                or not RELOAD_TOKEN_RE.match(token):
            return 404, {"error": "unknown confirm token"}
        with self.lock:
            self._prune_reload_tokens_locked()
            rec = self.reload_tokens.get(token)
            if rec is None:
                return 410, {"error": "token expired or already scanned: "
                                        "single-scan, valid only while the "
                                        "reload view shows"}
            commit_url = rec["commit_url"]
            live = (self.policy.active is not None
                    and self.policy.active.get("kind") == "reload"
                    and token == self.reload_token)
            # Single-scan: consume before confirming so a double-fetch
            # cannot confirm twice.
            self.reload_tokens.pop(token, None)
            base = None
            confirmed = False
            if live:
                active_token = self.policy.active.get("token")
                self.reload_token = None
                ok = self.policy.end_transient("reload", active_token)
                if ok[1]:
                    self._cancel_transient_timer()
                    self.policy.note_api()
                    base = copy.deepcopy(self.policy.base)
                    confirmed = True
        # Outside self.lock: _start_view locks internally (same shape as
        # _transient_expired -- holding both would deadlock).
        if confirmed:
            self._return_from_base(base)
        return 302, {"commit_url": commit_url,
                     "confirmed": confirmed}

    def reload_confirm_state(self):
        """Pending-confirmation fragment for /state (None when idle)."""
        with self.lock:
            active = self.policy.active
            if active is None or active.get("kind") != "reload":
                return None
            token = self.reload_token
            rec = self.reload_tokens.get(token) if token else None
            now = time.time()
            out = {"pending": True, "via": ["scan", "tap"]}
            if rec is not None and rec.get("expires_at") is not None:
                out["expires_in"] = round(
                    max(0.0, rec["expires_at"] - now), 1)
            else:
                # Indefinite window (or tap-only fallback with no token):
                # no countdown, the view waits for a scan or a tap.
                out["expires_in"] = None
                if rec is None:
                    out["tap_only"] = True
            return out

    # ---- delivery stamp ----------------------------------------------------

    def deploy_info(self):
        """Last delivery stamp (see read_deploy_stamp). Read-only: the
        file is written host-side by deploy.sh, never through the API."""
        return read_deploy_stamp()

    # Freshness window for the touch-service heartbeat (POST
    # /touch/announce): a heartbeat older than this makes
    # touch_check() report blind ("stale"), never a pass. The touch
    # service re-announces every ANNOUNCE_INTERVAL_SECONDS (touch.py),
    # far inside this window, so a healthy gate never ages out; only a
    # dead or wedged announcer goes blind. deploy.sh's post-restart
    # freshness proof (announced_at newer than pre-restart) is
    # unaffected: a restart still announces at startup first.
    TOUCH_HEARTBEAT_MAX_AGE = 1800.0

    def announce_touch(self, body):
        """Touch-service heartbeat (POST /touch/announce): the region
        set the running service is actually dispatching. Best-effort on
        the touch side, validated here. Never touches the policy clock:
        a machine heartbeat is not operator activity."""
        if touch_audit is None:
            raise ValueError("touch audit helper unavailable")
        cleaned = touch_audit.validate_announce(body or {})
        with self.lock:
            self.touch_live = {
                "announced": cleaned,
                "regions_sha": touch_audit.regions_sha(cleaned),
                "announced_at": time.time(),
            }
            sha = self.touch_live["regions_sha"]
            count = (len(cleaned["regions"])
                     + sum(len(v) for v in
                           cleaned["view_regions"].values()))
        return {"ok": True, "regions": count, "regions_sha": sha}

    def _expected_picker_views(self, params, name="picker"):
        """Mirror of _picker_live_params for the check path: explicit
        views win, else the live advertised set (minus self for the
        unified home screen). None only when the module itself is
        unloadable (then not assertable)."""
        mod = (self.renderers.get(name) or {}).get("module")
        if mod is None:
            return None
        params = params if isinstance(params, dict) else {}
        try:
            if "views" in params:
                return mod.coerce_views(params)
            live = getattr(mod, "live_tile_views", None)
            if live is None and hasattr(mod, "live_views"):
                live = mod.live_views
            if live is not None:
                return live(self.renderers)
            return mod.coerce_views(params)
        except Exception:
            return None

    def touch_check(self):
        """GET /touch/check body: DRAWN geometry vs the LIVE announced
        region set, evaluated per view (matrix) plus the showing view in
        detail. ok False is blind -- "unknown" (no heartbeat yet) or
        "stale" (heartbeat older than TOUCH_HEARTBEAT_MAX_AGE) -- or
        "mismatch" (exact moved/missing rects). Blind carries
        blind True (the monitor reporting its OWN blindness: an alarm,
        not a pass); drift carries no blind flag (a block). Undrawn
        live ids are
        reported, never failed. Non-showing views assume default params."""
        if touch_audit is None:
            return {"ok": False, "status": "error",
                    "error": "touch audit helper unavailable"}
        live = self.touch_live
        if live is None:
            return {"ok": False, "status": "unknown", "blind": True,
                    "current_view": self.current,
                    "error": "no touch heartbeat: restart displayd-touch "
                    "(it announces at startup and re-announces every "
                    "5 minutes) or run touch.py --announce",
                    "coverage": touch_audit.COVERAGE}
        try:
            age = time.time() - float(live.get("announced_at") or 0)
        except (TypeError, ValueError):
            age = float("inf")
        if age > self.TOUCH_HEARTBEAT_MAX_AGE:
            return {"ok": False, "status": "stale", "blind": True,
                    "current_view": self.current,
                    "error": ("touch heartbeat expired (%.0fs old, "
                                "max %.0fs): the touch service stopped "
                                "re-announcing; restart displayd-touch "
                                "or run touch.py --announce" %
                                (age, self.TOUCH_HEARTBEAT_MAX_AGE)),
                    "age_seconds": round(age, 1),
                    "max_age_seconds": self.TOUCH_HEARTBEAT_MAX_AGE,
                    "announced_at": live.get("announced_at"),
                    "regions_sha": live.get("regions_sha"),
                    "coverage": touch_audit.COVERAGE}
        announced = live.get("announced") or {}
        scoped = announced.get("view_regions") or {}
        global_regions = announced.get("regions") or []
        w, h = self.screen.W, self.screen.H
        views = sorted(name for name, entry in self.renderers.items()
                       if isinstance(entry, dict) and "module" in entry)
        results = {}
        for name in views:
            if name in ("picker", "unified"):
                params = (self.current_params
                          if name == self.current else {})
                picker_views = self._expected_picker_views(params, name)
                if picker_views is None:
                    results[name] = {"ok": True, "checkable": False,
                                     "reason": "%s unloadable" % name}
                    continue
            else:
                params, picker_views = (self.current_params
                                        if name == self.current else {}), None
            expected = touch_audit.expected_for_view(
                name, params, w, h, picker_views=picker_views)
            if not expected["checkable"]:
                results[name] = {"ok": True, "checkable": False,
                                 "reason": expected["reason"]}
                continue
            live_norm = touch_audit.normalize_live(
                touch_audit.candidates(global_regions, scoped, name))
            compared = touch_audit.compare_exact(
                expected["exact"], live_norm,
                force_strict=name in scoped)
            presence = touch_audit.compare_presence(expected["presence"],
                                                    live_norm)
            results[name] = {
                "ok": compared["ok"] and presence["ok"],
                "reason": expected["reason"],
                "missing": compared["missing"],
                "moved": compared["moved"],
                "unwired": compared["unwired"],
                "presence_missing": presence["missing"],
                "unasserted": compared["unasserted"]}
        file_info = {"present": False}
        try:
            with open(TOUCH_JSON_PATH, "r", encoding="utf-8") as fh:
                doc = json.load(fh) or {}
            file_sha = touch_audit.regions_sha(doc)
            file_info = {"present": True,
                         "regions_sha": file_sha,
                         "matches_live": file_sha == live["regions_sha"]}
        except Exception as exc:
            file_info = {"present": False, "error": str(exc)}
        current = None
        if self.current is not None and self.current in results:
            current = {"view": self.current,
                       "params": self.current_params,
                       **results[self.current]}
            if self.current in ("picker", "unified"):
                current["expected"] = touch_audit.expected_for_view(
                    self.current, self.current_params, w, h,
                    picker_views=self._expected_picker_views(
                        self.current_params, self.current))["exact"]
        ok = all(r.get("ok", True) for r in results.values())
        report = {"ok": ok, "status": "ok" if ok else "mismatch",
                  "current_view": self.current,
                  "display": [w, h], "current": current,
                  "views": results,
                  "unknown_views": sorted(set(scoped) - set(views)),
                  "announced_at": live["announced_at"],
                  "regions_sha": live["regions_sha"],
                  "touch_json": file_info,
                  "coverage": touch_audit.COVERAGE}
        if self.layout_state():
            report["note"] = ("layout active: matrix still evaluates "
                                "single-view wiring; no per-view taps "
                                "asserted while the layout owns the panel")
        return report

    # Tap-positioned actions (touch.py COORD_ACTIONS): the region names
    # the action, the tap point positions it. Resolve stamps the point
    # like the touch path, but feed/map revalidation stays at dispatch
    # (request_mouse/click/focus_move) -- out of scope for this step.
    RESOLVE_COORD_ACTIONS = ("talon_focus", "macbook_mouse",
                               "macbook_click")

    def resolve_touch(self, x, y):
        """Read-only tap resolution against the CURRENT UI.

        Takes display-pixel ints, topmost-hit-tests them against the
        announced live set (touch_audit.candidates: view-specific first,
        then global -- the same order the touch service dispatches), and
        returns what the tap WOULD do: the resolved region id and the
        semantic action. Dispatches nothing, queues nothing, notes no
        activity, changes no state: a question, not a command.

        The refusal gates mirror the real path's predicates exactly:
        unknown (no heartbeat) answers ok False like touch_check; a tap
        while a layout owns the panel sees global regions only (layout
        clears current, so the touch service never guesses a scope);
        view+mode-gated actions (macbook_mouse only in GLANCE,
        macbook_click only in AIM, talon_focus/talon_tab only in GLANCE,
        macbook_mode only while macbook shows, reload_confirm only while
        a reload transient is active) report refused with the same reason
        strings the dispatching handlers use, EXCEPT the macbook mode
        gates: a region whose action the showing mode refuses is not live
        (AIM never draws the map or the strip), so the tap falls through
        to the next region exactly like the touch dispatcher
        (touch_audit.candidates_for_mode) -- the second tap reaches the
        click catcher instead of reporting the map refused; a dead-zone tap while a
        reload transient is active reports consumed_by reload-dismiss
        (dismissal-consumes-tap precedence: the real tap would return
        the panel, never navigate). Raises ValueError on malformed or
        off-panel coordinates (HTTP 400)."""
        for value in (x, y):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    "resolve coordinates must be integers")
        width, height = self.screen.W, self.screen.H
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(
                "resolve coordinates off-panel: %r,%r "
                "for %dx%d" % (x, y, width, height))
        if touch_audit is None:
            return {"ok": False, "status": "error",
                    "error": "touch audit helper unavailable"}
        live = self.touch_live
        if live is None:
            return {"ok": False, "status": "unknown",
                    "error": "no touch heartbeat: restart displayd-touch "
                    "(it announces at startup) or run touch.py --announce"}
        view = self.current  # None while a layout owns the panel
        layout = self.layout_state() is not None
        announced = live.get("announced") or {}
        scoped = announced.get("view_regions") or {}
        global_regions = announced.get("regions") or []
        found = None
        for region in touch_audit.candidates_for_mode(
                global_regions, scoped, view, self._macbook_mode()):
            rect = region.get("rect")
            if not rect or len(rect) != 4:
                continue
            rx, ry, rw, rh = rect
            if rx <= x < rx + rw and ry <= y < ry + rh:
                found = region
                break
        base = {"ok": True, "view": view, "layout": layout,
                "x": x, "y": y}
        active = getattr(self.policy, "active", None)
        reload_active = (isinstance(active, dict)
                         and active.get("kind") == "reload")
        if found is None:
            base.update({
                "hit": False, "region": None, "action": None,
                "dispatched": False,
                "consumed_by": ("reload-dismiss" if reload_active
                                  else None),
                "fallback": ("touch-service tap_options "
                               "(daemon-side unknown)"),
            })
            return base
        rid = found.get("id")
        action = dict(found.get("action") or {})
        name = action.get("name")
        if name in self.RESOLVE_COORD_ACTIONS:
            # The region names it, the tap positions it (touch.py
            # handle_frame stamps the same way before dispatch).
            action["x"], action["y"] = x, y
        reason = self._resolve_refusal(name, view)
        base.update({"hit": True, "region": rid, "action": action,
                     "dispatched": False})
        if reason is not None:
            base.update({"refused": True, "reason": reason})
        else:
            base.update({"refused": False})
            if name in self.RESOLVE_COORD_ACTIONS:
                base["revalidation"] = (
                    "daemon feed/map revalidation still applies at "
                    "dispatch (request_mouse/click/focus_move)")
        return base

    def _resolve_refusal(self, name, view):
        """View/mode-gate predicate for one action name, read-only.

        Returns the refusal reason the dispatching handler would use,
        or None when no view/mode gate refuses. Reason strings match
        the handlers so diffs compare equal. Feed/freshness/map gates
        are NOT evaluated here (tap-positioned daemon revalidation)."""
        showing = "(showing %r)" % (view,)
        if name == "reload_confirm":
            active = getattr(self.policy, "active", None)
            if not (isinstance(active, dict)
                    and active.get("kind") == "reload"):
                return "no-reload-active"
            return None
        if name == "macbook_mouse":
            if view != "macbook":
                return "macbook view not showing " + showing
            if self._macbook_mode() != "glance":
                return ("map lives in GLANCE mode "
                        "(open AIM to review, not to position)")
            return None
        if name == "macbook_click":
            if view != "macbook":
                return "macbook view not showing " + showing
            if self._macbook_mode() != "aim":
                return ("review image lives in AIM mode "
                        "(position in GLANCE first)")
            return None
        if name in ("talon_focus", "talon_tab"):
            if view != "macbook":
                return "macbook view not showing " + showing
            if self._macbook_mode() != "glance":
                return "app strip lives in GLANCE mode"
            return None
        if name == "macbook_mode":
            if view != "macbook":
                return "macbook view not showing " + showing
            return None
        return None

    def dismiss_reload(self):
        """Dismiss an active reload transient after a touchscreen tap.

        Takes the same return path as the old expiry timer: the saved base
        view resumes, or the clock after a fresh restart with no base
        view. Dismissing anything but an active reload -- a notice, an
        attention pull, a plain view, or nothing at all -- is a harmless
        no-op reporting dismissed False, so repeated taps are safe. A tap
        is operator presence, so a successful dismissal notes API activity."""
        base, ok = self.policy.dismiss_transient("reload")
        if not ok:
            return {"dismissed": False, "view": self.current}
        self.policy.note_api()
        with self.lock:
            self._cancel_transient_timer()
            self._wake_if_idle()
            if self.reload_token is not None:
                # The window is over: its one-time token dies with the
                # view (late scans get 410, never a stale confirm).
                self.reload_tokens.pop(self.reload_token, None)
                self.reload_token = None
        try:
            if base is None:
                try:
                    self._start_view("clock", {})
                except (KeyError, ValueError):
                    self._clear_internal()
            else:
                self._start_view(base["renderer"], base["params"])
        except (KeyError, ValueError):
            pass
        return {"dismissed": True, "view": self.current}

    # ---- MacBook cursor (panel tap -> Mac mouse) ------------------------
    #
    # A tap while the macbook map view shows moves the MacBook cursor to
    # the tapped point AND enters the fullscreen AIM review of that zone
    # in the same gesture: touch.py posts panel pixels (closed named
    # action `macbook_mouse`); the daemon maps them through the same pure
    # geometry the renderer draws (macbook_map.frame/locate), holds one
    # pending Quartz command the Mac-side poller fetches each tick and
    # warps to directly (Quartz CGWarpMouseCursorPosition -- Talon
    # follows the OS cursor, verified 2026-09-28, so no Talon channel
    # is needed and none is depended on), then re-shows the merged view
    # in AIM mode (tab preserved). The tap is the only entry to the
    # zoom: the retired AIM button is gone, so positioning and review
    # are one gesture, not two. Single fullscreen-map view only: in
    # layout mode the map owns a sub-rect the frame math does not know,
    # so layout taps are refused rather than mis-mapped.
    #
    # Failure is always a refusal, never a half-move: the warp is one
    # atomic OS call on the Mac, so a lost race lands the full point or
    # nothing. Stale commands TTL-expire instead of firing late.
    MOUSE_TTL = 10.0  # pending commands older than this never run
    MOUSE_FRESH = 5.0  # macbook feed must be this fresh to map against

    def _macbook_map_state(self):
        """Latest macbook feed state, or None when absent/stale."""
        try:
            values = self.feeds.get("macbook", "state")
        except Exception:
            return None
        state = values[-1] if values else None
        if not isinstance(state, dict):
            return None
        try:
            age = time.time() - float(state.get("ts", 0))
        except (TypeError, ValueError):
            return None
        if age < 0 or age > self.MOUSE_FRESH:
            return None
        return state

    def request_mouse_move(self, px, py):
        """Queue a cursor move for panel pixel (px, py) and enter AIM.

        Returns {"ok": True, "command": {...}, "mode": "aim",
        "tab": ...} on success -- the warp is queued AND the showing
        view is re-pinned to the fullscreen AIM review (tab preserved),
        so the tap lands the user in the zoom, not back on the glance
        screen. Returns {"ok": False, "reason": ...} on any refusal
        (wrong view, stale feed, tap outside the display map); a
        refusal queues nothing and changes no mode. Raises ValueError
        only for malformed coordinates (non-int or off-panel)."""
        for value in (px, py):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    "macbook mouse coordinates must be integers")
        width, height = self.screen.W, self.screen.H
        if not (0 <= px < width and 0 <= py < height):
            raise ValueError(
                "macbook mouse coordinates off-panel: %r,%r "
                "for %dx%d" % (px, py, width, height))
        if self.current != "macbook":
            return {"ok": False,
                    "reason": "macbook view not showing "
                    "(showing %r)" % (self.current,)}
        if self._macbook_mode() != "glance":
            return {"ok": False,
                    "reason": "map lives in GLANCE mode "
                    "(open AIM to review, not to position)"}
        try:
            macbook_map = macbook_map_module
            if macbook_map is None:
                raise ImportError("macbook_map helper failed to load")
            layout = macbook_layout_module
            if layout is None:
                raise ImportError("macbook_layout helper failed to load")
        except Exception as exc:
            return {"ok": False,
                    "reason": "map geometry unavailable: %s" % (exc,)}
        state = self._macbook_map_state()
        if state is None:
            return {"ok": False,
                    "reason": "no fresh macbook feed "
                    "(poller quiet >%ds?)" % (self.MOUSE_FRESH,)}
        hit = macbook_map.locate(px, py, state.get("displays") or [],
                                 width, height,
                                 top=layout.header_bottom(),
                                 bottom=height)
        if hit is None:
            return {"ok": False,
                    "reason": "tap outside the display map"}
        command = {"x": float(hit["x"]), "y": float(hit["y"]),
                   "display_index": int(hit["display_index"]),
                   "ts": time.time()}
        with self.lock:
            self.mouse_seq = getattr(self, "mouse_seq", 0) + 1
            command["id"] = self.mouse_seq
            self.mouse_pending = command
        with self.cmd_cond:
            self.cmd_cond.notify_all()
        # The tap carries the user into the zoom: re-show the merged
        # feature in AIM mode (manual navigation, so rotation holds
        # while the review is up). Only on success -- a refusal above
        # returns before this line, leaving view and mode untouched.
        params = dict(self.current_params or {})
        params["mode"] = "aim"
        # A mode switch is not a page turn: drop any stale slide hint
        # so the fresh thread draws steady instead of replaying it.
        params.pop("tab_from", None)
        self.show("macbook", params)
        self.policy.note_api()
        return {"ok": True, "command": dict(command),
                "mode": "aim", "tab": params.get("tab", 0)}

    def take_mouse_move(self, since=None):
        """Pending cursor command newer than `since`, else None.

        Read-only: the poller tracks the last ts it acted on, so a
        retried fetch never double-fires and a crashed-then-restarted
        poller skips TTL-expired commands instead of replaying them."""
        try:
            since = float(since) if since is not None else 0.0
        except (TypeError, ValueError):
            since = 0.0
        with self.lock:
            pending = getattr(self, "mouse_pending", None)
            pending = dict(pending) if pending else None
        if pending is None:
            return None
        if pending["ts"] <= since:
            return None
        if time.time() - pending["ts"] > self.MOUSE_TTL:
            return None
        return pending

    # ---- Talon app-focus slot ------------------------------------------
    # Panel tap -> Mac focus, same shape as the macbook-mouse slot
    # (PR #10): POST /talon/focus queues ONE TTL command carrying a
    # panel point; the Mac-side poller (bridges/talon_apps.py) fetches it
    # via GET /talon/focus?since= and hands the app name to Talon. The
    # queued command names the app ONLY from the latest feed -- the HTTP
    # body carries coordinates, never a name -- so a tap can only ever
    # select a listed app, never an arbitrary target. Failures refuse,
    # never half-fire; stale commands TTL-expire instead of firing late.
    FOCUS_TTL = 10.0  # pending commands older than this never run
    FOCUS_FRESH = 5.0  # talon_apps feed must be this fresh to map against

    def _talon_apps_state(self):
        """Latest talon_apps feed state, or None when absent/stale."""
        try:
            values = self.feeds.get("talon_apps", "state")
        except Exception:
            return None
        state = values[-1] if values else None
        if not isinstance(state, dict):
            return None
        try:
            age = time.time() - float(state.get("ts", 0))
        except (TypeError, ValueError):
            return None
        if age < 0 or age > self.FOCUS_FRESH:
            return None
        return state

    def request_focus_move(self, px, py):
        """Queue a focus change for panel pixel (px, py).

        Returns {"ok": True, "command": {...}} on success, or
        {"ok": False, "reason": ...} on any refusal (wrong view or
        mode, stale feed, tap outside the app chips). Raises ValueError
        only for malformed coordinates (non-int or off-panel)."""
        for value in (px, py):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    "talon focus coordinates must be integers")
        width, height = self.screen.W, self.screen.H
        if not (0 <= px < width and 0 <= py < height):
            raise ValueError(
                "talon focus coordinates off-panel: %r,%r "
                "for %dx%d" % (px, py, width, height))
        if self.current != "macbook":
            return {"ok": False,
                    "reason": "macbook view not showing "
                    "(showing %r)" % (self.current,)}
        if self._macbook_mode() != "glance":
            return {"ok": False,
                    "reason": "app strip lives in GLANCE mode"}
        if talon_apps_module is None or macbook_layout_module is None:
            return {"ok": False,
                    "reason": "app-list geometry unavailable"}
        state = self._talon_apps_state()
        if state is None:
            return {"ok": False,
                    "reason": "no fresh talon_apps feed "
                    "(poller quiet >%ds?)" % (self.FOCUS_FRESH,)}
        apps = state.get("apps")
        if not isinstance(apps, list) or not apps:
            return {"ok": False, "reason": "no running apps in feed"}
        tab = self._macbook_tab()
        index = macbook_layout_module.chip_hit(px, py, width, apps, tab)
        if index is None:
            return {"ok": False,
                    "reason": "tap outside the app chips"}
        name = talon_apps_module.clean(apps[index])
        if not name:
            return {"ok": False,
                    "reason": "tap outside the app chips"}
        command = {"app": name, "index": index, "ts": time.time()}
        with self.lock:
            self.focus_seq = getattr(self, "focus_seq", 0) + 1
            command["id"] = self.focus_seq
            self.focus_pending = command
        with self.cmd_cond:
            self.cmd_cond.notify_all()
        self.policy.note_api()
        return {"ok": True, "command": dict(command)}

    def take_focus_move(self, since=None):
        """Pending focus command newer than `since`, else None.

        Read-only: the poller tracks the last ts it acted on, so a
        retried fetch never double-fires and a crashed-then-restarted
        poller skips TTL-expired commands instead of replaying them."""
        try:
            since = float(since) if since is not None else 0.0
        except (TypeError, ValueError):
            since = 0.0
        with self.lock:
            pending = getattr(self, "focus_pending", None)
            pending = dict(pending) if pending else None
        if pending is None:
            return None
        if pending["ts"] <= since:
            return None
        if time.time() - pending["ts"] > self.FOCUS_TTL:
            return None
        return pending

    # ---- Merged-feature navigation (GLANCE/AIM + tab page) --------------
    # The header controls are closed touch actions with static bodies
    # (macbook_mode pins the mode, talon_tab pins the page direction);
    # the daemon applies them to the showing macbook view, preserving the
    # other param, so a mode switch never loses the window start and a
    # tab page never leaves the mode. Both re-show through show() (manual
    # navigation: holds rotation, cancels transients), and both refuse
    # unless the merged feature is showing -- misses are 409, never a
    # view change.

    def _macbook_mode(self):
        """Showing mode: 'aim' or 'glance' (default). Never raises."""
        try:
            if self.current != "macbook":
                return "glance"
            params = self.current_params or {}
            return "aim" if str(params.get("mode") or "").lower() \
                == "aim" else "glance"
        except Exception:
            return "glance"

    def _macbook_tab(self):
        """Showing tab index: int >= 0, default 0. Never raises."""
        try:
            if self.current != "macbook":
                return 0
            return max(0, int((self.current_params or {}).get("tab", 0)))
        except (TypeError, ValueError):
            return 0
        except Exception:
            return 0

    def request_mode_move(self, mode):
        """Re-show the merged feature in `mode`, keeping the tab.

        Returns {"ok": True, "mode", "tab"} or {"ok": False,
        "reason"}. Raises ValueError only for a bad mode name."""
        if self.current != "macbook":
            return {"ok": False,
                    "reason": "macbook view not showing "
                    "(showing %r)" % (self.current,)}
        try:
            want = str(mode or "").lower()
        except Exception:
            want = ""
        if want not in ("glance", "aim"):
            raise ValueError("macbook mode must be glance or aim")
        params = dict(self.current_params or {})
        params["mode"] = want
        # A mode switch is not a page turn: drop any stale slide hint
        # so the fresh thread draws steady instead of replaying it.
        params.pop("tab_from", None)
        self.show("macbook", params)
        return {"ok": True, "mode": want,
                "tab": params.get("tab", 0)}

    def request_tab_step(self, direction):
        """Page the header app strip one window, clamped at both ends.

        One press moves the visible window by PAGE_STRIDE (VISIBLE - 1,
        so the new window overlaps the old by one chip) with a rapid
        slide; a press at either end is refused (ok False) and that
        stepper draws dim. Returns {"ok": True, "tab", "tab_from"}
        or {"ok": False, "reason"}. Raises ValueError only for a
        bad direction."""
        if self.current != "macbook":
            return {"ok": False,
                    "reason": "macbook view not showing "
                    "(showing %r)" % (self.current,)}
        if self._macbook_mode() != "glance":
            return {"ok": False,
                    "reason": "app strip lives in GLANCE mode"}
        if isinstance(direction, bool):
            raise ValueError("tab direction must be +1 or -1")
        try:
            direction = int(direction)
        except (TypeError, ValueError):
            raise ValueError("tab direction must be an integer")
        if abs(direction) != 1:
            raise ValueError("tab direction must be +1 or -1")
        if macbook_layout_module is None:
            return {"ok": False,
                    "reason": "app-list geometry unavailable"}
        state = self._talon_apps_state()
        if state is None:
            return {"ok": False,
                    "reason": "no fresh talon_apps feed "
                    "(poller quiet >%ds?)" % (self.FOCUS_FRESH,)}
        apps = state.get("apps")
        if not isinstance(apps, list) or not apps:
            return {"ok": False, "reason": "no running apps in feed"}
        params = dict(self.current_params or {})
        old = macbook_layout_module.page_start(
            params.get("tab", 0), len(apps))
        new, moved = macbook_layout_module.page(
            old, direction, len(apps))
        if not moved:
            edge = "first" if direction < 0 else "last"
            return {"ok": False,
                    "reason": "already at %s page (%d app%s)" %
                    (edge, len(apps),
                     "" if len(apps) == 1 else "s")}
        params["tab"] = new
        # Slide hint for the fresh renderer thread: it restarts on the
        # new window, so it needs the old one to animate old-to-new.
        params["tab_from"] = old
        self.show("macbook", params)
        return {"ok": True, "tab": new, "tab_from": old}

    # ---- MacBook second-tap click slot -----------------------------------
    # Stage 2 of the two-stage tap (stage 1 = POST /macbook/mouse moves
    # the cursor ONLY, then the Mac posts a magnified /feed/macbook/zoom
    # capture around it). POST /macbook/click queues ONE TTL click at
    # the REVIEWED point; the Mac-side poller fetches it via GET
    # /macbook/click?since= and posts one CG down+up pair. The tap
    # point only proves the tap landed on the review image (pane
    # membership) -- the click target is the capture's own crosshair
    # point, never a re-mapping of the tap, so a tap cannot drift off
    # the reviewed pixel. Commit gates (every miss is a 409 refusal,
    # never a click): fresh state feed, fresh zoom capture (proves the
    # review surface is current), the capture post-dates the
    # positioning tap, and the live cursor still sits on the point (a
    # first tap plus a later second tap never clicks where the mouse
    # has since moved). No arming, no double-click -- each POST queues
    # at most one command and the poller acts once per ts (take_*
    # stays read-only, like the rest).
    CLICK_TTL = 15.0  # pending clicks older than this never run
    CLICK_FRESH = 30.0  # zoom capture must be this fresh to click against
    CLICK_EPS = 8.0  # Quartz-px tolerance: capture vs cursor

    def _macbook_zoom_state(self):
        """Latest macbook zoom capture, or None when absent/stale."""
        try:
            values = self.feeds.get("macbook", "zoom")
        except Exception:
            return None
        zoom = values[-1] if values else None
        if not isinstance(zoom, dict):
            return None
        try:
            age = time.time() - float(zoom.get("ts", 0))
        except (TypeError, ValueError):
            return None
        if age < 0 or age > self.CLICK_FRESH:
            return None
        return zoom

    @staticmethod
    def _near(ax, ay, bx, by, eps):
        try:
            return abs(float(ax) - float(bx)) <= eps and \
                abs(float(ay) - float(by)) <= eps
        except (TypeError, ValueError):
            return False

    def request_click_move(self, px, py):
        """Queue a click for panel pixel (px, py).

        Returns {"ok": True, "command": {...}} or {"ok": False,
        "reason": ...}. Raises ValueError only for malformed
        coordinates (non-int or off-panel)."""
        for value in (px, py):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    "macbook click coordinates must be integers")
        width, height = self.screen.W, self.screen.H
        if not (0 <= px < width and 0 <= py < height):
            raise ValueError(
                "macbook click coordinates off-panel: %r,%r "
                "for %dx%d" % (px, py, width, height))
        if self.current != "macbook":
            return {"ok": False,
                    "reason": "macbook view not showing "
                    "(showing %r)" % (self.current,)}
        if self._macbook_mode() != "aim":
            return {"ok": False,
                    "reason": "review image lives in AIM mode "
                    "(position in GLANCE first)"}
        # AIM claims the full canvas: every on-panel tap is on the review
        # image (the header controls route first via touch order, and
        # direct POSTs already passed the bounds check above), so no
        # sub-pane check remains.
        state = self._macbook_map_state()
        if state is None:
            return {"ok": False,
                    "reason": "no fresh macbook feed "
                    "(poller quiet >%ds?)" % (self.MOUSE_FRESH,)}
        zoom = self._macbook_zoom_state()
        if zoom is None:
            return {"ok": False,
                    "reason": "no fresh review capture "
                    "(position first, then tap the image)"}
        try:
            qx, qy = float(zoom["x"]), float(zoom["y"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False,
                    "reason": "review capture has no point "
                    "(position again)"}
        mouse = state.get("mouse") or {}
        if not self._near(qx, qy, mouse.get("x"),
                           mouse.get("y"), self.CLICK_EPS):
            return {"ok": False,
                    "reason": "cursor moved since positioning "
                    "(position again, then tap the image)"}
        with self.lock:
            pending = getattr(self, "mouse_pending", None)
            pending = dict(pending) if pending else None
        if pending is not None:
            try:
                if float(zoom.get("ts", 0)) < float(pending["ts"]):
                    return {"ok": False,
                            "reason": "review capture predates the "
                            "last positioning (wait for the new image)"}
            except (TypeError, ValueError):
                pass
        try:
            display_index = int((mouse or {}).get("display_index", 0))
        except (TypeError, ValueError):
            display_index = 0
        command = {"x": qx, "y": qy,
                   "display_index": display_index,
                   "ts": time.time()}
        with self.lock:
            self.click_seq = getattr(self, "click_seq", 0) + 1
            command["id"] = self.click_seq
            self.click_pending = command
        with self.cmd_cond:
            self.cmd_cond.notify_all()
        self.policy.note_api()
        return {"ok": True, "command": dict(command)}

    def take_click_move(self, since=None):
        """Pending click command newer than `since`, else None.

        Read-only: the poller tracks the last ts it acted on, so a
        retried fetch never double-fires and a crashed-then-restarted
        poller skips TTL-expired commands instead of replaying them."""
        try:
            since = float(since) if since is not None else 0.0
        except (TypeError, ValueError):
            since = 0.0
        with self.lock:
            pending = getattr(self, "click_pending", None)
            pending = dict(pending) if pending else None
        if pending is None:
            return None
        if pending["ts"] <= since:
            return None
        if time.time() - pending["ts"] > self.CLICK_TTL:
            return None
        return pending

    # ---- command long-poll (tap-latency fast lane) --------------------
    # The three GET command endpoints accept an optional ?wait= (seconds).
    # wait=0 (or absent/garbage) is exactly today's behavior: one take_*
    # sample, immediate reply. wait>0 holds the reply until a tap queues
    # a newer command or the hold expires -- the TTL slot stays the truth
    # (socket-is-a-fast-lane shape from the transport report): an unacked
    # command simply sits for the next poll, so the fallback is the status
    # quo, not a second code path. Bounded by CMD_WAIT_MAX so a turn can
    # never hang; the held GET runs on its own handler thread and never
    # touches the idle clock (observation, like every GET). take_*
    # semantics (read-only, since= idempotency, TTL expiry) are unchanged.
    CMD_WAIT_MAX = 5.0

    def wait_command(self, kind, since=None, wait=0.0):
        """take_* with an optional bounded hold. `kind` is one of
        "mouse" / "focus" / "click". Returns the pending command
        newer than `since`, or None when idle at hold expiry -- the same
        shape as take_*, so callers and the wire format never change."""
        takes = {"mouse": self.take_mouse_move,
                 "focus": self.take_focus_move,
                 "click": self.take_click_move}
        take = takes[kind]  # internal callers only; unknown kind raises
        try:
            wait = float(wait) if wait is not None else 0.0
        except (TypeError, ValueError):
            wait = 0.0
        if not wait > 0:  # covers zero, negatives, and NaN alike
            wait = 0.0
        wait = min(wait, self.CMD_WAIT_MAX)
        cmd = take(since)
        if cmd is not None or wait <= 0:
            return cmd
        deadline = time.monotonic() + wait
        with self.cmd_cond:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cmd_cond.wait(timeout=remaining)
                cmd = take(since)  # re-sample: wakes can be spurious
                if cmd is not None:
                    return cmd
        return take(since)

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
        self.playlist.config_updated(patch or {})  # saving playlist = run
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
        # Same sleep view as the manual path (the wake target must work
        # identically), but the rotation keeps running: idle-off is not
        # a manual choice, so it must not hold the playlist.
        self._enter_sleep_view()
        with self.lock:
            if self.fb.blanked:
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

    def _enter_sleep_view(self):
        """Switch to the dedicated sleep view, remembering the return.

        The return target is the policy base (the pre-transient view),
        falling back to the current view only when no base exists -- a
        transient itself (reload/notice) is never restored, so waking
        after its window still lands somewhere sane. No-op when already
        there, when the sleep renderer is missing, or while a layout
        owns the panel (the layout survives the nap untouched). The
        power calls below do the actual darkening; this only arms the
        touch service's wake signal (renderer == sleep)."""
        if self.layout_state():
            return False
        with self.lock:
            if self.current == SLEEP_VIEW:
                return True
            if ("module" not in
                    (self.renderers.get(SLEEP_VIEW) or {})):
                return False
            base = copy.deepcopy(self.policy.base)
            if base is None and self.current is not None:
                base = {"renderer": self.current,
                        "params": copy.deepcopy(
                            self.current_params or {})}
            if (base is not None
                    and base.get("renderer") == SLEEP_VIEW):
                base = None
        try:
            self._start_view(SLEEP_VIEW, {})
        except (KeyError, ValueError):
            return False
        with self.lock:
            # Re-arm only if the switch actually landed: a concurrent
            # navigation in between owns the panel instead.
            if self.current == SLEEP_VIEW:
                self.sleep_restore = base
        return True

    def _exit_sleep_view(self):
        """Restore the pre-sleep view after power-on. One-shot: the
        slot is consumed whether or not the restore lands (an uninstalled
        renderer must not wedge every later wake). Only acts when the
        sleep view is actually showing -- a plain power-on never yanks
        the current view. With no return pending (slept from a blank
        panel, or a manual demo), falls back to the clock, mirroring
        the transient-expiry fallback: waking must always land somewhere
        navigable, never on a touch-sticky lit sleep view. Callers paint
        this while still dark so the first photons are the base view."""
        with self.lock:
            restore = self.sleep_restore
            self.sleep_restore = None
            waking = (self.current == SLEEP_VIEW)
        if not waking:
            return False
        try:
            if restore:
                self._start_view(restore["renderer"],
                                 restore.get("params") or {})
            else:
                self._start_view("clock", {})
        except (KeyError, ValueError):
            return False
        return True

    def set_power(self, power):
        power = (power or "").lower()
        if power not in ("on", "off"):
            raise ValueError("power must be 'on' or 'off'")
        self.policy.note_api()
        if power == "off":
            # Sleep is a manual choice like /show: hold the rotation so
            # no invisible frames advance it mid-nap, then switch to the
            # sleep view (arming the touch wake target) before darkening.
            self.playlist.on_manual()
            self._enter_sleep_view()
            with self.lock:
                # A manual power call means the operator owns the power
                # state: it clears the watchdog's idle_off claim.
                self.policy.idle_off = False
                result = self.fb.power_off()
        else:
            # Restore first (still dark), then light up: power_on
            # repaints the last frame, which is the base view again.
            self._exit_sleep_view()
            with self.lock:
                self.policy.idle_off = False
                result = self.fb.power_on()
        state = self.state()
        state["applied"] = result
        return state

    # ---- state ---------------------------------------------------------

    def state(self):
        return {
            "version": APP_VERSION,
            "renderer": self.current,
            "params": self.current_params,
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
            "playlist": self.playlist.status(),
            # Static-region composition (opt-in): None in single-view
            # mode, otherwise per-region binding + health. renderer stays
            # None while a layout owns the panel.
            "layout": self.layout_state(),
            # Delivery stamp (deploy.sh host file): date + SHA of the
            # running build, or {"deployed": False} when never recorded.
            "deploy": self.deploy_info(),
            # Reload scan-confirm: pending window + expiry while the
            # reload view shows, else None. No history is kept.
            "reload_confirm": self.reload_confirm_state(),
            # The optional html view's runtime: is the engine installed and
            # loadable from this process, and which templates can it read.
            # None when the renderer module failed to load at all.
            "html": self.html_runtime(),
        }

    def html_runtime(self):
        """The html view's runtime status, or None when it is not loadable.

        Asked of the renderer module rather than recomputed here, so /state
        reports the paths that renderer really resolves instead of a second
        copy of the same rules. Total by construction: a status probe that
        can raise is not a status, and /state must never fail on one.
        """
        entry = self.renderers.get("html") or {}
        module = entry.get("module")
        probe = getattr(module, "runtime_status", None)
        if probe is None:
            return None
        try:
            return probe()
        except Exception as err:  # a probe, not a dependency
            return {"ok": False, "error": str(err)}

    def renderer_list(self):
        out = []
        for name, entry in sorted(self.renderers.items()):
            if "module" not in entry:
                out.append({"name": name, "broken": entry["broken"]})
                continue
            accent = playlist_module.accent_for(entry)
            out.append(
                {
                    "name": name,
                    "description": entry["description"],
                    "params": entry["params"],
                    "inputs": entry.get("inputs", {}),
                    "static": entry["static"],
                    "accent": "#%02x%02x%02x" % accent,
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
  * { box-sizing: border-box; }
  body { background: #111; color: #eee; font-family: system-ui, sans-serif;
         max-width: 520px; margin: 0 auto; padding: 0 12px 32px; }
  h1 { font-size: 1.2em; margin: 12px 0 4px; }
  h2 { font-size: 1.05em; margin: 20px 0 8px; border-bottom: 1px solid #333;
       padding-bottom: 4px; }
  #topbar { position: sticky; top: 0; z-index: 10; background: #161616;
            border-bottom: 1px solid #333; margin: 0 -12px; padding: 8px 12px;
            font-size: 0.85em; display: flex; gap: 10px; align-items: center;
            flex-wrap: wrap; }
  #topbar .now { font-weight: bold; }
  #topbar .dep { color: #aaa; }
  .card { background: #1c1c1c; border: 1px solid #333; border-radius: 8px;
          padding: 12px; margin-bottom: 12px; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
         background: #666; margin-right: 6px; vertical-align: baseline; }
  .dot.ok { background: #3d3; } .dot.bad { background: #f44; }
  #preview { width: 100%; aspect-ratio: 16/9; background: #000; object-fit: contain;
             border: 1px solid #333; border-radius: 8px; }
  /* one-tap view grid: two fat thumb columns */
  #viewgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  #viewgrid button { min-height: 56px; font-size: 1rem; margin: 0;
                     background: #2b2b2b; color: #eee; border: 2px solid #444;
                     border-radius: 10px; font-weight: bold; cursor: pointer;
                     overflow: hidden; text-overflow: ellipsis; }
  #viewgrid button:active { background: #3a3a3a; }
  #viewgrid button.active { border-color: #2a5; background: #17351f;
                            box-shadow: 0 0 0 1px #2a5; }
  #viewgrid button:disabled { opacity: 0.45; }
  .btnrow { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px;
            margin-top: 10px; }
  .btnrow.two { grid-template-columns: 1fr 1fr; }
  .btnrow.one { grid-template-columns: 1fr; }
  button.big { min-height: 52px; font-size: 1rem; margin: 0; cursor: pointer;
               background: #2a5; color: #061; border: 0; border-radius: 10px;
               font-weight: bold; }
  button.big.ghost { background: #333; color: #eee; }
  button.big.warn { background: #a53; color: #fff; }
  button.big:active { filter: brightness(1.2); }
  button { min-height: 48px; padding: 8px 14px; margin: 8px 8px 0 0;
           cursor: pointer; background: #2a5; color: #061; border: 0;
           border-radius: 8px; font-weight: bold; font-size: 0.95rem; }
  button.warn { background: #a53; color: #fff; }
  button.ghost { background: #333; color: #eee; }
  #result { margin-top: 12px; min-height: 1.4em; font-size: 0.9em; color: #9cf; }
  .meta { color: #aaa; font-size: 0.9em; }
  .kv { display: flex; justify-content: space-between; gap: 8px;
        padding: 2px 0; font-size: 0.92em; }
  .kv > span:first-child { color: #aaa; }
  code { background: #222; padding: 1px 5px; border-radius: 3px;
         word-break: break-all; }
  label { display: block; margin: 8px 0 2px; font-size: 0.9em; }
  label .req { color: #f88; }
  label .help { color: #999; font-size: 0.85em; display: block; }
  input[type=text], input[type=number], select {
    width: 100%; box-sizing: border-box; padding: 10px 8px; font-size: 1rem;
    background: #222; color: #eee; border: 1px solid #444; border-radius: 8px; }
  details { margin-top: 10px; }
  details > summary { min-height: 48px; display: flex; align-items: center;
                      cursor: pointer; color: #9cf; font-size: 0.95em; }
  /* tap-to-rate stars */
  #ratebtns { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px;
              margin-top: 8px; }
  #ratebtns button { min-height: 56px; font-size: 1.3rem; margin: 0;
                     background: #2b2b2b; color: #eee; border: 2px solid #444;
                     border-radius: 10px; cursor: pointer; }
  #ratebtns button.picked { border-color: #fc3; background: #3a2f10; }
  .taplist { list-style: none; margin: 0; padding: 0; font-size: 0.9em; }
  .taplist li { padding: 6px 0; border-bottom: 1px solid #2a2a2a; }
  .taplist li:last-child { border-bottom: 0; }
  .taplist .eff { color: #aaa; }
  #fb-summary .fbline { display: flex; justify-content: space-between;
                        padding: 3px 0; font-size: 0.92em; }
  #reload-result a { color: #9cf; word-break: break-all; }
</style>
</head>
<body>
<header id="topbar">
  <span><span id="health" class="dot"></span><span id="healthtext">connecting&hellip;</span></span>
  <span class="now">Now: <span id="tb-view">&ndash;</span></span>
  <span class="dep">Deploy: <span id="tb-dep">&ndash;</span></span>
</header>
<h1>displayd control</h1>

<h2>Views &mdash; one tap to show</h2>
<div class="card">
  <div id="viewgrid" aria-label="all views, one tap each"></div>
  <div class="btnrow one">
    <button id="clear" class="big ghost">Blank screen</button>
  </div>
  <details>
    <summary>Show with parameters (for views that need options)</summary>
    <label for="renderer">Renderer</label>
    <select id="renderer"></select>
    <div id="rdesc" class="meta"></div>
    <div id="params"></div>
    <button id="show">Show with options</button>
  </details>
</div>

<h2>Playback &mdash; playlist rotation</h2>
<div class="card">
  <div class="meta" id="pl-status">playlist: loading&hellip;</div>
  <div class="btnrow">
    <button id="plpause" class="big ghost">Pause</button>
    <button id="plresume" class="big">Resume</button>
    <button id="plnext" class="big ghost">Next view</button>
  </div>
  <details>
    <summary>Playlist setup (views, bar, timing)</summary>
    <label><input type="checkbox" id="pl-en" style="width:auto"> Playlist enabled (rotates through the views below)</label>
    <label for="pl-place">Bar placement<span class="help">which edge the progress bar sits on (string: top/left/bottom/right)</span></label>
    <select id="pl-place"><option>top</option><option>left</option><option>bottom</option><option>right</option></select>
    <label for="pl-thick">Bar thickness (px)<span class="help">readable at distance without stealing content (number, 2-64)</span></label>
    <input type="number" id="pl-thick" min="2" max="64">
    <label for="pl-dir">Bar direction<span class="help">fill grows empty-to-full, drain shrinks full-to-empty (string)</span></label>
    <select id="pl-dir"><option>fill</option><option>drain</option></select>
    <label for="pl-color">Default bar colour<span class="help">a per-view color or a renderer accent wins over this (string, #rrggbb)</span></label>
    <input type="text" id="pl-color">
    <label for="pl-views">Views (JSON list)<span class="help">each {"renderer": name, "params"?: {}, "dwell"?: seconds 3-3600, "color"?: override}; a manual Show pauses rotation until resumed</span></label>
    <input type="text" id="pl-views">
    <button id="plsave">Save playlist</button>
  </details>
</div>

<h2>Proof &mdash; reload &amp; deploy stamp</h2>
<div class="card">
  <div class="kv"><span>Last deploy</span><span id="dep-when">&ndash;</span></div>
  <div class="kv"><span>SHA</span><code id="dep-sha">&ndash;</code></div>
  <div class="kv"><span>By</span><span id="dep-who">&ndash;</span></div>
  <label for="rl-sha">Reload SHA (full 40-char commit)<span class="help">shows a RELOADED screen with the SHA plus a QR code to the commit page, then returns</span></label>
  <input type="text" id="rl-sha" placeholder="40 hex characters" autocapitalize="off" spellcheck="false">
  <div class="btnrow one">
    <button id="reload" class="big">Reload with proof</button>
  </div>
  <div class="meta" id="reload-result"></div>
</div>

<h2>Feedback &mdash; rate this view</h2>
<div class="card">
  <label for="fb-view">View</label>
  <select id="fb-view"></select>
  <div id="ratebtns" aria-label="rating 1 to 5">
    <button data-rating="1">1</button>
    <button data-rating="2">2</button>
    <button data-rating="3">3</button>
    <button data-rating="4">4</button>
    <button data-rating="5">5</button>
  </div>
  <label for="fb-notes">Notes (optional)</label>
  <input type="text" id="fb-notes" placeholder="readable at distance? colours? layout?">
  <div class="btnrow one">
    <button id="fbsend" class="big">Send rating</button>
  </div>
  <div id="fb-summary" style="margin-top:10px"></div>
</div>

<h2>Now showing</h2>
<div class="card">
  <div class="kv"><span>Renderer</span><code id="cur-renderer">&ndash;</code></div>
  <div class="kv"><span>On screen for</span><span id="cur-age">&ndash;</span></div>
  <div class="kv"><span>Power</span><code id="cur-power">&ndash;</code></div>
  <div class="kv"><span>Backlight</span><span id="cur-bl">&ndash;</span></div>
  <div class="kv"><span>Framebuffer blank</span><code id="cur-blank">&ndash;</code></div>
  <div class="kv"><span>Feeds</span><span id="cur-feeds">&ndash;</span></div>
  <div class="kv"><span>Last switch</span><span id="cur-switch">&ndash;</span></div>
  <div class="kv"><span>Last error</span><span id="cur-err">none</span></div>
  <div class="btnrow two">
    <button id="pon" class="big">Turn on</button>
    <button id="poff" class="big warn">Turn off</button>
  </div>
  <span class="meta">Off darkens the backlight and blanks the framebuffer;
  on restores both and repaints the last frame. Screen power controls.</span>
</div>

<div class="card">
  <div class="meta">Live preview of the panel</div>
  <img id="preview" alt="live preview of the panel">
</div>

<details class="card">
  <summary>Notify (interrupt with a notice)</summary>
  <label for="nt-title">Title<span class="req"> *</span></label>
  <input type="text" id="nt-title">
  <label for="nt-body">Body</label>
  <input type="text" id="nt-body">
  <label for="nt-sev">Severity</label>
  <select id="nt-sev"><option>info</option><option>warn</option><option>critical</option></select>
  <label for="nt-dur">Duration (seconds, blank for policy default)</label>
  <input type="number" id="nt-dur" min="1" max="300">
  <button id="notify">Show notice</button>
  <span class="meta">Interrupts what is showing, then returns.</span>
</details>

<details class="card">
  <summary>Policy (what the screen does on its own)</summary>
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
</details>

<h2>Tap actions</h2>
<div class="card">
  <ul class="taplist">
    <li><code>playlist_next</code> <span class="eff">&mdash; advance playlist rotation (POST /playlist/next)</span></li>
    <li><code>playlist_pause</code> <span class="eff">&mdash; hold playlist rotation (POST /playlist/pause)</span></li>
    <li><code>playlist_resume</code> <span class="eff">&mdash; resume playlist rotation (POST /playlist/resume)</span></li>
    <li><code>screen_on</code> <span class="eff">&mdash; drive panel backlight on (POST /screen/on)</span></li>
    <li><code>screen_off</code> <span class="eff">&mdash; drive panel backlight off (POST /screen/off)</span></li>
    <li><code>clear</code> <span class="eff">&mdash; blank the panel (POST /clear)</span></li>
    <li><code>show</code> <span class="eff">&mdash; replace the shown view, renderer named in config (POST /show)</span></li>
    <li><code>select_view</code> <span class="eff">&mdash; reroute the displayed view to the named selection (POST /show)</span></li>
    <li><code>notify</code> <span class="eff">&mdash; interrupt the panel with a transient notice (POST /notify)</span></li>
    <li><code>feedback</code> <span class="eff">&mdash; record a fixed-shape tap-to-rate feedback rating (POST /feedback)</span></li>
    <li><code>reload_confirm</code> <span class="eff">&mdash; confirm the showing reload view via tap (POST /reload/confirm)</span></li>
    <li><code>macbook_mouse</code> <span class="eff">&mdash; move the MacBook cursor to the tapped map point (POST /macbook/mouse)</span></li>
    <li><code>macbook_click</code> <span class="eff">&mdash; click the reviewed point on the magnified image (POST /macbook/click)</span></li>
    <li><code>talon_focus</code> <span class="eff">&mdash; focus the tapped header app chip (POST /talon/focus)</span></li>
    <li><code>macbook_mode</code> <span class="eff">&mdash; pin the merged macbook view to GLANCE or AIM (POST /macbook/mode)</span></li>
    <li><code>talon_tab</code> <span class="eff">&mdash; page the header app strip one window back/forward (POST /talon/tab)</span></li>
  </ul>
  <div class="meta">What a tap on the panel can do (touch bridge allowlist).</div>
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
let CURRENT = null;
// The big four stay pinned at the top of the one-tap grid so they need
// no hunt; every other renderer follows alphabetically. Broken entries
// sink to the end, disabled.
const PINNED = ["clock", "chat", "row", "stream"];
function orderedNames() {
  const names = Object.keys(SCHEMAS);
  const ok = names.filter((n) => !(SCHEMAS[n] && SCHEMAS[n].broken));
  const broken = names.filter((n) => SCHEMAS[n] && SCHEMAS[n].broken);
  const pinned = PINNED.filter((n) => ok.includes(n));
  const rest = ok.filter((n) => !PINNED.includes(n)).sort();
  return pinned.concat(rest, broken.sort());
}
function buildViewGrid() {
  const grid = document.getElementById("viewgrid");
  grid.innerHTML = "";
  for (const name of orderedNames()) {
    const r = SCHEMAS[name];
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = name;
    b.dataset.renderer = name;
    if (r && r.broken) {
      b.disabled = true;
      b.title = "broken: " + r.broken;
    } else {
      b.onclick = () => oneTapShow(name);
    }
    if (name === CURRENT) b.classList.add("active");
    grid.appendChild(b);
  }
}
function markCurrent() {
  for (const b of document.getElementById("viewgrid").children) {
    b.classList.toggle("active", b.dataset.renderer === CURRENT);
  }
  document.getElementById("tb-view").textContent = CURRENT || "(blank)";
}
async function oneTapShow(name) {
  try {
    await api("/show", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ renderer: name, params: {} }) });
    say("showing " + name);
    refreshState(); refreshPreview();
  } catch (err) { say("show failed: " + err.message, true); }
}
async function refreshState() {
  try {
    await api("/health");
    const s = await api("/state");
    document.getElementById("health").className = "dot ok";
    document.getElementById("healthtext").textContent =
      "healthy \u00b7 " + s.display.width + "x" + s.display.height +
      " \u00b7 " + Object.keys(SCHEMAS).length + " renderers";
    CURRENT = s.renderer || null;
    markCurrent();
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
    const d = s.deploy || {};
    const when = d.deployed ? (d.date || "unknown date") : "never recorded";
    document.getElementById("dep-when").textContent = when;
    document.getElementById("dep-sha").textContent =
      d.deployed ? (d.sha || "?") : "\u2013";
    document.getElementById("dep-who").textContent =
      d.deployed ? (d.deployer || "?") : "\u2013";
    document.getElementById("tb-dep").textContent =
      d.deployed ? ((d.sha || "?").slice(0, 7) + " \u00b7 " + when) : "never recorded";
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
  buildViewGrid();
  refreshFeedbackViews(keep);
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
async function refreshPlaylist() {
  try {
    const s = await api("/playlist");
    const p = (await api("/policy")).config.playlist;
    document.getElementById("pl-en").checked = !!p.enabled;
    document.getElementById("pl-place").value = p.placement;
    document.getElementById("pl-thick").value = p.thickness;
    document.getElementById("pl-dir").value = p.direction;
    document.getElementById("pl-color").value = p.color;
    const v = document.getElementById("pl-views");
    if (document.activeElement !== v) v.value = JSON.stringify(p.views);
    const vname = s.view ? s.view.renderer : "(none)";
    document.getElementById("pl-status").textContent =
      "playlist: " + (s.enabled ? ("on \u00b7 " + vname +
        (s.progress == null ? "" : (" \u00b7 " + Math.round(s.progress * 100) + "%"))) : "off") +
      (s.hold && s.hold !== "disabled" && s.hold !== "empty" ? (" \u00b7 held (" + s.hold + ")") : "") +
      (s.last_error ? (" \u00b7 error: " + s.last_error) : "");
  } catch (err) { say("playlist load failed: " + err.message, true); }
}
document.getElementById("plsave").onclick = async () => {
  let views;
  try {
    views = JSON.parse(document.getElementById("pl-views").value || "[]");
  } catch (err) { say("views is not valid JSON", true); return; }
  const patch = { playlist: {
    enabled: document.getElementById("pl-en").checked,
    placement: document.getElementById("pl-place").value,
    direction: document.getElementById("pl-dir").value,
    color: document.getElementById("pl-color").value.trim() || "#FFFFFF",
    views: views } };
  const t = document.getElementById("pl-thick").value.trim();
  if (t !== "") patch.playlist.thickness = Number(t);
  try { await api("/policy", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch) });
    say("playlist saved"); refreshPlaylist(); }
  catch (err) { say("playlist save failed: " + err.message, true); }
};
document.getElementById("plpause").onclick = async () => {
  try { await api("/playlist/pause", { method: "POST" }); say("playlist paused"); refreshPlaylist(); }
  catch (err) { say("pause failed: " + err.message, true); }
};
document.getElementById("plresume").onclick = async () => {
  try { await api("/playlist/resume", { method: "POST" }); say("playlist resumed"); refreshPlaylist(); }
  catch (err) { say("resume failed: " + err.message, true); }
};
document.getElementById("plnext").onclick = async () => {
  try { await api("/playlist/next", { method: "POST" }); say("skipped to next view"); refreshPlaylist(); }
  catch (err) { say("skip failed: " + err.message, true); }
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
document.getElementById("reload").onclick = async () => {
  const sha = document.getElementById("rl-sha").value.trim().toLowerCase();
  const box = document.getElementById("reload-result");
  if (!/^[0-9a-f]{40}$/.test(sha)) {
    say("reload needs the full 40-character commit SHA", true);
    return;
  }
  try {
    const out = await api("/reload", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sha: sha }) });
    box.innerHTML = "";
    box.appendChild(document.createTextNode(
      "reloaded \u00b7 returns in " + out.return_in + "s \u00b7 proof: "));
    const a = document.createElement("a");
    a.href = out.commit_url;
    a.textContent = out.commit_url;
    a.target = "_blank";
    a.rel = "noopener";
    box.appendChild(a);
    say("reload proof showing");
    refreshState(); refreshPreview();
  } catch (err) { say("reload failed: " + err.message, true); }
};
let PICKED_RATING = 0;
for (const b of document.querySelectorAll("#ratebtns button")) {
  b.onclick = () => {
    PICKED_RATING = Number(b.dataset.rating);
    for (const x of document.querySelectorAll("#ratebtns button")) {
      x.classList.toggle("picked", x === b);
    }
  };
}
function refreshFeedbackViews(keep) {
  const sel = document.getElementById("fb-view");
  const prev = keep ? sel.value : null;
  sel.innerHTML = "";
  for (const name of Object.keys(SCHEMAS).sort()) {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name;
    sel.appendChild(o);
  }
  if (prev && SCHEMAS[prev]) sel.value = prev;
  else if (CURRENT && SCHEMAS[CURRENT]) sel.value = CURRENT;
}
async function refreshFeedbackSummary() {
  const box = document.getElementById("fb-summary");
  try {
    const s = await api("/feedback/summary");
    box.innerHTML = "";
    const head = document.createElement("div");
    head.className = "meta";
    head.textContent = s.total === 0 ? "no ratings yet"
      : (s.total + " rating" + (s.total === 1 ? "" : "s"));
    box.appendChild(head);
    for (const [view, agg] of Object.entries(s.views || {})) {
      const line = document.createElement("div");
      line.className = "fbline";
      const left = document.createElement("span");
      left.textContent = view + " ×" + agg.count;
      const right = document.createElement("span");
      right.textContent = agg.avg_rating == null ? "–" : ("avg " + agg.avg_rating);
      line.appendChild(left);
      line.appendChild(right);
      box.appendChild(line);
    }
  } catch (err) { say("feedback summary failed: " + err.message, true); }
}
document.getElementById("fbsend").onclick = async () => {
  if (!PICKED_RATING) { say("pick a rating 1-5 first", true); return; }
  const body = { view: document.getElementById("fb-view").value,
    rating: PICKED_RATING,
    notes: document.getElementById("fb-notes").value,
    agent: "control-page" };
  try { await api("/feedback", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    say("rating recorded");
    PICKED_RATING = 0;
    for (const x of document.querySelectorAll("#ratebtns button")) {
      x.classList.remove("picked");
    }
    document.getElementById("fb-notes").value = "";
    refreshFeedbackSummary(); }
  catch (err) { say("rating failed: " + err.message, true); }
};
(async function init() {
  try { await refreshRenderers(false); }
  catch (err) { say("could not load renderers: " + err.message, true); }
  await refreshState();
  refreshPreview();
  refreshPolicy();
  refreshPlaylist();
  refreshFeedbackSummary();
  setInterval(refreshState, 2000);
  setInterval(refreshPreview, 2000);
  setInterval(refreshPlaylist, 2000);
  setInterval(async () => {
    try { await refreshRenderers(true); } catch (e) { /* next tick */ }
    refreshFeedbackSummary();
  }, 15000);
})();
</script>
</body>
</html>
"""


DAEMON = None


def _resolve_coords(body, width, height):
    """POST /touch/resolve coordinates -> (x, y) display pixels.

    Accepts display pixels {x, y} (ints) or normalized {x_norm, y_norm}
    floats in [0, 1] (same edge mapping as touch.normalize: 0 -> 0,
    1 -> size - 1). Explicit pixels win when both forms are present.
    Pure: raises ValueError naming the defect, changes nothing."""
    body = body if isinstance(body, dict) else {}
    if body.get("x") is not None or body.get("y") is not None:
        if body.get("x") is None or body.get("y") is None:
            raise ValueError("resolve needs both {x, y} pixels")
        return body.get("x"), body.get("y")
    xn, yn = body.get("x_norm"), body.get("y_norm")
    if xn is None or yn is None:
        raise ValueError("resolve needs {x, y} pixels or "
                         "{x_norm, y_norm} fractions")
    for value in (xn, yn):
        if isinstance(value, bool) or \
                not isinstance(value, (int, float)):
            raise ValueError("resolve fractions must be numbers")
        if not 0.0 <= value <= 1.0:
            raise ValueError("resolve fractions range 0..1, "
                             "got %r,%r" % (xn, yn))
    return int(round(xn * (width - 1))), int(round(yn * (height - 1)))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _check_token(self):
        """Return None when the request is authorized, else an error body.

        With no API_TOKEN configured the API stays open (historical
        behavior). When it is configured, the bearer token must match in
        constant time, and the compare runs even when the header is absent
        so a missing header does not return measurably faster than a wrong
        one."""
        if not API_TOKEN:
            return None
        header = self.headers.get("Authorization", "")
        provided = header[7:] if header.startswith("Bearer ") else ""
        if hmac.compare_digest(provided.encode(), API_TOKEN):
            return None
        return {"error": "unauthorized"}

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
        auth_error = self._check_token()
        if auth_error is not None:
            return self._send(401, auth_error)
        if path == "/state":
            return self._send(200, DAEMON.state())
        if path == "/version":
            return self._send(200, {"version": APP_VERSION})
        if path == "/renderers":
            return self._send(200, {"renderers": DAEMON.renderer_list()})
        if path == "/snapshot":
            png = DAEMON.snapshot()
            if png is None:
                return self._send(404, {"error": "nothing has been drawn yet"})
            return self._send(200, png, "image/png")
        if path == "/policy":
            return self._send(200, DAEMON.get_policy())
        if path == "/deploy":
            return self._send(200, DAEMON.deploy_info())
        if path.startswith("/r/"):
            # One-time scan relay: consume the token, 302 the scanner to
            # the commit page, and confirm the reload view when it still
            # shows. Observation like any GET: never touches the idle
            # clock. Only the exact /r/<token> shape routes here.
            token = path[len("/r/"):]
            if "/" in token or not token:
                return self._send(404, {"error": "not found"})
            status, payload = DAEMON.handle_relay_scan(token)
            if status == 302:
                body = json.dumps(payload).encode()
                self.send_response(302)
                self.send_header("Location", payload["commit_url"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._send(status, payload)
        if path == "/playlist":
            return self._send(200, DAEMON.playlist.status())
        if path == "/macbook/mouse":
            # Poller fetch path: ?since=<last acted ts> returns the
            # pending cursor command, or {"command": None} when there
            # is nothing new (or it TTL-expired). Read-only and
            # idempotent -- a retried fetch never double-fires.
            # Optional ?wait=<seconds> holds (bounded, see wait_command)
            # until a tap queues, so the poller wakes on the tap instead
            # of its next tick; absent/zero is today's immediate reply.
            query = parse_qs(urlsplit(self.path).query)
            raw = (query.get("since", [None])[0])
            wait = (query.get("wait", [None])[0])
            return self._send(200, {"command":
                                    DAEMON.wait_command("mouse",
                                                        raw, wait)})
        if path == "/macbook/click":
            # Poller fetch path: ?since=<last acted ts> returns the
            # pending click, or {"command": None} when there is
            # nothing new (or it TTL-expired). Read-only and
            # idempotent -- a retried fetch never double-fires.
            # Optional ?wait=<seconds> holds (bounded, see wait_command)
            # until a tap queues, so the poller wakes on the tap instead
            # of its next tick; absent/zero is today's immediate reply.
            query = parse_qs(urlsplit(self.path).query)
            raw = (query.get("since", [None])[0])
            wait = (query.get("wait", [None])[0])
            return self._send(200, {"command":
                                    DAEMON.wait_command("click",
                                                        raw, wait)})
        if path == "/layout":
            return self._send(200, {"layout": DAEMON.layout_state()})
        if path == "/touch/check":
            # Drawn-vs-live region assertion (see touch_audit.py): 200
            # when the per-view matrix agrees, 409 with the exact
            # differing rects/ids when drifted, 503 when the gate
            # itself is blind (unknown: no heartbeat yet; stale: the
            # heartbeat expired -- an alarm, not a pass). A GET: never
            # touches the idle clock.
            report = DAEMON.touch_check()
            if report.get("ok"):
                return self._send(200, report)
            if report.get("blind"):
                return self._send(503, report)
            return self._send(409, report)
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
        if path == "/talon/focus":
            # Mac-side poller fetch: the one pending focus command
            # newer than ?since=, else no command. Read-only; the
            # poller tracks what it already acted on.
            # Optional ?wait=<seconds> holds (bounded, see wait_command)
            # until a tap queues, so the poller wakes on the tap instead
            # of its next tick; absent/zero is today's immediate reply.
            query = parse_qs(urlsplit(self.path).query)
            raw = query.get("since", [None])[0]
            wait = query.get("wait", [None])[0]
            return self._send(200, {"command":
                                    DAEMON.wait_command("focus",
                                                        raw, wait)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        auth_error = self._check_token()
        if auth_error is not None:
            return self._send(401, auth_error)
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
        if path == "/reload":
            body = self._body()
            try:
                result = DAEMON.reload(body.get("sha"), body.get("duration"),
                                       body.get("highlights"))
            except KeyError as exc:
                return self._send(404, {"error": str(exc)})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(200, result)
        if path == "/touch/tap":
            # Touchscreen tap dismissal: clears only an active reload
            # transient (saved base view, or clock after a fresh restart).
            # Always 200 -- dismissing anything else is a harmless no-op.
            self._body()  # drained for keep-alive; no fields read
            return self._send(200, DAEMON.dismiss_reload())
        if path == "/touch/announce":
            # Touch-service heartbeat: the region set it is actually
            # dispatching. 200 + count/sha, or 400 naming the defect.
            # A machine heartbeat, not operator activity: the idle clock
            # is untouched.
            try:
                return self._send(200,
                                    DAEMON.announce_touch(self._body()))
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
        if path == "/touch/resolve":
            # Read-only tap question: region + semantic action the
            # CURRENT UI would resolve to, view/mode-gated like the
            # touch path. No dispatch, no state change, no idle-clock
            # touch. 400 on malformed coordinates; unknown-heartbeat
            # and refused-tap answers are 200 (correct answers, not
            # request errors).
            body = self._body()
            try:
                x, y = _resolve_coords(
                    body, DAEMON.screen.W, DAEMON.screen.H)
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
            try:
                return self._send(200, DAEMON.resolve_touch(x, y))
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
        if path == "/macbook/mouse":
            # Panel tap -> MacBook cursor + fullscreen zoom: touch.py
            # posts panel pixels (closed `macbook_mouse` action); the
            # Mac-side poller fetches the queued Quartz point via GET
            # below, and the showing view is re-pinned to AIM (the tap
            # is the zoom entry; no AIM button remains). 200 +
            # {ok: True, command, mode} on queue; 400 on malformed or
            # off-panel coordinates; 409 on any refusal (wrong view,
            # stale feed, tap outside the map) -- never a mis-move and
            # never a mode change on a miss.
            body = self._body()
            try:
                result = DAEMON.request_mouse_move(body.get("x"),
                                                   body.get("y"))
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
            if result.get("ok"):
                return self._send(200, result)
            return self._send(409, result)
        if path == "/macbook/click":
            # Panel second tap on the review image: touch.py posts
            # panel pixels (closed `macbook_click` action); the
            # Mac-side poller fetches the queued Quartz point via GET
            # above and posts one CG down+up pair. 200 + {ok: True,
            # command} on queue; 400 on malformed coordinates; 409 on
            # any refusal (wrong view, stale feed, no fresh capture,
            # tap off the reviewed point, cursor moved) -- never a
            # blind click.
            body = self._body()
            try:
                result = DAEMON.request_click_move(body.get("x"),
                                                   body.get("y"))
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
            if result.get("ok"):
                return self._send(200, result)
            return self._send(409, result)
        if path == "/talon/focus":
            # Panel tap on an app row: queue ONE focus command for the
            # Mac-side poller. View-gated on talon_apps showing plus a
            # fresh feed; a miss is a 409 refusal, never a view change.
            # The body carries panel pixels only -- the app name comes
            # from the feed, so a tap can only select a listed app.
            body = self._body()
            try:
                result = DAEMON.request_focus_move(body.get("x"),
                                                   body.get("y"))
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            if result.get("ok"):
                return self._send(200, result)
            return self._send(409, result)
        if path == "/macbook/mode":
            # Header control: pin GLANCE/AIM on the showing merged
            # feature, keeping the tab. 400 on a bad mode, 409 unless
            # macbook shows -- never a view change on a miss.
            body = self._body()
            try:
                result = DAEMON.request_mode_move(body.get("mode"))
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
            if result.get("ok"):
                return self._send(200, result)
            return self._send(409, result)
        if path == "/talon/tab":
            # Header stepper: page the app-strip window with a slide,
            # clamped at both ends. 400 on a bad direction, 409 on
            # any refusal (including a press at the first/last page).
            body = self._body()
            try:
                result = DAEMON.request_tab_step(
                    body.get("dir", body.get("direction")))
            except ValueError as exc:
                return self._send(400, {"ok": False,
                                         "error": str(exc)})
            if result.get("ok"):
                return self._send(200, result)
            return self._send(409, result)
        if path == "/reload/confirm":
            # Tap/scan confirm path for the reload view only: with no
            # reload showing this is a 409 miss, never a view change.
            body = self._body()
            result = DAEMON.confirm_reload(body.get("via", "tap"),
                                           body.get("token"))
            if result.get("confirmed"):
                return self._send(200, result)
            return self._send(409, result)
        if path == "/policy":
            try:
                return self._send(200, DAEMON.set_policy(self._body()))
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
        if path == "/playlist/pause":
            DAEMON.policy.note_api()
            DAEMON.playlist.pause()
            return self._send(200, DAEMON.playlist.status())
        if path == "/playlist/resume":
            DAEMON.policy.note_api()
            DAEMON.playlist.resume()
            return self._send(200, DAEMON.playlist.status())
        if path == "/playlist/next":
            DAEMON.policy.note_api()
            DAEMON.playlist.next()
            return self._send(200, DAEMON.playlist.status())
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
                # A bare /show exits layout mode: single-renderer
                # behaviour is unchanged when no layout is active.
                return self._send(200, DAEMON.show(name, body.get("params")))
            if path == "/layout":
                return self._send(200, DAEMON.set_layout(body))
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

    def do_DELETE(self):
        path = self.path.split("?")[0]
        auth_error = self._check_token()
        if auth_error is not None:
            return self._send(401, auth_error)
        if path == "/layout":
            return self._send(200, DAEMON.clear_layout())
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
    # Boot-time clear is housekeeping, not a manual choice: let a persisted
    # enabled playlist resume rotating (it restores the first view itself).
    DAEMON.playlist.boot()
    DAEMON.start_watchdog()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print("displayd v%s listening on %s:%d with %d renderer(s)"
          % (APP_VERSION, args.bind, args.port, len(DAEMON.renderers)))
    try:
        server.serve_forever()
    finally:
        DAEMON.fb.release_console()


if __name__ == "__main__":
    main()
