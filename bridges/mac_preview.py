"""Live per-display previews for the macbook map (runs on the MacBook).

Companion to the state poller (bridges/macos_state.py): captures each
active display with CGDisplayCreateImage, downscales to a bounded JPEG,
and POSTs to ``/feed/macbook/preview`` at ~1 Hz for the GLANCE map
(renderers/macbook.py ``preview`` input). Runs on its own daemon
thread so a slow capture can never stall the 2 Hz state loop.

Cost (measured 2026-09-29, MacBookPro 3456x2234): capture is
latency-bound -- one ffmpeg per capture, ~1.5 s wall per display
(device open dominates, only ~0.4 s CPU), wire ~9 KB per display at
480 px / q6 (~18 KB for a two-display set), far under one zoom review
frame per second. The panel decodes small JPEGs; the rest of the feed
is untouched (state stays 2 Hz JSON, zoom stays one-shot).

Scheduling (2026-09-29): the tick is STAGGERED -- one display per tick,
round-robin, POSTed as soon as that display's own capture lands -- so a
frame's delivery age is its own capture wall instead of its capture plus
the other display's (the live batched p95 measured ~2-2.5 s at two
displays, ~1.5 s of it that cross-display queueing). Ticks are halved
to 0.5 s so the per-display refresh rate is unchanged, and captures per
second do not rise either (one capture per tick), so per-set CPU stays
at-or-below the batched baseline. The POST carries every display's
last-known frame so the map never degrades to a box, and a set whose
bytes are unchanged skips the wire POST while the panel's freshness
budget allows (bridges/mac_preview_plan.py).

Capture runs OUT OF PROCESS: one ffmpeg per display per tick doing
capture+scale+JPEG to stdout, piped back (never temp files). Python
keeps enumeration, POST, and the wire format. This deletes the ObjC
leak class outright (2026-09-29: in-process CGDisplayCreateImage
leaked ~25 MB/s, ~600x the no-preview rate) instead of pool hygiene.
Stdlib + ffmpeg + PIL (PIL only reads JPEG dimensions, never pixel
buffers). No focus stealing.

Permission: pixel capture needs the Screen Recording grant on the
ffmpeg binary. Verified live 2026-09-29: /opt/homebrew/bin/ffmpeg
returns real pixels (480x310 JPEG, ~9 KB) from this machine, so the
grant is held -- no captain action needed. A hung capture (grant
revoked, display asleep, device contention) hits FFMPEG_TIMEOUT and
that display is skipped; the panel renders boxes + PREVIEW OFF:
never a 400, never blank.
"""

import argparse
import logging
import os
import sys
import threading
import time

# Sibling import: macos_state.py puts this directory on sys.path, a bare
# `python3 mac_preview.py` does not. Same guard the poller uses, so a
# flat deployed dir (or any cwd) resolves the plan module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mac_preview_plan as _plan  # noqa: E402  (path set just above)
from mac_preview_capture import (FFMPEG_BIN, FFMPEG_CANDIDATES,
                                 FFMPEG_MIN_BYTES, FFMPEG_TIMEOUT,
                                 FRAME_JPEG_CAP, PREVIEW_MAX_W,
                                 PREVIEW_QUALITY, encode, ffmpeg_cmd,
                                 ffmpeg_frame, ffmpeg_quality, frame_jpeg,
                                 resolve_ffmpeg)
from mac_preview_wire import post_preview, preview_doc

LOG = logging.getLogger("mac-preview")

# Seconds between preview ticks (one display per tick, round-robin).
PREVIEW_INTERVAL = 0.5


def active_displays():
    """[(display_id, x, y, w, h, main)] in Quartz points. Raises."""
    import Quartz
    _, ids, _ = Quartz.CGGetActiveDisplayList(8, None, None)
    main_id = Quartz.CGMainDisplayID()
    out = []
    for d in ids or []:
        b = Quartz.CGDisplayBounds(d)
        out.append((d, b.origin.x, b.origin.y,
                    b.size.width, b.size.height, d == main_id))
    return out


