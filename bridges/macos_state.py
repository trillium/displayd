#!/usr/bin/env python3
"""macOS state -> displayd bridge (poller, runs on the MacBook).

Tiny always-on observer: frontmost app (NSWorkspace, kept live by a 50ms
runloop spin each tick), focused window title (AX, ~0.5 ms) and bounds
(CGWindowList, layer-0), active display by window-centre containment, pointer location, Talon mode
from the indicator's state file (zero Talon-side cost) -- emitting bounded
JSON at ~2 Hz to ``POST /feed/macbook/state`` (``renderers/macbook.py``).

WIRE CONTRACT v1 (renderer + poller must not drift; tests validate every
payload against the renderer's INPUTS):
  {"ts": ..., "accessibility_trusted": true,
   "focus": {"app_name", "bundle_id", "pid", "window_title",
     "window_bounds": {"x","y","w","h"}, "display_index"},
   "mouse": {"x","y","display_index"}, "displays": [{"bounds","main"}],
   "talon": {"mode","microphone","muted"}}
Geometry is Quartz throughout (origin top-left of the menu-bar display, y
down; screens above show negative y). Absent fields are OMITTED, never
null. Without Accessibility trust, title/bounds vanish and the panel
renders app-only degraded mode. Titles are PII-adjacent: truncated and
redacted before leaving the machine. No credentials, no history, no
subprocesses, no focus stealing (pure observer APIs). Stdlib + PyObjC;
PyObjC imports live inside functions so pure helpers stay importable.
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
MOUSE_FETCH_TIMEOUT = 2.0  # command fetch must never slow the 2 Hz tick
MOUSE_TTL = 10.0  # mirrors the daemon slot: stale taps never fire
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
        bx, by, bw, bh = _num(b.get("x")), _num(b.get("y")), _num(b.get("w")), _num(b.get("h"))
        if None not in (bx, by, bw, bh) and bx <= x < bx + bw and by <= y < by + bh:
            return i
    return None


def window_bounds(windows, pid, ax_title=""):
    """Focused-window bounds for pid from one CG pass (layer-0 only).
    Matches the AX title, else the first named window, else the topmost
    window regardless of name (window names need Screen Recording; bounds
    and order do not). None only when pid has no windows at all -- the
    honest windowless case. Never raises."""
    fallback = top = None
    for w in windows or []:
        get = getattr(w, "get", None)  # NSDictionary is not a dict
        if get is None or get("kCGWindowLayer") != 0: continue
        try: wpid = int(get("kCGWindowOwnerPID"))
        except (TypeError, ValueError): wpid = 0
        if wpid != pid: continue
        b = get("kCGWindowBounds") or {}
        x, y, bw, bh = _num(b.get("X")), _num(b.get("Y")), _num(b.get("Width")), _num(b.get("Height"))
        if None in (x, y, bw, bh) or bw <= 0 or bh <= 0:
            continue
        bounds, name = {"x": x, "y": y, "w": bw, "h": bh}, get("kCGWindowName") or ""
        if top is None: top = bounds
        if ax_title and name == ax_title: return bounds
        if fallback is None and name: fallback = bounds
    return fallback if fallback is not None else top


def app_info(pid):
    """(name, bundle) for pid; ("", "") when unknown."""
    import AppKit
    try:
        apps = AppKit.NSWorkspace.sharedWorkspace().runningApplications()
    except Exception:
        return ("", "")
    for a in apps or []:
        try:
            match = int(a.processIdentifier()) == pid
        except Exception:
            continue
        if match: return (str(a.localizedName() or ""), str(a.bundleIdentifier() or ""))
    return ("", "")


def build_payload(ts=None, trusted=True, app_name="", bundle_id="", pid=0, title="", bounds=None, focus_display=None, mouse=None, mouse_display=None, displays=None, talon=None):
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
        data = json.load(open(path, encoding="utf-8"))
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


def poll():
    """One full snapshot. The runloop spin keeps NSWorkspace live: without
    it frontmostApplication goes stale in a runloop-less poller (verified
    2026-09-28: stuck on the launch-era app)."""
    import AppKit, Quartz, Foundation
    import ApplicationServices
    AppKit.NSRunLoop.currentRunLoop().runUntilDate_(Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05))
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    pid = int(app.processIdentifier())
    app_name, bundle = app_info(pid)
    if not app_name: app_name, bundle = str(app.localizedName() or "unknown"), str(app.bundleIdentifier() or "")
    info = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    trusted = bool(ApplicationServices.AXIsProcessTrusted())
    title, bounds = "", window_bounds(info, pid)
    if pid and trusted:
        try:
            ref = ApplicationServices.AXUIElementCreateApplication(pid)
            err, win = ApplicationServices.AXUIElementCopyAttributeValue(ref, "AXFocusedWindow", None)
            if not err and win is not None:
                e2, raw = ApplicationServices.AXUIElementCopyAttributeValue(win, "AXTitle", None)
                ax_title = raw if not e2 and isinstance(raw, str) else ""
                title = redact(ax_title, bundle)
                if ax_title: bounds = window_bounds(info, pid, ax_title)
        except Exception as err:
            LOG.debug("AX read failed: %s", err)
    _, ids, _ = Quartz.CGGetActiveDisplayList(8, None, None)
    main_id = Quartz.CGMainDisplayID()
    displays, mouse = [], None
    for d in ids or []:
        b = Quartz.CGDisplayBounds(d)
        displays.append((b.origin.x, b.origin.y, b.size.width, b.size.height, d == main_id))
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
                         mouse_display=mouse_display, displays=displays, talon=read_talon())


def make_sender(displayd_base):
    """POST one payload to /feed/macbook/state; True on success."""
    url = displayd_base.rstrip("/") + "/feed/macbook/state"

    def send(payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp: resp.read(1024)
        except Exception as err:
            LOG.info("displayd unreachable (%s); retrying", err)
            return False
        return True
    return send


def fetch_mouse_command(displayd_base, since=0.0):
    """Pending cursor command newer than `since`; None when idle.
    Best-effort: any failure means no move -- the tap simply does not
    fire, and the daemon TTL-expires it, so failure can never land the
    cursor somewhere unexpected."""
    url = (displayd_base.rstrip("/") + "/macbook/mouse"
           + "?since=%s" % since)
    try:
        with urllib.request.urlopen(url, timeout=MOUSE_FETCH_TIMEOUT) as resp:
            doc = json.loads(resp.read(4096).decode("utf-8", "replace"))
        cmd = doc.get("command") if isinstance(doc, dict) else None
        x, y, ts = float(cmd["x"]), float(cmd["y"]), float(cmd.get("ts", 0))
    except Exception as err:
        LOG.debug("mouse fetch skipped (%s)", err)
        return None
    if abs(x) > 100000 or abs(y) > 100000:
        LOG.warning("mouse command out of range ignored: %r", cmd)
        return None
    return {"x": x, "y": y, "ts": ts,
            "display_index": cmd.get("display_index")}


def warp_mouse(x, y):
    """Move the cursor to Quartz (x, y) in one atomic OS call.

    Direct CGWarpMouseCursorPosition, NOT Talon: Talon follows the OS
    cursor (verified 2026-09-28 -- a bare warp reads back identically
    through the Talon REPL), so there is no desync to avoid and no
    Talon-running dependency to add. Either the whole point lands or
    nothing does; raises on failure so the caller logs and skips."""
    import Quartz
    Quartz.CGWarpMouseCursorPosition((float(x), float(y)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="macOS state -> displayd")
    ap.add_argument("--displayd", default=os.environ.get("DISPLAYD_BASE", "http://100.81.88.113:8980"))
    ap.add_argument("--interval", type=float, default=float(os.environ.get("MACOS_STATE_INTERVAL", str(INTERVAL))))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.once:
        print(json.dumps(poll(), indent=2)[:4000])
        return 0
    send, interval = make_sender(args.displayd), min(max(float(args.interval), 0.25), 10.0)
    last_mouse_ts = time.time()  # only taps from now on ever fire
    while True:
        t0 = time.monotonic()
        try:
            send(poll())
        except Exception as err:  # never die on a bad tick
            LOG.warning("poll tick failed: %s", err)
        try:
            cmd = fetch_mouse_command(args.displayd, since=last_mouse_ts)
            if cmd is not None:
                last_mouse_ts = max(last_mouse_ts, cmd["ts"])
                warp_mouse(cmd["x"], cmd["y"])
                LOG.info("cursor -> (%.0f, %.0f)", cmd["x"], cmd["y"])
        except Exception as err:  # a failed warp moves nothing, by design
            LOG.warning("mouse move failed: %s", err)
        time.sleep(max(0.05, interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
