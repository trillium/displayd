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

LOG = logging.getLogger("macos-state-bridge")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import macos_state_format as _format
from macos_observe import app_info, poll
from macos_wire import (LONGPOLL_WAIT_MAX, fetch_mouse_command, make_sender,
                        warp_mouse)

try:
    import mac_zoom
except Exception:  # zoom/click unavailable: state polling continues
    mac_zoom = None

try:
    import mac_preview
except Exception:  # previews unavailable: boxes stay, state continues
    mac_preview = None

# Re-exported for backwards compatibility (pure feed-document shaping
# lives in macos_state_format.py; the live observers in macos_observe.py,
# the wire half in macos_wire.py; this file owns the forgiving companion
# guards and the 2 Hz tick loop).
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

INTERVAL = 0.5


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
    # Long-poll hold matches the tick: loop cadence (and its per-tick
    # state POST) is preserved while taps wake the fetches at once.
    wait = min(interval, LONGPOLL_WAIT_MAX)
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
        acted = False
        state = None
        try:
            state = poll()
            send(state)
        except Exception as err:  # never die on a bad tick
            LOG.warning("poll tick failed: %s", err)
        try:
            cmd = fetch_mouse_command(args.displayd, since=last_mouse_ts,
                                      wait=wait)
            if cmd is not None:
                last_mouse_ts = max(last_mouse_ts, cmd["ts"])
                acted = True
                warp_mouse(cmd["x"], cmd["y"])
                LOG.info("cursor -> (%.0f, %.0f)", cmd["x"], cmd["y"])
                if mac_zoom is not None and mac_zoom.position_hook(
                        args.displayd, mac_zoom.comm_dir(),
                        state, cmd["x"], cmd["y"]):
                    LOG.info("review capture posted")
                elif mac_zoom is not None:
                    # Silence here is how the zoom feed starved stale
                    # (task-ax59w): a failed capture must be loud, so
                    # the next ladder starts from the Mac log, not a
                    # 490 s-old feed.
                    LOG.warning("review capture failed; "
                                "zoom feed going stale")
        except Exception as err:  # a failed warp moves nothing, by design
            LOG.warning("mouse move failed: %s", err)
        try:
            if mac_zoom is not None:
                cmd = mac_zoom.fetch_click_command(args.displayd,
                                                   since=last_click_ts,
                                                   wait=wait)
                if cmd is not None:
                    last_click_ts = max(last_click_ts, cmd["ts"])
                    mac_zoom.do_click(cmd["x"], cmd["y"])
                    LOG.info("click -> (%.0f, %.0f)", cmd["x"], cmd["y"])
                    acted = True
        except Exception as err:  # a failed click clicks nothing
            LOG.warning("mouse click failed: %s", err)
        # After acting on a tap the next hold re-parks at once (0.05
        # floor), so a back-to-back tap wakes instead of riding out the
        # tick sleep; idle ticks keep their cadence (state POSTs first).
        if acted:
            time.sleep(0.05)
        else:
            time.sleep(max(0.05, interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
