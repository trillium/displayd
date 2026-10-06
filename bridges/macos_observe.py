"""Reading the live macOS state that the macbook view renders.

Single concept: one snapshot of the machine's live state -- frontmost app,
focused window title (redacted) and bounds, active displays, pointer
location, and Talon mode -- assembled into the wire-contract payload
macos_state_format.build_payload produces. PyObjC imports stay inside the
functions, so this module is importable on a machine with no Quartz.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from macos_state_format import (build_payload, containing, read_talon,
                               redact, window_bounds)

LOG = logging.getLogger("macos-state-bridge")


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
