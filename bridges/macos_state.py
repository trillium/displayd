#!/usr/bin/env python3
"""macOS state -> displayd bridge (poller, runs on the MacBook).

Tiny always-on observer: frontmost app (~0.07 ms), focused window title (AX, ~0.5 ms),
window bounds (CGWindowList, layer-0 only),
active display by window-centre containment, pointer location, Talon mode
from the indicator's state file (zero Talon-side cost) -- emitting bounded
JSON at ~2 Hz to ``POST /feed/macbook/state`` (``renderers/macbook.py``).

WIRE CONTRACT v1 (renderer + poller must not drift; tests validate every
payload against the renderer's INPUTS):
  {"ts": 1789800900.0, "accessibility_trusted": true,
   "focus": {"app_name": "WezTerm", "bundle_id": "com.github.wez.wezterm",
     "pid": 3067, "window_title": "[1/2] macbookpro: 2M",
     "window_bounds": {"x": 0, "y": -1049, "w": 1920, "h": 1049},
     "display_index": 1},
   "mouse": {"x": 464, "y": -283, "display_index": 1},
   "displays": [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117},
     "main": true}],
   "talon": {"mode": "command", "microphone": "RODE", "muted": false}}
Geometry is Quartz throughout (origin top-left of the menu-bar display, y
down; screens above show negative y). Absent fields are OMITTED, never
null -- the daemon rejects null for typed fields. Without Accessibility
trust, title/bounds vanish and the panel renders app-only degraded mode.
Titles are PII-adjacent: truncated, redacted before leaving the machine.
No credentials, no history, no subprocesses, no focus stealing (pure
observer APIs). Stdlib + PyObjC; PyObjC imports live inside poll() so
pure helpers stay importable without AppKit.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.request

LOG = logging.getLogger("macos-state-bridge")

INTERVAL, TITLE_CHARS, TIMEOUT = 0.5, 60, 5.0
TALON_STATE = os.path.expanduser("~/.talon/user/trillium_talon/"
    "trillium/plugin/mode_indicator/mode_indicator_state.json")
BUNDLE_DENY = frozenset({"com.1password.1password", "com.apple.keychainaccess"})
REDACT_RE = re.compile(r"bank|chase|ledger|1password|secret|token|passwd",
                       re.IGNORECASE)
REDACTED = "[redacted]"


def redact(title, bundle_id=""):
    """Truncate + deny-list one window title. Never raises."""
    if bundle_id in BUNDLE_DENY:
        return REDACTED
    text = title if isinstance(title, str) else ""
    text = text.strip()[:TITLE_CHARS]
    return REDACTED if REDACT_RE.search(text) else text


def _num(value):
    """CGWindowList answers X/Y as strings, W/H as numbers; coerce."""
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def containing(x, y, displays):
    """Index of the display containing (x, y), else None."""
    for i, d in enumerate(displays):
        b = d if isinstance(d, dict) else {}
        bx, by, bw, bh = _num(b.get("x")), _num(b.get("y")), \
            _num(b.get("w")), _num(b.get("h"))
        if None in (bx, by, bw, bh):
            continue
        if bx <= x < bx + bw and by <= y < by + bh:
            return i
    return None


def pick_window(windows, pid, ax_title=""):
    """Focused-window bounds from a CGWindowList dump.

    Layer-0 windows of pid only (layer-1500 Talon canvas and Window Server
    chrome pollute the list); prefer the name matching the AX title, else
    the first named window, else None. Never raises.
    """
    fallback = None
    for w in windows or []:
        get = getattr(w, "get", None)  # NSDictionary is not a dict: duck-type
        if get is None or get("kCGWindowOwnerPID") != pid \
                or get("kCGWindowLayer") != 0:
            continue
        b = get("kCGWindowBounds") or {}
        x, y, bw, bh = _num(b.get("X")), _num(b.get("Y")), \
            _num(b.get("Width")), _num(b.get("Height"))
        if None in (x, y, bw, bh) or bw <= 0 or bh <= 0:
            continue
        bounds, name = {"x": x, "y": y, "w": bw, "h": bh}, \
            get("kCGWindowName") or ""
        if ax_title and name == ax_title:
            return bounds
        if fallback is None and name:
            fallback = bounds
    return fallback


def build_payload(ts=None, trusted=True, app_name="", bundle_id="", pid=0,
                  title="", bounds=None, focus_display=None, mouse=None,
                  mouse_display=None, displays=None, talon=None):
    """One feed document. Absent values omitted, never null."""
    payload = {"ts": ts if ts is not None else time.time(),
               "accessibility_trusted": bool(trusted)}
    focus = {"app_name": str(app_name or "unknown")}
    if bundle_id:
        focus["bundle_id"] = str(bundle_id)[:128]
    if pid:
        focus["pid"] = int(pid)
    if title:
        focus["window_title"] = title
    if bounds:
        focus["window_bounds"] = dict(bounds)
    if focus_display is not None:
        focus["display_index"] = int(focus_display)
    payload["focus"] = focus
    if mouse is not None:
        m = {"x": int(mouse[0]), "y": int(mouse[1])}
        if mouse_display is not None:
            m["display_index"] = int(mouse_display)
        payload["mouse"] = m
    if displays:
        payload["displays"] = [
            {"bounds": {"x": int(d[0]), "y": int(d[1]),
                        "w": int(d[2]), "h": int(d[3])},
             "main": bool(d[4])} for d in displays]
    if talon:
        payload["talon"] = dict(talon)
    return payload


def read_talon(path=TALON_STATE):
    """Talon mode/mic from the indicator's state file. None on any error."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    mic = data.get("microphone")
    mic = mic if isinstance(mic, str) else ""
    talon = {"mode": str(data.get("mode") or "other")[:16], "muted": mic == "None"}
    if mic:
        talon["microphone"] = mic[:64]
    return talon


