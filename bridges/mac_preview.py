"""Live per-display previews for the macbook map (runs on the MacBook).

Companion to the state poller (bridges/macos_state.py): captures each
active display with CGDisplayCreateImage, downscales to a bounded JPEG,
and POSTs to ``/feed/macbook/preview`` at ~1 Hz for the GLANCE map
(renderers/macbook.py ``preview`` input). Runs on its own daemon
thread so a slow capture can never stall the 2 Hz state loop.

Cost (measured 2026-09-29, MacBookPro 3456x2234 + external 3840x2160):
capture+downscale+JPEG ~350 ms per set, wire ~43 KB per set at
480 px / q60 -- roughly half one zoom review frame per second. The
panel decodes two small JPEGs per second; the rest of the feed is
untouched (state stays 2 Hz JSON, zoom stays one-shot).

Permission: pixel capture needs the Screen Recording grant on THIS
binary. Verified live 2026-09-29: CGDisplayCreateImage on the
LaunchAgent python3 returns real pixels (3456x2234), so the grant is
held -- no captain action needed. Without it every display is skipped
and the panel renders boxes + PREVIEW OFF: never a 400, never blank.

Stdlib + PyObjC + PIL (PIL only for the PNG->JPEG-style downscale,
same as mac_zoom). No subprocesses, no focus stealing.
"""

import argparse
import base64
import io
import logging
import os
import sys
import threading
import time
import urllib.request

LOG = logging.getLogger("mac-preview")

PREVIEW_MAX_W = 480  # downscale bound: map rects are ~800 px panel-side
PREVIEW_QUALITY = 60  # JPEG quality: readable text shapes, small wire
PREVIEW_INTERVAL = 1.0  # seconds between preview sets (~1 Hz)
# Raw-byte cap per frame: measured worst real screen ~19 KB; 40 KB is
# headroom for busy screens, still far under the schema ceiling (the
# b64 of this many bytes is at most ~54 KB against maxLength 56000).
FRAME_JPEG_CAP = 40000


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


def cg_to_pil(cg):
    """CGImage -> PIL RGBA. Raises on any failure (caller skips)."""
    import Quartz
    from PIL import Image
    w, h = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
    if not w or not h:
        raise ValueError("empty capture")
    bpr = Quartz.CGImageGetBytesPerRow(cg)
    raw = bytes(Quartz.CGDataProviderCopyData(
        Quartz.CGImageGetDataProvider(cg)))
    shot = Image.frombytes("RGBA", (w, h), raw, "raw", ("BGRA", bpr, 1))
    shot.load()
    return shot


def frame_jpeg(shot, max_w=PREVIEW_MAX_W, quality=PREVIEW_QUALITY,
               cap=FRAME_JPEG_CAP):
    """PIL image -> (jpeg bytes, w, h) downscaled to max_w. None over cap.

    Never raises (None on garbage): an unencodable frame is skipped,
    never a poisoned POST."""
    try:
        from PIL import Image  # noqa: F401 (ensures the encoder exists)
        w, h = shot.size
        if w <= 0 or h <= 0:
            return None
        small = shot.copy()
        small.thumbnail((int(max_w), int(max_w) * 4))
        out = io.BytesIO()
        small.convert("RGB").save(out, "JPEG", quality=int(quality))
        data = out.getvalue()
    except Exception:
        return None
    if not data or len(data) > cap:
        return None
    return (data, small.size[0], small.size[1])


def encode(data, cap=FRAME_JPEG_CAP):
    """JPEG bytes -> base64 text, or None when over cap. Never raises."""
    try:
        if not isinstance(data, (bytes, bytearray)) or not data:
            return None
        if len(data) > cap:
            return None
        return base64.b64encode(bytes(data)).decode("ascii")
    except Exception:
        return None


def capture_set(max_w=PREVIEW_MAX_W, quality=PREVIEW_QUALITY):
    """One preview set: [{display_index, w, h, jpeg}] (possibly partial).

    Never raises: a display that refuses capture (grant revoked,
    display asleep) is SKIPPED, so the panel shows that display as a
    box instead of killing the whole set."""
    try:
        import Quartz
    except Exception:
        return []
    try:
        displays = active_displays()
    except Exception as err:
        LOG.debug("display list failed: %s", err)
        return []
    frames = []
    for index, (did, _x, _y, _w, _h, _main) in enumerate(displays):
        try:
            cg = Quartz.CGDisplayCreateImage(did)
            if cg is None:
                continue
            enc = frame_jpeg(cg_to_pil(cg), max_w, quality)
            if enc is None:
                continue
            data, w, h = enc
            text = encode(data)
            if text is None:
                continue
            frames.append({"display_index": index, "w": w, "h": h,
                           "jpeg": text})
        except Exception as err:
            LOG.debug("display %d capture skipped: %s", index, err)
    return frames


def preview_doc(frames, now=None):
    """Frame list -> one /feed/macbook/preview document.

    None when there is nothing to show (no POST: the panel keeps
    boxes + PREVIEW OFF rather than ingesting an empty set)."""
    try:
        if not frames:
            return None
        return {"ts": float(now if now is not None else time.time()),
                "frames": list(frames)}
    except (TypeError, ValueError):
        return None


def post_preview(displayd_base, doc, timeout=5.0):
    """POST one preview document to the panel feed; True on success."""
    url = displayd_base.rstrip("/") + "/feed/macbook/preview"
    import json
    req = urllib.request.Request(url, data=json.dumps(doc).encode("utf-8"),
                                 headers={"Content-Type":
                                          "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1024)
    except Exception:
        return False
    return True


def preview_loop(displayd_base, interval=PREVIEW_INTERVAL, stop=None):
    """Thread body: capture + POST forever. Never raises out."""
    failures = 0
    while stop is None or not stop.is_set():
        t0 = time.monotonic()
        try:
            doc = preview_doc(capture_set())
            if doc is not None and post_preview(displayd_base, doc):
                failures = 0
            elif doc is not None:
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
