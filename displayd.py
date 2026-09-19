#!/usr/bin/env python3
"""displayd - a tiny content-agnostic display daemon.

Owns the physical screen of a headless Linux box and exposes it through a
small JSON API.  All content comes from renderer plugins dropped into
renderers/ -- the core knows nothing about any particular one.

API
  GET  /health                       liveness
  GET  /state                        what is showing + screen power
  GET  /renderers                    available renderers and their params
  GET  /snapshot                     PNG of the last presented frame
  POST /show    {"renderer":"name","params":{...}}
  POST /clear                        blank the screen to black
  POST /screen  {"power":"on"|"off"} also /screen/on and /screen/off
"""

import importlib.util
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

FB = "/dev/fb0"
FB_SYS = "/sys/class/graphics/fb0/"
BACKLIGHT_GLOB = "/sys/class/backlight"
PORT = int(os.environ.get("DISPLAYD_PORT", "8980"))
BIND = os.environ.get("DISPLAYD_BIND", "0.0.0.0")
RENDERER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "renderers")
VT = os.environ.get("DISPLAYD_VT", "/dev/tty1")

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
            if current and current > 0:
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
            restore = self.saved_brightness or (
                self._read_int(os.path.join(self.backlight, "max_brightness")) or 100
            )
            result["backlight"] = self.set_brightness(restore)
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

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.fb.present(img)

    def clear(self, background=(0, 0, 0)):
        self.fb.present(self.new_image(background))

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
            "static": getattr(mod, "STATIC", True),
        }
    return found


class DisplayDaemon:
    def __init__(self):
        self.fb = Framebuffer()
        self.screen = Screen(self.fb)
        self.renderers = load_renderers(RENDERER_DIR)
        self.lock = threading.Lock()
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

    # ---- content -------------------------------------------------------

    def _run(self, entry, params, stop):
        try:
            entry["module"].run(self.screen, params, stop)
        except Exception as err:
            self.last_error = "%s: %s" % (type(err).__name__, err)

    def show(self, name, params):
        entry = self.renderers.get(name)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % name)
        with self.lock:
            self._stop_locked()
            stop = threading.Event()
            self.stop_event = stop
            self.current = name
            self.started_at = time.time()
            self.last_error = None
            self.thread = threading.Thread(
                target=self._run, args=(entry, params or {}, stop), daemon=True
            )
            self.thread.start()
        return self.state()

    def clear(self):
        with self.lock:
            self._stop_locked()
            self.current = None
            self.started_at = None
        self.screen.clear()
        return self.state()

    def _stop_locked(self):
        if self.stop_event is not None:
            self.stop_event.set()
        self.thread = None
        self.stop_event = None

    # ---- screen power --------------------------------------------------

    def set_power(self, power):
        power = (power or "").lower()
        if power not in ("on", "off"):
            raise ValueError("power must be 'on' or 'off'")
        with self.lock:
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

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode() or "{}")
        except ValueError:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
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
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
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
    DAEMON = DisplayDaemon()
    DAEMON.clear()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print("displayd listening on %s:%d with %d renderer(s)" % (BIND, PORT, len(DAEMON.renderers)))
    try:
        server.serve_forever()
    finally:
        DAEMON.fb.release_console()


if __name__ == "__main__":
    main()
