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
  POST /clear                        blank the screen to black
  POST /screen  {"power":"on"|"off"} also /screen/on and /screen/off

The API has no authentication, so it listens on 127.0.0.1 by default.  Bind
wider only deliberately -- see --bind / --port (or DISPLAYD_BIND / DISPLAYD_PORT).
"""

import argparse
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
# The API is unauthenticated, so the default is loopback only: reaching it from
# another machine is a deliberate choice (--bind or DISPLAYD_BIND), and should be
# paired with a host firewall or a private network such as a VPN or tailnet.
PORT = int(os.environ.get("DISPLAYD_PORT", "8980"))
BIND = os.environ.get("DISPLAYD_BIND", "127.0.0.1")
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
(async function init() {
  try { await refreshRenderers(false); }
  catch (err) { say("could not load renderers: " + err.message, true); }
  await refreshState();
  refreshPreview();
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
    parser = argparse.ArgumentParser(description="displayd - API-driven display server")
    parser.add_argument("--bind", default=BIND,
                        help="address to listen on (default: %(default)s, loopback only)")
    parser.add_argument("--port", type=int, default=PORT,
                        help="TCP port to listen on (default: %(default)s)")
    args = parser.parse_args()
    DAEMON = DisplayDaemon()
    DAEMON.clear()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print("displayd listening on %s:%d with %d renderer(s)" % (args.bind, args.port, len(DAEMON.renderers)))
    try:
        server.serve_forever()
    finally:
        DAEMON.fb.release_console()


if __name__ == "__main__":
    main()
