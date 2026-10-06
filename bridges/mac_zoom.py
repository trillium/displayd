"""The tap follow-through for the macbook view (runs on the MacBook).

Single concept: what happens after the bridge warps the cursor to a tapped
point -- the stage-1 ``position_hook`` capture around that point and the
stage-2 ``do_click`` that lands the reviewed click -- plus the bounded
ffmpeg review crop the stage-1 hook falls back to when the Talon
file-channel capture fails. The fallback rides the SAME avfoundation pixel
path as the live-verified preview thread, so one broken capture mechanism
can no longer starve the zoom feed stale (task-ax59w).

The three helpers it composes live next door: the crop rect in
mac_zoom_crop.py, the Talon capture with its size caps in
mac_zoom_capture.py, and the zoom/click documents in mac_zoom_wire.py.
They are re-exported here because the macos_state bridge and the tests
import this module by name.
"""

import io
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mac_zoom_capture import (CAPTURE_IMAGE, JPEG_CAP, JPEG_QUALITY,
                              PNG_CAP, capture, capture_id, comm_dir, encode)
from mac_zoom_crop import CROP_H, CROP_W, crop_for
from mac_zoom_wire import (CLICK_TTL, fetch_click_command, post_zoom,
                           zoom_doc)


LOG = logging.getLogger("mac-zoom")


# Review-capture fallback: bounded ffmpeg crop (mirrors the proven
# bridges/mac_preview.py subprocess path -- same binary, same Screen
# Recording grant, same avfoundation input flags, verified live
# 2026-09-29). The Talon file-channel capture stays primary (exact
# Quartz-point crop, no scale math); the ffmpeg crop fires only when
# Talon fails, so a broken Talon side degrades to a slightly coarser
# review instead of a stale zoom feed that 409-refuses every image tap
# (task-ax59w: zoom 490 s stale vs CLICK_FRESH 30 s). Pipe-only, never
# temp files, never in-process pixel buffers (that path leaked
# ~25 MB/s, task-gjw6e).
FFMPEG_BIN = os.environ.get("DISPLAYD_FFMPEG", "/opt/homebrew/bin/ffmpeg")
FFMPEG_REVIEW_TIMEOUT = 10.0
FFMPEG_MIN_BYTES = 128