def poll(talon_path=TALON_STATE):
    """One full snapshot."""
    import AppKit, Quartz
    import ApplicationServices

    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    app_name, bundle = str(app.localizedName() or "unknown"), \
        str(app.bundleIdentifier() or "")
    pid = int(app.processIdentifier())
    trusted = bool(ApplicationServices.AXIsProcessTrusted())
    title, bounds = "", None
    if trusted:
        try:
            ref = ApplicationServices.AXUIElementCreateApplication(pid)
            err, win = ApplicationServices.AXUIElementCopyAttributeValue(
                ref, "AXFocusedWindow", None)
            if not err and win is not None:
                e2, raw = ApplicationServices.AXUIElementCopyAttributeValue(
                    win, "AXTitle", None)
                ax_title = raw if not e2 and isinstance(raw, str) else ""
                title = redact(ax_title, bundle)
                bounds = pick_window(Quartz.CGWindowListCopyWindowInfo(
                    Quartz.kCGWindowListOptionOnScreenOnly,
                    Quartz.kCGNullWindowID), pid, ax_title)
        except Exception as err:
            LOG.debug("AX/CG read failed: %s", err)
    _, ids, _ = Quartz.CGGetActiveDisplayList(8, None, None)
    main_id = Quartz.CGMainDisplayID()
    displays, mouse = [], None
    for d in ids or []:
        b = Quartz.CGDisplayBounds(d)
        displays.append((b.origin.x, b.origin.y, b.size.width,
                         b.size.height, d == main_id))
    try:
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        mouse = (loc.x, loc.y)
    except Exception as err:
        LOG.debug("mouse read failed: %s", err)
    quartz = [{"x": d[0], "y": d[1], "w": d[2], "h": d[3]} for d in displays]
    focus_display = containing(bounds["x"] + bounds["w"] / 2.0, bounds["y"] + bounds["h"] / 2.0, quartz) if bounds else None
    mouse_display = containing(mouse[0], mouse[1], quartz) if mouse is not None else None
    return build_payload(trusted=trusted, app_name=app_name, bundle_id=bundle,
                         pid=pid, title=title, bounds=bounds,
                         focus_display=focus_display, mouse=mouse,
                         mouse_display=mouse_display, displays=displays,
                         talon=read_talon(talon_path))


def make_sender(displayd_base):
    """POST one payload to /feed/macbook/state; True on success."""
    url = displayd_base.rstrip("/") + "/feed/macbook/state"

    def send(payload):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                resp.read(1024)
        except Exception as err:
            LOG.info("displayd unreachable (%s); retrying", err)
            return False
        return True
    return send


def main(argv=None):
    ap = argparse.ArgumentParser(description="macOS state -> displayd")
    ap.add_argument("--displayd", default=os.environ.get("DISPLAYD_BASE", "http://100.81.88.113:8980"))
    ap.add_argument("--interval", type=float, default=float(os.environ.get("MACOS_STATE_INTERVAL", str(INTERVAL))))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--talon-state", default=TALON_STATE)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.once:
        print(json.dumps(poll(args.talon_state), indent=2)[:4000])
        return 0
    send, interval = make_sender(args.displayd), min(max(float(args.interval), 0.25), 10.0)
    while True:
        t0 = time.monotonic()
        try:
            send(poll(args.talon_state))
        except Exception as err:  # never die on a bad tick
            LOG.warning("poll tick failed: %s", err)
        time.sleep(max(0.05, interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