def capture_set(max_w=PREVIEW_MAX_W, quality=PREVIEW_QUALITY):
    """One preview set: [{display_index, w, h, jpeg}] (possibly partial).

    One bounded ffmpeg per display per tick (capture+scale+JPEG to
    stdout, pipe-only). Never raises: a display that refuses capture
    (grant revoked, display asleep, ffmpeg error) is SKIPPED, so the
    panel shows that display as a box instead of killing the whole set."""
    try:
        displays = active_displays()
    except Exception as err:
        LOG.debug("display list failed: %s", err)
        return []
    frames = []
    for index in range(len(displays or [])):
        try:
            got = ffmpeg_frame(index, max_w, quality)
            if got is None:
                continue
            data, w, h = got
            text = encode(data)
            if text is None:
                continue
            frames.append({"display_index": index, "w": w, "h": h,
                           "jpeg": text})
        except Exception as err:
            LOG.debug("display %d capture skipped: %s", index, err)
    return frames


def preview_loop(displayd_base, interval=PREVIEW_INTERVAL, stop=None):
    """Thread body: capture one display per tick, POST as soon as it lands.

    Staggered round-robin (mac_preview_plan.next_display_index): each
    tick captures ONE display with the whole encode budget and POSTs the
    merged set immediately, so a frame's delivery age is its own capture
    wall instead of the other display's too (halved vs the batched
    baseline). The POST carries every display's last-known frame, so the
    map never degrades to a box; a byte-identical set skips the wire
    POST while the panel's freshness budget allows. A failed capture
    drops that display, exactly like the batched baseline.
    Never raises out."""
    failures = 0
    turn = 0
    known = {}  # display_index -> last successfully encoded frame
    last_sig, last_post = "", 0.0
    while stop is None or not stop.is_set():
        t0 = time.monotonic()
        try:
            count = 0
            try:
                count = len(active_displays() or [])
            except Exception as err:
                LOG.debug("display list failed: %s", err)
            index = _plan.next_display_index(turn, count)
            if index is not None:
                turn += 1
                frame = None
                got = ffmpeg_frame(index)
                if got is not None:
                    text = encode(got[0])
                    if text is not None:
                        frame = {"display_index": index, "w": got[1],
                                 "h": got[2], "jpeg": text}
                known = _plan.merge_frames(known, index, frame, count)
                doc = preview_doc(_plan.frame_list(known))
                if doc is not None:
                    now = time.monotonic()
                    sig = _plan.frame_signature(doc["frames"])
                    # Next tick lands ~one capture wall + the nominal
                    # wait from now; the skip guard needs that estimate.
                    period = max(0.2, interval) + max(0.0, now - t0)
                    if _plan.may_skip_post(sig, last_sig, now, last_post,
                                           period):
                        pass  # identical bytes: leave the wire quiet
                    elif post_preview(displayd_base, doc):
                        failures = 0
                        last_sig, last_post = sig, now
                    else:
                        failures += 1
        except Exception as err:  # the state loop must never feel this
            failures += 1
            LOG.debug("preview tick failed: %s", err)
        wait = max(0.2, interval - (time.monotonic() - t0))
        if failures >= 5:
            wait = max(wait, 5.0)  # panel down: back off, keep trying
        if stop is not None:
            stop.wait(wait)
        else:
            time.sleep(wait)


def start(displayd_base, interval=PREVIEW_INTERVAL):
    """Launch the preview thread (daemon); the Thread, never None."""
    stop = threading.Event()
    thread = threading.Thread(target=preview_loop,
                              args=(displayd_base, interval, stop),
                              name="mac-preview", daemon=True)
    thread.stop_event = stop  # type: ignore[attr-defined]
    thread.start()
    return thread


def main(argv=None):
    ap = argparse.ArgumentParser(description="display previews -> displayd")
    ap.add_argument("--displayd", default=os.environ.get(
        "DISPLAYD_BASE", "http://100.81.88.113:8980"))
    ap.add_argument("--interval", type=float, default=PREVIEW_INTERVAL)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.once:
        import json
        doc = preview_doc(capture_set())
        if doc is None:
            print(None)
            return 1
        summary = {"ts": doc["ts"], "frames": [
            {"display_index": f["display_index"], "w": f["w"],
             "h": f["h"], "jpeg_chars": len(f["jpeg"])}
            for f in doc["frames"]]}
        print(json.dumps(summary, indent=2))
        return 0
    stop = threading.Event()
    try:
        preview_loop(args.displayd, max(0.5, float(args.interval)), stop)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