def _ffmpeg_q(quality=JPEG_QUALITY):
    """PIL-style quality -> ffmpeg -q:v. Mirrors
    mac_preview.ffmpeg_quality (parity pinned by test); q70 maps to
    ~5, the review operating point."""
    try:
        q = int(quality)
    except (TypeError, ValueError):
        q = JPEG_QUALITY
    return max(2, min(20, (100 - max(0, min(100, q))) // 10 + 2))


def display_pixel_scales():
    """[(ox_pt, oy_pt, w_pt, h_pt, scale)] per display, CG order.

    scale maps Quartz points to avfoundation device pixels (2.0 on
    Retina). None when Quartz is unavailable (CI) or enumeration
    fails: the fallback needs device pixels, so no scale means no
    fallback -- the Talon path is unaffected."""
    try:
        import Quartz
        _, ids, _ = Quartz.CGGetActiveDisplayList(8, None, None)
        out = []
        for d in ids or []:
            b = Quartz.CGDisplayBounds(d)
            try:
                pw = int(Quartz.CGDisplayPixelsWide(d))
                scale = pw / float(b.size.width) if b.size.width else 0
            except Exception:
                scale = 0
            if scale <= 0:
                return None
            out.append((b.origin.x, b.origin.y,
                        b.size.width, b.size.height, scale))
        return out or None
    except Exception:
        return None


def ffmpeg_review_cmd(display_index, dev_rect, max_w=CROP_W,
                      quality=JPEG_QUALITY):
    """argv: one display -> cropped + scaled review JPEG on stdout.

    Input flags mirror mac_preview.ffmpeg_cmd exactly (same device,
    same pixel path the grant covers); only -vf gains the crop. The
    crop rect is DEVICE pixels snapped even (yuv420p refuses odd),
    scale keeps it even too. Pipe-only, never temp files."""
    try:
        x, y, w, h = (int(dev_rect["x"]) & ~1, int(dev_rect["y"]) & ~1,
                      int(dev_rect["w"]) & ~1, int(dev_rect["h"]) & ~1)
    except (KeyError, TypeError, ValueError):
        x = y = 0
        w = h = 0
    if w < 2 or h < 2:
        w, h = 2, 2
    return [FFMPEG_BIN, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-f", "avfoundation", "-framerate", "2",
            "-i", "Capture screen %d:none" % int(display_index),
            "-frames:v", "1",
            "-vf", "crop=%d:%d:%d:%d,scale=%d:-2" % (w, h, x, y,
                                                         int(max_w)),
            "-q:v", str(_ffmpeg_q(quality)),
            "-f", "mjpeg", "-"]


def ffmpeg_review_crop(x, y, displays, timeout=FFMPEG_REVIEW_TIMEOUT,
                       max_w=CROP_W):
    """Talon-independent review capture -> JPEG bytes, or None.

    Points in (Quartz, from the state feed), device pixels out (via
    live Quartz scales). Bounded subprocess, validated output (rc,
    floor, JPEG_CAP, PIL decodes). Never raises; None means post
    nothing, exactly like a Talon failure."""
    try:
        crop = crop_for(x, y, displays)
        if crop is None:
            return None
        scales = display_pixel_scales()
        if not scales:
            return None
        try:
            qx, qy = float(x), float(y)
        except (TypeError, ValueError):
            return None
        index = None
        for i, (ox, oy, w, h, _) in enumerate(scales):
            if ox <= qx < ox + w and oy <= qy < oy + h:
                index = i
                break
        if index is None:
            return None
        ox, oy, w_pt, h_pt, scale = scales[index]
        dev_w = int(round(w_pt * scale))
        dev_h = int(round(h_pt * scale))
        dx = int(round((crop["x"] - ox) * scale)) & ~1
        dy = int(round((crop["y"] - oy) * scale)) & ~1
        dw = int(round(crop["w"] * scale)) & ~1
        dh = int(round(crop["h"] * scale)) & ~1
        dx = min(max(dx, 0), max(dev_w - 2, 0))
        dy = min(max(dy, 0), max(dev_h - 2, 0))
        dw = min(max(dw, 2), max(dev_w - dx, 2))
        dh = min(max(dh, 2), max(dev_h - dy, 2))
        proc = subprocess.run(
            ffmpeg_review_cmd(index, {"x": dx, "y": dy,
                                      "w": dw, "h": dh},
                              max_w, JPEG_QUALITY),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout)
    except Exception:
        return None
    data = bytes(getattr(proc, "stdout", b"") or b"")
    if getattr(proc, "returncode", 1) != 0 or len(data) < FFMPEG_MIN_BYTES:
        return None
    if len(data) > JPEG_CAP:
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            w, h = im.size
        if not w or not h:
            return None
    except Exception:
        return None
    return data


def position_hook(displayd_base, directory, state, x, y, now=None,
                  talon_timeout=4.0):
    """Stage-1 follow-through: capture around (x, y), POST zoom.
    True when posted; never raises (failure posts nothing).

    Talon file-channel capture is primary; a bounded ffmpeg crop
    (same pixel path as the live-verified preview thread) fires only
    when Talon fails, so one broken capture mechanism can no longer
    starve the zoom feed stale past CLICK_FRESH."""
    try:
        now = time.time() if now is None else float(now)
        displays = (state.get("displays") if isinstance(state, dict)
                    else None) or []
        crop = crop_for(x, y, displays)
        if crop is None:
            return False
        data = None
        try:
            data = capture(directory, crop, timeout=talon_timeout)
        except Exception as err:
            LOG.warning("talon review capture failed (%s); "
                        "ffmpeg fallback", err)
        if data is None:
            data = ffmpeg_review_crop(x, y, displays)
            if data is None:
                LOG.warning("ffmpeg review capture failed; "
                            "no zoom posted")
            else:
                LOG.info("review capture posted via ffmpeg fallback")
        text = encode(data) if data else None
        doc = zoom_doc(x, y, text, now)
        return bool(doc) and post_zoom(displayd_base, doc)
    except Exception:
        return False


def do_click(x, y):
    """One left click at Quartz (x, y): down+up posted atomically.

    Direct CGEventPost, NOT Talon (the panel's own second-tap commit).
    Raises on failure (clicks nothing); range-checked before the
    Quartz import so the refusal is testable without PyObjC."""
    try:
        qx, qy = float(x), float(y)
    except (TypeError, ValueError):
        raise ValueError("click coordinates must be numbers: %r,%r"
                         % (x, y))
    if abs(qx) > 100000 or abs(qy) > 100000:
        raise ValueError("click out of range: %r,%r" % (x, y))
    import Quartz
    point = (qx, qy)
    for kind in (Quartz.kCGEventLeftMouseDown,
                 Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, kind, point,
                                               Quartz.kCGMouseButtonLeft)
        if event is None:
            raise RuntimeError("CGEventCreateMouseEvent failed")
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
