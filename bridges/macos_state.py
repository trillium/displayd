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
redacted before leaving the machine. No credentials, no history,
no focus stealing (pure observer APIs). The preview thread shells one
bounded ffmpeg per display per tick (capture+scale+JPEG piped to
stdout, never temp files); the state poller itself spawns nothing.
Stdlib + PyObjC;
PyObjC imports live inside functions so pure helpers stay importable.
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request

LOG = logging.getLogger("macos-state-bridge")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import macos_state_format as _format

try:
    import mac_zoom
except Exception:  # zoom/click unavailable: state polling continues
    mac_zoom = None

try:
    import mac_preview
except Exception:  # previews unavailable: boxes stay, state continues
    mac_preview = None

# Re-exported for backwards compatibility (pure feed-document shaping
# lives in macos_state_format.py; the live poller below is the only
# in-repo consumer besides the tests).
TITLE_CHARS = _format.TITLE_CHARS
TALON_STATE = _format.TALON_STATE
BUNDLE_DENY = _format.BUNDLE_DENY
REDACT_RE = _format.REDACT_RE
REDACTED = _format.REDACTED
redact = _format.redact
_num = _format._num
containing = _format.containing
window_bounds = _format.window_bounds
build_payload = _format.build_payload
read_talon = _format.read_talon

INTERVAL, TIMEOUT = 0.5, 5.0
MOUSE_FETCH_TIMEOUT = 2.0  # command fetch must never slow the 2 Hz tick
MOUSE_TTL = 10.0  # mirrors the daemon slot: stale taps never fire


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


def poll():
    """One full snapshot. The runloop spin keeps NSWorkspace live: without
    it frontmostApplication goes stale in a runloop-less poller (verified
    2026-09-28: stuck on the launch-era app)."""
    import AppKit, Quartz, Foundation
    import ApplicationServices
    AppKit.NSRunLoop.currentRunLoop().runUntilDate_(Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05))
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        # No GUI session (shell-run bridge, console without a login
        # session): degrade to app-unknown instead of dying, so the tick
        # still exercises enumeration + preview + wire format. pid 0 is
        # omitted by build_payload; -1 matches no window (malformed
        # entries coerce to owner 0, never -1).
        pid, app_name, bundle = 0, "unknown", ""
    else:
        pid = int(app.processIdentifier())
        app_name, bundle = app_info(pid)
        if not app_name: app_name, bundle = str(app.localizedName() or "unknown"), str(app.bundleIdentifier() or "")
    info = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    trusted = bool(ApplicationServices.AXIsProcessTrusted())
    title, bounds = "", window_bounds(info, pid if pid else -1)
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
    ap.add_argument("--no-preview", action="store_true",
                    help="skip the live display-preview thread")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.once:
        print(json.dumps(poll(), indent=2)[:4000])
        return 0
    send, interval = make_sender(args.displayd), min(max(float(args.interval), 0.25), 10.0)
    if mac_preview is not None and not args.no_preview:
        try:
            preview_interval = max(0.5, float(os.environ.get(
                "MACOS_PREVIEW_INTERVAL",
                str(mac_preview.PREVIEW_INTERVAL))))
            mac_preview.start(args.displayd, preview_interval)
            LOG.info("display previews on (~%.1f Hz -> /feed/macbook/preview)",
                       1.0 / preview_interval)
        except Exception as err:  # previews are optional; state is not
            LOG.warning("preview thread failed to start: %s", err)
    last_mouse_ts = time.time()  # only taps from now on ever fire
    last_click_ts = time.time()
    while True:
        t0 = time.monotonic()
        state = None
        try:
            state = poll()
            send(state)
        except Exception as err:  # never die on a bad tick
            LOG.warning("poll tick failed: %s", err)
        try:
            cmd = fetch_mouse_command(args.displayd, since=last_mouse_ts)
            if cmd is not None:
                last_mouse_ts = max(last_mouse_ts, cmd["ts"])
                warp_mouse(cmd["x"], cmd["y"])
                LOG.info("cursor -> (%.0f, %.0f)", cmd["x"], cmd["y"])
                if mac_zoom is not None and mac_zoom.position_hook(
                        args.displayd, mac_zoom.comm_dir(),
                        state, cmd["x"], cmd["y"]):
                    LOG.info("review capture posted")
        except Exception as err:  # a failed warp moves nothing, by design
            LOG.warning("mouse move failed: %s", err)
        try:
            if mac_zoom is not None:
                cmd = mac_zoom.fetch_click_command(args.displayd,
                                                   since=last_click_ts)
                if cmd is not None:
                    last_click_ts = max(last_click_ts, cmd["ts"])
                    mac_zoom.do_click(cmd["x"], cmd["y"])
                    LOG.info("click -> (%.0f, %.0f)", cmd["x"], cmd["y"])
        except Exception as err:  # a failed click clicks nothing
            LOG.warning("mouse click failed: %s", err)
        time.sleep(max(0.05, interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
